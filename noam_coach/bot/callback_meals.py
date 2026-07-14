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
from noam_coach.bot.ui import safe_answer_callback

RUNTIME_NAMES = ('render_post_meal_confirmation_day_status', 'APP_VERSION', 'Any', 'CALLBACK_DEBOUNCE_SECONDS', 'CONFIRM_PENDING', 'ContextTypes', 'DB', 'EXERCISE_MUSCLES', 'Exception', 'GOAL_STATUS_PROPOSED', 'GOAL_STATUS_PROVISIONAL', 'InlineKeyboardButton', 'InlineKeyboardMarkup', 'KeyError', 'LOGGER', 'MealAnalysis', 'PENDING_QUESTION', 'Path', 'RIR_UNKNOWN', 'SESSION_SCOPED_ACTIONS', 'SETTINGS', 'TypeError', 'Update', 'ValueError', 'WebAppInfo', '_DEBOUNCE_PREFIXES', '_LAST_CALLBACK', '_StaleSetStep', '_duration_s', '_home_hint', '_is_duplicate_tap', '_plan_type_label', '_started', 'action', 'activate_goal_version_provisional', 'active_flow', 'active_session', 'actual_reps', 'actual_rir', 'actual_weight', 'aiosqlite', 'alt', 'alt_muscle', 'alternative', 'alternative_index', 'analysis', 'analyze_duplicate_candidate', 'apply_reconcile_proposal', 'approval_id', 'bool', 'build_daily_status', 'build_evening_summary_text', 'build_health_status_text', 'build_morning_menu_text', 'build_next_meal_text', 'build_now_action_text', 'build_weekly_summary_text', 'button', 'buttons', 'callback_flow_id', 'callback_version', 'cancel_rest_timer', 'candidate', 'center', 'changed', 'check_duplicate_meal', 'choices', 'chosen_reps', 'chosen_weight', 'claimed', 'clear_confirm_pending', 'clear_meal_fix', 'clear_pending', 'clear_split_state', 'code', 'command_profile_query', 'completed', 'conn', 'connection', 'constraint_id', 'context', 'conversation', 'create_approval', 'create_goal_version', 'create_meal_edit_approval', 'cur', 'current', 'cursor', 'cutoff', 'data', 'datetime', 'decide_approval', 'deleted', 'delta', 'delta_text', 'dict', 'done', 'draft', 'dup', 'duplicate_approval_id', 'edit_approval_id', 'ensure_user', 'enumerate', 'error_id', 'esc', 'event_log', 'ex', 'exc', 'exercise_index', 'exercise_index_text', 'exercise_picker_keyboard', 'existing', 'extra', 'extra_seconds', 'fetch_approval', 'fetch_goal', 'field', 'final_rir', 'first_reps', 'first_weight', 'flags', 'float', 'frequency', 'friendly_error', 'get_daily_flags', 'get_meal_fix', 'get_split_state', 'get_user_plan', 'getattr', 'goal', 'goal_id', 'gv_id', 'handle_checkin_callback', 'handle_flags_callback', 'handle_goal_callback', 'handle_meal_callback', 'handle_menu_callback', 'handle_onboarding_callback', 'handle_plan_callback', 'handle_session_action_callback', 'handle_workout_setup_callback', 'home_keyboard', 'index', 'int', 'is_allowed', 'is_current_session_step', 'is_partial', 'is_provisional', 'isinstance', 'item', 'item_index', 'item_index_text', 'job', 'jobs', 'json', 'k', 'kb', 'key', 'kind', 'label', 'labels', 'last', 'len', 'level', 'list', 'logged_sets', 'max', 'meal', 'meal_id', 'meal_id_text', 'min', 'mini_app_url', 'missing', 'missing_labels', 'more_keyboard', 'msg', 'new_grams', 'new_max', 'new_min', 'new_val', 'new_weight', 'note', 'notify_admin', 'now', 'nutrition', 'object', 'ok', 'old_grams', 'option', 'option_index', 'pain_location', 'part', 'parts', 'parts_v2', 'payload', 'persist_meal', 'plan', 'plan_id', 'plan_now', 'plan_type', 'planned_sets', 'planning', 'plans_keyboard', 'progress', 'quality', 'query', 'range', 'ratio', 'rc', 'readiness', 'recommend_load', 'refreshed', 'render_candidate_list', 'render_exercise_params', 'render_meal', 'render_profile_snapshot', 'render_quantity_editor', 'render_smart_plan_hub', 'render_unified_plan', 'render_workout_overview', 'reopened', 'replacement', 'reps', 'reps_value', 'rest', 'rest_job_name', 'result', 'round', 'route_decision', 'row', 'rows', 'safe_edit', 'save_medical_constraint', 'save_split_set', 'second_base', 'second_reps', 'second_weight', 'secrets', 'select_todays_workout_code', 'selected', 'send_weight_chart', 'session', 'session_action_arg', 'session_action_data', 'session_id', 'set', 'set_daily_flags', 'set_exercise_override', 'set_goal_weight', 'set_meal_fix', 'set_pending', 'set_split_state', 'severity', 'show_session', 'split_reps_keyboard', 'split_rir_keyboard', 'split_state', 'split_summary_line', 'split_weight_keyboard', 'start_rest_timer', 'status', 'status_line', 'step', 'str', 'sum', 'summary_line', 'suppress', 't', 'tail', 'target_change_note', 'text', 'time', 'total_reps', 'track_event', 'training_intelligence', 'try_save_set', 'tuple', 'undo_last_set', 'undone', 'update', 'update_rest_message', 'update_session_step', 'url', 'user_choice', 'user_id', 'user_model', 'utc_now', 'value', 'value_text', 'warn', 'weight', 'workout', 'workout_summary', 'write_audit')

@runtime_bound(RUNTIME_NAMES)
async def _handle_meal_clarification_actions(
    query: Any,
    user_id: int,
    data: str,
) -> bool:
    if data.startswith("clarify:"):
        parts_clarify = data.split(":", maxsplit=2)
        if len(parts_clarify) != 3:
            return True
        _, approval_id, option_index_str = parts_clarify
        try:
            option_index = int(option_index_str)
        except ValueError:
            return True
        row = await fetch_approval(user_id, approval_id)
        if not row:
            return True

        analysis = MealAnalysis.model_validate(row["data"]["analysis"])
        option = analysis.options[option_index]
        item = None
        if option.item_index is not None and 0 <= option.item_index < len(analysis.items):
            item = analysis.items[option.item_index]
        elif option.item_name:
            item = max(
                analysis.items,
                key=lambda candidate: len(
                    set(candidate.name.casefold().split()) & set(option.item_name.casefold().split())
                ),
                default=None,
            )
        elif len(analysis.items) == 1:
            item = analysis.items[0]
        if item is None:
            await safe_answer_callback(query, "לא ניתן לקשר את ההבהרה לפריט הנכון", show_alert=True)
            return True
        item.calories = max(0, item.calories + option.calories_delta)
        item.protein = max(0, item.protein + option.protein_delta)
        item.carbs = max(0, item.carbs + option.carbs_delta)
        item.fat = max(0, item.fat + option.fat_delta)
        analysis.question = None
        analysis.options = []
        row["data"]["analysis"] = analysis.model_dump()

        await DB.execute(
            "UPDATE approvals SET payload=? WHERE id=?",
            (json.dumps(row["data"], ensure_ascii=False), approval_id),
        )
        _, rc = await get_meal_fix(user_id)
        await render_meal(query, user_id, approval_id, refine_count=rc)
        return True

    if data.startswith("fixmeal:"):
        approval_id = data.split(":", 1)[1]
        _, rc = await get_meal_fix(user_id)
        await set_meal_fix(user_id, approval_id, rc)
        await safe_edit(
            query,
            (
                "<b>תיקון ארוחה במלל</b>\n\n"
                "כתוב רק מה אכלת. אין צורך לכתוב כמויות.\n"
                "אחר כך אבצע ניתוח נוסף של התמונה יחד עם הטקסט שלך, "
                "אחשב מחדש את הכמויות לפי התמונה, "
                "ואחזיר פירוט לכל פריט לאישור."
            ),
            InlineKeyboardMarkup([[button("❌ ביטול", f"cancelfix:{approval_id}")]]),
        )
        return True

    if data.startswith("editqtymenu:"):
        approval_id = data.split(":", 1)[1]
        row = await fetch_approval(user_id, approval_id)
        if not row:
            return True
        analysis = MealAnalysis.model_validate(row["data"]["analysis"])
        rows = [
            [button(f"{index + 1}. {item.name}", f"editqty:{approval_id}:{index}")]
            for index, item in enumerate(analysis.items)
        ]
        rows.append([button("⬅️ חזרה לארוחה", f"backmeal:{approval_id}")])
        await safe_edit(query, "<b>בחר פריט לעריכת כמות</b>", InlineKeyboardMarkup(rows))
        return True

    if data.startswith("editqty:"):
        _, approval_id, item_index = data.split(":")
        await render_quantity_editor(query, user_id, approval_id, int(item_index))
        return True

    if data.startswith("qtydelta:"):
        parts_qty = data.split(":", maxsplit=3)
        if len(parts_qty) != 4:
            return True
        _, approval_id, item_index_text, delta_text = parts_qty
        try:
            item_index = int(item_index_text)
            delta = float(delta_text)
        except ValueError:
            return True
        row = await fetch_approval(user_id, approval_id)
        if not row:
            return True
        analysis = MealAnalysis.model_validate(row["data"]["analysis"])
        item = analysis.items[item_index]
        old_grams = max(1.0, item.grams)
        new_grams = max(1.0, item.grams + delta)
        ratio = new_grams / old_grams
        item.grams = round(new_grams, 1)
        item.calories = round(item.calories * ratio, 1)
        item.protein = round(item.protein * ratio, 1)
        item.carbs = round(item.carbs * ratio, 1)
        item.fat = round(item.fat * ratio, 1)
        row["data"]["analysis"] = analysis.model_dump()
        await DB.execute(
            "UPDATE approvals SET payload=? WHERE id=?",
            (json.dumps(row["data"], ensure_ascii=False), approval_id),
        )
        await render_quantity_editor(query, user_id, approval_id, item_index)
        return True
    return False


@runtime_bound(RUNTIME_NAMES)
async def _handle_meal_duplicate_actions(
    query: Any,
    user_id: int,
    data: str,
) -> bool:
    if data.startswith("dup:new:"):
        duplicate_approval_id = data.split(":", 2)[2]
        await safe_edit(query, "מנתח את התמונה כארוחה נוספת… ⏳", None)
        try:
            await analyze_duplicate_candidate(query, user_id, duplicate_approval_id)
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("Duplicate override analysis failed")
            await safe_edit(
                query,
                friendly_error(exc, "duplicate meal analysis"),
                home_keyboard(),
            )
        return True

    if data.startswith("reject_dup:"):
        duplicate_approval_id = data.split(":", 1)[1]
        row = await fetch_approval(user_id, duplicate_approval_id)
        if row and row["data"].get("image"):
            Path(row["data"]["image"]).unlink(missing_ok=True)
        await decide_approval(duplicate_approval_id, "rejected")
        await safe_edit(query, "התמונה לא נשמרה.", home_keyboard())
        return True
    return False


@runtime_bound(RUNTIME_NAMES)
async def _handle_meal_edit_actions(
    query: Any,
    user_id: int,
    data: str,
) -> bool:
    if data.startswith("editmeal:"):
        meal_id_text = data.split(":", 1)[1]
        if not meal_id_text.isdigit():
            return True
        approval_id = await create_meal_edit_approval(user_id, int(meal_id_text))
        if not approval_id:
            await safe_edit(query, "הארוחה כבר אינה זמינה לעריכה.", home_keyboard())
            return True
        await render_meal(query, user_id, approval_id)
        await set_meal_fix(user_id, approval_id, 0)
        return True

    if data.startswith("backmeal:"):
        approval_id = data.split(":", 1)[1]
        await render_meal(query, user_id, approval_id)
        return True

    if data.startswith("cancelfix:"):
        approval_id = data.split(":", 1)[1]
        await clear_meal_fix(user_id)
        await render_meal(query, user_id, approval_id)
        return True
    return False


@runtime_bound(RUNTIME_NAMES)
async def _handle_meal_decision_actions(
    query: Any,
    user_id: int,
    data: str,
) -> bool:
    if data.startswith("force_approve_meal:"):
        approval_id = data.split(":", 1)[1]
        # Fetch approval details before persisting so we can show a rich confirmation.
        force_row = await fetch_approval(user_id, approval_id)
        force_analysis = (
            MealAnalysis.model_validate(force_row["data"]["analysis"])
            if force_row
            else None
        )
        meal_id = await persist_meal(user_id, approval_id)
        if meal_id is None:
            return True
        await clear_meal_fix(user_id)
        totals = force_analysis.totals() if force_analysis else {"calories": 0.0, "protein": 0.0}
        confirmation = await render_post_meal_confirmation_day_status(user_id, totals)
        await safe_edit(
            query,
            confirmation,
            InlineKeyboardMarkup([
                [button("🍽️ מה לאכול עכשיו", "menu:nextmeal"), button("🏋️ סמן אימון", "menu:workout")],
                [button("✏️ ערוך ארוחה", f"editmeal:{meal_id}"), button("📊 מצב היום", "menu:status")],
            ]),
        )
        return True

    if data.startswith("approve_meal:"):
        approval_id = data.split(":", 1)[1]
        # Fetch approval details for duplicate check and confirmation message.
        row = await fetch_approval(user_id, approval_id)
        analysis = (
            MealAnalysis.model_validate(row["data"]["analysis"]) if row else None
        )
        if analysis:
            dup = await check_duplicate_meal(user_id, analysis)
            if dup:
                await safe_edit(
                    query,
                    f"⚠️ כבר שמרתי ארוחה דומה ({esc(dup['name'])}) לפני כמה דקות.\n"
                    "לשמור בכל זאת?",
                    InlineKeyboardMarkup([
                        [button("✅ כן, שמור", f"force_approve_meal:{approval_id}")],
                        [button("❌ לא, בטל", f"reject_meal:{approval_id}")],
                    ]),
                )
                return True
        meal_id = await persist_meal(user_id, approval_id)
        if meal_id is None:
            return True
        # Refinement loop ends on approval.
        await clear_meal_fix(user_id)
        totals = analysis.totals() if analysis else {"calories": 0.0, "protein": 0.0}
        confirmation = await render_post_meal_confirmation_day_status(user_id, totals)
        await safe_edit(
            query,
            confirmation,
            InlineKeyboardMarkup([
                [button("🍽️ מה לאכול עכשיו", "menu:nextmeal"), button("🏋️ סמן אימון", "menu:workout")],
                [button("✏️ ערוך ארוחה", f"editmeal:{meal_id}"), button("📊 מצב היום", "menu:status")],
            ]),
        )
        return True

    if data.startswith("undo_meal:"):
        meal_id_text = data.split(":", 1)[1]
        if not meal_id_text.isdigit():
            await safe_answer_callback(query, "הארוחה כבר אינה זמינה לביטול", show_alert=False)
            return True
        meal_id = int(meal_id_text)
        meal = await DB.fetch_one(
            "SELECT * FROM meals WHERE id=? AND user_id=?",
            (meal_id, user_id),
        )
        if not meal:
            await safe_edit(query, "הארוחה כבר בוטלה או אינה קיימת.", home_keyboard())
            return True
        approval_id = meal.get("approval_id")
        # Preserve an editable draft before deleting the durable meal.
        edit_approval_id = await create_meal_edit_approval(user_id, meal_id)
        if edit_approval_id:
            draft = await fetch_approval(user_id, edit_approval_id)
            if draft:
                draft["data"].pop("edit_meal_id", None)
                await DB.execute(
                    "UPDATE approvals SET kind='meal', payload=? WHERE id=?",
                    (json.dumps(draft["data"], ensure_ascii=False), edit_approval_id),
                )
        deleted = await DB.execute_rowcount(
            "DELETE FROM meals WHERE id=? AND user_id=?",
            (meal_id, user_id),
        )
        if not deleted:
            await safe_edit(query, "הארוחה כבר בוטלה או אינה קיימת.", home_keyboard())
            return True
        if approval_id:
            # Keep the historical approval decided; the new edit approval is the draft.
            pass
        await write_audit(user_id, "undo", "meal", meal_id)
        await event_log.append_event(
            DB,
            user_id,
            "MEAL_UNDONE",
            entity="meal",
            entity_id=meal_id,
            source="user",
            properties={"draft_approval_id": edit_approval_id},
        )
        with suppress(Exception):
            # FIX 43: undo is a domain event just like create/edit -- the
            # menu/recommendation built while this meal still counted toward
            # totals is no longer valid.
            from noam_coach.services.day_state_invalidation import invalidate_day_projections

            await invalidate_day_projections(DB, user_id, reason="meal_undone")
        rows = [[button("⬅️ תפריט", "menu:home")]]
        if edit_approval_id:
            rows.insert(0, [button("✏️ תקן ושמור מחדש", f"backmeal:{edit_approval_id}")])
        await safe_edit(
            query,
            "השמירה בוטלה. שמרתי טיוטה לעריכה כדי שלא תצטרך להתחיל מחדש ↩️",
            InlineKeyboardMarkup(rows),
        )
        return True

    if data.startswith("reject_meal:"):
        approval_id = data.split(":", 1)[1]
        row = await fetch_approval(user_id, approval_id)
        if row and row["data"].get("image"):
            Path(row["data"]["image"]).unlink(missing_ok=True)
        await decide_approval(approval_id, "rejected")
        await clear_meal_fix(user_id)
        await safe_edit(query, "הארוחה נדחתה ולא נשמרה.", home_keyboard())
        return True
    return False


@runtime_bound(RUNTIME_NAMES)
async def handle_meal_callback(
    query: Any,
    user_id: int,
    data: str,
) -> bool:
    """Route meal callbacks to focused clarification, duplicate, edit and decision handlers."""
    handlers = (
        _handle_meal_clarification_actions,
        _handle_meal_duplicate_actions,
        _handle_meal_edit_actions,
        _handle_meal_decision_actions,
    )
    for handler in handlers:
        if await handler(query, user_id, data):
            return True
    return False
