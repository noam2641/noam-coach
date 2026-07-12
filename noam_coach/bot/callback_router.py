# ruff: noqa: F401, F811, F821, I001
"""Callback routing and callback-family handlers.

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
from noam_coach.services.telegram_errors import classify_telegram_error, redact_sensitive_text

RUNTIME_NAMES = ('APP_VERSION', 'Any', 'CALLBACK_DEBOUNCE_SECONDS', 'CONFIRM_PENDING', 'ContextTypes', 'DB', 'EXERCISE_MUSCLES', 'Exception', 'GOAL_STATUS_PROPOSED', 'GOAL_STATUS_PROVISIONAL', 'InlineKeyboardButton', 'InlineKeyboardMarkup', 'KeyError', 'LOGGER', 'MealAnalysis', 'PENDING_QUESTION', 'Path', 'RIR_UNKNOWN', 'SESSION_SCOPED_ACTIONS', 'SETTINGS', 'TypeError', 'Update', 'ValueError', 'WebAppInfo', '_DEBOUNCE_PREFIXES', '_LAST_CALLBACK', '_StaleSetStep', '_duration_s', '_home_hint', '_is_duplicate_tap', '_plan_type_label', '_started', 'action', 'activate_goal_version_provisional', 'active_flow', 'active_session', 'actual_reps', 'actual_rir', 'actual_weight', 'aiosqlite', 'alt', 'alt_muscle', 'alternative', 'alternative_index', 'analysis', 'analyze_duplicate_candidate', 'apply_reconcile_proposal', 'approval_id', 'bool', 'build_daily_status', 'build_evening_summary_text', 'build_health_status_text', 'build_morning_menu_text', 'build_next_meal_text', 'build_now_action_text', 'build_weekly_summary_text', 'button', 'buttons', 'callback_flow_id', 'callback_version', 'cancel_rest_timer', 'candidate', 'center', 'changed', 'check_duplicate_meal', 'choices', 'chosen_reps', 'chosen_weight', 'claimed', 'clear_confirm_pending', 'clear_meal_fix', 'clear_pending', 'clear_split_state', 'code', 'command_profile_query', 'completed', 'conn', 'connection', 'constraint_id', 'context', 'conversation', 'create_approval', 'create_goal_version', 'create_meal_edit_approval', 'cur', 'current', 'cursor', 'cutoff', 'data', 'datetime', 'decide_approval', 'deleted', 'delta', 'delta_text', 'dict', 'done', 'draft', 'dup', 'duplicate_approval_id', 'edit_approval_id', 'ensure_user', 'enumerate', 'error_id', 'esc', 'event_log', 'ex', 'exc', 'exercise_index', 'exercise_index_text', 'exercise_picker_keyboard', 'existing', 'extra', 'extra_seconds', 'fetch_approval', 'fetch_goal', 'field', 'final_rir', 'first_reps', 'first_weight', 'flags', 'float', 'frequency', 'friendly_error', 'get_daily_flags', 'get_meal_fix', 'get_split_state', 'get_user_plan', 'getattr', 'goal', 'goal_id', 'gv_id', 'handle_checkin_callback', 'handle_flags_callback', 'handle_goal_callback', 'handle_meal_callback', 'handle_menu_callback', 'handle_onboarding_callback', 'handle_plan_callback', 'handle_session_action_callback', 'handle_workout_setup_callback', 'home_keyboard', 'index', 'int', 'is_allowed', 'is_current_session_step', 'is_partial', 'is_provisional', 'isinstance', 'item', 'item_index', 'item_index_text', 'job', 'jobs', 'json', 'k', 'kb', 'key', 'kind', 'label', 'labels', 'last', 'len', 'level', 'list', 'logged_sets', 'max', 'meal', 'meal_id', 'meal_id_text', 'min', 'mini_app_url', 'missing', 'missing_labels', 'more_keyboard', 'msg', 'new_grams', 'new_max', 'new_min', 'new_val', 'new_weight', 'note', 'notify_admin', 'now', 'nutrition', 'object', 'ok', 'old_grams', 'option', 'option_index', 'pain_location', 'part', 'parts', 'parts_v2', 'payload', 'persist_meal', 'plan', 'plan_id', 'plan_now', 'plan_type', 'planned_sets', 'planning', 'plans_keyboard', 'progress', 'quality', 'query', 'range', 'ratio', 'rc', 'readiness', 'recommend_load', 'refreshed', 'render_candidate_list', 'render_exercise_params', 'render_meal', 'render_profile_snapshot', 'render_quantity_editor', 'render_smart_plan_hub', 'render_unified_plan', 'render_workout_overview', 'reopened', 'replacement', 'reps', 'reps_value', 'rest', 'rest_job_name', 'result', 'round', 'route_decision', 'row', 'rows', 'safe_edit', 'save_medical_constraint', 'save_split_set', 'second_base', 'second_reps', 'second_weight', 'secrets', 'select_todays_workout_code', 'selected', 'send_weight_chart', 'session', 'session_action_arg', 'session_action_data', 'session_id', 'set', 'set_daily_flags', 'set_exercise_override', 'set_goal_weight', 'set_meal_fix', 'set_pending', 'set_split_state', 'severity', 'show_session', 'split_reps_keyboard', 'split_rir_keyboard', 'split_state', 'split_summary_line', 'split_weight_keyboard', 'start_rest_timer', 'status', 'status_line', 'step', 'str', 'sum', 'summary_line', 'suppress', 't', 'tail', 'target_change_note', 'text', 'time', 'total_reps', 'track_event', 'training_intelligence', 'try_save_set', 'tuple', 'undo_last_set', 'undone', 'update', 'update_rest_message', 'update_session_step', 'url', 'user_choice', 'user_id', 'user_model', 'utc_now', 'value', 'value_text', 'warn', 'weight', 'workout', 'workout_summary', 'write_audit')

_LAST_CALLBACK: dict[tuple[int, str], float] = {}

if "safe_answer_callback" not in RUNTIME_NAMES:
    RUNTIME_NAMES = (*RUNTIME_NAMES, "safe_answer_callback")

CALLBACK_DEBOUNCE_SECONDS = 1.2

_DEBOUNCE_PREFIXES = (
    "qtydelta:",
    "restadd:",
    "confirm:",
    "approve_meal:",
    "approve_goal:",
    "undo_meal:",
    "chk:med",
    "param:",
    "wparamtext:",
    "sub:",
    "wpause:",
    "wdone:",
    "wcancel:",
    "nextmeal:save:",
    "nextmeal:plan:",
    "nextmeal:choose:",
    "dailymenu:save:",
    # Codex audit round: sensitive single-shot writes that advance a flow —
    # a double-tap must not apply/skip TWO steps (e.g. health:skip_item
    # tapped twice would silently discard the next wizard item too).
    "health:confirm",
    "health:activate",
    "health:skip",
    "reconcile_ok:",
    "plan:set:",
    "qa:",
)

@runtime_bound(RUNTIME_NAMES)
def _is_duplicate_tap(user_id: int, data: str) -> bool:
    if not data.startswith(_DEBOUNCE_PREFIXES):
        return False  # legitimate repeats (set logging, navigation) pass through
    now = time.monotonic()
    key = (user_id, data)
    last = _LAST_CALLBACK.get(key)
    _LAST_CALLBACK[key] = now
    # Opportunistic cleanup of old entries.
    if len(_LAST_CALLBACK) > 500:
        cutoff = now - 60
        for k, t in list(_LAST_CALLBACK.items()):
            if t < cutoff:
                _LAST_CALLBACK.pop(k, None)
    return last is not None and (now - last) < CALLBACK_DEBOUNCE_SECONDS

@runtime_bound(RUNTIME_NAMES)
async def handle_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not await is_allowed(update):
        return

    query = update.callback_query
    await safe_answer_callback(query)
    user_id = await ensure_user(update)
    data = query.data or ""
    route_decision = await conversation.ConversationRouter.route(
        DB,
        user_id,
        "callback",
        command=("home" if data == "menu:home" else ""),
    )
    active_flow = route_decision.flow
    callback_version = conversation.extract_version(data)
    callback_flow_id = conversation.extract_flow_id(data)
    if (callback_version is not None or callback_flow_id is not None) and not conversation.check_version(
        active_flow, callback_version, callback_flow_id
    ):
        # REC-ONBOARD-02-06: Better stale-callback recovery
        await safe_answer_callback(query, "הכפתורים בהודעה הישנה כבר לא פעילים", show_alert=False)
        await event_log.append_event(
            DB, user_id, "stale_callback_recovered",
            entity="callback", source="user",
            properties={
                "callback_data": data[:100],
                "old_version": callback_version,
                "current_version": active_flow.version,
                "active_flow": active_flow.name.value if hasattr(active_flow.name, "value") else str(active_flow.name),
            },
        )
        # Try to remove old keyboard
        with suppress(Exception):
            await query.edit_message_reply_markup(reply_markup=None)
        # Route to the current active flow instead of generic home
        from noam_coach.bot.onboarding import resume_onboarding_after_restart
        if active_flow.name != conversation.FlowName.idle:
            # Re-render current active flow
            if active_flow.is_meal:
                approval_id = active_flow.step
                if approval_id:
                    await render_meal(query, user_id, approval_id)
                    return
            if active_flow.is_question or active_flow.name in (
                conversation.FlowName.routine_confirm,
            ):
                if await resume_onboarding_after_restart(query, user_id):
                    return
        # Check if user is in onboarding
        import onboarding
        if await onboarding.is_onboarding(DB, user_id):
            if await resume_onboarding_after_restart(query, user_id):
                return
        # Fallback: show home
        await safe_edit(
            query,
            "הכפתורים בהודעה הישנה כבר לא פעילים.\nפתחתי עבורך את השלב העדכני.",
            home_keyboard(),
        )
        return
    await track_event(user_id, "user_callback", data=data)

    # Ignore a rapid repeat of the exact same button (prevents double actions
    # like subtracting grams twice). Navigation taps are naturally distinct.
    if _is_duplicate_tap(user_id, data):
        return

    if data.startswith("onb:") or data.startswith("qa:") or data.startswith("routine:"):
        await handle_onboarding_callback(query, user_id, data)
        return

    if data.startswith("chk:"):
        await handle_checkin_callback(query, user_id, data)
        return

    if await handle_menu_callback(query, user_id, data):
        return

    if await handle_flags_callback(query, user_id, data):
        return

    if await handle_plan_callback(query, user_id, data):
        return

    if await handle_workout_setup_callback(query, context, user_id, data):
        return

    if await handle_goal_callback(query, user_id, data):
        return

    if await handle_meal_callback(query, user_id, data):
        return

    # Anything left is a session-scoped action (parts-encoded). Delegate to
    # the session-action sub-handler so handle_callback stays a thin router.
    await handle_session_action_callback(query, context, user_id, data)

@runtime_bound(RUNTIME_NAMES)
async def on_error(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    error_id = secrets.token_hex(3)
    exc = context.error
    decision = classify_telegram_error(
        exc,
        shutting_down=bool(getattr(RUNTIME_STATE, "shutting_down", False)),
        update=update,
    )
    safe_error = redact_sensitive_text(repr(exc))
    if decision.log_level == "debug":
        LOGGER.debug("[%s] Telegram transient error during shutdown: %s", error_id, safe_error)
    elif decision.log_level == "warning":
        LOGGER.warning("[%s] Telegram transient error: %s", error_id, safe_error)
    else:
        LOGGER.error("[%s] Telegram error: %s", error_id, safe_error)
    if decision.notify_admin:
        # PATCH-12 / IMG_002+IMG_008: the bot owner may be the same Telegram chat
        # as the end user.  Never send raw Telegram exception text into the chat
        # (e.g. "Query is too old..." or getaddrinfo stack traces).  Detailed
        # information stays in the application logs with the same error_id.
        await notify_admin(
            context.bot,
            f"[{error_id}] זוהתה תקלה פנימית בבוט. הפרטים המלאים נשמרו בלוגים; בצ׳אט לא מוצג traceback.",
        )
    if isinstance(update, Update) and update.effective_message:
        data = ""
        if update.callback_query:
            data = update.callback_query.data or ""
        user_id = update.effective_user.id if update.effective_user else 0
        if user_id and not decision.transient:
            with suppress(Exception):
                await event_log.append_event(
                    DB, user_id, "callback_error",
                    entity="callback", entity_id=redact_sensitive_text(data[:50]),
                    source="system",
                    properties={
                        "error": redact_sensitive_text(str(exc), max_length=200),
                        "callback_data": redact_sensitive_text(data[:100]),
                        "error_id": error_id,
                        "fingerprint": decision.fingerprint,
                    },
                )
        if decision.notify_user:
            with suppress(Exception):
                await update.effective_message.reply_text(
                    "המסך הזה כבר לא עדכני. רענן אותו ואמשיך מאותה נקודה.",
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [InlineKeyboardButton("🔄 רענן מסך", callback_data="menu:status")],
                            [InlineKeyboardButton("🏠 חזור לתפריט", callback_data="menu:home")],
                        ]
                    ),
                )
