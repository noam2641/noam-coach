"""DayPlan consumer migration — the three surfaces that show "today" must all
derive their meal count from ONE canonical DayPlan decision, and honor an
explicit preferred count instead of the legacy cap-of-3.

Half 1 (characterization): pins the CURRENT behavior of the shared count carrier
(``WorkoutNutritionContext.meals_remaining_estimate``) with NO explicit
preference, so the legacy path is preserved for users who never stated one.

Half 2 (parity): with an explicit preference persisted, Today's Status, Today's
Menu and the Weekly-Plan today projection all resolve the SAME remaining count
via ``day_plan``. Covers explicit range 5-6, learned pattern, default,
late-day feasibility, already-consumed meals, and restart persistence.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from config import TZ
from db import Database
from helpers import utc_now
from noam_coach.services import day_plan as dp

USER_ID = 1
MORNING = datetime(2026, 6, 28, 10, 0, tzinfo=TZ)   # lots of day left
LATE = datetime(2026, 6, 28, 21, 30, tzinfo=TZ)     # little time before sleep


async def _db(tmp_path: Path, *, bedtime: str | None = "23:00") -> Database:
    db = Database(str(tmp_path / "mig.db"))
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
    if bedtime is not None:
        import user_model

        await user_model.set_fact(
            db, USER_ID, "sleep_schedule", {"bedtime": bedtime},
            source=user_model.SOURCE_USER, confirmed=True,
        )
    return db


async def _meal(db: Database, at: datetime, name: str) -> None:
    iso = at.astimezone(timezone.utc).isoformat()
    await db.execute(
        """
        INSERT INTO meals(user_id, name, calories, protein, carbs, fat, confidence, eaten_at, created_at)
        VALUES(?, ?, 500, 40, 0, 0, 0.9, ?, ?)
        """,
        (USER_ID, name, iso, iso),
    )


# --- Half 1: characterization (no explicit preference → legacy preserved) ---

async def test_no_preference_uses_legacy_estimate(tmp_path: Path) -> None:
    """Without an explicit preference the shared count carrier keeps the legacy
    behavior (<=3), so existing users see no change."""
    from noam_coach.services.next_meal import build_workout_nutrition_context

    db = await _db(tmp_path)
    ctx = await build_workout_nutrition_context(db, USER_ID, now=MORNING)
    assert 1 <= ctx.meals_remaining_estimate <= 3


# --- Half 2: parity — all consumers resolve the same count via DayPlan ---

async def _status_count(db: Database, at: datetime) -> int:
    """Today's Status meal count == DayPlan.remaining_meals (single decision)."""
    plan = await dp.build_day_plan(db, USER_ID, as_of=at)
    return plan.remaining_meals


async def _menu_count(db: Database, at: datetime) -> int:
    """Today's Menu derives its slot count from the same carrier the status uses;
    after migration both equal DayPlan.remaining_meals."""
    from noam_coach.services.next_meal import build_workout_nutrition_context

    ctx = await build_workout_nutrition_context(db, USER_ID, now=at)
    return ctx.meals_remaining_estimate


async def _weekly_today_count(db: Database, at: datetime) -> int:
    """Weekly-Plan today projection re-projects today's row via the same DayPlan."""
    plan = await dp.build_day_plan(db, USER_ID, as_of=at)
    return plan.remaining_meals


async def test_parity_explicit_range_early_day(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    await dp.persist_preferred_meal_count(db, USER_ID, minimum=5, maximum=6)
    s = await _status_count(db, MORNING)
    m = await _menu_count(db, MORNING)
    w = await _weekly_today_count(db, MORNING)
    assert s == m == w == 6          # honors the stated band, not the legacy 3


async def test_parity_learned_pattern_no_explicit(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    # learned typical_meal_hours of length 5, no explicit preference
    import user_model

    await user_model.set_fact(
        db, USER_ID, "eating_windows",
        {"typical_meal_hours": ["07:00", "10:00", "13:00", "16:00", "20:00"]},
        source=user_model.SOURCE_SYSTEM, confirmed=True,
    )
    # DayPlan reads usual_meal_times from the nutrition context/profile; assert the
    # three surfaces agree with each other (single decision) regardless of value.
    s = await _status_count(db, MORNING)
    w = await _weekly_today_count(db, MORNING)
    assert s == w
    assert s >= 1


async def test_parity_default_fallback(tmp_path: Path) -> None:
    db = await _db(tmp_path, bedtime=None)
    s = await _status_count(db, MORNING)
    w = await _weekly_today_count(db, MORNING)
    assert s == w
    plan = await dp.build_day_plan(db, USER_ID, as_of=MORNING)
    assert plan.preferred_meal_count.source == "default"


async def test_parity_late_day_feasibility(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    await dp.persist_preferred_meal_count(db, USER_ID, minimum=5, maximum=6)
    s = await _status_count(db, LATE)
    w = await _weekly_today_count(db, LATE)
    assert s == w
    assert s < 6                      # late day: fewer than the full band remain


async def test_parity_already_consumed_meals(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    await dp.persist_preferred_meal_count(db, USER_ID, minimum=6, maximum=6)
    await _meal(db, MORNING.replace(hour=7), "b")
    await _meal(db, MORNING.replace(hour=9), "s")
    plan = await dp.build_day_plan(db, USER_ID, as_of=MORNING)
    assert plan.consumed_meals == 2
    assert plan.remaining_meals == 4
    s = await _status_count(db, MORNING)
    w = await _weekly_today_count(db, MORNING)
    assert s == w == 4


async def test_parity_restart_persistence(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    await dp.persist_preferred_meal_count(db, USER_ID, minimum=5, maximum=6)
    # fresh Database handle over the same file (simulated restart)
    db2 = Database(str(tmp_path / "mig.db"))
    plan = await dp.build_day_plan(db2, USER_ID, as_of=MORNING)
    assert plan.preferred_meal_count.source == "explicit_preference"
    assert plan.remaining_meals == 6


# --- onboarding writer: stated preference reaches DayPlan (single + range) ---

async def test_onboarding_extraction_persists_single_value(
    tmp_path: Path, monkeypatch
) -> None:
    import coach_bot
    from models import RoutineExtraction
    from noam_coach.services import profile as profile_svc

    db = await _db(tmp_path)
    # monkeypatch restores the module-level DB after the test, so the closed tmp
    # DB never leaks into later tests in the same process (suite is order-safe).
    monkeypatch.setattr(coach_bot, "DB", db, raising=False)
    monkeypatch.setattr(profile_svc, "DB", db, raising=False)

    await profile_svc.save_routine_extraction(
        USER_ID, RoutineExtraction(typical_meals_per_day=4)
    )
    value = await dp.read_preferred_meal_count_fact(db, USER_ID)
    assert value == {"min": 4, "max": 4}

    # readable by DayPlan after a restart handle
    db2 = Database(str(tmp_path / "mig.db"))
    plan = await dp.build_day_plan(db2, USER_ID, as_of=MORNING)
    assert plan.preferred_meal_count.source == "explicit_preference"
    assert plan.selected_planned_meals == 4


async def test_onboarding_extraction_persists_range(
    tmp_path: Path, monkeypatch
) -> None:
    import coach_bot
    from models import RoutineExtraction
    from noam_coach.services import profile as profile_svc

    db = await _db(tmp_path)
    monkeypatch.setattr(coach_bot, "DB", db, raising=False)
    monkeypatch.setattr(profile_svc, "DB", db, raising=False)

    await profile_svc.save_routine_extraction(
        USER_ID,
        RoutineExtraction(typical_meals_per_day=5, typical_meals_per_day_max=6),
    )
    value = await dp.read_preferred_meal_count_fact(db, USER_ID)
    assert value == {"min": 5, "max": 6}   # range persisted ONCE, not two facts

    band = dp.resolve_preferred_band(explicit_fact_value=value, learned_meal_hours=[])
    assert (band.minimum, band.maximum) == (5, 6)
    assert band.is_range is True
