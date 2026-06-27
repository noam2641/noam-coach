from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import user_model
from noam_coach.services.dietary_restrictions import (
    DietaryRestriction,
    merge_restrictions,
    normalize_restriction,
    parse_restrictions,
)

DISLIKE_FACT = "disliked_foods"
PREFERENCE_FACT = "preferred_foods"

_DISLIKE_MARKERS = (
    "לא אוהב",
    "לא אוהבת",
    "לא מתחבר",
    "לא מתחברת",
    "לא בא לי",
)
_PREFERENCE_MARKERS = ("אוהב", "אוהבת", "מעדיף", "מעדיפה")
_DIET_RULE_WORDS = (
    "צמחוני",
    "צמחונית",
    "טבעוני",
    "טבעונית",
    "כשר",
    "כשרה",
    "קטוגני",
    "ללא",
    "בלי",
    "גלוטן",
)
_CLEAN_PREFIXES = (
    "אני לא אוכל את",
    "אני לא אוכלת את",
    "אני לא אוכל",
    "אני לא אוכלת",
    "אני לא שותה את",
    "אני לא שותה",
    "אני לא צורך את",
    "אני לא צורכת את",
    "אני לא צורך",
    "אני לא צורכת",
    "אני לא נוגע ב",
    "אני לא נוגעת ב",
    "אני לא נוגע",
    "אני לא נוגעת",
    "אני לא אוהב את",
    "אני לא אוהבת את",
    "אני לא אוהב",
    "אני לא אוהבת",
    "לא אוכל את",
    "לא אוכלת את",
    "לא אוכל",
    "לא אוכלת",
    "לא שותה את",
    "לא שותה",
    "לא צורך את",
    "לא צורכת את",
    "לא צורך",
    "לא צורכת",
    "לא נוגע ב",
    "לא נוגעת ב",
    "לא נוגע",
    "לא נוגעת",
    "לא אוהב את",
    "לא אוהבת את",
    "לא אוהב",
    "לא אוהבת",
    "אני אוהב את",
    "אני אוהבת את",
    "אני אוהב",
    "אני אוהבת",
    "אני מעדיף את",
    "אני מעדיפה את",
    "אני מעדיף",
    "אני מעדיפה",
    "מעדיף את",
    "מעדיפה את",
    "מעדיף",
    "מעדיפה",
    "בלי",
    "ללא",
    "לא ",
    "אין ",
    "אל ",
    "אני ",
)


@dataclass(frozen=True)
class PreferenceUpdate:
    fact_key: str
    item: str
    new_value: str
    already_present: bool
    removed_from: list[str] = field(default_factory=list)


def split_fact_list(value: Any) -> list[str]:
    if value in (None, "", "none"):
        return []
    if isinstance(value, list):
        raw = [str(item) for item in value]
    else:
        raw = str(value).replace(";", ",").split(",")
    return [item.strip() for item in raw if item and item.strip()]


def join_fact_list(items: list[str]) -> str:
    deduped: list[str] = []
    seen: set[str] = set()
    for item in items:
        clean = item.strip()
        if not clean:
            continue
        key = normalize_restriction(clean)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(clean)
    return ", ".join(deduped)


def clean_food_item(text: str) -> str:
    clean = (text or "").strip()
    for prefix in _CLEAN_PREFIXES:
        if clean.startswith(prefix):
            clean = clean[len(prefix):].strip()
            break
    return clean or (text or "").strip()


def target_fact_for_slots(slots: dict[str, Any], text: str) -> str:
    kind = str(slots.get("kind") or "restriction")
    polarity = str(slots.get("polarity") or "").lower()
    combined = f"{text} {slots.get('item') or ''} {slots.get('note') or ''}".strip()
    if kind == "allergy":
        return "allergies"
    if any(marker in combined for marker in _DISLIKE_MARKERS) or (
        polarity == "avoid" and kind == "preference"
    ):
        return DISLIKE_FACT
    if kind == "preference" and any(word in combined for word in _DIET_RULE_WORDS):
        return "diet_restrictions"
    if kind == "preference" and (polarity == "prefer" or any(marker in combined for marker in _PREFERENCE_MARKERS)):
        return PREFERENCE_FACT
    return "diet_restrictions"


async def record_food_preference_from_slots(
    db: Any,
    user_id: int,
    slots: dict[str, Any],
    text: str,
) -> PreferenceUpdate:
    fact_key = target_fact_for_slots(slots, text)
    item = clean_food_item(str(slots.get("item") or slots.get("note") or text))
    current = split_fact_list(await user_model.get_value(db, user_id, fact_key))
    canonical_item = normalize_restriction(item)
    already = any(normalize_restriction(existing) == canonical_item for existing in current)
    if not already:
        current.append(item)
    await user_model.set_fact(
        db,
        user_id,
        fact_key,
        join_fact_list(current),
        kind=user_model.KIND_FACT,
        source=user_model.SOURCE_USER,
        confirmed=True,
    )

    removed_from: list[str] = []
    if fact_key in {DISLIKE_FACT, PREFERENCE_FACT}:
        opposite = PREFERENCE_FACT if fact_key == DISLIKE_FACT else DISLIKE_FACT
        opposite_items = split_fact_list(await user_model.get_value(db, user_id, opposite))
        kept = [item for item in opposite_items if normalize_restriction(item) != canonical_item]
        if len(kept) != len(opposite_items):
            removed_from.append(opposite)
            await user_model.set_fact(
                db,
                user_id,
                opposite,
                join_fact_list(kept),
                kind=user_model.KIND_FACT,
                source=user_model.SOURCE_USER,
                confirmed=True,
            )

    return PreferenceUpdate(
        fact_key=fact_key,
        item=item,
        new_value=join_fact_list(current),
        already_present=already,
        removed_from=removed_from,
    )


async def preference_restrictions_from_facts(db: Any, user_id: int) -> list[DietaryRestriction]:
    disliked = split_fact_list(await user_model.get_value(db, user_id, DISLIKE_FACT))
    if not disliked:
        return []
    return parse_restrictions(
        ", ".join(disliked),
        restriction_type="preference",
        severity="low",
        source=f"facts:{DISLIKE_FACT}",
        confirmed=True,
    )


def merge_with_preference_restrictions(
    base: list[DietaryRestriction],
    preferences: list[DietaryRestriction],
) -> list[DietaryRestriction]:
    return merge_restrictions(base, preferences)
