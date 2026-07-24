"""DayPlan builder integration — build_day_plan composes ONE plan from the
already-canonical sources (nutrition context, coaching day, workout context)
against a real DB, deterministically via ``as_of``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from config import TZ
from db import Database
from helpers import utc_now
from noam_coach.services import day_plan as dp

USER_ID = 1
# Late morning, plenty of day left — the full preferred band is feasible.
AS_OF = datetime(2026, 6, 28, 10, 0, tzinfo=TZ)


async def _db(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "dpbuild.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(?, 'T', NULL, ?)",
        (USER_ID, utc_now()),
    )
    await db.execute(
        """
        INSERT INTO goal_versions(user_id, calories, protein, steps, phase, status, source, created_at)
        VALUES(?, 2400, 180, 8000, 'fat_loss_muscle_retention', 'active', 'test', ?)
        """,
        (USER_ID, utc_now()),
    )
    return db


async def _add_meal(db: Database, at: datetime, name: str, cal: int, prot: int) -> None:
    iso = at.astimezone(timezone.utc).isoformat()
    await db.execute(
        """
        INSERT INTO meals(user_id, name, calories, protein, carbs, fat, confidence, eaten_at, created_at)
        VALUES(?, ?, ?, ?, 0, 0, 0.9, ?, ?)
        """,
        (USER_ID, name, cal, prot, iso, iso),
    )


async def test_build_day_plan_explicit_preference_full_band(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    await dp.persist_preferred_meal_count(db, USER_ID, minimum=5, maximum=6)

    plan = await dp.build_day_plan(db, USER_ID, as_of=AS_OF)

    assert plan.user_id == USER_ID
    assert plan.preferred_meal_count.source == "explicit_preference"
    assert (plan.preferred_meal_count.minimum, plan.preferred_meal_count.maximum) == (5, 6)
    assert plan.selected_planned_meals == 6         # plans the stated top-of-band
    assert plan.consumed_meals == 0
    assert plan.remaining_meals == 6                # early day: whole band fits
    assert plan.as_of == AS_OF.isoformat()
    assert plan.coaching_date  # non-empty canonical key
    assert plan.confidence >= 0.9
    # explicit preference is stated in the assumptions, never silent
    assert any("מפורשת" in a for a in plan.assumptions)


async def test_build_day_plan_consumed_reduces_remaining(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    await dp.persist_preferred_meal_count(db, USER_ID, minimum=6, maximum=6)
    # two meals already eaten today (well within the coaching day at AS_OF)
    await _add_meal(db, AS_OF.replace(hour=7), "breakfast", 500, 40)
    await _add_meal(db, AS_OF.replace(hour=9), "snack", 300, 25)

    plan = await dp.build_day_plan(db, USER_ID, as_of=AS_OF)

    assert plan.consumed_meals == 2
    assert plan.selected_planned_meals == 6
    assert plan.remaining_meals == 4               # 6 − 2, still feasible at 10:00
    assert len(plan.meal_slots) == 4
    # remaining budget is read from the canonical nutrition context, not recomputed
    assert plan.remaining_calories is not None


async def test_build_day_plan_default_when_no_preference(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    plan = await dp.build_day_plan(db, USER_ID, as_of=AS_OF)
    assert plan.preferred_meal_count.source == "default"
    assert plan.requires_confirmation is True
    assert "preferred_meal_count" in plan.missing_information


async def test_build_day_plan_is_read_only_no_writes(tmp_path: Path) -> None:
    """Resolving a DayPlan must not mutate persisted state (weekly-plan today
    projection depends on this)."""
    db = await _db(tmp_path)
    await dp.persist_preferred_meal_count(db, USER_ID, minimum=5, maximum=6)

    before_meals = (await db.fetch_one(
        "SELECT COUNT(*) AS c FROM meals WHERE user_id=?", (USER_ID,)))["c"]
    before_flags = await db.fetch_one(
        "SELECT COUNT(*) AS c FROM daily_flags WHERE user_id=?", (USER_ID,))

    await dp.build_day_plan(db, USER_ID, as_of=AS_OF)

    after_meals = (await db.fetch_one(
        "SELECT COUNT(*) AS c FROM meals WHERE user_id=?", (USER_ID,)))["c"]
    after_flags = await db.fetch_one(
        "SELECT COUNT(*) AS c FROM daily_flags WHERE user_id=?", (USER_ID,))
    assert after_meals == before_meals
    assert (after_flags or {}).get("c") == (before_flags or {}).get("c")


async def test_build_day_plan_timeline_is_chronological(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    await dp.persist_preferred_meal_count(db, USER_ID, minimum=3, maximum=3)
    plan = await dp.build_day_plan(db, USER_ID, as_of=AS_OF)
    hints = [e.time_hint for e in plan.daily_timeline if e.time_hint]
    assert hints == sorted(hints)  # chronological
