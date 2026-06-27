"""Acceptance coverage for REC-NEXT-MEAL-05.

The next-meal recommendation must be deterministic, workout-aware, and shared
between Telegram/proactive jobs and the Mini App API. Tests use a file-backed
SQLite database with synthetic data only.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import mini_api
import user_model
from config import TZ
from db import Database
from helpers import utc_now
from noam_coach.services.next_meal import (
    WorkoutPhase,
    format_next_meal_explanation,
    format_next_meal_recommendation,
    generate_next_meal_recommendation,
    record_next_meal_served,
    save_next_meal_option_feedback,
    save_next_meal_workout_status,
)


@pytest.fixture
async def db(tmp_path: Path) -> Database:
    database = Database(str(tmp_path / "rec_next_meal_05.db"))
    await database.init()
    return database


async def _add_user(db: Database, user_id: int = 1) -> None:
    await db.execute(
        "INSERT OR IGNORE INTO users(id, first_name, username, updated_at) "
        "VALUES(?, 'Test', NULL, ?)",
        (user_id, utc_now()),
    )


async def _set_fact(db: Database, user_id: int, key: str, value) -> None:
    await user_model.set_fact(
        db,
        user_id,
        key,
        value,
        source=user_model.SOURCE_USER,
        confirmed=True,
    )


async def _active_goal(db: Database, user_id: int = 1, *, calories: int = 2000, protein: int = 150) -> None:
    await db.execute(
        """
        INSERT INTO goal_versions(user_id, calories, protein, steps, phase, status, source, created_at)
        VALUES(?, ?, ?, 8000, 'fat_loss_muscle_retention', 'active', 'test', ?)
        """,
        (user_id, calories, protein, utc_now()),
    )


async def _meal(
    db: Database,
    user_id: int,
    now: datetime,
    *,
    calories: int,
    protein: int,
    minutes_ago: int = 180,
    name: str = "test meal",
) -> None:
    eaten_at = (now - timedelta(minutes=minutes_ago)).astimezone(TZ).isoformat()
    await db.execute(
        """
        INSERT INTO meals(user_id, name, calories, protein, carbs, fat, confidence, eaten_at, created_at)
        VALUES(?, ?, ?, ?, 0, 0, 1, ?, ?)
        """,
        (user_id, name, calories, protein, eaten_at, utc_now()),
    )


async def _workout_plan(db: Database, now: datetime, *, time_text: str, minutes: int = 60) -> None:
    weekday = (now.weekday() + 1) % 7
    payload = {
        "sessions": [
            {
                "weekday": weekday,
                "weekday_name": "today",
                "time": time_text,
                "minutes": minutes,
                "code": "A",
                "name": "Workout A",
                "exercises": [{"id": "squat"}],
            }
        ]
    }
    plan_id = await db.execute(
        """
        INSERT INTO plan_versions(
            user_id, plan_type, title, strategy, fit_score, status, payload,
            rationale, tradeoffs, assumptions, based_on, created_at, activated_at
        ) VALUES(1, 'workout', 'Test Workout', 'balanced', 1, 'active', ?, '[]', '[]', '[]', '{}', ?, ?)
        """,
        (json.dumps(payload), utc_now(), utc_now()),
    )
    await db.execute(
        "INSERT INTO active_plans(user_id, plan_type, plan_id, updated_at) VALUES(1, 'workout', ?, ?)",
        (plan_id, utc_now()),
    )


async def _ready_user(db: Database, now: datetime) -> None:
    await _add_user(db)
    await _active_goal(db)
    await _set_fact(db, 1, "sleep_schedule", {"bedtime": "23:00"})
    await _meal(db, 1, now, calories=700, protein=55)


async def _daily_flags(db: Database, now: datetime, flags: dict) -> None:
    await db.execute(
        """
        INSERT INTO daily_flags(user_id, day, flags, created_at)
        VALUES(1, ?, ?, ?)
        ON CONFLICT(user_id, day) DO UPDATE SET flags=excluded.flags
        """,
        (now.date().isoformat(), json.dumps(flags, ensure_ascii=False), utc_now()),
    )


@pytest.mark.asyncio
async def test_rest_day_balances_are_signed_and_reasonable(db: Database) -> None:
    now = datetime.now(TZ).replace(hour=14, minute=0, second=0, microsecond=0)
    await _ready_user(db, now)

    rec = await generate_next_meal_recommendation(db, 1, now=now)
    text = format_next_meal_recommendation(rec)

    assert rec.context.workout_phase == WorkoutPhase.REST_DAY
    assert rec.context.nutrition.calorie_balance == 1300
    assert rec.context.nutrition.protein_balance == 95
    # Answer-first message leads with the remaining balance (re7 P1-10).
    assert "1300" in text
    # Full signed-balance detail lives in the 'why it fits' view.
    assert "נותר להיום: 1300" in format_next_meal_explanation(rec)
    assert rec.budget.calories_max <= 750


@pytest.mark.asyncio
async def test_pre_workout_near_uses_light_digestible_budget(db: Database) -> None:
    now = datetime.now(TZ).replace(hour=16, minute=30, second=0, microsecond=0)
    await _ready_user(db, now)
    await _workout_plan(db, now, time_text="18:00")

    rec = await generate_next_meal_recommendation(db, 1, now=now)

    assert rec.context.workout_phase == WorkoutPhase.PRE_WORKOUT_NEAR
    assert rec.context.minutes_until_workout == 90
    assert rec.budget.meal_size == "pre_workout_meal"
    assert "לפני האימון" in rec.budget.rationale


@pytest.mark.asyncio
async def test_post_workout_completion_overrides_planned_time(db: Database) -> None:
    now = datetime.now(TZ).replace(hour=20, minute=0, second=0, microsecond=0)
    await _ready_user(db, now)
    await _workout_plan(db, now, time_text="18:00")
    await db.execute(
        """
        INSERT INTO sessions(user_id, code, name, plan, status, exercise_index, set_number, started_at, ended_at)
        VALUES(1, 'A', 'Workout A', '{}', 'completed', 0, 0, ?, ?)
        """,
        (
            (now - timedelta(hours=2)).astimezone(TZ).isoformat(),
            (now - timedelta(minutes=45)).astimezone(TZ).isoformat(),
        ),
    )

    rec = await generate_next_meal_recommendation(db, 1, now=now)

    assert rec.context.workout_phase == WorkoutPhase.POST_WORKOUT_IMMEDIATE
    assert rec.context.workout_source == "completed_session"
    assert rec.budget.protein_min >= 30


@pytest.mark.asyncio
async def test_planned_time_passed_needs_clarification_not_assumption(db: Database) -> None:
    now = datetime.now(TZ).replace(hour=21, minute=0, second=0, microsecond=0)
    await _ready_user(db, now)
    await _workout_plan(db, now, time_text="18:00", minutes=60)

    rec = await generate_next_meal_recommendation(db, 1, now=now)

    assert rec.context.workout_phase == WorkoutPhase.WORKOUT_PLANNED_TIME_PASSED
    assert rec.needs_workout_clarification is True
    assert "לא אניח שהאימון קרה בלי דיווח" in format_next_meal_recommendation(rec)


@pytest.mark.asyncio
async def test_workout_clarification_persists_for_today(db: Database) -> None:
    now = datetime.now(TZ).replace(hour=21, minute=0, second=0, microsecond=0)
    await _ready_user(db, now)
    await _workout_plan(db, now, time_text="18:00", minutes=60)

    await save_next_meal_workout_status(db, 1, "completed", now=now)
    rec = await generate_next_meal_recommendation(db, 1, now=now)

    assert rec.context.workout_phase == WorkoutPhase.POST_WORKOUT_IMMEDIATE
    assert rec.needs_workout_clarification is False


@pytest.mark.asyncio
async def test_calorie_overage_keeps_signed_overage_and_light_meal(db: Database) -> None:
    now = datetime.now(TZ).replace(hour=19, minute=0, second=0, microsecond=0)
    await _add_user(db)
    await _active_goal(db, calories=1800, protein=140)
    await _meal(db, 1, now, calories=2100, protein=120, minutes_ago=120)

    rec = await generate_next_meal_recommendation(db, 1, now=now)
    text = format_next_meal_recommendation(rec)

    assert rec.context.nutrition.calorie_balance == -300
    assert rec.context.nutrition.calorie_overage == 300
    assert rec.budget.calories_max <= 320
    # Signed overage shown in the detail/explanation view; headline notes it too.
    assert "חריגה מהיעד: 300" in format_next_meal_explanation(rec)
    assert "חריגה" in text


@pytest.mark.asyncio
async def test_options_respect_dietary_restrictions(db: Database) -> None:
    now = datetime.now(TZ).replace(hour=17, minute=0, second=0, microsecond=0)
    await _ready_user(db, now)
    await _set_fact(db, 1, "diet_restrictions", "gluten, dairy")
    await _set_fact(db, 1, "allergies", "fish")

    rec = await generate_next_meal_recommendation(db, 1, now=now)
    all_ingredients = " ".join(
        ingredient.lower()
        for option in rec.options
        for ingredient in option.ingredients
    )

    assert "טורטייה" not in all_ingredients
    assert "חיטה" not in all_ingredients
    assert "יוגורט" not in all_ingredients
    assert "טונה" not in all_ingredients


@pytest.mark.asyncio
async def test_options_respect_disliked_foods(db: Database) -> None:
    now = datetime.now(TZ).replace(hour=17, minute=0, second=0, microsecond=0)
    await _ready_user(db, now)
    await _set_fact(db, 1, "disliked_foods", "טורטייה")

    rec = await generate_next_meal_recommendation(db, 1, now=now)
    all_ingredients = " ".join(
        ingredient.lower()
        for option in rec.options
        for ingredient in option.ingredients
    )

    assert "טורט" not in all_ingredients


@pytest.mark.asyncio
async def test_next_meal_feedback_is_temporary_rejection_and_regenerates(db: Database) -> None:
    """re7 P1-7/8: 'לא מתאים לי' temporarily rejects the option (no permanent
    dislike) and returns a genuinely different alternative."""
    now = datetime.now(TZ).replace(hour=17, minute=0, second=0, microsecond=0)
    await _ready_user(db, now)
    initial = await generate_next_meal_recommendation(db, 1, now=now)
    rejected_title = initial.options[1].title

    saved_item, refreshed = await save_next_meal_option_feedback(db, 1, 2, now=now)
    refreshed_titles = [option.title for option in refreshed.options]

    assert saved_item == rejected_title
    # NOT stored as a permanent dislike.
    assert await user_model.get_value(db, 1, "disliked_foods") in (None, "", "none", [])
    # The rejected option is replaced by a different one.
    assert rejected_title not in refreshed_titles


@pytest.mark.asyncio
async def test_next_meal_history_prioritizes_fresh_options(db: Database) -> None:
    now = datetime.now(TZ).replace(hour=17, minute=0, second=0, microsecond=0)
    await _ready_user(db, now)
    first = await generate_next_meal_recommendation(db, 1, now=now)
    first_titles = {option.title for option in first.options}

    await record_next_meal_served(db, 1, first)
    second = await generate_next_meal_recommendation(db, 1, now=now)
    second_titles = [option.title for option in second.options]

    assert second_titles[0] not in first_titles
    assert any(title not in first_titles for title in second_titles)


@pytest.mark.asyncio
async def test_fasting_flag_changes_next_meal_and_not_fasting_disables_it(db: Database) -> None:
    now = datetime.now(TZ).replace(hour=19, minute=0, second=0, microsecond=0)
    await _ready_user(db, now)

    await _daily_flags(db, now, {"fasting": True})
    rec = await generate_next_meal_recommendation(db, 1, now=now)

    assert rec.context.fasting is True
    assert rec.budget.meal_size == "fast_break"
    assert any("צום" in notice for notice in rec.notices)

    await _daily_flags(db, now, {"fasting": False})
    rec = await generate_next_meal_recommendation(db, 1, now=now)

    assert rec.context.fasting is False
    assert rec.budget.meal_size != "fast_break"
    assert not any("צום" in notice for notice in rec.notices)


@pytest.mark.asyncio
async def test_mini_api_can_persist_workout_clarification(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(TZ).replace(hour=21, minute=0, second=0, microsecond=0)
    await _ready_user(db, now)
    await _workout_plan(db, now, time_text="18:00", minutes=60)
    monkeypatch.setattr(mini_api, "DB", db)

    async def fixed_recommendation(db_arg: Database, user_id: int):
        return await generate_next_meal_recommendation(db_arg, user_id, now=now)

    monkeypatch.setattr(mini_api, "generate_next_meal_recommendation", fixed_recommendation)

    response = await mini_api.mini_next_meal(user_id=1)
    body = json.loads(response.body)
    assert body["recommendation"]["needs_workout_clarification"] is True
    assert body["actions"]

    response = await mini_api.mini_next_meal_workout_status({"status": "completed"}, user_id=1)
    body = json.loads(response.body)

    assert body["recommendation"]["needs_workout_clarification"] is False
    assert body["recommendation"]["context"]["workout_phase"] == WorkoutPhase.POST_WORKOUT_IMMEDIATE.value


@pytest.mark.asyncio
async def test_mini_api_returns_same_rendered_recommendation(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(TZ).replace(hour=15, minute=0, second=0, microsecond=0)
    await _ready_user(db, now)
    monkeypatch.setattr(mini_api, "DB", db)

    response = await mini_api.mini_next_meal(user_id=1)
    body = json.loads(response.body)

    assert "recommendation" in body
    assert "text" in body
    # Answer-first rendering: leads with options, not the old header.
    assert "אפשרות 1" in body["text"]
    assert body["recommendation"]["context"]["nutrition"]["consumed_calories"] == 700
