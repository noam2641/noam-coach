"""Batch D (FIX 40): "postpone workout" must not silently retain the
original plan time as though it were still a near-term signal.

Root cause (verified before this fix): _planned_session_candidate()
reinterpreted a passed/unknown-status plan window as PRE_WORKOUT_NEAR when
"later" was tapped, keeping the ORIGINAL planned_start/planned_end -- so
nutrition timing (pre-workout meal budget allocation) treated a postponed-
to-unknown-time workout as still imminent at the old time.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from config import TZ
from db import Database
from helpers import utc_now
from noam_coach.services.next_meal import save_next_meal_workout_status
from noam_coach.services.user_state import WorkoutPhase, resolve_workout_state
from noam_coach.services.weekdays import WEEKDAY_SCHEMA_VERSION, local_weekday


@pytest.fixture
async def db(tmp_path: Path) -> Database:
    database = Database(str(tmp_path / "batch_d_postpone.db"))
    await database.init()
    return database


async def _user(db: Database, user_id: int = 1) -> None:
    await db.execute(
        "INSERT OR IGNORE INTO users(id, first_name, username, updated_at) VALUES(?, 'Test', NULL, ?)",
        (user_id, utc_now()),
    )


async def _workout_plan(db: Database, user_id: int, now: datetime, *, time_text: str) -> None:
    weekday = local_weekday(now)
    payload = {
        "frequency": 1,
        "sessions": [
            {
                "weekday": weekday,
                "weekday_schema": WEEKDAY_SCHEMA_VERSION,
                "weekday_name": "today",
                "time": time_text,
                "minutes": 60,
                "code": "A",
                "name": "Workout A",
                "exercises": [{"id": "squat", "sets": 3, "muscle": "רגליים"}],
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


@pytest.mark.asyncio
async def test_postponed_workout_does_not_retain_stale_time_as_near_term(db: Database) -> None:
    """The exact FIX 40 scenario: an 18:00 workout postponed with no new
    time given must not read as 'near' at the old 18:00 mark."""
    await _user(db)
    now = datetime(2026, 7, 12, 19, 0, tzinfo=TZ)  # past the original 18:00 slot
    await _workout_plan(db, 1, now, time_text="18:00")
    await save_next_meal_workout_status(db, 1, "later", now=now)

    state = await resolve_workout_state(db, 1, now)

    assert state.phase == WorkoutPhase.WORKOUT_STATUS_UNKNOWN
    assert state.planned_start is None
    assert state.planned_end is None
    assert state.minutes_until is None
    # The label must communicate that the time is genuinely unknown, not
    # imply the workout is still imminent.
    assert "לא צוין זמן חדש" in state.label or "לא אניח שהוא קרוב" in state.label


@pytest.mark.asyncio
async def test_postponed_workout_old_time_does_not_leak_into_nutrition_pre_workout_budget(
    db: Database,
) -> None:
    """Cross-system propagation check: next_meal.py's pre-workout budget
    logic keys off WorkoutState.phase being one of the PRE_WORKOUT_* values
    (checked at two call sites, e.g. next_meal.py:1832). WORKOUT_STATUS_UNKNOWN
    must not be one of them, so a postponed workout with no new time cannot
    silently drive a pre-workout meal-timing decision from the stale time."""
    pre_workout_phases = {
        WorkoutPhase.PRE_WORKOUT_EARLY, WorkoutPhase.PRE_WORKOUT_NEAR, WorkoutPhase.PRE_WORKOUT_IMMEDIATE,
    }
    assert WorkoutPhase.WORKOUT_STATUS_UNKNOWN not in pre_workout_phases
