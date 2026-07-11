"""Canonical food-environment context for nutrition planning.

The nutrition planner should not infer cooking, restaurants, delivery, or
workday food access from scattered hints. This module normalizes one
user-facing answer into a single reusable fact.
"""
from __future__ import annotations

import re
from typing import Any


HEBREW_NUMBERS = {
    "אפס": 0,
    "אחת": 1,
    "אחד": 1,
    "פעם": 1,
    "פעמיים": 2,
    "שתי": 2,
    "שניים": 2,
    "שני": 2,
    "שלוש": 3,
    "שלושה": 3,
    "ארבע": 4,
    "ארבעה": 4,
    "חמש": 5,
    "חמישה": 5,
}


def _lower_text(value: Any) -> str:
    return str(value or "").strip().lower()


def _contains(text: str, *words: str) -> bool:
    return any(word in text for word in words)


def _near_number(text: str, anchors: tuple[str, ...]) -> int | None:
    numbers: list[tuple[int, int]] = []
    for match in re.finditer(r"\b\d+\b", text):
        numbers.append((match.start(), int(match.group(0))))
    for word, value in HEBREW_NUMBERS.items():
        index = text.find(word)
        if index >= 0:
            numbers.append((index, value))
    if not numbers:
        return None
    anchor_positions = [text.find(anchor) for anchor in anchors if text.find(anchor) >= 0]
    if not anchor_positions:
        return numbers[0][1]
    anchor = min(anchor_positions)
    return min(numbers, key=lambda item: abs(item[0] - anchor))[1]


def _level_from_count(count: int | None, text: str) -> str:
    if count is not None:
        if count <= 0:
            return "none"
        if count <= 1:
            return "low"
        if count <= 3:
            return "moderate"
        return "high"
    if _contains(text, "בלי בישול", "לא מבשל", "לא מבשלת", "אין לי אפשרות לבשל"):
        return "none"
    if _contains(text, "מבשל הרבה", "אוהב לבשל", "אוהבת לבשל", "כל יום"):
        return "high"
    if _contains(text, "פעמיים", "כמה פעמים", "הכנה מראש"):
        return "moderate"
    if _contains(text, "מהיר", "בסיסי", "קצת"):
        return "low"
    return "unknown"


def normalize_food_environment_context(value: Any) -> dict[str, Any]:
    """Return a stable dict from either text or an already-normalized value."""
    if isinstance(value, dict):
        result = dict(value)
        result.setdefault("source_schema", "food_environment_v1")
        return result

    text = _lower_text(value)
    cooking_count = _near_number(text, ("בשל", "בישול", "מכין", "הכנה"))
    restaurant_count = _near_number(text, ("מסעד", "משלוח", "מזמין", "טייק"))
    no_restaurants = _contains(text, "לא מסעד", "לא מזמין", "בלי משלוח")
    restaurant_frequency = (
        "none"
        if no_restaurants
        else "high"
        if restaurant_count is not None and restaurant_count >= 3
        else "moderate"
        if restaurant_count is not None and restaurant_count >= 1
        else "high"
        if _contains(text, "הרבה במסעד", "הרבה משלוח", "כל יום מזמין")
        else "unknown"
    )
    return {
        "source_schema": "food_environment_v1",
        "raw_text": str(value or "").strip(),
        "cooking_sessions_per_week": cooking_count,
        "cooking_level": _level_from_count(cooking_count, text),
        "restaurant_frequency": restaurant_frequency,
        "delivery_or_takeaway": _contains(text, "משלוח", "מזמין", "טייק"),
        "needs_quick_meals": _contains(text, "מהיר", "מהירים", "אין זמן", "בעבודה", "יום עבודה"),
        "schedule_variability": _contains(text, "משתנה", "משתנות", "משמרות", "לא קבוע", "ספונטני"),
        "limited_food_access": _contains(text, "אין גישה", "אין אוכל מסודר", "בלי אוכל מסודר", "עמוס"),
    }


def personal_fit_signals(context: dict[str, Any] | None) -> dict[str, bool]:
    if not context:
        return {
            "low_cooking": False,
            "restaurant_or_delivery": False,
            "quick_or_limited_access": False,
            "variable_schedule": False,
            "prep_friendly": False,
        }
    cooking_level = str(context.get("cooking_level") or "unknown")
    restaurant_frequency = str(context.get("restaurant_frequency") or "unknown")
    return {
        "low_cooking": cooking_level in {"none", "low"},
        "restaurant_or_delivery": restaurant_frequency in {"moderate", "high"} or bool(context.get("delivery_or_takeaway")),
        "quick_or_limited_access": bool(context.get("needs_quick_meals") or context.get("limited_food_access")),
        "variable_schedule": bool(context.get("schedule_variability")),
        "prep_friendly": cooking_level in {"moderate", "high"},
    }
