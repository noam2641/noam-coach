from __future__ import annotations

from pathlib import Path

import pytest

import planning
import user_model
from db import Database
from helpers import utc_now


async def _ready_db(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "planning.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'Test',NULL,?)",
        (utc_now(),),
    )
    facts = {
        "weight_kg": 90,
        "height_cm": 174,
        "age": 32,
        "sex": "male",
        "primary_goal": "fat_loss_muscle_retention",
        "diet_restrictions": "none",
        "allergies": "none",
        "training_days_per_week": 3,
        "active_pain": "none",
        "medical_avoidance": "none",
        "session_minutes": 50,
        "training_location": "חדר כושר",
        "equipment": "חדר כושר מלא",
        "strength_experience": "intermediate",
        "weekly_availability": [
            {"weekday": 0, "start": "19:00", "minutes": 50},
            {"weekday": 2, "start": "19:00", "minutes": 50},
            {"weekday": 4, "start": "10:00", "minutes": 60},
            {"weekday": 5, "start": "10:00", "minutes": 60},
        ],
    }
    for key, value in facts.items():
        await user_model.set_fact(
            db, 1, key, value, source=user_model.SOURCE_USER, confirmed=True
        )
    proposal = await planning.build_goal_proposal(db, 1)
    goal_id = await planning.persist_goal_proposal(db, 1, proposal)
    assert await planning.activate_goal(db, 1, goal_id)
    return db


@pytest.mark.asyncio
async def test_three_nutrition_and_workout_candidates_are_generated(tmp_path: Path) -> None:
    db = await _ready_db(tmp_path)
    nutrition = await planning.generate_candidates(db, 1, "nutrition")
    workout = await planning.generate_candidates(db, 1, "workout")
    assert len(nutrition) == 3
    assert len(workout) == 3
    assert {item.strategy for item in nutrition} == {"structured", "flexible", "low_effort"}
    assert {item.strategy for item in workout} == {"consistency", "balanced", "performance"}
    assert all(session["time"] for session in workout[1].payload["sessions"])
    assert all(
        "warmup_sets" in exercise
        for session in workout[1].payload["sessions"]
        for exercise in session["exercises"]
    )


@pytest.mark.asyncio
async def test_plan_activation_is_versioned_and_single_active(tmp_path: Path) -> None:
    db = await _ready_db(tmp_path)
    candidates = await planning.generate_candidates(db, 1, "nutrition")
    first = await planning.activate_plan(db, 1, int(candidates[0].id or 0))
    second = await planning.activate_plan(db, 1, int(candidates[1].id or 0))
    assert first and second
    active = await planning.get_active_plan(db, 1, "nutrition")
    assert active and active["id"] == candidates[1].id
    old = await db.fetch_one("SELECT status FROM plan_versions WHERE id=?", (candidates[0].id,))
    assert old and old["status"] == "superseded"


@pytest.mark.asyncio
async def test_provisional_goal_cannot_be_activated(tmp_path: Path) -> None:
    """P0: a computed goal with missing mandatory data is not activatable."""
    db = Database(str(tmp_path / "goal_block.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'T',NULL,?)",
        (utc_now(),),
    )
    # Only weight is known — sex, age and primary_goal are missing.
    await user_model.set_fact(db, 1, "weight_kg", 90, source=user_model.SOURCE_USER, confirmed=True)
    proposal = await planning.build_goal_proposal(db, 1)
    assert proposal.provisional is True
    goal_id = await planning.persist_goal_proposal(db, 1, proposal)
    with pytest.raises(planning.GoalNotReady):
        await planning.activate_goal(db, 1, goal_id)
    missing = await planning.missing_goal_inputs(db, 1)
    assert "מין" in missing or "גיל" in missing


@pytest.mark.asyncio
async def test_manual_goal_activates_without_full_data(tmp_path: Path) -> None:
    """A manually-set goal is an explicit user value and is allowed."""
    db = Database(str(tmp_path / "manual_goal.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'T',NULL,?)",
        (utc_now(),),
    )
    goal_id = await db.execute(
        "INSERT INTO goal_versions(user_id, calories, protein, steps, phase, status, source, "
        "explanation, created_at) VALUES(1, 2000, 150, 8000, 'maintain', 'proposed', 'manual', '', ?)",
        (utc_now(),),
    )
    assert await planning.activate_goal(db, 1, int(goal_id)) is True


@pytest.mark.asyncio
async def test_gap_blocks_workout_readiness(tmp_path: Path) -> None:
    db = await _ready_db(tmp_path)
    await user_model.record_gap(db, 1, "equipment", why_matters="required")
    # record_gap does not overwrite a real fact; invalidate it to simulate a real gap.
    await db.execute("DELETE FROM user_facts WHERE user_id=1 AND key='equipment'")
    await user_model.record_gap(db, 1, "equipment", why_matters="required")
    with pytest.raises(planning.PlanningBlockedError):
        await planning.build_workout_candidates(db, 1)
