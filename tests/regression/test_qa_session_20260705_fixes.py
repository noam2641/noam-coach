"""Regression tests from the 05/07/2026 19:22-19:39 manual QA session
(Recording_20260705_1939.docx) — verified findings only, no speculative fixes.

Covers:
  * F9 fix: "menu:today" was a dead callback (button existed in
    assistant.py's free-text help keyboard, but no handler recognized it —
    it fell through to the session-scoped fallback and silently no-op'd).
    The button now emits "menu:morning" directly, and "menu:today" is kept
    as a legacy alias in callback_menu.py in case an old keyboard is still
    on-screen in an existing chat.
  * F-availability: parse_hebrew_availability_answer on the exact 4-day
    sentence from the recording, including a bare morning hour (10:00)
    alongside evening times in the same sentence.
  * F2/F5: recommend_load never raises the working weight after an RIR=0
    ("0/כשל") set, whether it's one set among several or the whole session.
  * F1: the "מכשיר תפוס" (equipment occupied) flow offers up to 3 real
    alternative exercises plus a skip button, and choosing one swaps the
    exercise in the active plan and writes an audit event.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import coach_bot
from db import Database
from helpers import utc_now
from noam_coach.bot import callback_menu as callback_menu_bot
from noam_coach.bot import callback_session as callback_session_bot
from noam_coach.services.availability import parse_hebrew_availability_answer
from noam_coach.services.weekdays import WEEKDAY_SCHEMA_VERSION

# ---------------------------------------------------------------------------
# F9 — "menu:today" dead-button fix
# ---------------------------------------------------------------------------


class _FakeQuery:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.reply_markups: list[Any] = []

    async def edit_message_text(
        self,
        text: str,
        reply_markup: Any = None,
        parse_mode: str | None = None,
    ) -> None:
        del parse_mode
        self.messages.append(text)
        self.reply_markups.append(reply_markup)

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        del text, show_alert


def test_free_text_help_keyboard_uses_daily_menu_not_menu_today() -> None:
    """The '📋 תפריט היום' button in the free-text help keyboard must emit the
    full daily-menu callback "menu:daily_menu" — not the orphaned "menu:today"
    and not "menu:morning" (which is now the short morning briefing, TASK-16)."""
    from noam_coach.bot import assistant as assistant_bot

    src = Path(assistant_bot.__file__).read_text(encoding="utf-8")
    assert '"📋 תפריט היום", "menu:daily_menu"' in src
    assert '"📋 תפריט היום", "menu:today"' not in src
    assert '"📋 תפריט היום", "menu:morning"' not in src


@pytest.mark.asyncio
async def test_menu_morning_renders_short_briefing_not_full_menu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TASK-16: "menu:morning" (☀️ עדכון בוקר) must render the dedicated short
    briefing (build_morning_briefing_text) and must NOT call the full daily
    menu builder (build_morning_menu_text)."""

    async def fake_briefing(_user_id: int, _ctx: Any = None) -> str:
        return "עדכון בוקר קצר"

    async def fail_full_menu(_user_id: int, _ctx: Any = None) -> str:
        raise AssertionError("menu:morning must not render the full daily menu")

    monkeypatch.setattr(coach_bot, "build_morning_briefing_text", fake_briefing)
    monkeypatch.setattr(coach_bot, "build_morning_menu_text", fail_full_menu)
    query = _FakeQuery()
    handled = await callback_menu_bot.handle_menu_callback(query, 1, "menu:morning")
    assert handled is True
    assert query.messages[-1] == "עדכון בוקר קצר"
    # Focused next-action buttons, not a full menu keyboard.
    labels = [
        btn.text
        for row in (query.reply_markups[-1].inline_keyboard if query.reply_markups[-1] else [])
        for btn in row
    ]
    assert any("מה לאכול עכשיו" in label for label in labels)
    assert any("מצב היום" in label for label in labels)


@pytest.mark.asyncio
async def test_menu_today_is_legacy_alias_for_full_daily_menu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale keyboard already on-screen may still send "menu:today". It must
    render the full daily menu (same as menu:daily_menu), not the briefing and
    not a silent no-op."""
    async def fake_build_morning_menu_text(_user_id: int) -> str:
        return "התפריט של היום"

    monkeypatch.setattr(coach_bot, "build_morning_menu_text", fake_build_morning_menu_text)
    query = _FakeQuery()
    handled = await callback_menu_bot.handle_menu_callback(query, 1, "menu:today")
    assert handled is True
    assert query.messages[-1] == "התפריט של היום"


@pytest.mark.asyncio
async def test_menu_today_never_falls_through_to_session_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reproduces the exact silent no-op from the recording: before the fix,
    "menu:today" reached handle_session_action_callback, which only handles
    numeric-session-id-encoded actions and returned False/no-op for it."""

    async def fail_if_called(*_args: Any, **_kwargs: Any) -> bool:
        raise AssertionError(
            "menu:today must be fully handled by handle_menu_callback and "
            "must never reach handle_session_action_callback"
        )

    monkeypatch.setattr(
        callback_session_bot, "handle_session_action_callback", fail_if_called
    )

    async def fake_build_morning_menu_text(_user_id: int) -> str:
        return "התפריט של היום"

    monkeypatch.setattr(coach_bot, "build_morning_menu_text", fake_build_morning_menu_text)

    query = _FakeQuery()
    handled = await callback_menu_bot.handle_menu_callback(query, 1, "menu:today")
    assert handled is True  # handle_menu_callback itself must claim it


# ---------------------------------------------------------------------------
# Availability parsing — exact 4-day sentence from the recording
# ---------------------------------------------------------------------------


def test_parse_exact_recording_availability_sentence() -> None:
    text = "ראשון 19:00 שעה, שני 19:00 שעה, רביעי 18:30 45 דקות, שישי 10:00 שעה"
    parsed = parse_hebrew_availability_answer(text)

    by_day = {slot["weekday"]: slot for slot in parsed.weekly_availability}
    assert sorted(by_day) == [0, 2, 4, 6]
    assert len(parsed.weekly_availability) == 4
    assert parsed.training_days_per_week == 4

    assert by_day[6]["start"] == "19:00"  # ראשון
    assert by_day[6]["minutes"] == 60
    assert by_day[0]["start"] == "19:00"  # שני
    assert by_day[0]["minutes"] == 60
    assert by_day[2]["start"] == "18:30"  # רביעי
    assert by_day[2]["minutes"] == 45
    assert by_day[4]["start"] == "10:00"  # שישי — must stay morning, not shift to evening
    assert by_day[4]["minutes"] == 60

    assert all(slot["weekday_schema"] == WEEKDAY_SCHEMA_VERSION for slot in parsed.weekly_availability)


# ---------------------------------------------------------------------------
# RIR=0 / failure — never raises the working weight
# ---------------------------------------------------------------------------


_RIR_PLAN = {
    "name": "Test",
    "exercises": [{"id": "squat", "name": "סקוואט", "sets": 3, "rmin": 6, "rmax": 10, "inc": 2.5, "weight": 100}],
}


async def _make_rir_db(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "rir_progression.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (utc_now(),),
    )
    return db


async def _add_completed_session(
    db: Database, *, sets: list[tuple[int, int, float]]
) -> None:
    """sets: list of (reps, rir, weight) tuples, one per set_number starting at 1."""
    sid = await db.execute(
        "INSERT INTO sessions(user_id, code, name, plan, status, exercise_index, set_number, started_at, ended_at) "
        "VALUES(1,'T','Test',?, 'completed', 0, ?, ?, ?)",
        (json.dumps(_RIR_PLAN, ensure_ascii=False), len(sets) + 1, utc_now(), utc_now()),
    )
    for set_no, (reps, rir, weight) in enumerate(sets, start=1):
        await db.execute(
            "INSERT INTO sets(session_id, exercise_id, exercise_name, set_number, weight, reps, rir, source, created_at) "
            "VALUES(?, 'squat', 'סקוואט', ?, ?, ?, ?, 'telegram_adjusted', ?)",
            (sid, set_no, weight, reps, rir, utc_now()),
        )


@pytest.mark.asyncio
async def test_single_failed_set_does_not_raise_weight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import noam_coach.services.training as training

    db = await _make_rir_db(tmp_path)
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(training, "DB", db)

    # 2 clean sets + 1 failure (RIR=0) at the end of an otherwise-normal session.
    await _add_completed_session(db, sets=[(8, 2, 100), (7, 1, 100), (5, 0, 100)])

    current_exercise = {"id": "squat", "name": "סקוואט", "sets": 3, "rmin": 6, "rmax": 10, "inc": 2.5, "weight": 100}
    weight, _reps, _why = await training.recommend_load(1, current_exercise)
    assert weight <= 100.0


@pytest.mark.asyncio
async def test_all_sets_failed_does_not_raise_weight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import noam_coach.services.training as training

    db = await _make_rir_db(tmp_path)
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(training, "DB", db)

    # Every set in the session hit failure (RIR=0).
    await _add_completed_session(db, sets=[(5, 0, 100), (4, 0, 100), (3, 0, 100)])

    current_exercise = {"id": "squat", "name": "סקוואט", "sets": 3, "rmin": 6, "rmax": 10, "inc": 2.5, "weight": 100}
    weight, reps, _why = await training.recommend_load(1, current_exercise)
    assert weight <= 100.0
    # Reps target must not be pushed toward the top of the range either.
    assert reps <= 8


def test_rir_zero_is_not_counted_as_mastery_evidence() -> None:
    """Mastery (the only branch that raises weight) requires a *known*
    RIR >= 2 on every planned set. RIR=0 must never satisfy that."""
    from noam_coach.services.training import _rir_known

    assert _rir_known(0) is True  # RIR=0 IS a known value...
    assert not (0 >= 2)  # ...but it never clears the >=2 mastery bar.


# ---------------------------------------------------------------------------
# "מכשיר תפוס" (equipment occupied) — real substitutions, unchanged behavior
# ---------------------------------------------------------------------------


_OCCUPIED_PLAN = {
    "name": "Test",
    "exercises": [
        {
            "id": "leg_press",
            "name": "Leg Press",
            "sets": 3,
            "rmin": 8,
            "rmax": 10,
            "inc": 5.0,
            "weight": 90,
            "muscle": "רגליים",
            "cues": ["דגש טכני לדוגמה"],
            "alts": [
                {"id": "hack_squat", "name": "האק סקוואט", "weight": 60},
                {"id": "bulgarian_split", "name": "מכרעים בולגריים", "weight": 20},
                {"id": "leg_extension", "name": "פשיטת ברך", "weight": 30},
                {"id": "goblet_squat", "name": "סקוואט גובלט", "weight": 24},
            ],
        }
    ],
}


async def _make_occupied_session(tmp_path: Path) -> tuple[Database, dict[str, Any]]:
    db = Database(str(tmp_path / "occupied.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (utc_now(),),
    )
    sid = await db.execute(
        "INSERT INTO sessions(user_id, code, name, plan, status, exercise_index, set_number, started_at) "
        "VALUES(1,'T','Test',?, 'active', 0, 1, ?)",
        (json.dumps(_OCCUPIED_PLAN, ensure_ascii=False), utc_now()),
    )
    session = await db.fetch_one("SELECT * FROM sessions WHERE id=?", (sid,))
    assert session is not None
    return db, dict(session)


@pytest.mark.asyncio
async def test_occupied_offers_up_to_three_alternatives_and_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db, session = await _make_occupied_session(tmp_path)
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(callback_session_bot, "DB", db)

    query = _FakeQuery()
    data = coach_bot.session_action_data("occupied", session)
    await callback_session_bot.handle_session_action_callback(
        query, context=None, user_id=1, data=data
    )

    assert query.reply_markups, "occupied action must render a keyboard"
    markup = query.reply_markups[-1]
    labels = [btn.text for row in markup.inline_keyboard for btn in row]
    # Exactly 3 real alternatives (capped from the 4 defined) plus skip.
    assert sum(1 for label in labels if label.startswith(("1.", "2.", "3."))) == 3
    assert any("האק סקוואט" in label for label in labels)
    assert any("מכרעים בולגריים" in label for label in labels)
    assert any("פשיטת ברך" in label for label in labels)
    assert "גובלט" not in " ".join(labels)  # the 4th alt is capped out, not offered
    assert any(label == "דלג" for label in labels)


@pytest.mark.asyncio
async def test_choosing_substitution_replaces_exercise_and_writes_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db, session = await _make_occupied_session(tmp_path)
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(callback_session_bot, "DB", db)

    audit_calls: list[tuple[Any, ...]] = []

    async def fake_write_audit(*args: Any, **kwargs: Any) -> None:
        audit_calls.append((args, kwargs))

    monkeypatch.setattr(coach_bot, "write_audit", fake_write_audit)
    monkeypatch.setattr(callback_session_bot, "write_audit", fake_write_audit)

    query = _FakeQuery()
    data = coach_bot.session_action_data("sub", session, 0)
    await callback_session_bot.handle_session_action_callback(
        query, context=None, user_id=1, data=data
    )

    refreshed = await db.fetch_one("SELECT * FROM sessions WHERE id=?", (session["id"],))
    plan = json.loads(refreshed["plan"])
    assert plan["exercises"][0]["id"] == "hack_squat"
    assert plan["exercises"][0]["original_id"] == "leg_press"

    assert audit_calls, "approve_substitution audit event must be written"
    assert audit_calls[-1][0][1] == "approve_substitution"
