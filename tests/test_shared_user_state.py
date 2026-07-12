"""REC-ARCH-01 — shared user-state layer, precedence policy, and the one
concrete cross-domain decision (pre-workout meal timing).

Covers the required regression scenarios from the architecture brief:
  1-4, 13-15: workout state precedence (actual > plan > routine; plan never
              read as actual; routine never overrides an explicit plan).
  5-6:        consumed vs. planned meal separation feeding workout context.
  7-10:       recent-meal -> workout-timing decision, including confidence /
              missing-data discipline.
  11-12:      one shared ``now``; nutrition reads the same resolved workout
              state instead of reconstructing a conflicting one.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from config import TZ
from db import Database
from helpers import utc_now
from noam_coach.services.meal_timing import (
    UserToleranceEvidence,
    WorkoutDemand,
    evaluate_pre_workout_meal_timing,
    recent_meal_state_from_consumed,
    workout_demand_from_session,
)
from noam_coach.services.next_meal import build_workout_nutrition_context
from noam_coach.services.nutrition_context import build_nutrition_context
from noam_coach.services.precedence import (
    PrecedenceRank,
    higher_precedence_source,
    workout_source_rank,
)
from noam_coach.services.user_state import (
    WorkoutPhase,
    build_shared_state,
    resolve_workout_state,
)
from noam_coach.services.weekdays import WEEKDAY_SCHEMA_VERSION, local_weekday
from noam_coach.services.workout_decision_context import project_workout_decision_context


@pytest.fixture
async def db(tmp_path: Path) -> Database:
    database = Database(str(tmp_path / "shared_user_state.db"))
    await database.init()
    return database


async def _user(db: Database, user_id: int = 1) -> None:
    await db.execute(
        "INSERT OR IGNORE INTO users(id, first_name, username, updated_at) VALUES(?, 'Test', NULL, ?)",
        (user_id, utc_now()),
    )


async def _goal(db: Database, user_id: int = 1) -> None:
    await db.execute(
        """
        INSERT INTO goal_versions(user_id, calories, protein, steps, phase, status, source, created_at)
        VALUES(?, 2200, 160, 8000, 'fat_loss_muscle_retention', 'active', 'test', ?)
        """,
        (user_id, utc_now()),
    )


async def _meal(
    db: Database,
    user_id: int,
    now: datetime,
    *,
    calories: int,
    protein: int = 20,
    minutes_ago: int = 30,
    name: str = "meal",
) -> None:
    eaten_at = (now - timedelta(minutes=minutes_ago)).astimezone(TZ).isoformat()
    await db.execute(
        """
        INSERT INTO meals(user_id, name, calories, protein, carbs, fat, confidence, eaten_at, created_at)
        VALUES(?, ?, ?, ?, 0, 0, 1, ?, ?)
        """,
        (user_id, name, calories, protein, eaten_at, utc_now()),
    )


async def _workout_plan(
    db: Database,
    user_id: int,
    now: datetime,
    *,
    time_text: str,
    minutes: int = 60,
    exercises: list[dict] | None = None,
) -> None:
    weekday = local_weekday(now)
    payload = {
        "frequency": 1,
        "sessions": [
            {
                "weekday": weekday,
                "weekday_schema": WEEKDAY_SCHEMA_VERSION,
                "weekday_name": "today",
                "time": time_text,
                "minutes": minutes,
                "code": "A",
                "name": "Workout A",
                "exercises": exercises or [{"id": "squat", "sets": 3, "muscle": "רגליים"}],
            }
        ],
    }
    plan_id = await db.execute(
        """
        INSERT INTO plan_versions(
            user_id, plan_type, title, strategy, fit_score, status, payload,
            rationale, tradeoffs, assumptions, based_on, created_at, activated_at
        ) VALUES(?, 'workout', 'Test Workout', 'balanced', 1, 'active', ?, '[]', '[]', '[]', '{}', ?, ?)
        """,
        (user_id, json.dumps(payload), utc_now(), utc_now()),
    )
    await db.execute(
        "INSERT INTO active_plans(user_id, plan_type, plan_id, updated_at) VALUES(?, 'workout', ?, ?)",
        (user_id, plan_id, utc_now()),
    )


async def _active_session(db: Database, user_id: int, started_at: datetime) -> None:
    await db.execute(
        """
        INSERT INTO sessions(user_id, code, name, plan, status, exercise_index, set_number, started_at, ended_at)
        VALUES(?, 'A', 'Workout A', '{}', 'active', 0, 0, ?, NULL)
        """,
        (user_id, started_at.astimezone(TZ).isoformat()),
    )


async def _completed_session(
    db: Database, user_id: int, started_at: datetime, ended_at: datetime, *, status: str = "completed"
) -> None:
    await db.execute(
        """
        INSERT INTO sessions(user_id, code, name, plan, status, exercise_index, set_number, started_at, ended_at)
        VALUES(?, 'A', 'Workout A', '{}', ?, 0, 0, ?, ?)
        """,
        (user_id, status, started_at.astimezone(TZ).isoformat(), ended_at.astimezone(TZ).isoformat()),
    )


async def _routine_profile(db: Database, user_id: int, weekday: int) -> None:
    profile = {"workout": {"common_weekdays": [weekday]}, "sleep": {}, "eating": {}}
    await db.execute(
        "INSERT INTO routine_profile(user_id, profile, updated_at) VALUES(?, ?, ?)",
        (user_id, json.dumps(profile), utc_now()),
    )


# ---------------------------------------------------------------------------
# 1. Actual workout start overrides today's planned start.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_active_session_overrides_planned_workout(db: Database) -> None:
    await _user(db)
    now = datetime(2026, 7, 12, 20, 30, tzinfo=TZ)  # Sunday
    await _workout_plan(db, 1, now, time_text="20:00")  # plan says 20:00
    await _active_session(db, 1, now - timedelta(minutes=13))  # actually started 20:17

    state = await resolve_workout_state(db, 1, now)

    assert state.phase == WorkoutPhase.DURING_WORKOUT
    assert state.source == "active_session"
    assert state.is_actual is True
    # The plan's 20:00 must not still be treated as "upcoming".
    assert state.is_future_plan is False


# ---------------------------------------------------------------------------
# 2. Today's planned workout overrides learned routine when no actual exists.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_overrides_routine_when_no_actual_workout(db: Database) -> None:
    await _user(db)
    now = datetime(2026, 7, 12, 17, 0, tzinfo=TZ)  # Sunday, ~1h before planned 18:00
    weekday = local_weekday(now)
    await _routine_profile(db, 1, weekday)  # routine also says "usually today"
    await _workout_plan(db, 1, now, time_text="18:00")

    state = await resolve_workout_state(db, 1, now)

    assert state.source == "active_workout_plan"
    assert state.phase in {WorkoutPhase.PRE_WORKOUT_NEAR, WorkoutPhase.PRE_WORKOUT_IMMEDIATE}
    assert workout_source_rank("active_workout_plan") > workout_source_rank("routine_pattern")


# ---------------------------------------------------------------------------
# 3. Learned routine used only as fallback when no plan exists.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_routine_only_used_as_fallback_without_plan(db: Database) -> None:
    await _user(db)
    now = datetime(2026, 7, 12, 17, 0, tzinfo=TZ)  # Sunday
    weekday = local_weekday(now)
    await _routine_profile(db, 1, weekday)
    # No active workout plan at all.

    state = await resolve_workout_state(db, 1, now)

    assert state.source == "routine_pattern"
    assert state.phase == WorkoutPhase.WORKOUT_STATUS_UNKNOWN


@pytest.mark.asyncio
async def test_no_evidence_falls_back_to_rest_day(db: Database) -> None:
    await _user(db)
    now = datetime(2026, 7, 12, 17, 0, tzinfo=TZ)

    state = await resolve_workout_state(db, 1, now)

    assert state.source == "no_workout_evidence"
    assert state.phase == WorkoutPhase.REST_DAY


# ---------------------------------------------------------------------------
# 4 / 5. Planned meal is never treated as consumed; a confirmed consumed meal
# appears in the workout decision / shared state.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_planned_meal_not_counted_as_consumed(db: Database) -> None:
    await _user(db)
    await _goal(db)
    now = datetime(2026, 7, 12, 12, 0, tzinfo=TZ)
    await db.execute(
        """
        INSERT INTO daily_flags(user_id, day, flags, created_at)
        VALUES(1, ?, ?, ?)
        """,
        (
            now.date().isoformat(),
            json.dumps({"next_meal_planned": [{"name": "planned dinner", "calories": 600, "protein": 40}]}),
            utc_now(),
        ),
    )

    state = await build_shared_state(db, 1, now=now)

    assert state.consumed_meals_today == ()
    assert "planned dinner" in state.planned_meal_titles


@pytest.mark.asyncio
async def test_confirmed_meal_appears_in_shared_state(db: Database) -> None:
    await _user(db)
    await _goal(db)
    now = datetime(2026, 7, 12, 12, 0, tzinfo=TZ)
    await _meal(db, 1, now, calories=500, protein=30, minutes_ago=20, name="lunch")

    state = await build_shared_state(db, 1, now=now)

    assert len(state.consumed_meals_today) == 1
    assert state.consumed_meals_today[0].name == "lunch"
    assert state.consumed_meals_today[0].calories == 500


# ---------------------------------------------------------------------------
# 6. Recent meal time preserves provenance/confidence.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recent_meal_state_preserves_confidence(db: Database) -> None:
    await _user(db)
    now = datetime(2026, 7, 12, 17, 0, tzinfo=TZ)
    await _meal(db, 1, now, calories=900, minutes_ago=10, name="big lunch")
    state = await build_shared_state(db, 1, now=now)
    recent = recent_meal_state_from_consumed(state.consumed_meals_today[0])

    assert recent is not None
    assert recent.time_confidence == "logged"
    assert recent.is_large is True


# ---------------------------------------------------------------------------
# 7 / 8. Large recent meal -> delay when demand is high and gap is short; NOT
# when demand is low or the workout is far enough away.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_large_meal_short_gap_high_demand_triggers_delay(db: Database) -> None:
    await _user(db)
    now = datetime(2026, 7, 12, 16, 55, tzinfo=TZ)  # Sunday
    heavy_session_exercises = [
        {"id": "squat", "sets": 5, "muscle": "רגליים"},
        {"id": "leg_press", "sets": 5, "muscle": "רגליים"},
        {"id": "rdl", "sets": 4, "muscle": "שרשרת אחורית"},
    ]
    await _workout_plan(db, 1, now, time_text="18:30", minutes=70, exercises=heavy_session_exercises)
    await _meal(db, 1, now, calories=920, minutes_ago=0, name="heavy lunch")

    state = await build_shared_state(db, 1, now=now)
    decision_ctx = project_workout_decision_context(state, session_plan=heavy_session_exercises and {
        "minutes": 70, "exercises": heavy_session_exercises,
    })
    recent = recent_meal_state_from_consumed(state.consumed_meals_today[0])
    demand = workout_demand_from_session(decision_ctx.session_plan)

    rec = evaluate_pre_workout_meal_timing(
        workout=state.workout,
        recent_meal=recent,
        demand=demand,
        minutes_until_workout=state.workout.minutes_until,
    )

    assert demand.is_high_demand is True
    assert rec is not None
    assert rec.should_delay is True
    assert rec.confidence in {"medium", "high"}


@pytest.mark.asyncio
async def test_low_demand_session_does_not_trigger_delay(db: Database) -> None:
    await _user(db)
    now = datetime(2026, 7, 12, 16, 55, tzinfo=TZ)
    light_session_exercises = [{"id": "curl", "sets": 3, "muscle": "יד קדמית"}]
    await _workout_plan(db, 1, now, time_text="18:30", minutes=20, exercises=light_session_exercises)
    await _meal(db, 1, now, calories=920, minutes_ago=0, name="heavy lunch")

    state = await build_shared_state(db, 1, now=now)
    demand = workout_demand_from_session({"minutes": 20, "exercises": light_session_exercises})
    recent = recent_meal_state_from_consumed(state.consumed_meals_today[0])

    rec = evaluate_pre_workout_meal_timing(
        workout=state.workout,
        recent_meal=recent,
        demand=demand,
        minutes_until_workout=state.workout.minutes_until,
    )

    assert demand.is_high_demand is False
    assert rec is None


@pytest.mark.asyncio
async def test_distant_workout_does_not_trigger_delay(db: Database) -> None:
    await _user(db)
    now = datetime(2026, 7, 12, 10, 0, tzinfo=TZ)
    heavy_session_exercises = [
        {"id": "squat", "sets": 5, "muscle": "רגליים"},
        {"id": "leg_press", "sets": 5, "muscle": "רגליים"},
    ]
    await _workout_plan(db, 1, now, time_text="18:30", minutes=70, exercises=heavy_session_exercises)
    await _meal(db, 1, now, calories=920, minutes_ago=0, name="heavy lunch")

    state = await build_shared_state(db, 1, now=now)
    demand = workout_demand_from_session({"minutes": 70, "exercises": heavy_session_exercises})
    recent = recent_meal_state_from_consumed(state.consumed_meals_today[0])

    rec = evaluate_pre_workout_meal_timing(
        workout=state.workout,
        recent_meal=recent,
        demand=demand,
        minutes_until_workout=state.workout.minutes_until,
    )

    # Workout is ~8.5h away — plenty of time regardless of meal size.
    assert rec is None


# ---------------------------------------------------------------------------
# 9. Low-confidence meal timing does not produce an overconfident exact delay.
# ---------------------------------------------------------------------------


def test_unknown_meal_time_confidence_yields_no_exact_minutes() -> None:
    from noam_coach.services.user_state import WorkoutState

    workout = WorkoutState(
        phase=WorkoutPhase.PRE_WORKOUT_NEAR,
        source="active_workout_plan",
        label="",
        minutes_until=90,
    )
    from noam_coach.services.meal_timing import RecentMealState

    recent = RecentMealState(calories=950, fat_g=None, minutes_since_eaten=None, time_confidence="unknown")
    demand = WorkoutDemand(planned_minutes=70, planned_sets=16, is_lower_body_focus=True)

    rec = evaluate_pre_workout_meal_timing(
        workout=workout, recent_meal=recent, demand=demand, minutes_until_workout=90
    )

    assert rec is not None
    assert rec.confidence == "low"
    assert rec.delay_minutes_min is None
    assert rec.delay_minutes_max is None


# ---------------------------------------------------------------------------
# 10. Missing HRV/RHR-type data does not silently become a "normal" default.
# ---------------------------------------------------------------------------


def test_tolerance_evidence_defaults_to_no_evidence_not_permissive() -> None:
    tolerance = UserToleranceEvidence()
    assert tolerance.sample_count == 0
    assert tolerance.tolerates_pre_workout_meals is None  # not True/False by default


def test_missing_fat_data_stays_unknown_not_zero() -> None:
    from noam_coach.services.meal_timing import RecentMealState

    meal = RecentMealState(calories=900, fat_g=None, minutes_since_eaten=10, time_confidence="logged")
    assert meal.fat_share is None
    assert meal.is_high_fat is None  # never silently "low fat"


# ---------------------------------------------------------------------------
# 11 / 12. One shared now; nutrition reads the same resolved workout state.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nutrition_and_shared_state_agree_on_workout_phase(db: Database) -> None:
    await _user(db)
    await _goal(db)
    now = datetime(2026, 7, 12, 17, 45, tzinfo=TZ)
    await _workout_plan(db, 1, now, time_text="18:30")

    shared = await build_shared_state(db, 1, now=now)
    workout_ctx = project_workout_decision_context(shared)
    nutrition_ctx = await build_nutrition_context(db, 1, "test", now=now)

    # Same instant, same resolved phase — no independent reconstruction.
    assert workout_ctx.now == shared.now
    assert nutrition_ctx.workout_status == shared.workout.phase.value
    assert nutrition_ctx.workout_source == shared.workout.source


@pytest.mark.asyncio
async def test_workout_and_nutrition_context_share_now(db: Database) -> None:
    await _user(db)
    await _goal(db)
    now = datetime(2026, 7, 12, 9, 0, tzinfo=TZ)

    shared = await build_shared_state(db, 1, now=now)
    nutrition_ctx = await build_workout_nutrition_context(db, 1, now=now)

    assert shared.now.isoformat() == nutrition_ctx.local_now


# ---------------------------------------------------------------------------
# 13. Active session immediately changes projected phase from planned to
#     actual/in-progress.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_starting_session_flips_phase_from_planned_to_in_progress(db: Database) -> None:
    await _user(db)
    now = datetime(2026, 7, 12, 18, 25, tzinfo=TZ)
    await _workout_plan(db, 1, now, time_text="18:30")

    before = await resolve_workout_state(db, 1, now)
    assert before.source == "active_workout_plan"
    assert before.is_future_plan is True

    await _active_session(db, 1, now)
    after = await resolve_workout_state(db, 1, now)

    assert after.source == "active_session"
    assert after.phase == WorkoutPhase.DURING_WORKOUT
    assert after.is_future_plan is False


# ---------------------------------------------------------------------------
# 14. Completed workout is not treated as a future planned workout.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_completed_workout_is_not_future_plan(db: Database) -> None:
    await _user(db)
    now = datetime(2026, 7, 12, 20, 0, tzinfo=TZ)
    await _workout_plan(db, 1, now, time_text="18:30")
    await _completed_session(db, 1, now - timedelta(hours=1, minutes=30), now - timedelta(minutes=20))

    state = await resolve_workout_state(db, 1, now)

    assert state.source == "completed_session"
    assert state.is_future_plan is False
    assert state.is_actual is True
    assert state.phase in {WorkoutPhase.POST_WORKOUT_IMMEDIATE, WorkoutPhase.POST_WORKOUT_LATER}


# ---------------------------------------------------------------------------
# 15. Routine time never overrides explicit current-day plan/schedule.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_routine_never_overrides_explicit_plan(db: Database) -> None:
    await _user(db)
    now = datetime(2026, 7, 12, 17, 0, tzinfo=TZ)
    weekday = local_weekday(now)
    # Routine says "usually trains today" but at no specific time; plan is explicit.
    await _routine_profile(db, 1, weekday)
    await _workout_plan(db, 1, now, time_text="20:00")

    state = await resolve_workout_state(db, 1, now)

    assert state.source == "active_workout_plan"
    assert state.planned_start is not None
    assert state.planned_start.hour == 20


# ---------------------------------------------------------------------------
# Precedence-policy unit coverage.
# ---------------------------------------------------------------------------


def test_precedence_rank_ordering() -> None:
    assert workout_source_rank("active_session") == PrecedenceRank.ACTUAL_CURRENT_DAY_EVENT
    assert workout_source_rank("active_workout_plan") == PrecedenceRank.CURRENT_PLAN
    assert workout_source_rank("routine_pattern") == PrecedenceRank.HIGH_CONFIDENCE_ROUTINE
    assert workout_source_rank("no_workout_evidence") == PrecedenceRank.DEFAULT
    assert (
        workout_source_rank("active_session")
        > workout_source_rank("active_workout_plan")
        > workout_source_rank("routine_pattern")
        > workout_source_rank("no_workout_evidence")
    )


def test_higher_precedence_source_picks_actual_over_plan() -> None:
    assert higher_precedence_source("active_workout_plan", "active_session") == "active_session"
    assert higher_precedence_source("active_session", "active_workout_plan") == "active_session"
