from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

import coach_bot
import user_model
from config import TZ
from db import Database
from helpers import utc_now
from noam_coach.bot import callback_menu as callback_menu_bot
from noam_coach.services import next_meal as next_meal_service
from noam_coach.services.availability import parse_hebrew_availability_answer
from noam_coach.services.next_meal import (
    generate_next_meal_recommendation,
    get_active_recommendation_options,
    remember_active_recommendation,
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

    # Recommendation generation genuinely depends on the time of day (budget
    # slot, candidate set, pre-sleep filtering against the 23:00 bedtime), so
    # the whole journey runs against ONE fixed instant instead of the wall
    # clock. The callback handler cannot receive `now`, so the service's own
    # deterministic-time parameter is injected at its module seam — the REAL
    # adjust_next_meal_quantity still runs.
    fixed_now = datetime(2026, 6, 28, 13, 0, tzinfo=TZ)
    real_adjust = next_meal_service.adjust_next_meal_quantity

    async def adjust_with_fixed_now(
        db_: Any, user_id: int, option_number: int, scale: float, *, now: Any = None,
    ) -> Any:
        del now
        return await real_adjust(db_, user_id, option_number, scale, now=fixed_now)

    monkeypatch.setattr(next_meal_service, "adjust_next_meal_quantity", adjust_with_fixed_now)

    # The buttons the user presses belong to a rendered, ACTIVE recommendation
    # (nextmeal:editqty renders them from the active state) — mirror that.
    before = await generate_next_meal_recommendation(db, 1, now=fixed_now)
    await remember_active_recommendation(db, 1, before, now=fixed_now)
    first_before = before.options[0]

    query = FakeQuery()
    handled = await callback_menu_bot.handle_menu_callback(query, 1, "nextmeal:qty:1:0.8")

    active_options = await get_active_recommendation_options(db, 1, now=fixed_now)
    assert handled is True
    # Identity: the quantity edit must adjust the SAME semantic option the
    # user was looking at — never swap the meal (list position is only
    # trusted because _replace_selected_option preserves positions; the
    # title check pins the semantic identity explicitly).
    first_after = active_options[0]
    assert first_after.title == first_before.title
    assert len(active_options) == len(before.options)
    # ➖20%: quantities scale down with unit-appropriate rounding
    # (_round_quantity_by_unit: gram items shrink ~20%; count units like
    # eggs round to whole pieces and may stay put — never grow).
    assert len(first_after.ingredient_details) == len(first_before.ingredient_details)
    for item_before, item_after in zip(
        first_before.ingredient_details, first_after.ingredient_details
    ):
        assert item_after.display_name == item_before.display_name
        assert item_after.quantity <= item_before.quantity
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
