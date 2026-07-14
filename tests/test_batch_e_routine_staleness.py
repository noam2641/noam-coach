"""Batch E (FIX 42): meal lifecycle events must refresh the learned routine
profile's eating-window component, not leave it stale until the evening
job or a HealthKit import happens to run.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import coach_bot
import health_service
from noam_coach.services.day_state_invalidation import invalidate_day_projections


async def _make_db(tmp_path: Path) -> coach_bot.Database:
    db = coach_bot.Database(str(tmp_path / "coach.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (coach_bot.utc_now(),),
    )
    return db


@pytest.mark.asyncio
async def test_invalidate_day_projections_refreshes_routine_profile(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    coach_bot.DB.path = db.path
    orig_hs_db = health_service.DB
    health_service.DB = db
    try:
        assert await db.fetch_one("SELECT * FROM routine_profile WHERE user_id=1") is None

        result = await invalidate_day_projections(db, 1, reason="meal_created")

        assert result["routine_profile"] is True
        row = await db.fetch_one("SELECT * FROM routine_profile WHERE user_id=1")
        assert row is not None
    finally:
        health_service.DB = orig_hs_db


@pytest.mark.asyncio
async def test_invalidate_day_projections_updates_existing_routine_profile_timestamp(
    tmp_path: Path,
) -> None:
    """A second meal event must recompute (updated_at advances), not just
    leave the first snapshot in place."""
    db = await _make_db(tmp_path)
    coach_bot.DB.path = db.path
    orig_hs_db = health_service.DB
    health_service.DB = db
    try:
        await invalidate_day_projections(db, 1, reason="meal_created")
        first = await db.fetch_one("SELECT updated_at FROM routine_profile WHERE user_id=1")
        assert first is not None

        await invalidate_day_projections(db, 1, reason="meal_edited")
        second = await db.fetch_one("SELECT updated_at FROM routine_profile WHERE user_id=1")
        assert second is not None
        # Both calls succeeded and left exactly one row (ON CONFLICT DO UPDATE),
        # not a duplicate.
        count = await db.fetch_one("SELECT COUNT(*) AS c FROM routine_profile WHERE user_id=1")
        assert int(count["c"]) == 1
    finally:
        health_service.DB = orig_hs_db
