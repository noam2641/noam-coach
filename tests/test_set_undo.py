"""Tests for undo_last_set — P1: immediate undo of a logged set."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import coach_bot

_PLAN = {
    "name": "Test",
    "exercises": [
        {"id": "squat", "name": "סקוואט", "sets": 3, "rmin": 5, "rmax": 8,
         "inc": 2.5, "rest": 120, "weight": 60, "cues": ["גב ישר"]},
        {"id": "bench", "name": "לחיצת חזה", "sets": 3, "rmin": 5, "rmax": 8,
         "inc": 2.5, "rest": 120, "weight": 50, "cues": ["שכמות אחורה"]},
    ],
}


async def _session_with_one_set(tmp_path: Path, monkeypatch) -> tuple[coach_bot.Database, int]:
    db = coach_bot.Database(str(tmp_path / "undo.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (coach_bot.utc_now(),),
    )
    monkeypatch.setattr(coach_bot, "DB", db)
    # Active session positioned on exercise 0, set 2 (one set already logged).
    session_id = await db.execute(
        "INSERT INTO sessions(user_id, code, name, plan, status, exercise_index, set_number, started_at) "
        "VALUES(1,'T','Test',?, 'active', 0, 2, ?)",
        (json.dumps(_PLAN, ensure_ascii=False), coach_bot.utc_now()),
    )
    await db.execute(
        "INSERT INTO sets(session_id, exercise_id, exercise_name, set_number, weight, reps, rir, source, created_at) "
        "VALUES(?, 'squat', 'סקוואט', 1, 60, 8, ?, 'telegram_one_tap', ?)",
        (session_id, coach_bot.RIR_UNKNOWN, coach_bot.utc_now()),
    )
    return db, int(session_id)


#: A plan that programs the SAME movement twice -- a superset, or one exercise
#: placed early and repeated late. Entirely legitimate, and it breaks any
#: attempt to recover a position from the exercise id alone.
_PLAN_WITH_REPEAT = {
    "name": "Repeat",
    "exercises": [
        {"id": "squat", "name": "סקוואט", "sets": 3, "rmin": 5, "rmax": 8,
         "inc": 2.5, "rest": 120, "weight": 60, "cues": []},
        {"id": "bench", "name": "לחיצת חזה", "sets": 3, "rmin": 5, "rmax": 8,
         "inc": 2.5, "rest": 120, "weight": 50, "cues": []},
        {"id": "squat", "name": "סקוואט", "sets": 2, "rmin": 8, "rmax": 12,
         "inc": 2.5, "rest": 90, "weight": 40, "cues": []},
    ],
}


@pytest.mark.asyncio
async def test_undo_rewinds_to_the_occurrence_that_was_performed(
    tmp_path, monkeypatch
) -> None:
    """Undo must return to where the set was performed, not to a namesake.

    `sets` stores `exercise_id` and no position, so undo reverse-mapped the id
    back to a plan index with `next(...)`. That returns the FIRST match. In a
    plan that programs one movement twice, undoing a set of the second
    occurrence rewound the pointer to the first -- teleporting the user
    backwards through a session they had already worked past, and re-presenting
    an exercise they had finished.

    The two questions are not the same: reverse-mapping asks "where does this id
    live now", while the pointer needs "where was the user when this was
    logged". Only a position recorded at write time answers the second.
    """
    db = coach_bot.Database(str(tmp_path / "repeat.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (coach_bot.utc_now(),),
    )
    monkeypatch.setattr(coach_bot, "DB", db)

    # The user is on the SECOND squat (index 2) and has logged its first set.
    session_id = await db.execute(
        "INSERT INTO sessions(user_id, code, name, plan, status, exercise_index, set_number, started_at) "
        "VALUES(1,'T','Repeat',?, 'active', 2, 2, ?)",
        (json.dumps(_PLAN_WITH_REPEAT, ensure_ascii=False), coach_bot.utc_now()),
    )
    await db.execute(
        "INSERT INTO sets(session_id, exercise_id, exercise_name, set_number, weight, reps, rir, "
        "source, exercise_index, created_at) "
        "VALUES(?, 'squat', 'סקוואט', 1, 40, 10, ?, 'telegram_one_tap', 2, ?)",
        (session_id, coach_bot.RIR_UNKNOWN, coach_bot.utc_now()),
    )

    assert await coach_bot.undo_last_set(1, int(session_id)) is True

    session = await db.fetch_one("SELECT * FROM sessions WHERE id=?", (session_id,))
    assert session["exercise_index"] == 2, (
        "undo rewound to the first exercise sharing this id, not the one performed"
    )
    assert session["set_number"] == 1


@pytest.mark.asyncio
async def test_undo_falls_back_to_reverse_mapping_for_legacy_rows(
    tmp_path, monkeypatch
) -> None:
    """Rows written before the column existed carry NULL and must still undo.

    The migration adds `exercise_index` as nullable with no backfill, because
    the position a historical set was performed at is not recoverable. Those
    rows keep the old reverse-mapping behaviour, which is correct whenever the
    plan holds the exercise exactly once -- the common case.
    """
    db, session_id = await _session_with_one_set(tmp_path, monkeypatch)
    row = await db.fetch_one("SELECT exercise_index FROM sets WHERE session_id=?", (session_id,))
    assert row["exercise_index"] is None, "seeded row must model a pre-migration set"

    assert await coach_bot.undo_last_set(1, session_id) is True

    session = await db.fetch_one("SELECT * FROM sessions WHERE id=?", (session_id,))
    assert session["exercise_index"] == 0
    assert session["set_number"] == 1


@pytest.mark.asyncio
async def test_undo_removes_set_and_rewinds_pointer(tmp_path, monkeypatch) -> None:
    db, session_id = await _session_with_one_set(tmp_path, monkeypatch)
    assert await coach_bot.undo_last_set(1, session_id) is True
    remaining = await db.fetch_all("SELECT * FROM sets WHERE session_id=?", (session_id,))
    assert remaining == []
    session = await db.fetch_one("SELECT * FROM sessions WHERE id=?", (session_id,))
    assert session["set_number"] == 1  # rewound to where the set was performed
    assert session["status"] == "active"


@pytest.mark.asyncio
async def test_undo_with_no_sets_returns_false(tmp_path, monkeypatch) -> None:
    db = coach_bot.Database(str(tmp_path / "empty.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (coach_bot.utc_now(),),
    )
    monkeypatch.setattr(coach_bot, "DB", db)
    session_id = await db.execute(
        "INSERT INTO sessions(user_id, code, name, plan, status, exercise_index, set_number, started_at) "
        "VALUES(1,'T','Test',?, 'active', 0, 1, ?)",
        (json.dumps(_PLAN, ensure_ascii=False), coach_bot.utc_now()),
    )
    assert await coach_bot.undo_last_set(1, int(session_id)) is False


@pytest.mark.asyncio
async def test_undo_reactivates_completed_session(tmp_path, monkeypatch) -> None:
    db, session_id = await _session_with_one_set(tmp_path, monkeypatch)
    # Simulate the session auto-completing on the final set.
    await db.execute(
        "UPDATE sessions SET status='completed', ended_at=? WHERE id=?",
        (coach_bot.utc_now(), session_id),
    )
    assert await coach_bot.undo_last_set(1, session_id) is True
    session = await db.fetch_one("SELECT * FROM sessions WHERE id=?", (session_id,))
    assert session["status"] == "active"
    assert session["ended_at"] is None


@pytest.mark.asyncio
async def test_split_set_rows_get_distinct_client_event_ids(tmp_path, monkeypatch) -> None:
    db = coach_bot.Database(str(tmp_path / "split_event_ids.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (coach_bot.utc_now(),),
    )
    monkeypatch.setattr(coach_bot, "DB", db)
    import noam_coach.bot.workout_runtime as workout_runtime

    monkeypatch.setattr(workout_runtime, "DB", db)
    session_id = await db.execute(
        "INSERT INTO sessions(user_id, code, name, plan, status, exercise_index, set_number, started_at) "
        "VALUES(1,'T','Test',?, 'active', 0, 1, ?)",
        (json.dumps(_PLAN, ensure_ascii=False), coach_bot.utc_now()),
    )
    session = await db.fetch_one("SELECT * FROM sessions WHERE id=?", (session_id,))

    completed, _rest = await workout_runtime.save_split_set(
        session,
        60,
        5,
        55,
        4,
        2,
        client_event_id="client-abc",
    )

    assert completed is False
    rows = await db.fetch_all(
        "SELECT source, client_event_id FROM sets WHERE session_id=? ORDER BY id",
        (session_id,),
    )
    assert [row["source"] for row in rows] == ["telegram_split_primary", "telegram_split_secondary"]
    assert [row["client_event_id"] for row in rows] == [
        "client-abc:primary",
        "client-abc:secondary",
    ]
