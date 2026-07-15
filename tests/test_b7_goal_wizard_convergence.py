"""B7 / ARCH-01 phase 1 — canonical flow convergence for the goal wizard.

Canonical ownership: active_flow owns lifecycle/identity; the goal_wizard
conversation_state row is scratchpad whose liveness DERIVES from the flow.

Required traces exercised here:
- interrupt: wizard start → photo interrupt → flow.suspended → meal flow →
  flow.resumed → wizard answer continues.
- expiry: flow expires → flow.expired → wizard row is gone → a late answer
  does not continue an orphan wizard.
- Home: wizard active → Home → flow explicitly resumable (suspended
  snapshot + resume control with the persisted flow_id), goal_wizard
  scratchpad preserved, free-text routing idle (no invisible continuation).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import coach_bot
import conversation
import event_log
from db import Database
from helpers import utc_now
from noam_coach.observability import ObservabilityMode, set_mode
from noam_coach.observability.emit import reset_observability_health
from noam_coach.observability.modes import reset_mode
from noam_coach.observability.state_trace import (
    install_state_trace,
    uninstall_state_trace,
)
from noam_coach.services.flow_convergence import (
    install_flow_convergence,
    uninstall_flow_convergence,
)
from noam_coach.services.flow_resume import (
    install_restart_resume,
    uninstall_restart_resume,
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


@pytest.fixture
async def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Database:
    database = Database(str(tmp_path / "b7.db"))
    await database.init()
    await database.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(?, 'Test', NULL, ?)",
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
    install_state_trace()  # flow.* lifecycle events
    yield
    uninstall_flow_convergence()
    uninstall_restart_resume()
    uninstall_state_trace()
    reset_mode()
    reset_observability_health()


def _events(events: list[Any], name: str) -> list[Any]:
    return [e for e in events if e.event == name]


def _controls(markup: Any) -> list[str]:
    return [
        btn.callback_data for row in markup.inline_keyboard for btn in row
    ]


async def _start_goal_wizard(db: Database) -> str:
    """Real wizard start: no height/goal-weight/timeframe facts → the wizard
    asks its first question and registers the pending question flow."""
    from noam_coach.bot import onboarding as onboarding_bot

    asked = await onboarding_bot.ask_next_goal_wizard_question(FakeQuery(), USER_ID)
    assert asked is True
    row = await db.fetch_one(
        "SELECT step FROM conversation_state WHERE user_id=? AND flow='goal_wizard'",
        (USER_ID,),
    )
    assert row is not None  # scratchpad row written
    active = await conversation.get_active_flow(db, USER_ID)
    assert not active.is_idle and active.flow_id
    return active.flow_id


async def _wizard_row(db: Database) -> Any:
    return await db.fetch_one(
        "SELECT step FROM conversation_state WHERE user_id=? AND flow='goal_wizard'",
        (USER_ID,),
    )


# ---------------------------------------------------------------------------
# Interrupt / suspend / resume — one authoritative lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_photo_interrupt_suspends_and_resume_continues_the_wizard(
    db: Database,
) -> None:
    install_flow_convergence()
    wizard_flow_id = await _start_goal_wizard(db)

    # Photo interrupt: the meal-correction microflow takes over.
    from noam_coach.services import core as core_services

    await core_services.set_meal_fix(USER_ID, "approval-1")
    events = await event_log.list_events(db, USER_ID)
    suspended = _events(events, "flow.suspended")
    assert suspended and suspended[-1].flow_id == wizard_flow_id
    active = await conversation.get_active_flow(db, USER_ID)
    assert active.name == conversation.FlowName.meal_correction

    # Meal flow ends → the wizard resumes with its ORIGINAL identity.
    await core_services.clear_meal_fix(USER_ID)
    events = await event_log.list_events(db, USER_ID)
    resumed = _events(events, "flow.resumed")
    assert resumed
    active = await conversation.get_active_flow(db, USER_ID)
    assert active.flow_id == wizard_flow_id
    assert await _wizard_row(db) is not None  # scratchpad still owned & live


# ---------------------------------------------------------------------------
# Expiry — no wizard row may outlive its flow
# ---------------------------------------------------------------------------


async def _force_expiry(db: Database) -> None:
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    await db.execute(
        "UPDATE active_flow SET expires_at=? WHERE user_id=?", (past, USER_ID)
    )


@pytest.mark.asyncio
async def test_expiry_clears_the_wizard_scratchpad_in_the_same_step(
    db: Database,
) -> None:
    install_flow_convergence()
    await _start_goal_wizard(db)
    await _force_expiry(db)

    flow = await conversation.expire_if_needed(db, USER_ID)

    assert flow.is_idle
    assert await _wizard_row(db) is None  # never appears live after expiry
    events = await event_log.list_events(db, USER_ID)
    assert _events(events, "flow.expired")


@pytest.mark.asyncio
async def test_late_answer_after_expiry_does_not_continue_an_orphan_wizard(
    db: Database,
) -> None:
    """The full required trace: flow expires → flow.expired → late qa answer
    → the router/handler does NOT re-enter the wizard."""
    await _start_goal_wizard(db)
    await _force_expiry(db)

    delegated: list[str] = []

    async def spy(query: Any, user_id: int, data: str) -> None:
        delegated.append(data)

    real = coach_bot.handle_onboarding_callback
    coach_bot.handle_onboarding_callback = spy
    try:
        install_flow_convergence()  # wrap the spy as the delegate
        query = FakeQuery()
        await coach_bot.handle_onboarding_callback(query, USER_ID, "qa:q_height_cm:0")

        # Expiry ran inside the gate: flow.expired traced, the wizard row is
        # gone, and the answer reaches the delegate as a NON-wizard answer —
        # there is no wizard row left for anything to continue.
        assert delegated == ["qa:q_height_cm:0"]
        assert await _wizard_row(db) is None
        events = await event_log.list_events(db, USER_ID)
        assert _events(events, "flow.expired")
        active = await conversation.get_active_flow(db, USER_ID)
        assert active.is_idle
    finally:
        uninstall_flow_convergence()
        coach_bot.handle_onboarding_callback = real


@pytest.mark.asyncio
async def test_orphan_wizard_row_is_refused_not_continued(db: Database) -> None:
    """A wizard row whose flow was cleared by a path that bypassed the
    expiry cleanup: the late tap is refused with a fresh-start offer."""
    await _start_goal_wizard(db)
    await conversation.clear_active_flow(db, USER_ID)  # bypasses cleanup

    delegated: list[str] = []

    async def spy(query: Any, user_id: int, data: str) -> None:
        delegated.append(data)

    real = coach_bot.handle_onboarding_callback
    coach_bot.handle_onboarding_callback = spy
    try:
        install_flow_convergence()  # wrap the spy as the delegate
        query = FakeQuery()
        await coach_bot.handle_onboarding_callback(query, USER_ID, "qa:q_height_cm:0")

        assert delegated == []  # refused at the gate, never delegated
        assert any("להתחיל שוב" in text or "שכבר הסתיים" in text for text in query.edits)
        assert "menu:goal" in _controls(query.markups[-1])
        assert await _wizard_row(db) is None
        events = await event_log.list_events(db, USER_ID)
        refused = [
            e for e in _events(events, "validation.failed") if e.entity == "goal_wizard"
        ]
        assert refused and refused[0].properties["reason"] == "orphan_wizard_answer"
    finally:
        uninstall_flow_convergence()
        coach_bot.handle_onboarding_callback = real


# ---------------------------------------------------------------------------
# Home — preserve resumable work, no invisible continuation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_home_suspends_the_wizard_and_offers_an_explicit_resume(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    wizard_flow_id = await _start_goal_wizard(db)

    async def fake_hint(_user_id: int) -> str:
        return "hint"

    from telegram import InlineKeyboardMarkup

    from noam_coach.bot.ui import button

    async def fake_keyboard(_user_id: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[button("📊 מצב", "menu:status")]])

    monkeypatch.setattr(coach_bot, "_home_hint", fake_hint)
    monkeypatch.setattr(coach_bot, "home_keyboard_for_user", fake_keyboard)

    delegated: list[str] = []

    async def spy(query: Any, user_id: int, data: str) -> bool:
        delegated.append(data)
        return True

    real = coach_bot.handle_menu_callback
    coach_bot.handle_menu_callback = spy
    try:
        install_flow_convergence()  # wraps the spy
        query = FakeQuery()
        handled = await coach_bot.handle_menu_callback(query, USER_ID, "menu:home")

        assert handled is True
        assert delegated == []  # protected clear_all_flows path never ran
        # Explicitly resumable: idle flow with the wizard in the snapshot...
        active = await conversation.get_active_flow(db, USER_ID)
        assert active.is_idle  # ...so free text routes idle: no invisible continuation
        assert active.suspended and active.suspended.get("flow_id") == wizard_flow_id
        # ...the scratchpad survives...
        assert await _wizard_row(db) is not None
        # ...and the home render carries the resume control with the flow id.
        assert f"resume:continue:f{wizard_flow_id}" in _controls(query.markups[-1])
        events = await event_log.list_events(db, USER_ID)
        suspended = _events(events, "flow.suspended")
        assert any(
            e.properties.get("reason") == "home_exit" for e in suspended
        )
    finally:
        uninstall_flow_convergence()
        coach_bot.handle_menu_callback = real


@pytest.mark.asyncio
async def test_resume_control_after_home_reenters_the_wizard(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B6+B7 composition: the Home resume control re-renders the pending
    wizard question through advance_after_answer."""
    wizard_flow_id = await _start_goal_wizard(db)

    async def fake_hint(_user_id: int) -> str:
        return "hint"

    from telegram import InlineKeyboardMarkup

    from noam_coach.bot.ui import button

    async def fake_keyboard(_user_id: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[button("📊 מצב", "menu:status")]])

    monkeypatch.setattr(coach_bot, "_home_hint", fake_hint)
    monkeypatch.setattr(coach_bot, "home_keyboard_for_user", fake_keyboard)

    install_restart_resume()
    install_flow_convergence()

    # Home: suspend.
    home_query = FakeQuery()
    await coach_bot.handle_menu_callback(home_query, USER_ID, "menu:home")
    assert (await conversation.get_active_flow(db, USER_ID)).is_idle

    # Resume: the wizard asks its pending question again.
    resume_query = FakeQuery()
    handled = await coach_bot.handle_menu_callback(
        resume_query, USER_ID, f"resume:continue:f{wizard_flow_id}"
    )
    assert handled is True
    rendered = " ".join(resume_query.edits)
    assert "שאלה להשלמת היעד" in rendered
    active = await conversation.get_active_flow(db, USER_ID)
    assert not active.is_idle  # the wizard question flow is live again


@pytest.mark.asyncio
async def test_home_with_no_meaningful_flow_keeps_the_old_behavior(
    db: Database,
) -> None:
    """Trivial/no flow: Home delegates to the protected handler unchanged —
    no added friction, no resume offer."""
    delegated: list[str] = []

    async def spy(query: Any, user_id: int, data: str) -> bool:
        delegated.append(data)
        return True

    real = coach_bot.handle_menu_callback
    coach_bot.handle_menu_callback = spy
    try:
        install_flow_convergence()
        handled = await coach_bot.handle_menu_callback(FakeQuery(), USER_ID, "menu:home")
        assert handled is True
        assert delegated == ["menu:home"]
    finally:
        uninstall_flow_convergence()
        coach_bot.handle_menu_callback = real


@pytest.mark.asyncio
async def test_qa_tap_on_suspended_wizard_resumes_and_answers(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tapping the wizard's own answer button after Home is an EXPLICIT
    continue: the suspended flow is restored and the answer proceeds."""
    await _start_goal_wizard(db)
    await conversation.set_active_flow(
        db, USER_ID, conversation.FlowName.idle, suspend_current=True
    )

    delegated: list[str] = []

    async def spy(query: Any, user_id: int, data: str) -> None:
        delegated.append(data)

    real = coach_bot.handle_onboarding_callback
    coach_bot.handle_onboarding_callback = spy
    try:
        install_flow_convergence()
        await coach_bot.handle_onboarding_callback(
            FakeQuery(), USER_ID, "qa:q_height_cm:0"
        )
        assert delegated == ["qa:q_height_cm:0"]  # answer proceeded
        active = await conversation.get_active_flow(db, USER_ID)
        assert not active.is_idle  # flow restored before the answer
    finally:
        uninstall_flow_convergence()
        coach_bot.handle_onboarding_callback = real
