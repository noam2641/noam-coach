# ruff: noqa: F401, F811, F821, I001
"""Natural-language routing, weekly summaries and charts.

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

RUNTIME_NAMES = ('Any', 'ContextTypes', 'DB', 'Exception', 'InlineKeyboardMarkup', 'LOGGER', 'OPENAI_CLIENT', 'PLANS', 'ParseMode', 'SETTINGS', 'TZ', 'TypeError', 'Update', 'ValueError', '_clean_pref_item', '_re', 'abs', 'action', 'active_session', 'actual_workouts', 'adherence', 'all_medication_names', 'already', 'analysis', 'analyze_meal_text', 'any', 'applied', 'approval_id', 'ask_deferred_for_plan', 'assistant', 'assistant_profile_summary', 'at', 'avg_calories', 'avg_protein', 'ax', 'banner', 'bare_number', 'body', 'bool', 'bot', 'buffer', 'build_evening_summary_text', 'build_morning_menu_text', 'build_next_meal_text', 'build_profile_text', 'build_progress_text', 'build_weekly_plan', 'build_weekly_summary_text', 'button', 'calorie_ratio', 'calorie_score', 'calories', 'chat_id', 'check_plan_readiness', 'cleaned', 'code', 'complete_calories', 'complete_day_count', 'complete_days', 'complete_protein', 'completeness', 'constraint_banner', 'context', 'conversation', 'create_approval', 'current_median', 'current_weights', 'datetime', 'day', 'day_key', 'day_scores', 'defaultdict', 'delta', 'dict', 'direction', 'dttime', 'duplicate_rows', 'eaten', 'end_local', 'end_utc', 'ensure_user', 'enumerate', 'esc', 'event_log', 'event_status', 'exc', 'exercise_data', 'existing', 'expected_meals', 'extra', 'f', 'fact_key', 'fatigue_banner', 'fetch_goal', 'fig', 'flag', 'flags', 'float', 'follow', 'format_weekly_plan', 'freq', 'friendly_error', 'gaps', 'get_daily_flags', 'get_user_plan', 'goal', 'gw', 'hh', 'hi', 'index', 'int', 'intent', 'io', 'is_allowed', 'is_sensitive', 'isinstance', 'item', 'kb', 'key', 'kind', 'known', 'label', 'len', 'lines', 'list', 'lo', 'load_routine_profile', 'local_date', 'log_meal_from_text', 'math', 'max', 'meal_days', 'meals_by_day', 'meals_data', 'measured', 'med_name', 'median', 'medication_name_from_text', 'message', 'min', 'minimum_complete_meals', 'mm', 'muscle', 'muscle_tag', 'new_value', 'note', 'now', 'now_local', 'nowh', 'num', 'ok', 'p', 'parts', 'plan', 'planned_workouts', 'planning', 'plt', 'points', 'prefix', 'prev', 'previous_median', 'previous_start_utc', 'previous_weights', 'profile', 'progress', 'protein', 'protein_ratio', 'q', 'questions', 'recommend_load', 'record_dietary_preference', 'record_medication', 'render_meal', 'render_workout_overview_reply', 'reps', 'reversed', 'row', 'rows', 'save_medical_constraint', 'select_todays_workout_code', 'send', 'send_weight_chart', 'session', 'set_confirm_pending', 'set_daily_flags', 'set_goal_weight', 'set_pending', 'slots', 'source_label', 'start_local', 'start_utc', 'status', 'str', 'structure', 'sum', 'summary', 'target', 'target_calories', 'target_change_note', 'text', 'timedelta', 'timezone', 'today', 'today_bounds_utc', 'total_cal', 'total_prot', 'track_event', 'trend', 'update', 'user_id', 'user_model', 'utc_now', 'val', 'val_display', 'value', 'week_end', 'week_start', 'weight', 'weights', 'wk', 'workout_completed_today', 'workout_hour', 'workout_overview_keyboard', 'workout_plan', 'workout_rows', 'write_audit', 'x', 'xs', 'ys')


@runtime_bound(RUNTIME_NAMES)
async def assistant_profile_summary(user_id: int) -> str:
    """Compact context handed to the intent classifier."""
    goal = await user_model.get_value(DB, user_id, "primary_goal", "—")
    weight = await user_model.get_value(DB, user_id, "weight_kg", "—")
    freq = await user_model.get_value(DB, user_id, "training_days_per_week", "—")
    return f"מטרה={goal}, משקל={weight}, אימונים/שבוע={freq}"


_DEFAULT_REPLY_KEYBOARD = object()


@dataclass
class FreeTextContext:
    """Normalized input and reply helper for one natural-language turn."""

    update: Update
    user_id: int
    message: Any
    text: str
    intent: Any
    action: str
    slots: dict[str, Any]
    follow: InlineKeyboardMarkup

    async def send(self, body: str, kb: Any = _DEFAULT_REPLY_KEYBOARD) -> None:
        reply_markup = self.follow if kb is _DEFAULT_REPLY_KEYBOARD else kb
        await self.message.reply_text(
            body,
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML,
        )


@runtime_bound(RUNTIME_NAMES)
async def _handle_ambiguous_sensitive_value(ctx: FreeTextContext) -> bool:
    """Require explicit confirmation before mutating a sensitive numeric fact."""
    import re as _re

    bare_number = _re.fullmatch(r"\s*\d+(?:\.\d+)?\s*ק?ל?ו?ר?י?ו?ת?\s*", ctx.text)
    is_sensitive = ctx.action in ("set_goal", "update_measurement")
    if not ((bare_number and is_sensitive) or (is_sensitive and ctx.intent.confidence < 0.5)):
        return False

    number = _re.search(r"\d+(?:\.\d+)?", ctx.text)
    if not number:
        return False
    value = number.group(0)
    await set_confirm_pending(ctx.user_id, {"value": float(value), "text": ctx.text})
    await ctx.send(
        f"לא בטוח למה התכוונת עם <b>{value}</b>. מה זה?",
        InlineKeyboardMarkup(
            [
                [button("🎯 יעד קלוריות", f"confirm:goal_cal:{value}")],
                [button("⚖️ משקל נוכחי", f"confirm:weight:{value}")],
                [button("🎯 משקל יעד", f"confirm:goal_weight:{value}")],
                [button("בטל", "confirm:cancel:0")],
            ]
        ),
    )
    return True


@runtime_bound(RUNTIME_NAMES)
async def _handle_meal_status_action(ctx: FreeTextContext) -> bool:
    """REC-PLAN-MEAL-03-04: Answer meal-status questions with actual meal data."""
    if ctx.action != "meal_status":
        return False
    start_utc, end_utc = today_bounds_utc()
    meals = await DB.fetch_all(
        "SELECT id, name, calories, protein, eaten_at FROM meals "
        "WHERE user_id=? AND eaten_at>=? AND eaten_at<? AND COALESCE(status, 'consumed')='consumed' ORDER BY eaten_at",
        (ctx.user_id, start_utc, end_utc),
    )
    if not meals:
        await ctx.send(
            "עוד לא נרשמו ארוחות היום.\nשלח תמונה או כתוב מה אכלת כדי להתחיל.",
            InlineKeyboardMarkup([
                [button("📷 הוסף ארוחה", "menu:food")],
                [button("📊 מצב היום", "menu:status")],
                [button("⬅️ תפריט", "menu:home")],
            ]),
        )
        return True
    total_cal = sum(float(m["calories"]) for m in meals)
    total_prot = sum(float(m["protein"]) for m in meals)
    lines = [f"<b>ארוחות היום ({len(meals)})</b>", ""]
    for i, m in enumerate(meals, 1):
        lines.append(
            f"{i}. {esc(m['name'])} — {float(m['calories']):.0f} קל׳, "
            f"{float(m['protein']):.0f} חלבון"
        )
    lines += [
        "",
        f"סה״כ: <b>{total_cal:.0f} קלוריות</b> | <b>{total_prot:.0f} חלבון</b>",
    ]
    await ctx.send(
        "\n".join(lines),
        InlineKeyboardMarkup([
            [button("📷 הוסף ארוחה", "menu:food")],
            [button("📊 מצב היום", "menu:status")],
            [button("⬅️ תפריט", "menu:home")],
        ]),
    )
    return True


@runtime_bound(RUNTIME_NAMES)
async def _handle_redundant_question_challenge(ctx: FreeTextContext) -> bool:
    """REC-PLAN-MEAL-03-17: Handle user frustration about redundant questions."""
    if ctx.action != "redundant_question_challenge":
        return False
    # Check if we are in a question flow
    flow = await conversation.get_active_flow(DB, ctx.user_id)
    if flow.is_question:
        fact_key = None
        q = questions.question_by_id(flow.step)
        if q:
            fact_key = q.fact_key
        if fact_key:
            existing = await user_model.get_fact(DB, ctx.user_id, fact_key)
            if existing and existing.get("value") is not None and existing["kind"] != user_model.KIND_GAP:
                val_display = user_model.display_value(fact_key, existing["value"])
                source_label = user_model.SOURCE_LABELS.get(
                    existing.get("source"), existing.get("source", "")
                )
                await ctx.send(
                    f"צודק — כבר יש לי מידע קודם:\n"
                    f"• <b>{esc(user_model.display_label(fact_key))}</b>: "
                    f"{esc(val_display)} <i>({esc(source_label)})</i>\n\n"
                    "זה עדיין נכון?",
                    InlineKeyboardMarkup([
                        [button("✅ כן", f"qa:{flow.step}:confirm_existing")],
                        [button("✏️ עדכן", f"qa:{flow.step}:update_existing")],
                        [button("⏳ אחר כך", f"qa:{flow.step}:skip")],
                    ]),
                )
                await event_log.append_event(
                    DB, ctx.user_id, "profile_question_reused_existing_value",
                    entity="fact", entity_id=fact_key,
                    source="user_challenge",
                )
                return True
    # Generic response when not in a specific question flow
    await ctx.send(
        "צודק, אשתמש במידע שכבר יש לי.\nאם משהו השתנה — אפשר לעדכן בפרופיל.",
        InlineKeyboardMarkup([
            [button("👤 פרופיל", "menu:profile")],
            [button("⬅️ תפריט", "menu:home")],
        ]),
    )
    return True


async def _reply_next_meal_recommendation(
    message: Any,
    user_id: int,
    *,
    recommendation: Any | None = None,
    prefix: str = "",
    record_served: bool = True,
) -> Any:
    from noam_coach.services.next_meal import (
        format_next_meal_recommendation,
        generate_next_meal_recommendation,
        next_meal_action_rows,
        record_next_meal_served,
        remember_active_recommendation,
    )

    if recommendation is None:
        await message.chat.send_action("typing")
        recommendation = await generate_next_meal_recommendation(DB, user_id)
    # TASK-03: next_meal_action_rows already ends with a "חזור לסיכום היום"
    # button — no extra status/home row needed on top of the 4-button cap.
    keyboard_rows = [
        [button(label, callback_data) for label, callback_data in row]
        for row in next_meal_action_rows(recommendation)
    ]
    body = format_next_meal_recommendation(recommendation)
    if prefix:
        body = f"{prefix}\n\n{body}"
    sent = await message.reply_text(
        body,
        reply_markup=InlineKeyboardMarkup(keyboard_rows),
        parse_mode=ParseMode.HTML,
    )
    if record_served:
        await record_next_meal_served(DB, user_id, recommendation)
    await remember_active_recommendation(
        DB,
        user_id,
        recommendation,
        message_id=getattr(sent, "message_id", None),
    )
    return recommendation


@runtime_bound(RUNTIME_NAMES)
async def _handle_status_text_action(ctx: FreeTextContext) -> bool:
    if ctx.action == "next_meal":
        await _reply_next_meal_recommendation(ctx.message, ctx.user_id)
        return True

    builders = {
        "today_menu": build_morning_menu_text,
        "evening_summary": build_evening_summary_text,
        "request_progress": build_progress_text,
        "show_profile": build_profile_text,
    }
    builder = builders.get(ctx.action)
    if builder is None:
        return False
    if ctx.action != "show_profile":
        await ctx.message.chat.send_action("typing")
    await ctx.send(await builder(ctx.user_id))
    return True


# An explicit ABC-split request in free text ("תוכנית ABC", "אימון איי בי סי").
# Detected deterministically so the request becomes a FULL 3-day plan build —
# never a single-exercise edit screen (Codex audit, images 11-12).
_ABC_SPLIT_RE = re.compile(r"\babc\b|\ba\s*[/\-]?\s*b\s*[/\-]?\s*c\b|איי\s*בי\s*סי", re.IGNORECASE)


def requested_split_frequency(text: str) -> int | None:
    """Return the weekly frequency implied by an explicit split request.

    PATCH-12 / IMG_011: when a user asks for ABC after already declaring four
    training days, the product expectation is not to drop Sunday and force a
    3-day ABC.  The project templates now support a professional 4-day
    "ABC + Full Body" structure, so explicit ABC defaults to four sessions;
    the availability gate below still protects users with fewer confirmed days.
    """
    if _ABC_SPLIT_RE.search(text or ""):
        return 4  # ABC + Full Body when four days are available.
    return None


async def _split_availability_gate(user_id: int, split_freq: int) -> tuple[str, Any] | None:
    """When the user asked for a split that needs more days than their
    CONFIRMED availability allows, return (message, keyboard) offering a fit
    instead of silently building a plan they can't follow. Returns None when
    the split fits or availability is unknown/unconfirmed."""
    from noam_coach.bot.ui import button
    from noam_coach.services.availability import resolve_availability

    try:
        availability = await resolve_availability(DB, user_id)
    except Exception:  # noqa: BLE001 - the gate must never block plan building
        return None
    days = int(availability.max_days_per_week or 0)
    if not availability.confirmed or days <= 0 or days >= split_freq:
        return None
    text = (
        f"תוכנית ABC/Full Body מותאמת דורשת {split_freq} אימונים בשבוע, אבל לפי הזמינות "
        f"שאישרת יש לך {days}. אפשר לבנות תוכנית מלאה שמתאימה לימים שלך, "
        "או בכל זאת ABC."
    )
    keyboard = InlineKeyboardMarkup(
        [
            [button(f"🏋️ בנה לפי {days} ימים בשבוע", f"plan:set:{days}")],
            [button(f"בכל זאת ABC ({split_freq} ימים)", f"plan:set:{split_freq}")],
        ]
    )
    return text, keyboard


@runtime_bound(RUNTIME_NAMES)
async def _handle_plan_text_action(ctx: FreeTextContext) -> bool:
    if ctx.action == "build_plan":
        frequency = ctx.slots.get("frequency")
        split_freq = requested_split_frequency(ctx.text)
        if split_freq is not None and not isinstance(frequency, (int, float)):
            gate = await _split_availability_gate(ctx.user_id, split_freq)
            if gate is not None:
                await ctx.send(gate[0], gate[1])
                return True
            frequency = split_freq
        if isinstance(frequency, (int, float)) and 1 <= frequency <= 7:
            frequency = int(frequency)
            gaps = await check_plan_readiness(ctx.user_id)
            if gaps:
                await ctx.send(
                    "לפני שאבנה תוכנית, חסר לי עוד מידע:\n• "
                    + "\n• ".join(gaps)
                    + "\n\nנענה על השאלות קודם?",
                )
                return True
            if await ask_deferred_for_plan(ctx.message, ctx.user_id, frequency):
                return True
            plan = await build_weekly_plan(ctx.user_id, frequency)
            plan_text = format_weekly_plan(plan)
            if split_freq is not None and frequency == split_freq:
                if frequency >= 4:
                    plan_text = "בניתי לך תוכנית ABC + Full Body מותאמת ל־4 ימים — בלי להוריד יום אימון שהזנת:\n\n" + plan_text
                else:
                    plan_text = "בניתי לך תוכנית ABC מלאה — A חזה, B גב, C כתפיים ורגליים:\n\n" + plan_text
            await ctx.send(
                plan_text,
                InlineKeyboardMarkup(
                    [
                        [button("🏋️ התחל אימון", "menu:workout")],
                        [button("✏️ לשנות כמות", "menu:plan")],
                    ]
                ),
            )
        else:
            await set_pending(ctx.user_id, "__plan_frequency__")
            await ctx.send("כמה אימונים בשבוע תרצה? כתוב מספר (למשל 3).", None)
        return True

    if ctx.action == "start_workout":
        code = await select_todays_workout_code(ctx.user_id)
        if code:
            await render_workout_overview_reply(ctx.message, ctx.user_id, code)
        else:
            await ctx.send(
                "עוד אין תוכנית פעילה. כתוב לי כמה אימונים בשבוע ואבנה אחת, "
                'או הקש על "📅 תוכנית אימונים".',
                InlineKeyboardMarkup([[button("📅 תוכנית אימונים", "menu:plan")]]),
            )
        return True
    return False


@runtime_bound(RUNTIME_NAMES)
async def _handle_goal_text_action(ctx: FreeTextContext) -> bool:
    if ctx.action == "set_goal":
        goal_weight = ctx.slots.get("goal_weight")
        if isinstance(goal_weight, (int, float)):
            from noam_coach.services.goal_validation import (
                is_explicit_goal_weight_confirmation,
                validate_goal_weight,
            )

            current_weight = await user_model.get_value(DB, ctx.user_id, "weight_kg")
            try:
                current_weight_float = float(current_weight) if current_weight is not None else None
            except (TypeError, ValueError):
                current_weight_float = None
            validation = validate_goal_weight(float(goal_weight), current_weight_float)
            if validation.needs_confirmation and not is_explicit_goal_weight_confirmation(ctx.text, float(goal_weight)):
                await ctx.send(
                    validation.message,
                    InlineKeyboardMarkup(
                        [
                            [button("✏️ אכתוב יעד אחר", "menu:goals")],
                            [button("⬅️ תפריט", "menu:home")],
                        ]
                    ),
                )
                return True
            await set_goal_weight(ctx.user_id, float(goal_weight))
            await ctx.send(
                f'רשמתי יעד של {goal_weight:g} ק"ג. אבדוק את הקצב לפי משקל, ביצועים, '
                "רעב ושינה — לא לפי יום בודד. רוצה שאבנה תוכנית סביב זה?",
                InlineKeyboardMarkup(
                    [
                        [button("📅 בנה תוכנית", "menu:plan")],
                        [button("⬅️ תפריט", "menu:home")],
                    ]
                ),
            )
        else:
            await ctx.send("מה היעד? כתוב לי משקל יעד (למשל 85).", None)
        return True

    if ctx.action == "set_calorie_goal":
        if not _is_explicit_calorie_goal_change(ctx.text):
            await ctx.send(
                "לא שיניתי יעד. כדי לעדכן יעד קלורי כתוב במפורש למשל: \"שנה יעד ל-2100 קלוריות\".",
                ctx.follow,
            )
            return True
        await _handle_calorie_goal_change(ctx)
        return True

    if ctx.action == "set_dietary_pref":
        await record_dietary_preference(ctx.user_id, ctx.slots, ctx.text, ctx.send)
        return True
    return False


def _is_explicit_calorie_goal_change(text: str) -> bool:
    t = f" {text.strip().lower()} "
    explicit_markers = (
        "יעד",
        "מטרה קלורית",
        "קלוריות ליום",
        "שנה ל",
        "עדכן ל",
        "קבע ל",
        "set goal",
        "calorie goal",
    )
    return any(marker in t for marker in explicit_markers)


@runtime_bound(RUNTIME_NAMES)
async def _handle_calorie_goal_change(ctx: FreeTextContext) -> None:
    """REC re7 P0-4: a free-text daily-calorie change -> explicit confirm flow.

    Shows previous vs new target and asks for confirmation. The activation
    itself runs through the existing confirm:goal_cal callback, which versions
    the goal, supersedes the old one, and refreshes the daily snapshot.
    """
    calories = ctx.slots.get("calories")
    try:
        new_cal = int(float(calories))
    except (TypeError, ValueError):
        new_cal = 0
    if not (800 <= new_cal <= 6000):
        await ctx.send(
            "מה יעד הקלוריות היומי שתרצה? כתוב מספר (למשל \"יעד 2100\").",
            None,
        )
        return

    current = await fetch_goal(ctx.user_id)
    prev_cal = int(current.get("calories") or 0)
    prev_label = "יעד זמני" if current.get("provisional") else "יעד נוכחי"
    await event_log.append_event(
        DB,
        ctx.user_id,
        "goal_change_requested",
        entity="goal",
        source="user_text",
        properties={"previous": prev_cal, "requested": new_cal},
    )
    await set_confirm_pending(ctx.user_id, {"value": float(new_cal), "text": ctx.text})
    await ctx.send(
        (
            "<b>שינוי יעד קלוריות</b>\n\n"
            f"{prev_label}: <b>{prev_cal:,}</b> קלוריות\n"
            f"יעד מבוקש: <b>{new_cal:,}</b> קלוריות\n\n"
            "אחרי אישור זה יהפוך ליעד הפעיל היחיד, ומצב היום והחישובים יתעדכנו מיד."
        ),
        InlineKeyboardMarkup(
            [
                [button("✅ אישור יעד", f"confirm:goal_cal:{new_cal}")],
                [button("✏️ עריכה", "confirm:cancel:0"), button("❌ ביטול", "confirm:cancel:0")],
            ]
        ),
    )


@runtime_bound(RUNTIME_NAMES)
async def _handle_daily_health_text_action(ctx: FreeTextContext) -> bool:
    if ctx.action == "morning_flag":
        flag = ctx.slots.get("flag")
        note = ctx.slots.get("note") or ctx.text
        known = await all_medication_names(ctx.user_id)
        medication = medication_name_from_text(ctx.text, flag, known=known)
        if medication:
            status = str(ctx.slots.get("status") or "taken")
            event_status = "skipped" if status == "skipped" else "taken"
            await record_medication(
                ctx.user_id,
                medication,
                source="user_text",
                status=event_status,
            )
            if event_status == "skipped":
                await ctx.send(
                    f"רשמתי שלא לקחת {esc(medication)} היום. "
                    "לא אשנה הנחיות מינון או תזמון."
                )
                return True
            extra = (
                " ביום כזה התיאבון בדרך כלל יורד — אקל על הארוחות, "
                "אדגיש חלבון ואזכיר לשתות."
                if "ריטלין" in medication or medication.lower() == "ritalin"
                else ""
            )
            await ctx.send(f"רשמתי שלקחת {esc(medication)} 💊{extra}")
            return True

        flags = await get_daily_flags(ctx.user_id)
        flags["notes"] = note
        if flag == "not_fasting" or ctx.slots.get("status") == "not_fasting":
            flags["fasting"] = False
        elif flag == "fasting":
            flags["fasting"] = True
        await set_daily_flags(ctx.user_id, flags)
        await write_audit(ctx.user_id, "daily_flags", "flags", None, text=note)
        await ctx.send("נרשם להיום, אתאים את ההמלצות בהתאם 👍")
        return True

    if ctx.action == "report_pain":
        await set_pending(ctx.user_id, "__pain_location__")
        await save_medical_constraint(
            ctx.user_id,
            kind="pain",
            note="reported via chat",
            affects=("exercise_selection",),
        )
        await ctx.send(
            "תודה שעדכנת. אתאים תרגילים כדי לא להחמיר את הכאב, ואני לא מאבחן — "
            "אם הכאב חד, מתגבר או עם נפיחות, כדאי בדיקה מקצועית.\n"
            'איפה בדיוק כואב? (למשל "ברך ימין")',
            None,
        )
        return True
    return False


@runtime_bound(RUNTIME_NAMES)
async def _handle_measurement_or_meal_text_action(ctx: FreeTextContext) -> bool:
    if ctx.action == "update_measurement":
        previous_target = await target_calories(ctx.user_id)
        applied = []
        for key, fact_key, lower, upper in (
            ("weight_kg", "weight_kg", 30, 400),
            ("height_cm", "height_cm", 100, 250),
            ("body_fat_pct", "body_fat_pct", 3, 70),
        ):
            value = ctx.slots.get(key)
            if isinstance(value, (int, float)) and lower <= value <= upper:
                await user_model.set_fact(
                    DB,
                    ctx.user_id,
                    fact_key,
                    float(value),
                    kind=user_model.KIND_FACT,
                    source=user_model.SOURCE_USER,
                    confirmed=True,
                )
                applied.append(fact_key)
        if applied:
            await ctx.send("עודכן ✅\n" + await target_change_note(ctx.user_id, previous_target))
        else:
            await ctx.send('לא הצלחתי לקרוא את המספר. נסה למשל: "המשקל שלי 89".', None)
        return True

    if ctx.action == "log_meal_text":
        await log_meal_from_text(ctx.message, ctx.user_id, ctx.slots.get("text") or ctx.text)
        return True
    return False


@runtime_bound(RUNTIME_NAMES)
async def _send_free_text_help(ctx: FreeTextContext) -> None:
    await ctx.send(
        "<b>מה אני יכול לעשות?</b>\n\n"
        "🍽 <b>תזונה</b> — שלח תמונה של אוכל או כתוב מה אכלת\n"
        "📋 <b>תפריט</b> — תפריט יומי מותאם אישית\n"
        "🏋️ <b>אימון</b> — תוכנית אימונים + מעקב סטים\n"
        "📊 <b>מעקב</b> — סיכום יומי, שבועי, התקדמות\n"
        "💊 <b>דיווח</b> — תרופות, כאבים, צום\n"
        "📱 <b>Apple Health</b> — שלח ZIP או נתיב מקומי לייבוא תקופתי\n\n"
        "אפשר פשוט לכתוב בשפה חופשית, למשל:\n"
        '"מה לאכול עכשיו" / "המשקל שלי 89" / "כואבת לי הברך"',
        InlineKeyboardMarkup(
            [
                [button("📋 תפריט היום", "menu:daily_menu"), button("🏋️ אימון", "menu:workout")],
                [button("📊 סיכום יומי", "menu:status"), button("📅 תוכנית", "menu:plan")],
                [button("👤 פרופיל", "menu:profile"), button("📈 שבועי", "menu:weekly")],
            ]
        ),
    )


@runtime_bound(RUNTIME_NAMES)
async def route_free_text(update: Update, user_id: int) -> None:
    """Classify a free-text turn and route it through focused intent handlers."""
    message = update.effective_message
    text = (message.text or "").strip()
    if not text:
        return

    if "מה לאכול עכשיו" in text:
        prefix = ""
        if "למה" in text or "אין" in text:
            prefix = "הנה כפתור והמלצה ל״מה לאכול עכשיו״. זה שייך לתזונה, לא לאימון."
        await _reply_next_meal_recommendation(message, user_id, prefix=prefix)
        return

    from noam_coach.services.next_meal import (
        get_active_recommendation_state,
        handle_recommendation_correction,
    )

    active_recommendation = await get_active_recommendation_state(DB, user_id)
    if active_recommendation:
        correction = await handle_recommendation_correction(DB, user_id, text)
        if correction is not None:
            prefix, recommendation = correction
            await _reply_next_meal_recommendation(
                message,
                user_id,
                recommendation=recommendation,
                prefix=prefix,
                record_served=False,
            )
            return

    summary = await assistant_profile_summary(user_id)
    intent = await assistant.classify_intent(
        OPENAI_CLIENT,
        SETTINGS.openai_model,
        text,
        summary,
    )
    ctx = FreeTextContext(
        update=update,
        user_id=user_id,
        message=message,
        text=text,
        intent=intent,
        action=intent.action,
        slots=intent.slots or {},
        follow=InlineKeyboardMarkup([[button("⬅️ תפריט", "menu:home")]]),
    )
    handlers = (
        _handle_ambiguous_sensitive_value,
        _handle_redundant_question_challenge,
        _handle_meal_status_action,
        _handle_status_text_action,
        _handle_plan_text_action,
        _handle_goal_text_action,
        _handle_daily_health_text_action,
        _handle_measurement_or_meal_text_action,
    )
    for handler in handlers:
        if await handler(ctx):
            return
    await _send_free_text_help(ctx)


@runtime_bound(RUNTIME_NAMES)
async def render_workout_overview_reply(message: Any, user_id: int, code: str) -> None:
    """Same brief as render_workout_overview but as a fresh reply message."""
    plan = await get_user_plan(user_id, code)
    lines = []
    for index, exercise_data in enumerate(plan["exercises"], start=1):
        weight, reps, _ = await recommend_load(user_id, exercise_data)
        muscle = exercise_data.get("muscle")
        muscle_tag = f" <i>({muscle})</i>" if muscle else ""
        lines.append(
            f"{index}. <b>{exercise_data['name']}</b>{muscle_tag} — "
            f'{exercise_data["sets"]}×{reps} במשקל {weight:g} ק"ג'
        )
    banner = await constraint_banner(user_id)
    banner += await fatigue_banner(user_id)
    text = f"<b>{plan['name']}</b>\n\n" + banner + "\n".join(lines) + "\n\nמוכן? לחץ <b>התחל</b>."
    await message.reply_text(
        text,
        reply_markup=workout_overview_keyboard(code),
        parse_mode=ParseMode.HTML,
    )


@runtime_bound(RUNTIME_NAMES)
async def build_progress_text(user_id: int) -> str:
    """A simple progress snapshot from stored facts + recent weight points."""
    weight = await user_model.get_value(DB, user_id, "weight_kg")
    trend = await DB.fetch_all(
        "SELECT value, start_time FROM health "
        "WHERE user_id=? AND sample_type='weight' ORDER BY start_time DESC "
        "LIMIT 14",
        (user_id,),
    )
    lines = ["<b>ההתקדמות שלך</b>", ""]
    if weight is not None:
        lines.append(f'משקל נוכחי: <b>{weight} ק"ג</b>')
    if len(trend) >= 2:
        delta = float(trend[0]["value"]) - float(trend[-1]["value"])
        direction = "ירידה" if delta < 0 else "עלייה"
        lines.append(f'מגמה ב-{len(trend)} מדידות אחרונות: {direction} של {abs(delta):.1f} ק"ג')
    profile = await load_routine_profile(user_id)
    wk = (profile.get("workout") or {}).get("weekly_frequency")
    if wk is not None:
        lines.append(f"תדירות אימונים מזוהה: ~{wk} בשבוע")
    if len(lines) == 2:
        lines.append(
            "עוד אין מספיק נתונים להציג מגמה. שלח ייצוא Apple Health עם /import כדי שאוכל לעקוב."
        )
    return "\n".join(lines)


@runtime_bound(RUNTIME_NAMES)
def _clean_pref_item(item: str) -> str:
    """Strip leading negations/qualifiers from a stated preference phrase.

    "אני לא שותה אלכוהול" -> "אלכוהול"; "לא אלכוהול" -> "אלכוהול".
    Falls back to the trimmed phrase when nothing matches.
    """
    cleaned = (item or "").strip()
    for prefix in (
        "אני לא אוכל", "אני לא שותה", "אני לא צורך", "אני לא נוגע ב",
        "אני לא", "לא אוכל", "לא שותה", "לא צורך", "לא נוגע ב",
        "בלי", "ללא", "לא ", "אין ", "אל ",
    ):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):].strip()
            break
    return cleaned or (item or "").strip()


@runtime_bound(RUNTIME_NAMES)
async def record_dietary_preference(
    user_id: int,
    slots: dict[str, Any],
    text: str,
    send: Any,
) -> None:
    """Store a standing dietary preference/restriction/allergy as a fact.

    Fixes the P0 where "לא אלכוהול" was saved as a zero-calorie meal: such text
    is a restriction, never an eating event. Appends to the existing value
    (string, comma-separated, deduped) and tells the user exactly what changed,
    including the source so it is clear the bot inferred this from their text.
    """
    item = _clean_pref_item(str(slots.get("item") or slots.get("note") or text))
    if not item:
        await send("לא הצלחתי להבין איזו העדפה לרשום. נסה למשל: \"אני לא שותה אלכוהול\".", None)
        return

    from noam_coach.services.food_preferences import (
        DISLIKE_FACT,
        PREFERENCE_FACT,
        record_food_preference_from_slots,
    )

    update = await record_food_preference_from_slots(DB, user_id, slots, text)
    item = update.item
    new_value = update.new_value

    labels = {
        "allergies": "אלרגיה",
        "diet_restrictions": "מגבלה תזונתית",
        DISLIKE_FACT: "מאכל שלא מתאים לך",
        PREFERENCE_FACT: "העדפת אוכל",
    }
    label = labels.get(update.fact_key, "העדפה תזונתית")
    contradiction_note = "\nניקיתי גם סתירה קודמת ברשימת ההעדפות." if update.removed_from else ""
    if update.already_present:
        body = f"כבר רשום אצלי ש{label} כוללת: <b>{esc(item)}</b>. לא שיניתי דבר."
    else:
        body = (
            f"עדכנתי {label}: <b>{esc(item)}</b> ✅\n"
            f"זו לא ארוחה — שמרתי את זה כהעדפה קבועה (מקור: דיווח שלך).\n"
            f"כל הרשימה כעת: {esc(new_value)}{contradiction_note}"
        )
    await send(
        body,
        InlineKeyboardMarkup([[button("👤 פרופיל", "menu:profile"), button("⬅️ תפריט", "menu:home")]]),
    )


@runtime_bound(RUNTIME_NAMES)
async def log_meal_from_text(
    message: Any, user_id: int, text: str, *, source: str = "text"
) -> None:
    """Log a meal described in text only (no photo) via the AI analyzer.

    ``source`` is stored on the approval so the meal's origin (e.g.
    "manual_text" for the dedicated manual-entry action) is identifiable
    downstream while still flowing through the normal meal pipeline.
    """
    if OPENAI_CLIENT is None:
        await message.reply_text("כדי לנתח ארוחה מטקסט צריך מפתח OpenAI. אפשר לשלוח תמונה במקום.")
        return
    progress = await message.reply_text("רושם את הארוחה… ⏳")
    try:
        analysis = await analyze_meal_text(text, user_id=user_id)
        if not analysis.is_meaningful():
            await progress.edit_text(
                "זה לא נראה כמו ארוחה שאפשר לרשום (אין מזון או ערכים תזונתיים).\n"
                "אם התכוונת להעדפה או מגבלה — נסה למשל \"אני לא שותה אלכוהול\".\n"
                "אם באמת אכלת — פרט מעט יותר (כמות וסוג המזון)."
            )
            return
        approval_id = await create_approval(
            user_id,
            "meal",
            {
                "analysis": analysis.model_dump(),
                "image": None,
                "source": source,
                "eaten_at": utc_now(),
            },
        )
        await render_meal(progress, user_id, approval_id)
        # Enable the cumulative free-text correction path (fixmeal) for the
        # manually-entered meal, exactly like the photo flow does.
        from noam_coach.bot.meals import set_meal_fix

        await set_meal_fix(user_id, approval_id, 0)
    except Exception as exc:  # noqa: BLE001
        LOGGER.exception("text meal log failed")
        await progress.edit_text(friendly_error(exc, "text meal log"))


@runtime_bound(RUNTIME_NAMES)
async def build_workout_prompt_text(user_id: int) -> str | None:
    plan = await user_model.get_value(DB, user_id, "active_workout_plan")
    if not plan or not plan.get("sessions"):
        return None
    if await active_session(user_id):
        return None
    if await workout_completed_today(user_id):
        return None
    today = datetime.now(TZ).weekday()
    profile = await load_routine_profile(user_id)
    workout_hour = (profile.get("workout") or {}).get("typical_hour")
    for session in plan["sessions"]:
        if session.get("weekday") == today:
            at = session.get("time") or workout_hour or "הזמן הרגיל שלך"
            return f"🏋️ <b>{session['name']}</b> מתוכנן להיום ({at}). רוצה להתחיל?"
    if workout_hour:
        hh, mm = (int(x) for x in workout_hour.split(":"))
        now = datetime.now(TZ)
        nowh = now.hour + now.minute / 60.0
        target = hh + mm / 60.0
        if abs(nowh - target) <= 1.0:
            code = await select_todays_workout_code(user_id)
            if code:
                return f"🏋️ זה סביב שעת האימון הרגילה שלך (~{workout_hour}). רוצה להתחיל את {PLANS[code]['name']}?"
    return None


@runtime_bound(RUNTIME_NAMES)
async def build_weekly_summary_text(user_id: int) -> str:
    """Build a weekly review only from sufficiently complete, de-duplicated days."""
    from statistics import median

    now_local = datetime.now(TZ)
    week_end = now_local.date()
    week_start = week_end - timedelta(days=6)
    start_local = datetime.combine(week_start, dttime.min, tzinfo=TZ)
    end_local = datetime.combine(week_end + timedelta(days=1), dttime.min, tzinfo=TZ)
    start_utc = start_local.astimezone(timezone.utc).isoformat()
    end_utc = end_local.astimezone(timezone.utc).isoformat()
    previous_start_utc = (start_local - timedelta(days=7)).astimezone(timezone.utc).isoformat()

    meals_data = await DB.fetch_all(
        """
        SELECT id, calories, protein, confidence, eaten_at
        FROM meals WHERE user_id=? AND eaten_at>=? AND eaten_at<?
          AND COALESCE(status, 'consumed')='consumed'
        ORDER BY eaten_at
        """,
        (user_id, start_utc, end_utc),
    )
    meals_by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in meals_data:
        try:
            eaten = datetime.fromisoformat(str(row.get("eaten_at")))
            if eaten.tzinfo is None:
                eaten = eaten.replace(tzinfo=timezone.utc)
            day_key = eaten.astimezone(TZ).date().isoformat()
        except (TypeError, ValueError):
            day_key = str(row.get("eaten_at") or "")[:10]
        if day_key:
            meals_by_day[day_key].append(row)

    structure = await user_model.get_value(DB, user_id, "meal_structure_preference")
    expected_meals = {
        "two_large": 2,
        "small_frequent": 4,
        "structured": 3,
        "flexible": 3,
    }.get(str(structure or ""), 3)
    minimum_complete_meals = max(2, math.ceil(expected_meals * 0.67))
    complete_days = {
        day: rows
        for day, rows in meals_by_day.items()
        if len(rows) >= minimum_complete_meals
    }

    duplicate_rows = await DB.fetch_all(
        """
        SELECT fingerprint, COUNT(*) AS c
        FROM meal_fingerprints
        WHERE user_id=? AND created_at>=? AND created_at<? AND fingerprint!=''
        GROUP BY fingerprint HAVING COUNT(*)>1
        """,
        (user_id, start_utc, end_utc),
    )

    workout_rows = await DB.fetch_all(
        """
        SELECT status FROM sessions
        WHERE user_id=? AND status IN ('completed','partial')
          AND ended_at>=? AND ended_at<?
        """,
        (user_id, start_utc, end_utc),
    )
    actual_workouts = len(workout_rows)
    workout_plan = await planning.get_active_plan(DB, user_id, "workout")
    planned_workouts = 0
    if workout_plan:
        planned_workouts = int(workout_plan.get("payload", {}).get("frequency") or 0)

    goal = await fetch_goal(user_id)
    day_scores: list[float] = []
    complete_calories: list[float] = []
    complete_protein: list[float] = []
    for rows in complete_days.values():
        calories = sum(float(row["calories"]) for row in rows)
        protein = sum(float(row["protein"]) for row in rows)
        complete_calories.append(calories)
        complete_protein.append(protein)
        calorie_ratio = calories / max(1, float(goal["calories"]))
        protein_ratio = min(1.0, protein / max(1, float(goal["protein"])))
        calorie_score = max(0.0, 1 - abs(1 - calorie_ratio))
        day_scores.append(calorie_score * 0.6 + protein_ratio * 0.4)

    weights = await DB.fetch_all(
        """
        SELECT value, start_time FROM health
        WHERE user_id=? AND sample_type='weight' AND start_time>=? AND start_time<?
        ORDER BY start_time
        """,
        (user_id, previous_start_utc, end_utc),
    )
    current_weights: list[float] = []
    previous_weights: list[float] = []
    for row in weights:
        try:
            measured = datetime.fromisoformat(str(row["start_time"]))
            if measured.tzinfo is None:
                measured = measured.replace(tzinfo=timezone.utc)
            local_date = measured.astimezone(TZ).date()
        except (TypeError, ValueError):
            continue
        if local_date >= week_start:
            current_weights.append(float(row["value"]))
        else:
            previous_weights.append(float(row["value"]))

    meal_days = len(meals_by_day)
    complete_day_count = len(complete_days)
    completeness = complete_day_count / 7
    lines = [
        f"<b>סיכום {week_start.strftime('%d/%m')}–{week_end.strftime('%d/%m')}</b>",
        "",
        f"דיווח אוכל: <b>{meal_days} מתוך 7 ימים</b>",
        f"ימים מלאים לניתוח: <b>{complete_day_count} מתוך 7</b>",
    ]
    if duplicate_rows:
        lines.append("⚠️ קיים חשד לארוחות כפולות; הן לא משמשות למסקנה חזקה.")
    if completeness < 0.43:
        lines.append("<i>איכות הנתונים: נמוכה — לא משנים יעד על בסיס השבוע הזה.</i>")
    elif completeness < 0.72:
        lines.append("<i>איכות הנתונים: בינונית.</i>")
    else:
        lines.append("<i>איכות הנתונים: טובה.</i>")
    lines.append("")

    if planned_workouts:
        lines.append(f"אימונים: <b>{actual_workouts} מתוך {planned_workouts} מתוכננים</b>")
    else:
        lines.append(f"אימונים שבוצעו: <b>{actual_workouts}</b>")
    lines.append("")

    if complete_day_count >= 4 and not duplicate_rows:
        avg_calories = sum(complete_calories) / complete_day_count
        avg_protein = sum(complete_protein) / complete_day_count
        adherence = sum(day_scores) / len(day_scores)
        lines.append(f"ממוצע קלורי בימים מלאים: <b>{avg_calories:.0f}</b> (יעד: {goal['calories']})")
        lines.append(f"ממוצע חלבון בימים מלאים: <b>{avg_protein:.0f} גרם</b> (יעד: {goal['protein']})")
        lines.append(f"עמידה תזונתית: <b>{adherence:.0%}</b>")
    else:
        lines.append("קלוריות וחלבון: <i>אין מספיק ימים מלאים ונקיים מכפילויות לממוצע אמין</i>")
    lines.append("")

    if len(current_weights) >= 2 and len(previous_weights) >= 2:
        current_median = median(current_weights)
        previous_median = median(previous_weights)
        delta = current_median - previous_median
        direction = "ירידה" if delta < 0 else "עלייה"
        lines.append(f"מגמת משקל: {direction} של <b>{abs(delta):.1f} ק״ג</b> לפי חציון שבועי")
    elif current_weights:
        lines.append(f"משקל חציוני השבוע: <b>{median(current_weights):.1f} ק״ג</b> — אין שבוע קודם להשוואה")
    else:
        lines.append("משקל: <i>אין מספיק מדידות</i>")
    lines += ["", "<b>הפעולה החשובה לשבוע הבא:</b>"]

    if duplicate_rows:
        lines.append("לעבור על הארוחות הכפולות לפני הסקת מסקנות.")
    elif complete_day_count < 4:
        lines.append("לדווח לפחות שתי ארוחות ביום במשך ארבעה ימים מלאים.")
    elif planned_workouts and actual_workouts < planned_workouts:
        lines.append(f"לבצע לפחות {min(planned_workouts, actual_workouts + 1)} אימונים, בלי לשנות עדיין את כל התוכנית.")
    elif day_scores and sum(day_scores) / len(day_scores) < 0.75:
        lines.append("לשפר את ארוחת החלבון הקבועה בשעה שבה הכי קשה לעמוד ביעד.")
    else:
        lines.append("להמשיך את התוכנית הנוכחית ללא שינוי נוסף השבוע.")
    return "\n".join(lines)


@runtime_bound(RUNTIME_NAMES)
async def send_weight_chart(bot: Any, chat_id: int, user_id: int) -> bool:
    try:
        import matplotlib.pyplot as plt
    except Exception:
        return False
    points = await DB.fetch_all(
        "SELECT value, start_time FROM health WHERE user_id=? AND sample_type='weight' ORDER BY start_time DESC LIMIT 30",
        (user_id,),
    )
    if len(points) < 2:
        return False
    points = list(reversed(points))
    xs = [p["start_time"][:10] for p in points]
    ys = [float(p["value"]) for p in points]
    fig = plt.figure(figsize=(8, 4))
    ax = fig.add_subplot(111)
    ax.plot(xs, ys, marker="o")
    ax.set_title("מגמת משקל")
    ax.set_xlabel("תאריך")
    ax.set_ylabel('ק"ג')
    ax.tick_params(axis="x", rotation=45)
    fig.tight_layout()
    buffer = io.BytesIO()
    fig.savefig(buffer, format="png")
    plt.close(fig)
    buffer.seek(0)
    await bot.send_photo(chat_id=chat_id, photo=buffer)
    return True


@runtime_bound(RUNTIME_NAMES)
async def command_weekly(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if not await is_allowed(update):
        return
    user_id = await ensure_user(update)
    await track_event(user_id, "command_weekly")
    await update.effective_message.reply_text(
        await build_weekly_summary_text(user_id), parse_mode=ParseMode.HTML
    )


@runtime_bound(RUNTIME_NAMES)
async def command_chart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_allowed(update):
        return
    user_id = await ensure_user(update)
    await track_event(user_id, "command_chart")
    ok = await send_weight_chart(context.bot, update.effective_chat.id, user_id)
    if not ok:
        await update.effective_message.reply_text("עוד אין מספיק נתוני משקל כדי להציג גרף.")
