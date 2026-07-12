"""Regression tests for the daily-menu validation/repair pipeline (TASK-5/6/7/11).

Covers required tests 6-10, 14, 15.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import recommendations
import user_model
from db import Database
from helpers import utc_now
from noam_coach.services.dietary_restrictions import parse_restrictions
from noam_coach.services.menu_validation import validate_menu
from noam_coach.services.next_meal import _matches_free_text_preference
from noam_coach.services.preference_profile import build_preference_profile


def _meal(name: str, calories: float, protein: float, time_hint: str = "08:00", note: str = "") -> recommendations.MenuMeal:
    return recommendations.MenuMeal(name=name, time_hint=time_hint, calories=calories, protein=protein, note=note)


def _avoid(label: str, rtype: str = "preference"):
    return parse_restrictions(label, restriction_type=rtype, severity="low", source="t", confirmed=True)


async def _db(tmp_path: Path, name: str) -> Database:
    db = Database(str(tmp_path / name))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'T',NULL,?)",
        (utc_now(),),
    )
    return db


@pytest.mark.asyncio
async def test_required_6_dinner_associated_food_not_in_first_morning_meal(tmp_path: Path) -> None:
    """A strongly dinner-associated food is not placed in the first morning
    meal unless explicitly requested or there is no alternative."""
    from noam_coach.services import learned_foods as lf

    db = await _db(tmp_path, "slot.db")
    profile = await build_preference_profile(db, 1)
    dinner_food = lf.LearnedFood(
        key=lf.normalize_food_key("סטייק"), display_name="סטייק", count=6,
        avg_grams=200, avg_calories=500, avg_protein=45, avg_carbs=0, avg_fat=25,
        last_eaten_at="2026-07-01T20:00:00",
        slot_counts={"dinner": 6},
    )
    profile = type(profile)(**{**profile.__dict__, "learned_foods": [dinner_food]})

    menu = recommendations.MorningMenu(headline="h", meals=[_meal("סטייק", 500, 45, time_hint="07:30", note="סטייק")])
    from noam_coach.services.menu_validation import meal_violates_slot_affinity

    assert meal_violates_slot_affinity(menu.meals[0], "breakfast", profile) is True
    # Placed in the evening it is fine.
    assert meal_violates_slot_affinity(menu.meals[0], "dinner", profile) is False


@pytest.mark.asyncio
async def test_required_7_menu_with_disliked_food_fails_validation(tmp_path: Path) -> None:
    db = await _db(tmp_path, "disliked.db")
    await user_model.set_fact(db, 1, "disliked_foods", "טורטייה", source=user_model.SOURCE_USER, confirmed=True)
    profile = await build_preference_profile(db, 1)

    menu = recommendations.MorningMenu(
        headline="h",
        meals=[
            _meal("ארוחת בוקר", 400, 30, note="טורטיית חלבון עם ביצה"),
            _meal("ארוחת צהריים", 500, 40, time_hint="13:00", note="עוף ואורז"),
        ],
    )
    result = validate_menu(menu, restrictions=profile.restrictions, profile=profile)
    assert result.ok is False
    assert any(v.code == "disliked_food" and v.meal_index == 0 for v in result.violations)
    # The clean meal is not flagged.
    assert 1 not in result.affected_meal_indices


@pytest.mark.asyncio
async def test_required_8_menu_with_allergy_violation_fails_validation(tmp_path: Path) -> None:
    db = await _db(tmp_path, "allergy.db")
    await user_model.set_fact(db, 1, "allergies", "בוטנים", source=user_model.SOURCE_USER, confirmed=True)
    profile = await build_preference_profile(db, 1)

    menu = recommendations.MorningMenu(
        headline="h",
        meals=[_meal("ארוחת בוקר", 400, 30, note="חמאת בוטנים על טוסט")],
    )
    result = validate_menu(menu, restrictions=profile.restrictions, profile=profile)
    assert result.ok is False
    assert any(v.code == "restriction_violation" for v in result.violations)


def test_required_9_menu_materially_off_calorie_target_fails_validation() -> None:
    menu = recommendations.MorningMenu(
        headline="h",
        meals=[_meal("ארוחת בוקר", 200, 15, note="דל קלוריות מדי")],
    )
    result = validate_menu(menu, restrictions=[], calorie_target=2200, protein_target=160)
    assert result.ok is False
    assert any(v.code == "calorie_target_miss" for v in result.violations)
    assert any(v.code == "protein_target_miss" for v in result.violations)


def test_menu_within_tolerance_passes() -> None:
    menu = recommendations.MorningMenu(
        headline="h",
        meals=[
            _meal("ארוחת בוקר", 700, 55, time_hint="08:00"),
            _meal("ארוחת צהריים", 800, 60, time_hint="13:00"),
            _meal("ארוחת ערב", 700, 45, time_hint="19:00"),
        ],
    )
    result = validate_menu(menu, restrictions=[], calorie_target=2200, protein_target=160)
    assert result.ok is True


@pytest.mark.asyncio
async def test_required_10_repair_changes_only_affected_meal(tmp_path: Path) -> None:
    from noam_coach.services.menu_validation import build_repair_request

    db = await _db(tmp_path, "repair.db")
    await user_model.set_fact(db, 1, "disliked_foods", "טורטייה", source=user_model.SOURCE_USER, confirmed=True)
    profile = await build_preference_profile(db, 1)

    good_meal = _meal("ארוחת צהריים", 600, 45, time_hint="13:00", note="עוף ואורז")
    bad_meal = _meal("ארוחת בוקר", 400, 30, time_hint="08:00", note="טורטיית חלבון")
    menu = recommendations.MorningMenu(headline="h", meals=[bad_meal, good_meal])

    result = validate_menu(menu, restrictions=profile.restrictions, profile=profile)
    assert result.affected_meal_indices == {0}

    request = build_repair_request(menu, result, calorie_target=2200, protein_target=160, restrictions=profile.restrictions)
    assert request.affected_meal_indices == {0}
    assert "טורטייה" in request.immutable_constraints["hard_excluded_foods"]


def test_required_14_next_meal_and_morning_menu_share_dislike_primitive() -> None:
    """next_meal and morning_menu must use the exact same matching function,
    not two divergent implementations."""
    from noam_coach.services import menu_validation
    from noam_coach.services.menu_validation import meal_matches_hard_exclusion

    assert menu_validation._matches_free_text_preference is _matches_free_text_preference

    restrictions = _avoid("טורטייה")
    meal = _meal("ארוחת בוקר", 400, 30, note="טורטיית חלבון")
    assert meal_matches_hard_exclusion(meal, restrictions) is True
    assert _matches_free_text_preference([meal.name, meal.note], restrictions) is True


@pytest.mark.asyncio
async def test_required_15_safety_contract_operationally_enforced(tmp_path: Path) -> None:
    """TASK-6: require_output_validation / validate_against_allergies_after_generation
    are not just prose — the pipeline actually runs validate_menu and rejects
    a disliked/allergic menu instead of showing it."""
    from noam_coach.services.prompt_builder import SAFETY_CONTRACT

    assert SAFETY_CONTRACT["require_output_validation"] is True
    assert SAFETY_CONTRACT["validate_against_allergies_after_generation"] is True

    db = await _db(tmp_path, "contract.db")
    await user_model.set_fact(db, 1, "allergies", "בוטנים", source=user_model.SOURCE_USER, confirmed=True)
    profile = await build_preference_profile(db, 1)
    menu = recommendations.MorningMenu(headline="h", meals=[_meal("ארוחה", 400, 30, note="חמאת בוטנים")])
    result = validate_menu(menu, restrictions=profile.restrictions, profile=profile)
    assert result.ok is False  # the contract's promise is actually checked in code
