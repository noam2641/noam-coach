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
