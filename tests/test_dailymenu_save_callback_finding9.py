"""End-to-end regression for corrective-review Finding 9: the daily-menu
"confirm I ate" callback must save the EXACT daily-menu meal, and must never
fall through to whatever the currently-active next-meal recommendation is.

Before the fix, every daily-menu edit reply's confirm button was hardcoded to
"nextmeal:save:1", which regenerates/loads the CURRENT active next-meal
recommendation and saves option 1 from THAT -- completely unrelated to the
meal the user was just shown in the edited daily menu.
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
    db = Database(str(tmp_path / "dailymenu_save.db"))
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
async def test_dailymenu_save_persists_the_exact_daily_menu_meal_not_next_meal_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The core Finding 9 scenario: an active daily-menu meal A differs from
    whatever the active next-meal recommendation would generate as option 1
    (meal B). Confirming from the daily menu must save A, never B."""
    db = await _ready_db(tmp_path)
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(callback_menu_bot, "DB", db)

    meal_a = MenuMealRecord(
        meal_id="breakfast-0", slot="breakfast", time="08:00",
        role="ארוחת בוקר מהתפריט היומי", calories=555, protein=44,
        note="שקשוקה עם לחם מלא", ingredients=[{"name": "ביצים", "grams": 120, "calories": 190, "protein": 15}],
    )
    saved_state = await remember_active_daily_menu(
        db, 1, text="<b>תפריט</b>", meals=[meal_a],
    )
    menu_id = str(saved_state["menu_id"])

    query = FakeQuery()
    handled = await callback_menu_bot.handle_menu_callback(query, 1, f"dailymenu:save:{menu_id}:breakfast-0")
    assert handled is True

    consumed = await _consumed_meal_names(db)
    # Meal identity fix: the saved name is the actual composition (note),
    # not the behavioral role label -- "ארוחת בוקר מהתפריט היומי" would be
    # useless to learned_foods/repetition/routine analysis, which key off
    # the meal name.
    assert "שקשוקה עם לחם מלא" in consumed
    assert "ארוחת בוקר מהתפריט היומי" not in consumed
    # Never fell through to a next-meal recommendation title (which would be
    # some deterministic-fallback name like "ארוחת בוקר עתירת חלבון" etc.,
    # generated fresh by generate_next_meal_recommendation with entirely
    # different macros than meal_a's 555/44).
    assert len(consumed) == 1


@pytest.mark.asyncio
async def test_dailymenu_save_with_stale_menu_id_does_not_save_superseded_meal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A button rendered against an OLD menu_id (the menu was refreshed after
    the message was sent) must not silently save a superseded meal."""
    db = await _ready_db(tmp_path)
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(callback_menu_bot, "DB", db)

    meal = MenuMealRecord(meal_id="breakfast-0", slot="breakfast", time="08:00", role="ארוחת בוקר", calories=500, protein=40)
    await remember_active_daily_menu(db, 1, text="<b>תפריט</b>", meals=[meal])

    query = FakeQuery()
    handled = await callback_menu_bot.handle_menu_callback(query, 1, "dailymenu:save:menu-stale-id:breakfast-0")
    assert handled is True
    consumed = await _consumed_meal_names(db)
    assert consumed == []  # nothing saved


@pytest.mark.asyncio
async def test_dailymenu_save_is_idempotent_on_double_tap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = await _ready_db(tmp_path)
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(callback_menu_bot, "DB", db)

    meal = MenuMealRecord(meal_id="breakfast-0", slot="breakfast", time="08:00", role="ארוחת בוקר", calories=500, protein=40)
    saved_state = await remember_active_daily_menu(db, 1, text="<b>תפריט</b>", meals=[meal])
    menu_id = str(saved_state["menu_id"])

    query = FakeQuery()
    await callback_menu_bot.handle_menu_callback(query, 1, f"dailymenu:save:{menu_id}:breakfast-0")
    await callback_menu_bot.handle_menu_callback(query, 1, f"dailymenu:save:{menu_id}:breakfast-0")

    consumed = await _consumed_meal_names(db)
    assert len(consumed) == 1  # not duplicated
