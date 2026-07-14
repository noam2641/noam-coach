"""Batch I (FIX 54, gap identified during the final cross-system pass):
HealthKit import must invalidate dependent day-state projections when it
actually inserts new rows, and must NOT invalidate on a duplicate-only
import.

Root cause: import_health_export_file() called sync_health_measurements_to_facts
and save_routine_profile, but never called the invalidation contract Batch
A/E already built for meal events -- an active daily menu or next-meal
recommendation built from pre-import reality stayed actionable after an
import that materially changed the day (e.g. today's workout appeared).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import coach_bot
from db import Database
from helpers import utc_now
from noam_coach.services import health_jobs
from noam_coach.services.daily_menu_state import remember_active_daily_menu


def _write_export_xml(path: Path, *, with_workout: bool) -> None:
    now = datetime.now(timezone.utc)
    start = (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S +0000")
    end = now.strftime("%Y-%m-%d %H:%M:%S +0000")
    body = ""
    if with_workout:
        body = (
            f'<Workout workoutActivityType="HKWorkoutActivityTypeRunning" '
            f'duration="45" durationUnit="min" sourceName="Watch" '
            f'startDate="{start}" endDate="{end}"/>'
        )
    xml = f"<?xml version='1.0'?><HealthData>{body}</HealthData>"
    path.write_text(xml, encoding="utf-8")


async def _make_db(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "batch_i_health_import.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (utc_now(),),
    )
    return db


@pytest.mark.asyncio
async def test_import_with_new_rows_invalidates_active_menu(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    coach_bot.DB.path = db.path
    health_jobs.DB = db

    await remember_active_daily_menu(db, 1, text="<b>תפריט</b>", strategy="balanced")

    xml_path = tmp_path / "export.xml"
    _write_export_xml(xml_path, with_workout=True)

    await health_jobs.import_health_export_file(
        1, xml_path, max_bytes=10_000_000, audit_source="test",
    )

    from noam_coach.services.daily_menu_state import get_active_daily_menu

    menu = await get_active_daily_menu(db, 1)
    assert menu is not None
    assert menu.get("stale") is True


@pytest.mark.asyncio
async def test_import_with_zero_rows_does_not_invalidate(tmp_path: Path) -> None:
    """The exact required-behavior nuance: a duplicate-only / empty import
    (nothing new inserted) must cause no invalidation."""
    db = await _make_db(tmp_path)
    coach_bot.DB.path = db.path
    health_jobs.DB = db

    await remember_active_daily_menu(db, 1, text="<b>תפריט</b>", strategy="balanced")

    xml_path = tmp_path / "export_empty.xml"
    _write_export_xml(xml_path, with_workout=False)

    await health_jobs.import_health_export_file(
        1, xml_path, max_bytes=10_000_000, audit_source="test",
    )

    from noam_coach.services.daily_menu_state import get_active_daily_menu

    menu = await get_active_daily_menu(db, 1)
    assert menu is not None
    assert not menu.get("stale")
