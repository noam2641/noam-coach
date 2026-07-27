"""Skipping an exercise and reporting pain must leave a trace.

Both mutate a live workout and neither was observable. In the 2026-07-26
session a skip at 20:42:33 produced no event whatsoever, so an exercise
vanished from the workout with nothing recording that it had been skipped or
which one it was. A pain report at 20:41:49 reached medical_constraints and
audit but emitted no domain event, so the canonical event stream -- the one
trace_reader and session_review read -- could not reconstruct it.

Note on sessions.pain_location: it reads NULL after a successful report by
design. `painloc` writes the location, then `painlevel` clears it back to
NULL as a transactional claim token before inserting the constraint. NULL is
the SUCCESS state here, not evidence of a lost write.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import coach_bot
from db import Database
from helpers import utc_now
from noam_coach.bot import callback_session as callback_session_bot
from noam_coach.bot import meal_text as meal_text_bot
from noam_coach.bot import ui as ui_bot
from noam_coach.bot import workout as workout_bot


class FakeMessage:
    chat_id = 1
    message_id = 555

    def __init__(self) -> None:
        self.texts: list[str] = []

    async def reply_text(
        self, text: str, reply_markup: Any = None, parse_mode: str | None = None
    ) -> None:
        del reply_markup, parse_mode
        self.texts.append(text)


class FakeQuery:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.answers: list[str | None] = []
        self.message = FakeMessage()

    async def edit_message_text(
        self, text: str, reply_markup: Any = None, parse_mode: str | None = None
    ) -> None:
        del reply_markup, parse_mode
        self.messages.append(text)

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        del show_alert
        self.answers.append(text)


class FakeJobQueue:
    def __init__(self) -> None:
        self.scheduled: list[dict[str, Any]] = []

    def run_repeating(self, callback: Any, **kwargs: Any) -> None:
        del callback
        self.scheduled.append(kwargs)

    def get_jobs_by_name(self, name: str) -> tuple[Any, ...]:
        del name
        return ()


def _ctx() -> Any:
    return type("Ctx", (), {"bot": object(), "job_queue": FakeJobQueue()})()


async def _make_db(tmp_path: Path, name: str) -> Database:
    db = Database(str(tmp_path / f"{name}.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1, 'Test', NULL, ?)",
        (utc_now(),),
    )
    return db


def _bind(monkeypatch: pytest.MonkeyPatch, db: Database) -> None:
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(ui_bot, "DB", db)
    monkeypatch.setattr(workout_bot, "DB", db)
    monkeypatch.setattr(meal_text_bot, "DB", db)
    monkeypatch.setattr(callback_session_bot, "DB", db)


def _ex(exercise_id: str = "db_bench", name: str = "לחיצת חזה") -> dict[str, Any]:
    return {
        "id": exercise_id,
        "name": name,
        "sets": 3,
        "rmin": 8,
        "rmax": 12,
        "rest": 90,
        "weight": 50.0,
        "inc": 2.5,
        "cues": ["גב צמוד"],
        "alts": [],
        "muscle": "חזה",
    }


async def _start_session(
    db: Database, *, exercises: list[dict[str, Any]] | None = None
) -> int:
    plan = {"exercises": exercises or [_ex(), _ex("db_row", "חתירה")]}
    return int(
        await db.execute(
            """
            INSERT INTO sessions(
                user_id, code, name, plan, status, exercise_index, set_number,
                pending_weight, pending_reps, started_at
            ) VALUES(1, 'A', 'אימון A', ?, 'active', 0, 1, NULL, NULL, ?)
            """,
            (json.dumps(plan, ensure_ascii=False), utc_now()),
        )
    )


async def _session_row(db: Database, session_id: int) -> dict[str, Any]:
    return dict(await db.fetch_one("SELECT * FROM sessions WHERE id=?", (session_id,)))


async def _tap(db: Database, session_id: int, action: str) -> FakeQuery:
    session = await _session_row(db, session_id)
    query = FakeQuery()
    await coach_bot.handle_session_action_callback(
        query,
        _ctx(),
        1,
        f"{action}:{session_id}:{session['exercise_index']}:{session['set_number']}",
    )
    return query


async def _session_events(db: Database, action: str) -> list[dict[str, Any]]:
    """State-mutation events this suite cares about, decoded."""
    rows = await db.fetch_all(
        "SELECT * FROM product_events WHERE user_id=1 ORDER BY id"
    )
    found = []
    for row in rows:
        try:
            props = json.loads(row["properties"] or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if props.get("domain") == "workout_session" and props.get("action") == action:
            found.append(props)
    return found


@pytest.fixture(autouse=True)
def _allow_everyone(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _is_allowed(update: Any) -> bool:
        del update
        return True

    async def _ensure_user(update: Any) -> int:
        del update
        return 1

    async def _track_event(*args: Any, **kwargs: Any) -> None:
        del args, kwargs

    monkeypatch.setattr(coach_bot, "is_allowed", _is_allowed)
    monkeypatch.setattr(coach_bot, "ensure_user", _ensure_user)
    monkeypatch.setattr(coach_bot, "track_event", _track_event)


# ---------------------------------------------------------------------------
# skip
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_skipping_an_exercise_emits_an_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path, "skip")
    _bind(monkeypatch, db)
    session_id = await _start_session(db)

    await _tap(db, session_id, "skip")

    events = await _session_events(db, "exercise_skipped")
    assert len(events) == 1, "a skipped exercise left no trace"
    assert events[0]["session_id"] == session_id
    assert events[0]["exercise_index"] == 0
    assert events[0]["exercise_id"] == "db_bench"
    assert events[0]["was_last_exercise"] is False


@pytest.mark.asyncio
async def test_skip_event_identifies_which_exercise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The event must name the exercise that was skipped, not the next one."""
    db = await _make_db(tmp_path, "which")
    _bind(monkeypatch, db)
    session_id = await _start_session(db)

    await _tap(db, session_id, "skip")
    await _tap(db, session_id, "skip")

    events = await _session_events(db, "exercise_skipped")
    assert [e["exercise_id"] for e in events] == ["db_bench", "db_row"]
    assert [e["exercise_index"] for e in events] == [0, 1]


@pytest.mark.asyncio
async def test_skipping_the_last_exercise_is_marked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path, "last")
    _bind(monkeypatch, db)
    session_id = await _start_session(db, exercises=[_ex()])

    await _tap(db, session_id, "skip")

    events = await _session_events(db, "exercise_skipped")
    assert len(events) == 1
    assert events[0]["was_last_exercise"] is True
    assert events[0]["session_status"]


@pytest.mark.asyncio
async def test_skip_still_advances_the_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Observability must not change behaviour."""
    db = await _make_db(tmp_path, "advance")
    _bind(monkeypatch, db)
    session_id = await _start_session(db)

    await _tap(db, session_id, "skip")

    row = await _session_row(db, session_id)
    assert row["exercise_index"] == 1
    assert row["set_number"] == 1


# ---------------------------------------------------------------------------
# pain
# ---------------------------------------------------------------------------
async def _report_pain(db: Database, session_id: int, level: int) -> None:
    session = await _session_row(db, session_id)
    step = f"{session['exercise_index']}:{session['set_number']}"
    await coach_bot.handle_session_action_callback(
        FakeQuery(), _ctx(), 1, f"painloc:{session_id}:{step}:knee"
    )
    await coach_bot.handle_session_action_callback(
        FakeQuery(), _ctx(), 1, f"painlevel:{session_id}:{step}:{level}"
    )


@pytest.mark.asyncio
async def test_pain_report_emits_an_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path, "pain")
    _bind(monkeypatch, db)
    session_id = await _start_session(db)

    await _report_pain(db, session_id, level=1)

    events = await _session_events(db, "pain_reported")
    assert len(events) == 1, "a pain report left no domain event"
    assert events[0]["session_id"] == session_id
    assert events[0]["location"] == "knee"
    assert events[0]["severity"] == 1
    assert events[0]["constraint_id"] > 0


@pytest.mark.asyncio
async def test_pain_report_still_persists_the_constraint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The event is additive: the existing writes must be untouched."""
    db = await _make_db(tmp_path, "constraint")
    _bind(monkeypatch, db)
    session_id = await _start_session(db)

    await _report_pain(db, session_id, level=3)

    rows = await db.fetch_all(
        "SELECT * FROM medical_constraints WHERE user_id=1 AND kind='pain'"
    )
    assert len(rows) == 1
    assert rows[0]["location"] == "knee"
    assert rows[0]["severity"] == 3
    assert rows[0]["status"] == "active"


@pytest.mark.asyncio
async def test_pain_location_is_null_after_a_successful_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pins the claim-token semantics so NULL is not misread as a lost write.

    painloc writes sessions.pain_location; painlevel clears it as part of the
    same transaction that inserts the constraint. Observing NULL afterwards
    is the success state.
    """
    db = await _make_db(tmp_path, "token")
    _bind(monkeypatch, db)
    session_id = await _start_session(db)

    await _report_pain(db, session_id, level=2)

    row = await _session_row(db, session_id)
    assert row["pain_location"] is None
    constraints = await db.fetch_all(
        "SELECT * FROM medical_constraints WHERE user_id=1"
    )
    assert len(constraints) == 1, "the NULL must correspond to a committed constraint"


@pytest.mark.asyncio
async def test_no_event_when_nothing_was_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path, "quiet")
    _bind(monkeypatch, db)
    await _start_session(db)

    assert await _session_events(db, "pain_reported") == []
    assert await _session_events(db, "exercise_skipped") == []
