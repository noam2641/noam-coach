from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import coach_bot
import user_model
from db import Database
from helpers import utc_now
from noam_coach.bot import callback_menu as callback_menu_bot
from noam_coach.services.availability import parse_hebrew_availability_answer
from noam_coach.services.next_meal import (
    generate_next_meal_recommendation,
    get_active_recommendation_options,
    validate_meal_option,
)


class FakeQuery:
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


async def _ready_db(tmp_path: Path, *, calories: int = 2100, protein: int = 160) -> Database:
    db = Database(str(tmp_path / "claude_re8.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1, 'Noam', NULL, ?)",
        (utc_now(),),
    )
    await db.execute(
        """
        INSERT INTO goal_versions(user_id, calories, protein, steps, phase, status, source, created_at)
        VALUES(1, ?, ?, 8000, 'fat_loss_muscle_retention', 'active', 'test', ?)
        """,
        (calories, protein, utc_now()),
    )
    await user_model.set_fact(
        db,
        1,
        "sleep_schedule",
        {"bedtime": "23:00"},
        source=user_model.SOURCE_USER,
        confirmed=True,
    )
    return db


async def _meal(db: Database, *, calories: int, protein: int) -> None:
    await db.execute(
        """
        INSERT INTO meals(user_id, name, calories, protein, carbs, fat, confidence, eaten_at, created_at)
        VALUES(1, 'logged', ?, ?, 0, 0, 1, ?, ?)
        """,
        (calories, protein, utc_now(), utc_now()),
    )


@pytest.mark.asyncio
async def test_low_remaining_protein_is_calculated_from_ingredients(tmp_path: Path) -> None:
    db = await _ready_db(tmp_path)
    await _meal(db, calories=1831, protein=118)

    rec = await generate_next_meal_recommendation(db, 1)

    assert rec.context.nutrition.calorie_balance == 269
    assert all(option.calories <= 269 for option in rec.options)
    assert all(option.protein < 40 for option in rec.options)
    for option in rec.options:
        assert option.ingredient_details
        assert option.calories == round(sum(item.calories for item in option.ingredient_details))
        assert option.protein == round(sum(item.protein_g for item in option.ingredient_details))
        assert not validate_meal_option(option, rec.budget, [])


@pytest.mark.asyncio
async def test_quantity_edit_recalculates_option_totals_through_callback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = await _ready_db(tmp_path)
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(callback_menu_bot, "DB", db)

    before = await generate_next_meal_recommendation(db, 1)
    first_before = before.options[0]

    query = FakeQuery()
    handled = await callback_menu_bot.handle_menu_callback(query, 1, "nextmeal:qty:1:0.8")

    active_options = await get_active_recommendation_options(db, 1)
    first_after = active_options[0]
    assert handled is True
    assert first_after.calories < first_before.calories
    assert first_after.protein < first_before.protein
    assert first_after.calories == round(sum(item.calories for item in first_after.ingredient_details))
    assert first_after.protein == round(sum(item.protein_g for item in first_after.ingredient_details))
    assert "חישבתי מחדש" in query.messages[-1]


def test_availability_friday_one_hour_keeps_per_day_duration() -> None:
    parsed = parse_hebrew_availability_answer("ראשון 19:00, שני 19, רביעי 18:30 45 דקות, שישי 10:00 שעה")

    by_day = {slot["weekday"]: slot for slot in parsed.weekly_availability}
    assert by_day[6]["start"] == "19:00"
    assert by_day[0]["start"] == "19:00"
    assert by_day[2]["start"] == "18:30"
    assert by_day[2]["minutes"] == 45
    assert by_day[4]["start"] == "10:00"
    assert by_day[4]["minutes"] == 60
