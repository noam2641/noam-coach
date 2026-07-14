"""TASK-14 — one canonical product/food identity across meal-photo analysis.

The meal title is derived from the canonical items and cannot contradict them
(the Bamba case). A user correction updates the item identity AND the title so
all downstream representations stay consistent.
"""
from __future__ import annotations

from meal_intelligence import (
    MealCorrection,
    apply_item_removal_correction,
    apply_item_replacement_correction,
)
from models import FoodItem, MealAnalysis


def _item(name: str, **kw) -> FoodItem:
    base = dict(grams=150, calories=200, protein=20, carbs=10, fat=5, confidence=0.8)
    base.update(kw)
    return FoodItem(name=name, **base)


def test_bamba_title_cannot_contradict_item_description() -> None:
    # Title claims corn flour; item says peanut snack → title falls back to item.
    m = MealAnalysis(
        meal_name="במבה (חטיף מקמח תירס)",
        confidence=0.85,
        items=[_item("במבה (חטיף בוטנים)", grams=30, calories=160, protein=4)],
    )
    assert m.meal_name == "במבה (חטיף בוטנים)"
    assert "תירס" not in m.meal_name


def test_recognizable_product_identity_preserved() -> None:
    m = MealAnalysis(meal_name="במבה", confidence=0.85, items=[_item("במבה (חטיף בוטנים)")])
    # A recognizable product keeps its canonical item name as the title.
    assert "במבה" in m.meal_name


def test_user_replacement_updates_title_and_item() -> None:
    m = MealAnalysis(
        meal_name="קציצות בקר ואורז",
        confidence=0.8,
        items=[_item("קציצות בקר", calories=400, protein=30), _item("אורז", calories=200, protein=4)],
    )
    corrected = apply_item_replacement_correction(
        m, MealCorrection(kind="replace", item_hint="קציצות", value="חזה עוף", original_text="הקציצות היו עוף")
    )
    names = [i.name for i in corrected.items]
    assert "חזה עוף" in names
    assert "קציצות בקר" not in names
    # Title reconciled — no stale "בקר".
    assert "בקר" not in corrected.meal_name
    assert "חזה עוף" in corrected.meal_name


def test_removal_reconciles_title() -> None:
    m = MealAnalysis(
        meal_name="עוף אורז ושמן",
        confidence=0.8,
        items=[_item("חזה עוף"), _item("אורז"), _item("שמן זית", grams=10, calories=90, protein=0)],
    )
    corrected = apply_item_removal_correction(
        m, MealCorrection(kind="remove", item_hint="שמן", value=None, original_text="בלי שמן")
    )
    assert all("שמן" not in i.name for i in corrected.items)
    # Title no longer mentions the removed oil.
    assert "שמן" not in corrected.meal_name


def test_derive_title_is_the_single_canonical_source() -> None:
    from models import derive_meal_title

    assert derive_meal_title([_item("PRO 40 שייק")]) == "PRO 40 שייק"
