"""DayPlan meal-count resolution — the four quantities and their precedence.

Covers the ledger D-H regression matrix:
  1. explicit single value
  2. explicit range (5–6)
  3. learned pattern, no explicit preference
  4. default when neither exists
  5. late-day feasibility
  6. completed meals reducing remaining slots
  7. explicit preference surviving restart and readable by DayPlan
"""

from __future__ import annotations

from pathlib import Path

from db import Database
from helpers import utc_now
from noam_coach.services import day_plan as dp

USER_ID = 1


# --- band resolution (precedence: explicit > learned > default) ------------

def test_explicit_single_value_band() -> None:
    band = dp.resolve_preferred_band(explicit_fact_value={"min": 4, "max": 4},
                                     learned_meal_hours=["08:00", "13:00", "20:00"])
    assert (band.minimum, band.maximum, band.source) == (4, 4, "explicit_preference")
    assert band.is_range is False
    # learned hours (3) must NOT override the explicit preference (4)
    assert band.maximum == 4


def test_explicit_range_band() -> None:
    band = dp.resolve_preferred_band(explicit_fact_value={"min": 5, "max": 6},
                                     learned_meal_hours=["08:00", "20:00"])
    assert (band.minimum, band.maximum, band.source) == (5, 6, "explicit_preference")
    assert band.is_range is True


def test_learned_pattern_when_no_explicit() -> None:
    band = dp.resolve_preferred_band(explicit_fact_value=None,
                                     learned_meal_hours=["08:00", "12:00", "16:00", "20:00"])
    assert (band.minimum, band.maximum, band.source) == (4, 4, "learned_pattern")


def test_default_when_neither() -> None:
    band = dp.resolve_preferred_band(explicit_fact_value=None, learned_meal_hours=[])
    assert (band.minimum, band.maximum, band.source) == (
        dp.DEFAULT_PREFERRED_MIN, dp.DEFAULT_PREFERRED_MAX, "default")


def test_parse_band_accepts_range_string() -> None:
    assert dp.parse_preferred_band("5-6") == (5, 6)
    assert dp.parse_preferred_band("6") == (6, 6)
    assert dp.parse_preferred_band(6) == (6, 6)
    assert dp.parse_preferred_band({"min": 6, "max": 5}) == (5, 6)  # normalized
    assert dp.parse_preferred_band(None) is None


# --- four-quantity resolution ----------------------------------------------

def test_full_band_planned_early_day_no_consumed() -> None:
    band = dp.MealCountBand(5, 6, "explicit_preference")
    r = dp.resolve_meal_counts(band=band, consumed_meals=0, hours_until_sleep=14.0)
    assert r.selected_planned == 6            # plans the top of the stated band
    assert r.consumed == 0
    assert r.remaining == 6                    # early day: whole band is feasible
    assert any("מפורשת" in a for a in r.assumptions)


def test_late_day_feasibility_lowers_remaining_with_explicit_note() -> None:
    band = dp.MealCountBand(5, 6, "explicit_preference")
    # ~3h until sleep with 2 already eaten: fewer than the planned 4 remaining
    # are realistic, and the reduction must be stated explicitly.
    r = dp.resolve_meal_counts(band=band, consumed_meals=2, hours_until_sleep=3.0)
    assert r.selected_planned == 6
    assert r.consumed == 2
    assert r.remaining < (r.selected_planned - r.consumed)  # feasibility bit
    assert r.remaining >= 1
    # deviation from the full plan / preferred band must be stated, never silent
    assert any("שעות עד השינה" in a or "נמוך מהמינימום" in a for a in r.assumptions)


def test_very_late_day_can_go_below_preferred_minimum() -> None:
    band = dp.MealCountBand(5, 6, "explicit_preference")
    # ~1h until sleep, 4 eaten -> at most 1 remaining, below the preferred min 5
    r = dp.resolve_meal_counts(band=band, consumed_meals=4, hours_until_sleep=1.0)
    assert r.remaining <= 1
    assert any("נמוך מהמינימום" in a for a in r.assumptions)


def test_completed_meals_reduce_remaining() -> None:
    band = dp.MealCountBand(6, 6, "explicit_preference")
    r = dp.resolve_meal_counts(band=band, consumed_meals=4, hours_until_sleep=12.0)
    assert r.consumed == 4
    assert r.remaining == 2                     # 6 planned − 4 consumed
    r_all = dp.resolve_meal_counts(band=band, consumed_meals=6, hours_until_sleep=8.0)
    assert r_all.remaining == 0
    assert any("כבר אכלת" in a for a in r_all.assumptions)


def test_never_silently_falls_back_to_three() -> None:
    # A stated 6 must never be quietly turned into 3 (the old cap).
    band = dp.MealCountBand(6, 6, "explicit_preference")
    r = dp.resolve_meal_counts(band=band, consumed_meals=0, hours_until_sleep=16.0)
    assert r.selected_planned == 6
    assert r.remaining == 6


# --- persistence + restart (case 7) ----------------------------------------

async def _db(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "dayplan.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(?, 'T', NULL, ?)",
        (USER_ID, utc_now()),
    )
    return db


async def test_explicit_preference_persists_and_survives_restart(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    changed = await dp.persist_preferred_meal_count(db, USER_ID, minimum=5, maximum=6)
    assert changed is True

    # "restart": a fresh Database over the same file must read the same fact.
    db2 = Database(str(tmp_path / "dayplan.db"))
    value = await dp.read_preferred_meal_count_fact(db2, USER_ID)
    assert value == {"min": 5, "max": 6}

    band = dp.resolve_preferred_band(explicit_fact_value=value, learned_meal_hours=[])
    assert (band.minimum, band.maximum, band.source) == (5, 6, "explicit_preference")


async def test_unconfirmed_preference_is_not_decision_grade(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    import user_model

    # an UNconfirmed guess must not become authoritative
    await user_model.set_fact(
        db, USER_ID, dp.PREFERRED_MEAL_COUNT_KEY, {"min": 6, "max": 6},
        source=user_model.SOURCE_SYSTEM, confirmed=False,
    )
    assert await dp.read_preferred_meal_count_fact(db, USER_ID) is None


async def test_single_value_persists_as_equal_min_max(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    await dp.persist_preferred_meal_count(db, USER_ID, minimum=4)
    value = await dp.read_preferred_meal_count_fact(db, USER_ID)
    assert value == {"min": 4, "max": 4}
