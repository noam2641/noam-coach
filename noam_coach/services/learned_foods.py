"""User-learned foods/products from approved meal history.

The bot should not treat every recommendation as if it starts from a blank
catalog.  Once the user repeatedly uploads or approves meals, those item names
and their observed nutrition values become useful context for:

* photo/text calorie estimation (recognise likely products/foods), and
* future next-meal / daily-menu recommendations (prefer familiar items when
  they still fit the nutrition budget and restrictions).

This module deliberately derives that knowledge from the existing durable
``meals``/``meal_items`` tables.  No migration is needed and only approved
(consumed) meals are used.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from config import TZ

# TASK-5: coarse meal slots derived from the local hour a meal was eaten, used to
# rank learned foods by relevance to the current meal slot.
MEAL_SLOT_LABELS_HE: dict[str, str] = {
    "breakfast": "ארוחת בוקר",
    "lunch": "ארוחת צהריים",
    "afternoon": "ארוחת ביניים",
    "dinner": "ארוחת ערב",
    "late": "ארוחת לילה",
}


def meal_slot_for_hour(hour: int) -> str:
    """Map a local hour (0-23) to a coarse meal slot."""
    if 5 <= hour < 11:
        return "breakfast"
    if 11 <= hour < 15:
        return "lunch"
    if 15 <= hour < 18:
        return "afternoon"
    if 18 <= hour < 22:
        return "dinner"
    return "late"


def _meal_slot_for_timestamp(eaten_at: str) -> str | None:
    try:
        dt = datetime.fromisoformat(str(eaten_at))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(TZ)
    return meal_slot_for_hour(dt.hour)


_NORMALIZE_RE = re.compile(r"[\s\-_/|,.;:()\[\]{}]+")
_COOKED_MARKERS = ("מבושל", "מבושלת", "מוכן", "מוכנה")
_GENERIC_ITEMS = {
    "ירקות",
    "סלט",
    "מים",
    "שתיה",
    "רוטב",
    "תוספת",
    "ארוחה",
    "מנה",
}


@dataclass(frozen=True)
class LearnedFood:
    key: str
    display_name: str
    count: int
    avg_grams: float
    avg_calories: float
    avg_protein: float
    avg_carbs: float
    avg_fat: float
    last_eaten_at: str
    # TASK-5: how often this food was eaten in each meal slot, so menu generation
    # can rank a learned food by relevance to the current meal slot instead of
    # treating learned foods as one flat list.
    slot_counts: dict[str, int] = field(default_factory=dict)

    @property
    def dominant_slot(self) -> str | None:
        """The meal slot this food is most associated with, or None if unclear."""
        if not self.slot_counts:
            return None
        slot, count = max(self.slot_counts.items(), key=lambda kv: kv[1])
        return slot if count > 0 else None

    def slot_relevance(self, slot: str) -> float:
        """Fraction of this food's occurrences that fell in ``slot`` (0..1)."""
        total = sum(self.slot_counts.values())
        if total <= 0:
            return 0.0
        return self.slot_counts.get(slot, 0) / total

    def prompt_line(self) -> str:
        grams = f"{self.avg_grams:.0f} גרם" if self.avg_grams else "מנה רגילה"
        slot = self.dominant_slot
        slot_he = f", בדרך כלל ב{MEAL_SLOT_LABELS_HE.get(slot, slot)}" if slot else ""
        return (
            f"{self.display_name} — הופיע {self.count} פעמים; "
            f"ממוצע למנה: {grams}, {self.avg_calories:.0f} קל׳, "
            f"{self.avg_protein:.0f} ג׳ חלבון{slot_he}"
        )

    def ai_payload(self) -> dict[str, Any]:
        return {
            "name": self.display_name,
            "times_logged": self.count,
            "avg_grams": round(self.avg_grams, 1),
            "avg_calories": round(self.avg_calories, 1),
            "avg_protein": round(self.avg_protein, 1),
            "avg_carbs": round(self.avg_carbs, 1),
            "avg_fat": round(self.avg_fat, 1),
            "last_eaten_at": self.last_eaten_at,
            "usual_meal_slot": self.dominant_slot,
            "meal_slot_counts": dict(self.slot_counts),
            "source": "approved_meal_history",
        }


def normalize_food_key(value: str) -> str:
    clean = str(value or "").strip().lower()
    for marker in _COOKED_MARKERS:
        clean = clean.replace(marker, "")
    clean = clean.replace("׳", "'").replace("״", '"')
    clean = _NORMALIZE_RE.sub(" ", clean).strip()
    return clean


def _is_useful_name(value: str) -> bool:
    key = normalize_food_key(value)
    if not key or len(key) < 2:
        return False
    if key in _GENERIC_ITEMS:
        return False
    return True


def learned_food_keys(foods: Iterable[LearnedFood]) -> set[str]:
    return {food.key for food in foods if food.key}


def text_matches_learned_food(texts: Iterable[str], keys: set[str]) -> bool:
    if not keys:
        return False
    normalized_text = " ".join(normalize_food_key(text) for text in texts if str(text).strip())
    if not normalized_text:
        return False
    return any(key and key in normalized_text for key in keys)


async def learned_foods_from_meals(
    db: Any,
    user_id: int,
    *,
    limit: int = 8,
    min_count: int = 2,
    days_back: int = 180,
) -> list[LearnedFood]:
    """Return frequent user foods learned from approved meals.

    ``min_count=1`` is useful for meal-analysis prompts (a product uploaded once
    is still helpful context).  Recommendations should usually use the default
    ``min_count=2`` so a one-off meal does not dominate future suggestions.
    """
    since = (datetime.now(TZ).astimezone(timezone.utc) - timedelta(days=days_back)).isoformat()
    rows = await db.fetch_all(
        """
        SELECT mi.name, mi.grams, mi.calories, mi.protein, mi.carbs, mi.fat, m.eaten_at
        FROM meal_items mi
        JOIN meals m ON m.id = mi.meal_id
        WHERE m.user_id=? AND m.eaten_at>=?
        ORDER BY m.eaten_at DESC, mi.id DESC
        LIMIT 500
        """,
        (user_id, since),
    )
    buckets: dict[str, dict[str, Any]] = {}
    for row in rows:
        name = str(row["name"] or "").strip()
        if not _is_useful_name(name):
            continue
        key = normalize_food_key(name)
        bucket = buckets.setdefault(
            key,
            {
                "display_name": name,
                "count": 0,
                "grams": 0.0,
                "calories": 0.0,
                "protein": 0.0,
                "carbs": 0.0,
                "fat": 0.0,
                "last_eaten_at": str(row["eaten_at"] or ""),
                "slot_counts": {},
            },
        )
        bucket["count"] += 1
        for field_name in ("grams", "calories", "protein", "carbs", "fat"):
            try:
                bucket[field_name] += float(row[field_name] or 0)
            except (TypeError, ValueError):
                pass
        slot = _meal_slot_for_timestamp(str(row["eaten_at"] or ""))
        if slot is not None:
            bucket["slot_counts"][slot] = bucket["slot_counts"].get(slot, 0) + 1
        if str(row["eaten_at"] or "") > str(bucket["last_eaten_at"] or ""):
            bucket["last_eaten_at"] = str(row["eaten_at"] or "")
            bucket["display_name"] = name

    learned: list[LearnedFood] = []
    for key, bucket in buckets.items():
        count = int(bucket["count"] or 0)
        if count < min_count:
            continue
        learned.append(
            LearnedFood(
                key=key,
                display_name=str(bucket["display_name"]),
                count=count,
                avg_grams=float(bucket["grams"] or 0) / count,
                avg_calories=float(bucket["calories"] or 0) / count,
                avg_protein=float(bucket["protein"] or 0) / count,
                avg_carbs=float(bucket["carbs"] or 0) / count,
                avg_fat=float(bucket["fat"] or 0) / count,
                last_eaten_at=str(bucket["last_eaten_at"] or ""),
                slot_counts=dict(bucket["slot_counts"]),
            )
        )
    learned.sort(key=lambda item: (item.count, item.last_eaten_at), reverse=True)
    return learned[:limit]


async def learned_foods_prompt_block(
    db: Any,
    user_id: int | None,
    *,
    limit: int = 8,
    min_count: int = 1,
) -> str:
    if not user_id:
        return ""
    foods = await learned_foods_from_meals(db, user_id, limit=limit, min_count=min_count)
    if not foods:
        return ""
    lines = "\n".join(f"- {food.prompt_line()}" for food in foods)
    return (
        "\n\nUser-learned foods/products from approved meal history. "
        "Use this as calibration and recognition context when visually/textually plausible; "
        "do not force these foods if the photo/text clearly shows something else:\n"
        f"{lines}"
    )
