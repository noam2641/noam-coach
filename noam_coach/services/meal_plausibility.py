"""Canonical quantity interpretation + nutrition plausibility (audit F-A2).

Production evidence: a reanalysis wrote a serving COUNT into the grams
field — "שניצל — 3 גרם | 8 קל׳" — and the meal persisted at ~210 kcal,
poisoning every daily total that day. Nothing in the pipeline asked
whether the numbers could describe food.

Canonical interpretation contract (enforced here, documented once):

- ``FoodItem.grams``          — the item's WEIGHT/VOLUME estimate, never a count;
- ``FoodItem.quantity_count`` — how many servings/units were seen;
- ``FoodItem.quantity_unit``  — the unit label of that count;
- ``FoodItem.quantity_source``— where the quantity came from
  (visual_count / package_label / canonical / user / estimate).

The validators below are deterministic and CONSERVATIVE: they block only
physically impossible or signature-of-confusion values, and they never
guess a correction — an implausible analysis is surfaced for the user to
fix (quantity editor / text correction), because inventing grams would
just replace one wrong meal with another.

RULES_VERSION history:
  1 — initial rules (count-vs-grams confusion, per-unit weight floor,
      protein > mass, kcal > 9.5/g, near-zero-energy solid).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

RULES_VERSION = 1

# Physically grounded bounds.
_MAX_KCAL_PER_GRAM = 9.5          # pure fat is ~9 kcal/g
_MIN_KCAL_PER_GRAM_SOLID = 0.05   # below this a "solid food" is water
_MIN_GRAMS_PER_UNIT = 2.5         # olives ~3-5 g; nothing plated is lighter
_COUNT_CONFUSION_MAX_GRAMS = 30.0  # the count→grams signature zone

_BEVERAGE_WORDS = (
    "שייק", "משקה", "שתייה", "מיץ", "קפה", "תה", "חלב", "קולה", "מים",
    "בירה", "יין", 'מ"ל', "מ״ל",
)


def is_beverage_name(name: str) -> bool:
    lowered = str(name or "")
    return any(word in lowered for word in _BEVERAGE_WORDS)


@dataclass(frozen=True)
class PlausibilityIssue:
    code: str
    severity: str  # "block" | "warn"
    message: str
    item_name: str | None = None


def check_item(item: Any) -> list[PlausibilityIssue]:
    """Deterministic plausibility of one analyzed item."""
    issues: list[PlausibilityIssue] = []
    name = str(getattr(item, "name", "") or "")
    grams = float(getattr(item, "grams", 0) or 0)
    calories = float(getattr(item, "calories", 0) or 0)
    protein = float(getattr(item, "protein", 0) or 0)
    count = getattr(item, "quantity_count", None)
    count = float(count) if count else None
    beverage = is_beverage_name(name)

    # 1) The incident signature: grams ≈ serving count (a count written
    #    into the weight field). Only fires for a real multi-unit count in
    #    the confusion zone — a genuine 3-gram ingredient has no count.
    if (
        count is not None and count >= 2
        and 0 < grams <= _COUNT_CONFUSION_MAX_GRAMS
        and grams <= count * 1.5
    ):
        issues.append(PlausibilityIssue(
            code="count_written_as_grams",
            severity="block",
            message=(
                f'הכמות של "{name}" לא הגיונית: {grams:g} גרם עבור '
                f"{count:g} יחידות — נראה שמספר היחידות נרשם כמשקל. "
                "עדכן את הכמות לפני שמירה."
            ),
            item_name=name,
        ))
        return issues  # the weight is meaningless; skip density rules

    # 2) Per-unit weight floor for counted solid items.
    if (
        count is not None and count >= 1 and grams > 0 and not beverage
        and grams / count < _MIN_GRAMS_PER_UNIT
    ):
        issues.append(PlausibilityIssue(
            code="implausible_unit_weight",
            severity="block",
            message=(
                f'המשקל של "{name}" ({grams:g} גרם ל-{count:g} יחידות) '
                "קטן מכל מזון מוגש. עדכן את הכמות לפני שמירה."
            ),
            item_name=name,
        ))

    # 3) Protein cannot exceed mass.
    if grams > 0 and protein > grams:
        issues.append(PlausibilityIssue(
            code="impossible_protein_density",
            severity="block",
            message=(
                f'"{name}": {protein:g} גרם חלבון בתוך {grams:g} גרם מזון — '
                "בלתי אפשרי פיזית. עדכן את הערכים לפני שמירה."
            ),
            item_name=name,
        ))

    # 4) Energy density cannot exceed pure fat.
    if grams >= 5 and calories > grams * _MAX_KCAL_PER_GRAM:
        issues.append(PlausibilityIssue(
            code="impossible_calorie_density",
            severity="block",
            message=(
                f'"{name}": {calories:g} קק״ל ב-{grams:g} גרם חורג מצפיפות '
                "האנרגיה של שומן טהור. עדכן את הערכים לפני שמירה."
            ),
            item_name=name,
        ))

    # 5) A named solid food with essentially zero energy is not food.
    if (
        not beverage and grams >= 30
        and calories < grams * _MIN_KCAL_PER_GRAM_SOLID
    ):
        issues.append(PlausibilityIssue(
            code="implausible_zero_energy_solid",
            severity="block",
            message=(
                f'"{name}" ({grams:g} גרם) מדווח כמעט ללא קלוריות — '
                "לא סביר עבור מזון מוצק. עדכן את הערכים לפני שמירה."
            ),
            item_name=name,
        ))
    return issues


def check_analysis(items: Iterable[Any]) -> list[PlausibilityIssue]:
    """Plausibility of the whole analysis (item rules + meal-level hints)."""
    issues: list[PlausibilityIssue] = []
    items = list(items)
    solid_items = 0
    total_calories = 0.0
    for item in items:
        issues.extend(check_item(item))
        if not is_beverage_name(str(getattr(item, "name", "") or "")):
            solid_items += 1
        total_calories += float(getattr(item, "calories", 0) or 0)
    # Meal-level sanity HINT (warn, never block): several solid items that
    # together carry less energy than a cucumber deserve a second look even
    # when each item passes individually.
    if solid_items >= 2 and total_calories < 100 and not any(
        issue.severity == "block" for issue in issues
    ):
        issues.append(PlausibilityIssue(
            code="implausibly_light_meal",
            severity="warn",
            message=(
                f"סה״כ {total_calories:g} קק״ל ל-{solid_items} פריטים מוצקים — "
                "שווה לוודא את הכמויות."
            ),
        ))
    return issues
