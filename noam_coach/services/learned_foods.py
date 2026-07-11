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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from config import TZ

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

    def prompt_line(self) -> str:
        grams = f"{self.avg_grams:.0f} גרם" if self.avg_grams else "מנה רגילה"
        return (
            f"{self.display_name} — הופיע {self.count} פעמים; "
            f"ממוצע למנה: {grams}, {self.avg_calories:.0f} קל׳, "
            f"{self.avg_protein:.0f} ג׳ חלבון"
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
            },
        )
        bucket["count"] += 1
        for field in ("grams", "calories", "protein", "carbs", "fat"):
            try:
                bucket[field] += float(row[field] or 0)
            except (TypeError, ValueError):
                pass
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
