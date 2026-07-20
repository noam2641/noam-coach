"""B5 / ARCH-06 — stable recommendation/control identity anchors.

Required regressions per family: render A delivered → displayed option X →
state/ranking changes → user presses the control from render A → the
mutation targets X (never a regenerated replacement); stale/unresolved
source renders are refused explicitly.
"""

from __future__ import annotations

from datetime import datetime
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
from noam_coach.services.next_meal import (
    generate_next_meal_recommendation,
    option_fingerprint,
    record_next_meal_served,
    remember_active_recommendation,
    save_next_meal_option_feedback,
    save_next_meal_unavailable_item,
)
from noam_coach.services.recommendation_identity import (
    install_recommendation_identity_gate,
    uninstall_recommendation_identity_gate,
)

USER_ID = 1
# The render-level gate reads the active state with the REAL clock (as
# production does), so the remembered card must be fresh relative to it —
# a fixed past date would silently expire past the 6h recommendation TTL.
NOW = datetime.now(TZ)


class FakeQuery:
    def __init__(self, message_id: int = 500) -> None:
        self.message = SimpleNamespace(message_id=message_id, chat=SimpleNamespace(id=USER_ID))
        self.from_user = SimpleNamespace(id=USER_ID)
        self.edits: list[str] = []

    async def edit_message_text(self, text: str, reply_markup: Any = None, parse_mode: str | None = None) -> None:
        self.edits.append(text)

    async def answer(self, *a: Any, **k: Any) -> None:
        return None


@pytest.fixture
async def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Database:
    database = Database(str(tmp_path / "b5.db"))
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
    uninstall_recommendation_identity_gate()
    reset_mode()
    reset_observability_health()


# ---------------------------------------------------------------------------
# Service-level identity anchor (dislike / nostock)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dislike_targets_the_displayed_option_not_a_regeneration(db: Database) -> None:
    """render A displayed option X → ranking rotates (served-title rotation)
    → dislike(1) must reject X, never the regenerated replacement."""
    displayed = await generate_next_meal_recommendation(db, USER_ID, now=NOW)
    displayed_option = displayed.options[0]
    displayed_fp = option_fingerprint(displayed_option)
    await remember_active_recommendation(db, USER_ID, displayed, message_id=500, now=NOW)

    # State/ranking changes AFTER the render: the rotation now yields a
    # different top option on any fresh regeneration.
    await record_next_meal_served(db, USER_ID, displayed)
    fresh = await generate_next_meal_recommendation(db, USER_ID, now=NOW)
    assert fresh.options[0].title != displayed_option.title  # rotation is real

    saved_title, refreshed = await save_next_meal_option_feedback(db, USER_ID, 1, now=NOW)

    # The mutation targeted the DISPLAYED option.
    assert saved_title == displayed_option.title
    from noam_coach.services.daily_flags_cas import get_daily_flags_with_revision
    from noam_coach.services.daily_state import coaching_day_key

    day = await coaching_day_key(db, USER_ID, NOW)
    flags, _ = await get_daily_flags_with_revision(db, USER_ID, day)
    rejected = {
        str(r.get("fingerprint"))
        for r in (flags.get("next_meal_rejections") or [])
        if isinstance(r, dict)
    }
    assert displayed_fp in rejected, (sorted(rejected), displayed_fp)
    assert option_fingerprint(fresh.options[0]) not in rejected
    # And the refreshed recommendation is a genuinely different meal.
    assert all(option.title != displayed_option.title for option in refreshed.options)


@pytest.mark.asyncio
async def test_nostock_shares_the_same_anchor(db: Database) -> None:
    displayed = await generate_next_meal_recommendation(db, USER_ID, now=NOW)
    await remember_active_recommendation(db, USER_ID, displayed, message_id=500, now=NOW)
    await record_next_meal_served(db, USER_ID, displayed)

    item, _refreshed = await save_next_meal_unavailable_item(db, USER_ID, 1, now=NOW)
    assert item == displayed.options[0].title


@pytest.mark.asyncio
async def test_dislike_without_active_recommendation_refuses(db: Database) -> None:
    with pytest.raises(ValueError):
        await save_next_meal_option_feedback(db, USER_ID, 1, now=NOW)


# ---------------------------------------------------------------------------
# Render-level gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [
    "nextmeal:dislike:1", "nextmeal:nostock:1",
    "nextmeal:smaller:1", "nextmeal:bigger:1", "nextmeal:editqty:1",
])
async def test_gate_refuses_when_no_active_recommendation(db: Database, data: str) -> None:
    calls: list[str] = []

    async def spy_handler(query: Any, user_id: int, payload: str) -> bool:
        calls.append(payload)
        return True

    real_handler = coach_bot.handle_menu_callback
    coach_bot.handle_menu_callback = spy_handler
    try:
        install_recommendation_identity_gate()
        query = FakeQuery(message_id=500)
        handled = await coach_bot.handle_menu_callback(query, USER_ID, data)
        assert handled is True
        assert calls == []  # never delegated — refused at the gate
        assert any("לא פעילה" in text for text in query.edits)
        refused = [e for e in await event_log.list_events(db, USER_ID, event="validation.failed")
                   if e.entity == "recommendation_control"]
        assert refused and refused[0].properties["reason"] == "no_active_recommendation"
    finally:
        uninstall_recommendation_identity_gate()
        coach_bot.handle_menu_callback = real_handler


@pytest.mark.asyncio
async def test_gate_refuses_control_from_superseded_card(db: Database) -> None:
    """Pressing save/dislike on an OLD card while a NEWER recommendation is
    active must refuse — never apply the action to the newer meal."""
    displayed = await generate_next_meal_recommendation(db, USER_ID, now=NOW)
    await remember_active_recommendation(db, USER_ID, displayed, message_id=800, now=NOW)

    calls: list[str] = []

    async def spy_handler(query: Any, user_id: int, payload: str) -> bool:
        calls.append(payload)
        return True

    real_handler = coach_bot.handle_menu_callback
    coach_bot.handle_menu_callback = spy_handler
    try:
        install_recommendation_identity_gate()
        old_card = FakeQuery(message_id=555)  # NOT the active card (800)
        handled = await coach_bot.handle_menu_callback(old_card, USER_ID, "nextmeal:save:1")
        assert handled is True and calls == []
        assert any("קודמת" in text for text in old_card.edits)

        # The live card delegates normally.
        live_card = FakeQuery(message_id=800)
        await coach_bot.handle_menu_callback(live_card, USER_ID, "nextmeal:save:1")
        assert calls == ["nextmeal:save:1"]
    finally:
        uninstall_recommendation_identity_gate()
        coach_bot.handle_menu_callback = real_handler


@pytest.mark.asyncio
async def test_gate_permissive_when_message_id_unknown(db: Database) -> None:
    """Unprovable mismatch is not proof of staleness: active state without a
    recorded message id delegates normally."""
    displayed = await generate_next_meal_recommendation(db, USER_ID, now=NOW)
    await remember_active_recommendation(db, USER_ID, displayed, now=NOW)  # no message_id

    calls: list[str] = []

    async def spy_handler(query: Any, user_id: int, payload: str) -> bool:
        calls.append(payload)
        return True

    real_handler = coach_bot.handle_menu_callback
    coach_bot.handle_menu_callback = spy_handler
    try:
        install_recommendation_identity_gate()
        await coach_bot.handle_menu_callback(FakeQuery(1), USER_ID, "nextmeal:dislike:1")
        assert calls == ["nextmeal:dislike:1"]
    finally:
        uninstall_recommendation_identity_gate()
        coach_bot.handle_menu_callback = real_handler


@pytest.mark.asyncio
async def test_end_to_end_dislike_through_real_handler_with_gate(db: Database) -> None:
    """Full production path: gate installed over the REAL handler; the live
    card's dislike lands on the displayed option."""
    displayed = await generate_next_meal_recommendation(db, USER_ID, now=NOW)
    displayed_title = displayed.options[0].title
    await remember_active_recommendation(db, USER_ID, displayed, message_id=900, now=NOW)
    await record_next_meal_served(db, USER_ID, displayed)

    install_recommendation_identity_gate()
    try:
        query = FakeQuery(message_id=900)
        handled = await coach_bot.handle_menu_callback(query, USER_ID, "nextmeal:dislike:1")
        assert handled is True
        rendered = " ".join(query.edits)
        assert displayed_title in rendered  # "רשמתי שלא מתאים לך עכשיו <displayed>"
    finally:
        uninstall_recommendation_identity_gate()
