from __future__ import annotations

from pathlib import Path

import pytest

import data_quality
from db import Database
from helpers import utc_now
from models import FoodItem, MealAnalysis


def test_macro_mismatch_lowers_meal_quality() -> None:
    analysis = MealAnalysis(
        meal_name="בדיקה",
        confidence=0.95,
        items=[
            FoodItem(
                name="פריט",
                grams=100,
                calories=800,
                protein=10,
                carbs=10,
                fat=2,
                confidence=0.95,
            )
        ],
    )
    report = data_quality.assess_meal(analysis)
    assert report.score < 0.8
    assert any(issue.code == "macro_calorie_mismatch" for issue in report.issues)


@pytest.mark.asyncio
async def test_partial_day_is_not_usable_for_strong_nutrition_advice(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "quality.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (utc_now(),),
    )
    await db.execute(
        """
        INSERT INTO meals(user_id,name,calories,protein,carbs,fat,confidence,eaten_at,created_at)
        VALUES(1,'ארוחה',500,30,40,15,0.8,'2026-06-22T10:00:00+00:00',?)
        """,
        (utc_now(),),
    )
    report = await data_quality.assess_day(
        db,
        1,
        "2026-06-22T00:00:00+00:00",
        "2026-06-23T00:00:00+00:00",
        expected_meals=3,
    )
    assert report.usable is False
    assert any(issue.code == "partial_day" for issue in report.issues)
