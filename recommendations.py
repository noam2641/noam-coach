"""AI recommendation layer.

Wraps the OpenAI Responses API (same client/style as ``analyze_meal_image`` in
``coach_bot.py``) to turn the learned routine + the day's numbers into:

* a morning menu tailored to eating routine, today's workout and the calorie/
  protein goal,
* intraday "what to eat next" guidance once a calorie threshold is crossed,
* an end-of-day summary with strengths / improvements and flags for
  calorie-dense or low-protein-ratio foods,
* short, human-feeling motivation lines.

The OpenAI client and model are injected so this module has no global state and
is easy to test. If no client is supplied, deterministic fallbacks are returned
so the bot still works without an API key.
"""

from __future__ import annotations

import json
import logging
import random
from typing import Any

from pydantic import BaseModel, Field

LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Structured output models (parsed via responses.parse(text_format=...))
# ---------------------------------------------------------------------------


class MenuMeal(BaseModel):
    name: str
    time_hint: str = Field(description="When to eat, e.g. 'אחרי האימון'")
    calories: float = Field(ge=0, le=4000)
    protein: float = Field(ge=0, le=400)
    note: str = ""


class MorningMenu(BaseModel):
    headline: str
    meals: list[MenuMeal]
    training_advice: str = ""
    closing: str = ""


class NextMealSuggestion(BaseModel):
    headline: str
    suggestions: list[MenuMeal]
    warning: str = ""


class FoodFlag(BaseModel):
    food: str
    reason: str  # e.g. צפיפות קלורית גבוהה / יחס חלבון-קלוריות נמוך


class EveningSummary(BaseModel):
    headline: str
    strengths: list[str]
    improvements: list[str]
    food_flags: list[FoodFlag] = Field(default_factory=list)
    closing: str = ""


# ---------------------------------------------------------------------------
# Motivation (seeded bank + AI rephrasing so it feels alive, not canned)
# ---------------------------------------------------------------------------

MOTIVATION_SEEDS = [
    "אני יודע שאתה רוצה לאכול את זה. בוא נתרגל: דמיין שהמאכל כבר שלך, אתה לא "
    "אוסר אותו על עצמך — אתה רק דוחה את הקץ. נסה לדחות את האכילה המיותרת "
    "למועד מאוחר יותר.",
    "הכאב זמני, הדאווה נצחית. עוד שעה תודה לעצמך שלא נכנעת.",
    "כל פעם שאתה אומר 'לא' לרעב מיותר, אתה אומר 'כן' לגוף שאתה בונה.",
    "המשמעת היא לזכור מה אתה באמת רוצה, גם כשבא לך משהו אחר עכשיו.",
    "אתה לא חייב להרגיש מוטיבציה כדי לעשות את הדבר הנכון. פשוט תתחיל.",
    "האימון של היום הוא הסיבה שמחר יהיה לך קל יותר. אל תוותר עליו.",
    "רעב הוא לא חירום. תן לו דקה, שתה מים, והוא יירגע.",
    "ההישג הכי גדול הוא להיות עקבי כשאף אחד לא מסתכל.",
    "תחשוב כמה רחוק הגעת, לא כמה נשאר. אתה בכיוון הנכון.",
    "הגוף שלך עובד בשבילך עכשיו — אל תעצור אותו באמצע.",
]


def _client_ready(client: Any) -> bool:
    return client is not None


async def motivation_message(
    client: Any | None,
    model: str,
    context_hint: str = "",
) -> str:
    """Return a motivation line. Uses a random seed, optionally rephrased by AI
    so repeats feel fresh and contextual."""
    seed = random.choice(MOTIVATION_SEEDS)
    if not _client_ready(client):
        return seed
    try:
        response = await client.responses.create(
            model=model,
            input=[
                {
                    "role": "system",
                    "content": (
                        "You are a warm, no-nonsense fitness coach writing in "
                        "Hebrew. Rephrase the given motivational idea into one "
                        "short, fresh, human message (1-2 sentences). Keep the "
                        "core meaning. No emojis unless natural. Do not give "
                        "medical advice."
                    ),
                },
                {
                    "role": "user",
                    "content": (f"רעיון בסיס: {seed}\nהקשר נוכחי: {context_hint or 'אין'}"),
                },
            ],
        )
        text = (response.output_text or "").strip()
        return text or seed
    except Exception:  # noqa: BLE001 - never let motivation crash a job
        return seed


def _profile_block(profile: dict[str, Any]) -> str:
    sleep = profile.get("sleep", {})
    workout = profile.get("workout", {})
    eating = profile.get("eating", {})
    return (
        "שגרה שנלמדה (ממוצעים נעים, חריגים הוסרו):\n"
        f"- שינה: הולך לישון ~{sleep.get('typical_bedtime')}, "
        f"מתעורר ~{sleep.get('typical_wake_time')}, "
        f"משך ~{sleep.get('avg_duration_minutes')} דק'.\n"
        f"- אימונים: ~{workout.get('weekly_frequency')} בשבוע, "
        f"בדרך כלל בשעה ~{workout.get('typical_hour')}, "
        f"~{workout.get('avg_duration_minutes')} דק'.\n"
        f"- אכילה: ארוחה ראשונה ~{eating.get('first_meal_time')}, "
        f"אחרונה ~{eating.get('last_meal_time')}, "
        f"שעות אכילה נפוצות {eating.get('typical_meal_hours')}, "
        f"ממוצע קלוריות יומי ~{eating.get('avg_daily_calories')}."
    )


def _learned_food_names(nutrition_context: dict[str, Any] | None, limit: int = 3) -> list[str]:
    raw = (nutrition_context or {}).get("learned_foods") or []
    names: list[str] = []
    for item in raw:
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
        else:
            name = str(item).strip()
        if name and name not in names:
            names.append(name)
        if len(names) >= limit:
            break
    return names


def _with_learned_food(base: str, learned_names: list[str], index: int) -> str:
    if len(learned_names) <= index:
        return base
    return f"{base} עם {learned_names[index]}"


async def morning_menu(
    client: Any | None,
    model: str,
    profile: dict[str, Any],
    goal: dict[str, Any],
    today_has_workout: bool,
    daily_flags: dict[str, Any] | None = None,
    nutrition_context: dict[str, Any] | None = None,
) -> MorningMenu:
    flags = daily_flags or {}
    if not _client_ready(client):
        cal = goal.get("calories", 2000)
        prot = goal.get("protein", 150)
        first_time = profile.get("eating", {}).get("first_meal_time", "בבוקר") or "בבוקר"
        has_ritalin = flags.get("ritalin")
        is_fasting = flags.get("fasting", False)
        learned_names = _learned_food_names(nutrition_context)
        learned_note = (
            "שילבתי פריטים שמופיעים אצלך הרבה: " + ", ".join(learned_names)
            if learned_names
            else ""
        )
        meals: list[MenuMeal] = []
        notes: list[str] = []

        if is_fasting:
            notes.append("יום צום — לא מציע ארוחות. שתה הרבה מים.")
        elif has_ritalin:
            notes.append("ריטלין — התיאבון יורד. דגש על חלבון בארוחות קטנות.")
            if learned_note:
                notes.append(learned_note)
            meals = [
                MenuMeal(
                    name=_with_learned_food("ארוחה קלה עתירת חלבון", learned_names, 0),
                    time_hint=first_time,
                    calories=cal * 0.25,
                    protein=prot * 0.3,
                    note="קטנה ופשוטה — התיאבון נמוך",
                ),
                MenuMeal(
                    name="ארוחת צהריים עם חלבון",
                    time_hint="צהריים",
                    calories=cal * 0.35,
                    protein=prot * 0.35,
                ),
                MenuMeal(
                    name="ארוחת ערב (כשהתיאבון חוזר)",
                    time_hint="ערב",
                    calories=cal * 0.4,
                    protein=prot * 0.35,
                ),
            ]
        else:
            if learned_note:
                notes.append(learned_note)
            meals = [
                MenuMeal(
                    name=_with_learned_food("ארוחת בוקר עתירת חלבון", learned_names, 0),
                    time_hint=first_time,
                    calories=cal * 0.3,
                    protein=prot * 0.35,
                ),
                MenuMeal(
                    name=_with_learned_food("ארוחת צהריים מאוזנת", learned_names, 1),
                    time_hint="צהריים",
                    calories=cal * 0.35,
                    protein=prot * 0.3,
                ),
                MenuMeal(
                    name=_with_learned_food("ארוחת ערב", learned_names, 2),
                    time_hint="ערב",
                    calories=cal * 0.35,
                    protein=prot * 0.35,
                ),
            ]
        training_advice = "ארוחה עם פחמימות שעה לפני האימון." if today_has_workout else ""
        closing_parts = ["(ללא AI — תפריט בסיס)"] + notes
        return MorningMenu(
            headline="תפריט הבוקר",
            meals=meals,
            training_advice=training_advice,
            closing="\n".join(closing_parts),
        )

    try:
        response = await client.responses.parse(
            model=model,
            input=[
                {
                    "role": "system",
                    "content": (
                        "You are a Hebrew-speaking nutrition & training coach. "
                        "Build a varied daily menu that fits the user's learned "
                        "eating routine, today's training, and the calorie/protein "
                        "goal. Distribute calories across the user's typical meal "
                        "times. Keep it varied day to day. If a flag like Ritalin "
                        "is present, account for reduced appetite (lighter, "
                        "protein-dense, fewer forced meals). No medical advice."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"{_profile_block(profile)}\n\n"
                        f"יעד היום: {goal.get('calories')} קלוריות, "
                        f"{goal.get('protein')} גרם חלבון, "
                        f"שלב: {goal.get('phase')}.\n"
                        f"יש אימון היום: {'כן' if today_has_workout else 'לא'}.\n"
                        f"אינדיקציות בוקר מהמשתמש: {flags or 'אין'}.\n"
                        "בנה תפריט מגוון להיום."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "Structured nutrition context:\n"
                        f"{json.dumps(nutrition_context or {}, ensure_ascii=False)}"
                    ),
                },
            ],
            text_format=MorningMenu,
        )
        if not response.output_parsed:
            raise RuntimeError("לא התקבל תפריט מובנה")
        return response.output_parsed
    except Exception:  # noqa: BLE001
        LOGGER.exception("morning_menu AI call failed, using deterministic fallback")
        return await morning_menu(
            None,
            model,
            profile,
            goal,
            today_has_workout,
            daily_flags,
            nutrition_context,
        )


async def intraday_next_meals(
    client: Any | None,
    model: str,
    profile: dict[str, Any],
    remaining_calories: float,
    remaining_protein: float,
    hours_left_in_day: float,
    today_has_workout: bool,
    daily_flags: dict[str, Any] | None = None,
) -> NextMealSuggestion:
    flags = daily_flags or {}
    if not _client_ready(client):
        has_ritalin = flags.get("ritalin")
        warning = ""
        if remaining_calories < 300 and hours_left_in_day > 4:
            warning = "נותרו מעט קלוריות עם הרבה שעות ביום — נסה לחלק בחוכמה."
        note = "קטנה ועתירת חלבון — התיאבון נמוך" if has_ritalin else ""
        # Split remaining into 1-2 meals based on hours left.
        if hours_left_in_day <= 3 or remaining_calories < 400:
            suggestions = [
                MenuMeal(name="ארוחה אחרונה עתירת חלבון", time_hint="בארוחה הבאה",
                         calories=max(0.0, remaining_calories),
                         protein=max(0.0, remaining_protein), note=note)
            ]
        else:
            half_cal = max(0.0, remaining_calories / 2)
            half_prot = max(0.0, remaining_protein / 2)
            suggestions = [
                MenuMeal(name="ארוחה קרובה", time_hint="בשעה הקרובה",
                         calories=half_cal, protein=half_prot, note=note),
                MenuMeal(name="ארוחה אחרונה", time_hint="ערב",
                         calories=half_cal, protein=half_prot),
            ]
        return NextMealSuggestion(
            headline="המלצה לארוחות הבאות",
            suggestions=suggestions,
            warning=warning,
        )

    try:
        response = await client.responses.parse(
            model=model,
            input=[
                {
                    "role": "system",
                    "content": (
                        "You are a Hebrew-speaking coach giving mid-day guidance. "
                        "Given the calories/protein left, time left in the day, "
                        "the workout and learned routine, suggest the next 1-3 "
                        "meals so the user lands on goal without going hungry late. "
                        "If remaining calories are very low with much day left, "
                        "set a gentle warning. No medical advice."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"{_profile_block(profile)}\n\n"
                        f"נותרו היום: {remaining_calories:.0f} קלוריות, "
                        f"{remaining_protein:.0f} גרם חלבון.\n"
                        f"שעות שנותרו ביום (עד שינה): {hours_left_in_day:.1f}.\n"
                        f"יש אימון היום: {'כן' if today_has_workout else 'לא'}.\n"
                        f"אינדיקציות בוקר: {flags or 'אין'}."
                    ),
                },
            ],
            text_format=NextMealSuggestion,
        )
        if not response.output_parsed:
            raise RuntimeError("לא התקבלה המלצת ארוחות")
        return response.output_parsed
    except Exception:  # noqa: BLE001
        LOGGER.exception("intraday_next_meals AI call failed, using deterministic fallback")
        return await intraday_next_meals(
            None, model, profile, remaining_calories, remaining_protein,
            hours_left_in_day, today_has_workout, daily_flags,
        )


async def evening_summary(
    client: Any | None,
    model: str,
    profile: dict[str, Any],
    goal: dict[str, Any],
    consumed_calories: float,
    consumed_protein: float,
    meals: list[dict[str, Any]],
    daily_flags: dict[str, Any] | None = None,
    nutrition_context: dict[str, Any] | None = None,
) -> EveningSummary:
    flags = daily_flags or {}
    # Pre-compute objective food flags so the model has hard signals to cite.
    computed_flags = compute_food_flags(meals)
    if not _client_ready(client):
        cal_target = goal.get("calories", 2000)
        prot_target = goal.get("protein", 150)
        strengths: list[str] = []
        improvements: list[str] = []
        cal_diff = consumed_calories - cal_target
        prot_diff = consumed_protein - prot_target
        if abs(cal_diff) < cal_target * 0.1:
            strengths.append(f"קלוריות ביעד — {consumed_calories:.0f}/{cal_target}.")
        elif cal_diff < 0:
            improvements.append(f"קלוריות מתחת ליעד ({consumed_calories:.0f}/{cal_target}).")
        else:
            improvements.append(f"חריגה קלורית ({consumed_calories:.0f}/{cal_target}).")
        if prot_diff >= 0:
            strengths.append(f"חלבון ביעד — {consumed_protein:.0f}/{prot_target} ג'.")
        else:
            improvements.append(f"חלבון מתחת ליעד ({consumed_protein:.0f}/{prot_target} ג').")
        return EveningSummary(
            headline="סיכום היום",
            strengths=strengths or [f"נצרכו {consumed_calories:.0f} קלוריות."],
            improvements=improvements,
            food_flags=[FoodFlag(food=f["food"], reason=f["reason"]) for f in computed_flags],
        )

    try:
        response = await client.responses.parse(
            model=model,
            input=[
                {
                    "role": "system",
                    "content": (
                        "You are a Hebrew-speaking coach writing a short, honest "
                        "end-of-day review. Say where the user was strong and what "
                        "to improve. Use the pre-computed food flags (calorie-dense "
                        "or low protein-to-calorie ratio foods that can hurt fat "
                        "loss or training) and explain them simply. Encouraging but "
                        "truthful. No medical advice."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"{_profile_block(profile)}\n\n"
                        f"יעד: {goal.get('calories')} קל' / "
                        f"{goal.get('protein')} ג' חלבון.\n"
                        f"נצרך בפועל: {consumed_calories:.0f} קל' / "
                        f"{consumed_protein:.0f} ג' חלבון.\n"
                        f"ארוחות היום: {[m.get('name') for m in meals]}\n"
                        f"דגלי מזון שחושבו: {computed_flags}\n"
                        f"אינדיקציות בוקר: {flags or 'אין'}."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "Structured nutrition context:\n"
                        f"{json.dumps(nutrition_context or {}, ensure_ascii=False)}"
                    ),
                },
            ],
            text_format=EveningSummary,
        )
        if not response.output_parsed:
            raise RuntimeError("לא התקבל סיכום יומי")
        return response.output_parsed
    except Exception:  # noqa: BLE001
        LOGGER.exception("evening_summary AI call failed, using deterministic fallback")
        return await evening_summary(
            None, model, profile, goal, consumed_calories, consumed_protein, meals, daily_flags,
            nutrition_context,
        )


def compute_food_flags(
    meals: list[dict[str, Any]],
    dense_kcal_per_100g: float = 350.0,
    low_protein_ratio_g_per_100kcal: float = 5.0,
) -> list[dict[str, str]]:
    """Objective, deterministic flags from meal/meal_item rows.

    * calorie-dense: >= ``dense_kcal_per_100g`` kcal per 100g.
    * low protein ratio: < ``low_protein_ratio_g_per_100kcal`` g protein per
      100 kcal (i.e. lots of calories, little protein).
    """
    flags: list[dict[str, str]] = []
    for meal in meals:
        name = meal.get("name", "מאכל")
        grams = float(meal.get("grams") or 0.0)
        calories = float(meal.get("calories") or 0.0)
        protein = float(meal.get("protein") or 0.0)
        if grams > 0 and calories > 0:
            density = calories / grams * 100.0
            if density >= dense_kcal_per_100g:
                flags.append(
                    {
                        "food": name,
                        "reason": (f"צפיפות קלורית גבוהה (~{density:.0f} קל' ל-100 גרם)"),
                    }
                )
        if calories >= 150:
            ratio = protein / calories * 100.0
            if ratio < low_protein_ratio_g_per_100kcal:
                flags.append(
                    {
                        "food": name,
                        "reason": (f"יחס חלבון-קלוריות נמוך (~{ratio:.1f} ג' חלבון ל-100 קל')"),
                    }
                )
    return flags
