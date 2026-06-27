from __future__ import annotations

from pathlib import Path

import pytest

import coach_bot
from db import Database
from helpers import utc_now
from noam_coach.services.next_meal import (
    format_next_meal_recommendation,
    generate_next_meal_recommendation,
)


@pytest.fixture
async def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Database:
    database = Database(str(tmp_path / "menu_product_behavior.db"))
    await database.init()
    await database.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1, 'Test', NULL, ?)",
        (utc_now(),),
    )
    monkeypatch.setattr(coach_bot, "DB", database)
    monkeypatch.setattr(coach_bot, "OPENAI_CLIENT", None)
    return database


@pytest.mark.asyncio
async def test_morning_menu_without_active_goal_uses_clear_default_disclaimer(db: Database) -> None:
    text = await coach_bot.build_morning_menu_text(1)

    assert "תפריט הבוקר" in text
    assert "ברירת מחדל" in text


@pytest.mark.asyncio
async def test_next_meal_without_active_goal_labels_default_goal(db: Database) -> None:
    recommendation = await generate_next_meal_recommendation(db, 1)
    text = format_next_meal_recommendation(recommendation)

    assert recommendation.context.nutrition.goal_status == "default"
    assert "ברירת מחדל" in text
    assert recommendation.options
