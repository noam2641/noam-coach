"""Batch C (FIX 45): rest timers must survive a process restart.

Root cause (verified before this fix): rest-timer deadlines lived only in
the Telegram JobQueue's in-memory job data, keyed off time.monotonic(). A
restart destroyed the timer while the durable session row stayed on the
same step, leaving the Telegram card frozen with no way to resume.

TASK-WORKOUT-REST-NEXT-ACTION extends this file: every visible rest card must
also say what happens when the rest ends, that instruction must survive the
throttled countdown edits without adding a single message, and it must be the
SAME decision as the transition that actually fires at zero.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

import coach_bot
from noam_coach.bot.workout_runtime import (
    clear_persisted_rest_timer,
    persist_rest_timer,
    resolve_rest_next_action,
    rest_keyboard,
    rest_text,
    restore_rest_timers_on_startup,
    update_rest_message,
)
from noam_coach.services import workout_next_action


def _plan_json() -> str:
    def ex(exercise_id: str, name: str, sets: int, rmin: int, rmax: int) -> dict[str, Any]:
        return {
            "id": exercise_id,
            "name": name,
            "sets": sets,
            "rmin": rmin,
            "rmax": rmax,
            "rest": 90,
            "weight": 50.0,
            "inc": 2.5,
            "cues": ["cue"],
            "muscle": "חזה",
            "alts": [{"id": "chest_machine", "name": "Chest Press", "weight": 55}],
        }

    return json.dumps(
        {
            "name": "אימון A",
            "exercises": [
                ex("bench", "לחיצת חזה", 4, 8, 10),
                ex("row", "חתירה בישיבה", 3, 10, 12),
            ],
        },
        ensure_ascii=False,
    )


async def _make_db(tmp_path: Path) -> coach_bot.Database:
    db = coach_bot.Database(str(tmp_path / "coach.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (coach_bot.utc_now(),),
    )
    return db


async def _session(
    db: coach_bot.Database,
    *,
    exercise_index: int = 0,
    set_number: int = 1,
    plan: str = "{}",
) -> int:
    session_id = await db.execute(
        """
        INSERT INTO sessions(user_id, code, name, plan, status, exercise_index,
                              set_number, started_at)
        VALUES(1, 'A', 'Workout A', ?, 'active', ?, ?, ?)
        """,
        (plan, exercise_index, set_number, coach_bot.utc_now()),
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


def _timer_data(
    session_id: int,
    chat_id: int,
    message_id: int,
    total_seconds: int,
    exercise_index: int = 0,
    set_number: int = 1,
) -> dict[str, Any]:
    return {
        "user_id": 1,
        "session_id": session_id,
        "session_step": {
            "id": session_id,
            "exercise_index": exercise_index,
            "set_number": set_number,
        },
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


# ---------------------------------------------------------------------------
# TASK-WORKOUT-REST-NEXT-ACTION — the rest card must say what comes next.
# ---------------------------------------------------------------------------


class _CountingBot(_FakeBot):
    """Counts edits AND sends, so "no flooding" is asserted on both axes."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[dict[str, Any]] = []

    async def send_message(self, **kwargs: Any) -> None:
        self.sent.append(kwargs)


class _FakeContext:
    def __init__(self, bot: Any) -> None:
        self.bot = bot


def _live_timer_data(
    session_id: int,
    total_seconds: int,
    remaining: float,
    next_action: Any,
) -> dict[str, Any]:
    import time as _time

    data = _timer_data(session_id, chat_id=100, message_id=200, total_seconds=total_seconds)
    data["ends_at"] = _time.monotonic() + remaining
    data["next_action"] = next_action
    return data


@pytest.mark.asyncio
async def test_rest_card_states_the_next_set_of_the_same_exercise(tmp_path: Path) -> None:
    """Case A: the card names the exercise, the upcoming set number and total,
    and the rep target — no need to remember or go looking."""
    db = await _make_db(tmp_path)
    coach_bot.DB.path = db.path
    session_id = await _session(db, exercise_index=0, set_number=3, plan=_plan_json())

    action = await resolve_rest_next_action(session_id, previous_weight=70.0)
    text = rest_text(70.0, 8, 2, remaining=84, total_seconds=120, next_action=action)

    assert action.kind == workout_next_action.SAME_EXERCISE
    assert "הבא:" in text
    assert "סט 3 מתוך 4 — לחיצת חזה" in text
    assert "יעד: 8–10 חזרות" in text
    assert "בסט הקודם: 70 ק״ג" in text
    assert "01:24" in text  # the countdown is still there


@pytest.mark.asyncio
async def test_rest_card_states_the_transition_to_a_different_exercise(tmp_path: Path) -> None:
    """Case B: the pointer sits past the last set of exercise 1, so the card
    must name the EXACT next exercise, not another set of the current one."""
    db = await _make_db(tmp_path)
    coach_bot.DB.path = db.path
    session_id = await _session(db, exercise_index=0, set_number=5, plan=_plan_json())

    action = await resolve_rest_next_action(session_id, previous_weight=70.0)
    text = rest_text(70.0, 8, 2, remaining=84, total_seconds=120, next_action=action)

    assert action.kind == workout_next_action.NEXT_EXERCISE
    assert "התרגיל הבא:" in text
    assert "חתירה בישיבה — סט 1 מתוך 3" in text
    assert "יעד: 10–12 חזרות" in text
    # A weight from the PREVIOUS movement must not ride along.
    assert "בסט הקודם" not in text


@pytest.mark.asyncio
async def test_final_rest_shows_completion_and_no_ghost_exercise(tmp_path: Path) -> None:
    """Case D: nothing left to perform — the card states the completion step
    instead of inventing an exercise that does not exist."""
    db = await _make_db(tmp_path)
    coach_bot.DB.path = db.path
    # Pointer past the last set of the last exercise: nothing remains.
    session_id = await _session(db, exercise_index=1, set_number=4, plan=_plan_json())

    action = await resolve_rest_next_action(session_id)
    text = rest_text(70.0, 8, 2, remaining=30, total_seconds=120, next_action=action)

    assert action.is_complete
    assert "לאחר המנוחה:" in text
    assert "סיום האימון ומעבר לסיכום." in text
    assert "לחיצת חזה" not in text
    assert "חתירה בישיבה" not in text


@pytest.mark.asyncio
async def test_next_action_survives_every_throttled_edit_without_flooding(
    tmp_path: Path,
) -> None:
    """The instruction lives in the SAME card and is re-rendered on each edit
    the existing throttle allows — and the throttle itself is untouched, so a
    120s rest still produces a handful of edits and ZERO new messages."""
    db = await _make_db(tmp_path)
    coach_bot.DB.path = db.path
    session_id = await _session(db, exercise_index=0, set_number=3, plan=_plan_json())

    bot = _CountingBot()
    context = _FakeContext(bot)
    action = await resolve_rest_next_action(session_id, previous_weight=70.0)

    # Walk a whole 120s rest, one simulated second at a time.
    for remaining in range(120, -1, -1):
        data = _live_timer_data(session_id, 120, remaining, action)
        data["last_remaining"] = remaining + 1
        await update_rest_message(context, data)

    edits = bot.message.edits
    # Anti-flood preserved: start + halfway + the last 10s + zero.
    assert len(edits) <= 14, len(edits)
    assert bot.sent == []  # never a new chat message per tick
    # Same card every time.
    assert {e["message_id"] for e in edits} == {200}
    # And the instruction is on EVERY one of them, including the last.
    for edit in edits:
        assert "סט 3 מתוך 4 — לחיצת חזה" in edit["text"]


@pytest.mark.asyncio
async def test_zero_edit_repeats_the_next_action_and_keeps_controls(
    tmp_path: Path,
) -> None:
    db = await _make_db(tmp_path)
    coach_bot.DB.path = db.path
    session_id = await _session(db, exercise_index=0, set_number=3, plan=_plan_json())

    bot = _CountingBot()
    action = await resolve_rest_next_action(session_id, previous_weight=70.0)
    data = _live_timer_data(session_id, 120, -1, action)
    remaining = await update_rest_message(_FakeContext(bot), data)

    assert remaining == 0
    final = bot.message.edits[-1]["text"]
    assert "המנוחה הסתיימה" in final
    # Prominently repeated at zero, not dropped.
    assert "סט 3 מתוך 4 — לחיצת חזה" in final


def test_rest_controls_are_intact_alongside_the_next_action() -> None:
    """ready / +30s / undo must all survive this change."""
    step = {"id": 1, "exercise_index": 0, "set_number": 3}
    labels = [b.text for row in rest_keyboard(step, finished=False).inline_keyboard for b in row]

    assert any("מוכן" in label for label in labels)
    assert any("30" in label for label in labels)
    assert any("בטל" in label for label in labels)


@pytest.mark.asyncio
async def test_plus_30s_extension_keeps_the_next_action(tmp_path: Path) -> None:
    """The restadd handler mutates ends_at/total_seconds and re-renders via
    update_rest_message — the instruction must come back with it."""
    db = await _make_db(tmp_path)
    coach_bot.DB.path = db.path
    session_id = await _session(db, exercise_index=0, set_number=3, plan=_plan_json())

    bot = _CountingBot()
    action = await resolve_rest_next_action(session_id, previous_weight=70.0)
    # A full window that is then extended: total becomes 150 and remaining is
    # 150 too, i.e. the "start of window" branch the existing throttle admits.
    data = _live_timer_data(session_id, 120, 120, action)

    # Exactly what callback_session "restadd" does.
    data["ends_at"] += 30
    data["total_seconds"] += 30
    data["last_remaining"] = None
    await update_rest_message(_FakeContext(bot), data)

    assert bot.sent == []  # extension edits the card, never posts a new one
    assert bot.message.edits, "the extension must re-render the card"
    assert "02:30" in bot.message.edits[-1]["text"]  # the extended window
    assert "סט 3 מתוך 4 — לחיצת חזה" in bot.message.edits[-1]["text"]
    # The controls survive the extension.
    labels = [
        b.text
        for row in bot.message.edits[-1]["reply_markup"].inline_keyboard
        for b in row
    ]
    assert any("מוכן" in label for label in labels)
    assert any("30" in label for label in labels)


@pytest.mark.asyncio
async def test_state_change_during_rest_is_not_left_stale(tmp_path: Path) -> None:
    """A `sub` rewrites the plan in place WHILE the timer runs. The next edit
    must show the substituted exercise, because the action is re-derived from
    the persisted row rather than captured once at timer start."""
    db = await _make_db(tmp_path)
    coach_bot.DB.path = db.path
    session_id = await _session(db, exercise_index=0, set_number=3, plan=_plan_json())

    bot = _CountingBot()
    stale = await resolve_rest_next_action(session_id, previous_weight=70.0)
    assert stale.exercise_name == "לחיצת חזה"

    plan = json.loads(_plan_json())
    plan["exercises"][0]["name"] = "Chest Press"
    plan["exercises"][0]["id"] = "chest_machine"
    await db.execute(
        "UPDATE sessions SET plan=? WHERE id=?",
        (json.dumps(plan, ensure_ascii=False), session_id),
    )

    data = _live_timer_data(session_id, 120, 5, stale)
    await update_rest_message(_FakeContext(bot), data)

    text = bot.message.edits[-1]["text"]
    assert "Chest Press" in text
    assert "לחיצת חזה" not in text


@pytest.mark.asyncio
async def test_undo_during_rest_rewinds_the_displayed_next_action(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    coach_bot.DB.path = db.path
    session_id = await _session(db, exercise_index=0, set_number=4, plan=_plan_json())

    bot = _CountingBot()
    before = await resolve_rest_next_action(session_id, previous_weight=70.0)
    assert before.set_number == 4

    await db.execute("UPDATE sessions SET set_number=3 WHERE id=?", (session_id,))
    data = _live_timer_data(session_id, 120, 5, before)
    await update_rest_message(_FakeContext(bot), data)

    assert "סט 3 מתוך 4" in bot.message.edits[-1]["text"]


@pytest.mark.asyncio
async def test_display_and_transition_consult_the_same_resolver(tmp_path: Path) -> None:
    """The identity that makes the instruction trustworthy: what the card says
    and what rest_timer_tick will hand to show_session are ONE decision over
    the same canonical row."""
    db = await _make_db(tmp_path)
    coach_bot.DB.path = db.path

    session_id = await _session(db, exercise_index=0, set_number=1, plan=_plan_json())

    for index, set_no in ((0, 1), (0, 3), (0, 4), (0, 5), (1, 1), (1, 3), (1, 4)):
        await db.execute(
            "UPDATE sessions SET exercise_index=?, set_number=? WHERE id=?",
            (index, set_no, session_id),
        )
        displayed = await resolve_rest_next_action(session_id)

        # What the transition sees: the same row, read the same way.
        row = await db.fetch_one(
            "SELECT plan, exercise_index, set_number, status FROM sessions WHERE id=?",
            (session_id,),
        )
        transition = workout_next_action.resolve_from_session(row)

        assert displayed == transition
        # And the resolved index is exactly the step show_session will render.
        if not displayed.is_complete:
            assert displayed.exercise_index == (
                index if displayed.kind == workout_next_action.SAME_EXERCISE else index + 1
            )


@pytest.mark.asyncio
async def test_resume_after_interruption_shows_next_action_from_persisted_state(
    tmp_path: Path,
) -> None:
    """The restart path must rebuild the instruction from the sessions row —
    NOT from the frozen card or from rest_timers' denormalised last-set data."""
    db = await _make_db(tmp_path)
    coach_bot.DB.path = db.path
    session_id = await _session(db, exercise_index=0, set_number=3, plan=_plan_json())
    await persist_rest_timer(
        _timer_data(
            session_id, chat_id=100, message_id=200, total_seconds=90, set_number=3
        )
    )
    # The session moved while the process was down is covered elsewhere; here
    # the step still matches and the deadline has passed, so the stale card is
    # resolved — and it must carry the correct next action.
    past = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
    await db.execute(
        "UPDATE rest_timers SET ends_at_utc=? WHERE session_id=?", (past, session_id)
    )

    bot = _FakeBot()
    jq = _FakeJobQueue(bot)
    assert await restore_rest_timers_on_startup(jq) == 1

    text = bot.message.edits[0]["text"]
    assert "סט 3 מתוך 4 — לחיצת חזה" in text


@pytest.mark.asyncio
async def test_resume_mid_rest_arms_a_timer_carrying_the_next_action(
    tmp_path: Path,
) -> None:
    db = await _make_db(tmp_path)
    coach_bot.DB.path = db.path
    session_id = await _session(db, exercise_index=0, set_number=2, plan=_plan_json())
    await persist_rest_timer(
        _timer_data(
            session_id, chat_id=100, message_id=200, total_seconds=90, set_number=2
        )
    )

    jq = _FakeJobQueue(_FakeBot())
    assert await restore_rest_timers_on_startup(jq) == 1

    armed = jq.scheduled[0]["data"]["next_action"]
    assert armed.kind == workout_next_action.SAME_EXERCISE
    assert armed.set_number == 2
    assert armed.exercise_name == "לחיצת חזה"


@pytest.mark.asyncio
async def test_duplicate_update_does_not_create_a_second_timer_or_advance(
    tmp_path: Path,
) -> None:
    """A duplicate Telegram delivery replays the SAME edit: one card, one row,
    the session pointer untouched, and an identical instruction both times."""
    db = await _make_db(tmp_path)
    coach_bot.DB.path = db.path
    session_id = await _session(db, exercise_index=0, set_number=3, plan=_plan_json())

    bot = _CountingBot()
    context = _FakeContext(bot)
    action = await resolve_rest_next_action(session_id, previous_weight=70.0)
    data = _live_timer_data(session_id, 120, 5, action)

    await update_rest_message(context, data)
    first_len = len(bot.message.edits)
    # Exact duplicate: same job data, same remaining — the last_remaining
    # dedupe must absorb it.
    await update_rest_message(context, data)

    assert len(bot.message.edits) == first_len
    assert bot.sent == []

    # Only one persisted timer row, and the pointer did not advance.
    await persist_rest_timer(data)
    await persist_rest_timer(data)
    rows = await db.fetch_all("SELECT * FROM rest_timers WHERE session_id=?", (session_id,))
    assert len(rows) == 1
    session = await db.fetch_one("SELECT * FROM sessions WHERE id=?", (session_id,))
    assert (session["exercise_index"], session["set_number"]) == (0, 3)

    # And the instruction is identical across the duplicate.
    assert bot.message.edits[0]["text"] == bot.message.edits[-1]["text"]


@pytest.mark.asyncio
async def test_next_action_never_exposes_internal_ids_in_the_card(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    coach_bot.DB.path = db.path
    session_id = await _session(db, exercise_index=0, set_number=3, plan=_plan_json())

    action = await resolve_rest_next_action(session_id, previous_weight=70.0)
    text = rest_text(70.0, 8, 2, remaining=60, total_seconds=120, next_action=action)

    for leaked in ("bench", "row", "exercise_index", "session_id", "chest_machine"):
        assert leaked not in text


def test_rest_text_without_a_resolved_action_keeps_its_previous_shape() -> None:
    """A read failure must degrade to the pre-existing card, never to a card
    that asserts a next step it does not know."""
    text = rest_text(50.0, 8, 2, remaining=90, total_seconds=120, next_action=None)

    assert "01:30" in text
    assert "הבא:" not in text
    assert "התרגיל הבא:" not in text
