from __future__ import annotations

import pytest

import coach_bot
import user_model
from db import Database
from helpers import utc_now
from models import FoodItem, MealAnalysis
from noam_coach.services.meal_validation import validate_meal_analysis


def _tuna_analysis() -> MealAnalysis:
    return MealAnalysis(
        meal_name="tuna wrap",
        confidence=0.95,
        items=[
            FoodItem(
                name="tuna",
                grams=120,
                calories=170,
                protein=32,
                carbs=0,
                fat=4,
                confidence=0.95,
            )
        ],
    )


async def _user(db: Database) -> None:
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1, 'Noam', NULL, ?)",
        (utc_now(),),
    )


def test_meal_validation_blocks_allergy_conflict() -> None:
    result = validate_meal_analysis(_tuna_analysis(), allergies="fish")

    assert result.blocked is True
    assert result.issues[0].code == "restricted_food_block"


@pytest.mark.asyncio
async def test_persist_meal_blocks_allergy_conflict(tmp_path) -> None:
    db = coach_bot.Database(str(tmp_path / "coach.db"))
    await db.init()
    await _user(db)
    await user_model.set_fact(
        db,
        1,
        "allergies",
        "fish",
        source=user_model.SOURCE_USER,
        confirmed=True,
    )

    original_db = coach_bot.DB
    coach_bot.DB = db
    try:
        approval_id = await coach_bot.create_approval(
            1,
            "meal",
            {"analysis": _tuna_analysis().model_dump(), "eaten_at": utc_now()},
        )
        with pytest.raises(ValueError, match="אלרגיה|רגישות|fish"):
            await coach_bot.persist_meal(1, approval_id)
        row = await db.fetch_one("SELECT COUNT(*) AS c FROM meals WHERE user_id=1")
        assert int(row["c"]) == 0
    finally:
        coach_bot.DB = original_db
