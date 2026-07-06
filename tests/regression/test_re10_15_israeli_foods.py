"""RE10-15 / D5 regression tests — Israeli food recognition + allergy context.

Covers:
  * israeli_foods.lookup: exact/alias matching, no false positives on
    unrelated generic terms.
  * israeli_foods.scaled_macros: per-100g values scale correctly to a
    specific gram amount.
  * profile._apply_israeli_food_overrides: a confident name match rewrites
    the item's macros; an unmatched item is left untouched.
  * profile._meal_safety_context (D5): unanswered (gap) allergy/diet facts
    are treated as "nothing to report", not leaked into the AI context.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import coach_bot
import israeli_foods
import user_model
from models import FoodItem, MealAnalysis
from noam_coach.services import profile as profile_service


async def _make_db(tmp_path: Path) -> coach_bot.Database:
    db = coach_bot.Database(str(tmp_path / "coach.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (coach_bot.utc_now(),),
    )
    return db


# ---------------------------------------------------------------------------
# israeli_foods.lookup
# ---------------------------------------------------------------------------


def test_lookup_matches_bamba_by_hebrew_name() -> None:
    match = israeli_foods.lookup("במבה")
    assert match is not None
    assert match.canonical_name == "במבה (חטיף בוטנים)"


def test_lookup_matches_bamba_inside_longer_phrase() -> None:
    match = israeli_foods.lookup("שקית במבה")
    assert match is not None
    assert match.canonical_name == "במבה (חטיף בוטנים)"


def test_lookup_matches_bamba_by_transliteration() -> None:
    match = israeli_foods.lookup("Bamba")
    assert match is not None
    assert match.canonical_name == "במבה (חטיף בוטנים)"


def test_lookup_does_not_match_generic_potato() -> None:
    assert israeli_foods.lookup("תפוחי אדמה") is None
    assert israeli_foods.lookup("חטיף תפוחי אדמה מטוגנים") is None


def test_lookup_empty_or_none_returns_none() -> None:
    assert israeli_foods.lookup("") is None
    assert israeli_foods.lookup("   ") is None


def test_lookup_prefers_longest_more_specific_alias() -> None:
    # "במבה אגוזים" is a distinct catalog entry from plain "במבה"; the
    # longest-alias-wins rule must resolve to the more specific product.
    match = israeli_foods.lookup("במבה אגוזים")
    assert match is not None
    assert match.canonical_name == "במבה אגוזים"


# ---------------------------------------------------------------------------
# israeli_foods.scaled_macros
# ---------------------------------------------------------------------------


def test_scaled_macros_25g_bamba() -> None:
    bamba = israeli_foods.lookup("במבה")
    assert bamba is not None
    scaled = israeli_foods.scaled_macros(bamba, 25)
    assert scaled["calories"] == pytest.approx(130.0, abs=0.5)
    assert scaled["protein"] == pytest.approx(3.25, abs=0.1)


def test_scaled_macros_zero_grams() -> None:
    bamba = israeli_foods.lookup("במבה")
    assert bamba is not None
    scaled = israeli_foods.scaled_macros(bamba, 0)
    assert scaled["calories"] == 0
    assert scaled["protein"] == 0


# ---------------------------------------------------------------------------
# profile._apply_israeli_food_overrides
# ---------------------------------------------------------------------------


def _analysis_with_item(name: str, grams: float, calories: float, protein: float) -> MealAnalysis:
    return MealAnalysis(
        meal_name="חטיף",
        items=[
            FoodItem(
                name=name, grams=grams, calories=calories, protein=protein,
                carbs=calories / 5, fat=1.0, confidence=0.8,
            )
        ],
        confidence=0.8,
    )


def test_apply_overrides_rewrites_matched_bamba_item() -> None:
    analysis = _analysis_with_item("חטיפי תפוחי אדמה מטוגנים", 30, 150, 2)
    # The AI returned a generic wrong name — simulate that it can still be
    # matched if the AI happens to mention "במבה" in the name; a purely wrong
    # generic name with NO reference to bamba is intentionally left alone
    # (the override only fires on a real name match, never on macros alone).
    analysis.items[0].name = "במבה"
    fixed = profile_service._apply_israeli_food_overrides(analysis)
    item = fixed.items[0]
    assert item.name == "במבה (חטיף בוטנים)"
    assert item.calories == pytest.approx(156.0, abs=1)  # 520 * 0.30
    assert item.protein == pytest.approx(3.9, abs=0.2)


def test_apply_overrides_leaves_unmatched_item_untouched() -> None:
    analysis = _analysis_with_item("סלט ירקות", 150, 60, 2)
    fixed = profile_service._apply_israeli_food_overrides(analysis)
    item = fixed.items[0]
    assert item.name == "סלט ירקות"
    assert item.calories == 60


# ---------------------------------------------------------------------------
# D5 — profile._meal_safety_context treats gaps as "nothing to report"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_meal_safety_context_none_when_no_user_id() -> None:
    context = await profile_service._meal_safety_context(None)
    assert context is None


@pytest.mark.asyncio
async def test_meal_safety_context_none_when_facts_are_gaps(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    coach_bot.DB.path = db.path
    await user_model.record_gap(db, 1, "allergies", why_matters="בטיחות תזונתית")
    await user_model.record_gap(db, 1, "diet_restrictions", why_matters="קובע אילו מאכלים להציע")

    context = await profile_service._meal_safety_context(1)
    assert context is None


@pytest.mark.asyncio
async def test_meal_safety_context_includes_real_allergy(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    coach_bot.DB.path = db.path
    await user_model.set_fact(
        db, 1, "allergies", "בוטנים",
        kind=user_model.KIND_FACT,
        source=user_model.SOURCE_USER,
        confirmed=True,
    )

    context = await profile_service._meal_safety_context(1)
    assert context is not None
    assert context["allergies"] == "בוטנים"
    assert context["diet_restrictions"] is None


def test_safety_context_block_formats_allergies() -> None:
    from noam_coach.services.meal_prompts import safety_context_block

    text = safety_context_block({"allergies": "בוטנים", "diet_restrictions": None})
    assert "בוטנים" in text
    assert "allergies" in text


def test_safety_context_block_empty_when_nothing_reported() -> None:
    from noam_coach.services.meal_prompts import safety_context_block

    assert safety_context_block(None) == ""
    assert safety_context_block({"allergies": None, "diet_restrictions": None}) == ""
