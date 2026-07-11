# ruff: noqa: F401, F811, F821, I001
"""Profile, meal analysis and routine extraction services.

Extracted from the legacy composition module. Public names are re-exported
by coach_bot.py for backward compatibility.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import io
import json
import math
import random
import re
import secrets
import shutil
import time
from collections import defaultdict, deque
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from datetime import time as dttime
from pathlib import Path
from typing import Any, Awaitable, Callable

import aiosqlite
import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.constants import ParseMode
from telegram.error import (
    BadRequest,
    NetworkError,
    RetryAfter,
    TimedOut,
)
from telegram.ext import (
    AIORateLimiter,
    Application,
    ApplicationBuilder,
    CallbackContext,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import assistant
import coach_intelligence
import conversation
import data_quality
import event_log
import health_import
import meal_intelligence
import onboarding
import planning
import questions
import recommendations
import reconcile
import targets
import training_intelligence
import user_model

# --- Extracted modules (re-exported for backward compatibility) ---
from config import (  # noqa: F401
    APP_VERSION,
    LOGGER,
    OPENAI_CLIENT,
    RUNTIME_STATE,
    SETTINGS,
    TZ,
    RuntimeState,
    Settings,
)
from db import DB, Database  # noqa: F401
from helpers import _safe_html_block, esc, friendly_error, today_bounds_utc, utc_now  # noqa: F401
from models import (  # noqa: F401
    ClarificationOption,
    FoodItem,
    HealthBatch,
    HealthSample,
    MealAnalysis,
    MealCorrectionResult,
    MiniProfileUpdate,
    RoutineExtraction,
    ShortcutHealthPayload,
    WatchSetPayload,
)
from retention import cleanup_loop as cleanup_photos  # noqa: F401
from retention import (
    cleanup_operational_data_once,  # noqa: F401
    cleanup_photos_once,  # noqa: F401
)

# ---------------------------------------------------------------------------
# The Settings, Database, Pydantic models, and utility functions have been
# extracted to config.py, db.py, models.py, and helpers.py respectively.
# They are re-imported above for backward compatibility.
# ---------------------------------------------------------------------------

from noam_coach.runtime_bind import runtime_bound

RUNTIME_NAMES = ('Any', 'DB', 'Exception', 'LOGGER', 'MealAnalysis', 'OPENAI_CLIENT', 'OVERRIDE_FIELDS', 'PLANS', 'Path', 'ROUTINE_ESTIMATE_KEYS', 'RoutineExtraction', 'RuntimeError', 'SETTINGS', '_enforce_user_quantities', 'analysis', 'base64', 'bytes', 'c', 'code', 'constraints', 'cook_he', 'correction_text', 'description', 'dict', 'encoded', 'exercise_index', 'extraction', 'fact', 'field', 'float', 'idx', 'image_bytes', 'image_path', 'int', 'item', 'items_text', 'json', 'key', 'len', 'lines', 'list', 'locked_block', 'locked_corrections', 'meal_intelligence', 'min', 'parsed', 'plan', 're', 'response', 'row', 'rows', 'str', 'type_suffix', 'unmatched', 'user_id', 'user_model', 'utc_now', 'val', 'value', 'work_type_he')


@runtime_bound(RUNTIME_NAMES)
async def set_exercise_override(
    user_id: int, code: str, exercise_index: int, field: str, value: float
) -> None:
    await DB.execute(
        """
        INSERT INTO exercise_overrides(
            user_id, code, exercise_index, field, value, updated_at
        ) VALUES(?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id, code, exercise_index, field) DO UPDATE SET
            value=excluded.value, updated_at=excluded.updated_at
        """,
        (user_id, code, exercise_index, field, float(value), utc_now()),
    )


@runtime_bound(RUNTIME_NAMES)
async def get_user_plan(user_id: int, code: str) -> dict[str, Any]:
    """Return a deep copy of PLANS[code] with the user's overrides applied.

    Never mutates the global PLANS template.
    """
    plan = json.loads(json.dumps(PLANS[code], ensure_ascii=False))
    rows = await DB.fetch_all(
        "SELECT exercise_index, field, value FROM exercise_overrides WHERE user_id=? AND code=?",
        (user_id, code),
    )
    for row in rows:
        idx = int(row["exercise_index"])
        field = row["field"]
        if 0 <= idx < len(plan["exercises"]) and field in OVERRIDE_FIELDS:
            val = row["value"]
            # sets/rmin/rmax/rest are integers; weight stays float.
            plan["exercises"][idx][field] = val if field == "weight" else int(val)
    return plan


async def _meal_safety_context(user_id: int | None) -> dict[str, Any] | None:
    """RE10-15 / D5: fetch just the safety fields for the first-pass prompts.

    Uses ``get_fact`` (not ``get_value``) so an unanswered allergy/restriction
    question (a gap) is treated as "nothing to report" instead of leaking the
    internal gap-dict into the AI request (same principle as RE10-2).
    """
    if user_id is None:
        return None
    allergy_fact = await user_model.get_fact(DB, user_id, "allergies")
    diet_fact = await user_model.get_fact(DB, user_id, "diet_restrictions")
    allergies = allergy_fact["value"] if allergy_fact and allergy_fact["kind"] != user_model.KIND_GAP else None
    diet = diet_fact["value"] if diet_fact and diet_fact["kind"] != user_model.KIND_GAP else None
    if not allergies and not diet:
        return None
    return {"allergies": allergies, "diet_restrictions": diet}


def _apply_israeli_food_overrides(analysis: MealAnalysis) -> MealAnalysis:
    """RE10-15: align macros to the curated Israeli-foods table on a confident match.

    Only overrides when the AI-returned item name matches a known product —
    a weak/no match leaves the AI's own estimate untouched (better a free-form
    estimate than a wrong deterministic override).
    """
    import israeli_foods

    for item in analysis.items:
        match = israeli_foods.lookup(item.name)
        if match is None:
            continue
        scaled = israeli_foods.scaled_macros(match, item.grams)
        item.name = match.canonical_name
        item.calories = scaled["calories"]
        item.protein = scaled["protein"]
        item.carbs = scaled["carbs"]
        item.fat = scaled["fat"]
    return analysis


@runtime_bound(RUNTIME_NAMES)
async def analyze_meal_image(
    image_bytes: bytes,
    user_id: int | None = None,
    nutrition_context: dict[str, Any] | None = None,
) -> MealAnalysis:
    if not OPENAI_CLIENT:
        raise RuntimeError("OPENAI_API_KEY אינו מוגדר")

    from noam_coach.services.meal_prompts import ISRAELI_LOCALE_BLOCK, safety_context_block
    from noam_coach.services.learned_foods import learned_foods_prompt_block

    safety_context = await _meal_safety_context(user_id)
    learned_user_id = user_id
    if isinstance(nutrition_context, dict):
        try:
            learned_user_id = int(nutrition_context.get("user_id") or 0) or user_id
        except (TypeError, ValueError):
            learned_user_id = user_id
    learned_context = await learned_foods_prompt_block(DB, learned_user_id)
    encoded = base64.b64encode(image_bytes).decode("utf-8")
    response = await OPENAI_CLIENT.responses.parse(
        model=SETTINGS.openai_model,
        input=[
            {
                "role": "system",
                "content": (
                    "Analyze meal photos conservatively. Return Hebrew food names. "
                    "Estimate grams, calories, protein, carbs and fat. "
                    "All gram estimates must refer to COOKED/READY-TO-EAT weight "
                    "(as served on the plate). Append '(מבושל)' to the item name "
                    "for items where cooked vs raw weight matters (rice, pasta, "
                    "meat, legumes). "
                    "Ask at most one clarification ONLY when it materially changes "
                    "the calorie estimate (>15%). The question must be specific to "
                    "what you see in the photo — e.g. 'האם הסלט עם שמן זית או בלי?' "
                    "Never ask generic questions about food types clearly visible "
                    "in the image. Options must include concrete calorie deltas. "
                    "Use learned foods only as supporting recognition evidence; never add foods "
                    "that are not supported by the current image. "
                    "Do not present estimates as medical advice.\n\n"
                    + ISRAELI_LOCALE_BLOCK
                    + safety_context_block(safety_context)
                    + learned_context
                ),
            },
            {
                "role": "user",
                "content": (
                    "Structured nutrition context for this user and day:\n"
                    f"{json.dumps(nutrition_context or {}, ensure_ascii=False)}\n\n"
                    "Evidence precedence: hard allergies/restrictions, explicit current user text, "
                    "deterministic known-food data, approved learned history, image inference, "
                    "then generic estimates. Do not count planned meals as eaten."
                ),
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "זהה את המזון והערך את הכמויות והערכים התזונתיים.",
                    },
                    {
                        "type": "input_image",
                        "image_url": f"data:image/jpeg;base64,{encoded}",
                        "detail": "high",
                    },
                ],
            },
        ],
        text_format=MealAnalysis,
    )
    if not response.output_parsed:
        raise RuntimeError("לא התקבל ניתוח מובנה")
    return _apply_israeli_food_overrides(response.output_parsed)


@runtime_bound(RUNTIME_NAMES)
async def analyze_meal_text(description: str, user_id: int | None = None) -> MealAnalysis:
    """Estimate a meal from a text description only (no photo)."""
    if not OPENAI_CLIENT:
        raise RuntimeError("OPENAI_API_KEY אינו מוגדר")

    from noam_coach.services.meal_prompts import ISRAELI_LOCALE_BLOCK, safety_context_block
    from noam_coach.services.learned_foods import learned_foods_prompt_block

    safety_context = await _meal_safety_context(user_id)
    learned_context = await learned_foods_prompt_block(DB, user_id)
    response = await OPENAI_CLIENT.responses.parse(
        model=SETTINGS.openai_model,
        input=[
            {
                "role": "system",
                "content": (
                    "Estimate a meal from the user's Hebrew text description. "
                    "Return Hebrew food names with grams, calories, protein, "
                    "carbs and fat per item. Be conservative. "
                    "All gram estimates must refer to COOKED/READY-TO-EAT weight. "
                    "Append '(מבושל)' to item names where cooked vs raw matters "
                    "(rice, pasta, meat, legumes). "
                    "Ask at most one clarification ONLY when it materially changes "
                    "the calorie estimate (>15%). The question must reference what "
                    "the user described — e.g. if they said 'סלט' ask 'עם שמן זית "
                    "או בלי?' not 'סלט ירקות או סלט פסטה?'. Options must include "
                    "concrete calorie deltas. Not medical advice.\n\n"
                    + ISRAELI_LOCALE_BLOCK
                    + safety_context_block(safety_context)
                    + learned_context
                ),
            },
            {
                "role": "user",
                "content": f"זה מה שאכלתי: {description}",
            },
        ],
        text_format=MealAnalysis,
    )
    if not response.output_parsed:
        raise RuntimeError("לא התקבל ניתוח מובנה")
    return _apply_israeli_food_overrides(response.output_parsed)


_QUANTITY_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:גרם|גר'?|gr?(?:am)?s?)\s+(.+?)(?:[.,،;]|$)",
    re.IGNORECASE,
)


@runtime_bound(RUNTIME_NAMES)
def _enforce_user_quantities(
    analysis: MealAnalysis, correction_text: str
) -> MealAnalysis:
    """Apply explicit user quantities as immutable hard constraints."""
    constraints = meal_intelligence.parse_locked_quantities(correction_text)
    analysis, unmatched = meal_intelligence.apply_locked_quantities(analysis, constraints)
    if unmatched:
        analysis.notes.extend(
            [f"כמות מפורשת שלא הותאמה אוטומטית: {item.grams:g} גרם {item.food_text}" for item in unmatched]
        )
        analysis.confidence = min(analysis.confidence, 0.65)
    return analysis


@runtime_bound(RUNTIME_NAMES)
async def reanalyze_meal_with_text_and_image(
    image_path: str,
    correction_text: str,
    locked_corrections: list[str] | None = None,
    nutrition_context: dict[str, Any] | None = None,
) -> MealAnalysis:
    """Re-analyse a meal image with a user text correction.

    *locked_corrections* is the list of previous corrections that must remain
    in effect during this re-analysis (REC-PLAN-MEAL-03-12).  They are
    injected into the AI prompt as hard constraints so that, e.g., a prior
    "בלי שמן" is not silently reversed when the user sends a follow-up
    correction.
    """
    if not OPENAI_CLIENT:
        raise RuntimeError("OPENAI_API_KEY אינו מוגדר")

    from noam_coach.services.meal_prompts import ISRAELI_LOCALE_BLOCK
    from noam_coach.services.learned_foods import learned_foods_prompt_block

    learned_user_id = None
    if isinstance(nutrition_context, dict):
        try:
            learned_user_id = int(nutrition_context.get("user_id") or 0) or None
        except (TypeError, ValueError):
            learned_user_id = None
    learned_context = await learned_foods_prompt_block(DB, learned_user_id)

    image_bytes = Path(image_path).read_bytes()
    encoded = base64.b64encode(image_bytes).decode("utf-8")

    # Build locked-corrections context block for the prompt.
    locked_block = ""
    if locked_corrections:
        items_text = "\n".join(f"  - {c}" for c in locked_corrections)
        locked_block = (
            "\n8. PREVIOUS USER CORRECTIONS (hard constraints — never reverse them):\n"
            f"{items_text}\n"
            "These must be respected exactly as if the user had just said them now."
        )

    response = await OPENAI_CLIENT.responses.parse(
        model=SETTINGS.openai_model,
        input=[
            {
                "role": "system",
                "content": (
                    "You receive a meal photo and a Hebrew text description from the user. "
                    "CRITICAL RULES:\n"
                    "0. THE USER'S TEXT IS AUTHORITATIVE. If the text contradicts the image analysis "
                    "(different food, different dish, different ingredients), always trust the text. "
                    "The user knows what they ate; the image analysis may be wrong.\n"
                    "1. If the user specifies an explicit number (grams, weight), that number is a HARD CONSTRAINT — "
                    "use it exactly as given. Do NOT override it with your own estimate from the image.\n"
                    "2. If the user names a specific food, use that name exactly.\n"
                    "3. The image should ONLY be used to fill in information the user did NOT provide "
                    "(e.g., identifying other items on the plate, estimating items without a stated weight).\n"
                    "4. Return Hebrew item names with grams, calories, protein, carbs and fat per item.\n"
                    "5. All gram estimates refer to COOKED/READY-TO-EAT weight. "
                    "Append '(מבושל)' to items where cooked vs raw matters.\n"
                    "6. Do not ask follow-up questions. Do not omit items mentioned by the user.\n"
                    "7. NEGATION means the user is correcting a misidentification. "
                    "'לא אלכוהול' / 'זה לא X' / 'בלי X' means the item is NOT X — "
                    "re-identify it from the image (e.g. a soft drink / iced tea / water). "
                    "Do NOT invent a nonsense name like 'X ללא אלכוהול' and do NOT keep the "
                    "rejected identification. If the drink is genuinely a non-caloric "
                    "beverage, return it with its real (possibly zero) values and a clear name. "
                    "8. Learned foods are calibration only; never restore or add foods that are not "
                    "supported by the current image/text and the user's locked corrections."
                    + locked_block
                    + "\n\n"
                    + ISRAELI_LOCALE_BLOCK
                    + learned_context
                ),
            },
            {
                "role": "user",
                "content": (
                    "Structured nutrition context for this user and day:\n"
                    f"{json.dumps(nutrition_context or {}, ensure_ascii=False)}\n\n"
                    "Use this only for relevant safety context such as allergies, dietary rules, "
                    "fasting/medication flags, and whether today's food logging is incomplete. "
                    "Do not count planned meals as eaten."
                ),
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": (
                            "תיאור המשתמש הוא הסמכותי — הוא יודע מה אכל. "
                            "אם יש סתירה בין התיאור לתמונה, עדיף תמיד על התיאור. "
                            "כל מספר שהמשתמש כתב (גרמים, משקל) הוא מדויק — אסור לשנות אותו. "
                            "השתמש בתמונה רק להשלמת מידע שלא צוין בתיאור. "
                            f"תיאור המשתמש: {correction_text}"
                        ),
                    },
                    {
                        "type": "input_image",
                        "image_url": f"data:image/jpeg;base64,{encoded}",
                        "detail": "high",
                    },
                ],
            },
        ],
        text_format=MealAnalysis,
    )
    if not response.output_parsed:
        raise RuntimeError("לא התקבל ניתוח מתוקן")
    parsed = response.output_parsed
    parsed.question = None
    parsed.options = []
    # RE10-15: align to the curated table BEFORE locking user-stated
    # quantities, so an explicit user correction always wins over the table.
    parsed = _apply_israeli_food_overrides(parsed)
    parsed = _enforce_user_quantities(parsed, correction_text)
    return parsed


@runtime_bound(RUNTIME_NAMES)
async def extract_daily_routine(description: str) -> RoutineExtraction:
    if not OPENAI_CLIENT:
        return RoutineExtraction()
    try:
        response = await OPENAI_CLIENT.responses.parse(
            model=SETTINGS.openai_model,
            input=[
                {
                    "role": "system",
                    "content": (
                        "Extract structured daily routine information from the "
                        "user's Hebrew free-text description. Fill in only fields "
                        "that are explicitly or clearly implied. Leave unknown "
                        "fields as null. Times should be in HH:MM format. "
                        "Do not invent information the user did not provide. "
                        "Do not infer diagnosis, medication dosage, fasting status, "
                        "or medical treatment unless the user explicitly stated it. "
                        "Mark uncertain extracted values with approximate times "
                        "(e.g. '~18:00' for 'around 18'). "
                        "If the user mentions medication-related eating behavior "
                        "(e.g. reduced appetite on certain days), capture it in "
                        "medication_appetite_note as a factual observation, not as "
                        "a medical conclusion."
                    ),
                },
                {
                    "role": "user",
                    "content": f"תיאור יום רגיל של המשתמש:\n{description}",
                },
            ],
            text_format=RoutineExtraction,
        )
        return response.output_parsed or RoutineExtraction()
    except Exception:
        LOGGER.exception("extract_daily_routine AI call failed")
        return RoutineExtraction()


@runtime_bound(RUNTIME_NAMES)
def format_routine_confirmation(extraction: RoutineExtraction) -> str:
    lines = ["<b>זה מה שהבנתי — נכון?</b>", ""]
    if extraction.wake_time:
        lines.append(f"⏰ קימה: {extraction.wake_time}")
    if extraction.sleep_time:
        lines.append(f"🌙 שינה: {extraction.sleep_time}")
    if extraction.work_start and extraction.work_end:
        work_type_he = {
            "office": "משרדית",
            "physical": "פיזית",
            "remote": "מהבית",
            "shifts": "משמרות",
        }.get(extraction.work_type or "", "")
        type_suffix = f" ({work_type_he})" if work_type_he else ""
        lines.append(f"💼 עבודה: {extraction.work_start}–{extraction.work_end}{type_suffix}")
    if extraction.commute_minutes:
        lines.append(f"🚗 נסיעה: ~{extraction.commute_minutes} דקות")
    if extraction.meal_break_time:
        lines.append(f"🍽️ הפסקת אוכל: {extraction.meal_break_time}")
    if extraction.has_fridge_at_work is not None:
        lines.append(f"🧊 מקרר בעבודה: {'כן' if extraction.has_fridge_at_work else 'לא'}")
    if extraction.preferred_workout_time:
        lines.append(f"🏋️ אימון מועדף: {extraction.preferred_workout_time}")
    if extraction.available_workout_minutes:
        lines.append(f"⏱️ זמן לאימון: ~{extraction.available_workout_minutes} דקות")
    if extraction.cooking_willingness:
        cook_he = {
            "none": "כמעט לא",
            "basic": "בסיסי",
            "moderate": "מסתדר",
            "enjoys": "נהנה לבשל",
        }.get(extraction.cooking_willingness, extraction.cooking_willingness)
        lines.append(f"🍳 בישול: {cook_he}")
    if extraction.typical_meals_per_day:
        lines.append(f"🍽️ ארוחות ביום: {extraction.typical_meals_per_day}")
    if extraction.medication_appetite_note:
        lines.append(f"💊 השפעת תרופה על תיאבון: {extraction.medication_appetite_note}")
    if extraction.main_challenges:
        lines.append(f"⚡ אתגרים: {', '.join(extraction.main_challenges)}")
    if len(lines) <= 2:
        return "<b>לא הצלחתי לחלץ מידע מספיק מהתיאור.</b>\nאפשר לנסות שוב או לדלג."
    lines.append("")
    lines.append("אם הכול נכון, אשר. אם משהו לא מדויק — כתוב לי מה לתקן.")
    return "\n".join(lines)


@runtime_bound(RUNTIME_NAMES)
async def save_routine_extraction(user_id: int, extraction: RoutineExtraction) -> None:
    if extraction.work_start and extraction.work_end:
        await user_model.set_fact(
            DB, user_id, "work_schedule",
            {"start": extraction.work_start, "end": extraction.work_end,
             "type": extraction.work_type},
            kind=user_model.KIND_ESTIMATE, source=user_model.SOURCE_DERIVED,
            confidence=0.7, affects=("meal_timing", "workout_timing"),
        )
    if extraction.commute_minutes:
        await user_model.set_fact(
            DB, user_id, "commute_minutes", extraction.commute_minutes,
            kind=user_model.KIND_ESTIMATE, source=user_model.SOURCE_DERIVED,
            confidence=0.7, affects=("workout_timing",),
        )
    if extraction.meal_break_time:
        await user_model.set_fact(
            DB, user_id, "meal_break_info",
            {"break_time": extraction.meal_break_time,
             "has_fridge": extraction.has_fridge_at_work,
             "has_microwave": extraction.has_microwave_at_work},
            kind=user_model.KIND_ESTIMATE, source=user_model.SOURCE_DERIVED,
            confidence=0.7, affects=("meal_timing", "menu_planning"),
        )
    if extraction.preferred_workout_time:
        await user_model.set_fact(
            DB, user_id, "workout_window",
            {"time": extraction.preferred_workout_time,
             "minutes": extraction.available_workout_minutes},
            kind=user_model.KIND_ESTIMATE, source=user_model.SOURCE_DERIVED,
            confidence=0.7, affects=("workout_timing", "workout_schedule"),
        )
    if extraction.cooking_willingness:
        await user_model.set_fact(
            DB, user_id, "cooking_capacity", extraction.cooking_willingness,
            kind=user_model.KIND_ESTIMATE, source=user_model.SOURCE_DERIVED,
            confidence=0.7, affects=("menu_planning",),
        )
    if extraction.wake_time or extraction.sleep_time:
        await user_model.set_fact(
            DB, user_id, "sleep_schedule",
            {"bedtime": extraction.sleep_time, "wake_time": extraction.wake_time},
            kind=user_model.KIND_ESTIMATE, source=user_model.SOURCE_DERIVED,
            confidence=0.7, affects=("meal_timing", "workout_timing", "recovery_tracking"),
        )
    await user_model.set_fact(
        DB, user_id, "daily_routine_summary",
        extraction.model_dump(exclude_none=True),
        kind=user_model.KIND_ESTIMATE, source=user_model.SOURCE_DERIVED,
        confidence=0.7,
        affects=("meal_timing", "workout_timing", "menu_planning"),
    )


ROUTINE_ESTIMATE_KEYS = (
    "work_schedule",
    "commute_minutes",
    "meal_break_info",
    "workout_window",
    "cooking_capacity",
    "sleep_schedule",
    "daily_routine_summary",
)


@runtime_bound(RUNTIME_NAMES)
async def confirm_routine_facts(user_id: int) -> None:
    for key in ROUTINE_ESTIMATE_KEYS:
        fact = await user_model.get_fact(DB, user_id, key)
        if fact and fact["kind"] == user_model.KIND_ESTIMATE:
            await user_model.confirm_fact(DB, user_id, key)


@runtime_bound(RUNTIME_NAMES)
async def discard_unconfirmed_routine_facts(user_id: int) -> None:
    """Discard routine estimates when the user explicitly skips them."""
    for key in ROUTINE_ESTIMATE_KEYS:
        fact = await user_model.get_fact(DB, user_id, key)
        if (
            fact
            and fact["kind"] == user_model.KIND_ESTIMATE
            and not fact.get("confirmed")
        ):
            await user_model.invalidate_fact(DB, user_id, key)
