from __future__ import annotations

import meal_intelligence
from models import FoodItem, MealAnalysis


def _analysis() -> MealAnalysis:
    return MealAnalysis(
        meal_name="צלחת",
        confidence=0.8,
        items=[
            FoodItem(
                name="אורז לבן מבושל",
                grams=120,
                calories=156,
                protein=3,
                carbs=34,
                fat=0.4,
                confidence=0.8,
            )
        ],
    )


def _analysis_with_oil() -> MealAnalysis:
    """Analysis that includes an oil line item alongside rice."""
    return MealAnalysis(
        meal_name="סלט",
        confidence=0.8,
        items=[
            FoodItem(
                name="אורז לבן מבושל",
                grams=120,
                calories=156,
                protein=3,
                carbs=34,
                fat=0.4,
                confidence=0.8,
            ),
            FoodItem(
                name="שמן זית",
                grams=10,
                calories=88,
                protein=0,
                carbs=0,
                fat=10,
                confidence=0.7,
            ),
        ],
    )


def test_explicit_quantity_is_a_hard_constraint() -> None:
    constraints = meal_intelligence.parse_locked_quantities("אורז 150 גרם")
    corrected, unmatched = meal_intelligence.apply_locked_quantities(_analysis(), constraints)
    assert not unmatched
    assert corrected.items[0].grams == 150
    assert corrected.items[0].confidence >= 0.95


def test_sensitive_starch_requires_cooked_or_raw_clarification() -> None:
    constraints = meal_intelligence.parse_locked_quantities("150 גרם אורז")
    assert meal_intelligence.requires_cooked_raw_clarification(constraints)
    cooked = meal_intelligence.parse_locked_quantities("150 גרם אורז אחרי בישול")
    assert not meal_intelligence.requires_cooked_raw_clarification(cooked)


def test_hamming_distance_for_identical_hashes_is_zero() -> None:
    assert meal_intelligence.hamming_distance_hex("0f" * 8, "0f" * 8) == 0


# ---------------------------------------------------------------------------
# REC-PLAN-MEAL-03-12: Oil-removal correction tests
# ---------------------------------------------------------------------------

def test_bli_shemen_parsed_as_removal() -> None:
    """'בלי שמן' must produce a 'remove' correction, not a preparation change."""
    corrections = meal_intelligence.parse_meal_correction("בלי שמן")
    assert len(corrections) == 1
    assert corrections[0].kind == "remove"
    assert corrections[0].item_hint == "שמן"


def test_llal_shemen_parsed_as_removal() -> None:
    """'ללא שמן' must produce a 'remove' correction."""
    corrections = meal_intelligence.parse_meal_correction("ללא שמן")
    assert len(corrections) == 1
    assert corrections[0].kind == "remove"
    assert corrections[0].item_hint == "שמן"


def test_bli_shemen_zait_parsed_as_removal() -> None:
    """'בלי שמן זית' must produce a 'remove' correction."""
    corrections = meal_intelligence.parse_meal_correction("בלי שמן זית")
    assert len(corrections) == 1
    assert corrections[0].kind == "remove"
    assert corrections[0].item_hint == "שמן"


def test_apply_item_removal_removes_oil() -> None:
    """Applying the removal correction must drop the oil item from the analysis."""
    analysis = _analysis_with_oil()
    assert len(analysis.items) == 2

    corrections = meal_intelligence.parse_meal_correction("בלי שמן")
    assert corrections[0].kind == "remove"

    result = meal_intelligence.apply_item_removal_correction(analysis, corrections[0])
    assert len(result.items) == 1
    assert result.items[0].name == "אורז לבן מבושל"
    # A note should record the removal
    assert any("שמן" in note for note in result.notes)


def test_apply_item_removal_no_match_leaves_analysis_unchanged() -> None:
    """If the hinted item is not in the analysis, the analysis must not change."""
    analysis = _analysis()  # no oil item
    original_items = list(analysis.items)

    correction = meal_intelligence.MealCorrection(
        kind="remove", item_hint="שמן", value="", original_text="בלי שמן"
    )
    result = meal_intelligence.apply_item_removal_correction(analysis, correction)
    # Items list must be unchanged
    assert result.items == original_items


def test_removal_takes_priority_over_preparation_in_parser() -> None:
    """parse_meal_correction must return only removal when the text is 'בלי שמן',
    not a preparation correction."""
    corrections = meal_intelligence.parse_meal_correction("בלי שמן")
    kinds = {c.kind for c in corrections}
    assert kinds == {"remove"}
    assert "preparation" not in kinds
