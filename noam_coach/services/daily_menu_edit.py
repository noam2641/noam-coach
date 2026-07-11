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
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from config import TZ
from helpers import esc
from noam_coach.services.learned_foods import learned_foods_from_meals


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
_CHANGE_TOKENS = ("תחליף", "החלף", "תשנה", "שנה", "רענן", "תעדכן", "לעדכן", "להחליף")
_PROTEIN_TOKENS = ("חלבון", "protein", "גבינה", "יוגורט", "טונה", "עוף", "ביצה")
_LIGHT_TOKENS = ("קל", "קלה", "קטן", "קטנה", "פחות", "דל", "דליל")


def parse_daily_menu_edit(text: str) -> DailyMenuEditIntent | None:
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
    if not (has_change and mentions_menu):
        return None
    slot = "snack"
    for candidate, aliases in _SLOT_ALIASES:
        if any(alias in normalized for alias in aliases):
            slot = candidate
            break
    no_item = None
    match = re.search(r"(?:בלי|ללא|אל תשים)\s+([^,.!?\n]+)", normalized)
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


def _learned_choice(foods: list[Any], *, wants_protein: bool, avoid: str | None) -> Any | None:
    avoid_text = str(avoid or "").lower()
    candidates = []
    for food in foods:
        name = str(getattr(food, "display_name", "") or "")
        if avoid_text and avoid_text in name.lower():
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


async def try_build_daily_menu_edit_reply(
    db: Any,
    user_id: int,
    text: str,
) -> tuple[str, list[list[tuple[str, str]]]] | None:
    """Return a menu-edit reply and buttons, or None for unrelated text."""
    intent = parse_daily_menu_edit(text)
    if intent is None:
        return None
    foods = await learned_foods_from_meals(db, user_id, limit=8, min_count=1)
    learned = _learned_choice(foods, wants_protein=intent.wants_protein, avoid=intent.wants_no_item)
    if learned is not None:
        name = str(getattr(learned, "display_name", "הפריט המוכר") or "הפריט המוכר")
        calories = int(round(float(getattr(learned, "avg_calories", 0) or 0)))
        protein = int(round(float(getattr(learned, "avg_protein", 0) or 0)))
        reason = "בחרתי פריט שכבר אישרת בעבר, ולכן הוא מתאים יותר להעדפות שלך"
    else:
        name, calories, protein, reason = _fallback_suggestion(intent)
    await _remember_request(db, user_id, intent)
    avoid_line = f"\nהסרתי/נמנעתי מ: {esc(intent.wants_no_item)}" if intent.wants_no_item else ""
    body = (
        f"<b>עדכון לתפריט היומי</b>\n\n"
        f"הבנתי שאתה רוצה לשנות את {esc(intent.slot_label)}.\n"
        f"הצעה ממוקדת במקום הארוחה הזו:\n"
        f"<b>{esc(name)}</b>\n"
        f"≈{calories} קל׳ | ≈{protein} ג׳ חלבון{avoid_line}\n\n"
        f"למה זה מתאים: {esc(reason)}.\n\n"
        "זה עדיין לא נספר כאכילה בפועל. רק אם תאשר שאכלת — זה ייכנס ליומן."
    )
    rows = [
        [("✅ אשר שאכלתי", "nextmeal:save:1")],
        [("🔄 רענן תפריט", "menu:refresh_daily_menu"), ("🍽 מה לאכול עכשיו", "menu:nextmeal")],
        [("📊 מצב היום", "menu:status")],
    ]
    return body, rows
