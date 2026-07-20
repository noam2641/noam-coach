"""Curated lookup of common Israeli packaged foods and dishes (RE10-15).

Free-form AI meal analysis (``analyze_meal_image`` / ``analyze_meal_text`` /
``reanalyze_meal_with_text_and_image`` in ``noam_coach/services/profile.py``)
had no notion of Israeli products or brands: a photo of Bamba (a peanut-puff
snack) was described generically as "fried potato chips" — wrong both as a
name AND nutritionally (Bamba is peanut-based, not potato-based).

This module is the deterministic source of truth for a curated set of common
Israeli foods. It never talks to the network and never guesses — it only
recognizes an EXACT match (after Hebrew/transliteration normalization) against
a known alias, and returns per-100g macros plus a typical serving size.

Design notes
------------
* Values are per-100g so any AI-estimated gram amount can be rescaled.
* Aliases include Hebrew, common transliterations, and a couple of brand
  variants (e.g. "במבה אסם"). Matching is substring-based on a normalized
  (lowercased, niqqud/punctuation-stripped) string so "שקית במבה" and "במבה"
  both match the same entry.
* This is intentionally a small, curated list (not a full food database) —
  scope is the items that are common enough to misfire generic AI vision
  models, per the product decision for RE10-15.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class IsraeliFood:
    canonical_name: str  # Hebrew display name
    aliases: tuple[str, ...]  # normalized match strings (see _normalize)
    calories_per_100g: float
    protein_per_100g: float
    carbs_per_100g: float
    fat_per_100g: float
    typical_serving_g: float  # a common single-serving size, for reference only


def _normalize(text: str) -> str:
    """Lowercase, strip Hebrew niqqud/punctuation and collapse whitespace."""
    text = text.strip().lower()
    text = re.sub(r"[֑-ׇ]", "", text)  # niqqud/cantillation marks
    text = re.sub(r"[^\w\s֐-׿]", " ", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


# Per-100g values sourced from manufacturer nutrition labels (Osem, Strauss,
# Elite, Tara, Tnuva) where available; otherwise a conservative estimate of
# the common preparation.
_FOODS: tuple[IsraeliFood, ...] = (
    IsraeliFood(
        "במבה (חטיף בוטנים)",
        ("bamba", "במבה", "במבה אסם", "חטיף במבה"),
        calories_per_100g=520, protein_per_100g=13, carbs_per_100g=54, fat_per_100g=31,
        typical_serving_g=25,
    ),
    IsraeliFood(
        "ביסלי",
        ("bissli", "ביסלי"),
        calories_per_100g=470, protein_per_100g=9, carbs_per_100g=63, fat_per_100g=20,
        typical_serving_g=25,
    ),
    IsraeliFood(
        "אפרופו",
        ("apropo", "אפרופו"),
        calories_per_100g=540, protein_per_100g=6, carbs_per_100g=52, fat_per_100g=34,
        typical_serving_g=20,
    ),
    IsraeliFood(
        "שוקו (משקה חלב בטעם שוקולד)",
        ("שוקו", "שוקו תנובה", "choco milk"),
        calories_per_100g=75, protein_per_100g=3.2, carbs_per_100g=11, fat_per_100g=1.8,
        typical_serving_g=200,
    ),
    IsraeliFood(
        "קוטג'",
        ("קוטג'", "קוטג", "cottage"),
        calories_per_100g=98, protein_per_100g=11, carbs_per_100g=3.5, fat_per_100g=5,
        typical_serving_g=250,
    ),
    IsraeliFood(
        "פתיתים",
        ("פתיתים", "ptitim", "israeli couscous", "פתיתים מבושלים"),
        calories_per_100g=170, protein_per_100g=6, carbs_per_100g=35, fat_per_100g=0.5,
        typical_serving_g=150,
    ),
    IsraeliFood(
        "קרלו (חטיף תירס)",
        ("carlo", "קרלו"),
        calories_per_100g=525, protein_per_100g=6, carbs_per_100g=58, fat_per_100g=30,
        typical_serving_g=20,
    ),
    IsraeliFood(
        "מילקי",
        ("milky", "מילקי"),
        calories_per_100g=145, protein_per_100g=3.5, carbs_per_100g=17, fat_per_100g=7,
        typical_serving_g=100,
    ),
    IsraeliFood(
        "חלווה",
        ("halva", "חלווה", "חלבה"),
        calories_per_100g=500, protein_per_100g=11, carbs_per_100g=48, fat_per_100g=30,
        typical_serving_g=30,
    ),
    IsraeliFood(
        "בורקס",
        ("borekas", "בורקס"),
        calories_per_100g=310, protein_per_100g=6, carbs_per_100g=28, fat_per_100g=19,
        typical_serving_g=100,
    ),
    IsraeliFood(
        "ג'חנון",
        ("jachnun", "ג'חנון", "גחנון"),
        calories_per_100g=380, protein_per_100g=6, carbs_per_100g=40, fat_per_100g=22,
        typical_serving_g=150,
    ),
    IsraeliFood(
        "סביח",
        ("sabich", "סביח"),
        calories_per_100g=220, protein_per_100g=6, carbs_per_100g=22, fat_per_100g=12,
        typical_serving_g=350,
    ),
    IsraeliFood(
        "חומוס (מוכן, קנוי)",
        ("hummus", "חומוס"),
        calories_per_100g=175, protein_per_100g=8, carbs_per_100g=12, fat_per_100g=11,
        typical_serving_g=150,
    ),
    IsraeliFood(
        "לחם אחיד/פרוס",
        ("לחם אחיד", "לחם פרוס", "sliced bread"),
        calories_per_100g=250, protein_per_100g=9, carbs_per_100g=48, fat_per_100g=2.5,
        typical_serving_g=60,
    ),
    # TASK-58 (Israeli food override safety): the generic word "טחינה" is
    # AMBIGUOUS in normal Israeli usage — raw paste (595 kcal/100g), prepared/
    # diluted tahini (~230 kcal/100g), or another visually similar spread.
    # A deterministic macro override may therefore match only genuinely
    # SPECIFIC names; a generic "טחינה" item keeps the AI's own estimate.
    IsraeliFood(
        "טחינה גולמית",
        ("raw tahini", "tahini paste", "טחינה גולמית"),
        calories_per_100g=595, protein_per_100g=17, carbs_per_100g=21, fat_per_100g=54,
        typical_serving_g=30,
    ),
    IsraeliFood(
        "טחינה מוכנה",
        ("prepared tahini", "סלט טחינה", "טחינה מוכנה"),
        calories_per_100g=230, protein_per_100g=7, carbs_per_100g=10, fat_per_100g=19,
        typical_serving_g=50,
    ),
    IsraeliFood(
        "חציל במיונז",
        ("סלט חצילים במיונז", "חציל עם מיונז", "חציל במיונז"),
        calories_per_100g=165, protein_per_100g=1.5, carbs_per_100g=6, fat_per_100g=15,
        typical_serving_g=100,
    ),
    IsraeliFood(
        "שניצל (מטוגן)",
        ("schnitzel", "שניצל"),
        calories_per_100g=280, protein_per_100g=17, carbs_per_100g=15, fat_per_100g=17,
        typical_serving_g=150,
    ),
    IsraeliFood(
        "לחמנייה",
        ("לחמניה", "לחמניות", "bun"),
        calories_per_100g=280, protein_per_100g=9, carbs_per_100g=52, fat_per_100g=4,
        typical_serving_g=70,
    ),
    IsraeliFood(
        "עוגיות פתי בר",
        ("petit beurre", "פתי בר"),
        calories_per_100g=440, protein_per_100g=7, carbs_per_100g=72, fat_per_100g=14,
        typical_serving_g=30,
    ),
    IsraeliFood(
        "וופל נוגט",
        ("nougat wafer", "וופל נוגט"),
        calories_per_100g=530, protein_per_100g=6, carbs_per_100g=58, fat_per_100g=31,
        typical_serving_g=27,
    ),
    IsraeliFood(
        "גזר מגורד",
        ("גזר מגורד", "grated carrot"),
        calories_per_100g=41, protein_per_100g=0.9, carbs_per_100g=10, fat_per_100g=0.2,
        typical_serving_g=100,
    ),
    IsraeliFood(
        "לבנה",
        ("labaneh", "לבנה"),
        calories_per_100g=110, protein_per_100g=5, carbs_per_100g=4, fat_per_100g=8,
        typical_serving_g=100,
    ),
    IsraeliFood(
        "במבה אגוזים",
        ("במבה אגוזים",),
        calories_per_100g=555, protein_per_100g=12, carbs_per_100g=45, fat_per_100g=37,
        typical_serving_g=25,
    ),
    IsraeliFood(
        "עמק 9%",
        ("עמק 9",),
        calories_per_100g=105, protein_per_100g=10, carbs_per_100g=3, fat_per_100g=9,
        typical_serving_g=125,
    ),
    IsraeliFood(
        "יוגורט תות (טרה/יטבתה)",
        ("יוגורט תות", "strawberry yogurt"),
        calories_per_100g=95, protein_per_100g=3.2, carbs_per_100g=15, fat_per_100g=2.5,
        typical_serving_g=150,
    ),
    IsraeliFood(
        "שקדי מרק (אטריות)",
        ("שקדי מרק", "soup noodles"),
        calories_per_100g=370, protein_per_100g=12, carbs_per_100g=72, fat_per_100g=2,
        typical_serving_g=40,
    ),
    IsraeliFood(
        "פלאפל",
        ("falafel", "פלאפל"),
        calories_per_100g=330, protein_per_100g=13, carbs_per_100g=32, fat_per_100g=18,
        typical_serving_g=100,
    ),
    IsraeliFood(
        "שקשוקה",
        ("shakshuka", "שקשוקה"),
        calories_per_100g=120, protein_per_100g=7, carbs_per_100g=6, fat_per_100g=8,
        typical_serving_g=300,
    ),
    IsraeliFood(
        "מלאווח",
        ("malawach", "מלאווח"),
        calories_per_100g=390, protein_per_100g=6, carbs_per_100g=38, fat_per_100g=24,
        typical_serving_g=120,
    ),
    IsraeliFood(
        "בייגלה",
        ("bagele", "בייגלה"),
        calories_per_100g=400, protein_per_100g=10, carbs_per_100g=72, fat_per_100g=8,
        typical_serving_g=30,
    ),
    IsraeliFood(
        "חלה",
        ("challah", "חלה"),
        calories_per_100g=300, protein_per_100g=9, carbs_per_100g=50, fat_per_100g=7,
        typical_serving_g=70,
    ),
)

_NORMALIZED_INDEX: tuple[tuple[str, IsraeliFood], ...] = tuple(
    (_normalize(alias), food) for food in _FOODS for alias in (*food.aliases, food.canonical_name)
)


def lookup(name: str) -> IsraeliFood | None:
    """Return the matching curated food for a name, or None.

    Matching is intentionally conservative: the query matches when it EQUALS
    a known alias or CONTAINS one as a whole normalized string ("במבה אסם"
    contains "במבה"). TASK-58 evidence-aware canonicalization: the reverse
    direction — a query that is merely a SUBSTRING of a longer, more specific
    alias — no longer matches, because a generic name ("טחינה") carries less
    evidence than the specific alias ("טחינה גולמית") and must not receive
    that food's deterministic macros. Specificity must come from the item
    name itself, never be invented by canonicalization.
    """
    if not name or not name.strip():
        return None
    normalized_query = _normalize(name)
    if not normalized_query:
        return None
    best: IsraeliFood | None = None
    best_len = 0
    for alias, food in _NORMALIZED_INDEX:
        if not alias:
            continue
        if alias == normalized_query or alias in normalized_query:
            if len(alias) > best_len:
                best = food
                best_len = len(alias)
    return best


def scaled_macros(food: IsraeliFood, grams: float) -> dict[str, float]:
    """Return calories/protein/carbs/fat for the given gram amount."""
    ratio = max(0.0, grams) / 100.0
    return {
        "calories": round(food.calories_per_100g * ratio, 1),
        "protein": round(food.protein_per_100g * ratio, 1),
        "carbs": round(food.carbs_per_100g * ratio, 1),
        "fat": round(food.fat_per_100g * ratio, 1),
    }
