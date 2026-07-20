"""B13 — final adversarial audit regressions.

The audit assumed every batch was wrong and re-ran the mechanical sweeps.
Two real contradictions survived B5's identity work and are fixed + pinned
here:

1. nextmeal:dislikeitem resolved the item for a PERMANENT avoid-preference
   from a fresh regeneration — under served-title rotation the persisted
   dislike could name a different meal than the displayed card. The
   identity gate now resolves it from the ACTIVE stored card.
2. legacy nextmeal:plan callbacks (old cards) were not identity-gated: with
   no active recommendation the handler silently planned a REGENERATED meal.
   plan is now a gated prefix (no-active/stale-card → refusal).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import coach_bot
import user_model
from config import TZ
from db import Database
from helpers import utc_now
from noam_coach.observability import ObservabilityMode, set_mode
from noam_coach.observability.emit import reset_observability_health
from noam_coach.observability.modes import reset_mode
from noam_coach.services.next_meal import (
    generate_next_meal_recommendation,
    record_next_meal_served,
    remember_active_recommendation,
)
from noam_coach.services.recommendation_identity import (
    install_recommendation_identity_gate,
    uninstall_recommendation_identity_gate,
)

USER_ID = 1
NOW = datetime.now(TZ)


class FakeQuery:
    def __init__(self, message_id: int = 900) -> None:
        self.message = SimpleNamespace(
            message_id=message_id, chat=SimpleNamespace(id=USER_ID)
        )
        self.from_user = SimpleNamespace(id=USER_ID)
        self.edits: list[str] = []

    async def edit_message_text(
        self, text: str, reply_markup: Any = None, parse_mode: str | None = None
    ) -> None:
        self.edits.append(text)

    async def answer(self, *a: Any, **k: Any) -> None:
        return None


@pytest.fixture
async def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Database:
    database = Database(str(tmp_path / "b13.db"))
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


@pytest.mark.asyncio
async def test_dislikeitem_persists_the_displayed_item_not_a_regeneration(
    db: Database,
) -> None:
    """AUDIT FINDING 1: a permanent avoid-preference must name the meal the
    user was LOOKING at, even after ranking rotation."""
    displayed = await generate_next_meal_recommendation(db, USER_ID, now=NOW)
    displayed_title = displayed.options[0].title
    await remember_active_recommendation(db, USER_ID, displayed, message_id=900)

    # Ranking rotates: a fresh regeneration now leads with a DIFFERENT meal.
    await record_next_meal_served(db, USER_ID, displayed)
    fresh = await generate_next_meal_recommendation(db, USER_ID, now=NOW)
    assert fresh.options[0].title != displayed_title

    install_recommendation_identity_gate()
    query = FakeQuery(message_id=900)
    handled = await coach_bot.handle_menu_callback(
        query, USER_ID, "nextmeal:dislikeitem:1"
    )
    assert handled is True

    disliked = await user_model.get_value(db, USER_ID, "disliked_foods")
    assert disliked, "the permanent preference must persist"
    assert displayed_title in str(disliked)
    assert fresh.options[0].title not in str(disliked)  # never the regenerated one


@pytest.mark.asyncio
async def test_plan_without_active_recommendation_is_refused(db: Database) -> None:
    """AUDIT FINDING 2: a legacy plan control from an expired card must not
    silently plan a regenerated meal."""
    calls: list[str] = []

    async def spy(query: Any, user_id: int, data: str) -> bool:
        calls.append(data)
        return True

    real = coach_bot.handle_menu_callback
    coach_bot.handle_menu_callback = spy
    try:
        install_recommendation_identity_gate()
        query = FakeQuery()
        handled = await coach_bot.handle_menu_callback(query, USER_ID, "nextmeal:plan:1")
        assert handled is True
        assert calls == []  # refused at the gate — never delegated
        assert any("לא פעילה" in text for text in query.edits)
        flags_row = await db.fetch_one(
            "SELECT flags FROM daily_flags WHERE user_id=?", (USER_ID,)
        )
        assert flags_row is None or "next_meal_planned" not in str(flags_row["flags"])
    finally:
        uninstall_recommendation_identity_gate()
        coach_bot.handle_menu_callback = real


@pytest.mark.asyncio
async def test_plan_on_superseded_card_is_refused(db: Database) -> None:
    displayed = await generate_next_meal_recommendation(db, USER_ID, now=NOW)
    await remember_active_recommendation(db, USER_ID, displayed, message_id=800)

    calls: list[str] = []

    async def spy(query: Any, user_id: int, data: str) -> bool:
        calls.append(data)
        return True

    real = coach_bot.handle_menu_callback
    coach_bot.handle_menu_callback = spy
    try:
        install_recommendation_identity_gate()
        old_card = FakeQuery(message_id=555)
        handled = await coach_bot.handle_menu_callback(old_card, USER_ID, "nextmeal:plan:1")
        assert handled is True and calls == []
        assert any("קודמת" in text for text in old_card.edits)
    finally:
        uninstall_recommendation_identity_gate()
        coach_bot.handle_menu_callback = real
