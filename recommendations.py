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

import routine

LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Structured output models (parsed via responses.parse(text_format=...))
# ---------------------------------------------------------------------------


class MenuIngredient(BaseModel):
    """One structured food component of a MenuMeal (Finding 8).

    Generation must expose real food identity, not only a free-text
    ``note`` — the daily-menu validator and the targeted editor ("בלי ביצים")
    need to know a meal actually CONTAINS eggs, not guess it from whether the
    word "ביצים" happens to appear in a rendered sentence.
    """

    name: str
    grams: float | None = None
    calories: float | None = None
    protein: float | None = None
    carbs: float | None = None
    fat: float | None = None


class MenuMeal(BaseModel):
    name: str
    time_hint: str = Field(description="When to eat, e.g. 'אחרי האימון'")
    calories: float = Field(ge=0, le=4000)
    protein: float = Field(ge=0, le=400)
    note: str = ""
    # TASK-8 Finding 2: when meal_intents are supplied, the AI must echo back
    # which code-defined intent this meal fills, so validation/repair can
    # check the meal against the SAME intent object the generation contract
    # gave it — never re-derived from the generated meal's free-text name.
    intent_id: str = ""
    # Finding 8: structured food components, in addition to (not replacing)
    # the human-readable `note`. Empty for the deterministic (no-AI)
    # fallback and for legacy callers that never populate it.
    ingredients: list[MenuIngredient] = Field(default_factory=list)


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


def _eating_line(eating: Any) -> str:
    """The learned-eating line of the AI prompt block (W1-20).

    A learned eating window only reaches the menu-generating AI as authoritative
    routine when ``routine.eating_window_is_trustworthy`` says so. A degenerate
    window (``first_meal_time == last_meal_time``, the single-logged-day
    artifact) or one backed by too few meals used to be interpolated raw here,
    so the AI planned a whole day of meals around a zero-width eating window it
    had no reason to doubt.

    The evidence is not erased — ``avg_daily_calories`` is still reported when
    present, because the calorie average does not depend on the window's width,
    and the line says plainly that the window itself is not yet known. Absent
    and weak stay distinguishable: they produce different text.

    A non-dict ``eating`` is treated as no evidence rather than raising. Both
    call sites coerce with ``or {}``, which covers None/""/0 but NOT a *truthy*
    non-dict — a corrupted or legacy ``routine_profile.profile`` blob whose
    ``eating`` is a string or list would reach ``.get`` and raise. This runs on
    the morning_menu hot path, where an AttributeError would be a worse
    regression than the window defect this function exists to fix, so the
    coercion happens here (mirroring the isinstance handling the gate itself
    does in ``routine.eating_window_is_trustworthy``) instead of relying on
    every caller to have gotten its own coercion right.
    """
    if not isinstance(eating, dict):
        eating = {}
    calories = eating.get("avg_daily_calories")
    calorie_part = (
        f" ממוצע קלוריות יומי ~{calories}." if calories is not None else ""
    )
    if routine.eating_window_is_trustworthy(eating):
        return (
            f"- אכילה: ארוחה ראשונה ~{eating.get('first_meal_time')}, "
            f"אחרונה ~{eating.get('last_meal_time')}, "
            f"שעות אכילה נפוצות {eating.get('typical_meal_hours')},"
            f"{calorie_part or ' ממוצע קלוריות יומי לא ידוע.'}"
        )
    if not eating or not eating.get("meals_sampled"):
        return (
            "- אכילה: אין עדיין נתוני שגרת אכילה נלמדת — אל תניח שעות ארוחה, "
            "קבע אותן לפי ההיגיון התזונתי." + calorie_part
        )
    return (
        "- אכילה: שגרת האכילה עדיין לא נלמדה במידה מספקת "
        f"(מבוסס על {eating.get('meals_sampled')} ארוחות בלבד) — "
        "אל תתייחס לשעות שנצפו כשגרה קבועה, קבע שעות לפי ההיגיון התזונתי."
        + calorie_part
    )


def _profile_block(profile: dict[str, Any]) -> str:
    sleep = profile.get("sleep", {})
    workout = profile.get("workout", {})
    # No `or {}` needed: _eating_line coerces any non-dict itself (W1-20 F1),
    # so the safety lives in one place rather than at each call site.
    eating = profile.get("eating")
    from noam_coach.services import coaching_day

    return (
        "שגרה שנלמדה (ממוצעים נעים, חריגים הוסרו):\n"
        f"- שינה: הולך לישון ~{coaching_day.sleep_bedtime(sleep)}, "
        f"מתעורר ~{coaching_day.sleep_wake_time(sleep)}, "
        f"משך ~{sleep.get('avg_duration_minutes')} דק'.\n"
        f"- אימונים: ~{workout.get('weekly_frequency')} בשבוע, "
        f"בדרך כלל בשעה ~{workout.get('typical_hour')}, "
        f"~{workout.get('avg_duration_minutes')} דק'.\n"
        f"{_eating_line(eating)}"
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
    meal_intents: list[dict[str, Any]] | None = None,
) -> MorningMenu:
    """Generate the daily menu.

    ``meal_intents`` (TASK-8 Finding 2), when supplied, is the CODE-defined
    generation contract from ``noam_coach.services.meal_intent`` — one dict
    per ``MealIntent.ai_payload()``. When present it is authoritative: the AI
    must compose exactly one meal per intent, matched by ``intent_id``, using
    that intent's calorie/protein target and role rather than inventing its
    own day allocation. The deterministic (no-AI) fallback below also uses it
    when available, instead of an arbitrary fixed 0.3/0.35/0.35 split of the
    RAW daily target (which double-counts already-consumed calories).
    """
    flags = daily_flags or {}
    if not _client_ready(client):
        cal = goal.get("calories", 2000)
        prot = goal.get("protein", 150)
        # W1-20: the same read-path gate as the AI prompt block. A learned
        # first-meal time is only used as this menu's breakfast hint when the
        # window it came from is trustworthy; otherwise the pre-existing
        # generic "בבוקר" default stands, exactly as it does for a user with no
        # learned routine at all.
        # A non-dict `eating` (corrupted/legacy profile blob) fails the gate's
        # own isinstance check, so `.get` is never reached on one — but coerce
        # anyway so this stays true if the gate is ever reordered (W1-20 F1).
        _eating = profile.get("eating")
        if not isinstance(_eating, dict):
            _eating = {}
        first_time = (
            _eating.get("first_meal_time")
            if routine.eating_window_is_trustworthy(_eating)
            else None
        ) or "בבוקר"
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
        elif meal_intents:
            # TASK-8: build the fallback from the SAME code-defined intents
            # (already consumed-aware remaining budget) instead of dividing
            # the raw daily target — Finding 1 fix applies to the fallback
            # path too, since it is what deterministic repair also uses.
            if learned_note:
                notes.append(learned_note)
            if has_ritalin:
                notes.append("ריטלין — התיאבון יורד. דגש על חלבון בארוחות קטנות.")
            for index, intent in enumerate(meal_intents):
                meals.append(
                    MenuMeal(
                        name=_with_learned_food(str(intent.get("role_label") or "ארוחה"), learned_names, index),
                        time_hint=f"{int(intent.get('approx_hour') or 0):02d}:00",
                        calories=max(0.0, float(intent.get("calorie_target") or 0)),
                        protein=max(0.0, float(intent.get("protein_target") or 0)),
                        intent_id=str(intent.get("intent_id") or ""),
                    )
                )
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

    intents_block = ""
    if meal_intents:
        intents_block = (
            "\n\nCODE-DEFINED MEAL INTENTS (authoritative — this is the day's "
            "real eating-opportunity plan, already computed from the "
            "remaining calorie/protein budget after accounting for what the "
            "user already ate today; it is NOT the raw full-day target). You "
            "MUST produce exactly one meal per intent below, in the same "
            "order, each meal's 'intent_id' field set to that intent's "
            "intent_id. Use each intent's calorie_target/protein_target "
            "(the calorie_min/calorie_max is the acceptable range) as the "
            "meal's nutrition — do not invent a different day-wide split. "
            "'workout_relationship' and 'digestion_requirement' describe the "
            "meal's role relative to today's training; 'familiar_food_names' "
            "are foods this user actually eats around that slot — prefer "
            "them. Do not add, drop, or reorder meals relative to this list:\n"
            f"{json.dumps(meal_intents, ensure_ascii=False)}"
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
                        "protein-dense, fewer forced meals). No medical advice.\n"
                        # TASK-20: build the menu around the user's ACTUAL day.
                        "Optimize for real-life adherence, not only nutrition: "
                        "prefer practical, realistic meals for each time of day "
                        "(don't force grilled chicken breast at 08:00 unless the "
                        "user's history clearly supports it). Breakfast is OPTIONAL "
                        "— if the food-routine context suggests breakfast is often "
                        "skipped, redistribute those calories instead of forcing an "
                        "early meal. Use the 'day_type' field: on 'friday'/"
                        "'saturday' (the Israeli weekend) adapt naturally to a "
                        "different rhythm than a workday. Respect the food "
                        "environment (cooking availability, restaurants, takeaway, "
                        "quick meals) when choosing meals.\n"
                        # TASK-5: personalize to the user's actual learned foods.
                        "The context includes 'learned_foods' with a "
                        "'usual_meal_slot' (breakfast/lunch/afternoon/dinner/late) "
                        "and 'meal_slot_counts'. When history exists, build meals "
                        "PRIMARILY from these familiar foods and realistic "
                        "variations, and place each learned food in the meal slot "
                        "it is actually eaten in — do NOT put a food the user only "
                        "eats at dinner into an 08:00 breakfast just because its "
                        "macros fit. Use new foods only to fill gaps the learned "
                        "foods cannot cover, for variety, or when explicitly asked.\n"
                        # TASK-7: schedule + target-total consistency.
                        "Format each meal for a chronological schedule: put a real "
                        "clock time (HH:MM) in 'time_hint', a short meal-context in "
                        "'name' (e.g. 'ארוחת בוקר'/'ארוחת צהריים'/'ארוחת ביניים'/"
                        "'ארוחת ערב'/'קדם אימון'/'אחרי אימון' — never 'ארוחה 1/2/3'), "
                        "and the actual food components in 'note'. Additionally, "
                        "populate the structured 'ingredients' list for every meal "
                        "with each real food component (name, and grams/calories/"
                        "protein/carbs/fat when known) — do not leave it empty when "
                        "the meal is composed of identifiable foods; this is what "
                        "lets downstream code detect e.g. 'this meal contains eggs' "
                        "without re-parsing 'note', and log real per-ingredient "
                        "macros when the meal is saved as eaten, not just an "
                        "aggregate calorie/protein total. The SUM of the "
                        "meals' calories must match the daily calorie target and the "
                        "SUM of protein must match the protein target (within ~5%). "
                        "Do not add a generic closing sentence.\n"
                        # TASK-8 Finding 2: when meal_intents are supplied they
                        # are the authoritative allocation — see the user
                        # message block below; do not re-derive your own.
                        + ("When CODE-DEFINED MEAL INTENTS are provided in the "
                           "user message, they override the general guidance "
                           "above about distributing calories: follow them "
                           "exactly, one meal per intent." if meal_intents else "")
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
                        f"{intents_block}"
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
            meal_intents,
        )


async def repair_menu_meals(
    client: Any | None,
    model: str,
    *,
    menu: "MorningMenu",
    affected_meal_indices: list[int],
    violations_by_index: dict[int, list[str]],
    immutable_constraints: dict[str, Any],
    meal_intents: list[dict[str, Any]] | None = None,
) -> "MorningMenu":
    """ONE bounded targeted AI repair call (Finding 6).

    Sends ONLY the flagged meals (by intent_id/index) plus the exact
    violation reasons and immutable constraints (hard exclusions, allergies,
    calorie/protein target) to the model, and asks it to replace ONLY those
    meals. Unaffected meals are never included in the request and are spliced
    back in by the caller — this function does not touch them. If the client
    is unavailable or the call fails, the caller is responsible for falling
    back to the deterministic path; this function raises rather than
    silently degrading, so "no real repair happened" is never disguised as
    one.
    """
    if not _client_ready(client) or not affected_meal_indices:
        raise RuntimeError("no AI client available or nothing to repair")

    affected_meals = [
        {
            "index": index,
            "intent_id": str(getattr(menu.meals[index], "intent_id", "") or ""),
            "current": {
                "name": menu.meals[index].name,
                "time_hint": menu.meals[index].time_hint,
                "calories": menu.meals[index].calories,
                "protein": menu.meals[index].protein,
                "note": menu.meals[index].note,
            },
            "violations": violations_by_index.get(index, []),
        }
        for index in affected_meal_indices
        if 0 <= index < len(menu.meals)
    ]
    intent_by_id = {str(intent.get("intent_id")): intent for intent in (meal_intents or [])}
    affected_intents = [
        intent_by_id[meal["intent_id"]]
        for meal in affected_meals
        if meal["intent_id"] in intent_by_id
    ]

    response = await client.responses.parse(
        model=model,
        input=[
            {
                "role": "system",
                "content": (
                    "You are repairing SPECIFIC flagged meals in an existing "
                    "Hebrew daily nutrition menu. You are given the exact "
                    "meals that violated a constraint and why. Replace ONLY "
                    "those meals (same count, same order they were given in). "
                    "Absolutely never reintroduce any food in "
                    "'hard_excluded_foods' or 'allergies' below, in any form "
                    "or morphological variant. Respect each meal's original "
                    "meal_intent (calorie_min/calorie_max/protein_target/"
                    "role/workout_relationship) when provided. Return exactly "
                    "one meal per input meal, in the same order, each with "
                    "its original 'intent_id' echoed back."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "immutable_constraints": immutable_constraints,
                        "meals_to_repair": affected_meals,
                        "meal_intents_for_these_meals": affected_intents,
                    },
                    ensure_ascii=False,
                ),
            },
        ],
        text_format=MorningMenu,
    )
    if not response.output_parsed or not response.output_parsed.meals:
        raise RuntimeError("repair call returned no meals")
    repaired_meals = response.output_parsed.meals
    if len(repaired_meals) != len(affected_meals):
        raise RuntimeError("repair call returned a different meal count than requested")

    result_meals = list(menu.meals)
    for position, index in enumerate(
        idx for idx in affected_meal_indices if 0 <= idx < len(menu.meals)
    ):
        result_meals[index] = repaired_meals[position]
    return MorningMenu(
        headline=menu.headline,
        meals=result_meals,
        training_advice=menu.training_advice,
        closing=menu.closing,
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
