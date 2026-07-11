from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import planning
import questions
import user_model
from db import Database
from helpers import utc_now
from noam_coach.services.food_environment import normalize_food_environment_context
from noam_coach.services.nutrition_context import build_nutrition_context


async def _make_user(db: Database) -> None:
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1, 'Test', NULL, ?)",
        (utc_now(),),
    )


async def _set(db: Database, key: str, value: Any) -> None:
    await user_model.set_fact(db, 1, key, value, source=user_model.SOURCE_USER, confirmed=True)


async def _ready_nutrition_db(tmp_path: Path, *, food_environment: Any | None = None) -> Database:
    db = Database(str(tmp_path / "food_environment.db"))
    await db.init()
    await _make_user(db)
    for key, value in {
        "weight_kg": 90,
        "height_cm": 174,
        "age": 32,
        "sex": "male",
        "primary_goal": "fat_loss_muscle_retention",
        "diet_restrictions": "none",
        "allergies": "none",
    }.items():
        await _set(db, key, value)
    if food_environment is not None:
        await _set(db, "food_environment_context", normalize_food_environment_context(food_environment))
    proposal = await planning.build_goal_proposal(db, 1)
    goal_id = await planning.persist_goal_proposal(db, 1, proposal)
    assert await planning.activate_goal(db, 1, goal_id)
    return db


def test_food_environment_parser_covers_canonical_fields() -> None:
    parsed = normalize_food_environment_context(
        "אני מבשל פעמיים בשבוע, מזמין אוכל 3 פעמים, צריך פתרונות מהירים בעבודה ויש ימים בלי אוכל מסודר"
    )

    assert parsed["source_schema"] == "food_environment_v1"
    assert parsed["cooking_sessions_per_week"] == 2
    assert parsed["cooking_level"] == "moderate"
    assert parsed["restaurant_frequency"] == "high"
    assert parsed["delivery_or_takeaway"] is True
    assert parsed["needs_quick_meals"] is True
    assert parsed["limited_food_access"] is True


@pytest.mark.asyncio
async def test_food_environment_is_one_missing_nutrition_item(tmp_path: Path) -> None:
    db = await _ready_nutrition_db(tmp_path)

    readiness = await user_model.compute_readiness(db, 1, "nutrition")
    question = questions.question_by_fact_key("food_environment_context")

    assert "food_environment_context" in readiness["missing"]
    assert readiness["missing"].count("food_environment_context") == 1
    assert question is not None
    assert question.id == "q_food_environment_context"


@pytest.mark.asyncio
async def test_recorded_answer_is_stored_as_canonical_nutrition_context(tmp_path: Path) -> None:
    db = await _ready_nutrition_db(tmp_path)
    question = questions.question_by_fact_key("food_environment_context")
    assert question is not None

    await questions.record_answer(db, 1, question, "מבשל פעם בשבוע, מזמין לפעמים וצריך משהו מהיר בעבודה")
    stored = await user_model.get_value(db, 1, "food_environment_context")
    nutrition_context = await build_nutrition_context(db, 1, "menu")

    assert isinstance(stored, dict)
    assert stored["source_schema"] == "food_environment_v1"
    assert stored["needs_quick_meals"] is True
    assert nutrition_context.food_environment_context == stored


@pytest.mark.asyncio
async def test_unconfirmed_cooking_capacity_does_not_drive_strategy_ranking(tmp_path: Path) -> None:
    db = await _ready_nutrition_db(tmp_path)
    await _set(db, "cooking_capacity", "none")

    with pytest.raises(planning.PlanningBlockedError) as exc:
        await planning.generate_candidates(db, 1, "nutrition")

    assert "food_environment_context" in exc.value.missing


@pytest.mark.asyncio
async def test_confirmed_food_environment_changes_strategy_fit_explanation(tmp_path: Path) -> None:
    db = await _ready_nutrition_db(
        tmp_path,
        food_environment="לא מבשל כמעט, מזמין אוכל 4 פעמים בשבוע והשעות שלי משתנות",
    )

    candidates = await planning.generate_candidates(db, 1, "nutrition")
    flexible = next(candidate for candidate in candidates if candidate.strategy == "flexible")
    structured = next(candidate for candidate in candidates if candidate.strategy == "structured")

    assert any("משלוחים" in item or "מסעדות" in item for item in flexible.rationale)
    assert any("שעות משתנות" in item for item in flexible.rationale)
    assert flexible.score > structured.score
