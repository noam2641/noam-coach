"""B8 / ARCH-01 phase 2 + ARCH-10 — remaining continuation stores and
centralized Home/cancel semantics.

Store policies (the explicit per-store contract):
- goal_wizard, profile_field_edit — flow-scoped scratchpads: suspend with
  the flow (Home on meaningful work), expire with the flow, invalidated by
  terminal cancel and by Home when trivial/orphaned.
- deferred_plan, plan_completion — resumable continuations (ARCH-11):
  preserved across Home and restart, invalidated ONLY by terminal cancel.
- health_confirm — owns its own step lifecycle (done/deferred payload);
  preserved except terminal cancel.

Required trace: Home/cancel emits the complete lifecycle/invalidation set;
no orphan continuation row remains actionable after terminal cancellation.
"""

from __future__ import annotations

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
    terminal_cancel_command,
    uninstall_flow_convergence,
)
from noam_coach.services.flow_resume import (
    install_restart_resume,
    uninstall_restart_resume,
)

USER_ID = 1

ALL_STORES = (
    "goal_wizard",
    "profile_field_edit",
    "deferred_plan",
    "plan_completion",
    "health_confirm",
)


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
    def __init__(self) -> None:
        self.replies: list[str] = []

    async def reply_text(self, text: str, reply_markup: Any = None, parse_mode: str | None = None) -> None:
        self.replies.append(text)


def _fake_update() -> Any:
    return SimpleNamespace(
        effective_user=SimpleNamespace(
            id=USER_ID, first_name="Test", username=None, is_bot=False
        ),
        effective_message=FakeMessage(),
        effective_chat=SimpleNamespace(id=USER_ID),
    )


@pytest.fixture
async def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Database:
    database = Database(str(tmp_path / "b8.db"))
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
    install_state_trace()
    yield
    uninstall_flow_convergence()
    uninstall_restart_resume()
    uninstall_state_trace()
    reset_mode()
    reset_observability_health()


def _events(events: list[Any], name: str) -> list[Any]:
    return [e for e in events if e.event == name]


async def _seed_store(db: Database, flow: str, step: str = "s") -> None:
    await db.execute(
        """
        INSERT INTO conversation_state(user_id, flow, step, payload, updated_at)
        VALUES(?, ?, ?, '{}', ?)
        ON CONFLICT(user_id, flow) DO UPDATE SET step=excluded.step
        """,
        (USER_ID, flow, step, utc_now()),
    )


async def _stores_present(db: Database) -> set[str]:
    rows = await db.fetch_all(
        "SELECT flow FROM conversation_state WHERE user_id=?", (USER_ID,)
    )
    return {str(r["flow"]) for r in rows}


# ---------------------------------------------------------------------------
# Terminal cancel — the complete invalidation set
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminal_cancel_invalidates_every_continuation_store(
    db: Database,
) -> None:
    """/cancel while flows + all five scratchpad stores are alive: nothing
    resumable/actionable may remain, and each invalidation is traced."""
    for store in ALL_STORES:
        await _seed_store(db, store)
    await conversation.set_active_flow(
        db, USER_ID, conversation.FlowName.onboarding_question, step="q_height_cm"
    )
    install_flow_convergence()

    update = _fake_update()
    await terminal_cancel_command(update, None)

    # Original escape-hatch behavior preserved (reply + flow cleared)...
    assert update.effective_message.replies
    assert (await conversation.get_active_flow(db, USER_ID)).is_idle
    # ...and no continuation row survives the terminal cancel.
    assert await _stores_present(db) == set()
    events = await event_log.list_events(db, USER_ID)
    invalidated = [
        e for e in _events(events, "state.mutated")
        if e.properties.get("reason") == "user_cancel"
    ]
    assert {e.entity_id for e in invalidated} == set(ALL_STORES)


@pytest.mark.asyncio
async def test_no_orphan_row_is_actionable_after_terminal_cancel(
    db: Database,
) -> None:
    """After /cancel: no restart-resume offer fires and a late wizard tap is
    refused — the cancellation is genuinely terminal."""
    await _seed_store(db, "goal_wizard", step="q_height_cm")
    await _seed_store(db, "deferred_plan", step="3")
    await conversation.set_active_flow(
        db, USER_ID, conversation.FlowName.onboarding_question, step="q_height_cm"
    )
    install_restart_resume()
    install_flow_convergence()

    await terminal_cancel_command(_fake_update(), None)

    # Restart-style load finds nothing to offer...
    await coach_bot.load_pending_state()
    delegated: list[str] = []

    async def spy(query: Any, user_id: int, data: str) -> bool:
        delegated.append(data)
        return True

    real = coach_bot.handle_menu_callback
    # Rewire the outermost wrap chain over the spy.
    uninstall_flow_convergence()
    uninstall_restart_resume()
    coach_bot.handle_menu_callback = spy
    try:
        install_restart_resume()
        install_flow_convergence()
        await coach_bot.load_pending_state()
        handled = await coach_bot.handle_menu_callback(FakeQuery(), USER_ID, "menu:home")
        assert handled is True
        assert delegated == ["menu:home"]  # no resume offer — nothing resumable
    finally:
        uninstall_flow_convergence()
        uninstall_restart_resume()
        coach_bot.handle_menu_callback = real


@pytest.mark.asyncio
async def test_cancel_without_scratchpad_stays_quiet(db: Database) -> None:
    install_flow_convergence()
    update = _fake_update()
    await terminal_cancel_command(update, None)
    assert update.effective_message.replies  # normal escape-hatch reply
    events = await event_log.list_events(db, USER_ID)
    assert [
        e for e in _events(events, "state.mutated")
        if e.properties.get("reason") == "user_cancel"
    ] == []


# ---------------------------------------------------------------------------
# Home — trivial/orphaned flow-scoped rows must not stay behind
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_home_clears_trivial_profile_edit_scratchpad(db: Database) -> None:
    """A single-field profile edit is trivial by the resolved decision: Home
    exits it — and must not leave its scratchpad row alive behind the
    cleared flow."""
    await _seed_store(db, "profile_field_edit", step="height_cm")
    await conversation.set_active_flow(
        db, USER_ID, conversation.FlowName.onboarding_question, step="height_cm"
    )

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
        assert delegated == ["menu:home"]  # trivial → normal Home behavior
        assert "profile_field_edit" not in await _stores_present(db)
        events = await event_log.list_events(db, USER_ID)
        cleared = [
            e for e in _events(events, "state.mutated")
            if e.entity_id == "profile_field_edit"
        ]
        assert cleared and cleared[0].properties["reason"] == "home_exit"
    finally:
        uninstall_flow_convergence()
        coach_bot.handle_menu_callback = real


@pytest.mark.asyncio
async def test_home_preserves_resumable_continuations(db: Database) -> None:
    """deferred_plan/plan_completion are ARCH-11 continuations: Home leaves
    them resumable (only terminal cancel invalidates them)."""
    await _seed_store(db, "deferred_plan", step="3")
    await _seed_store(db, "health_confirm", step="step1")

    delegated: list[str] = []

    async def spy(query: Any, user_id: int, data: str) -> bool:
        delegated.append(data)
        return True

    real = coach_bot.handle_menu_callback
    coach_bot.handle_menu_callback = spy
    try:
        install_flow_convergence()
        await coach_bot.handle_menu_callback(FakeQuery(), USER_ID, "menu:home")
        present = await _stores_present(db)
        assert "deferred_plan" in present
        assert "health_confirm" in present
    finally:
        uninstall_flow_convergence()
        coach_bot.handle_menu_callback = real


# ---------------------------------------------------------------------------
# Expiry — extended to every flow-scoped scratchpad
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expiry_clears_profile_edit_scratchpad_too(db: Database) -> None:
    from datetime import datetime, timedelta, timezone

    await _seed_store(db, "profile_field_edit", step="height_cm")
    await conversation.set_active_flow(
        db, USER_ID, conversation.FlowName.onboarding_question, step="height_cm"
    )
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    await db.execute(
        "UPDATE active_flow SET expires_at=? WHERE user_id=?", (past, USER_ID)
    )
    install_flow_convergence()

    flow = await conversation.expire_if_needed(db, USER_ID)

    assert flow.is_idle
    assert "profile_field_edit" not in await _stores_present(db)
