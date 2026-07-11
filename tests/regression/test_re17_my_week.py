"""TASK-17 — one "My Week" action replaces Build + Show.

Covers:
  * get_or_build_unified_week builds when no week exists and reuses a valid one;
  * a nutrition-plan change (new active plan id) invalidates the week;
  * a workout-plan change invalidates the week;
  * logging a meal does NOT invalidate the week;
  * the smartplan hub exposes exactly one weekly-plan button (planv2:my_week)
    and no longer exposes planv2:unify / planv2:show:unified as buttons.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import planning
import user_model
from db import Database
from helpers import utc_now


async def _ready_db(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "my_week.db"))
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
        "weekly_availability": [{"weekday": d, "start": "18:47", "minutes": 50} for d in [0, 2, 4]],
    }
    for key, value in facts.items():
        await user_model.set_fact(db, 1, key, value, source=user_model.SOURCE_USER, confirmed=True)
    proposal = await planning.build_goal_proposal(db, 1)
    goal_id = await planning.persist_goal_proposal(db, 1, proposal)
    await planning.activate_goal(db, 1, goal_id)

    nutrition = await planning.generate_candidates(db, 1, "nutrition")
    await planning.activate_plan(db, 1, int(nutrition[0].id or 0))
    workout = await planning.generate_candidates(db, 1, "workout")
    balanced = next(c for c in workout if c.strategy == "balanced")
    await planning.activate_plan(db, 1, int(balanced.id or 0))
    return db


@pytest.mark.asyncio
async def test_my_week_builds_when_missing(tmp_path: Path) -> None:
    db = await _ready_db(tmp_path)
    candidate, rebuilt = await planning.get_or_build_unified_week(db, 1)
    assert rebuilt is True
    assert candidate.payload.get("days")


@pytest.mark.asyncio
async def test_my_week_reuses_valid_week_without_rebuild(tmp_path: Path) -> None:
    db = await _ready_db(tmp_path)
    first, rebuilt_first = await planning.get_or_build_unified_week(db, 1)
    assert rebuilt_first is True
    second, rebuilt_second = await planning.get_or_build_unified_week(db, 1)
    assert rebuilt_second is False
    assert second.id == first.id


@pytest.mark.asyncio
async def test_nutrition_change_invalidates_week(tmp_path: Path) -> None:
    db = await _ready_db(tmp_path)
    await planning.get_or_build_unified_week(db, 1)
    # Re-generate + re-activate nutrition → new active plan id.
    nutrition = await planning.generate_candidates(db, 1, "nutrition")
    await planning.activate_plan(db, 1, int(nutrition[0].id or 0))
    _candidate, rebuilt = await planning.get_or_build_unified_week(db, 1)
    assert rebuilt is True


@pytest.mark.asyncio
async def test_workout_change_invalidates_week(tmp_path: Path) -> None:
    db = await _ready_db(tmp_path)
    await planning.get_or_build_unified_week(db, 1)
    workout = await planning.generate_candidates(db, 1, "workout")
    other = next(c for c in workout if c.strategy != "balanced")
    await planning.activate_plan(db, 1, int(other.id or 0))
    _candidate, rebuilt = await planning.get_or_build_unified_week(db, 1)
    assert rebuilt is True


@pytest.mark.asyncio
async def test_meal_logging_does_not_invalidate_week(tmp_path: Path) -> None:
    db = await _ready_db(tmp_path)
    await planning.get_or_build_unified_week(db, 1)
    await db.execute(
        "INSERT INTO meals(user_id, name, calories, protein, carbs, fat, confidence, eaten_at, created_at) "
        "VALUES(1,'ארוחה',500,40,50,15,0.9,?,?)",
        (utc_now(), utc_now()),
    )
    _candidate, rebuilt = await planning.get_or_build_unified_week(db, 1)
    assert rebuilt is False


def test_smartplan_hub_has_single_weekly_button() -> None:
    src = (Path(__file__).resolve().parents[2] / "noam_coach/bot/onboarding.py").read_text(encoding="utf-8")
    assert 'button("🗓️ השבוע שלי", "planv2:my_week")' in src
    # The old two-button flow is gone.
    assert 'planv2:unify")' not in src
    assert 'planv2:show:unified")' not in src


def test_unified_week_is_current_helper() -> None:
    nutrition = {"id": 10}
    workout = {"id": 20}
    unified_ok = {"payload": {"nutrition_plan_id": 10, "workout_plan_id": 20}}
    unified_stale = {"payload": {"nutrition_plan_id": 9, "workout_plan_id": 20}}
    assert planning.unified_week_is_current(unified_ok, nutrition, workout) is True
    assert planning.unified_week_is_current(unified_stale, nutrition, workout) is False
    assert planning.unified_week_is_current(None, nutrition, workout) is False
    assert planning.unified_week_is_current(unified_ok, None, workout) is False
