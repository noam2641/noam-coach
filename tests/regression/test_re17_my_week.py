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


def test_next_best_action_suggests_the_canonical_my_week_callback() -> None:
    """Audit correction: coach_intelligence.next_best_action's "לחבר את
    השבוע" ("build_unified_plan") suggestion used to still construct the
    retired "planv2:unify" string instead of "planv2:my_week" — functionally
    masked because callback_plans.py accepts it as a legacy alias, but this
    home-keyboard suggestion (a different call site than onboarding.py,
    which test_smartplan_hub_has_single_weekly_button above already covered)
    was never actually checked and quietly contradicted "old callback fully
    replaced"."""
    src = (Path(__file__).resolve().parents[2] / "coach_intelligence.py").read_text(encoding="utf-8")
    unify_block = src[src.index('"build_unified_plan"'):src.index('calories_goal = float')]
    assert '"planv2:my_week",' in unify_block
    # The NextAction(...) callback argument itself must not be the retired
    # string (an explanatory code comment mentioning it as legacy context is
    # fine — only the constructed value matters).
    assert '"planv2:unify",' not in unify_block


@pytest.mark.asyncio
async def test_next_best_action_build_unified_plan_uses_my_week_callback(
    tmp_path: Path,
) -> None:
    """Behavioral counterpart to the source check above: drive
    next_best_action all the way to the "connect the week" suggestion with
    both plans active but no unified week yet, and confirm the returned
    action's callback is the canonical planv2:my_week, not the retired
    planv2:unify."""
    import coach_intelligence

    db = await _ready_db(tmp_path)
    # _ready_db (this file's shared fixture) already has an approved
    # nutrition + workout profile, an active goal, and both plans selected —
    # confirm that and that no unified week exists yet before asserting on
    # the suggested action.
    nutrition_plan = await planning.get_active_plan(db, 1, "nutrition")
    workout_plan = await planning.get_active_plan(db, 1, "workout")
    assert nutrition_plan is not None
    assert workout_plan is not None
    assert await planning.get_active_plan(db, 1, "unified") is None

    action = await coach_intelligence.next_best_action(db, 1)
    assert action.kind == "build_unified_plan"
    assert action.callback == "planv2:my_week"


def test_unified_week_is_current_helper() -> None:
    nutrition = {"id": 10}
    workout = {"id": 20}
    unified_ok = {"payload": {"nutrition_plan_id": 10, "workout_plan_id": 20}}
    unified_stale = {"payload": {"nutrition_plan_id": 9, "workout_plan_id": 20}}
    assert planning.unified_week_is_current(unified_ok, nutrition, workout) is True
    assert planning.unified_week_is_current(unified_stale, nutrition, workout) is False
    assert planning.unified_week_is_current(None, nutrition, workout) is False
    assert planning.unified_week_is_current(unified_ok, None, workout) is False
