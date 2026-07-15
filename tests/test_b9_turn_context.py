"""B9 / ARCH-08+16 — AssistantTurnContext + deterministic reference resolution.

Required traces exercised here:
- "כן" resolves to the correct pending confirmation entity (recovered from
  the DISPLAYED confirm control via render evidence), and the final domain
  mutation references the same entity id.
- "השני" resolves to the second DISPLAYED option (fingerprint identity),
  never a regenerated list.
- "תשמור את זה" resolves to the selected recommendation and dispatches on
  the ACTIVE card's message id (so the B5 identity gate validates it).
- "תחזור" resolves to the suspended flow and resumes it.
- Unresolved turns delegate to the classifier WITH bounded candidates
  (never forcing a guess); context.built is emitted per turn.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import coach_bot
import conversation
import event_log
from config import TZ
from db import Database
from helpers import utc_now
from noam_coach.observability import (
    ObservabilityMode,
    interaction_scope,
    set_mode,
)
from noam_coach.observability.emit import reset_observability_health
from noam_coach.observability.modes import reset_mode
from noam_coach.services.turn_context import (
    build_turn_context,
    install_turn_context,
    resolve_reference,
    uninstall_turn_context,
)

USER_ID = 1
NOW = datetime.now(TZ)


class FakeMessage:
    def __init__(self, text: str = "", message_id: int = 42) -> None:
        self.text = text
        self.message_id = message_id
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
    database = Database(str(tmp_path / "b9.db"))
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
    yield
    uninstall_turn_context()
    reset_mode()
    reset_observability_health()


def _events(events: list[Any], name: str) -> list[Any]:
    return [e for e in events if e.event == name]


async def _seed_two_option_recommendation(db: Database) -> list[str]:
    """A displayed card with TWO options whose identities we control."""
    from noam_coach.services import next_meal as next_meal_module
    from noam_coach.services.next_meal import (
        generate_next_meal_recommendation,
        option_fingerprint,
        remember_active_recommendation,
    )

    await db.execute(
        """
        INSERT INTO goal_versions(user_id, calories, protein, steps, phase, status, source, created_at)
        VALUES(?, 2100, 150, 8000, 'fat_loss_muscle_retention', 'active', 'test', ?)
        """,
        (USER_ID, utc_now()),
    )
    rec = await generate_next_meal_recommendation(db, USER_ID, now=NOW)
    first = rec.options[0]
    import dataclasses

    second = dataclasses.replace(first, title=first.title + " — גרסה 2")
    rec = dataclasses.replace(rec, options=[first, second])
    await remember_active_recommendation(db, USER_ID, rec, message_id=900, now=NOW)
    del next_meal_module
    return [option_fingerprint(first), option_fingerprint(second)]


async def _seed_pending_confirmation(db: Database) -> None:
    """Pending goal-calorie confirmation + the render evidence for its card."""
    await conversation.set_active_flow(
        db, USER_ID, conversation.FlowName.confirm_number,
        step="active", payload={"value": 2100.0, "text": "יעד 2100"},
    )
    from noam_coach.observability import taxonomy
    from noam_coach.observability.emit import emit_event

    with interaction_scope(user_id=USER_ID):
        await emit_event(
            db, USER_ID, taxonomy.UI_RENDER_PREPARED,
            entity="render", entity_id="r-1", source="bot", status="prepared",
            properties={
                "render_id": "r-1",
                "controls": [
                    {"label": "✅ אשר", "callback_data": "confirm:goal_cal:2100"},
                    {"label": "בטל", "callback_data": "confirm:cancel:0"},
                ],
            },
        )


# ---------------------------------------------------------------------------
# Deterministic resolutions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_yes_resolves_to_the_pending_confirmation_entity(db: Database) -> None:
    await _seed_pending_confirmation(db)

    dispatched: list[tuple[Any, str]] = []

    async def spy(query: Any, user_id: int, data: str) -> bool:
        dispatched.append((query, data))
        return True

    real = coach_bot.handle_menu_callback
    coach_bot.handle_menu_callback = spy
    original_routes: list[str] = []

    async def route_spy(update: Any, user_id: int) -> None:
        original_routes.append(update.effective_message.text)

    real_route = coach_bot.route_free_text
    coach_bot.route_free_text = route_spy
    try:
        install_turn_context()
        await coach_bot.route_free_text(_update("כן"), USER_ID)

        assert original_routes == []  # resolved deterministically, no LLM
        assert [d for _q, d in dispatched] == ["confirm:goal_cal:2100"]
        events = await event_log.list_events(db, USER_ID)
        finalized = [
            e for e in _events(events, "decision.finalized")
            if e.entity == "reference_resolution"
        ]
        assert finalized and finalized[0].entity_id == "goal_cal:2100.0"
        assert finalized[0].properties["decision"] == "yes"
        assert _events(events, "context.built")
    finally:
        uninstall_turn_context()
        coach_bot.route_free_text = real_route
        coach_bot.handle_menu_callback = real


@pytest.mark.asyncio
async def test_no_resolves_to_cancel_of_the_pending_entity(db: Database) -> None:
    await _seed_pending_confirmation(db)
    dispatched: list[str] = []

    async def spy(query: Any, user_id: int, data: str) -> bool:
        dispatched.append(data)
        return True

    real = coach_bot.handle_menu_callback
    coach_bot.handle_menu_callback = spy
    try:
        install_turn_context()
        await coach_bot.route_free_text(_update("לא"), USER_ID)
        assert dispatched == ["confirm:cancel:0"]
    finally:
        uninstall_turn_context()
        coach_bot.handle_menu_callback = real


@pytest.mark.asyncio
async def test_ordinal_resolves_to_the_second_displayed_option(db: Database) -> None:
    """"השני" must target the DISPLAYED option #2 by fingerprint — a
    regenerated list plays no part in the resolution."""
    fingerprints = await _seed_two_option_recommendation(db)

    dispatched: list[str] = []

    async def spy(query: Any, user_id: int, data: str) -> bool:
        dispatched.append(data)
        return True

    real = coach_bot.handle_menu_callback
    coach_bot.handle_menu_callback = spy
    try:
        install_turn_context()
        await coach_bot.route_free_text(_update("השני"), USER_ID)

        assert dispatched == ["nextmeal:choose:2"]
        events = await event_log.list_events(db, USER_ID)
        finalized = [
            e for e in _events(events, "decision.finalized")
            if e.entity == "reference_resolution"
        ]
        assert finalized and finalized[0].entity_id == fingerprints[1]
    finally:
        uninstall_turn_context()
        coach_bot.handle_menu_callback = real


@pytest.mark.asyncio
async def test_save_this_resolves_to_the_selected_option_on_the_active_card(
    db: Database,
) -> None:
    """"תשמור את זה" → the SELECTED option, dispatched as a press on the
    active card's message id so the B5 identity gate validates it."""
    fingerprints = await _seed_two_option_recommendation(db)
    from noam_coach.services.next_meal import mark_active_recommendation_selection

    await mark_active_recommendation_selection(db, USER_ID, 2, now=NOW)

    pressed: list[tuple[Any, str]] = []

    async def spy(query: Any, user_id: int, data: str) -> bool:
        pressed.append((query, data))
        return True

    real = coach_bot.handle_menu_callback
    coach_bot.handle_menu_callback = spy
    try:
        install_turn_context()
        await coach_bot.route_free_text(_update("תשמור את זה"), USER_ID)

        assert [d for _q, d in pressed] == ["nextmeal:save:2"]
        query = pressed[0][0]
        assert query.message.message_id == 900  # the ACTIVE card, not the text
        events = await event_log.list_events(db, USER_ID)
        finalized = [
            e for e in _events(events, "decision.finalized")
            if e.entity == "reference_resolution"
        ]
        assert finalized and finalized[0].entity_id == fingerprints[1]
    finally:
        uninstall_turn_context()
        coach_bot.handle_menu_callback = real


@pytest.mark.asyncio
async def test_return_resolves_to_the_suspended_flow(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    await conversation.set_active_flow(
        db, USER_ID, conversation.FlowName.onboarding_question, step="q_height_cm"
    )
    suspended = await conversation.get_active_flow(db, USER_ID)
    await conversation.set_active_flow(
        db, USER_ID, conversation.FlowName.idle, suspend_current=True
    )

    resumed: list[int] = []

    async def fake_advance(target: Any, user_id: int) -> None:
        resumed.append(user_id)

    from noam_coach.bot import onboarding as onboarding_bot

    monkeypatch.setattr(onboarding_bot, "advance_after_answer", fake_advance)
    try:
        install_turn_context()
        await coach_bot.route_free_text(_update("תחזור"), USER_ID)

        assert resumed == [USER_ID]
        active = await conversation.get_active_flow(db, USER_ID)
        assert active.flow_id == suspended.flow_id  # the SAME suspended flow
        events = await event_log.list_events(db, USER_ID)
        finalized = [
            e for e in _events(events, "decision.finalized")
            if e.entity == "reference_resolution"
        ]
        assert finalized and finalized[0].entity_id == suspended.flow_id
    finally:
        uninstall_turn_context()


# ---------------------------------------------------------------------------
# Unresolved → classifier with bounded candidates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unresolved_turn_delegates_with_bounded_candidates(
    db: Database,
) -> None:
    await _seed_two_option_recommendation(db)

    import assistant as assistant_root

    seen_candidates: list[Any] = []

    async def route_spy(update: Any, user_id: int) -> None:
        seen_candidates.append(assistant_root.REFERENCE_CANDIDATES.get())

    real_route = coach_bot.route_free_text
    coach_bot.route_free_text = route_spy
    try:
        install_turn_context()
        await coach_bot.route_free_text(_update("מה אכלתי היום בכלל?"), USER_ID)

        assert len(seen_candidates) == 1
        candidates = seen_candidates[0]
        assert candidates is not None  # candidates handed over, not guessed
        assert len(candidates["recommendation_options"]) == 2
        assert candidates["recommendation_options"][0]["fingerprint"]
        # ...and the contextvar does not leak past the turn.
        assert assistant_root.REFERENCE_CANDIDATES.get() is None
    finally:
        uninstall_turn_context()
        coach_bot.route_free_text = real_route


@pytest.mark.asyncio
async def test_yes_without_render_evidence_stays_unresolved(db: Database) -> None:
    """No proven displayed confirm control → 'כן' is NOT guessed; it goes to
    classification with the pending confirmation as a candidate."""
    await conversation.set_active_flow(
        db, USER_ID, conversation.FlowName.confirm_number,
        step="active", payload={"value": 2100.0, "text": "יעד 2100"},
    )

    ctx = await build_turn_context(db, USER_ID, "כן")
    assert resolve_reference(ctx) is None
    assert ctx.pending_confirmation is not None
    assert ctx.pending_confirmation.get("control") is None


@pytest.mark.asyncio
async def test_context_built_is_bounded(db: Database) -> None:
    await _seed_two_option_recommendation(db)
    ctx = await build_turn_context(db, USER_ID, "משהו")
    events = await event_log.list_events(db, USER_ID)
    built = _events(events, "context.built")
    assert built
    sections = built[-1].properties["sections"]
    assert set(sections) == {
        "active_flow", "suspended_flow", "pending_confirmation",
        "recommendation_options", "selected_option", "resumable_stores",
    }
    for option in sections["recommendation_options"]:
        assert set(option) == {"number", "title", "fingerprint"}
    del ctx
