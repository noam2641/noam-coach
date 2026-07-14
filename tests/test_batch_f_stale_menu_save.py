"""Batch F: dailymenu:save: must reject a menu marked stale by
day_state_invalidation.invalidate_day_projections (Batch A), not just check
menu_id equality.

Root cause (verified before this fix): a meal create/edit/undo event
already marks the active daily menu ``stale`` (Batch A), but the
dailymenu:save: handler only ever checked ``menu_id`` match -- a menu
invalidated by a meal event minutes ago, with the SAME menu_id (nothing
regenerated it yet), was still silently accepted as current.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import coach_bot
import user_model
from db import Database
from helpers import utc_now
from noam_coach.bot import callback_menu as callback_menu_bot
from noam_coach.services.daily_menu_state import (
    MenuMealRecord,
    mark_daily_menu_stale,
    remember_active_daily_menu,
)


class FakeQuery:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.reply_markups: list[Any] = []

    async def edit_message_text(self, text: str, reply_markup: Any = None, parse_mode: str | None = None) -> None:
        del parse_mode
        self.messages.append(text)
        self.reply_markups.append(reply_markup)

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        del text, show_alert


async def _ready_db(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "batch_f_stale_menu.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'Test',NULL,?)",
        (utc_now(),),
    )
    await db.execute(
        """
        INSERT INTO goal_versions(user_id, calories, protein, steps, phase, status, source, created_at)
        VALUES(1, 2000, 150, 8000, 'fat_loss_muscle_retention', 'active', 'test', ?)
        """,
        (utc_now(),),
    )
    await user_model.set_fact(db, 1, "sleep_schedule", {"bedtime": "23:00"}, source=user_model.SOURCE_USER, confirmed=True)
    for key, value in {
        "primary_goal": "fat_loss_muscle_retention",
        "weight_kg": 80, "sex": "male", "age": 30,
        "diet_restrictions": "none", "allergies": "none",
    }.items():
        await user_model.set_fact(db, 1, key, value, source=user_model.SOURCE_USER, confirmed=True)
    return db


async def _consumed_meal_names(db: Database) -> list[str]:
    rows = await db.fetch_all("SELECT name FROM meals WHERE user_id=1 AND status='consumed'")
    return [str(row["name"]) for row in rows]


@pytest.mark.asyncio
async def test_dailymenu_save_rejects_menu_marked_stale_by_meal_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = await _ready_db(tmp_path)
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(callback_menu_bot, "DB", db)

    meal_a = MenuMealRecord(
        meal_id="breakfast-0", slot="breakfast", time="08:00",
        role="ארוחת בוקר", calories=555, protein=44, note="שקשוקה",
    )
    saved_state = await remember_active_daily_menu(db, 1, text="<b>תפריט</b>", meals=[meal_a])
    menu_id = str(saved_state["menu_id"])

    # Simulate a meal event marking the menu stale (same menu_id, nothing
    # regenerated it yet -- exactly the reachable-but-previously-unchecked
    # window).
    await mark_daily_menu_stale(db, 1, reason="meal_created")

    query = FakeQuery()
    handled = await callback_menu_bot.handle_menu_callback(query, 1, f"dailymenu:save:{menu_id}:breakfast-0")
    assert handled is True

    consumed = await _consumed_meal_names(db)
    assert consumed == []  # nothing saved from the stale menu
    assert query.messages
    assert "התעדכן" in query.messages[-1]


@pytest.mark.asyncio
async def test_dailymenu_save_still_works_when_menu_not_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sanity check: the new stale check must not block the normal path."""
    db = await _ready_db(tmp_path)
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(callback_menu_bot, "DB", db)

    meal_a = MenuMealRecord(
        meal_id="breakfast-0", slot="breakfast", time="08:00",
        role="ארוחת בוקר", calories=555, protein=44, note="שקשוקה",
    )
    saved_state = await remember_active_daily_menu(db, 1, text="<b>תפריט</b>", meals=[meal_a])
    menu_id = str(saved_state["menu_id"])

    query = FakeQuery()
    handled = await callback_menu_bot.handle_menu_callback(query, 1, f"dailymenu:save:{menu_id}:breakfast-0")
    assert handled is True

    consumed = await _consumed_meal_names(db)
    assert "שקשוקה" in consumed
