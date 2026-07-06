from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import planning
import user_model
from db import Database
from helpers import utc_now
from noam_coach.bot.assistant import _is_explicit_calorie_goal_change
from noam_coach.services.availability import resolve_availability
from noam_coach.services.next_meal import (
    classify_recommendation_correction,
    generate_next_meal_recommendation,
    save_chosen_meal,
)
from noam_coach.services.weekdays import WEEKDAY_SCHEMA_VERSION


async def _db(tmp_path: Path, *, calories: int = 2100, protein: int = 195) -> Database:
    db = Database(str(tmp_path / "re9.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1, 'Test', NULL, ?)",
        (utc_now(),),
    )
    await db.execute(
        """
        INSERT INTO goal_versions(user_id, calories, protein, steps, phase, status, source, created_at)
        VALUES(1, ?, ?, 8000, 'fat_loss_muscle_retention', 'active', 'user_approved', ?)
        """,
        (calories, protein, utc_now()),
    )
    return db


async def _meal(db: Database, *, calories: int, protein: int, hours_ago: int = 2) -> None:
    eaten_at = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
    await db.execute(
        """
        INSERT INTO meals(user_id, name, calories, protein, carbs, fat, confidence, eaten_at, created_at)
        VALUES(1, 'meal', ?, ?, 20, 10, 0.9, ?, ?)
        """,
        (calories, protein, eaten_at, utc_now()),
    )


@pytest.mark.asyncio
async def test_re9_user_days_conflict_with_health_history_user_wins_once(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    user_slots = [
        {"weekday": day, "weekday_schema": WEEKDAY_SCHEMA_VERSION, "start": "19:00", "minutes": 45}
        for day in [0, 2, 4, 6]
    ]
    await user_model.set_fact(db, 1, "weekly_availability", user_slots, source=user_model.SOURCE_USER, confirmed=True)
    await user_model.set_fact(db, 1, "training_days_per_week", 4, source=user_model.SOURCE_USER, confirmed=True)
    await user_model.set_fact(
        db,
        1,
        "workout_pattern",
        {"weekly_frequency": 3, "common_weekdays": [1, 3, 5], "weekday_schema": WEEKDAY_SCHEMA_VERSION},
        source=user_model.SOURCE_DERIVED,
        kind=user_model.KIND_ESTIMATE,
        confirmed=False,
    )

    availability = await resolve_availability(db, 1)

    assert availability.preferred_days == [0, 2, 4, 6]
    assert availability.max_days_per_week == 4
    assert availability.source == "user_corrected"
    assert availability.conflicts and availability.conflicts[0]["field"] == "preferred_days"


@pytest.mark.asyncio
async def test_re9_four_days_at_1900_preserved_in_all_three_workout_candidates(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    required = {
        "weight_kg": 90,
        "height_cm": 174,
        "age": 32,
        "sex": "male",
        "primary_goal": "fat_loss_muscle_retention",
        "diet_restrictions": "none",
        "allergies": "none",
        "active_pain": "none",
        "medical_avoidance": "none",
        "training_location": "gym",
        "equipment": "full gym",
        "strength_experience": "intermediate",
        "session_minutes": 50,
        "training_days_per_week": 4,
        "workout_window": "19:00",
        "weekly_availability": [
            {"weekday": day, "weekday_schema": WEEKDAY_SCHEMA_VERSION, "start": "19:00", "minutes": 50}
            for day in [0, 2, 4, 6]
        ],
    }
    for key, value in required.items():
        await user_model.set_fact(db, 1, key, value, source=user_model.SOURCE_USER, confirmed=True)

    candidates = await planning.generate_candidates(db, 1, "workout")

    assert len(candidates) == 3
    # RE10-3 (D13): the three candidates now legitimately differ in
    # frequency (consistency/balanced/performance), so only "balanced" keeps
    # exactly the 4 confirmed days at 19:00. Every candidate's sessions must
    # still only ever use confirmed days/times, never invented ones.
    balanced = next(c for c in candidates if c.strategy == "balanced")
    assert balanced.payload["frequency"] == 4
    assert [session["weekday"] for session in balanced.payload["sessions"]] == [0, 2, 4, 6]
    assert {session["time"] for session in balanced.payload["sessions"]} == {"19:00"}

    confirmed_days = {0, 2, 4, 6}
    for candidate in candidates:
        for session in candidate.payload["sessions"]:
            assert session["weekday"] in confirmed_days
            assert session["time"] == "19:00"


@pytest.mark.asyncio
async def test_re9_2100_195_protein_five_hours_to_sleep_has_feasible_natural_options(tmp_path: Path) -> None:
    db = await _db(tmp_path, calories=2100, protein=195)
    await _meal(db, calories=1500, protein=110)
    now = datetime.now(timezone.utc).astimezone().replace(hour=18, minute=0, second=0, microsecond=0)
    rec = await generate_next_meal_recommendation(db, 1, now=now)

    assert 1 <= len(rec.options) <= 2
    for option in rec.options:
        assert option.protein * 4 <= option.calories
        for ingredient in option.ingredient_details:
            assert float(ingredient.quantity).is_integer() or ingredient.unit not in {"גרם", "יחידה", "כפית"}
        assert option.calories == round(sum(item.calories for item in option.ingredient_details))
        assert option.protein == round(sum(item.protein_g for item in option.ingredient_details))


def test_re9_feedback_with_numbers_does_not_request_goal_change() -> None:
    assert not _is_explicit_calorie_goal_change("הטקסט עם 2100 קלוריות ו-195 חלבון היה רק משוב")
    assert _is_explicit_calorie_goal_change("שנה יעד ל-2100 קלוריות")


def test_re9_unrelated_text_during_meal_edit_is_not_swallowed() -> None:
    assert classify_recommendation_correction("מה עם תוכנית האימון שלי?") == {"kind": "none"}


@pytest.mark.asyncio
async def test_re9_render_retry_after_save_does_not_duplicate_meal(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    rec = await generate_next_meal_recommendation(db, 1)
    option = rec.options[0]

    assert await save_chosen_meal(db, 1, option) is True
    # Simulates a render failure after the DB write: retrying the save path must
    # be idempotent by fingerprint and not create another consumed meal.
    assert await save_chosen_meal(db, 1, option) is False
    row = await db.fetch_one("SELECT COUNT(*) AS c, COALESCE(SUM(calories), 0) AS cal FROM meals WHERE user_id=1")

    assert int(row["c"]) == 1
    assert int(row["cal"]) == option.calories
