"""Regression tests for the nutrition personalization redesign (TASK-1/2/3/4).

Covers required tests 1-5 from the personalization/menu-generation hardening
spec: explicit dislike as hard exclusion (generalized, not tortilla-specific
in production code), explicit dislike overriding historical consumption,
one-off foods not becoming personalization evidence, familiarity vs explicit
preference being distinct semantic categories, and ambiguous slot
distributions not producing a false-confidence dominant slot.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

import user_model
from config import TZ
from db import Database
from helpers import utc_now
from noam_coach.services import learned_foods as lf
from noam_coach.services.preference_profile import (
    FAMILIARITY_MIN_COUNT,
    POLARITY_FAMILIAR,
    POLARITY_HARD_EXCLUDE,
    POLARITY_PREFERRED,
    build_preference_profile,
)


async def _db(tmp_path: Path, name: str) -> Database:
    db = Database(str(tmp_path / name))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'T',NULL,?)",
        (utc_now(),),
    )
    return db


async def _log_meal_item(db: Database, name: str, *, days_ago: int, hour: int = 13) -> None:
    when = (datetime.now(TZ) - timedelta(days=days_ago)).replace(hour=hour, minute=0, second=0, microsecond=0)
    eaten = when.astimezone().isoformat()
    meal_id = await db.execute(
        "INSERT INTO meals(user_id, name, calories, protein, carbs, fat, confidence, eaten_at, created_at) "
        "VALUES(1,?,300,20,30,10,0.9,?,?)",
        (name, eaten, utc_now()),
    )
    await db.execute(
        "INSERT INTO meal_items(meal_id, name, grams, calories, protein, carbs, fat, confidence) "
        "VALUES(?,?,150,300,20,30,10,0.9)",
        (meal_id, name),
    )


@pytest.mark.asyncio
async def test_required_1_explicit_dislike_is_hard_exclusion_generalized(tmp_path: Path) -> None:
    """A generic Hebrew food word is used here, not hardcoded in production
    code — production matching (``_hebrew_stem``/``_food_word_matches``) is
    morphology-generic; only this test names a concrete food."""
    db = await _db(tmp_path, "dislike.db")
    await user_model.set_fact(
        db, 1, "disliked_foods", "טורטייה", source=user_model.SOURCE_USER, confirmed=True,
    )
    profile = await build_preference_profile(db, 1)
    assert profile.is_hard_excluded("טורטיית חלבון") is True
    assert profile.is_hard_excluded("טורטייה") is True
    assert profile.is_hard_excluded("חזה עוף ואורז") is False


@pytest.mark.asyncio
async def test_required_2_later_explicit_dislike_overrides_20_historical_occurrences(tmp_path: Path) -> None:
    db = await _db(tmp_path, "override.db")
    for day in range(1, 21):
        await _log_meal_item(db, "טורטיית חלבון", days_ago=day)
    # A later explicit dislike statement is recorded (most recent source of truth).
    await user_model.set_fact(
        db, 1, "disliked_foods", "טורטייה", source=user_model.SOURCE_USER, confirmed=True,
    )

    profile = await build_preference_profile(db, 1)

    assert profile.is_hard_excluded("טורטיית חלבון") is True
    # It must not also appear as familiar/positive-evidence food despite 20
    # historical occurrences (Task 1 precedence rule).
    assert all(signal.canonical_key != lf.normalize_food_key("טורטיית חלבון") for signal in profile.familiar_foods)
    bonus, _reason = profile.preference_bonus("טורטיית חלבון")
    assert bonus == 0.0


@pytest.mark.asyncio
async def test_required_3_one_off_food_is_not_strong_personalization_evidence(tmp_path: Path) -> None:
    db = await _db(tmp_path, "oneoff.db")
    await _log_meal_item(db, "קינואה עם ירקות", days_ago=2)

    profile = await build_preference_profile(db, 1)

    assert profile.familiar_foods == []
    bonus, _reason = profile.preference_bonus("קינואה עם ירקות")
    assert bonus == 0.0


@pytest.mark.asyncio
async def test_required_4_familiarity_and_explicit_preference_are_distinct_categories(tmp_path: Path) -> None:
    db = await _db(tmp_path, "distinct.db")
    for day in range(1, 4):
        await _log_meal_item(db, "עוף ואורז", days_ago=day)
    await user_model.set_fact(
        db, 1, "preferred_foods", "סלמון", source=user_model.SOURCE_USER, confirmed=True,
    )

    profile = await build_preference_profile(db, 1)

    familiar_keys = {s.canonical_key for s in profile.familiar_foods}
    liked_keys = {s.canonical_key for s in profile.explicit_likes}
    assert lf.normalize_food_key("עוף ואורז") in familiar_keys
    assert lf.normalize_food_key("סלמון") in liked_keys
    # The categories are disjoint and carry different polarity/strength.
    assert familiar_keys.isdisjoint(liked_keys)
    familiar_signal = next(s for s in profile.familiar_foods if s.canonical_key == lf.normalize_food_key("עוף ואורז"))
    liked_signal = next(s for s in profile.explicit_likes if s.canonical_key == lf.normalize_food_key("סלמון"))
    assert familiar_signal.polarity == POLARITY_FAMILIAR
    assert liked_signal.polarity == POLARITY_PREFERRED
    # Familiarity strength is capped strictly below any explicit strength.
    assert familiar_signal.strength < liked_signal.strength


@pytest.mark.asyncio
async def test_required_4b_hard_exclusion_is_its_own_category(tmp_path: Path) -> None:
    db = await _db(tmp_path, "hardcat.db")
    await user_model.set_fact(
        db, 1, "disliked_foods", "בוטנים", source=user_model.SOURCE_USER, confirmed=True,
    )
    profile = await build_preference_profile(db, 1)
    assert any(s.polarity == POLARITY_HARD_EXCLUDE for s in profile.hard_exclusions)
    assert FAMILIARITY_MIN_COUNT == 2


def test_required_5_ambiguous_slot_distribution_has_no_high_confidence_dominant_slot() -> None:
    food = lf.LearnedFood(
        key="chicken", display_name="עוף", count=5,
        avg_grams=150, avg_calories=300, avg_protein=30, avg_carbs=0, avg_fat=5,
        last_eaten_at="2026-07-01T12:00:00",
        slot_counts={"breakfast": 2, "lunch": 2, "dinner": 1},
    )
    assert food.dominant_slot is None
    assert food.dominant_slot_confidence == "none"


def test_required_5b_strong_evidence_still_yields_confident_dominant_slot() -> None:
    food = lf.LearnedFood(
        key="oat", display_name="שיבולת שועל", count=5,
        avg_grams=60, avg_calories=220, avg_protein=8, avg_carbs=40, avg_fat=4,
        last_eaten_at="2026-07-01T08:00:00",
        slot_counts={"breakfast": 4, "afternoon": 1},
    )
    assert food.dominant_slot == "breakfast"
    assert food.dominant_slot_confidence == "strong"


def test_dominant_slot_confidence_weak_below_count_threshold() -> None:
    food = lf.LearnedFood(
        key="x", display_name="x", count=2,
        avg_grams=100, avg_calories=200, avg_protein=10, avg_carbs=20, avg_fat=5,
        last_eaten_at="2026-07-01T08:00:00",
        slot_counts={"breakfast": 2},
    )
    assert food.dominant_slot is None
    assert food.dominant_slot_confidence == "weak"


def test_dominant_slot_confidence_moderate() -> None:
    food = lf.LearnedFood(
        key="x", display_name="x", count=5,
        avg_grams=100, avg_calories=200, avg_protein=10, avg_carbs=20, avg_fat=5,
        last_eaten_at="2026-07-01T08:00:00",
        slot_counts={"lunch": 3, "dinner": 2},
    )
    assert food.dominant_slot == "lunch"
    assert food.dominant_slot_confidence == "moderate"
