"""RE10-12 / D12 regression tests — chronological ordering in the unified weekly plan.

Covers:
  * build_unified_week produces a merged, time-sorted "items" list per day.
  * A workout scheduled between meals appears between them, not always last.
  * Backward-compatible "meals"/"workouts" lists are still present.
  * render_unified_plan prints items in chronological order.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import planning
import user_model
from db import Database
from helpers import utc_now
from noam_coach.bot import onboarding as onboarding_bot


async def _ready_db_with_nutrition_and_workout(
    tmp_path: Path,
    *,
    weekly_availability: list[dict[str, Any]] | None = None,
) -> Database:
    db = Database(str(tmp_path / "unified.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'Test',NULL,?)",
        (utc_now(),),
    )
    facts = {
        "weight_kg": 90, "height_cm": 174, "age": 32, "sex": "male",
        "primary_goal": "fat_loss_muscle_retention",
        "diet_restrictions": "none", "allergies": "none",
        "food_environment_context": {
            "source_schema": "food_environment_v1",
            "cooking_level": "moderate",
            "restaurant_frequency": "low",
            "delivery_or_takeaway": False,
            "needs_quick_meals": False,
            "schedule_variability": False,
            "limited_food_access": False,
        },
        "training_days_per_week": 3, "active_pain": "none", "medical_avoidance": "none",
        "session_minutes": 50, "training_location": "gym", "equipment": "full_gym",
        "strength_experience": "intermediate",
        "weekly_availability": weekly_availability
        or [{"weekday": d, "start": "18:47", "minutes": 50} for d in [0, 2, 4]],
    }
    for key, value in facts.items():
        await user_model.set_fact(db, 1, key, value, source=user_model.SOURCE_USER, confirmed=True)
    proposal = await planning.build_goal_proposal(db, 1)
    goal_id = await planning.persist_goal_proposal(db, 1, proposal)
    await planning.activate_goal(db, 1, goal_id)

    nutrition_candidates = await planning.generate_candidates(db, 1, "nutrition")
    await planning.activate_plan(db, 1, int(nutrition_candidates[0].id or 0))
    workout_candidates = await planning.generate_candidates(db, 1, "workout")
    balanced = next(c for c in workout_candidates if c.strategy == "balanced")
    await planning.activate_plan(db, 1, int(balanced.id or 0))
    return db


@pytest.mark.asyncio
async def test_unified_week_items_are_chronologically_sorted(tmp_path: Path) -> None:
    db = await _ready_db_with_nutrition_and_workout(tmp_path)
    unified = await planning.build_unified_week(db, 1)

    workout_day = next(
        d for d in unified.payload["days"] if d.get("workouts")
    )
    items = workout_day["items"]
    times = [item["time"] for item in items if item.get("time")]
    assert times == sorted(times)


@pytest.mark.asyncio
async def test_workout_between_meals_lands_in_the_middle(tmp_path: Path) -> None:
    """A workout at 18:47 with meals at 08:00/12:30/16:30/20:30 must appear
    between 16:30 and 20:30, not after every meal regardless of its own time."""
    db = await _ready_db_with_nutrition_and_workout(tmp_path)
    unified = await planning.build_unified_week(db, 1)

    workout_day = next(d for d in unified.payload["days"] if d.get("workouts"))
    items = workout_day["items"]
    workout_index = next(i for i, item in enumerate(items) if item["type"] == "workout")

    # Every meal item strictly before the workout in the list must have an
    # earlier (or equal) time string, and every one after must have a later one.
    for i, item in enumerate(items):
        if item.get("time") and items[workout_index].get("time"):
            if i < workout_index:
                assert item["time"] <= items[workout_index]["time"]
            elif i > workout_index:
                assert item["time"] >= items[workout_index]["time"]


@pytest.mark.asyncio
async def test_meals_and_workouts_keys_still_present_for_backward_compat(tmp_path: Path) -> None:
    db = await _ready_db_with_nutrition_and_workout(tmp_path)
    unified = await planning.build_unified_week(db, 1)
    for day in unified.payload["days"]:
        assert "meals" in day
        assert "workouts" in day
        assert "items" in day
        assert len(day["items"]) == len(day["meals"]) + len(day["workouts"])


@pytest.mark.asyncio
async def test_unified_week_preserves_day_specific_workout_times(tmp_path: Path) -> None:
    db = await _ready_db_with_nutrition_and_workout(
        tmp_path,
        weekly_availability=[
            {"weekday": 0, "start": "19:00", "minutes": 50},
            {"weekday": 2, "start": "18:30", "minutes": 50},
            {"weekday": 4, "start": "09:00", "minutes": 50},
        ],
    )
    unified = await planning.build_unified_week(db, 1)

    workouts_by_day = {
        day["weekday"]: day["workouts"][0]["time"]
        for day in unified.payload["days"]
        if day.get("workouts")
    }

    assert workouts_by_day == {0: "19:00", 2: "18:30", 4: "09:00"}


@pytest.mark.asyncio
async def test_unified_week_uses_morning_nutrition_timing_for_friday_morning(tmp_path: Path) -> None:
    db = await _ready_db_with_nutrition_and_workout(
        tmp_path,
        weekly_availability=[
            {"weekday": 0, "start": "19:00", "minutes": 50},
            {"weekday": 2, "start": "18:30", "minutes": 50},
            {"weekday": 4, "start": "09:00", "minutes": 50},
        ],
    )
    unified = await planning.build_unified_week(db, 1)

    friday = next(day for day in unified.payload["days"] if day["weekday"] == 4)
    friday_workout_meals = [meal for meal in friday["meals"] if meal.get("workout_time") == "09:00"]
    assert {meal["time"] for meal in friday_workout_meals} == {"07:30", "10:15"}
    assert any("בוקר" in meal["name"] for meal in friday_workout_meals)
    assert not any(meal["time"].startswith("17:") for meal in friday_workout_meals)

    monday = next(day for day in unified.payload["days"] if day["weekday"] == 0)
    monday_workout_meals = [meal for meal in monday["meals"] if meal.get("workout_time") == "19:00"]
    assert {meal["time"] for meal in monday_workout_meals} == {"17:30", "20:15"}
    assert not any("בוקר" in meal["name"] for meal in monday_workout_meals)


@pytest.mark.asyncio
async def test_render_unified_plan_prints_chronological_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import coach_bot

    db = await _ready_db_with_nutrition_and_workout(tmp_path)
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(onboarding_bot, "DB", db)
    await planning.build_unified_week(db, 1)

    class FakeTarget:
        def __init__(self) -> None:
            self.messages: list[str] = []

        async def edit_message_text(self, text: str, reply_markup: Any = None, parse_mode: str | None = None) -> None:
            del reply_markup, parse_mode
            self.messages.append(text)

    target = FakeTarget()
    await onboarding_bot.render_unified_plan(target, 1)
    text = target.messages[-1]

    # Find the workout day's block and verify the 🏋️ line is not always last.
    lines = text.split("\n")
    workout_line_index = next(i for i, line in enumerate(lines) if "🏋️" in line)
    # There must be at least one meal line (🍽️) with a LATER time than the
    # workout somewhere after it in that day's block (proves it's not last).
    day_end = next(
        (i for i in range(workout_line_index + 1, len(lines)) if lines[i] == ""),
        len(lines),
    )
    later_meal_lines = [line for line in lines[workout_line_index + 1:day_end] if "🍽️" in line]
    assert later_meal_lines, "expected at least one meal scheduled after the workout in the same day"
