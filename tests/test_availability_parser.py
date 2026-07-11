from __future__ import annotations

from pathlib import Path

import pytest

import user_model
from db import Database
from helpers import utc_now
from noam_coach.services.availability import (
    parse_hebrew_availability_answer,
    resolve_availability,
)
from noam_coach.services.weekdays import WEEKDAY_SCHEMA_VERSION


def test_parse_hebrew_availability_days_time_and_duration() -> None:
    parsed = parse_hebrew_availability_answer("ימי ראשון ורביעי בערב, 45 דקות")

    assert [slot["weekday"] for slot in parsed.weekly_availability] == [2, 6]
    assert {slot["weekday_schema"] for slot in parsed.weekly_availability} == {WEEKDAY_SCHEMA_VERSION}
    assert all(slot["start"] == "18:00" for slot in parsed.weekly_availability)
    assert all(slot["minutes"] == 45 for slot in parsed.weekly_availability)
    assert parsed.workout_window == "18:00"
    assert parsed.session_minutes == 45
    assert parsed.training_days_per_week == 2


def test_parse_hebrew_availability_explicit_hour() -> None:
    parsed = parse_hebrew_availability_answer("שני וחמישי ב־19:30")

    assert [slot["weekday"] for slot in parsed.weekly_availability] == [0, 3]
    assert parsed.workout_window == "19:30"


def test_parse_hebrew_availability_per_day_distinct_times() -> None:
    """H-NEW-1 regression: each day keeps its own time; a bare hour means HH:00.

    The first time must NOT be applied to every day, and the duration must be
    stored separately from the hours.
    """
    parsed = parse_hebrew_availability_answer("ראשון 19:00, שני 19, שלישי 18:30 45 דקות")

    by_day = {slot["weekday"]: slot for slot in parsed.weekly_availability}
    assert sorted(by_day) == [0, 1, 6]
    assert by_day[6]["start"] == "19:00"          # ראשון
    assert by_day[0]["start"] == "19:00"          # שני (bare hour 19 -> 19:00)
    assert by_day[1]["start"] == "18:30"          # שלישי MUST be 18:30, not 19:00
    assert all(slot["minutes"] == 45 for slot in parsed.weekly_availability)
    assert parsed.session_minutes == 45


def test_parse_hebrew_availability_global_fallback_only_for_missing_day() -> None:
    """A day without its own time falls back to the shared window; an explicit
    per-day time is never overwritten by the fallback."""
    parsed = parse_hebrew_availability_answer("שני וחמישי 20:00, שישי 17:00")

    by_day = {slot["weekday"]: slot["start"] for slot in parsed.weekly_availability}
    assert by_day == {0: "20:00", 3: "20:00", 4: "17:00"}


@pytest.mark.asyncio
async def test_resolve_availability_uses_parsed_hebrew_slots(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "availability_parser.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1, 'Test', NULL, ?)",
        (utc_now(),),
    )
    parsed = parse_hebrew_availability_answer("ראשון ורביעי בערב, שעה")
    await user_model.set_fact(
        db,
        1,
        "weekly_availability",
        parsed.weekly_availability,
        source=user_model.SOURCE_USER,
        confirmed=True,
    )
    await user_model.set_fact(
        db,
        1,
        "workout_window",
        parsed.workout_window,
        source=user_model.SOURCE_USER,
        confirmed=True,
    )
    await user_model.set_fact(
        db,
        1,
        "session_minutes",
        parsed.session_minutes,
        source=user_model.SOURCE_USER,
        confirmed=True,
    )

    availability = await resolve_availability(db, 1)

    assert availability.preferred_days == [2, 6]
    assert availability.preferred_time == "18:00"
    assert availability.session_minutes == 60
    assert availability.max_days_per_week == 2


@pytest.mark.asyncio
async def test_resolve_availability_ignores_active_plan_as_source(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "availability_active_plan.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1, 'Test', NULL, ?)",
        (utc_now(),),
    )
    await user_model.set_fact(
        db,
        1,
        "active_workout_plan",
        {"frequency": 6, "sessions": [{"weekday": 1}, {"weekday": 3}, {"weekday": 5}]},
        source=user_model.SOURCE_SYSTEM,
        confirmed=True,
    )

    availability = await resolve_availability(db, 1)

    assert availability.max_days_per_week == 3
    assert availability.preferred_days == [0, 2, 4]
    assert availability.source == "default"


@pytest.mark.asyncio
async def test_resolve_availability_marks_missing_weekday_schema_for_confirmation(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "availability_schema.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1, 'Test', NULL, ?)",
        (utc_now(),),
    )
    await user_model.set_fact(
        db,
        1,
        "weekly_availability",
        [{"weekday": 0, "start": "19:00", "minutes": 45, "available": True}],
        source=user_model.SOURCE_USER,
        confirmed=True,
    )

    availability = await resolve_availability(db, 1)

    assert availability.preferred_days == [0]
    assert "missing_or_unknown_weekday_schema" in (availability.warnings or [])


@pytest.mark.asyncio
async def test_active_training_days_override_health_detected_days(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "availability_active_days.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1, 'Test', NULL, ?)",
        (utc_now(),),
    )
    await user_model.set_fact(
        db,
        1,
        "workout_pattern",
        {
            "weekly_frequency": 2,
            "common_weekdays": [0, 2],
            "weekday_schema": WEEKDAY_SCHEMA_VERSION,
        },
        kind=user_model.KIND_ESTIMATE,
        source=user_model.SOURCE_DERIVED,
        confirmed=False,
    )
    await user_model.set_fact(
        db,
        1,
        "active_training_days",
        [6, 0, 2, 4],
        kind=user_model.KIND_FACT,
        source=user_model.SOURCE_USER,
        confirmed=True,
    )
    availability = await resolve_availability(db, 1)
    assert availability.preferred_days == [0, 2, 4, 6]
    assert availability.max_days_per_week == 4
    assert availability.field_sources["preferred_days"] == "user_corrected"
