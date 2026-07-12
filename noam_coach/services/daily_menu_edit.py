"""Natural-language edits for the standalone daily menu.

TASK-05/06 asked that the daily menu be a pin-friendly object users can work
against during the day, including requests such as "תחליף לי את ארוחת הבוקר
לחלבון אחר".  This module owns the lightweight deterministic parsing and reply
contract for those requests.

It intentionally does not mark anything as eaten.  It returns a focused
replacement suggestion + actions; the regular next-meal save path is still the
only path that writes a consumed meal.
"""

from __future__ import annotations

import json
import re
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from config import TZ
from helpers import esc
from noam_coach.services.daily_menu_state import (
    get_active_daily_menu,
    is_structured_menu,
    remember_active_daily_menu,
    structured_meals,
)
from noam_coach.services.food_preferences import record_food_preference_from_slots
from noam_coach.services.learned_foods import learned_foods_from_meals, normalize_food_key
from noam_coach.services.menu_validation import (
    PROBLEM_DISLIKED_FOOD,
    PROBLEM_RESTRICTION_VIOLATION,
    validate_menu,
)
from noam_coach.services.preference_profile import FAMILIARITY_MIN_COUNT, build_preference_profile

# TASK-10: markers that make a stated avoidance sound PERMANENT ("I don't
# like X" / "I don't eat X") as opposed to a today-only context constraint
# ("I don't have time to cook today"). Generic, not food-specific — the same
# markers food_preferences.py already uses to route a fact.
_PERMANENT_MARKERS = (
    "לא אוהב",
    "לא אוהבת",
    "לא אוכל",
    "לא אוכלת",
    "אני לא אוהב",
    "אני לא אוהבת",
    "אלרגי",
    "רגיש",
)
# Markers that make an avoidance sound explicitly TEMPORARY/today-scoped —
# these must never be written to a permanent fact even if a food word is
# also present in the same sentence.
_TEMPORARY_MARKERS = (
    "היום",
    "עכשיו",
    "כרגע",
    "הפעם",
    "אין לי זמן",
)


@dataclass(frozen=True)
class DailyMenuEditIntent:
    slot: str
    slot_label: str
    instruction: str
    wants_protein: bool
    wants_light: bool
    wants_no_item: str | None = None


_SLOT_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("breakfast", ("ארוחת בוקר", "בוקר", "breakfast")),
    ("lunch", ("ארוחת צהריים", "צהריים", "lunch")),
    ("pre_workout", ("לפני אימון", "קדם אימון", "פרי וורקאאוט", "pre workout")),
    ("dinner", ("ארוחת ערב", "ערב", "dinner")),
    ("snack", ("נשנוש", "ביניים", "חטיף", "snack")),
)
_SLOT_LABELS = {
    "breakfast": "ארוחת הבוקר",
    "lunch": "ארוחת הצהריים",
    "pre_workout": "הארוחה לפני האימון",
    "dinner": "ארוחת הערב",
    "snack": "ארוחת הביניים",
}
_CHANGE_TOKENS = (
    "תחליף",
    "החלף",
    "תשנה",
    "שנה",
    "רענן",
    "תעדכן",
    "לעדכן",
    "להחליף",
    "בלי",
    "ללא",
    "פחות",
    "יותר",
    "גדול",
    "גדולה",
    "קטן",
    "קטנה",
    "מסעד",
    "בישול",
    "לא אוהב",
    "לא אוהבת",
    "לא רוצה",
)
_PROTEIN_TOKENS = ("חלבון", "protein", "גבינה", "יוגורט", "טונה", "עוף", "ביצה")
_LIGHT_TOKENS = ("קל", "קלה", "קטן", "קטנה", "פחות", "דל", "דליל")


def parse_daily_menu_edit(text: str, *, active_menu_context: bool = False) -> DailyMenuEditIntent | None:
    """Parse a natural-language daily-menu edit request.

    Returns None for ordinary chat so it does not steal unrelated messages.
    """
    normalized = str(text or "").strip().lower()
    if not normalized:
        return None
    has_change = any(token in normalized for token in _CHANGE_TOKENS)
    mentions_menu = "תפריט" in normalized or any(
        any(alias in normalized for alias in aliases) for _, aliases in _SLOT_ALIASES
    )
    if not (has_change and (mentions_menu or active_menu_context)):
        return None
    slot = "snack"
    for candidate, aliases in _SLOT_ALIASES:
        if any(alias in normalized for alias in aliases):
            slot = candidate
            break
    no_item = None
    match = re.search(
        r"(?:בלי|ללא|אל תשים|לא אוהב(?:ת)?|לא רוצה|פחות)\s+([^,.!?\n]+)",
        normalized,
    )
    if match:
        no_item = match.group(1).strip()
    return DailyMenuEditIntent(
        slot=slot,
        slot_label=_SLOT_LABELS.get(slot, "הארוחה"),
        instruction=str(text).strip(),
        wants_protein=any(token in normalized for token in _PROTEIN_TOKENS),
        wants_light=any(token in normalized for token in _LIGHT_TOKENS),
        wants_no_item=no_item,
    )


def _learned_choice(
    foods: list[Any], *, wants_protein: bool, avoid: str | None, profile: Any | None = None,
) -> Any | None:
    """Pick the best learned-food replacement.

    Bug found via the corrective-review Finding-10 test: this previously only
    excluded the item currently being avoided ("בלי ביצים" -> exclude eggs)
    but never checked the user's OTHER hard exclusions — so a learned food
    the user separately dislikes/is allergic to could be suggested as the
    replacement for an unrelated edit. ``profile`` (when supplied) is checked
    with the same generic hard-exclusion matcher used everywhere else.
    """
    avoid_text = str(avoid or "").lower()
    candidates = []
    for food in foods:
        name = str(getattr(food, "display_name", "") or "")
        if avoid_text and avoid_text in name.lower():
            continue
        if profile is not None and profile.is_hard_excluded(name):
            continue
        protein = float(getattr(food, "avg_protein", 0) or 0)
        calories = float(getattr(food, "avg_calories", 0) or 0)
        score = float(getattr(food, "count", 0) or 0)
        if wants_protein:
            score += protein / 10
        if calories > 0:
            score += min(2.0, protein / max(1.0, calories) * 20)
        candidates.append((score, food))
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: item[0], reverse=True)[0][1]


def _fallback_suggestion(intent: DailyMenuEditIntent) -> tuple[str, int, int, str]:
    if intent.wants_protein:
        return ("יוגורט/קוטג׳ עשיר בחלבון + ירק או פרי", 260, 28, "נותן חלבון ברור בלי להפוך את הארוחה לכבדה מדי")
    if intent.wants_light:
        return ("טורטייה קטנה עם מקור חלבון רזה וירקות", 330, 30, "גרסה קלה יותר ששומרת על חלבון")
    return ("מקור חלבון רזה + פחמימה מדודה + ירקות", 480, 42, "שומר על איזון קלוריות/חלבון בלי לספור את זה כאכילה")


async def _remember_request(db: Any, user_id: int, intent: DailyMenuEditIntent) -> None:
    day = datetime.now(TZ).date().isoformat()
    row = await db.fetch_one("SELECT flags FROM daily_flags WHERE user_id=? AND day=?", (user_id, day))
    flags: dict[str, Any]
    if row:
        try:
            flags = json.loads(row["flags"] or "{}")
        except (TypeError, json.JSONDecodeError):
            flags = {}
    else:
        flags = {}
    flags["daily_menu_last_edit_request"] = {
        "slot": intent.slot,
        "instruction": intent.instruction,
        "saved_at": datetime.now(TZ).isoformat(),
    }
    await db.execute(
        """
        INSERT INTO daily_flags(user_id, day, flags, created_at)
        VALUES(?, ?, ?, ?)
        ON CONFLICT(user_id, day) DO UPDATE SET flags=excluded.flags
        """,
        (user_id, day, json.dumps(flags, ensure_ascii=False), datetime.now(TZ).isoformat()),
    )


def _without_avoided_lines(text: str, avoid: str | None) -> str:
    if not text or not avoid:
        return text
    avoid_norm = str(avoid).strip().lower()
    if not avoid_norm:
        return text
    kept = [line for line in text.splitlines() if avoid_norm not in line.lower()]
    return "\n".join(kept).strip() or text


def _build_revision_text(
    *,
    current_text: str | None,
    intent: DailyMenuEditIntent,
    replacement_name: str,
    calories: int,
    protein: int,
    reason: str,
    revision: int,
) -> str:
    base = _without_avoided_lines(current_text or "", intent.wants_no_item)
    replacement_block = (
        f"<b>עדכון לתפריט היומי - גרסה {revision}</b>\n"
        f"עודכן לפי הבקשה: {esc(intent.instruction)}\n"
        f"עבור {esc(intent.slot_label)}: <b>{esc(replacement_name)}</b>\n"
        f"≈{calories} קל׳ | ≈{protein} ג׳ חלבון\n"
        f"למה זה מתאים: {esc(reason)}."
    )
    if intent.wants_no_item:
        replacement_block += f"\nנשמר כאילוץ להמשך היום: בלי {esc(intent.wants_no_item)}."
    if not base:
        return replacement_block
    return (
        f"{base}\n\n"
        f"{replacement_block}\n\n"
        "אפשר להמשיך לכתוב לי שינויים, וכל עדכון יתבסס על הגרסה הפעילה הזו."
    )


def _is_permanent_avoidance(instruction: str, wants_no_item: str | None) -> bool:
    """TASK-10: distinguish a permanent dislike statement from a daily
    context constraint. Only fires when there is an actual avoided food item
    AND permanence phrasing, and no same-sentence temporary-scope marker."""
    if not wants_no_item:
        return False
    normalized = str(instruction or "").strip().lower()
    if any(marker in normalized for marker in _TEMPORARY_MARKERS):
        return False
    return any(marker in normalized for marker in _PERMANENT_MARKERS)


async def _persist_permanent_dislike(db: Any, user_id: int, item: str, instruction: str) -> None:
    with suppress(Exception):
        await record_food_preference_from_slots(
            db,
            user_id,
            {"kind": "preference", "polarity": "avoid", "item": item},
            instruction,
        )


def _meal_matches_avoided_item(meal: dict[str, Any], avoid_key: str) -> bool:
    """TASK-10/Finding 8: check structured ingredient identities FIRST (the
    reliable signal — "does this meal actually contain egg"), falling back to
    free-text role/note matching only when no structured ingredients were
    persisted (older/legacy menus, or a meal the AI didn't decompose)."""
    from noam_coach.services.next_meal import _food_word_matches

    if not avoid_key:
        return False
    ingredients = meal.get("ingredients") or []
    if isinstance(ingredients, list) and ingredients:
        for item in ingredients:
            if not isinstance(item, dict):
                continue
            key = normalize_food_key(str(item.get("name") or ""))
            if key and _food_word_matches(avoid_key, key):
                return True
        return False
    texts = [str(meal.get("role") or ""), str(meal.get("note") or "")]
    for text in texts:
        key = normalize_food_key(text)
        if key and _food_word_matches(avoid_key, key):
            return True
    return False


def _substitute_ingredients(
    ingredients: list[dict[str, Any]],
    *,
    avoid_key: str,
    replacement_name: str,
    replacement_calories: int,
    replacement_protein: int,
) -> list[dict[str, Any]] | None:
    """Remove only the ingredient(s) matching ``avoid_key`` and add ONE
    replacement ingredient in their place — the unaffected ingredients are
    preserved untouched. Returns ``None`` when nothing in ``ingredients``
    actually matches (the caller should not treat this as a match).

    This is the targeted-substitution fix: previously a meal composed of
    "ביצים + לחם + גבינה + ירקות" would have its ENTIRE content replaced by a
    single familiar food the moment eggs were detected, discarding the bread/
    cheese/vegetables that had nothing to do with the request. Now only the
    egg contribution is removed and replaced; bread/cheese/vegetables survive.
    """
    from noam_coach.services.next_meal import _food_word_matches

    kept: list[dict[str, Any]] = []
    any_removed = False
    for item in ingredients:
        if not isinstance(item, dict):
            continue
        key = normalize_food_key(str(item.get("name") or ""))
        if key and _food_word_matches(avoid_key, key):
            any_removed = True
            continue
        kept.append(dict(item))
    if not any_removed:
        return None
    kept.append(
        {
            "name": replacement_name,
            "grams": None,
            "calories": replacement_calories,
            "protein": replacement_protein,
            "carbs": None,
            "fat": None,
        }
    )
    return kept


def _meal_totals_from_ingredients(ingredients: list[dict[str, Any]]) -> tuple[int, int]:
    calories = sum(float(item.get("calories") or 0) for item in ingredients)
    protein = sum(float(item.get("protein") or 0) for item in ingredients)
    return int(round(calories)), int(round(protein))


def _regenerate_structured_meals(
    meals: list[dict[str, Any]],
    *,
    avoid: str | None,
    replacement_name: str,
    calories: int,
    protein: int,
) -> tuple[list[dict[str, Any]], list[int]]:
    """TASK-10: repair only the meals matching the avoided item; every other
    meal is preserved byte-for-byte (same contract as the morning-menu repair
    pipeline — no blanket regeneration for a targeted edit).

    Targeted ingredient substitution (not whole-meal replacement): when a
    matching meal has structured ingredients, only the ingredient(s)
    identifying the avoided item are removed and replaced — other ingredients
    in that meal (bread, cheese, vegetables, ...) are preserved, and the
    meal's calories/protein/note are recomputed from the resulting ingredient
    list, not overwritten with the substitute's own totals. A meal with no
    structured ingredients (legacy/undecomposed) falls back to whole-meal
    replacement, since there is nothing more granular to target.

    Finding 11: the meal's behavioral ``role`` (e.g. "ארוחת בוקר") is a
    distinct concept from the composed food's display name/description and
    must survive a food substitution — only ``note``/``calories``/``protein``/
    ``ingredients`` describe the actual food. Overwriting ``role`` with a
    food name previously destroyed the slot/intent semantics that later
    validation and rendering rely on.
    """
    avoid_key = normalize_food_key(avoid or "")
    updated: list[dict[str, Any]] = []
    changed_indices: list[int] = []
    for index, meal in enumerate(meals):
        if not (avoid_key and _meal_matches_avoided_item(meal, avoid_key)):
            updated.append(dict(meal))
            continue
        replaced = dict(meal)
        existing_ingredients = meal.get("ingredients") or []
        new_ingredients = (
            _substitute_ingredients(
                existing_ingredients,
                avoid_key=avoid_key,
                replacement_name=replacement_name,
                replacement_calories=calories,
                replacement_protein=protein,
            )
            if isinstance(existing_ingredients, list) and existing_ingredients
            else None
        )
        if new_ingredients is not None:
            # Targeted substitution: keep unaffected ingredients, recompute
            # totals from the actual resulting ingredient list.
            total_calories, total_protein = _meal_totals_from_ingredients(new_ingredients)
            replaced["ingredients"] = new_ingredients
            replaced["calories"] = total_calories
            replaced["protein"] = total_protein
            kept_names = [str(item.get("name") or "") for item in new_ingredients if item.get("name") != replacement_name]
            replaced["note"] = " + ".join([*kept_names, replacement_name]) if kept_names else replacement_name
        else:
            # No structured ingredients to target -- whole-meal replacement
            # is the only option (legacy menu / undecomposed meal).
            replaced["note"] = replacement_name
            replaced["calories"] = calories
            replaced["protein"] = protein
            replaced["ingredients"] = [
                {"name": replacement_name, "grams": None, "calories": calories, "protein": protein, "carbs": None, "fat": None}
            ]
        updated.append(replaced)
        changed_indices.append(index)
    return updated, changed_indices


class _EditedMeal:
    """Adapter so a structured-menu-edit dict can be validated by
    ``menu_validation.validate_menu``, which expects ``recommendations.
    MenuMeal``-shaped attribute access (``.name``/``.note``/``.calories``/...).
    Finding 10: a targeted edit must go through the SAME validator as
    generation/repair, not a separate unvalidated write path."""

    def __init__(self, meal: dict[str, Any]) -> None:
        self.name = str(meal.get("role") or "")
        self.time_hint = str(meal.get("time") or "")
        self.calories = float(meal.get("calories") or 0)
        self.protein = float(meal.get("protein") or 0)
        self.note = str(meal.get("note") or "")
        self.intent_id = str(meal.get("meal_id") or "")


class _EditedMenu:
    def __init__(self, meals: list[dict[str, Any]]) -> None:
        self.meals = [_EditedMeal(meal) for meal in meals]
        self.headline = "תפריט יומי"
        self.training_advice = ""
        self.closing = ""


async def _validate_edited_menu(db: Any, user_id: int, meals: list[dict[str, Any]], profile: Any | None = None) -> Any:
    """Finding 10: run the edited structured menu through the shared
    deterministic validator (same primitives as generation/repair) so a
    targeted edit can never silently introduce a hard-exclusion or
    restriction violation that generation/repair would have blocked.

    ``profile`` may be passed in to reuse an already-built
    ``NutritionPreferenceProfile`` for this same edit request instead of
    re-querying the database a second time.
    """
    if profile is None:
        profile = await build_preference_profile(db, user_id)
    meal_slots = [str(meal.get("slot") or "") or None for meal in meals]
    return validate_menu(
        _EditedMenu(meals),
        restrictions=profile.restrictions,
        profile=profile,
        meal_slots=meal_slots,
    )


def _structured_meals_to_text(meals: list[dict[str, Any]], headline: str = "תפריט יומי") -> str:
    lines = [f"<b>{esc(headline)}</b>", ""]
    for meal in meals:
        time = str(meal.get("time") or "").strip()
        role = str(meal.get("role") or "").strip()
        header = f"🍽️ {esc(time)} — <b>{esc(role)}</b>" if time else f"🍽️ <b>{esc(role)}</b>"
        lines.append(header)
        lines.append(f"{float(meal.get('calories') or 0):.0f} קל׳ | {float(meal.get('protein') or 0):.0f} ג׳ חלבון")
        note = str(meal.get("note") or "").strip()
        if note and note != role:
            lines.append(esc(note))
        lines.append("")
    if lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


async def try_build_daily_menu_edit_reply(
    db: Any,
    user_id: int,
    text: str,
) -> tuple[str, list[list[tuple[str, str]]]] | None:
    """Return a menu-edit reply and buttons, or None for unrelated text."""
    active_menu = await get_active_daily_menu(db, user_id)
    intent = parse_daily_menu_edit(text, active_menu_context=active_menu is not None)
    if intent is None:
        return None
    # TASK-4: this picks a personalized replacement suggestion for the active
    # menu, not a calibration/recognition prompt, so it uses the module's
    # documented min_count=2 default (a one-off meal should not drive a menu
    # substitution suggestion).
    foods = await learned_foods_from_meals(db, user_id, limit=8, min_count=FAMILIARITY_MIN_COUNT)
    edit_profile = await build_preference_profile(db, user_id)
    learned = _learned_choice(
        foods, wants_protein=intent.wants_protein, avoid=intent.wants_no_item, profile=edit_profile,
    )
    if learned is not None:
        name = str(getattr(learned, "display_name", "הפריט המוכר") or "הפריט המוכר")
        calories = int(round(float(getattr(learned, "avg_calories", 0) or 0)))
        protein = int(round(float(getattr(learned, "avg_protein", 0) or 0)))
        reason = "בחרתי פריט שכבר אישרת בעבר, ולכן הוא מתאים יותר להעדפות שלך"
    else:
        name, calories, protein, reason = _fallback_suggestion(intent)
    await _remember_request(db, user_id, intent)

    # TASK-10: an explicit PERMANENT statement ("אני לא אוהב X") also updates
    # the durable preference fact, not just today's menu — so future menus
    # (and next_meal) inherit the exclusion too. A same-sentence temporary
    # marker ("היום"/"עכשיו"/"אין לי זמן") is intentionally excluded from
    # this and stays daily-scoped only.
    if _is_permanent_avoidance(intent.instruction, intent.wants_no_item):
        await _persist_permanent_dislike(db, user_id, str(intent.wants_no_item), intent.instruction)

    avoid_line = f"\nהסרתי/נמנעתי מ: {esc(intent.wants_no_item)}" if intent.wants_no_item else ""
    revision = int((active_menu or {}).get("revision") or 0) + 1
    active_text = str((active_menu or {}).get("text") or "")

    # TASK-9/10: when the active menu is structured, regenerate only the
    # meals that actually contain the avoided item and preserve the rest,
    # instead of doing string-line surgery on rendered text.
    if intent.wants_no_item and is_structured_menu(active_menu):
        current_meals = structured_meals(active_menu)
        updated_meals, changed = _regenerate_structured_meals(
            current_meals, avoid=intent.wants_no_item, replacement_name=name, calories=calories, protein=protein,
        )
        if changed:
            # Finding 10: the edited menu must pass the SAME deterministic
            # validator generation/repair use — a targeted edit is not a
            # second, unvalidated recommendation path. If the edit itself
            # introduced a hard-fail (e.g. calorie drift is out of tolerance,
            # or the replacement collides with another restriction), do not
            # persist it silently; fall through to the safe legacy text patch
            # and let the user know via the normal reply instead of shipping
            # an invalid structured menu.
            edit_validation = await _validate_edited_menu(db, user_id, updated_meals, profile=edit_profile)
            hard_blocking = {PROBLEM_DISLIKED_FOOD, PROBLEM_RESTRICTION_VIOLATION}
            if not (edit_validation.hard_fail_codes & hard_blocking):
                body = _structured_meals_to_text(updated_meals)
                body += (
                    f"\n\n<b>עדכון לתפריט היומי - גרסה {revision}</b>\n"
                    f"עודכן לפי הבקשה: {esc(intent.instruction)}\n"
                    f"למה זה מתאים: {esc(reason)}."
                )
                if intent.wants_no_item:
                    body += f"\nנשמר כאילוץ להמשך היום: בלי {esc(intent.wants_no_item)}."
                saved_state = await remember_active_daily_menu(
                    db,
                    user_id,
                    text=body,
                    strategy=str((active_menu or {}).get("strategy") or "") or None,
                    revision=revision,
                    source="daily_menu_revision",
                    meals=updated_meals,
                    context_version=str((active_menu or {}).get("context_version") or "") or None,
                )
                menu_id = str(saved_state.get("menu_id") or "")
                # Finding 9: point the confirm button at THIS exact daily-menu
                # meal (by stable menu_id + meal_id), never at whatever the
                # unrelated active next-meal recommendation happens to be.
                first_meal_id = str(updated_meals[0].get("meal_id") or "") if updated_meals else ""
                confirm_cb = (
                    f"dailymenu:save:{menu_id}:{first_meal_id}"
                    if menu_id and first_meal_id
                    else "menu:daily_menu"
                )
                rows = [
                    [("✅ אשר שאכלתי", confirm_cb)],
                    [("🔄 רענן תפריט", "menu:refresh_daily_menu"), ("🍽 מה לאכול עכשיו", "menu:nextmeal")],
                    [("📊 מצב היום", "menu:status")],
                ]
                return body, rows
        # No structured meal actually matched the avoided item, or the edit
        # failed hard validation — fall through to the safe legacy text-based
        # patch below so the user still gets a reply.

    body = _build_revision_text(
        current_text=active_text,
        intent=intent,
        replacement_name=name,
        calories=calories,
        protein=protein,
        reason=reason,
        revision=revision,
    )
    if not active_text:
        body += (
            f"\n\nהצעה ממוקדת במקום הארוחה הזו:{avoid_line}\n"
            "זה עדיין לא נספר כאכילה בפועל. רק אם תאשר שאכלת - זה ייכנס ליומן."
        )
    await remember_active_daily_menu(
        db,
        user_id,
        text=body,
        strategy=str((active_menu or {}).get("strategy") or "") or None,
        revision=revision,
        source="daily_menu_revision",
    )
    # Finding 9: a text-only (legacy/unstructured) revision has no exact meal
    # identity to save against — do not expose a confirm button that would
    # silently save an unrelated next-meal recommendation instead. Route the
    # user to the real, correctly-scoped "what should I eat now" flow.
    rows = [
        [("🍽 מה לאכול עכשיו", "menu:nextmeal")],
        [("🔄 רענן תפריט", "menu:refresh_daily_menu")],
        [("📊 מצב היום", "menu:status")],
    ]
    return body, rows
