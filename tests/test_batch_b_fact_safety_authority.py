"""Batch B (FIX 47, 55): explicit fact-read policy and final-action safety
revalidation for next-meal saves.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import coach_bot
import user_model
from noam_coach.services.next_meal import MealOption, MealSafetyRejected, save_chosen_meal


async def _make_db(tmp_path: Path) -> coach_bot.Database:
    db = coach_bot.Database(str(tmp_path / "coach.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (coach_bot.utc_now(),),
    )
    return db


# ---------------------------------------------------------------------------
# FIX 47: get_decision_value vs get_value policy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_decision_value_rejects_unconfirmed_estimate(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)

    await user_model.set_fact(
        db, 1, "avg_steps", 8000,
        kind=user_model.KIND_ESTIMATE, source=user_model.SOURCE_DERIVED,
        confidence=0.6, confirmed=False,
    )
    # get_value still returns it (display/draft use is legitimate)
    assert await user_model.get_value(db, 1, "avg_steps") == 8000
    # get_decision_value must not treat it as authoritative
    assert await user_model.get_decision_value(db, 1, "avg_steps") is None


@pytest.mark.asyncio
async def test_get_decision_value_accepts_confirmed_fact(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)

    await user_model.set_fact(
        db, 1, "primary_goal", "fat_loss",
        kind=user_model.KIND_FACT, source=user_model.SOURCE_USER, confirmed=True,
    )
    assert await user_model.get_decision_value(db, 1, "primary_goal") == "fat_loss"


@pytest.mark.asyncio
async def test_get_raw_fact_exposes_full_row_regardless_of_confirmation(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)

    await user_model.set_fact(
        db, 1, "avg_steps", 8000,
        kind=user_model.KIND_ESTIMATE, source=user_model.SOURCE_DERIVED,
        confidence=0.6, confirmed=False,
    )
    raw = await user_model.get_raw_fact(db, 1, "avg_steps")
    assert raw is not None
    assert raw["value"] == 8000
    assert raw["confirmed"] is False


# ---------------------------------------------------------------------------
# FIX 55: save_chosen_meal revalidates current safety facts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_chosen_meal_rejects_option_matching_new_allergy(tmp_path: Path) -> None:
    """The exact FIX 55 scenario: allergy added AFTER the option was
    generated (restriction_validated=True at generation time), then the
    stale save button is pressed."""
    db = await _make_db(tmp_path)
    coach_bot.DB.path = db.path

    option = MealOption(
        title="סלט עם אגוזים",
        ingredients=["אגוזים", "חסה"],
        calories=400,
        protein=10,
        rationale="test",
        restriction_validated=True,  # was safe when generated
    )

    # Allergy reported AFTER generation, before the save tap.
    await user_model.set_fact(
        db, 1, "allergies", "אגוזים",
        kind=user_model.KIND_FACT, source=user_model.SOURCE_USER, confirmed=True,
    )

    with pytest.raises(MealSafetyRejected):
        await save_chosen_meal(db, 1, option)

    # And the meal must not have been persisted.
    count = await db.fetch_one("SELECT COUNT(*) AS c FROM meals WHERE user_id=1")
    assert int((count or {}).get("c") or 0) == 0


@pytest.mark.asyncio
async def test_save_chosen_meal_succeeds_when_no_conflicting_safety_fact(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    coach_bot.DB.path = db.path

    option = MealOption(
        title="חזה עוף עם אורז",
        ingredients=["חזה עוף", "אורז"],
        calories=500,
        protein=40,
        rationale="test",
    )
    saved = await save_chosen_meal(db, 1, option)
    assert saved is True

    count = await db.fetch_one("SELECT COUNT(*) AS c FROM meals WHERE user_id=1")
    assert int((count or {}).get("c") or 0) == 1


@pytest.mark.asyncio
async def test_save_chosen_meal_allows_non_blocking_avoidance(tmp_path: Path) -> None:
    """A 'warn'-level avoidance (not an allergy) must not block the save --
    only allergy/sensitivity/intolerance are block-severity."""
    db = await _make_db(tmp_path)
    coach_bot.DB.path = db.path

    option = MealOption(
        title="פסטה",
        ingredients=["פסטה"],
        calories=500,
        protein=15,
        rationale="test",
    )
    await user_model.set_fact(
        db, 1, "diet_restrictions", "גלוטן",
        kind=user_model.KIND_FACT, source=user_model.SOURCE_USER, confirmed=True,
    )
    # "פסטה" doesn't match a gluten alias in this simplified case, so this
    # mainly proves the gate doesn't false-positive-block unrelated items.
    saved = await save_chosen_meal(db, 1, option)
    assert saved is True
