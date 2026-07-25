"""Onboarding flow — content and stage management.

Implements chapters 1-2 + the data-confirmation screens (chapter 5) as a small
state machine whose current stage is stored in the user model (so it survives
restarts). Telegram rendering lives in ``coach_bot.py``; this module owns the
stages, transitions, and message text so they are easy to adjust and test.
"""

from __future__ import annotations

from typing import Any

import user_model
from noam_coach.services import coaching_day

STAGE_KEY = "onboarding_stage"

# Stages
S_NONE = "none"  # not started
S_OPEN = "open"  # intro shown, waiting for ZIP or choice
S_EXPORT_HELP = "export_help"  # showed export instructions
S_CONFIRM_BASICS = "confirm_basics"  # showed measured summary
S_CONFIRM_PATTERNS = "confirm_patterns"  # showed inferred patterns
S_SAFETY = "safety"  # safety gate
S_PLAN_QUESTIONS = "plan_questions"  # essential plan questions
S_DONE = "done"


INTRO_TEXT = (
    "היי נועם 👋\n\n"
    "אני אבנה עבורך פרופיל אישי שישתפר ככל שאכיר אותך. "
    "המידע משמש להתאמת האימונים, התזונה והמעקב שלך.\n\n"
    "אני לא תחליף לרופא — לא מאבחן, לא משנה תרופות ולא נותן אישור רפואי "
    "להתאמן. אם יעלה סימן אזהרה, אפנה אותך לבדיקה מקצועית.\n\n"
    "נתחיל מנתוני Apple Health, או שנוכל להתחיל גם בלעדיהם.\n"
    "פשוט שלח לי את קובץ ה-ZIP, או בחר:"
)

EXPORT_HELP_TEXT = (
    "<b>איך מייצאים נתונים מה-iPhone</b>\n\n"
    "1. פתח את אפליקציית <b>בריאות</b> באייפון.\n"
    "2. לחץ על תמונת הפרופיל / ראשי התיבות שלך.\n"
    "3. בחר <b>ייצוא כל נתוני הבריאות</b>.\n"
    "4. המתן להכנת הקובץ.\n"
    "5. שמור אותו בקבצים או שתף אותו לכאן.\n"
    "6. אין צורך לפתוח או לחלץ את ה-ZIP בעצמך — אני אדאג לזה.\n\n"
    "כשמוכן, פשוט שלח לי את הקובץ כאן."
)

NO_DATA_TEXT = (
    "אין בעיה, נתחיל בלי הנתונים כרגע. נסמן את מה שחסר, ונבסס את התוכנית על "
    "מה שתספר לי. תמיד אפשר לשלוח ייצוא Apple Health בהמשך עם /import."
)

LATER_TEXT = "סבבה, נמשיך בלי הקובץ בינתיים. כשתרצה לשלוח ייצוא — פשוט שלח אותו או הקש /import."


async def get_stage(db: user_model.SupportsDB, user_id: int) -> str:
    return await user_model.get_value(db, user_id, STAGE_KEY, S_NONE)


async def set_stage(db: user_model.SupportsDB, user_id: int, stage: str) -> None:
    await user_model.set_fact(
        db,
        user_id,
        STAGE_KEY,
        stage,
        kind=user_model.KIND_FACT,
        source=user_model.SOURCE_SYSTEM,
        confidence=1.0,
        confirmed=True,
        affects=("onboarding",),
    )


async def is_onboarding(db: user_model.SupportsDB, user_id: int) -> bool:
    stage = await get_stage(db, user_id)
    return stage not in (S_NONE, S_DONE)


def basics_summary(profile_view: dict[str, list[dict[str, Any]]], extras: dict[str, Any]) -> str:
    """Build the chapter-5 'what I identified' screen from measured facts."""
    lines = ["<b>מה זיהיתי עד כה</b>", ""]
    measured = {f["key"]: f["value"] for f in profile_view.get("measured", [])}

    def show(label: str, value: Any, unit: str = "") -> None:
        if value is None:
            lines.append(f"{label}: לא נמצא")
        else:
            lines.append(f"{label}: {value}{unit}")

    show("גובה", measured.get("height_cm"), ' ס"מ')
    show("משקל אחרון", measured.get("weight_kg"), ' ק"ג')
    if extras.get("weight_range"):
        lo, hi = extras["weight_range"]
        lines.append(f'טווח משקל: {lo}–{hi} ק"ג')
    if extras.get("weight_trend_90d") is not None:
        lines.append(f"מגמה ב-90 יום: {extras['weight_trend_90d']}")
    show("ממוצע צעדים", measured.get("avg_steps"), " ביום")
    if extras.get("avg_sleep"):
        lines.append(f"שינה ממוצעת: {extras['avg_sleep']}")
    if extras.get("weekly_workouts") is not None:
        lines.append(f"אימונים מזוהים: {extras['weekly_workouts']} בשבוע")
    lines.append("")
    lines.append("הכול נכון?")
    return "\n".join(lines)


def patterns_text(profile: dict[str, Any]) -> tuple[str, list[dict[str, str]]]:
    """Build chapter-5 'patterns I inferred' screen + the confirmable items.

    Returns (text, items) where each item has id/text/display_label for buttons.
    The display_label is the friendly Hebrew name shown on confirmation buttons
    instead of the internal key (REC-ONBOARD-02-03).
    """
    sleep = profile.get("sleep") or {}
    workout = profile.get("workout") or {}
    eating = profile.get("eating") or {}
    items: list[dict[str, str]] = []

    if workout.get("typical_hour"):
        days_text = ""
        day_indices = workout.get("common_weekdays") or []
        if day_indices:
            from noam_coach.services.weekdays import sunday_first_order, weekday_he

            names = [weekday_he(d) for d in sunday_first_order(day_indices)]
            days_text = f", בימים {', '.join(names)}"
        items.append(
            {
                "id": "workout_pattern",
                "display_label": user_model.display_label("workout_pattern"),
                "text": (
                    f"רוב האימונים שלך סביב {workout['typical_hour']}, "
                    f"~{workout.get('weekly_frequency')} בשבוע{days_text}."
                ),
            }
        )
    _sleep_bedtime = coaching_day.sleep_bedtime(sleep)
    if _sleep_bedtime:
        items.append(
            {
                "id": "sleep_schedule",
                "display_label": user_model.display_label("sleep_schedule"),
                "text": (
                    f"שינה טיפוסית ~{_sleep_bedtime}–{coaching_day.sleep_wake_time(sleep)}."
                ),
            }
        )
    if eating.get("typical_meal_hours"):
        items.append(
            {
                "id": "eating_windows",
                "display_label": user_model.display_label("eating_windows"),
                "text": (f"שעות אכילה נפוצות: {', '.join(eating['typical_meal_hours'])}."),
            }
        )

    lines = ["<b>דפוסים אפשריים שזיהיתי</b>", ""]
    if not items:
        lines.append("עוד לא הצטברו מספיק נתונים כדי לזהות דפוסים ברורים. נמשיך ונלמד תוך כדי.")
    else:
        for item in items:
            lines.append(f"• {item['text']}")
        lines.append("")
        lines.append("אלה נכונים? אשר כל אחד או סמן לתיקון.")
    return "\n".join(lines), items
