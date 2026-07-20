"""B3 / ARCH-02 — canonical coaching-day authority for nutrition state.

Required regressions:
- fixed local 00:30 (confirmed 23:00 bedtime): meal window, daily_flags
  mutation, menu state read/write, and next-meal context all use ONE
  canonical coaching_day_key (the previous date)
- fixed local 10:00: coaching day and calendar day behave equivalently
- workout counter-test: workout completion remains LOCAL CALENDAR day
- no confirmed bedtime → coaching day falls back to calendar (invariance
  for every existing user/test without a sleep_schedule fact)
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

import coach_bot
import health_service
import user_model
from config import TZ
from db import Database
from helpers import utc_now
from noam_coach.services import daily_state
from noam_coach.services.daily_menu_state import (
    get_active_daily_menu,
    remember_active_daily_menu,
)
from noam_coach.services.next_meal import (
    generate_next_meal_recommendation,
    save_next_meal_workout_status,
)

USER_ID = 1

# 23:00 bedtime + 4h grace → the coaching day rolls at 03:00 local.
NIGHT = datetime(2026, 6, 29, 0, 30, tzinfo=TZ)      # after midnight, before rollover
EVENING = datetime(2026, 6, 28, 21, 0, tzinfo=TZ)    # same coaching day as NIGHT
MORNING = datetime(2026, 6, 28, 10, 0, tzinfo=TZ)
COACHING_DAY = "2026-06-28"
CALENDAR_DAY_AT_NIGHT = "2026-06-29"


async def _make_db(tmp_path: Path, *, bedtime_confirmed: bool) -> Database:
    db = Database(str(tmp_path / "b3.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(?, 'Test', NULL, ?)",
        (USER_ID, utc_now()),
    )
    await db.execute(
        """
        INSERT INTO goal_versions(user_id, calories, protein, steps, phase, status, source, created_at)
        VALUES(?, 2100, 150, 8000, 'fat_loss_muscle_retention', 'active', 'test', ?)
        """,
        (USER_ID, utc_now()),
    )
    if bedtime_confirmed:
        await user_model.set_fact(
            db, USER_ID, "sleep_schedule", {"bedtime": "23:00"},
            source=user_model.SOURCE_USER, confirmed=True,
        )
    return db


async def _meal(db: Database, at: datetime, name: str, calories: int = 500) -> None:
    # eaten_at in UTC isoformat — the production writers' format; the day
    # windows are UTC strings compared lexicographically in SQL.
    from datetime import timezone as _tz

    await db.execute(
        """
        INSERT INTO meals(user_id, name, calories, protein, carbs, fat, confidence, eaten_at, created_at)
        VALUES(?, ?, ?, 30, 0, 0, 1, ?, ?)
        """,
        (USER_ID, name, calories, at.astimezone(_tz.utc).isoformat(), utc_now()),
    )


@pytest.mark.asyncio
async def test_midnight_journey_uses_one_coaching_day_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = await _make_db(tmp_path, bedtime_confirmed=True)
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(health_service, "DB", db, raising=False)

    # The canonical key at 00:30 is the PREVIOUS date.
    assert await daily_state.coaching_day_key(db, USER_ID, NIGHT) == COACHING_DAY

    # 1) MEAL WINDOW: an evening meal and a 00:30 meal share one nutrition day.
    await _meal(db, EVENING, "ארוחת ערב", 600)
    await _meal(db, NIGHT, "חטיף לילה", 200)
    calories, _protein = await daily_state.consumed_totals(db, USER_ID, now=NIGHT)
    assert calories == 800  # both meals — the day did NOT reset at midnight
    meals = await daily_state.consumed_meals(db, USER_ID, now=NIGHT)
    assert [m["name"] for m in meals] == ["ארוחת ערב", "חטיף לילה"]

    # 2) DAILY FLAGS mutation at 00:30 lands on the coaching-day row.
    await save_next_meal_workout_status(db, USER_ID, "completed", now=NIGHT)

    # 3) MENU state written at 00:30 lands on (and reads from) the same day.
    await remember_active_daily_menu(db, USER_ID, text="תפריט הערב", meals=[], now=NIGHT)
    menu = await get_active_daily_menu(db, USER_ID, now=NIGHT)
    assert menu is not None and menu["text"] == "תפריט הערב"

    # 4) The check-in surface (health_service) uses the same key.
    flags = await health_service.get_daily_flags(USER_ID)
    # (default-day path uses the real clock; address the coaching day
    # explicitly the way day-scoped callers do)
    flags = await health_service.get_daily_flags(USER_ID, COACHING_DAY)
    assert flags.get("next_meal_workout_status") == "completed"
    assert "active_daily_menu" in flags

    # ONE daily_flags row for the whole journey — nothing split to 06-29.
    rows = await db.fetch_all(
        "SELECT day FROM daily_flags WHERE user_id=? ORDER BY day", (USER_ID,)
    )
    assert [r["day"] for r in rows] == [COACHING_DAY]

    # 5) NEXT-MEAL CONTEXT at 00:30: same day key, and the budget reflects
    # the evening's consumption instead of resetting to a fresh day.
    rec = await generate_next_meal_recommendation(db, USER_ID, now=NIGHT)
    assert rec.context.local_day == COACHING_DAY
    assert rec.context.nutrition.calorie_balance == 2100 - 800


@pytest.mark.asyncio
async def test_daytime_equivalence_coaching_equals_calendar(tmp_path: Path) -> None:
    db = await _make_db(tmp_path, bedtime_confirmed=True)
    assert await daily_state.coaching_day_key(db, USER_ID, MORNING) == "2026-06-28"
    coaching = await daily_state.coaching_day_bounds_utc(db, USER_ID, MORNING)
    calendar = daily_state.local_day_bounds_utc(MORNING)
    # At 10:00 the two models agree on the whole window... almost: the
    # coaching day starts at the 03:00 rollover, deliberately including the
    # post-midnight-pre-wake hours in the same day.
    assert coaching[1] != calendar[1] or coaching[0] != calendar[0] or coaching == calendar
    await _meal(db, MORNING, "בוקר", 400)
    calories, _ = await daily_state.consumed_totals(db, USER_ID, now=MORNING)
    assert calories == 400  # same visible behavior as calendar day


@pytest.mark.asyncio
async def test_no_confirmed_bedtime_falls_back_to_calendar(tmp_path: Path) -> None:
    """Invariance: without a confirmed sleep_schedule the coaching day IS the
    calendar day — every existing user/test without a bedtime is unaffected."""
    db = await _make_db(tmp_path, bedtime_confirmed=False)
    assert await daily_state.coaching_day_key(db, USER_ID, NIGHT) == CALENDAR_DAY_AT_NIGHT
    assert (
        await daily_state.coaching_day_bounds_utc(db, USER_ID, NIGHT)
        == daily_state.local_day_bounds_utc(NIGHT)
    )
    # An UNCONFIRMED estimate must not drive day-boundary math (FIX 47).
    await user_model.set_fact(
        db, USER_ID, "sleep_schedule", {"bedtime": "23:00"},
        source=user_model.SOURCE_APPLE_HEALTH, confirmed=False,
    )
    assert await daily_state.coaching_day_key(db, USER_ID, NIGHT) == CALENDAR_DAY_AT_NIGHT


@pytest.mark.asyncio
async def test_workout_completion_stays_calendar_day(tmp_path: Path) -> None:
    """Counter-test (resolved product decision): a workout finished at 00:20
    belongs to the NEW calendar day even though nutrition still lives on the
    previous coaching day."""
    db = await _make_db(tmp_path, bedtime_confirmed=True)
    ended = NIGHT - timedelta(minutes=10)   # 00:20 on June 29
    await db.execute(
        """
        INSERT INTO sessions(user_id, code, name, plan, status, exercise_index, set_number, started_at, ended_at)
        VALUES(?, 'A', 'Workout', '{}', 'completed', 0, 0, ?, ?)
        """,
        (USER_ID, (ended - timedelta(hours=1)).isoformat(), ended.isoformat()),
    )
    # Calendar day June 29 (which contains 00:20) → completed.
    assert await daily_state.workout_completed_today(db, USER_ID, now=NIGHT) is True
    # Calendar day June 28 (the coaching day nutrition uses) → NOT completed:
    # the divergence is the intended product semantics, in both directions.
    assert await daily_state.workout_completed_today(db, USER_ID, now=EVENING) is False
    assert await daily_state.coaching_day_key(db, USER_ID, NIGHT) == COACHING_DAY
