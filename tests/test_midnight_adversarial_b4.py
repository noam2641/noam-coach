"""B4 / ARCH-02 — adversarial midnight verification of the B3 migration.

DAY-SEMANTICS MATRIX (verified classification at this batch):
- coaching day (nutrition): meal windows, daily_flags day key, daily menu
  day (+ menu-edit memory, fixed here), next-meal context/scales/
  rejections/recent-titles, workout-status clarification flag, nutrition
  quality gating of proactive sends, check-in flags.
- calendar day (intentional): workout completion evidence + split
  selector, weekly summary aggregation, proactive job_state send budget,
  routine pattern learning, Health-import date attribution
  (local_day_str / measured_at day / is_today_activity_available),
  weekly-summary job gate.
- UTC/storage: meals.eaten_at, health sample timestamps, event
  created_at (uniformly utc_now()/UTC isoformat — verified).
- historical date: weekly charts, newest-import display.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import coach_bot
import health_service
import mini_api
import user_model
from config import TZ
from db import Database
from helpers import utc_now
from noam_coach.services import daily_state
from noam_coach.services.next_meal import save_next_meal_workout_status
from noam_coach.services.user_state import build_shared_state, resolve_workout_state

USER_ID = 1
NIGHT = datetime(2026, 6, 29, 0, 30, tzinfo=TZ)     # post-midnight, pre-rollover
LATE_EVENING = datetime(2026, 6, 28, 23, 50, tzinfo=TZ)
COACHING_DAY = "2026-06-28"


async def _make_db(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "b4.db"))
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
    await user_model.set_fact(
        db, USER_ID, "sleep_schedule", {"bedtime": "23:00"},
        source=user_model.SOURCE_USER, confirmed=True,
    )
    return db


@pytest.mark.asyncio
async def test_pre_midnight_clarification_visible_after_midnight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE continuity payoff: 'סיימתי אימון' tapped at 23:50 must still be
    the coach's understanding at 00:30 — before B3, the flag lived on the
    June-28 calendar row while the 00:30 read looked at June 29."""
    db = await _make_db(tmp_path)
    await save_next_meal_workout_status(db, USER_ID, "completed", now=LATE_EVENING)

    state = await resolve_workout_state(db, USER_ID, NIGHT)
    assert state.source == "user_clarification"
    shared = await build_shared_state(db, USER_ID, now=NIGHT)
    assert shared.local_day == COACHING_DAY


@pytest.mark.asyncio
async def test_mini_app_write_cannot_split_the_canonical_coaching_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ARCH-07A shared-state safety (the only Mini App scope of this batch):
    the Mini App workout-status mutation goes through the SAME
    save_next_meal_workout_status boundary, so a post-midnight Mini App
    write lands on the canonical coaching-day row Telegram reads — one row,
    no split, no corruption of Telegram-visible state."""
    db = await _make_db(tmp_path)
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(mini_api, "DB", db)
    from config import SETTINGS

    monkeypatch.setattr(SETTINGS, "telegram_allowed_user_id", USER_ID, raising=False)

    # Telegram wrote a flag on the coaching day in the evening...
    await save_next_meal_workout_status(db, USER_ID, "later", now=LATE_EVENING)

    # ...and the Mini App endpoint mutates at 00:30 (its internal "now" is
    # the real clock; pin the day derivation to the fixed night instant the
    # same way the endpoint's own now-resolution would see it).
    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: D401
            return NIGHT.astimezone(tz) if tz else NIGHT.replace(tzinfo=None)

    from noam_coach.services import next_meal as next_meal_module

    monkeypatch.setattr(next_meal_module, "datetime", _FrozenDatetime)
    # The endpoint resolves its own "now" once and threads it through.
    monkeypatch.setattr(mini_api, "datetime", _FrozenDatetime)
    response = await mini_api.mini_next_meal_workout_status(
        payload={"status": "completed"}, user_id=USER_ID,
    )
    assert response.status_code == 200

    rows = await db.fetch_all(
        "SELECT day FROM daily_flags WHERE user_id=? ORDER BY day", (USER_ID,)
    )
    assert [r["day"] for r in rows] == [COACHING_DAY]  # ONE canonical row
    flags = await health_service.get_daily_flags(USER_ID, COACHING_DAY)
    assert flags["next_meal_workout_status"] == "completed"
    # And Telegram's post-midnight read sees the Mini App's update.
    state = await resolve_workout_state(db, USER_ID, NIGHT)
    assert state.source == "user_clarification"


@pytest.mark.asyncio
async def test_menu_edit_memory_lands_on_coaching_day(tmp_path: Path) -> None:
    """The B4 sweep's one misclassification, fixed: menu-edit memory is
    nutrition state and must key on the coaching day."""
    db = await _make_db(tmp_path)
    from noam_coach.services import daily_menu_edit as dme

    class _Intent:
        slot = "snack"
        instruction = "בלי טונה"

    class _FrozenDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return NIGHT.astimezone(tz) if tz else NIGHT.replace(tzinfo=None)

    import pytest as _pytest  # noqa: F401

    orig_dt = dme.datetime
    dme.datetime = _FrozenDT
    try:
        await dme._remember_request(db, USER_ID, _Intent())
    finally:
        dme.datetime = orig_dt

    rows = await db.fetch_all("SELECT day FROM daily_flags WHERE user_id=?", (USER_ID,))
    assert [r["day"] for r in rows] == [COACHING_DAY]


@pytest.mark.asyncio
async def test_evening_and_night_meals_one_budget_next_meal_not_reset(tmp_path: Path) -> None:
    """Adversarial re-check of the whole nutrition read path at 00:30: the
    next-meal budget must reflect the evening's eating (not a fresh day)."""
    db = await _make_db(tmp_path)
    for at, cal in ((datetime(2026, 6, 28, 20, 0, tzinfo=TZ), 700),
                    (datetime(2026, 6, 29, 0, 15, tzinfo=TZ), 300)):
        await db.execute(
            """
            INSERT INTO meals(user_id, name, calories, protein, carbs, fat, confidence, eaten_at, created_at)
            VALUES(?, 'm', ?, 30, 0, 0, 1, ?, ?)
            """,
            (USER_ID, cal, at.astimezone(timezone.utc).isoformat(), utc_now()),
        )
    from noam_coach.services.next_meal import generate_next_meal_recommendation

    rec = await generate_next_meal_recommendation(db, USER_ID, now=NIGHT)
    assert rec.context.local_day == COACHING_DAY
    assert rec.context.nutrition.calorie_balance == 2100 - 1000


@pytest.mark.asyncio
async def test_calendar_paths_unchanged_by_the_migration(tmp_path: Path) -> None:
    """Workout evidence and Health 'today' attribution stay calendar."""
    db = await _make_db(tmp_path)
    ended = NIGHT - timedelta(minutes=5)
    await db.execute(
        """
        INSERT INTO sessions(user_id, code, name, plan, status, exercise_index, set_number, started_at, ended_at)
        VALUES(?, 'A', 'W', '{}', 'completed', 0, 0, ?, ?)
        """,
        (USER_ID, (ended - timedelta(hours=1)).isoformat(), ended.isoformat()),
    )
    assert await daily_state.workout_completed_today(db, USER_ID, now=NIGHT) is True
    assert await daily_state.coaching_day_key(db, USER_ID, NIGHT) == COACHING_DAY
    # Calendar bounds remain available and calendar-shaped.
    start, _end = daily_state.local_day_bounds_utc(NIGHT)
    assert start.startswith("2026-06-28T21:00:00")  # June 29 00:00 +03:00 in UTC
