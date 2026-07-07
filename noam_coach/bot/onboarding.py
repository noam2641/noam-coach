# ruff: noqa: F401, F811, F821, I001
"""Telegram onboarding, profile and planning flows.

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
from noam_coach.bot.ui import safe_answer_callback
from noam_coach.services.weekdays import sunday_first_key, weekday_labels_he

RUNTIME_NAMES = ('Any', 'CONFIRM_PENDING', 'ContextTypes', 'DB', 'Exception', 'InlineKeyboardMarkup', 'LOGGER', 'MAX_FREQUENCY', 'MIN_FREQUENCY', 'PENDING_QUESTION', 'PLANS', 'ParseMode', 'PlanConstraint', 'SETTINGS', 'SPLIT_BY_FREQUENCY', 'TypeError', 'Update', 'ValueError', '_CANCEL_WORDS', '_ENUM_DISPLAY_MAP', '_as_float', '_format_candidate', '_format_fact_value', '_parse_dietary_answer', '_plan_type_label', '_re', '_safe_cb', 'a_parts', 'abs', 'active_constraints', 'affects', 'allergies', 'allowed', 'applied', 'apply_basics_fix', 'ask_deferred_for_plan', 'ask_next_question', 'assumptions', 'at', 'block', 'bool', 'build_profile_text', 'build_weekly_plan', 'button', 'c', 'callback', 'candidate', 'candidates', 'chosen_days', 'clear_flow_state', 'clear_meal_fix', 'clear_pending', 'compute_basics_extras', 'confirm', 'confirm_routine_facts', 'confirmation_text', 'constraint_id', 'constraint_text', 'constraints', 'context_pending_fix', 'conversation', 'ctx', 'current', 'd', 'data', 'dataclass', 'datetime', 'day', 'days', 'days_source', 'default_spread', 'deferred', 'delta', 'detail', 'detected_days', 'dict', 'diet', 'direction', 'discard_unconfirmed_routine_facts', 'display', 'display_val', 'ensure_user', 'enumerate', 'esc', 'event_log', 'exc', 'existing', 'existing_a', 'existing_r', 'exp_labels', 'experience', 'extract_daily_routine', 'extraction', 'extras', 'fact', 'facts', 'finish_onboarding', 'first_item', 'float', 'flow', 'flow_name', 'food_item', 'format_constraints_summary', 'format_routine_confirmation', 'format_weekly_plan', 'freq', 'frequency', 'gap', 'gaps', 'gather_plan_constraints', 'get_flow_state', 'goal', 'goal_labels', 'group', 'handle_safety_answer', 'hard', 'hasattr', 'head', 'home_keyboard', 'hour', 'i', 'icon', 'index', 'index_str', 'int', 'is_allowed', 'isinstance', 'item', 'items', 'json', 'k', 'key', 'keyboard', 'kind', 'kind_label', 'label', 'latest_bf', 'latest_weight', 'len', 'lines', 'list', 'load_routine_profile', 'loc', 'loc_labels', 'location', 'mapping', 'mark', 'match', 'max', 'mc', 'meal', 'medical', 'message', 'min', 'mins', 'missing', 'missing_labels', 'name', 'needs_follow_up', 'new_val', 'note', 'num', 'nutrition', 'onboarding', 'onboarding_frequency_keyboard', 'onboarding_open_keyboard', 'out', 'parsed_items', 'parts', 'payload', 'pct', 'pending', 'plan', 'plan_constraints', 'plan_type', 'planning', 'prefix', 'profile', 'progress', 'prompt', 'pts', 'q', 'qid', 'query', 'question', 'question_names', 'questions', 'r', 'range', 'rationale', 're', 'readable', 'readiness', 'record_medication', 'restriction_type', 'result', 'rng', 'round', 'row', 'rows', 's', 'safe_edit', 'save_medical_constraint', 'save_routine_extraction', 'score', 'session', 'session_min', 'sessions', 'set_flow_state', 'set_pending', 'severity', 'show_onboarding_patterns', 'since', 'sleep', 'snapshot', 'soft', 'sorted', 'source', 'spec', 'split', 'stage', 'start_onboarding', 'str', 'suggestions', 'suppress', 'suspend', 'target', 'text', 'time_text', 'timedelta', 'timezone', 'title', 'track_event', 'tradeoffs', 'tuple', 'type_label', 'type_labels', 'understood', 'unified', 'update', 'user', 'user_id', 'user_model', 'utc_now', 'v', 'value', 'view', 'weekday_he', 'when', 'why', 'wk', 'workout', 'workout_window', 'write_audit')


@runtime_bound(RUNTIME_NAMES)
async def is_allowed(update: Update) -> bool:
    user = update.effective_user
    allowed = bool(user and user.id == SETTINGS.telegram_allowed_user_id)
    if not allowed and update.effective_message:
        await update.effective_message.reply_text("אין הרשאה להשתמש בבוט.")
    return allowed


@runtime_bound(RUNTIME_NAMES)
async def command_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    del context
    if not await is_allowed(update):
        return
    user_id = await ensure_user(update)
    await track_event(user_id, "command_start")
    stage = await onboarding.get_stage(DB, user_id)
    if stage == onboarding.S_DONE:
        # Returning user — offer to continue, with quick access to onboarding.
        await update.effective_message.reply_text(
            "<b>המאמן האישי שלך</b>\n\n"
            'אפשר פשוט לכתוב לי מה בא לך — "תפריט להיום", "בוא נתאמן", '
            '"מה לאכול עכשיו" — או לשלוח תמונת אוכל. הכפתורים כאן לקיצור דרך.',
            reply_markup=home_keyboard(),
            parse_mode=ParseMode.HTML,
        )
        return
    await start_onboarding(update.effective_message, user_id)


@runtime_bound(RUNTIME_NAMES)
def onboarding_open_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [button("📱 איך מייצאים מה-iPhone?", "onb:export_help")],
            [button("⏭️ אין לי כרגע", "onb:no_data")],
        ]
    )


@runtime_bound(RUNTIME_NAMES)
async def start_onboarding(target: Any, user_id: int) -> None:
    await onboarding.set_stage(DB, user_id, onboarding.S_OPEN)
    await target.reply_text(
        onboarding.INTRO_TEXT,
        reply_markup=onboarding_open_keyboard(),
        parse_mode=ParseMode.HTML,
    )


@runtime_bound(RUNTIME_NAMES)
async def compute_basics_extras(user_id: int) -> dict[str, Any]:
    """Aggregate measured extras for the onboarding basics screen."""
    extras: dict[str, Any] = {}
    rng = await DB.fetch_one(
        "SELECT MIN(value) AS lo, MAX(value) AS hi FROM health "
        "WHERE user_id=? AND sample_type='weight'",
        (user_id,),
    )
    if rng and rng["lo"] is not None:
        extras["weight_range"] = (round(rng["lo"], 1), round(rng["hi"], 1))

    # 90-day weight trend (first vs last reading in window).
    since = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    pts = await DB.fetch_all(
        "SELECT value, start_time FROM health "
        "WHERE user_id=? AND sample_type='weight' AND start_time>=? "
        "ORDER BY start_time",
        (user_id, since),
    )
    # Codex audit: two readings are not a trend — require at least 3 and say
    # how many measurements the trend is based on.
    if len(pts) >= 3:
        delta = float(pts[-1]["value"]) - float(pts[0]["value"])
        direction = "ירידה" if delta < 0 else "עלייה"
        extras["weight_trend_90d"] = (
            f'{direction} של {abs(delta):.1f} ק"ג (על בסיס {len(pts)} מדידות)'
        )
    elif len(pts) == 2:
        extras["weight_trend_90d"] = "עדיין אין מגמה מהימנה (רק 2 מדידות)"

    sleep = await DB.fetch_one(
        "SELECT AVG(value) AS v FROM health WHERE user_id=? AND sample_type='sleep_session'",
        (user_id,),
    )
    if sleep and sleep["v"]:
        mins = int(sleep["v"])
        extras["avg_sleep"] = f"{mins // 60} שעות ו-{mins % 60} דקות"

    profile = await load_routine_profile(user_id)
    wk = (profile.get("workout") or {}).get("weekly_frequency")
    if wk is not None:
        extras["weekly_workouts"] = wk
    return extras


BASICS_AUDIT_KEYS = (
    "height_cm",
    "weight_kg",
    "body_fat_pct",
    "avg_steps",
    "sleep_schedule",
    "workout_pattern",
    "goal_weight_kg",
    "training_days_per_week",
    "workout_window",
    "training_location",
    "equipment",
    "allergies",
    "diet_restrictions",
)


def _profile_audit_status_he(row: dict[str, Any]) -> str:
    action = str(row.get("action_required") or "none")
    if action == "none":
        return "מאושר" if row.get("approved") else "זוהה"
    if action == "confirm":
        return "דורש אישור"
    if action == "correct":
        return "דורש תיקון"
    if action == "refresh_health":
        return "ישן - לרענן Health"
    if action == "ask_user":
        return "חסר"
    return action.replace("_", " ")


def _profile_audit_lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return []
    source_labels = {
        user_model.SOURCE_APPLE_HEALTH: "Apple Health",
        user_model.SOURCE_DERIVED: "הוסק",
        user_model.SOURCE_USER: "דיווח שלך",
        user_model.SOURCE_SYSTEM: "מערכת",
        "unknown": "לא ידוע",
    }
    lines = ["", "<b>אישור נתוני בסיס</b>"]
    for row in rows:
        action = str(row.get("action_required") or "none")
        value = row.get("display_value")
        if value is None and action == "none":
            continue
        label = esc(str(row.get("label") or row.get("field_name") or ""))
        display = esc(str(value)) if value is not None else "חסר"
        source = source_labels.get(str(row.get("source") or "unknown"), str(row.get("source") or "לא ידוע"))
        confidence = esc(str(row.get("confidence_label") or ""))
        status = esc(_profile_audit_status_he(row))
        lines.append(f"• {label}: <b>{display}</b> · {status} · מקור: {esc(source)} · אמינות: {confidence}")
    if len(lines) == 2:
        return []
    lines.append("")
    lines.append("אפשר לאשר הכול, או לכתוב תיקון ישירות כאן.")
    return lines


@runtime_bound(RUNTIME_NAMES)
async def show_onboarding_basics(target: Any, user_id: int) -> None:
    await onboarding.set_stage(DB, user_id, onboarding.S_CONFIRM_BASICS)
    view = await user_model.get_profile_view(DB, user_id)
    extras = await compute_basics_extras(user_id)
    text = onboarding.basics_summary(view, extras)
    audit_rows = await user_model.build_profile_audit(DB, user_id, keys=list(BASICS_AUDIT_KEYS))
    audit_lines = _profile_audit_lines(audit_rows)
    if audit_lines:
        text += "\n" + "\n".join(audit_lines)
    latest_weight = await user_model.get_value(DB, user_id, "weight_kg")
    latest_bf = await user_model.get_value(DB, user_id, "body_fat_pct")
    suggestions = []
    if latest_weight is not None:
        suggestions.append(f'משקל מהנתונים: {float(latest_weight):.1f} ק"ג')
    if latest_bf is not None:
        suggestions.append(f"שומן: {float(latest_bf):.1f}%")
    if suggestions:
        text += (
            "\n\n<b>זיהיתי מהנתונים שיובאו:</b>\n"
            + "\n".join(f"• {s}" for s in suggestions)
            + '\n\nלאשר, או לכתוב תיקון (למשל "המשקל 90").'
        )
        # RE11: accept a typed correction directly at this screen — no forced
        # tap on "יש מה לתקן" first.
        await context_pending_fix(user_id)
    await track_event(
        user_id,
        "onboarding_basics_shown",
        has_weight=latest_weight is not None,
        has_body_fat=latest_bf is not None,
    )
    keyboard = InlineKeyboardMarkup(
        [
            [button("✅ הכול נכון", "onb:basics_ok")],
        ]
    )
    if hasattr(target, "edit_message_text"):
        await safe_edit(target, text, keyboard)
    else:
        await target.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)


@runtime_bound(RUNTIME_NAMES)
async def show_onboarding_patterns(target: Any, user_id: int) -> None:
    await onboarding.set_stage(DB, user_id, onboarding.S_CONFIRM_PATTERNS)
    profile = await load_routine_profile(user_id)
    text, items = onboarding.patterns_text(profile)
    await track_event(user_id, "onboarding_patterns_shown", items=len(items))
    rows = []
    for item in items:
        status = await _pattern_confirmation_status(user_id, item["id"])
        if status == user_model.CONFIRM_CONFIRMED:
            rows.append([button(f"✅ מאושר: {item['display_label']}", f"onb:pat_noop:{item['id']}")])
        elif status == user_model.CONFIRM_INVALID:
            rows.append([button(f"✏️ סומן לתיקון: {item['display_label']}", f"onb:pat_noop:{item['id']}")])
        elif status == user_model.CONFIRM_DEFERRED:
            rows.append([button(f"⏳ נדחה: {item['display_label']}", f"onb:pat_noop:{item['id']}")])
        else:
            rows.append([
                button("✅ מאושר", f"onb:pat_ok:{item['id']}"),
                button("✏️ לתקן", f"onb:pat_fix:{item['id']}"),
                button("⏳ אחר כך", f"onb:pat_defer:{item['id']}"),
            ])
    rows.append([button("המשך ➡️", "onb:patterns_done")])
    keyboard = InlineKeyboardMarkup(rows)
    if hasattr(target, "edit_message_text"):
        await safe_edit(target, text, keyboard)
    else:
        await target.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)


@runtime_bound(RUNTIME_NAMES)
async def _pattern_confirmation_status(user_id: int, key: str) -> str:
    row = await DB.fetch_one(
        "SELECT kind, source, confirmed, valid FROM user_facts WHERE user_id=? AND key=?",
        (user_id, key),
    )
    if not row:
        return "missing"
    if not bool(row.get("valid", True)):
        return user_model.CONFIRM_INVALID
    if row.get("kind") == user_model.KIND_GAP and row.get("source") == user_model.SOURCE_USER:
        return user_model.CONFIRM_DEFERRED
    if row.get("confirmed"):
        return user_model.CONFIRM_CONFIRMED
    return user_model.CONFIRM_INFERRED


@runtime_bound(RUNTIME_NAMES)
async def confirm_visible_basics(user_id: int) -> None:
    view = await user_model.get_profile_view(DB, user_id)
    for group in ("measured", "inferred", "reported"):
        for fact in view.get(group, []):
            if fact.get("key"):
                await user_model.confirm_fact(DB, user_id, fact["key"])
    for key in (
        "sleep_schedule",
        "workout_pattern",
        "work_schedule",
        "workout_window",
        "meal_break_info",
        "cooking_capacity",
    ):
        fact = await user_model.get_fact(DB, user_id, key)
        if fact and fact.get("kind") != user_model.KIND_GAP:
            await user_model.confirm_fact(DB, user_id, key)


@runtime_bound(RUNTIME_NAMES)
async def _ask_training_frequency_trend_question(
    target: Any, user_id: int, question: questions.Question
) -> bool:
    """RE11: when Health data shows the user's recent training frequency has
    drifted from their long-run average, propose a number based on the
    recent trend (not the stale overall average) instead of the plain
    question/"already have" flows. Returns True if this proposal was shown.
    """
    import routine

    existing = await user_model.get_fact(DB, user_id, "training_days_per_week")
    if existing and existing.get("kind") != user_model.KIND_GAP and existing.get("confirmed"):
        return False  # user already has a confirmed answer — nothing to propose

    profile = await load_routine_profile(user_id)
    workout = profile.get("workout") or {}
    pattern = routine.WorkoutPattern(
        weekly_frequency=workout.get("weekly_frequency"),
        sessions_sampled=workout.get("sessions_sampled", 0),
        recent_weekly_frequency=workout.get("recent_weekly_frequency"),
        recent_sessions_sampled=workout.get("recent_sessions_sampled", 0),
    )
    proposal = routine.build_frequency_trend_proposal(pattern)
    if proposal is None:
        return False

    await set_pending(user_id, question.id)
    text = f"<b>שאלה</b>\n\n{esc(proposal.message)}"
    rows = [
        [button(label, f"qa:{question.id}:trend:{value:g}")]
        for label, value in proposal.choices
    ]
    keyboard = InlineKeyboardMarkup(rows)
    if hasattr(target, "edit_message_text"):
        await safe_edit(target, text, keyboard)
    else:
        await target.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
    return True


@runtime_bound(RUNTIME_NAMES)
async def ask_next_question(target: Any, user_id: int) -> bool:
    """Ask the highest-priority pending question. Returns False when none left.

    Safety questions win first (safety*3 weight); once they're answered the
    essential plan questions follow.
    """
    # During onboarding, only ask the minimal essential set; defer the rest.
    ctx: dict[str, Any] = {}
    question = await questions.next_question(DB, user_id, ctx, pool=questions.ONBOARDING_QUESTIONS)
    if question is None:
        return False

    # RE11: for training frequency, prefer a trend-aware proposal (recent
    # vs. overall Health-derived average) over the plain question/confirm
    # flows below, when Health data actually shows a meaningful shift.
    if question.fact_key == "training_days_per_week":
        trend_shown = await _ask_training_frequency_trend_question(target, user_id, question)
        if trend_shown:
            return True

    # REC-PLAN-MEAL-03-05: If we already have data for this fact (e.g. from
    # Apple Health import), show it for confirmation instead of re-asking.
    # RE11: a typed correction is accepted directly at this same prompt (no
    # forced tap on "לא, אעדכן" first) for any question that accepts free
    # text at all — plain free-text questions and free_text_fallback ones.
    existing = await user_model.get_fact(DB, user_id, question.fact_key)
    if existing and existing.get("value") is not None and existing["kind"] != user_model.KIND_GAP:
        val_display = _format_fact_value(question.fact_key, existing["value"])
        source_label = user_model.SOURCE_LABELS.get(
            existing.get("source"), existing.get("source", "")
        )
        await set_pending(user_id, question.id)
        accepts_free_text = not question.options or question.free_text_fallback
        confirm_line = "זה נכון?" if not accepts_free_text else "זה נכון, או שיש עדכון? (אפשר גם לכתוב ישירות)"
        text = (
            f"<b>שאלה</b>\n\n{question.text}\n\n"
            f"💡 כבר יש לי: <b>{esc(val_display)}</b> "
            f"<i>({esc(source_label)})</i>\n{confirm_line}"
        )
        rows = [[button("✅ כן, נכון", f"qa:{question.id}:confirm_existing")]]
        if not accepts_free_text:
            rows.append([button("✏️ לא, אעדכן", f"qa:{question.id}:update_existing")])
        keyboard = InlineKeyboardMarkup(rows)
        if hasattr(target, "edit_message_text"):
            await safe_edit(target, text, keyboard)
        else:
            await target.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
        return True

    await set_pending(user_id, question.id)
    if question.options:
        rows = [
            [button(label, f"qa:{question.id}:{index}")]
            for index, (label, _value) in enumerate(question.options)
        ]
        keyboard = InlineKeyboardMarkup(rows)
    else:
        keyboard = None  # free-text answer captured in handle_text_message
    text = f"<b>שאלה</b>\n\n{question.text}"
    if hasattr(target, "edit_message_text"):
        await safe_edit(target, text, keyboard)
    else:
        await target.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
    return True


PENDING_QUESTION: dict[int, str] = {}


CONFIRM_PENDING: dict[int, dict[str, Any]] = {}


PLAN_COMPLETION_FLOW = "plan_completion"
PLAN_COMPLETION_PROFILES = ("workout", "nutrition", "safety")


async def set_flow_state(user_id: int, flow: str, step: str, payload: dict[str, Any]) -> None:
    from noam_coach.services.core import set_flow_state as _set_flow_state

    await _set_flow_state(user_id, flow, step, payload)


async def get_flow_state(user_id: int, flow: str) -> dict[str, Any] | None:
    from noam_coach.services.core import get_flow_state as _get_flow_state

    return await _get_flow_state(user_id, flow)


async def clear_flow_state(user_id: int, flow: str) -> None:
    from noam_coach.services.core import clear_flow_state as _clear_flow_state

    await _clear_flow_state(user_id, flow)


@runtime_bound(RUNTIME_NAMES)
def _plan_completion_profile_order(plan_type: str | None) -> tuple[str, ...]:
    """Order readiness profiles so the plan the user actually asked for is
    completed first (RE10-6). ``safety`` always stays reachable — it never
    blocks the requested plan's questions, but it is still asked afterwards
    if still missing, since it protects exercise selection for any plan.
    """
    if plan_type in PLAN_COMPLETION_PROFILES:
        rest = [p for p in PLAN_COMPLETION_PROFILES if p != plan_type]
        return (plan_type, *rest)
    return PLAN_COMPLETION_PROFILES


@runtime_bound(RUNTIME_NAMES)
async def first_missing_plan_question(
    user_id: int, plan_type: str | None = None
) -> questions.Question | None:
    """Return the next missing profile question for the plan-completion flow.

    ``plan_type`` (RE10-6) prioritizes the profile the user actually asked to
    complete (e.g. "nutrition") so a nutrition completion never opens with
    workout questions just because ``workout`` is first in the static tuple.
    """
    readiness = await user_model.compute_all_readiness(DB, user_id)
    seen: set[str] = set()
    for profile_name in _plan_completion_profile_order(plan_type):
        for key in readiness.get(profile_name, {}).get("missing", []):
            if key in seen:
                continue
            seen.add(key)
            question = questions.question_by_fact_key(key)
            if question is not None:
                return question
    return None


@runtime_bound(RUNTIME_NAMES)
async def ask_next_plan_completion_question(
    target: Any, user_id: int, plan_type: str | None = None
) -> bool:
    """Ask the next missing plan detail and keep the user inside plan setup."""
    from noam_coach.bot.ui import button, safe_edit

    if plan_type is None:
        # Resume: read back the plan_type recorded when this flow started.
        state = await get_flow_state(user_id, PLAN_COMPLETION_FLOW)
        if state:
            plan_type = (state.get("payload") or {}).get("plan_type")

    question = await first_missing_plan_question(user_id, plan_type)
    if question is None:
        await clear_flow_state(user_id, PLAN_COMPLETION_FLOW)
        await render_smart_plan_hub(target, user_id)
        return False
    await set_flow_state(
        user_id,
        PLAN_COMPLETION_FLOW,
        question.id,
        {"return_to": "menu:smartplan", "plan_type": plan_type},
    )
    await set_pending(user_id, question.id)
    rows = []
    if question.options:
        rows = [
            [button(label, f"qa:{question.id}:{index}")]
            for index, (label, _value) in enumerate(question.options)
        ]
    rows.append([button("⬅️ חזור לתוכנית", "menu:smartplan")])
    keyboard = InlineKeyboardMarkup(rows) if rows else None
    text = f"<b>שאלה להשלמת התוכנית</b>\n\n{question.text}"
    if hasattr(target, "edit_message_text"):
        await safe_edit(target, text, keyboard)
    else:
        await target.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
    return True


@runtime_bound(RUNTIME_NAMES)
async def advance_after_answer(target: Any, user_id: int) -> None:
    """Shared "what happens after any question is answered" continuation.

    Checks, in order: plan-completion/goal-wizard/profile-edit flows, then a
    deferred-plan flow (asks the next deferred question or builds the plan),
    then falls back to the normal onboarding question loop. Every answer path
    (button and free text) must call this so a deferred-plan flow is resumed
    correctly regardless of how the answer was given.
    """
    if await continue_after_plan_completion_answer(target, user_id):
        return
    deferred = await get_flow_state(user_id, "deferred_plan")
    if deferred:
        freq = int(deferred["step"])
        message = target.message if hasattr(target, "message") else target
        if await ask_deferred_for_plan(message, user_id, freq):
            return
        plan = await build_weekly_plan(user_id, freq)
        await message.reply_text(
            format_weekly_plan(plan),
            reply_markup=InlineKeyboardMarkup(
                [
                    [button("🏋️ התחל אימון", "menu:workout")],
                    [button("⬅️ תפריט", "menu:home")],
                ]
            ),
            parse_mode=ParseMode.HTML,
        )
        return
    if not await ask_next_question(target, user_id):
        await finish_onboarding(target, user_id)


@runtime_bound(RUNTIME_NAMES)
async def continue_after_plan_completion_answer(target: Any, user_id: int) -> bool:
    """Continue plan-completion OR goal-wizard questions after an answer.

    Both flows use the same "ask next missing question, else finish" shape
    and both need to be checked after any generic question answer, so this
    single entry point (already called from every answer-handling path) is
    where the goal wizard (RE10-9) hooks in too — that avoids touching every
    individual call site that already checks plan-completion.
    """
    state = await get_flow_state(user_id, PLAN_COMPLETION_FLOW)
    if state:
        plan_type = (state.get("payload") or {}).get("plan_type")
        if await ask_next_plan_completion_question(target, user_id, plan_type):
            return True
        return True

    goal_state = await get_flow_state(user_id, GOAL_WIZARD_FLOW)
    if goal_state:
        return await continue_after_goal_wizard_answer(target, user_id)

    edit_state = await get_flow_state(user_id, "profile_field_edit")
    if edit_state:
        return await finish_profile_field_edit(target, user_id, str(edit_state["step"]))

    return False


# ---------------------------------------------------------------------------
# RE10-9 — goal wizard: complete height / goal weight / timeframe BEFORE
# showing the calorie/protein proposal, so the proposal doesn't open with
# "⚠️ missing: height" the very first time the user taps "יעדים".
# ---------------------------------------------------------------------------

GOAL_WIZARD_FLOW = "goal_wizard"
GOAL_WIZARD_FACT_KEYS = ("height_cm", "goal_weight_kg", "goal_timeframe_weeks")


@runtime_bound(RUNTIME_NAMES)
async def _first_missing_goal_wizard_question(user_id: int) -> questions.Question | None:
    for key in GOAL_WIZARD_FACT_KEYS:
        fact = await user_model.get_fact(DB, user_id, key)
        if fact is not None and fact.get("kind") != user_model.KIND_GAP:
            continue
        question = questions.question_by_fact_key(key)
        if question is not None:
            return question
    return None


@runtime_bound(RUNTIME_NAMES)
async def ask_next_goal_wizard_question(target: Any, user_id: int) -> bool:
    """Ask the next missing goal-wizard fact. Returns False when nothing is
    missing (caller should render the proposal instead)."""
    from noam_coach.bot.ui import button, safe_edit

    question = await _first_missing_goal_wizard_question(user_id)
    if question is None:
        await clear_flow_state(user_id, GOAL_WIZARD_FLOW)
        return False
    await set_flow_state(user_id, GOAL_WIZARD_FLOW, question.id, {})
    await set_pending(user_id, question.id)
    rows = []
    if question.options:
        rows = [
            [button(label, f"qa:{question.id}:{index}")]
            for index, (label, _value) in enumerate(question.options)
        ]
    rows.append([button("⬅️ תפריט", "menu:home")])
    keyboard = InlineKeyboardMarkup(rows) if rows else InlineKeyboardMarkup(
        [[button("⬅️ תפריט", "menu:home")]]
    )
    text = f"<b>שאלה להשלמת היעד</b>\n\n{question.text}"
    if hasattr(target, "edit_message_text"):
        await safe_edit(target, text, keyboard)
    else:
        await target.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
    return True


@runtime_bound(RUNTIME_NAMES)
async def continue_after_goal_wizard_answer(target: Any, user_id: int) -> bool:
    """Continue the goal wizard, or render the final proposal once done.

    Returns True when this flow owned the answer (caller must not also
    advance the generic onboarding-question flow for the same answer).
    """
    state = await get_flow_state(user_id, GOAL_WIZARD_FLOW)
    if not state:
        return False
    if await ask_next_goal_wizard_question(target, user_id):
        return True
    from noam_coach.bot.callback_plans import render_goal_proposal

    await render_goal_proposal(target, user_id)
    return True


@runtime_bound(RUNTIME_NAMES)
async def set_confirm_pending(user_id: int, payload: dict[str, Any]) -> None:
    CONFIRM_PENDING[user_id] = payload
    current = await conversation.get_active_flow(DB, user_id)
    await conversation.set_active_flow(
        DB,
        user_id,
        conversation.FlowName.confirm_number,
        step="active",
        payload=payload,
        suspend_current=not current.is_idle,
    )


@runtime_bound(RUNTIME_NAMES)
async def clear_confirm_pending(user_id: int) -> None:
    CONFIRM_PENDING.pop(user_id, None)
    current = await conversation.get_active_flow(DB, user_id)
    if current.name == conversation.FlowName.confirm_number:
        await conversation.resume_suspended(DB, user_id) or await conversation.clear_active_flow(DB, user_id)


@runtime_bound(RUNTIME_NAMES)
async def set_pending(user_id: int, prompt: str) -> None:
    """Set one authoritative pending question in ``active_flow``."""
    PENDING_QUESTION[user_id] = prompt
    flow_name = conversation.PENDING_KEY_TO_FLOW.get(
        prompt, conversation.FlowName.onboarding_question
    )
    current = await conversation.get_active_flow(DB, user_id)
    suspend = not current.is_idle and current.name not in conversation.QUESTION_FLOWS
    await conversation.set_active_flow(
        DB, user_id, flow_name, step=prompt, suspend_current=suspend
    )


@runtime_bound(RUNTIME_NAMES)
async def clear_pending(user_id: int) -> None:
    PENDING_QUESTION.pop(user_id, None)
    current = await conversation.get_active_flow(DB, user_id)
    if current.is_question or current.name == conversation.FlowName.routine_confirm:
        await conversation.resume_suspended(DB, user_id) or await conversation.clear_active_flow(DB, user_id)


@runtime_bound(RUNTIME_NAMES)
async def load_pending_state() -> None:
    """Rebuild optional in-memory caches from the authoritative active_flow table."""
    PENDING_QUESTION.clear()
    CONFIRM_PENDING.clear()
    rows = await DB.fetch_all("SELECT user_id, flow, step, payload FROM active_flow")
    question_names = {flow.value for flow in conversation.QUESTION_FLOWS}
    for row in rows:
        user_id = int(row["user_id"])
        if row["flow"] in question_names or row["flow"] == conversation.FlowName.routine_confirm.value:
            PENDING_QUESTION[user_id] = row["step"]
        elif row["flow"] == conversation.FlowName.confirm_number.value:
            with suppress(Exception):
                CONFIRM_PENDING[user_id] = json.loads(row.get("payload") or "{}")
    # Old rows are no longer authoritative; removing them prevents split-brain state.
    await DB.execute(
        "DELETE FROM conversation_state WHERE flow IN "
        "('pending_prompt','meal_fix','confirm_pending','deferred_plan')"
    )


@runtime_bound(RUNTIME_NAMES)
async def handle_onboarding_callback(query: Any, user_id: int, data: str) -> None:
    parts = data.split(":")
    head = parts[1] if len(parts) > 1 else ""

    if data == "onb:edit_menu":
        await render_profile_edit_menu(query, user_id)
        return

    if head == "edit" and len(parts) >= 3:
        await start_profile_field_edit(query, user_id, parts[2])
        return

    if data == "onb:export_help":
        await onboarding.set_stage(DB, user_id, onboarding.S_EXPORT_HELP)
        await safe_edit(
            query,
            onboarding.EXPORT_HELP_TEXT,
            InlineKeyboardMarkup([[button("אעשה זאת מאוחר יותר", "onb:later")]]),
        )
        return

    if data in ("onb:no_data", "onb:later"):
        text = onboarding.NO_DATA_TEXT if data == "onb:no_data" else onboarding.LATER_TEXT
        await safe_edit(query, text, None)
        # Skip straight to safety + essential plan questions.
        if not await ask_next_question(query, user_id):
            await finish_onboarding(query, user_id)
        return

    if data == "onb:basics_ok":
        # Promote every visible fact on the summary screen, not only hard
        # measurements. The user just approved the combined "what I understood"
        # summary, which includes routine-derived schedule facts too.
        await confirm_visible_basics(user_id)
        await show_onboarding_patterns(query, user_id)
        return

    if data == "onb:basics_fix":
        await safe_edit(
            query,
            'מה לתקן? כתוב לי בהודעה (למשל: "הגובה 176", "המשקל 90") ואעדכן.',
            InlineKeyboardMarkup([[button("המשך בכל זאת", "onb:basics_ok")]]),
        )
        await context_pending_fix(user_id)
        return

    if head == "pat_ok" and len(parts) >= 3:
        await user_model.confirm_fact(DB, user_id, parts[2])
        await event_log.append_event(
            DB, user_id, "inferred_profile_fact_confirmed",
            entity="fact", entity_id=parts[2], source="onboarding",
        )
        await safe_answer_callback(query, "מאושר ✅")
        await show_onboarding_patterns(query, user_id)
        return

    if head == "pat_fix" and len(parts) >= 3:
        await user_model.invalidate_fact(DB, user_id, parts[2])
        await event_log.append_event(
            DB, user_id, "inferred_profile_fact_corrected",
            entity="fact", entity_id=parts[2], source="onboarding",
        )
        await safe_answer_callback(query, "סומן לתיקון ✏️")
        await show_onboarding_patterns(query, user_id)
        return

    if head == "pat_defer" and len(parts) >= 3:
        await user_model.defer_fact(DB, user_id, parts[2])
        await event_log.append_event(
            DB, user_id, "inferred_profile_fact_deferred",
            entity="fact", entity_id=parts[2], source="onboarding",
        )
        await safe_answer_callback(query, "נדחה לאחר כך ⏳")
        await show_onboarding_patterns(query, user_id)
        return

    if head == "pat_noop":
        await safe_answer_callback(query, "כבר סומן")
        return

    if data == "routine:confirm":
        await confirm_routine_facts(user_id)
        await clear_pending(user_id)
        await safe_edit(query, "מצוין, הפרופיל עודכן ✅", None)
        if not await ask_next_question(query, user_id):
            await finish_onboarding(query, user_id)
        return

    if data == "routine:fix":
        await clear_pending(user_id)
        await set_pending(user_id, "__routine_confirm__")
        await safe_edit(
            query,
            "כתוב לי מה לתקן — למשל: \"אני עובד עד 18:00\" או \"אימון בבוקר ולא בערב\".",
            InlineKeyboardMarkup([[button("⏭️ לדלג ולהמשיך", "routine:skip")]]),
        )
        return

    if data == "routine:skip":
        await discard_unconfirmed_routine_facts(user_id)
        await clear_pending(user_id)
        await safe_edit(query, "דילגנו על תיאור היום. נלמד מהשימוש שלך בהמשך.", None)
        if not await ask_next_question(query, user_id):
            await finish_onboarding(query, user_id)
        return

    if data == "onb:patterns_done":
        if not await ask_next_question(query, user_id):
            await finish_onboarding(query, user_id)
        return

    # REC-PLAN-MEAL-03-01: Handle dietary restriction type classification
    if data.startswith("qa:diet_type:") and len(parts) >= 4:
        restriction_type = parts[2]
        food_item = parts[3] if len(parts) > 3 else ""
        if restriction_type == "cancel":
            # User didn't mean to avoid this food — remove it
            current = await _existing_list_value(user_id, "diet_restrictions")
            if food_item:
                current = [p for p in current if food_item not in p]
                new_val = ", ".join(current)
                await user_model.set_fact(
                    DB, user_id, "diet_restrictions", new_val or "none",
                    kind=user_model.KIND_FACT,
                    source=user_model.SOURCE_USER,
                    confirmed=True,
                )
            await safe_edit(query, f"הסרתי את {esc(food_item)} מרשימת ההימנעות.", None)
        elif restriction_type == "allergy":
            # Move from diet_restrictions to allergies
            current = await _existing_list_value(user_id, "diet_restrictions")
            if food_item:
                current = [p for p in current if food_item not in p]
                await user_model.set_fact(
                    DB, user_id, "diet_restrictions", ", ".join(current) or "none",
                    kind=user_model.KIND_FACT,
                    source=user_model.SOURCE_USER,
                    confirmed=True,
                )
            a_parts = await _existing_list_value(user_id, "allergies")
            if food_item and food_item not in a_parts:
                a_parts.append(food_item)
            await user_model.set_fact(
                DB, user_id, "allergies", ", ".join(a_parts),
                kind=user_model.KIND_FACT,
                source=user_model.SOURCE_USER,
                confirmed=True,
            )
            await safe_edit(
                query,
                f"רשמתי {esc(food_item)} כאלרגיה מאובחנת. אתייחס לזה ברצינות בכל ההמלצות.",
                None,
            )
        else:
            # preference, intolerance, sensitivity — keep in diet_restrictions
            type_labels = {
                "preference": "מעדיף להימנע",
                "intolerance": "אי-נוחות",
                "sensitivity": "רגישות",
            }
            type_label = type_labels.get(restriction_type, restriction_type)
            await _mark_no_allergies_if_missing(user_id)
            await safe_edit(
                query,
                f"רשמתי: {esc(food_item)} — {esc(type_label)} ✅",
                None,
            )
        await event_log.append_event(
            DB, user_id, "dietary_restriction_confirmed",
            entity="fact", entity_id=food_item,
            source="onboarding",
            properties={"restriction_type": restriction_type, "food_item": food_item},
        )
        if await continue_after_plan_completion_answer(query, user_id):
            return
        if not await ask_next_question(query, user_id):
            await finish_onboarding(query, user_id)
        return

    if data.startswith("qa:") and len(parts) >= 3:
        qid = parts[1]
        index_str = parts[2]
        question = questions.question_by_id(qid)

        if index_str == "confirm_existing":
            # REC-PLAN-MEAL-03-05: User confirms existing data is correct
            if question:
                await user_model.confirm_fact(DB, user_id, question.fact_key)
                await event_log.append_event(
                    DB, user_id, "existing_fact_confirmed_during_question",
                    entity="fact", entity_id=question.fact_key,
                    source="onboarding",
                )
            await clear_pending(user_id)
            if await continue_after_plan_completion_answer(query, user_id):
                return
            if not await ask_next_question(query, user_id):
                await finish_onboarding(query, user_id)
            return
        if index_str == "update_existing":
            # REC-PLAN-MEAL-03-05: User wants to update — show question normally
            if question and question.options:
                rows = [
                    [button(label, f"qa:{question.id}:{index}")]
                    for index, (label, _value) in enumerate(question.options)
                ]
                keyboard = InlineKeyboardMarkup(rows)
            else:
                keyboard = None
            text = f"<b>שאלה</b>\n\n{question.text}" if question else "כתוב את הערך החדש."
            await safe_edit(query, text, keyboard)
            return

        if index_str == "retry":
            # REC-PLAN-MEAL-03-02: Re-ask the failed question.
            if question:
                await set_pending(user_id, question.id)
                if question.options:
                    rows = [
                        [button(label, f"qa:{question.id}:{idx}")]
                        for idx, (label, _v) in enumerate(question.options)
                    ]
                    kb = InlineKeyboardMarkup(rows)
                else:
                    kb = None
                await safe_edit(query, f"<b>שאלה</b>\n\n{question.text}", kb)
            return

        if index_str == "trend" and len(parts) >= 4 and question:
            # RE11: user picked one of the trend-proposal buttons (stay at N /
            # go to M) instead of typing a number directly.
            try:
                trend_value = questions.normalize_answer(question, parts[3])
            except ValueError as exc:
                await safe_answer_callback(query, str(exc), show_alert=True)
                return
            await questions.record_answer(DB, user_id, question, trend_value)
            await clear_pending(user_id)
            await safe_edit(query, f"נרשם: {trend_value:g} אימונים בשבוע ✅", None)
            await advance_after_answer(query, user_id)
            return

        if question:
            existing = await user_model.get_fact(DB, user_id, question.fact_key)
            if existing and existing["kind"] != user_model.KIND_GAP and existing.get("confirmed"):
                await safe_answer_callback(query, "כבר ענית על השאלה הזו")
                return
        try:
            _index_int = int(index_str)
        except ValueError:
            _index_int = -1
        if question and index_str != "skip" and 0 <= _index_int < len(question.options):
            _label, value = question.options[_index_int]
            await questions.record_answer(DB, user_id, question, value)
            needs_follow_up = await handle_safety_answer(query, user_id, question, value)
            if needs_follow_up:
                # A free-text follow-up (e.g. pain location) is now pending —
                # do NOT clear it or advance; wait for the user's reply.
                return
        await clear_pending(user_id)
        await advance_after_answer(query, user_id)
        return


@runtime_bound(RUNTIME_NAMES)
async def context_pending_fix(user_id: int) -> None:
    await set_pending(user_id, "__basics_fix__")


@runtime_bound(RUNTIME_NAMES)
async def handle_safety_answer(
    query: Any, user_id: int, question: questions.Question, value: Any
) -> bool:
    """If a safety question flags an issue, record a constraint.

    RE11: "has X" detail is now captured directly as free text at the initial
    question prompt (see the free_text_fallback branch in
    handle_onboarding_text), so button presses here only ever carry "none" or
    a genuine multi-choice value — no follow-up prompt is needed.

    Returns True when a follow-up prompt is now pending (so the caller must not
    clear it or advance to the next question).
    """
    if question.fact_key == "training_location" and value == "gym":
        # RE10-7: a full gym implies full equipment — do not re-ask q_equipment.
        # "home"/"mixed" still need the equipment question (home gear varies).
        await user_model.set_fact(
            DB, user_id, "equipment", "full_gym",
            kind=user_model.KIND_FACT,
            source=user_model.SOURCE_USER,
            confirmed=True,
            affects=("exercise_selection",),
        )
    return False


@runtime_bound(RUNTIME_NAMES)
async def save_medical_constraint(
    user_id: int,
    *,
    kind: str,
    location: str | None = None,
    severity: int | None = None,
    note: str | None = None,
    affects: tuple[str, ...] = ("exercise_selection",),
) -> int:
    constraint_id = await DB.execute(
        """
        INSERT INTO medical_constraints(
            user_id, kind, location, severity, status, note, affects, created_at
        ) VALUES(?, ?, ?, ?, 'active', ?, ?, ?)
        """,
        (
            user_id,
            kind,
            location,
            severity,
            note,
            json.dumps(list(affects), ensure_ascii=False),
            utc_now(),
        ),
    )
    await write_audit(
        user_id,
        "safety_alert",
        "constraint",
        constraint_id,
        kind=kind,
        location=location,
    )
    return constraint_id


@runtime_bound(RUNTIME_NAMES)
async def active_constraints(user_id: int) -> list[dict[str, Any]]:
    return await DB.fetch_all(
        "SELECT * FROM medical_constraints WHERE user_id=? AND status='active'",
        (user_id,),
    )


@dataclass
class PlanConstraint:
    """A constraint that affects plan generation."""
    key: str
    kind: str  # 'hard' (must obey) or 'soft' (prefer but can override)
    label: str
    check: str  # what it checks: 'exercise', 'timing', 'nutrition', 'volume'
    source: str  # 'medical', 'user_pref', 'safety', 'computed'
    value: Any = None


@runtime_bound(RUNTIME_NAMES)
async def _usable_fact_value(user_id: int, key: str) -> Any:
    """Return a fact's value only when it is a real (non-gap) answer.

    REC-PLAN-MEAL-03-07 / RE10-2: ``record_gap`` stores unanswered questions as
    a dict (``{"missing": True, "why_matters": ...}``). Reading that value with
    plain ``get_value`` and interpolating it into a label leaks a raw Python
    dict (and English keys) into user-facing text. Callers that build display
    labels must go through this helper instead of ``user_model.get_value``.
    """
    fact = await user_model.get_fact(DB, user_id, key)
    if fact is None or fact.get("kind") == user_model.KIND_GAP:
        return None
    return fact.get("value")


@runtime_bound(RUNTIME_NAMES)
async def _existing_list_value(user_id: int, key: str) -> list[str]:
    """Return a comma-separated fact's items as a clean list, never a gap dict.

    RE10-2 path B: several handlers merge a new item into an *existing*
    comma-separated fact (e.g. ``diet_restrictions``) by reading the current
    value and splitting on ",". When the current value is still an unanswered
    gap (a dict), that repr was being split and persisted back as part of the
    new answer — permanently poisoning the fact even for screens that already
    filter gaps. This helper treats a gap (or any non-string value) as "no
    existing items yet" instead of stringifying it.
    """
    value = await _usable_fact_value(user_id, key)
    if not value or not isinstance(value, str):
        return []
    return [p.strip() for p in value.split(",") if p.strip()]


@runtime_bound(RUNTIME_NAMES)
async def _mark_no_allergies_if_missing(user_id: int) -> bool:
    """Close the allergy question when a typed food was classified as non-allergy."""
    fact = await user_model.get_fact(DB, user_id, "allergies")
    if fact is not None and fact.get("kind") != user_model.KIND_GAP:
        return False
    await user_model.set_fact(
        DB,
        user_id,
        "allergies",
        "none",
        kind=user_model.KIND_FACT,
        source=user_model.SOURCE_USER,
        confirmed=True,
        affects=("menu_planning", "safety"),
    )
    return True


@runtime_bound(RUNTIME_NAMES)
async def gather_plan_constraints(user_id: int) -> list[PlanConstraint]:
    """Collect all constraints relevant to plan building for a user."""
    constraints: list[PlanConstraint] = []

    # Hard: medical constraints
    medical = await active_constraints(user_id)
    for mc in medical:
        constraints.append(PlanConstraint(
            key=f"medical_{mc['id']}",
            kind="hard",
            label=mc.get("note") or mc["kind"],
            check="exercise",
            source="medical",
            value=mc,
        ))

    # Hard: allergies
    allergies = await _usable_fact_value(user_id, "allergies")
    if allergies and allergies != "none":
        constraints.append(PlanConstraint(
            key="allergies",
            kind="hard",
            label=f"אלרגיות: {_format_fact_value('allergies', allergies)}",
            check="nutrition",
            source="safety",
            value=allergies,
        ))

    # Soft: diet restrictions
    diet = await _usable_fact_value(user_id, "diet_restrictions")
    if diet:
        constraints.append(PlanConstraint(
            key="diet_restrictions",
            kind="soft",
            label=f"העדפות תזונה: {_format_fact_value('diet_restrictions', diet)}",
            check="nutrition",
            source="user_pref",
            value=diet,
        ))

    # Soft: workout time preference
    workout_window = await _usable_fact_value(user_id, "workout_window")
    if workout_window:
        constraints.append(PlanConstraint(
            key="workout_window",
            kind="soft",
            label=f"חלון אימון מועדף: {_format_fact_value('workout_window', workout_window)}",
            check="timing",
            source="user_pref",
            value=workout_window,
        ))

    # Soft: session duration
    session_min = await _usable_fact_value(user_id, "session_minutes")
    if session_min:
        constraints.append(PlanConstraint(
            key="session_duration",
            kind="soft",
            label=f"אורך אימון: {int(session_min)} דקות",
            check="volume",
            source="user_pref",
            value=int(session_min),
        ))

    # Soft: training location
    location = await _usable_fact_value(user_id, "training_location")
    if location:
        constraints.append(PlanConstraint(
            key="training_location",
            kind="soft",
            label=f"מיקום: {_format_fact_value('training_location', location)}",
            check="exercise",
            source="user_pref",
            value=location,
        ))

    return constraints


@runtime_bound(RUNTIME_NAMES)
def format_constraints_summary(constraints: list[PlanConstraint]) -> str:
    """Format constraints for display to the user."""
    if not constraints:
        return ""
    hard = [c for c in constraints if c.kind == "hard"]
    soft = [c for c in constraints if c.kind == "soft"]
    lines: list[str] = []
    if hard:
        lines.append("<b>🔒 מגבלות קשיחות:</b>")
        for c in hard:
            lines.append(f"  · {esc(c.label)}")
    if soft:
        lines.append("<b>🔧 העדפות:</b>")
        for c in soft:
            lines.append(f"  · {esc(c.label)}")
    return "\n".join(lines)


_ENUM_DISPLAY_MAP: dict[str, dict[str, str]] = {
    "primary_goal": {
        "fat_loss_muscle_retention": "ירידה בשומן תוך שמירה על מסת שריר",
        "muscle_gain": "עלייה במסת שריר",
        "strength": "כוח",
        "general_health": "בריאות וכושר כללי",
    },
    "training_location": {
        "gym": "חדר כושר",
        "home": "בית",
        "mixed": "משולב",
    },
    "equipment": {
        "full_gym": "חדר כושר מאובזר",
        "home_dumbbells": "משקולות בבית",
        "bodyweight": "משקל גוף",
        "custom": "ציוד מותאם",
    },
    "strength_experience": {
        "beginner": "מתחיל",
        "intermediate": "בינוני",
        "advanced": "מתקדם",
    },
    "cooking_capacity": {
        "none": "כמעט בלי בישול",
        "basic": "בסיסי ומהיר",
        "moderate": "הכנה פעמיים בשבוע",
        "enjoys": "נהנה לבשל",
    },
    "meal_structure_preference": {
        "three_structured": "3 ארוחות מסודרות",
        "two_large": "2 ארוחות גדולות",
        "small_frequent": "ארוחות קטנות",
        "flexible": "מסגרת גמישה",
    },
    "coaching_style": {
        "supportive": "תומך ועדין",
        "direct": "ענייני",
        "challenging": "תובעני",
        "data_driven": "מבוסס נתונים",
    },
    "sex": {
        "male": "זכר",
        "female": "נקבה",
    },
}


@runtime_bound(RUNTIME_NAMES)
def _format_fact_value(key: str, value: Any) -> str:
    """Format a fact value for human display.

    REC-PLAN-MEAL-03-07: Never expose internal enum names, snake_case
    identifiers, raw dicts, or Python repr to the user. Uses centralized
    display mappings.
    """
    if value is None or value == "none" or value == "None":
        return "לא צוין"

    # Check enum display map first
    if key in _ENUM_DISPLAY_MAP and isinstance(value, str):
        mapped = _ENUM_DISPLAY_MAP[key].get(value)
        if mapped:
            return mapped
        # Unknown enum: show value but strip underscores
        return value.replace("_", " ")

    if isinstance(value, dict):
        if key == "active_pain":
            loc = value.get("location", "לא צוין")
            return str(loc)
        if key == "work_schedule":
            start = value.get("start", "")
            end = value.get("end", "")
            if start and end:
                return f"{start}–{end}"
            return f"{start}{end}" if (start or end) else "לא צוין"
        return _format_structured_profile_item(key, value)

    if isinstance(value, (list, tuple)):
        readable = [_format_structured_profile_item(key, item) for item in value]
        readable = [item for item in readable if item and item != "לא צוין"]
        return ", ".join(readable) if readable else "לא צוין"

    def _as_float() -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            LOGGER.warning("fact %s has non-numeric value %r", key, value)
            return None

    if key in ("weight_kg", "goal_weight_kg"):
        num = _as_float()
        return f'{num:.1f} ק"ג' if num is not None else "לא צוין"
    if key == "height_cm":
        num = _as_float()
        return f"{num:.0f} ס״מ" if num is not None else "לא צוין"
    if key == "body_fat_pct":
        num = _as_float()
        return f"{num:.1f}%" if num is not None else "לא צוין"
    if key == "avg_steps":
        num = _as_float()
        return f"{round(num):,} צעדים" if num is not None else "לא צוין"
    if key == "resting_hr":
        num = _as_float()
        return f"{round(num)} פעימות/דקה" if num is not None else "לא צוין"
    if key == "training_days_per_week":
        return f"{value} בשבוע"
    if key == "session_minutes":
        return f"{value} דקות"
    if key == "goal_timeframe_weeks":
        num = _as_float()
        if num is None:
            return "לא צוין"
        if num >= 52:
            return f"{num / 52:.0f} שנה" if num % 52 == 0 else f"{num:.0f} שבועות"
        if num >= 4:
            months = num / 4.33
            return f"{months:.0f} חודשים" if months >= 1.5 else f"{num:.0f} שבועות"
        return f"{num:.0f} שבועות"
    # Fallback: clean snake_case from string values
    s = str(value)
    if "_" in s and s.replace("_", "").isalpha():
        return s.replace("_", " ")
    return s


def _format_structured_profile_item(key: str, value: Any) -> str:
    if not isinstance(value, dict):
        return str(value).replace("_", " ")
    if key == "weekly_availability" or "weekday" in value:
        weekday = _weekday_label(value.get("weekday"))
        start = str(value.get("start") or value.get("time") or "").strip()
        minutes = value.get("minutes")
        parts = [part for part in (weekday, start) if part]
        if minutes:
            parts.append(f"{minutes} דקות")
        return " ".join(parts) if parts else "לא צוין"
    if "value" in value:
        return _format_structured_profile_item(key, value["value"])
    readable = []
    for item_key, item_value in value.items():
        if item_value is None or item_key in {"type", "source", "status", "confidence"}:
            continue
        if isinstance(item_value, (dict, list, tuple)):
            nested = _format_fact_value(item_key, item_value)
            if nested != "לא צוין":
                readable.append(nested)
        else:
            readable.append(str(item_value).replace("_", " "))
    return ", ".join(readable) if readable else "לא צוין"


def _weekday_label(value: Any) -> str:
    names = {
        0: "ראשון",
        1: "שני",
        2: "שלישי",
        3: "רביעי",
        4: "חמישי",
        5: "שישי",
        6: "שבת",
    }
    try:
        return names.get(int(value), "")
    except (TypeError, ValueError):
        return str(value or "").replace("_", " ")


@runtime_bound(RUNTIME_NAMES)
async def build_profile_text(user_id: int) -> str:
    view = await user_model.get_profile_view(DB, user_id)

    def block(title: str, facts: list[dict[str, Any]]) -> list[str]:
        if not facts:
            return []
        out = [f"<b>{title}</b>"]
        for fact in facts:
            spec = user_model.FACT_REGISTRY.get(fact["key"])
            if not spec:
                continue
            label = spec.label
            display = _format_fact_value(fact["key"], fact["value"])
            if display == "לא צוין":
                continue
            mark = "✓" if fact["confirmed"] else "·"
            out.append(f"{mark} {esc(label)}: {esc(display)}")
        if len(out) <= 1:
            return []
        out.append("")
        return out

    lines = ["<b>הפרופיל שלך</b>", ""]
    lines += block("📏 נמדד", view["measured"])
    lines += block("💬 דווח על ידך", view["reported"])
    if view["gaps"]:
        lines.append("<b>❔ חסר</b>")
        for gap in view["gaps"]:
            spec = user_model.FACT_REGISTRY.get(gap["key"])
            if not spec:
                continue
            lines.append(f"· {esc(spec.label)}")
    # REC-PROGRAM-04-01: Show resolved training availability
    from noam_coach.services.availability import format_availability_summary, resolve_availability
    avail = await resolve_availability(DB, user_id)
    avail_text = format_availability_summary(avail)
    lines += ["", "<b>🗓️ זמינות לאימון</b>"]
    for avail_line in avail_text.split("\n"):
        lines.append(esc(avail_line))

    constraints = await active_constraints(user_id)
    if constraints:
        lines += ["", "<b>⚠️ מגבלות פעילות</b>"]
        for c in constraints:
            loc = f" ({esc(c['location'])})" if c["location"] else ""
            kind_label = {"pain": "כאב", "medical_avoidance": "הימנעות רפואית"}.get(c["kind"], c["kind"])
            lines.append(f"· {esc(kind_label)}{loc}")

    readiness = await user_model.compute_all_readiness(DB, user_id)
    lines += [
        "",
        "<b>📊 מוכנות</b>",
        "<i>מוכן = כל נתוני החובה קיימים. רק אז אפשר להפעיל תוכנית.</i>",
    ]
    for name in ("nutrition", "workout", "safety", "tracking"):
        profile = user_model.READINESS_PROFILES[name]
        r = readiness[name]
        pct = int(r["score"] * 100)
        label = r.get("label") or profile.label
        if name == "safety":
            status_txt = "הושלם" if r["ready"] else "חסר מידע"
            icon = "✅" if r["ready"] else "⚠️"
            lines.append(f"{icon} {esc(label)}: {status_txt}")
        elif r["ready"]:
            lines.append(f"✅ {esc(label)}: {pct}% — מוכן")
        else:
            detail_labels = r.get("missing_labels") or [
                user_model.display_label(key) for key in r["missing"]
            ]
            detail = f" — חסר: {esc(', '.join(detail_labels))}" if detail_labels else ""
            lines.append(f"⚠️ {esc(label)}: {pct}%{detail}")

    return "\n".join(lines) or "עוד אין נתונים בפרופיל."


@runtime_bound(RUNTIME_NAMES)
async def command_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear all active flows — user escape hatch."""
    del context
    if not await is_allowed(update):
        return
    user_id = await ensure_user(update)
    await clear_pending(user_id)
    await clear_meal_fix(user_id)
    await conversation.clear_all_flows(DB, user_id)
    await update.effective_message.reply_text(
        "בוטל הכל. אפשר להתחיל מחדש.",
        reply_markup=home_keyboard(),
    )


@runtime_bound(RUNTIME_NAMES)
async def command_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if not await is_allowed(update):
        return
    user_id = await ensure_user(update)
    await update.effective_message.reply_text(
        await build_profile_text(user_id), parse_mode=ParseMode.HTML
    )


@runtime_bound(RUNTIME_NAMES)
async def command_profile_query(query: Any, user_id: int) -> None:
    await safe_edit(
        query,
        await build_profile_text(user_id),
        InlineKeyboardMarkup([
            [button("✏️ ערוך פרטים", "onb:edit_menu")],
            [button("⬅️ תפריט", "menu:home")],
        ]),
    )


# RE10-14: fields the user can edit from "הפרופיל שלך". Each maps to a
# fact_key that already has a question in questions.py (reused as-is) except
# weight_kg, which is captured via free text (no dedicated question exists —
# it is normally set by Apple Health import or the numeric-confirmation flow).
_EDITABLE_PROFILE_FIELDS: tuple[tuple[str, str], ...] = (
    ("weight_kg", "⚖️ משקל נוכחי"),
    ("height_cm", "📏 גובה"),
    ("age", "🎂 גיל"),
    ("goal_weight_kg", "🎯 משקל יעד"),
    ("goal_timeframe_weeks", "⏳ משך זמן ליעד"),
    ("allergies", "🚫 אלרגיות"),
    ("diet_restrictions", "🥗 איסורים תזונתיים"),
    ("training_location", "📍 מקום אימון"),
    ("equipment", "🏋️ ציוד זמין"),
    ("session_minutes", "⏱️ זמן לאימון"),
    ("training_days_per_week", "📅 ימי אימון בשבוע"),
)

# Editing one of these facts changes the calorie/protein target — offer
# (never force) a re-check of the goal afterward.
_EDIT_TRIGGERS_GOAL_REVIEW = {"weight_kg", "height_cm", "age", "goal_weight_kg", "goal_timeframe_weeks"}


@runtime_bound(RUNTIME_NAMES)
async def render_profile_edit_menu(target: Any, user_id: int) -> None:
    lines = ["<b>איזה פרט לערוך?</b>", ""]
    rows = []
    for key, label in _EDITABLE_PROFILE_FIELDS:
        fact = await user_model.get_fact(DB, user_id, key)
        if fact is not None and fact.get("kind") != user_model.KIND_GAP:
            display = _format_fact_value(key, fact.get("value"))
            lines.append(f"{label}: <b>{esc(display)}</b>")
        else:
            lines.append(f"{label}: <i>טרם דווח</i>")
        rows.append([button(label, f"onb:edit:{key}")])
    rows.append([button("⬅️ חזרה לפרופיל", "menu:profile")])
    await safe_edit(target, "\n".join(lines), InlineKeyboardMarkup(rows))


@runtime_bound(RUNTIME_NAMES)
async def start_profile_field_edit(target: Any, user_id: int, key: str) -> None:
    """Begin editing one profile field (RE10-14): invalidate the old fact and
    either ask its existing question (reusing questions.py) or, for fields
    with no question (weight_kg), prompt free text directly.
    """
    existing = await user_model.get_fact(DB, user_id, key)
    if existing is not None:
        await user_model.invalidate_fact(DB, user_id, key)

    question = questions.question_by_fact_key(key)
    if question is not None:
        await set_flow_state(user_id, "profile_field_edit", key, {})
        rows = []
        if question.options:
            rows = [
                [button(label, f"qa:{question.id}:{index}")]
                for index, (label, _value) in enumerate(question.options)
            ]
        rows.append([button("⬅️ ביטול", "onb:edit_menu")])
        text = f"<b>עריכת פרט</b>\n\n{question.text}"
        await set_pending(user_id, question.id)
        if hasattr(target, "edit_message_text"):
            await safe_edit(target, text, InlineKeyboardMarkup(rows))
        else:
            await target.reply_text(text, reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML)
        return

    # No question exists for this field (currently only weight_kg) — free text.
    await set_pending(user_id, f"__profile_edit_{key}__")
    text = f"כתוב את הערך החדש עבור {esc(user_model.display_label(key))}."
    keyboard = InlineKeyboardMarkup([[button("⬅️ ביטול", "onb:edit_menu")]])
    if hasattr(target, "edit_message_text"):
        await safe_edit(target, text, keyboard)
    else:
        await target.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)


@runtime_bound(RUNTIME_NAMES)
async def finish_profile_field_edit(target: Any, user_id: int, key: str) -> bool:
    """After a profile-edit question is answered, show the updated profile
    and — for fields that affect the calorie target — offer (not force) a
    goal recheck. Returns True (always owns the answer once called)."""
    await clear_flow_state(user_id, "profile_field_edit")
    label = user_model.display_label(key)
    text = f"עודכן: {esc(label)} ✅"
    rows = []
    if key in _EDIT_TRIGGERS_GOAL_REVIEW:
        text += "\n\nהנתון הזה משפיע על היעד הקלורי. לבדוק יעד מעודכן?"
        rows.append([button("🎯 בדוק יעד מעודכן", "menu:goal")])
    rows.append([button("👤 חזרה לפרופיל", "menu:profile")])
    if hasattr(target, "edit_message_text"):
        await safe_edit(target, text, InlineKeyboardMarkup(rows))
    else:
        await target.reply_text(text, reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML)
    return True


@runtime_bound(RUNTIME_NAMES)
def _plan_type_label(plan_type: str) -> str:
    return {"nutrition": "תזונה", "workout": "אימונים", "unified": "שבועית"}.get(plan_type, plan_type)


@runtime_bound(RUNTIME_NAMES)
def _format_candidate(candidate: dict[str, Any], index: int | None = None) -> str:
    """REC-PROGRAM-04-04: Show rationale instead of opaque percentage."""
    title = esc(candidate.get("title", "תוכנית"))
    prefix = f"{index}. " if index is not None else ""
    rationale = candidate.get("rationale") or []
    tradeoffs = candidate.get("tradeoffs") or []
    assumptions = candidate.get("assumptions") or []
    # Show a brief explainable summary instead of an opaque score
    lines = [f"<b>{prefix}{title}</b>"]
    if rationale:
        lines.append("✓ " + " · ".join(esc(str(item)) for item in rationale[:3]))
    if tradeoffs:
        lines.append("△ " + " · ".join(esc(str(item)) for item in tradeoffs[:2]))
    if assumptions:
        lines.append("⚠️ " + " · ".join(esc(str(item)) for item in assumptions[:2]))
    return "\n".join(lines)


@runtime_bound(RUNTIME_NAMES)
async def render_smart_plan_hub(target: Any, user_id: int) -> None:
    from noam_coach.bot.ui import button, safe_edit

    readiness = await user_model.compute_all_readiness(DB, user_id)
    nutrition = await planning.get_active_plan(DB, user_id, "nutrition")
    workout = await planning.get_active_plan(DB, user_id, "workout")
    unified = await planning.get_active_plan(DB, user_id, "unified")

    # REC-PROGRAM-04-01: Show resolved availability in program center
    from noam_coach.services.availability import format_availability_summary, resolve_availability
    avail = await resolve_availability(DB, user_id)
    avail_summary = format_availability_summary(avail)

    lines = ["<b>מרכז התוכנית האישית</b>", ""]
    for name in ("nutrition", "workout", "safety", "tracking"):
        result = readiness[name]
        label = result.get("label") or user_model.READINESS_PROFILES[name].label
        if name == "safety":
            # REC-ONBOARD-02-11: don't show percentage for safety
            icon = "✅" if result["ready"] else "⚠️"
            status_text = "הושלם" if result["ready"] else "חסר מידע"
            lines.append(f"{icon} {esc(label)}: {status_text}")
        else:
            icon = "✅" if result["ready"] else "⚠️"
            if result["ready"]:
                lines.append(f"{icon} {esc(label)}: {int(result['score'] * 100)}% — מוכן")
            else:
                missing_labels = result.get("missing_labels") or [
                    user_model.display_label(key) for key in result.get("missing", [])
                ]
                detail = f" — חסר: {esc(', '.join(missing_labels[:3]))}" if missing_labels else ""
                lines.append(f"{icon} {esc(label)}: {int(result['score'] * 100)}%{detail}")
    # REC-PROGRAM-04-01: availability section
    lines += [""]
    for avail_line in avail_summary.split("\n"):
        lines.append(esc(avail_line))

    lines += [""]
    lines.append(
        f"תזונה פעילה: <b>{esc(nutrition['title'])}</b>" if nutrition else "תזונה פעילה: טרם נבחרה"
    )
    lines.append(
        f"אימונים פעילים: <b>{esc(workout['title'])}</b>" if workout else "אימונים פעילים: טרם נבחרו"
    )
    if unified:
        lines.append(f"תוכנית שבועית: <b>{esc(unified['title'])}</b>")
    rows = [
        [button("🥗 צור 3 הצעות תזונה", "planv2:generate:nutrition")],
        [button("🏋️ צור 3 הצעות אימון", "planv2:generate:workout")],
    ]
    if nutrition and workout:
        rows.append([button("📅 בנה שבוע מאוחד", "planv2:unify")])
    if unified:
        rows.append([button("👁️ הצג את השבוע", "planv2:show:unified")])
    rows.append([button("👤 כך הבנתי אותך", "planv2:profile")])
    rows.append([button("⬅️ תפריט", "menu:home")])
    keyboard = InlineKeyboardMarkup(rows)
    if hasattr(target, "edit_message_text"):
        await safe_edit(target, "\n".join(lines), keyboard)
    else:
        await target.reply_text("\n".join(lines), reply_markup=keyboard, parse_mode=ParseMode.HTML)


# RE10-11 — three-step workout wizard: type -> structure -> exercises.
# Reuses the existing 3-candidate generation (planning.generate_candidates)
# and the existing single-plan activation (planv2:select) — the wizard only
# changes what the user sees at each step, not the underlying data model.
_STRATEGY_RECOMMENDATION_FOR_GOAL: dict[str, str] = {
    "fat_loss_muscle_retention": "consistency",
    "muscle_gain": "performance",
    "strength": "performance",
    "general_health": "balanced",
}
_STRATEGY_LABELS: dict[str, str] = {
    "consistency": "מקסימום עקביות",
    "balanced": "מאוזנת",
    "performance": "ביצועים",
}
_STRATEGY_WHY: dict[str, str] = {
    "consistency": "פחות ימים, קל יותר להתמיד — טוב לירידה במשקל ולשמירה על שגרה.",
    "balanced": "איזון בין נפח להתאוששות — טוב לבריאות וכושר כללי.",
    "performance": "יותר נפח והזדמנויות להתקדם — טוב לבניית מסה או כוח.",
}


@runtime_bound(RUNTIME_NAMES)
async def render_workout_type_choice(target: Any, user_id: int) -> None:
    """Wizard step A (RE10-11): choose a workout TYPE before seeing structure."""
    candidates = await planning.list_plan_candidates(DB, user_id, "workout")  # type: ignore[arg-type]
    if not candidates:
        await safe_edit(
            target,
            "אין כרגע הצעות שמורות. צור הצעות חדשות.",
            InlineKeyboardMarkup([[button("⬅️ לתוכניות", "menu:smartplan")]]),
        )
        return
    primary_goal = str(await user_model.get_value(DB, user_id, "primary_goal") or "")
    recommended = _STRATEGY_RECOMMENDATION_FOR_GOAL.get(primary_goal)

    flow = await conversation.get_active_flow(DB, user_id)
    if flow.name != conversation.FlowName.workout_plan_selection:
        await conversation.set_active_flow(
            DB, user_id, conversation.FlowName.workout_plan_selection,
            step="choose_type",
            payload={"candidate_ids": [int(item["id"]) for item in candidates]},
            expiry_minutes=24 * 60,
        )
        flow = await conversation.get_active_flow(DB, user_id)

    lines = ["<b>שלב 1 מתוך 3 — איזה סוג תוכנית אימונים?</b>", ""]
    rows = []
    for candidate in candidates:
        strategy = str(candidate.get("strategy") or "")
        label = _STRATEGY_LABELS.get(strategy, candidate.get("title", strategy))
        why = _STRATEGY_WHY.get(strategy, "")
        badge = " (מומלץ עבורך)" if strategy == recommended else ""
        lines.append(f"<b>{esc(label)}{badge}</b>")
        if why:
            lines.append(f"<i>{esc(why)}</i>")
        lines.append("")
        callback = conversation.encode_callback(
            "planv2", "wiz_type", strategy, version=flow.version, flow_id=flow.flow_id,
        )
        button_label = f"{label}{' ⭐' if strategy == recommended else ''}"
        rows.append([button(button_label, callback)])
    rows.append([button("⬅️ לתוכניות", "menu:smartplan")])
    await safe_edit(target, "\n".join(lines), InlineKeyboardMarkup(rows))


@runtime_bound(RUNTIME_NAMES)
async def render_workout_structure_choice(target: Any, user_id: int, strategy: str) -> None:
    """Wizard step B (RE10-11): show the chosen type's day/session structure."""
    candidates = await planning.list_plan_candidates(DB, user_id, "workout")  # type: ignore[arg-type]
    candidate = next((c for c in candidates if c.get("strategy") == strategy), None)
    if candidate is None:
        await render_workout_type_choice(target, user_id)
        return

    flow = await conversation.get_active_flow(DB, user_id)
    await conversation.set_active_flow(
        DB, user_id, conversation.FlowName.workout_plan_selection,
        step="choose_structure",
        payload={**flow.payload, "chosen_strategy": strategy, "chosen_plan_id": int(candidate["id"])},
        expiry_minutes=24 * 60,
    )
    flow = await conversation.get_active_flow(DB, user_id)

    label = _STRATEGY_LABELS.get(strategy, candidate.get("title", strategy))
    lines = [f"<b>שלב 2 מתוך 3 — מבנה התוכנית: {esc(label)}</b>", ""]
    lines.append(_format_candidate(candidate, None))
    payload = candidate.get("payload", {})
    sessions = sorted(payload.get("sessions", []), key=lambda s: sunday_first_key(s.get("weekday", 0)))
    for session in sessions:
        day_name = session.get("weekday_name", "")
        session_name = session.get("name", "")
        exercises = session.get("exercises", [])
        ex_count = len(exercises)
        session_mins = session.get("minutes", 0)
        ex_preview = ", ".join(esc(e.get("name_he") or e.get("name", "")) for e in exercises[:3])
        if ex_count > 3:
            ex_preview += f" +{ex_count - 3}"
        lines.append(
            f"  📋 {esc(day_name)} · {esc(session_name)} "
            f"({ex_count} תרגילים, {session_mins} דק׳): {ex_preview}"
        )
    lines.append("")

    confirm_cb = conversation.encode_callback(
        "planv2", "wiz_review", str(candidate["id"]), version=flow.version, flow_id=flow.flow_id,
    )
    back_cb = conversation.encode_callback(
        "planv2", "wiz_back_type", "0", version=flow.version, flow_id=flow.flow_id,
    )
    rows = [
        [button("➡️ המשך לאישור תרגילים", confirm_cb)],
        [button("⬅️ חזרה לבחירת סוג", back_cb)],
        [button("⬅️ לתוכניות", "menu:smartplan")],
    ]
    await safe_edit(target, "\n".join(lines), InlineKeyboardMarkup(rows))


@runtime_bound(RUNTIME_NAMES)
async def render_workout_exercise_review(target: Any, user_id: int, plan_id: int) -> None:
    """Wizard step C (RE10-11): final exercise list before activation, with
    the existing per-exercise edit entry point (editparams_menu) available
    before the user commits."""
    candidates = await planning.list_plan_candidates(DB, user_id, "workout")  # type: ignore[arg-type]
    candidate = next((c for c in candidates if int(c["id"]) == plan_id), None)
    if candidate is None:
        await render_workout_type_choice(target, user_id)
        return

    flow = await conversation.get_active_flow(DB, user_id)
    await conversation.set_active_flow(
        DB, user_id, conversation.FlowName.workout_plan_selection,
        step="review_exercises",
        payload={**flow.payload, "chosen_plan_id": plan_id},
        expiry_minutes=24 * 60,
    )
    flow = await conversation.get_active_flow(DB, user_id)

    label = _STRATEGY_LABELS.get(candidate.get("strategy", ""), candidate.get("title", ""))
    lines = [f"<b>שלב 3 מתוך 3 — אישור תרגילים: {esc(label)}</b>", ""]
    payload = candidate.get("payload", {})
    rows = []
    sessions = sorted(payload.get("sessions", []), key=lambda s: sunday_first_key(s.get("weekday", 0)))
    for session in sessions:
        day_name = session.get("weekday_name", "")
        session_name = session.get("name", "")
        exercises = session.get("exercises", [])
        lines.append(f"<b>{esc(day_name)} · {esc(session_name)}</b>")
        for exercise_entry in exercises:
            name = esc(exercise_entry.get("name_he") or exercise_entry.get("name", ""))
            sets = exercise_entry.get("sets")
            rmin = exercise_entry.get("rmin")
            rmax = exercise_entry.get("rmax")
            lines.append(f"  • {name} — {sets}×{rmin}-{rmax}")
        lines.append("")
        code = session.get("code")
        if code:
            rows.append([button(f"🔁 החלף/ערוך תרגילים ב-{esc(session_name)}", f"editparams_menu:{code}")])

    select_cb = conversation.encode_callback(
        "planv2", "select", str(plan_id), version=flow.version, flow_id=flow.flow_id,
    )
    back_cb = conversation.encode_callback(
        "planv2", "wiz_type", str(candidate.get("strategy", "")), version=flow.version, flow_id=flow.flow_id,
    )
    rows.append([button("✅ אשר תוכנית", select_cb)])
    rows.append([button("⬅️ חזרה למבנה", back_cb)])
    rows.append([button("⬅️ לתוכניות", "menu:smartplan")])
    await safe_edit(target, "\n".join(lines), InlineKeyboardMarkup(rows))


@runtime_bound(RUNTIME_NAMES)
async def render_candidate_list(target: Any, user_id: int, plan_type: str) -> None:
    candidates = await planning.list_plan_candidates(DB, user_id, plan_type)  # type: ignore[arg-type]
    if not candidates:
        await safe_edit(
            target,
            "אין כרגע הצעות שמורות. צור הצעות חדשות.",
            InlineKeyboardMarkup([[button("⬅️ לתוכניות", "menu:smartplan")]]),
        )
        return
    flow_name = (
        conversation.FlowName.nutrition_plan_selection
        if plan_type == "nutrition"
        else conversation.FlowName.workout_plan_selection
    )
    await conversation.set_active_flow(
        DB,
        user_id,
        flow_name,
        step="choose",
        payload={"candidate_ids": [int(item["id"]) for item in candidates]},
        expiry_minutes=24 * 60,
    )
    flow = await conversation.get_active_flow(DB, user_id)
    lines = [f"<b>שלוש הצעות {_plan_type_label(plan_type)}</b>", ""]
    rows = []
    for index, candidate in enumerate(candidates, start=1):
        lines.append(_format_candidate(candidate, index))
        # REC-PROGRAM-04-02 / D6: show every session, not just the first 4 —
        # once workout candidates can differ in frequency (RE10-3 D13), a
        # 5- or 6-day performance plan must not have its later days hidden
        # from the user before they choose.
        if plan_type == "workout":
            payload = candidate.get("payload", {})
            sessions = sorted(
                payload.get("sessions", []), key=lambda s: sunday_first_key(s.get("weekday", 0))
            )
            for session in sessions:
                day_name = session.get("weekday_name", "")
                session_name = session.get("name", "")
                exercises = session.get("exercises", [])
                ex_count = len(exercises)
                session_mins = session.get("minutes", 0)
                ex_preview = ", ".join(
                    esc(e.get("name_he") or e.get("name", ""))
                    for e in exercises[:3]
                )
                if ex_count > 3:
                    ex_preview += f" +{ex_count - 3}"
                lines.append(
                    f"  📋 {esc(day_name)} · {esc(session_name)} "
                    f"({ex_count} תרגילים, {session_mins} דק׳): {ex_preview}"
                )
        lines.append("")
        callback = conversation.encode_callback(
            "planv2",
            "select",
            str(candidate["id"]),
            version=flow.version,
            flow_id=flow.flow_id,
        )
        rows.append([button(f"בחר הצעה {index}: {candidate['title']}", callback)])
    rows.append([button("🔄 צור מחדש", f"planv2:generate:{plan_type}")])
    rows.append([button("⬅️ לתוכניות", "menu:smartplan")])
    await safe_edit(target, "\n".join(lines), InlineKeyboardMarkup(rows))


@runtime_bound(RUNTIME_NAMES)
async def render_profile_snapshot(target: Any, user_id: int) -> None:
    snapshot = await planning.profile_snapshot(DB, user_id)
    facts = snapshot["facts"]
    # REC-PROGRAM-04-01: Resolve and display training availability
    from noam_coach.services.availability import format_availability_summary, resolve_availability
    avail = await resolve_availability(DB, user_id)
    lines = ["<b>כך הבנתי אותך</b>", ""]
    for key in (
        "primary_goal", "weight_kg", "work_schedule", "commute_minutes",
        "training_days_per_week", "weekly_availability", "session_minutes",
        "training_location", "equipment", "cooking_capacity", "diet_restrictions",
        "allergies", "active_pain",
    ):
        fact = facts.get(key)
        if not fact or fact.get("kind") == user_model.KIND_GAP:
            continue
        spec = user_model.FACT_REGISTRY.get(key)
        label = spec.label if spec else key
        # REC-PLAN-MEAL-03-07: Use Hebrew source labels, never raw source keys
        source = user_model.SOURCE_LABELS.get(fact.get("source"), "")
        confirm = "" if fact.get("confirmed") else " · טרם אושר"
        display_val = _format_fact_value(key, fact.get("value"))
        lines.append(f"• <b>{esc(label)}</b>: {esc(display_val)} <i>({esc(source)}{confirm})</i>")
    # REC-PROGRAM-04-01: Availability summary in profile snapshot
    avail_text = format_availability_summary(avail)
    lines += [""]
    for avail_line in avail_text.split("\n"):
        lines.append(esc(avail_line))
    missing = []
    for group in ("nutrition", "workout", "safety"):
        missing.extend(snapshot["readiness"][group]["missing"])
    missing = list(dict.fromkeys(missing))
    if missing:
        lines += ["", "<b>חסר לפני תוכנית מלאה</b>"]
        for key in missing[:8]:
            lines.append(f"• {esc(planning.FACT_LABELS.get(key, user_model.FACT_REGISTRY.get(key).label if user_model.FACT_REGISTRY.get(key) else key))}")
    else:
        lines += ["", "✅ יש מספיק מידע ליצירת הצעות."]
    await safe_edit(
        target,
        "\n".join(lines),
        InlineKeyboardMarkup([[button("⬅️ לתוכניות", "menu:smartplan")]]),
    )


@runtime_bound(RUNTIME_NAMES)
async def render_unified_plan(target: Any, user_id: int) -> None:
    plan = await planning.get_active_plan(DB, user_id, "unified")
    if not plan:
        await safe_edit(target, "עוד אין תוכנית שבועית מאוחדת.", InlineKeyboardMarkup([[button("⬅️ לתוכניות", "menu:smartplan")]]))
        return
    lines = ["<b>התוכנית השבועית שלי</b>", ""]
    for day in plan["payload"].get("days", []):
        lines.append(f"<b>{esc(day['weekday_name'])}</b>")
        # D12: render the chronologically-merged "items" list (falls back to
        # the old meals-then-workouts order only for a unified plan payload
        # saved before this field existed).
        items = day.get("items")
        if items is None:
            items = [{**m, "type": "meal"} for m in day.get("meals", [])] + [
                {**s, "type": "workout"} for s in day.get("workouts", [])
            ]
        for item in items:
            time_text = f"{item.get('time')} · " if item.get("time") else ""
            icon = "🏋️" if item.get("type") == "workout" else "🍽️"
            default_name = "אימון" if item.get("type") == "workout" else "ארוחה"
            lines.append(f"{icon} {esc(time_text + item.get('name', default_name))}")
        lines.append("")
    await safe_edit(target, "\n".join(lines), InlineKeyboardMarkup([[button("⬅️ לתוכניות", "menu:smartplan")]]))


@runtime_bound(RUNTIME_NAMES)
async def render_plan_builder(query: Any, user_id: int) -> None:
    profile = await load_routine_profile(user_id)
    workout = profile.get("workout") or {}
    days = workout.get("common_weekdays") or []
    hour = workout.get("typical_hour")
    freq = workout.get("weekly_frequency")

    lines = ["<b>בניית תוכנית אימונים</b>", ""]

    # "This is what I understood" — show known facts relevant to the plan.
    understood = []
    goal = await user_model.get_value(DB, user_id, "primary_goal")
    if goal:
        understood.append(f"מטרה: {_format_fact_value('primary_goal', goal)}")
    experience = await user_model.get_value(DB, user_id, "strength_experience")
    if experience:
        understood.append(f"ניסיון: {_format_fact_value('strength_experience', experience)}")
    location = await user_model.get_value(DB, user_id, "training_location")
    if location:
        understood.append(f"מיקום: {_format_fact_value('training_location', location)}")
    session_min = await user_model.get_value(DB, user_id, "session_minutes")
    if session_min:
        understood.append(f"זמן לאימון: {int(session_min)} דקות")

    if understood:
        lines.append("<b>זה מה שאני יודע:</b>")
        for item in understood:
            lines.append(f"· {item}")
        lines.append("")

    plan_constraints = await gather_plan_constraints(user_id)
    constraint_text = format_constraints_summary(plan_constraints)
    if constraint_text:
        lines.append(constraint_text)
        lines.append("")

    if days:
        day_labels = ", ".join(weekday_labels_he(days))
        lines.append(f"לפי השעון אתה מתאמן בדרך כלל בימים {day_labels}.")
        if hour:
            lines[-1] = lines[-1][:-1] + f" סביב {hour}."
    if freq is not None and freq > 0:
        lines.append(f"תדירות מזוהה: ~{freq} בשבוע.")
    elif not days:
        lines.append("עוד אין מספיק אימונים כדי לזהות תדירות רגילה.")
    lines.append("")
    lines.append(
        'כמה אימונים בשבוע תרצה? בחר מספר, או לחץ "תמליץ לי" ואבחר לפי מה שאני יודע.'
    )
    await set_pending(user_id, "__plan_frequency__")
    await safe_edit(
        query,
        "\n".join(lines),
        onboarding_frequency_keyboard(),
    )


@runtime_bound(RUNTIME_NAMES)
async def ask_deferred_for_plan(target: Any, user_id: int, frequency: int) -> bool:
    """Ask the first unanswered deferred question before building a plan.

    Returns True if a question was asked (caller should wait for the answer),
    False if all deferred questions are already answered and the plan can proceed.
    """
    for key, _why in questions.DEFERRED_GAP_KEYS.items():
        fact = await user_model.get_fact(DB, user_id, key)
        if fact is not None:
            continue
        q = questions.question_by_id(f"q_{key}")
        if q is None:
            continue
        # Store the frequency so we can resume after the answer.
        await set_flow_state(user_id, "deferred_plan", str(frequency), {"remaining_key": key})
        await set_pending(user_id, q.id)
        if q.options:
            rows = [
                [button(label, f"qa:{q.id}:{index}")]
                for index, (label, _value) in enumerate(q.options)
            ]
            rows.append([button("דלג", f"qa:{q.id}:skip")])
            await target.reply_text(
                q.text,
                reply_markup=InlineKeyboardMarkup(rows),
                parse_mode=ParseMode.HTML,
            )
        else:
            await target.reply_text(
                f'{q.text}\n\n(כתוב "ביטול" כדי לדלג.)',
                parse_mode=ParseMode.HTML,
            )
        return True
    # Clean up any leftover deferred_plan state.
    await clear_flow_state(user_id, "deferred_plan")
    return False


@runtime_bound(RUNTIME_NAMES)
async def check_plan_readiness(user_id: int) -> list[str]:
    """Return a list of missing requirements that block plan activation.

    Empty list means the user is ready for a workout plan.
    """
    gaps: list[str] = []
    pending = await questions.pending_safety_questions(DB, user_id)
    if pending:
        gaps.append("שאלות בטיחות לא נענו")
    goal = await user_model.get_value(DB, user_id, "primary_goal")
    if goal is None:
        gaps.append("לא נבחרה מטרה ראשית")
    return gaps


@runtime_bound(RUNTIME_NAMES)
async def build_weekly_plan(user_id: int, frequency: int) -> dict[str, Any]:
    """Map a split onto the user's detected training days (or sensible
    defaults), store it as the active plan in the user model, and return it."""
    # Clamp here so no caller can request a frequency without a matching split.
    frequency = max(MIN_FREQUENCY, min(MAX_FREQUENCY, frequency))
    profile = await load_routine_profile(user_id)
    workout = profile.get("workout") or {}
    detected_days = list(workout.get("common_weekdays") or [])
    hour = workout.get("typical_hour")

    split = SPLIT_BY_FREQUENCY.get(frequency, SPLIT_BY_FREQUENCY[3])
    # Choose days: prefer detected days; pad with a full weekday spread.
    default_spread = [6, 1, 3, 0, 4, 5, 2]  # Sun,Tue,Thu,Mon,Fri,Sat,Wed (0=Mon)
    days = detected_days + [d for d in default_spread if d not in detected_days]
    chosen_days = sorted(days[:frequency])

    sessions = [
        {
            "weekday": chosen_days[i]
            if i < len(chosen_days)
            else default_spread[i % len(default_spread)],
            "time": hour,
            "code": split[i],
            "name": PLANS[split[i]]["name"],
        }
        for i in range(frequency)
    ]
    days_source = "detected" if detected_days else "default"
    plan = {
        "frequency": frequency,
        "method": "moving_weight_double_progression",
        "sessions": sessions,
        "days_source": days_source,
    }
    await user_model.set_fact(
        DB,
        user_id,
        "training_days_per_week",
        frequency,
        kind=user_model.KIND_FACT,
        source=user_model.SOURCE_USER,
        confirmed=True,
    )
    await user_model.set_fact(
        DB,
        user_id,
        "active_workout_plan",
        plan,
        kind=user_model.KIND_FACT,
        source=user_model.SOURCE_SYSTEM,
        confidence=0.8,
        confirmed=True,
        affects=("workout_schedule",),
    )
    return plan


@runtime_bound(RUNTIME_NAMES)
def format_weekly_plan(plan: dict[str, Any]) -> str:
    lines = [
        f"<b>התוכנית השבועית שלך — {plan['frequency']} אימונים</b>",
        "",
    ]
    sorted_sessions = sorted(plan["sessions"], key=lambda s: sunday_first_key(s["weekday"]))
    for i, s in enumerate(sorted_sessions, start=1):
        when = weekday_he(s["weekday"])
        at = f" · {s['time']}" if s.get("time") else ""
        lines.append(f"{i}. יום {when}{at} — {s['name']}")
    if plan.get("days_source") == "default":
        lines += [
            "",
            "⚠️ <i>הימים נבחרו אוטומטית — עדיין אין מספיק נתונים לזהות את "
            "הימים הקבועים שלך. אפשר לשנות למטה.</i>",
        ]
    lines += [
        "",
        "<i>שיטת משקל נע: נשארים על אותו משקל עד שליטה בטווח החזרות, ואז "
        "עולים מדרגה. כל אימון נבנה לפי הביצועים האחרונים שלך.</i>",
        "",
        'פתח "אימון" כדי להתחיל את האימון הבא לפי הסדר.',
    ]
    return "\n".join(lines)


_CANCEL_WORDS = {"ביטול", "בטל", "עזוב", "תעזוב", "לא משנה", "skip", "cancel", "דלג"}


def _diet_type_keyboard(food_item: str) -> InlineKeyboardMarkup:
    safe_item = _safe_cb(food_item)
    return InlineKeyboardMarkup([
        [button("🚫 מעדיף להימנע", f"qa:diet_type:preference:{safe_item}")],
        [button("🤢 גורם לי לאי־נוחות", f"qa:diet_type:intolerance:{safe_item}")],
        [button("⚠️ רגישות", f"qa:diet_type:sensitivity:{safe_item}")],
        [button("🆘 אלרגיה מאובחנת", f"qa:diet_type:allergy:{safe_item}")],
        [button("❌ לא התכוונתי להימנע", f"qa:diet_type:cancel:{safe_item}")],
    ])


@runtime_bound(RUNTIME_NAMES)
async def handle_onboarding_text(update: Update, user_id: int) -> bool:
    """Capture free-text answers tied to onboarding/safety. Returns True if it
    consumed the message."""
    # ``active_flow`` is authoritative; the dictionary is only a compatibility cache.
    flow = await conversation.expire_if_needed(DB, user_id)
    pending = (
        flow.step
        if (flow.is_question or flow.name == conversation.FlowName.routine_confirm)
        else None
    )
    if not pending:
        PENDING_QUESTION.pop(user_id, None)
        return False
    PENDING_QUESTION[user_id] = pending
    message = update.effective_message
    text = (message.text or "").strip()
    if not text:
        return False

    # Universal cancel — user can always exit a pending flow.
    if text.strip() in _CANCEL_WORDS:
        await clear_pending(user_id)
        await message.reply_text("בוטל. אפשר להמשיך כרגיל.")
        return True

    if pending.startswith("__profile_edit_") and pending.endswith("__"):
        # RE10-14: free-text edit for a profile field with no dedicated
        # question (currently only weight_kg).
        key = pending[len("__profile_edit_"):-2]
        try:
            value: Any = float(text.strip().replace(",", "."))
        except ValueError:
            await message.reply_text("כתוב מספר, למשל 82.5.")
            return True
        await user_model.set_fact(
            DB, user_id, key, value,
            kind=user_model.KIND_FACT,
            source=user_model.SOURCE_USER,
            confirmed=True,
        )
        await clear_pending(user_id)
        await finish_profile_field_edit(message, user_id, key)
        return True

    if pending.startswith("__health_edit_") and pending.endswith("__"):
        # RE10-4: user typed a corrected value for one imported Health item.
        from noam_coach.services.health_jobs import (
            apply_health_wizard_text_edit,
            ask_next_health_confirm_step,
            finish_health_confirm_wizard,
        )

        key = pending[len("__health_edit_"):-2]
        if "." in key:
            # RE12: a workout sub-step (frequency / days / hour) — parse the
            # correction into the pattern and the matching plan facts.
            handled, reply = await apply_health_wizard_text_edit(user_id, key, text)
            if not handled:
                await message.reply_text(reply)
                return True
            await clear_pending(user_id)
            if not await ask_next_health_confirm_step(message, user_id, ack_text=reply):
                await finish_health_confirm_wizard(message, user_id, ack_text=reply)
            return True
        await user_model.set_fact(
            DB, user_id, key, text.strip(),
            kind=user_model.KIND_FACT,
            source=user_model.SOURCE_USER,
            confirmed=True,
        )
        await clear_pending(user_id)
        ack = f"עודכן: {esc(user_model.display_label(key))} — {esc(text.strip())} ✅"
        if not await ask_next_health_confirm_step(message, user_id, ack_text=ack):
            await finish_health_confirm_wizard(message, user_id, ack_text=ack)
        return True

    if pending == "__med_name__":
        await clear_pending(user_id)
        await record_medication(user_id, text.strip(), source="user_text")
        await message.reply_text(
            f"רשמתי שלקחת {esc(text.strip())}. זה יישמר ביומן שלך, "
            "ובהמשך נוכל להשוות מול תיאבון ואימונים."
        )
        return True

    if pending == "__plan_frequency__":
        import re as _re

        match = _re.search(r"\d+", text)
        if not match:
            await message.reply_text("כתוב מספר אימונים בשבוע, למשל 3.")
            return True
        frequency = max(MIN_FREQUENCY, min(MAX_FREQUENCY, int(match.group(0))))
        await clear_pending(user_id)
        # Ask deferred questions before building the plan.
        if await ask_deferred_for_plan(message, user_id, frequency):
            return True
        plan = await build_weekly_plan(user_id, frequency)
        await message.reply_text(
            format_weekly_plan(plan),
            reply_markup=InlineKeyboardMarkup(
                [
                    [button("🏋️ התחל אימון", "menu:workout")],
                    [button("⬅️ תפריט", "menu:home")],
                ]
            ),
            parse_mode=ParseMode.HTML,
        )
        return True

    if pending == "__basics_fix__":
        # Lightweight parse for common corrections: "<label> <number>".
        applied = await apply_basics_fix(user_id, text)
        await clear_pending(user_id)
        await message.reply_text(
            "עודכן, תודה."
            if applied
            else 'לא הצלחתי לפענח את התיקון. אפשר לנסות שוב למשל: "משקל 90".'
        )
        if applied:
            await show_onboarding_patterns(message, user_id)
        return True

    if pending == "__pain_location__":
        await DB.execute(
            "UPDATE medical_constraints SET location=? "
            "WHERE id=(SELECT MAX(id) FROM medical_constraints "
            "WHERE user_id=? AND kind='pain')",
            (text, user_id),
        )
        await user_model.set_fact(
            DB,
            user_id,
            "active_pain",
            {"location": text, "status": "active"},
            kind=user_model.KIND_FACT,
            source=user_model.SOURCE_USER,
            confirmed=True,
        )
        await clear_pending(user_id)
        await message.reply_text(
            f"רשמתי כאב ב{text}. אנסה להסיר או להחליף תרגילים שמעמיסים על האזור הזה. "
            "אם הכאב חד, מתגבר או מגביל תנועה — כדאי בדיקה מקצועית."
        )
        if await continue_after_plan_completion_answer(message, user_id):
            return True
        if not await ask_next_question(message, user_id):
            await finish_onboarding(message, user_id)
        return True

    if pending == "__avoidance_detail__":
        await DB.execute(
            "UPDATE medical_constraints SET note=? "
            "WHERE id=(SELECT MAX(id) FROM medical_constraints "
            "WHERE user_id=? AND kind='medical_avoidance')",
            (text, user_id),
        )
        await clear_pending(user_id)
        await message.reply_text("נרשם, אתאים את התוכנית בהתאם.")
        if await continue_after_plan_completion_answer(message, user_id):
            return True
        if not await ask_next_question(message, user_id):
            await finish_onboarding(message, user_id)
        return True

    if pending == "__allergy_detail__":
        await user_model.set_fact(
            DB, user_id, "allergies", text,
            kind=user_model.KIND_FACT, source=user_model.SOURCE_USER,
            confirmed=True, affects=("menu_planning", "safety"),
        )
        await clear_pending(user_id)
        await message.reply_text(f"רשמתי: {text}. אתחשב בזה בתכנון התזונה.")
        if await continue_after_plan_completion_answer(message, user_id):
            return True
        if not await ask_next_question(message, user_id):
            await finish_onboarding(message, user_id)
        return True

    if pending == "__equipment_detail__":
        await user_model.set_fact(
            DB, user_id, "equipment", text,
            kind=user_model.KIND_FACT, source=user_model.SOURCE_USER,
            confirmed=True, affects=("exercise_selection",),
        )
        await clear_pending(user_id)
        await message.reply_text(f"רשמתי: {text}. אבחר תרגילים בהתאם.")
        if await continue_after_plan_completion_answer(message, user_id):
            return True
        if not await ask_next_question(message, user_id):
            await finish_onboarding(message, user_id)
        return True

    if pending == "__manual_goal_calories__":
        import re as _re

        if questions.looks_like_time_range(text):
            await message.reply_text("זה נראה כמו טווח שעות. כרגע ביקשתי יעד קלורי יומי, למשל 2100.")
            return True
        match = _re.search(r"\d+(?:\.\d+)?", text)
        if not match:
            await message.reply_text("כתוב מספר קלוריות, למשל 2100.")
            return True
        value = float(match.group(0))
        if not (800 <= value <= 6000):
            await message.reply_text("המספר צריך להיות בטווח סביר של 800–6000 קלוריות ליום.")
            return True
        await clear_pending(user_id)
        await message.reply_text(
            f"לוודא: יעד קלורי יומי של <b>{value:g}</b>?",
            reply_markup=InlineKeyboardMarkup(
                [
                    [button("✅ אשר", f"confirm:goal_cal:{value:g}")],
                    [button("❌ בטל", "confirm:cancel:0")],
                ]
            ),
            parse_mode=ParseMode.HTML,
        )
        return True

    if pending == "q_daily_routine":
        progress = await message.reply_text("מנתח את התיאור שלך…")
        extraction = await extract_daily_routine(text)
        await save_routine_extraction(user_id, extraction)
        _q_daily_routine = questions.question_by_id("q_daily_routine")
        if _q_daily_routine is not None:
            await questions.record_answer(DB, user_id, _q_daily_routine, text)
        confirmation_text = format_routine_confirmation(extraction)
        await progress.edit_text(
            confirmation_text,
            reply_markup=InlineKeyboardMarkup([
                [button("✅ נכון", "routine:confirm")],
                [button("✏️ לתקן", "routine:fix")],
                [button("⏭️ דלג", "routine:skip")],
            ]),
            parse_mode=ParseMode.HTML,
        )
        await clear_pending(user_id)
        await set_pending(user_id, "__routine_confirm__")
        return True

    if pending == "__routine_confirm__":
        # A text response here is a correction, not an implicit approval.
        progress = await message.reply_text("מעדכן את השגרה לפי התיקון…")
        extraction = await extract_daily_routine(text)
        await save_routine_extraction(user_id, extraction)
        await confirm_routine_facts(user_id)
        await clear_pending(user_id)
        await progress.edit_text(
            "תודה, התיקון נשמר והפרופיל עודכן ✅",
            parse_mode=ParseMode.HTML,
        )
        if await continue_after_plan_completion_answer(message, user_id):
            return True
        if not await ask_next_question(message, user_id):
            await finish_onboarding(message, user_id)
        return True

    # Otherwise it's a free-text answer to a normal question.
    question = questions.question_by_id(pending)

    # RE11: questions with free_text_fallback=True keep a single "none" button
    # but also accept a typed answer directly at the same prompt — the user
    # should never have to tap "יש"/"אחר" before typing. Route the typed text
    # through the same detail-persistence used by the button follow-up flow.
    if question is not None and question.free_text_fallback and question.fact_key in (
        "active_pain", "medical_avoidance", "allergies", "equipment",
    ):
        if question.fact_key == "equipment":
            # Free text may also match one of the quick-pick categories.
            normalized = text.strip()
            matched_value = next(
                (value for label, value in question.options if label == normalized),
                None,
            )
            final_value = matched_value if matched_value is not None else text
            await user_model.set_fact(
                DB, user_id, "equipment", final_value,
                kind=user_model.KIND_FACT, source=user_model.SOURCE_USER,
                confirmed=True, affects=("exercise_selection",),
            )
            await clear_pending(user_id)
            await message.reply_text(f"רשמתי: {text}. אבחר תרגילים בהתאם.")
        elif question.fact_key == "active_pain":
            await save_medical_constraint(
                user_id, kind="pain", note="reported during onboarding",
                affects=("exercise_selection",),
            )
            await DB.execute(
                "UPDATE medical_constraints SET location=? "
                "WHERE id=(SELECT MAX(id) FROM medical_constraints "
                "WHERE user_id=? AND kind='pain')",
                (text, user_id),
            )
            await user_model.set_fact(
                DB, user_id, "active_pain", {"location": text, "status": "active"},
                kind=user_model.KIND_FACT, source=user_model.SOURCE_USER,
                confirmed=True,
            )
            await clear_pending(user_id)
            await message.reply_text(
                f"רשמתי כאב ב{text}. אנסה להסיר או להחליף תרגילים שמעמיסים על האזור הזה. "
                "אם הכאב חד, מתגבר או מגביל תנועה — כדאי בדיקה מקצועית."
            )
        elif question.fact_key == "medical_avoidance":
            await save_medical_constraint(
                user_id, kind="medical_avoidance", note="reported during onboarding",
                affects=("exercise_selection",),
            )
            await DB.execute(
                "UPDATE medical_constraints SET note=? "
                "WHERE id=(SELECT MAX(id) FROM medical_constraints "
                "WHERE user_id=? AND kind='medical_avoidance')",
                (text, user_id),
            )
            await user_model.set_fact(
                DB, user_id, "medical_avoidance", text,
                kind=user_model.KIND_FACT, source=user_model.SOURCE_USER,
                confirmed=True, affects=("exercise_selection", "safety"),
            )
            await clear_pending(user_id)
            await message.reply_text("נרשם, אתאים את התוכנית בהתאם.")
        else:  # allergies — reuse the existing dietary-answer parser/classifier.
            try:
                parsed_items = _parse_dietary_answer(text)
            except Exception:  # noqa: BLE001
                LOGGER.exception("Dietary restriction parse failed for: %r", text)
                parsed_items = []
                await event_log.append_event(
                    DB, user_id, "dietary_restriction_parse_failed",
                    entity="fact", entity_id=question.fact_key,
                    source="onboarding",
                    properties={"raw_text": text[:200]},
                )
            if parsed_items:
                first_item = parsed_items[0]
                await clear_pending(user_id)
                await message.reply_text(
                    f"איך להתייחס ל{esc(first_item)}?",
                    reply_markup=_diet_type_keyboard(first_item),
                    parse_mode=ParseMode.HTML,
                )
                return True
            if not text.strip():
                await message.reply_text("לא קיבלתי תשובה. אפשר לכתוב למשל: \"אני נמנע מקשיו\".")
                return True
            await user_model.set_fact(
                DB, user_id, "allergies", text,
                kind=user_model.KIND_FACT, source=user_model.SOURCE_USER,
                confirmed=True, affects=("menu_planning", "safety"),
            )
            await clear_pending(user_id)
            await message.reply_text(f"רשמתי: {text}. אתחשב בזה בתכנון התזונה.")

        await advance_after_answer(message, user_id)
        return True

    if question is not None and not question.options:
        value: Any = text
        if question.numeric:
            import re as _re

            if questions.looks_like_time_range(text):
                await message.reply_text("זה נראה כמו טווח שעות. כרגע ביקשתי מספר לשאלה הזו.")
                return True
            match = _re.search(r"\d+(?:\.\d+)?", text)
            if not match:
                await message.reply_text("כתוב מספר בבקשה, למשל 3.")
                return True
            try:
                value = questions.normalize_answer(question, match.group(0))
            except ValueError as exc:
                await message.reply_text(str(exc))
                return True

        # REC-PLAN-MEAL-03-01: Parse dietary restriction answers with
        # a structured follow-up for restriction type classification.
        if question.fact_key in ("diet_restrictions", "allergies"):
            try:
                parsed_items = _parse_dietary_answer(text)
            except Exception:  # noqa: BLE001
                LOGGER.exception("Dietary restriction parse failed for: %r", text)
                parsed_items = []
                await event_log.append_event(
                    DB, user_id, "dietary_restriction_parse_failed",
                    entity="fact", entity_id=question.fact_key,
                    source="onboarding",
                    properties={"raw_text": text[:200]},
                )
            if parsed_items:
                await event_log.append_event(
                    DB, user_id, "dietary_restriction_parse_started",
                    entity="fact", entity_id=question.fact_key,
                    source="onboarding",
                    properties={"items": parsed_items, "raw_text": text[:200]},
                )
                # Store structured restrictions provisionally
                parts = await _existing_list_value(user_id, "diet_restrictions")
                for item_name in parsed_items:
                    if item_name and item_name not in parts:
                        parts.append(item_name)
                new_val = ", ".join(dict.fromkeys(parts))
                await user_model.set_fact(
                    DB, user_id, "diet_restrictions", new_val,
                    kind=user_model.KIND_FACT,
                    source=user_model.SOURCE_USER,
                    confirmed=True,
                )
                # Ask for restriction type classification
                first_item = parsed_items[0]
                await clear_pending(user_id)
                await message.reply_text(
                    f"איך להתייחס ל{esc(first_item)}?",
                    reply_markup=_diet_type_keyboard(first_item),
                    parse_mode=ParseMode.HTML,
                )
                return True
            elif not text.strip():
                await message.reply_text("לא קיבלתי תשובה. אפשר לכתוב למשל: \"אני נמנע מקשיו\".")
                return True
            # No items parsed but text was provided — store as-is
            value = text

        if question.fact_key in {"weekly_availability", "workout_window", "session_minutes"}:
            from noam_coach.services.availability import parse_hebrew_availability_answer

            parsed_availability = parse_hebrew_availability_answer(text)
            if question.fact_key == "weekly_availability" and parsed_availability.weekly_availability:
                value = parsed_availability.weekly_availability
                if parsed_availability.training_days_per_week:
                    await user_model.set_fact(
                        DB,
                        user_id,
                        "training_days_per_week",
                        parsed_availability.training_days_per_week,
                        kind=user_model.KIND_FACT,
                        source=user_model.SOURCE_USER,
                        confirmed=True,
                    )
            if parsed_availability.workout_window:
                if question.fact_key == "workout_window":
                    value = parsed_availability.workout_window
                await user_model.set_fact(
                    DB,
                    user_id,
                    "workout_window",
                    parsed_availability.workout_window,
                    kind=user_model.KIND_FACT,
                    source=user_model.SOURCE_USER,
                    confirmed=True,
                )
            if parsed_availability.session_minutes:
                if question.fact_key == "session_minutes":
                    value = parsed_availability.session_minutes
                await user_model.set_fact(
                    DB,
                    user_id,
                    "session_minutes",
                    parsed_availability.session_minutes,
                    kind=user_model.KIND_FACT,
                    source=user_model.SOURCE_USER,
                    confirmed=True,
                )

        try:
            await questions.record_answer(DB, user_id, question, value)
        except Exception:  # noqa: BLE001
            LOGGER.exception("Failed to record answer for %s", question.id)
            await event_log.append_event(
                DB, user_id, "question_answer_failed",
                entity="question", entity_id=question.id,
                source="onboarding",
                properties={"raw_text": text[:200], "fact_key": question.fact_key},
            )
            # REC-PLAN-MEAL-03-02: Keep the question flow active, show recovery
            await message.reply_text(
                "לא הצלחתי לשמור את התשובה. אפשר לנסות שוב, לדלג, או לחזור.",
                reply_markup=InlineKeyboardMarkup([
                    [button("🔄 נסה שוב", f"qa:{question.id}:retry")],
                    [button("⏭️ דלג", f"qa:{question.id}:skip")],
                    [button("⬅️ תפריט", "menu:home")],
                ]),
            )
            return True
        await clear_pending(user_id)
        await advance_after_answer(message, user_id)
        return True

    return False


def _safe_cb(text: str) -> str:
    """Truncate text for callback data (Telegram 64-byte limit)."""
    return text[:15].strip()


def _parse_dietary_answer(text: str) -> list[str]:
    """Parse dietary restriction free-text answer into food item names.

    REC-PLAN-MEAL-03-01: Handles various Hebrew and English patterns:
    - "כן קשיו" -> ["קשיו"]
    - "קשיו" -> ["קשיו"]
    - "לא אוכל קשיו" -> ["קשיו"]
    - "cashew" -> ["cashew"]
    - "אגוזי קשיו" -> ["אגוזי קשיו"]
    - "גלוטן, חלב, ביצים" -> ["גלוטן", "חלב", "ביצים"]
    """
    if not text or not text.strip():
        return []
    t = text.strip()
    # Remove affirmative prefixes
    for prefix in ("כן ", "כן, ", "כן,", "יש ", "יש, "):
        if t.startswith(prefix):
            t = t[len(prefix):].strip()
            break
    # Remove negation prefixes (meaning "I don't eat X")
    for prefix in (
        "אני לא אוכל ", "אני נמנע מ", "אני נמנעת מ",
        "לא אוכל ", "לא אוכלת ", "לא מסתדר עם ",
        "לא מסתדרת עם ", "אני לא ", "לא ",
    ):
        if t.startswith(prefix):
            t = t[len(prefix):].strip()
            break
    if not t:
        return []
    # Split by commas, "ו" connector, or newlines
    import re as _re
    items = _re.split(r"[,\n]+|\s+ו\s+", t)
    result = []
    for item in items:
        cleaned = item.strip().strip(".,;:!?\"'")
        if cleaned and len(cleaned) >= 2:
            result.append(cleaned)
    # If no split occurred, return the whole cleaned text
    if not result and t.strip():
        result = [t.strip()]
    return result


@runtime_bound(RUNTIME_NAMES)
async def apply_basics_fix(user_id: int, text: str) -> bool:
    """Parse simple '<label> <number>' corrections into measured facts."""
    import re

    mapping = {
        "משקל": "weight_kg",
        "גובה": "height_cm",
        "שומן": "body_fat_pct",
    }
    applied = False
    for label, key in mapping.items():
        match = re.search(rf"{label}[^\d]*(\d+(?:\.\d+)?)", text)
        if match:
            await user_model.set_fact(
                DB,
                user_id,
                key,
                float(match.group(1)),
                kind=user_model.KIND_FACT,
                source=user_model.SOURCE_USER,
                confirmed=True,
            )
            applied = True
    return applied


@runtime_bound(RUNTIME_NAMES)
async def finish_onboarding(target: Any, user_id: int) -> None:
    await onboarding.set_stage(DB, user_id, onboarding.S_DONE)
    # Record deferred slots as gaps — they'll be asked just-in-time later.
    for key, why in questions.DEFERRED_GAP_KEYS.items():
        await user_model.record_gap(DB, user_id, key, why_matters=why)
    text = (
        "<b>מצוין, סיימנו את ההיכרות הראשונית 🎯</b>\n\n"
        "בניתי לך פרופיל התחלתי. השבוע הראשון הוא שבוע למידה — "
        "אלמד מהדיווחים שלך ואדייק את התוכנית בהתאם.\n\n"
        "מכאן אפשר פשוט <b>לכתוב לי מה בא לך</b> — "
        '"תפריט להיום", "בוא נתאמן", "לקחתי ריטלין", או לשלוח תמונת אוכל. '
        "אשאל פרטים נוספים רק כשהם באמת משנים את ההמלצה.\n\n"
        "אפשר לראות את הפרופיל בכל רגע עם /profile."
    )
    if hasattr(target, "edit_message_text"):
        await safe_edit(target, text, home_keyboard())
    else:
        await target.reply_text(text, reply_markup=home_keyboard(), parse_mode=ParseMode.HTML)


@runtime_bound(RUNTIME_NAMES)
async def resume_onboarding_after_restart(target: Any, user_id: int) -> bool:
    """Resume the onboarding flow after a restart (REC-ONBOARD-02-05).

    Loads the persisted stage and re-renders the current step.  Returns True
    if onboarding was resumed, False if the user is not in onboarding.
    """
    stage = await onboarding.get_stage(DB, user_id)
    if stage in (onboarding.S_NONE, onboarding.S_DONE):
        return False

    await event_log.append_event(
        DB, user_id, "onboarding_resumed_after_restart",
        entity="onboarding", source="system",
        properties={"stage": stage},
    )

    if stage == onboarding.S_CONFIRM_BASICS:
        await show_onboarding_basics(target, user_id)
        return True
    if stage == onboarding.S_CONFIRM_PATTERNS:
        await show_onboarding_patterns(target, user_id)
        return True
    if stage in (onboarding.S_SAFETY, onboarding.S_PLAN_QUESTIONS):
        if not await ask_next_question(target, user_id):
            await finish_onboarding(target, user_id)
        return True
    if stage in (onboarding.S_OPEN, onboarding.S_EXPORT_HELP):
        if hasattr(target, "reply_text"):
            await target.reply_text(
                onboarding.INTRO_TEXT,
                reply_markup=InlineKeyboardMarkup([
                    [button("📋 איך מייצאים?", "onb:export_help")],
                    [button("⏭️ נתחיל בלי", "onb:no_data")],
                ]),
                parse_mode=ParseMode.HTML,
            )
        return True
    # Fallback: ask next question or finish
    if not await ask_next_question(target, user_id):
        await finish_onboarding(target, user_id)
    return True


@runtime_bound(RUNTIME_NAMES)
async def reconcile_onboarding_stage(user_id: int) -> None:
    """Complete onboarding only when the profile is actually usable.

    A meal or a legacy plan is not evidence that onboarding is complete.  The
    readiness model is the single gate, which prevents a user from reaching an
    active plan while safety, schedule or equipment facts are still gaps.
    """
    stage = await onboarding.get_stage(DB, user_id)
    if stage in {onboarding.S_DONE, onboarding.S_NONE}:
        return
    readiness = await user_model.compute_all_readiness(DB, user_id)
    goal = await planning.active_goal(DB, user_id)
    workout = await planning.get_active_plan(DB, user_id, "workout")
    if (
        readiness["safety"]["ready"]
        and readiness["workout"]["ready"]
        and goal is not None
        and workout is not None
    ):
        await onboarding.set_stage(DB, user_id, onboarding.S_DONE)
        await event_log.append_event(
            DB,
            user_id,
            "ONBOARDING_RECONCILED",
            entity="onboarding",
            payload={"reason": "readiness_and_active_plan"},
            source="system",
        )
        LOGGER.info("Reconciled onboarding to DONE for user %s", user_id)
