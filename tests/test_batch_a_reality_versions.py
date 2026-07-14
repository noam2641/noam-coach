"""Batch A (FIX 41, 43, 57/22): canonical coaching-day boundary, atomic
daily_flags compare-and-swap, and meal-lifecycle invalidation of dependent
day-state projections.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

import coach_bot
import user_model
from config import TZ
from noam_coach.services import daily_menu_state
from noam_coach.services import next_meal as next_meal_service
from noam_coach.services.coaching_day import calendar_day_key, resolve_coaching_day
from noam_coach.services.daily_flags_cas import (
    DailyFlagsConflict,
    get_daily_flags_with_revision,
    patch_daily_flags,
)
from noam_coach.services.day_state_invalidation import invalidate_day_projections


async def _make_db(tmp_path: Path) -> coach_bot.Database:
    db = coach_bot.Database(str(tmp_path / "coach.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (coach_bot.utc_now(),),
    )
    return db


# ---------------------------------------------------------------------------
# FIX 41: canonical coaching-day boundary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_sleep_schedule_falls_back_to_calendar_day(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    coach_bot.DB.path = db.path

    late_night = datetime(2026, 3, 5, 0, 30, tzinfo=TZ)
    result = await resolve_coaching_day(db, 1, local_now=late_night)
    assert result.rollover_reason == "calendar_midnight"
    assert result.day_key == calendar_day_key(late_night)


@pytest.mark.asyncio
async def test_confirmed_sleep_schedule_keeps_post_midnight_as_previous_day(
    tmp_path: Path,
) -> None:
    """The exact FIX 41 scenario: user sleeps at 01:00, so 00:30 must still
    resolve to the previous coaching day, not a fresh one."""
    db = await _make_db(tmp_path)
    coach_bot.DB.path = db.path

    await user_model.set_fact(
        db, 1, "sleep_schedule", {"bedtime": "01:00", "wake_time": "08:00"},
        kind=user_model.KIND_FACT, source=user_model.SOURCE_USER, confirmed=True,
    )

    evening = datetime(2026, 3, 5, 22, 0, tzinfo=TZ)
    evening_result = await resolve_coaching_day(db, 1, local_now=evening)

    just_after_midnight = datetime(2026, 3, 6, 0, 30, tzinfo=TZ)
    post_midnight_result = await resolve_coaching_day(db, 1, local_now=just_after_midnight)

    assert post_midnight_result.day_key == evening_result.day_key
    assert post_midnight_result.rollover_reason == "sleep_schedule_bedtime"
    assert post_midnight_result.phase == "post_midnight_pre_wake"

    # Well past the bedtime+grace window, a new coaching day must start.
    next_afternoon = datetime(2026, 3, 6, 14, 0, tzinfo=TZ)
    next_day_result = await resolve_coaching_day(db, 1, local_now=next_afternoon)
    assert next_day_result.day_key != evening_result.day_key


@pytest.mark.asyncio
async def test_unconfirmed_sleep_schedule_does_not_drive_day_boundary(tmp_path: Path) -> None:
    """FIX 47 policy applied locally: an unconfirmed/estimated bedtime must
    not become decision-grade for day-boundary math."""
    db = await _make_db(tmp_path)
    coach_bot.DB.path = db.path

    await user_model.set_fact(
        db, 1, "sleep_schedule", {"bedtime": "01:00", "wake_time": "08:00"},
        kind=user_model.KIND_ESTIMATE, source=user_model.SOURCE_DERIVED,
        confidence=0.7, confirmed=False,
    )

    just_after_midnight = datetime(2026, 3, 6, 0, 30, tzinfo=TZ)
    result = await resolve_coaching_day(db, 1, local_now=just_after_midnight)
    assert result.rollover_reason == "calendar_midnight"


# ---------------------------------------------------------------------------
# FIX 57/22: daily_flags compare-and-swap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patch_daily_flags_preserves_unrelated_keys_across_concurrent_writers(
    tmp_path: Path,
) -> None:
    """The exact FIX 57 scenario: two writers touch different keys; both
    updates must survive, not just the last writer's."""
    db = await _make_db(tmp_path)
    now = coach_bot.utc_now()

    await patch_daily_flags(
        db, 1, "2026-03-05", lambda flags: {**flags, "workout_status": "later"},
        created_at=now,
    )
    await patch_daily_flags(
        db, 1, "2026-03-05", lambda flags: {**flags, "medication": "taken"},
        created_at=now,
    )

    final_flags, revision = await get_daily_flags_with_revision(db, 1, "2026-03-05")
    assert final_flags["workout_status"] == "later"
    assert final_flags["medication"] == "taken"
    assert revision == 2


@pytest.mark.asyncio
async def test_patch_daily_flags_raises_after_exhausting_retries_on_permanent_conflict(
    tmp_path: Path,
) -> None:
    db = await _make_db(tmp_path)
    now = coach_bot.utc_now()

    # Deterministic conflict: pre-seed a row, then patch while a helper
    # directly bumps the revision underneath every read.
    await db.execute(
        "INSERT INTO daily_flags(user_id, day, flags, created_at, revision) "
        "VALUES(1, '2026-03-05', '{}', ?, 1)",
        (now,),
    )

    from noam_coach.services import daily_flags_cas as cas_module

    original_read = cas_module._read_flags_and_revision

    async def _always_stale_revision(db_arg, user_id, day):
        # Report a revision that never matches the row's real (and constantly
        # advancing) revision, so every CAS UPDATE attempt loses the race --
        # simulating a writer that can never catch up to a hostile concurrent
        # bumper. Also bump the real row so the mismatch is genuine, not just
        # a stale read.
        await db_arg.execute(
            "UPDATE daily_flags SET revision=revision+1 WHERE user_id=? AND day=?",
            (user_id, day),
        )
        flags, _real_revision = await original_read(db_arg, user_id, day)
        return flags, -1

    cas_module._read_flags_and_revision = _always_stale_revision
    try:
        with pytest.raises(DailyFlagsConflict):
            await patch_daily_flags(
                db, 1, "2026-03-05", lambda flags: {**flags, "y": 1}, created_at=now
            )
    finally:
        cas_module._read_flags_and_revision = original_read


# ---------------------------------------------------------------------------
# FIX 43: meal lifecycle invalidates active menu / active recommendation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalidate_day_projections_marks_active_menu_stale(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    coach_bot.DB.path = db.path

    await daily_menu_state.remember_active_daily_menu(db, 1, text="תפריט לדוגמה")
    result = await invalidate_day_projections(db, 1, reason="meal_created")
    assert result["active_daily_menu"] is True

    menu = await daily_menu_state.get_active_daily_menu(db, 1)
    assert menu["stale"] is True
    assert menu["stale_reason"] == "meal_created"
    # Old text/history is preserved, not deleted.
    assert menu["text"] == "תפריט לדוגמה"


@pytest.mark.asyncio
async def test_invalidate_day_projections_clears_active_recommendation(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    coach_bot.DB.path = db.path

    from datetime import timedelta

    from noam_coach.services import core as core_services

    await core_services.set_flow_state(
        1, next_meal_service.ACTIVE_RECOMMENDATION_FLOW, "active",
        {"options": [], "option_payloads": [], "option_titles": [],
         "remaining_calories": 500, "budget_policy": "normal", "message_id": None,
         "created_at": datetime.now(TZ).isoformat(),
         "expiry": (datetime.now(TZ) + timedelta(hours=6)).isoformat()},
    )

    result = await invalidate_day_projections(db, 1, reason="meal_created")
    assert result["active_recommendation"] is True

    state = await next_meal_service.get_active_recommendation_state(db, 1)
    assert state is None


@pytest.mark.asyncio
async def test_invalidate_day_projections_no_op_when_nothing_active(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    coach_bot.DB.path = db.path

    result = await invalidate_day_projections(db, 1, reason="meal_created")
    # routine_profile refresh (FIX 42, added in Batch E) is unconditional --
    # it does not depend on an active menu/recommendation existing.
    assert result["active_daily_menu"] is False
    assert result["active_recommendation"] is False
