"""Tests for recommendations.py — food flags, motivation, fallback menus."""

from __future__ import annotations

import pytest

import recommendations


def test_compute_food_flags_calorie_dense() -> None:
    meals = [{"name": "שוקולד", "grams": 100, "calories": 550, "protein": 5}]
    flags = recommendations.compute_food_flags(meals)
    reasons = [f["reason"] for f in flags]
    assert any("צפיפות" in r for r in reasons)


def test_compute_food_flags_low_protein() -> None:
    meals = [{"name": "לחם לבן", "grams": 200, "calories": 400, "protein": 8}]
    flags = recommendations.compute_food_flags(meals)
    reasons = [f["reason"] for f in flags]
    assert any("חלבון" in r for r in reasons)


def test_compute_food_flags_healthy_food_no_flags() -> None:
    meals = [{"name": "חזה עוף", "grams": 200, "calories": 330, "protein": 62}]
    flags = recommendations.compute_food_flags(meals)
    assert len(flags) == 0


def test_compute_food_flags_ignores_small_portions() -> None:
    # Calories < 150: low protein check shouldn't trigger
    meals = [{"name": "סוכריה", "grams": 20, "calories": 80, "protein": 0}]
    flags = recommendations.compute_food_flags(meals)
    # Only density flag possible (80/20*100=400 >= 350)
    for f in flags:
        assert "חלבון" not in f["reason"]


def test_compute_food_flags_missing_grams() -> None:
    meals = [{"name": "משהו", "grams": 0, "calories": 500, "protein": 10}]
    flags = recommendations.compute_food_flags(meals)
    # No density flag (grams=0), but low protein flag should appear
    reasons = [f["reason"] for f in flags]
    assert any("חלבון" in r for r in reasons)


def test_compute_food_flags_empty() -> None:
    assert recommendations.compute_food_flags([]) == []


@pytest.mark.asyncio
async def test_motivation_message_fallback() -> None:
    msg = await recommendations.motivation_message(None, "gpt-4")
    assert msg in recommendations.MOTIVATION_SEEDS


@pytest.mark.asyncio
async def test_morning_menu_fallback() -> None:
    profile = {"eating": {"first_meal_time": "08:00"}}
    goal = {"calories": 2000, "protein": 150}
    menu = await recommendations.morning_menu(None, "gpt-4", profile, goal, False)
    assert menu.headline == "תפריט הבוקר"
    assert len(menu.meals) == 3
    assert "(ללא AI" in menu.closing


@pytest.mark.asyncio
async def test_morning_menu_fallback_ritalin() -> None:
    profile = {"eating": {"first_meal_time": "08:00"}}
    goal = {"calories": 2000, "protein": 150}
    menu = await recommendations.morning_menu(
        None, "gpt-4", profile, goal, False, daily_flags={"ritalin": True}
    )
    assert len(menu.meals) == 3
    assert "ריטלין" in menu.closing


@pytest.mark.asyncio
async def test_morning_menu_fallback_fasting() -> None:
    profile = {"eating": {"first_meal_time": "08:00"}}
    goal = {"calories": 2000, "protein": 150}
    menu = await recommendations.morning_menu(
        None, "gpt-4", profile, goal, False, daily_flags={"fasting": True}
    )
    assert len(menu.meals) == 0
    assert "צום" in menu.closing


@pytest.mark.asyncio
async def test_morning_menu_fallback_workout() -> None:
    profile = {"eating": {"first_meal_time": "08:00"}}
    goal = {"calories": 2000, "protein": 150}
    menu = await recommendations.morning_menu(None, "gpt-4", profile, goal, True)
    assert "אימון" in menu.training_advice


@pytest.mark.asyncio
async def test_intraday_next_meals_fallback() -> None:
    result = await recommendations.intraday_next_meals(
        None, "gpt-4", {}, 800.0, 60.0, 6.0, False
    )
    assert result.headline
    assert len(result.suggestions) == 2  # splits into 2 meals for long day


@pytest.mark.asyncio
async def test_intraday_fallback_low_calories_warning() -> None:
    result = await recommendations.intraday_next_meals(
        None, "gpt-4", {}, 200.0, 30.0, 8.0, False
    )
    assert result.warning  # should warn about low calories + many hours


@pytest.mark.asyncio
async def test_evening_summary_fallback_on_target() -> None:
    meals = [{"name": "ארוחה", "grams": 300, "calories": 600, "protein": 40}]
    result = await recommendations.evening_summary(
        None, "gpt-4", {}, {"calories": 2000, "protein": 150}, 1950.0, 155.0, meals
    )
    assert result.headline == "סיכום היום"
    assert any("ביעד" in s for s in result.strengths)


@pytest.mark.asyncio
async def test_evening_summary_fallback_over_target() -> None:
    meals = [{"name": "ארוחה", "grams": 300, "calories": 600, "protein": 40}]
    result = await recommendations.evening_summary(
        None, "gpt-4", {}, {"calories": 2000, "protein": 150}, 2500.0, 100.0, meals
    )
    assert any("חריגה" in s for s in result.improvements)


def test_pydantic_models_validate() -> None:
    meal = recommendations.MenuMeal(
        name="test", time_hint="morning", calories=500, protein=30
    )
    assert meal.name == "test"

    menu = recommendations.MorningMenu(headline="test", meals=[meal])
    assert len(menu.meals) == 1

    summary = recommendations.EveningSummary(
        headline="test", strengths=["a"], improvements=["b"]
    )
    assert summary.headline == "test"
