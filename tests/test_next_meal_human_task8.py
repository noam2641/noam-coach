"""TASK-8 — "What should I eat now?" reads like a coach, not an optimizer.

No internal match-percentage scoring in the user-facing text; food is rendered
naturally (main item keeps its quantity, light side vegetables are combined
without exact grams); reasoning is practical.
"""
from __future__ import annotations

from noam_coach.services import next_meal as nm


class _Opt:
    def __init__(self, ingredients: list[str]) -> None:
        self.ingredients = ingredients


def test_natural_ingredients_combines_side_veg_without_grams() -> None:
    opt = _Opt(["קוטג' 5% 150 גרם", "מלפפון 100 גרם", "עגבנייה 120 גרם"])
    out = nm._natural_ingredients(opt)
    # Main item keeps its quantity.
    assert "קוטג' 5% 150 גרם" in out
    # Side veg combined, without their gram weights.
    assert "מלפפון" in out and "עגבנייה" in out
    assert "מלפפון 100 גרם" not in out
    assert "עגבנייה 120 גרם" not in out
    assert "+" in out  # main + sides


def test_natural_ingredients_single_main_item() -> None:
    opt = _Opt(["שייק PRO 40 330 מ\"ל"])
    out = nm._natural_ingredients(opt)
    assert "PRO 40" in out
    assert "+" not in out  # nothing to combine


def test_fit_score_label_not_shown_in_render() -> None:
    # The recommendation render must not include a match percentage.
    import inspect

    src = inspect.getsource(nm.format_next_meal_recommendation)
    assert "_fit_score_label" not in src
    assert "התאמה" not in src


def test_strip_display_quantity_is_separate_from_fingerprint_strip() -> None:
    # Regression guard: the display strip must not replace the fingerprint strip.
    assert nm._strip_display_quantity is not nm._strip_quantity
    # Fingerprint strip removes interior quantities too; display strip only the
    # trailing quantity suffix.
    assert nm._strip_display_quantity("קוטג' 5% 150 גרם") == "קוטג' 5%"
