from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

import recommendations
from config import TZ
from db import Database
from helpers import utc_now
from noam_coach.services.learned_foods import (
    learned_foods_from_meals,
    learned_foods_prompt_block,
)
from noam_coach.services.next_meal import (
    MealBudget,
    MealIngredient,
    MealOption,
    NutritionTotals,
    WorkoutNutritionContext,
    WorkoutPhase,
    _rank_and_recommend,
)
from noam_coach.services.nutrition_context import build_nutrition_context


async def _db(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "learned_foods.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1, 'Noam', NULL, ?)",
        (utc_now(),),
    )
    return db


async def _meal_with_item(db: Database, item_name: str, *, calories: float, protein: float) -> None:
    now = datetime(2026, 7, 7, 12, 0, tzinfo=TZ).isoformat()
    meal_id = await db.execute(
        """
        INSERT INTO meals(
            user_id, name, calories, protein, carbs, fat, confidence,
            image_path, approval_id, eaten_at, created_at
        ) VALUES(1, ?, ?, ?, 0, 0, 0.9, NULL, NULL, ?, ?)
        """,
        (item_name, calories, protein, now, now),
    )
    await db.execute(
        """
        INSERT INTO meal_items(meal_id, name, grams, calories, protein, carbs, fat, confidence)
        VALUES(?, ?, 200, ?, ?, 0, 0, 0.9)
        """,
        (meal_id, item_name, calories, protein),
    )


@pytest.mark.asyncio
async def test_learns_frequent_foods_from_approved_meal_items(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    await _meal_with_item(db, "קוטג' 5%", calories=180, protein=25)
    await _meal_with_item(db, "קוטג' 5%", calories=200, protein=27)
    await _meal_with_item(db, "אוכל חד פעמי", calories=500, protein=10)

    learned = await learned_foods_from_meals(db, 1, min_count=2)

    assert [food.display_name for food in learned] == ["קוטג' 5%"]
    assert learned[0].count == 2
    assert learned[0].avg_calories == 190
    assert "קוטג'" in await learned_foods_prompt_block(db, 1, min_count=1)


@pytest.mark.asyncio
async def test_nutrition_context_exposes_learned_foods_to_menu_ai(tmp_path: Path) -> None:
    # TASK-4: morning_menu personalization requires min_count>=2 (the
    # learned_foods module's own documented default) so a single one-off meal
    # never masquerades as reliable personalization evidence. Two occurrences
    # is real evidence and must be exposed.
    db = await _db(tmp_path)
    await _meal_with_item(db, "טורטיית חלבון", calories=260, protein=22)
    await _meal_with_item(db, "טורטיית חלבון", calories=260, protein=22)

    context = await build_nutrition_context(db, 1, "morning_menu", local_now=datetime(2026, 7, 7, 9, 0, tzinfo=TZ))

    assert context.learned_foods[0]["name"] == "טורטיית חלבון"
    assert context.to_ai_payload()["learned_foods"][0]["source"] == "approved_meal_history"


@pytest.mark.asyncio
async def test_nutrition_context_does_not_expose_one_off_food_to_menu_ai(tmp_path: Path) -> None:
    """TASK-4/required-test-3: a single occurrence is not strong enough
    evidence to influence daily-menu personalization."""
    db = await _db(tmp_path)
    await _meal_with_item(db, "טורטיית חלבון", calories=260, protein=22)

    context = await build_nutrition_context(db, 1, "morning_menu", local_now=datetime(2026, 7, 7, 9, 0, tzinfo=TZ))

    assert context.learned_foods == []


def test_next_meal_ranking_prefers_learned_food_when_other_fit_is_similar() -> None:
    nutrition = NutritionTotals(
        target_calories=2200,
        target_protein=160,
        consumed_calories=900,
        consumed_protein=80,
        calorie_balance=1300,
        protein_balance=80,
        calorie_overage=0,
        protein_overage=0,
        goal_status="active",
        goal_source="user",
    )
    context = WorkoutNutritionContext(
        user_id=1,
        local_now=datetime(2026, 7, 7, 14, 0, tzinfo=TZ).isoformat(),
        local_day="2026-07-07",
        nutrition=nutrition,
        workout_phase=WorkoutPhase.REST_DAY,
        workout_source="none",
        workout_label="יום ללא אימון",
    )
    budget = MealBudget(400, 650, 30, 60, "balanced", "test")
    learned = MealOption(
        title="קערת טורטיית חלבון",
        ingredients=["טורטיית חלבון 1 יחידה", "ירקות 100 גרם"],
        calories=500,
        protein=40,
        rationale="fits",
        ingredient_details=[
            MealIngredient("tortilla", "טורטיית חלבון", 1, "יחידה", 260, 22),
            MealIngredient("veg", "ירקות", 100, "גרם", 240, 18),
        ],
    )
    generic = MealOption(
        title="קערת עוף",
        ingredients=["עוף 150 גרם", "ירקות 100 גרם"],
        calories=500,
        protein=40,
        rationale="fits",
        ingredient_details=[
            MealIngredient("chicken", "עוף", 150, "גרם", 300, 30),
            MealIngredient("veg", "ירקות", 100, "גרם", 200, 10),
        ],
    )

    ranked = _rank_and_recommend([generic, learned], budget, context, set(), {"טורטיית חלבון"})

    assert ranked[0].title == "קערת טורטיית חלבון"
    assert "פריט שאתה אוכל" in ranked[0].recommended_reason


@pytest.mark.asyncio
async def test_morning_menu_fallback_prefers_learned_food_names() -> None:
    # Directly exercise the deterministic fallback without an AI client.
    result = await recommendations.morning_menu(
        None,
        "unused",
        {"eating": {"first_meal_time": "08:00"}},
        {"calories": 2100, "protein": 160, "phase": "fat_loss"},
        False,
        {},
        {"learned_foods": [{"name": "קוטג' 5%"}]},
    )

    assert "קוטג' 5%" in result.meals[0].name
    assert "שילבתי פריטים" in result.closing
