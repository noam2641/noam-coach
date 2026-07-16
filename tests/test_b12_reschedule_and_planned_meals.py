"""B12 — ARCH-14 (concrete reschedule) + ARCH-13 (planned-meal lifecycle).

ARCH-14 contract: "the workout is later" collects a concrete time (slot
controls + text input), normalizes and validates it, and persists ONLY a
concrete scheduling decision; invalid input gets a user-visible correction;
"don't know yet" stays a vague later that is never treated as a time.

ARCH-13 contract: planned meals carry durable identity (plan_id) and an
explicit lifecycle — planned → consumed (fingerprint match on save, never
title text) / expired (view-level after the expiry window); the follow-up's
substring matching remains only as the text-log fallback.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import coach_bot
import event_log
from config import TZ
from db import Database
from helpers import utc_now
from noam_coach.observability import ObservabilityMode, set_mode
from noam_coach.observability.emit import reset_observability_health
from noam_coach.observability.modes import reset_mode
from noam_coach.services.workout_reschedule import (
    install_workout_reschedule,
    parse_reschedule_time,
    uninstall_workout_reschedule,
)

USER_ID = 1


class FakeQuery:
    def __init__(self, message_id: int = 500) -> None:
        self.message = SimpleNamespace(
            message_id=message_id, chat=SimpleNamespace(id=USER_ID)
        )
        self.from_user = SimpleNamespace(id=USER_ID)
        self.edits: list[str] = []
        self.markups: list[Any] = []

    async def edit_message_text(
        self, text: str, reply_markup: Any = None, parse_mode: str | None = None
    ) -> None:
        self.edits.append(text)
        self.markups.append(reply_markup)

    async def answer(self, *a: Any, **k: Any) -> None:
        return None


class FakeMessage:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.message_id = 42
        self.chat = SimpleNamespace(id=USER_ID)
        self.replies: list[str] = []

    async def reply_text(
        self, text: str, reply_markup: Any = None, parse_mode: str | None = None
    ) -> None:
        self.replies.append(text)


def _update(text: str) -> Any:
    return SimpleNamespace(
        effective_message=FakeMessage(text),
        effective_user=SimpleNamespace(id=USER_ID),
        effective_chat=SimpleNamespace(id=USER_ID),
    )


@pytest.fixture
async def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Database:
    database = Database(str(tmp_path / "b12.db"))
    await database.init()
    await database.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(?, 'Test', NULL, ?)",
        (USER_ID, utc_now()),
    )
    await database.execute(
        """
        INSERT INTO goal_versions(user_id, calories, protein, steps, phase, status, source, created_at)
        VALUES(?, 2100, 150, 8000, 'fat_loss_muscle_retention', 'active', 'test', ?)
        """,
        (USER_ID, utc_now()),
    )
    monkeypatch.setattr(coach_bot, "DB", database)
    from config import SETTINGS

    monkeypatch.setattr(SETTINGS, "telegram_allowed_user_id", USER_ID, raising=False)
    return database


@pytest.fixture(autouse=True)
def _isolation():
    set_mode(ObservabilityMode.CONTENT)
    reset_observability_health()
    yield
    uninstall_workout_reschedule()
    reset_mode()
    reset_observability_health()


def _controls(markup: Any) -> list[str]:
    return [btn.callback_data for row in markup.inline_keyboard for btn in row]


async def _flags(db: Database) -> dict[str, Any]:
    import health_service

    return await health_service.get_daily_flags(USER_ID)


# ---------------------------------------------------------------------------
# ARCH-14 — time normalization
# ---------------------------------------------------------------------------


def test_parse_reschedule_time_normalizes_and_validates() -> None:
    now = datetime.now(TZ).replace(hour=15, minute=0, second=0, microsecond=0)
    ok, reason = parse_reschedule_time("19:30", now)
    assert reason is None and ok is not None and (ok.hour, ok.minute) == (19, 30)
    ok, reason = parse_reschedule_time("19.30", now)
    assert reason is None and (ok.hour, ok.minute) == (19, 30)
    ok, reason = parse_reschedule_time("20", now)
    assert reason is None and (ok.hour, ok.minute) == (20, 0)
    ok, reason = parse_reschedule_time("בעוד שעה", now)
    assert reason is None and ok == now + timedelta(minutes=60)
    ok, reason = parse_reschedule_time("14:00", now)
    assert ok is None and reason == "past"  # valid time, already gone
    ok, reason = parse_reschedule_time("99:80", now)
    assert ok is None and reason == "unparseable"
    ok, reason = parse_reschedule_time("אין לי מושג", now)
    assert ok is None and reason == "unparseable"


# ---------------------------------------------------------------------------
# ARCH-14 — the collection flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_later_renders_time_collection_not_a_vague_persist(db: Database) -> None:
    install_workout_reschedule()
    query = FakeQuery()
    handled = await coach_bot.handle_menu_callback(query, USER_ID, "nextmeal:wkt:later")

    assert handled is True
    flags = await _flags(db)
    assert flags.get("next_meal_workout_status") == "later"  # the claim persists
    assert "next_meal_workout_expected_at" not in flags     # but never as a time
    controls = _controls(query.markups[-1])
    assert any(c.startswith("wktat:") and c[6:].isdigit() for c in controls)  # slots
    assert "wktat:text" in controls
    assert "wktat:skip" in controls


@pytest.mark.asyncio
async def test_slot_tap_persists_a_concrete_decision(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    from noam_coach.bot import callback_menu as callback_menu_bot

    rendered: list[str] = []

    async def fake_render(query: Any, user_id: int, prefix: str = "", **k: Any) -> None:
        rendered.append(prefix)

    monkeypatch.setattr(callback_menu_bot, "_render_next_meal_screen", fake_render)
    install_workout_reschedule()

    future = (datetime.now(TZ) + timedelta(hours=3)).replace(second=0, microsecond=0)
    hhmm = f"{future:%H%M}"
    handled = await coach_bot.handle_menu_callback(FakeQuery(), USER_ID, f"wktat:{hhmm}")

    assert handled is True
    flags = await _flags(db)
    assert flags.get("next_meal_workout_status") == "later"
    expected = flags.get("next_meal_workout_expected_at")
    assert expected and f"{future:%H:%M}" in str(expected)
    assert rendered and f"{future:%H:%M}" in rendered[0]
    events = await event_log.list_events(db, USER_ID)
    assert any(e.event == "next_meal_workout_rescheduled" for e in events)


@pytest.mark.asyncio
async def test_free_text_time_is_normalized_and_persisted(db: Database) -> None:
    install_workout_reschedule()
    # "אכתוב שעה" arms the pending question...
    await coach_bot.handle_menu_callback(FakeQuery(), USER_ID, "wktat:text")
    # ...and the next text turn is parsed as the time.
    update = _update("19:30")
    now = datetime.now(TZ)
    if now.hour >= 19 and (now.hour, now.minute) >= (19, 30):
        update = _update("23:59")
    await coach_bot.route_free_text(update, USER_ID)

    flags = await _flags(db)
    assert flags.get("next_meal_workout_expected_at")
    assert update.effective_message.replies  # confirmation with the time
    # The pending question is released.
    row = await db.fetch_one(
        "SELECT step FROM conversation_state WHERE user_id=? AND flow='workout_reschedule'",
        (USER_ID,),
    )
    assert row is None


@pytest.mark.asyncio
async def test_invalid_time_gets_a_visible_correction(db: Database) -> None:
    install_workout_reschedule()
    await coach_bot.handle_menu_callback(FakeQuery(), USER_ID, "wktat:text")

    update = _update("99:80")
    await coach_bot.route_free_text(update, USER_ID)

    assert any("לא הצלחתי" in reply for reply in update.effective_message.replies)
    flags = await _flags(db)
    assert "next_meal_workout_expected_at" not in flags  # nothing persisted
    row = await db.fetch_one(
        "SELECT step FROM conversation_state WHERE user_id=? AND flow='workout_reschedule'",
        (USER_ID,),
    )
    assert row is not None  # still pending — the correction preserved the flow


@pytest.mark.asyncio
async def test_non_time_text_releases_the_pending_question(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_workout_reschedule()
    await coach_bot.handle_menu_callback(FakeQuery(), USER_ID, "wktat:text")

    delegated: list[str] = []

    async def route_spy(update: Any, user_id: int) -> None:
        delegated.append(update.effective_message.text)

    from noam_coach.services import workout_reschedule as wr

    monkeypatch.setattr(wr, "_original_route_free_text", route_spy)
    # Re-close over the spy: reinstall on top of it.
    uninstall_workout_reschedule()
    real_route = coach_bot.route_free_text
    coach_bot.route_free_text = route_spy
    try:
        install_workout_reschedule()
        update = _update("מה המצב עם הארוחות?")
        await coach_bot.route_free_text(update, USER_ID)
        assert delegated == ["מה המצב עם הארוחות?"]  # user moved on: delegated
        row = await db.fetch_one(
            "SELECT step FROM conversation_state WHERE user_id=? AND flow='workout_reschedule'",
            (USER_ID,),
        )
        assert row is None  # not trapped
    finally:
        uninstall_workout_reschedule()
        coach_bot.route_free_text = real_route


@pytest.mark.asyncio
async def test_concrete_reschedule_reaches_the_canonical_resolver(db: Database) -> None:
    """The persisted decision drives phase/planned_start for ALL consumers."""
    from noam_coach.services.next_meal import save_workout_reschedule_time
    from noam_coach.services.user_state import resolve_workout_state

    now = datetime.now(TZ).replace(second=0, microsecond=0)
    expected = now + timedelta(minutes=90)
    await save_workout_reschedule_time(db, USER_ID, expected, now=now)

    state = await resolve_workout_state(db, USER_ID, now)
    assert state.source == "user_clarification"
    assert state.planned_start == expected
    assert state.minutes_until == 90
    assert f"{expected:%H:%M}" in state.label

    from noam_coach.services.next_meal import build_workout_nutrition_context

    context = await build_workout_nutrition_context(db, USER_ID, now=now)
    assert context.planned_workout_start == expected.isoformat()


# ---------------------------------------------------------------------------
# ARCH-13 — planned-meal durable identity + lifecycle
# ---------------------------------------------------------------------------


async def _plan_and_get_entry(db: Database, now: datetime) -> tuple[Any, dict[str, Any]]:
    from noam_coach.services.next_meal import (
        generate_next_meal_recommendation,
        plan_chosen_meal,
    )

    rec = await generate_next_meal_recommendation(db, USER_ID, now=now)
    option = rec.options[0]
    assert await plan_chosen_meal(db, USER_ID, option, now=now) is True
    flags = await _flags(db)
    entries = flags["next_meal_planned"]
    return option, entries[0]


@pytest.mark.asyncio
async def test_planned_meal_gets_durable_identity(db: Database) -> None:
    now = datetime.now(TZ)
    _option, entry = await _plan_and_get_entry(db, now)
    assert entry["plan_id"].startswith(f"pm-{USER_ID}-")
    assert entry["status"] == "planned"
    assert entry["status_at"]


@pytest.mark.asyncio
async def test_saving_the_planned_meal_transitions_to_consumed_by_fingerprint(
    db: Database,
) -> None:
    from noam_coach.services.next_meal import save_chosen_meal

    now = datetime.now(TZ)
    option, entry = await _plan_and_get_entry(db, now)

    assert await save_chosen_meal(db, USER_ID, option, now=now) is True

    flags = await _flags(db)
    updated = flags["next_meal_planned"][0]
    assert updated["status"] == "consumed"
    assert updated["meal_id"]  # durable link to the created meals row
    events = await event_log.list_events(db, USER_ID)
    consumed = [
        e for e in events
        if e.event == "state.mutated" and e.entity == "planned_meal"
    ]
    assert consumed and consumed[0].properties["transition"] == "consumed"
    assert consumed[0].entity_id == entry["plan_id"]
    # No longer surfaced as planned anywhere.
    from noam_coach.services.nutrition_context import build_nutrition_context

    context = await build_nutrition_context(db, USER_ID, "morning_menu")
    assert all(
        meal.get("plan_id") != entry["plan_id"] for meal in context.planned_meals
    )
    from noam_coach.services.meal_followup import planned_meal_followup

    assert planned_meal_followup(context, now=now + timedelta(hours=3)) is None


@pytest.mark.asyncio
async def test_stale_planned_meal_reads_as_expired(db: Database) -> None:
    from noam_coach.services.next_meal import planned_meal_view_status
    from noam_coach.services.user_state import build_shared_state

    now = datetime.now(TZ)
    _option, entry = await _plan_and_get_entry(db, now)

    later = now + timedelta(hours=7)
    assert planned_meal_view_status(entry, later) == "expired"
    assert planned_meal_view_status(entry, now + timedelta(hours=1)) == "planned"

    shared = await build_shared_state(db, USER_ID, now=now)
    assert entry["name"] in shared.planned_meal_titles  # fresh → visible


@pytest.mark.asyncio
async def test_text_logged_meal_still_matches_by_substring_fallback(
    db: Database,
) -> None:
    """The fuzzy path survives ONLY for free-text logs with no fingerprint."""
    from datetime import timezone as _tz

    from noam_coach.services.meal_followup import planned_meal_followup
    from noam_coach.services.nutrition_context import build_nutrition_context

    now = datetime.now(TZ)
    option, _entry = await _plan_and_get_entry(db, now)
    short_title = str(option.title).split()[0]
    await db.execute(
        """
        INSERT INTO meals(user_id, name, calories, protein, carbs, fat, confidence, eaten_at, created_at)
        VALUES(?, ?, 400, 30, 0, 0, 1, ?, ?)
        """,
        (USER_ID, short_title, now.astimezone(_tz.utc).isoformat(), utc_now()),
    )
    context = await build_nutrition_context(db, USER_ID, "morning_menu")
    followup = planned_meal_followup(context, now=now + timedelta(hours=3))
    assert followup is None  # substring fallback recognized the text log
