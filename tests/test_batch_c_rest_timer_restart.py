"""Batch C (FIX 45): rest timers must survive a process restart.

Root cause (verified before this fix): rest-timer deadlines lived only in
the Telegram JobQueue's in-memory job data, keyed off time.monotonic(). A
restart destroyed the timer while the durable session row stayed on the
same step, leaving the Telegram card frozen with no way to resume.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

import coach_bot
from noam_coach.bot.workout_runtime import (
    clear_persisted_rest_timer,
    persist_rest_timer,
    restore_rest_timers_on_startup,
)


async def _make_db(tmp_path: Path) -> coach_bot.Database:
    db = coach_bot.Database(str(tmp_path / "coach.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (coach_bot.utc_now(),),
    )
    return db


async def _session(db: coach_bot.Database, *, exercise_index: int = 0, set_number: int = 1) -> int:
    session_id = await db.execute(
        """
        INSERT INTO sessions(user_id, code, name, plan, status, exercise_index,
                              set_number, started_at)
        VALUES(1, 'A', 'Workout A', '{}', 'active', ?, ?, ?)
        """,
        (exercise_index, set_number, coach_bot.utc_now()),
    )
    return session_id


class _FakeMessage:
    def __init__(self) -> None:
        self.edits: list[dict[str, Any]] = []

    async def edit_message_text(self, chat_id: int, message_id: int, text: str, **kwargs: Any) -> None:
        self.edits.append({"chat_id": chat_id, "message_id": message_id, "text": text, **kwargs})


class _FakeBot:
    def __init__(self) -> None:
        self.message = _FakeMessage()

    async def edit_message_text(self, **kwargs: Any) -> None:
        await self.message.edit_message_text(**kwargs)


class _FakeJobQueue:
    def __init__(self, bot: _FakeBot) -> None:
        self.application = type("App", (), {"bot": bot})()
        self.scheduled: list[dict[str, Any]] = []

    def run_repeating(self, func: Any, **kwargs: Any) -> None:
        self.scheduled.append(kwargs)


def _timer_data(session_id: int, chat_id: int, message_id: int, total_seconds: int) -> dict[str, Any]:
    return {
        "user_id": 1,
        "session_id": session_id,
        "session_step": {"id": session_id, "exercise_index": 0, "set_number": 1},
        "chat_id": chat_id,
        "message_id": message_id,
        "weight": 60.0,
        "reps": 8,
        "rir": 2,
        "total_seconds": total_seconds,
        "last_remaining": None,
        "cancelled": False,
        "summary_line": None,
    }


@pytest.mark.asyncio
async def test_restart_mid_rest_re_arms_a_real_timer(tmp_path: Path) -> None:
    """The exact FIX 45 scenario: restart happens while rest is still
    counting down -- the timer must be re-armed, not lost."""
    db = await _make_db(tmp_path)
    coach_bot.DB.path = db.path
    session_id = await _session(db)

    await persist_rest_timer(_timer_data(session_id, chat_id=100, message_id=200, total_seconds=90))

    bot = _FakeBot()
    jq = _FakeJobQueue(bot)
    processed = await restore_rest_timers_on_startup(jq)

    assert processed == 1
    assert len(jq.scheduled) == 1
    # No stale-card edit should happen when the timer is still live.
    assert bot.message.edits == []


@pytest.mark.asyncio
async def test_restart_after_deadline_resolves_stale_card(tmp_path: Path) -> None:
    """Restart happens AFTER the rest deadline already passed -- the card
    must be edited into the resume state, not left frozen."""
    db = await _make_db(tmp_path)
    coach_bot.DB.path = db.path
    session_id = await _session(db)

    timer_data = _timer_data(session_id, chat_id=100, message_id=200, total_seconds=1)
    await persist_rest_timer(timer_data)
    # Force the persisted deadline into the past (simulating "process was
    # down long enough that rest already ended").
    past = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
    await db.execute(
        "UPDATE rest_timers SET ends_at_utc=? WHERE session_id=?", (past, session_id)
    )

    bot = _FakeBot()
    jq = _FakeJobQueue(bot)
    processed = await restore_rest_timers_on_startup(jq)

    assert processed == 1
    assert jq.scheduled == []  # not re-armed, already over
    assert len(bot.message.edits) == 1
    assert bot.message.edits[0]["chat_id"] == 100
    assert bot.message.edits[0]["message_id"] == 200

    # The persisted row must be cleared after resolving.
    remaining = await db.fetch_one("SELECT * FROM rest_timers WHERE session_id=?", (session_id,))
    assert remaining is None


@pytest.mark.asyncio
async def test_restart_drops_timer_for_session_that_already_advanced(tmp_path: Path) -> None:
    """A timer card from an old session step cannot advance a new session --
    if the durable session moved past the persisted step, the row is
    dropped rather than incorrectly re-armed or resolved against a step
    that no longer matches the card."""
    db = await _make_db(tmp_path)
    coach_bot.DB.path = db.path
    session_id = await _session(db, exercise_index=0, set_number=1)

    await persist_rest_timer(_timer_data(session_id, chat_id=100, message_id=200, total_seconds=90))

    # Session advances to the next set while the process is down.
    await db.execute(
        "UPDATE sessions SET set_number=2 WHERE id=?", (session_id,)
    )

    bot = _FakeBot()
    jq = _FakeJobQueue(bot)
    processed = await restore_rest_timers_on_startup(jq)

    assert processed == 0
    assert jq.scheduled == []
    assert bot.message.edits == []
    remaining = await db.fetch_one("SELECT * FROM rest_timers WHERE session_id=?", (session_id,))
    assert remaining is None


@pytest.mark.asyncio
async def test_restart_drops_timer_for_finished_session(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    coach_bot.DB.path = db.path
    session_id = await _session(db)
    await persist_rest_timer(_timer_data(session_id, chat_id=100, message_id=200, total_seconds=90))
    await db.execute("UPDATE sessions SET status='completed' WHERE id=?", (session_id,))

    jq = _FakeJobQueue(_FakeBot())
    processed = await restore_rest_timers_on_startup(jq)

    assert processed == 0
    remaining = await db.fetch_one("SELECT * FROM rest_timers WHERE session_id=?", (session_id,))
    assert remaining is None


@pytest.mark.asyncio
async def test_clear_persisted_rest_timer_removes_row(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    coach_bot.DB.path = db.path
    session_id = await _session(db)
    await persist_rest_timer(_timer_data(session_id, chat_id=100, message_id=200, total_seconds=90))

    await clear_persisted_rest_timer(session_id)
    remaining = await db.fetch_one("SELECT * FROM rest_timers WHERE session_id=?", (session_id,))
    assert remaining is None


@pytest.mark.asyncio
async def test_no_persisted_timers_is_a_clean_no_op(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    coach_bot.DB.path = db.path
    jq = _FakeJobQueue(_FakeBot())
    processed = await restore_rest_timers_on_startup(jq)
    assert processed == 0
