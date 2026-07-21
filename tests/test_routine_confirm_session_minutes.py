"""Confirmed-user-knowledge audit fix: a workout-duration value the user
described in free text and explicitly confirmed via the "describe your day"
summary card must actually satisfy the workout-plan prerequisite check and
be visible to the planner.

Root cause (traced, not assumed): ``save_routine_extraction`` used to write
the extracted ``available_workout_minutes`` value ONLY nested inside
``workout_window.minutes`` -- a location no downstream reader (readiness or
planning) ever consults. Both the plan-completion prerequisite checker
(``user_model.compute_readiness``'s workout profile) and the planner
(``availability.resolve_availability``, ``planning.py``) read a dedicated
``session_minutes`` fact key that this flow never wrote, so a confirmed value
still showed up as "missing" and was asked again.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import coach_bot
import user_model
from models import RoutineExtraction
from noam_coach.services.profile import (
    ROUTINE_ESTIMATE_KEYS,
    confirm_routine_facts,
    save_routine_extraction,
)


async def _make_db(tmp_path: Path) -> coach_bot.Database:
    db = coach_bot.Database(str(tmp_path / "coach.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (coach_bot.utc_now(),),
    )
    return db


@pytest.mark.asyncio
async def test_session_minutes_is_registered_for_confirmation(tmp_path: Path) -> None:
    """session_minutes must be in the set confirm_routine_facts iterates --
    otherwise a confirmed extraction's duration is never flipped from
    KIND_ESTIMATE to confirmed, the same way work_schedule/sleep_schedule/etc
    already are."""
    assert "session_minutes" in ROUTINE_ESTIMATE_KEYS


@pytest.mark.asyncio
async def test_confirmed_workout_duration_reaches_session_minutes_fact(
    tmp_path: Path,
) -> None:
    db = await _make_db(tmp_path)
    coach_bot.DB.path = db.path

    extraction = RoutineExtraction(
        preferred_workout_time="19:00",
        available_workout_minutes=45,
    )
    await save_routine_extraction(1, extraction)

    fact = await user_model.get_fact(db, 1, "session_minutes")
    assert fact is not None, "session_minutes was never written by save_routine_extraction"
    assert int(fact["value"]) == 45
    assert fact["kind"] == user_model.KIND_ESTIMATE  # unconfirmed until routine:confirm

    await confirm_routine_facts(1)
    confirmed = await user_model.get_fact(db, 1, "session_minutes")
    # confirm_fact() flips confirmed=True and raises the confidence floor; it
    # does not itself change kind (matches every other ROUTINE_ESTIMATE_KEYS
    # field's behavior -- confirmed becomes the readiness-relevant signal).
    assert confirmed["confirmed"] is True
    assert confirmed["confidence"] >= 0.9


@pytest.mark.asyncio
async def test_confirmed_workout_duration_satisfies_readiness_prerequisite(
    tmp_path: Path,
) -> None:
    """The exact reported bug: after extraction + confirmation, the workout
    readiness profile must NOT list session_minutes as missing."""
    db = await _make_db(tmp_path)
    coach_bot.DB.path = db.path

    for key, value in {
        "primary_goal": "fat_loss_muscle_retention",
        "training_days_per_week": 4,
        "training_limitations": "none",
        "training_location": "gym",
        "equipment": "full_gym",
        "strength_experience": "intermediate",
        "weekly_availability": "1,2,3,4",
    }.items():
        await user_model.set_fact(
            db, 1, key, value,
            kind=user_model.KIND_FACT, source=user_model.SOURCE_USER, confirmed=True,
        )

    extraction = RoutineExtraction(preferred_workout_time="19:00", available_workout_minutes=45)
    await save_routine_extraction(1, extraction)
    await confirm_routine_facts(1)

    readiness = await user_model.compute_readiness(db, 1, "workout")
    assert "session_minutes" not in readiness["missing"]
    assert "session_minutes" in readiness["present"]


@pytest.mark.asyncio
async def test_workout_duration_still_nested_in_workout_window_for_compat(
    tmp_path: Path,
) -> None:
    """The pre-existing workout_window.minutes write is left in place
    (backward-compatible, low-risk) -- this pins that it still happens
    alongside the new dedicated session_minutes fact, not instead of it."""
    db = await _make_db(tmp_path)
    coach_bot.DB.path = db.path

    extraction = RoutineExtraction(preferred_workout_time="19:00", available_workout_minutes=60)
    await save_routine_extraction(1, extraction)

    window_fact = await user_model.get_fact(db, 1, "workout_window")
    assert window_fact["value"]["minutes"] == 60
    session_fact = await user_model.get_fact(db, 1, "session_minutes")
    assert int(session_fact["value"]) == 60


@pytest.mark.asyncio
async def test_no_stated_duration_does_not_write_a_fabricated_session_minutes(
    tmp_path: Path,
) -> None:
    """If the user's description didn't mention workout duration at all,
    nothing should be written -- never fabricate a default."""
    db = await _make_db(tmp_path)
    coach_bot.DB.path = db.path

    extraction = RoutineExtraction(preferred_workout_time="19:00")  # no duration
    await save_routine_extraction(1, extraction)

    fact = await user_model.get_fact(db, 1, "session_minutes")
    assert fact is None
