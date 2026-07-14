"""Batch D (FIX 52): bot and HealthKit workout events must be reconciled
for historical/adherence readers, not counted from one source only.

Root cause (verified before this fix): planning.adherence_snapshot() counted
bot sessions only; routine.learn_workout_pattern() read HealthKit only. A
HealthKit-only workout (imported, never started via the bot) was invisible
to adherence reporting even though the current-day resolver already
recognized it.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

import planning
from config import TZ
from db import Database
from helpers import utc_now
from noam_coach.services.workout_reconciliation import reconciled_workout_days


async def _make_db(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "batch_d_reconcile.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (utc_now(),),
    )
    return db


def _bounds(day: datetime) -> tuple[str, str]:
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return start.astimezone(TZ).isoformat(), end.astimezone(TZ).isoformat()


@pytest.mark.asyncio
async def test_healthkit_only_workout_counts_in_reconciled_days(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    day = datetime(2026, 7, 10, 18, 0, tzinfo=TZ)
    start_utc, end_utc = _bounds(day)

    await db.execute(
        """
        INSERT INTO health(user_id, external_id, sample_type, value, unit, start_time, end_time, created_at)
        VALUES(1, 'hk-1', 'workout', 45, 'min', ?, ?, ?)
        """,
        (day.isoformat(), (day + timedelta(minutes=45)).isoformat(), utc_now()),
    )

    days = await reconciled_workout_days(db, 1, start_utc, end_utc)
    assert days == {"2026-07-10"}


@pytest.mark.asyncio
async def test_bot_only_workout_counts_in_reconciled_days(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    day = datetime(2026, 7, 10, 18, 0, tzinfo=TZ)
    start_utc, end_utc = _bounds(day)

    await db.execute(
        """
        INSERT INTO sessions(user_id, code, name, plan, status, exercise_index, set_number, started_at, ended_at)
        VALUES(1, 'A', 'Workout A', '{}', 'completed', 0, 0, ?, ?)
        """,
        (day.isoformat(), (day + timedelta(minutes=45)).isoformat()),
    )

    days = await reconciled_workout_days(db, 1, start_utc, end_utc)
    assert days == {"2026-07-10"}


@pytest.mark.asyncio
async def test_same_day_both_sources_counts_once(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    day = datetime(2026, 7, 10, 18, 0, tzinfo=TZ)
    start_utc, end_utc = _bounds(day)

    await db.execute(
        """
        INSERT INTO sessions(user_id, code, name, plan, status, exercise_index, set_number, started_at, ended_at)
        VALUES(1, 'A', 'Workout A', '{}', 'completed', 0, 0, ?, ?)
        """,
        (day.isoformat(), (day + timedelta(minutes=45)).isoformat()),
    )
    await db.execute(
        """
        INSERT INTO health(user_id, external_id, sample_type, value, unit, start_time, end_time, created_at)
        VALUES(1, 'hk-1', 'workout', 45, 'min', ?, ?, ?)
        """,
        (day.isoformat(), (day + timedelta(minutes=45)).isoformat(), utc_now()),
    )

    days = await reconciled_workout_days(db, 1, start_utc, end_utc)
    assert days == {"2026-07-10"}
    assert len(days) == 1


@pytest.mark.asyncio
async def test_two_genuine_workouts_on_different_days_both_count(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    day1 = datetime(2026, 7, 10, 18, 0, tzinfo=TZ)
    day2 = datetime(2026, 7, 11, 18, 0, tzinfo=TZ)
    start_utc, _ = _bounds(day1)
    _, end_utc = _bounds(day2)

    await db.execute(
        """
        INSERT INTO sessions(user_id, code, name, plan, status, exercise_index, set_number, started_at, ended_at)
        VALUES(1, 'A', 'Workout A', '{}', 'completed', 0, 0, ?, ?)
        """,
        (day1.isoformat(), (day1 + timedelta(minutes=45)).isoformat()),
    )
    await db.execute(
        """
        INSERT INTO health(user_id, external_id, sample_type, value, unit, start_time, end_time, created_at)
        VALUES(1, 'hk-1', 'workout', 45, 'min', ?, ?, ?)
        """,
        (day2.isoformat(), (day2 + timedelta(minutes=45)).isoformat(), utc_now()),
    )

    days = await reconciled_workout_days(db, 1, start_utc, end_utc)
    assert days == {"2026-07-10", "2026-07-11"}


@pytest.mark.asyncio
async def test_adherence_snapshot_exposes_reconciled_count_alongside_bot_only(
    tmp_path: Path,
) -> None:
    """adherence_snapshot's existing workouts_completed field keeps its
    bot-only meaning (backward compatible); the new field adds HealthKit
    visibility instead of replacing it."""
    db = await _make_db(tmp_path)
    day = datetime(2026, 7, 10, 18, 0, tzinfo=TZ)
    start_utc, end_utc = _bounds(day)

    await db.execute(
        "INSERT INTO goal_versions(user_id, calories, protein, steps, phase, status, source, created_at) "
        "VALUES(1, 2100, 160, 8000, 'fat_loss_muscle_retention', 'active', 'test', ?)",
        (utc_now(),),
    )
    await db.execute(
        """
        INSERT INTO health(user_id, external_id, sample_type, value, unit, start_time, end_time, created_at)
        VALUES(1, 'hk-1', 'workout', 45, 'min', ?, ?, ?)
        """,
        (day.isoformat(), (day + timedelta(minutes=45)).isoformat(), utc_now()),
    )

    snapshot = await planning.adherence_snapshot(db, 1, start_utc, end_utc)
    assert snapshot["workouts_completed"] == 0  # bot-only, unchanged meaning
    assert snapshot["workout_days_reconciled"] == 1  # HealthKit now visible
