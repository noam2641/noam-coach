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

RUNTIME_NAMES = ('APP_VERSION', 'Any', 'CALLBACK_DEBOUNCE_SECONDS', 'CONFIRM_PENDING', 'ContextTypes', 'DB', 'EXERCISE_MUSCLES', 'Exception', 'GOAL_STATUS_PROPOSED', 'GOAL_STATUS_PROVISIONAL', 'InlineKeyboardButton', 'InlineKeyboardMarkup', 'KeyError', 'LOGGER', 'MealAnalysis', 'PENDING_QUESTION', 'Path', 'RIR_UNKNOWN', 'SESSION_SCOPED_ACTIONS', 'SETTINGS', 'TypeError', 'Update', 'ValueError', 'WebAppInfo', '_DEBOUNCE_PREFIXES', '_LAST_CALLBACK', '_StaleSetStep', '_duration_s', '_home_hint', '_is_duplicate_tap', '_plan_type_label', '_started', 'action', 'activate_goal_version_provisional', 'active_flow', 'active_session', 'actual_reps', 'actual_rir', 'actual_weight', 'aiosqlite', 'alt', 'alt_muscle', 'alternative', 'alternative_index', 'analysis', 'analyze_duplicate_candidate', 'apply_reconcile_proposal', 'approval_id', 'bool', 'build_daily_status', 'build_evening_summary_text', 'build_health_status_text', 'build_morning_menu_text', 'build_next_meal_text', 'build_now_action_text', 'build_weekly_summary_text', 'button', 'buttons', 'callback_flow_id', 'callback_version', 'cancel_rest_timer', 'candidate', 'center', 'changed', 'check_duplicate_meal', 'choices', 'chosen_reps', 'chosen_weight', 'claimed', 'clear_confirm_pending', 'clear_meal_fix', 'clear_pending', 'clear_split_state', 'code', 'command_profile_query', 'completed', 'conn', 'connection', 'constraint_id', 'context', 'conversation', 'create_approval', 'create_goal_version', 'create_meal_edit_approval', 'cur', 'current', 'cursor', 'cutoff', 'data', 'datetime', 'decide_approval', 'deleted', 'delta', 'delta_text', 'dict', 'done', 'draft', 'dup', 'duplicate_approval_id', 'edit_approval_id', 'ensure_user', 'enumerate', 'error_id', 'esc', 'event_log', 'ex', 'exc', 'exercise_index', 'exercise_index_text', 'exercise_picker_keyboard', 'existing', 'extra', 'extra_seconds', 'fetch_approval', 'fetch_goal', 'field', 'final_rir', 'first_reps', 'first_weight', 'flags', 'float', 'frequency', 'friendly_error', 'get_daily_flags', 'get_meal_fix', 'get_split_state', 'get_user_plan', 'getattr', 'goal', 'goal_id', 'gv_id', 'handle_checkin_callback', 'handle_flags_callback', 'handle_goal_callback', 'handle_meal_callback', 'handle_menu_callback', 'handle_onboarding_callback', 'handle_plan_callback', 'handle_session_action_callback', 'handle_workout_setup_callback', 'home_keyboard', 'index', 'int', 'is_allowed', 'is_current_session_step', 'is_partial', 'is_provisional', 'isinstance', 'item', 'item_index', 'item_index_text', 'job', 'jobs', 'json', 'k', 'kb', 'key', 'kind', 'label', 'labels', 'last', 'len', 'level', 'list', 'logged_sets', 'max', 'meal', 'meal_id', 'meal_id_text', 'min', 'mini_app_url', 'missing', 'missing_labels', 'more_keyboard', 'msg', 'new_grams', 'new_max', 'new_min', 'new_val', 'new_weight', 'note', 'notify_admin', 'now', 'nutrition', 'object', 'ok', 'old_grams', 'option', 'option_index', 'pain_location', 'part', 'parts', 'parts_v2', 'payload', 'persist_meal', 'plan', 'plan_id', 'plan_now', 'plan_type', 'planned_sets', 'planning', 'plans_keyboard', 'progress', 'quality', 'query', 'range', 'ratio', 'rc', 'readiness', 'recommend_load', 'refreshed', 'render_candidate_list', 'render_exercise_params', 'render_meal', 'render_profile_snapshot', 'render_quantity_editor', 'render_smart_plan_hub', 'render_unified_plan', 'render_workout_overview', 'reopened', 'replacement', 'reps', 'reps_value', 'rest', 'rest_job_name', 'result', 'round', 'route_decision', 'row', 'rows', 'safe_edit', 'save_medical_constraint', 'save_split_set', 'second_base', 'second_reps', 'second_weight', 'secrets', 'select_todays_workout_code', 'selected', 'send_weight_chart', 'session', 'session_action_arg', 'session_action_data', 'session_id', 'set', 'set_daily_flags', 'set_exercise_override', 'set_goal_weight', 'set_meal_fix', 'set_pending', 'set_split_state', 'severity', 'show_session', 'split_reps_keyboard', 'split_rir_keyboard', 'split_state', 'split_summary_line', 'split_weight_keyboard', 'start_rest_timer', 'status', 'status_line', 'step', 'str', 'sum', 'summary_line', 'suppress', 't', 'tail', 'target_change_note', 'text', 'time', 'total_reps', 'track_event', 'training_intelligence', 'try_save_set', 'tuple', 'undo_last_set', 'undone', 'update', 'update_rest_message', 'update_session_step', 'url', 'user_choice', 'user_id', 'user_model', 'utc_now', 'value', 'value_text', 'warn', 'weight', 'workout', 'workout_summary', 'write_audit')

@runtime_bound(RUNTIME_NAMES)
async def _handle_session_core_actions(
    query: Any,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    action: str,
    parts: list[str],
    session_id: int,
    session: dict[str, Any],
    plan: dict[str, Any],
    current: dict[str, Any],
    weight: float,
    reps: int,
) -> bool:
    if action == "setok":
        # One-tap save: the user did NOT report RIR, so store it as unknown
        # rather than fabricating a value (P0).
        result = await try_save_set(
            session,
            weight,
            reps,
            RIR_UNKNOWN,
            "telegram_one_tap",
        )
        if result is None:
            # Duplicate/stale tap — the set was already logged. Just refresh.
            await show_session(query, user_id, session_id)
            return True
        completed, rest = result
        if completed:
            await safe_edit(
                query,
                await workout_summary(user_id, session_id),
                home_keyboard(),
            )
            return True
        await start_rest_timer(
            context=context,
            query=query,
            user_id=user_id,
            session_id=session_id,
            weight=weight,
            reps=reps,
            rir=RIR_UNKNOWN,
            rest_seconds=rest,
        )
        return True

    if action == "restadd":
        if context.job_queue is None:
            return True
        jobs = context.job_queue.get_jobs_by_name(rest_job_name(user_id, session_id))
        if not jobs:
            await show_session(query, user_id, session_id)
            return True
        job = jobs[0]
        if not isinstance(job.data, dict):
            return True
        extra_seconds = int(session_action_arg(parts))
        job.data["ends_at"] += extra_seconds
        job.data["total_seconds"] += extra_seconds
        job.data["last_remaining"] = None
        await update_rest_message(context, job.data)
        return True

    if action == "undoset":
        await cancel_rest_timer(context, user_id, session_id)
        undone = await undo_last_set(user_id, session_id)
        if undone:
            await query.answer("הסט האחרון בוטל", show_alert=False)
        else:
            await query.answer("אין סט לביטול", show_alert=False)
        await show_session(query, user_id, session_id)
        return True

    if action == "ready":
        await clear_split_state(user_id, session_id)
        await cancel_rest_timer(context, user_id, session_id)
        await show_session(query, user_id, session_id)
        return True
    return False

@runtime_bound(RUNTIME_NAMES)
async def _handle_session_split_actions(
    query: Any,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    action: str,
    parts: list[str],
    session_id: int,
    session: dict[str, Any],
    plan: dict[str, Any],
    current: dict[str, Any],
    weight: float,
    reps: int,
) -> bool:
    if action == "split":
        await set_split_state(
            user_id,
            session_id,
            {
                "first_weight": weight,
                "second_weight": max(0.0, round(weight - current["inc"], 2)),
            },
        )
        await safe_edit(
            query,
            "<b>סט מפוצל</b>\n\nחלק ראשון — בחר משקל.",
            split_weight_keyboard(session, 1, weight, current["inc"]),
        )
        return True

    if action == "splitw":
        split_state = await get_split_state(user_id, session_id)
        part = int(session_action_arg(parts, 0))
        chosen_weight = float(session_action_arg(parts, 1))

        if part == 1:
            split_state["first_weight"] = chosen_weight
            await set_split_state(user_id, session_id, split_state)
            await safe_edit(
                query,
                f"חלק ראשון — {chosen_weight:g} ק״ג\nכמה חזרות עשית?",
                split_reps_keyboard(session, 1, max(current["rmin"], min(current["rmax"], reps))),
            )
        else:
            split_state["second_weight"] = chosen_weight
            await set_split_state(user_id, session_id, split_state)
            await safe_edit(
                query,
                f"חלק שני — {chosen_weight:g} ק״ג\nכמה חזרות עשית?",
                split_reps_keyboard(session, 2, max(current["rmin"], min(current["rmax"], reps))),
            )
        return True

    if action == "splitr":
        split_state = await get_split_state(user_id, session_id)
        part = int(session_action_arg(parts, 0))
        reps_value = int(session_action_arg(parts, 1))

        if part == 1:
            split_state["first_reps"] = reps_value
            second_base = split_state.get(
                "second_weight",
                max(
                    0.0,
                    round(split_state.get("first_weight", weight) - current["inc"], 2),
                ),
            )
            await set_split_state(user_id, session_id, split_state)
            await safe_edit(
                query,
                "חלק שני — בחר משקל.",
                split_weight_keyboard(session, 2, float(second_base), current["inc"]),
            )
        else:
            split_state["second_reps"] = reps_value
            await set_split_state(user_id, session_id, split_state)
            total_reps = split_state.get("first_reps", 0) + reps_value
            await safe_edit(
                query,
                f"סה״כ ביצעת {total_reps} חזרות.\nכמה נשארו בסוף הסט?",
                split_rir_keyboard(session),
            )
        return True

    if action == "splitrir":
        split_state = await get_split_state(user_id, session_id)
        if split_state is None:
            await show_session(query, user_id, session_id)
            return True
        first_weight = float(split_state.get("first_weight", weight))
        first_reps = int(split_state.get("first_reps", 0))
        second_weight = float(
            split_state.get(
                "second_weight",
                max(0.0, weight - current["inc"]),
            )
        )
        second_reps = int(split_state.get("second_reps", 0))
        final_rir = int(session_action_arg(parts))

        if first_reps <= 0 or second_reps <= 0:
            await safe_edit(
                query,
                "כדי לשמור סט מפוצל צריך לבחור חזרות לשני החלקים.",
                split_rir_keyboard(session),
            )
            return True
        try:
            completed, rest = await save_split_set(
                session,
                first_weight,
                first_reps,
                second_weight,
                second_reps,
                final_rir,
            )
        except _StaleSetStep:
            await clear_split_state(user_id, session_id)
            await show_session(query, user_id, session_id)
            return True
        summary_line = split_summary_line(
            first_weight,
            first_reps,
            second_weight,
            second_reps,
            final_rir,
        )
        await clear_split_state(user_id, session_id)

        if completed:
            await safe_edit(
                query,
                await workout_summary(user_id, session_id),
                home_keyboard(),
            )
            return True
        await start_rest_timer(
            context=context,
            query=query,
            user_id=user_id,
            session_id=session_id,
            weight=first_weight,
            reps=first_reps + second_reps,
            rir=final_rir,
            rest_seconds=rest,
            summary_line=summary_line,
        )
        return True
    return False

@runtime_bound(RUNTIME_NAMES)
async def _handle_session_adjustment_actions(
    query: Any,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    action: str,
    parts: list[str],
    session_id: int,
    session: dict[str, Any],
    plan: dict[str, Any],
    current: dict[str, Any],
    weight: float,
    reps: int,
) -> bool:
    if action == "different":
        step = max(0.5, float(current.get("inc", 2.5)))
        await safe_edit(
            query,
            "בחר שינוי משקל",
            InlineKeyboardMarkup(
                [
                    [
                        button(
                            f"−{2 * step:g}",
                            session_action_data("weight", session, -2 * step),
                        ),
                        button(f"−{step:g}", session_action_data("weight", session, -step)),
                        button(f"{weight:g} ✓", session_action_data("weight", session, 0)),
                        button(f"+{step:g}", session_action_data("weight", session, step)),
                        button(
                            f"+{2 * step:g}",
                            session_action_data("weight", session, 2 * step),
                        ),
                    ]
                ]
            ),
        )
        return True

    if action == "weight":
        new_weight = max(
            0.0,
            weight + float(session_action_arg(parts)),
        )
        if not await update_session_step(
            session,
            "pending_weight=?",
            (new_weight,),
        ):
            await show_session(query, user_id, session_id)
            return True
        center = max(current["rmin"], min(current["rmax"], reps))
        choices = list(range(max(1, center - 2), center + 3))
        rows = [
            [
                button(
                    str(value),
                    session_action_data("reps", session, value),
                )
                for value in choices[index : index + 5]
            ]
            for index in range(0, len(choices), 5)
        ]
        await safe_edit(
            query,
            f"משקל: <b>{new_weight:g}</b>\nכמה חזרות? (יעד ~{center})",
            InlineKeyboardMarkup(rows),
        )
        return True

    if action == "reps":
        chosen_reps = int(session_action_arg(parts))
        if not await update_session_step(
            session,
            "pending_reps=?",
            (chosen_reps,),
        ):
            await show_session(query, user_id, session_id)
            return True
        await safe_edit(
            query,
            "כמה נשארו?",
            InlineKeyboardMarkup(
                [
                    [
                        button("3+", session_action_data("rir", session, 3)),
                        button("2", session_action_data("rir", session, 2)),
                        button("1", session_action_data("rir", session, 1)),
                        button("0/כשל", session_action_data("rir", session, 0)),
                    ]
                ]
            ),
        )
        return True

    if action == "rir":
        refreshed = await DB.fetch_one(
            "SELECT * FROM sessions WHERE id=?",
            (session_id,),
        )
        actual_weight = float(
            refreshed["pending_weight"] if refreshed["pending_weight"] is not None else weight
        )
        actual_reps = int(
            refreshed["pending_reps"] if refreshed["pending_reps"] is not None else reps
        )
        actual_rir = int(session_action_arg(parts))

        result = await try_save_set(
            refreshed,
            actual_weight,
            actual_reps,
            actual_rir,
            "telegram_adjusted",
        )
        if result is None:
            await show_session(query, user_id, session_id)
            return True
        completed, rest = result
        if completed:
            await safe_edit(
                query,
                await workout_summary(user_id, session_id),
                home_keyboard(),
            )
            return True
        await start_rest_timer(
            context=context,
            query=query,
            user_id=user_id,
            session_id=session_id,
            weight=actual_weight,
            reps=actual_reps,
            rir=actual_rir,
            rest_seconds=rest,
        )
        return True

    if action == "occupied":
        buttons = []
        for index, alt in enumerate(current["alts"][:3]):
            alt_muscle = EXERCISE_MUSCLES.get(alt["id"], current.get("muscle", ""))
            label = f"{index + 1}. {alt['name']}"
            if alt_muscle:
                label += f" ({alt_muscle})"
            buttons.append([button(label, session_action_data("sub", session, index))])
        buttons.append([button("דלג", session_action_data("skip", session))])
        await safe_edit(query, "<b>שלוש חלופות</b>", InlineKeyboardMarkup(buttons))
        return True

    if action == "sub":
        alternative_index = int(session_action_arg(parts))
        if not 0 <= alternative_index < len(current["alts"]):
            await query.answer("החלופה אינה זמינה", show_alert=False)
            await show_session(query, user_id, session_id)
            return True
        alternative = current["alts"][alternative_index]
        replacement = dict(current)
        replacement.update(
            id=alternative["id"],
            name=alternative["name"],
            weight=alternative["weight"],
            muscle=EXERCISE_MUSCLES.get(
                alternative["id"],
                current.get("muscle", ""),
            ),
            original_id=current.get("original_id", current["id"]),
        )
        plan["exercises"][session["exercise_index"]] = replacement
        if not await update_session_step(
            session,
            "plan=?, pending_weight=NULL, pending_reps=NULL",
            (json.dumps(plan, ensure_ascii=False),),
        ):
            await show_session(query, user_id, session_id)
            return True
        await write_audit(
            user_id,
            "approve_substitution",
            "exercise",
            session_id,
            source=current["id"],
            target=alternative["id"],
        )
        await show_session(query, user_id, session_id)
        return True
    return False

@runtime_bound(RUNTIME_NAMES)
async def _handle_session_safety_actions(
    query: Any,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    action: str,
    parts: list[str],
    session_id: int,
    session: dict[str, Any],
    plan: dict[str, Any],
    current: dict[str, Any],
    weight: float,
    reps: int,
) -> bool:
    if action == "pain":
        await safe_edit(
            query,
            "איפה הכאב?",
            InlineKeyboardMarkup(
                [
                    [
                        button("כתף", session_action_data("painloc", session, "shoulder")),
                        button("מרפק", session_action_data("painloc", session, "elbow")),
                    ],
                    [
                        button("גב", session_action_data("painloc", session, "back")),
                        button("ברך", session_action_data("painloc", session, "knee")),
                    ],
                    [button("אחר", session_action_data("painloc", session, "other"))],
                ]
            ),
        )
        return True

    if action == "painloc":
        pain_location = session_action_arg(parts)
        if pain_location not in {
            "shoulder",
            "elbow",
            "back",
            "knee",
            "other",
        }:
            await query.answer("בחירת הכאב אינה תקינה", show_alert=False)
            return True
        if not await update_session_step(
            session,
            "pain_location=?",
            (pain_location,),
        ):
            await show_session(query, user_id, session_id)
            return True
        await safe_edit(
            query,
            "עוצמת הכאב?",
            InlineKeyboardMarkup(
                [
                    [
                        button(
                            "קל",
                            session_action_data("painlevel", session, 1),
                        ),
                        button(
                            "בינוני",
                            session_action_data("painlevel", session, 2),
                        ),
                        button(
                            "חד/חזק",
                            session_action_data("painlevel", session, 3),
                        ),
                    ]
                ]
            ),
        )
        return True

    if action == "painlevel":
        severity = int(session_action_arg(parts))
        if severity not in {1, 2, 3}:
            await query.answer("עוצמת הכאב אינה תקינה", show_alert=False)
            return True
        pain_location = session.get("pain_location")
        if not pain_location:
            await query.answer(
                "הדיווח כבר נשמר או שהמסך אינו עדכני",
                show_alert=False,
            )
            await show_session(query, user_id, session_id)
            return True
        now = utc_now()
        try:
            async with DB.transaction() as connection:
                claimed = await connection.execute(
                    """
                    UPDATE sessions
                    SET pain_location=NULL
                    WHERE id=?
                      AND status='active'
                      AND exercise_index=?
                      AND set_number=?
                      AND pain_location=?
                    """,
                    (
                        session_id,
                        session["exercise_index"],
                        session["set_number"],
                        pain_location,
                    ),
                )
                if claimed.rowcount != 1:
                    raise _StaleSetStep()

                cursor = await connection.execute(
                    """
                    INSERT INTO medical_constraints(
                        user_id, kind, location, severity, status,
                        note, affects, created_at
                    ) VALUES(?, 'pain', ?, ?, 'active', ?, ?, ?)
                    """,
                    (
                        user_id,
                        pain_location,
                        severity,
                        f"reported during workout on exercise {current['id']}",
                        json.dumps(
                            ["exercise_selection"],
                            ensure_ascii=False,
                        ),
                        now,
                    ),
                )
                constraint_id = int(cursor.lastrowid or 0)
                await connection.execute(
                    """
                    INSERT INTO audit(
                        user_id, action, entity, entity_id,
                        details, created_at
                    ) VALUES(?, 'pain_report', 'exercise', ?, ?, ?)
                    """,
                    (
                        user_id,
                        current["id"],
                        json.dumps(
                            {
                                "level": severity,
                                "location": pain_location,
                                "constraint_id": constraint_id,
                            },
                            ensure_ascii=False,
                        ),
                        now,
                    ),
                )
        except _StaleSetStep:
            await show_session(query, user_id, session_id)
            return True
        if severity >= 3:
            await safe_edit(
                query,
                "<b>עוצרים את התרגיל.</b> כאב חד אינו מצב להמשך אתגר.",
                InlineKeyboardMarkup(
                    [
                        [
                            button(
                                "דלג",
                                session_action_data("skip", session),
                            ),
                            button(
                                "סיים",
                                session_action_data("finish", session),
                            ),
                        ]
                    ]
                ),
            )
            return True
        buttons = []
        for index, alt in enumerate(current["alts"][:3]):
            alt_muscle = EXERCISE_MUSCLES.get(
                alt["id"],
                current.get("muscle", ""),
            )
            label = alt["name"] + (f" ({alt_muscle})" if alt_muscle else "")
            buttons.append(
                [
                    button(
                        label,
                        session_action_data("sub", session, index),
                    )
                ]
            )
        buttons.append([button("דלג", session_action_data("skip", session))])
        await safe_edit(
            query,
            "בחר חלופה שאינה מעוררת כאב.",
            InlineKeyboardMarkup(buttons),
        )
        return True
    return False

@runtime_bound(RUNTIME_NAMES)
async def _handle_session_lifecycle_actions(
    query: Any,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    action: str,
    parts: list[str],
    session_id: int,
    session: dict[str, Any],
    plan: dict[str, Any],
    current: dict[str, Any],
    weight: float,
    reps: int,
) -> bool:
    if action == "skip":
        await clear_split_state(user_id, session_id)
        if session["exercise_index"] + 1 >= len(plan["exercises"]):
            planned_sets = sum(ex["sets"] for ex in plan["exercises"])
            done = await DB.fetch_one(
                "SELECT COUNT(*) AS c FROM sets WHERE session_id=? "
                "AND source != 'telegram_split_secondary'",
                (session_id,),
            )
            try:
                _started = datetime.fromisoformat(session["started_at"])
                _duration_s = (datetime.fromisoformat(utc_now()) - _started).total_seconds()
            except (TypeError, ValueError, KeyError):
                _duration_s = None
            status = training_intelligence.workout_status(
                int(done["c"]) if done else 0,
                planned_sets,
                duration_seconds=_duration_s,
            )
            changed = await update_session_step(
                session,
                "status=?, ended_at=?",
                (status, utc_now()),
            )
            if not changed:
                await show_session(query, user_id, session_id)
                return True
            await safe_edit(
                query,
                await workout_summary(user_id, session_id),
                home_keyboard(),
            )
            return True
        changed = await update_session_step(
            session,
            """
            exercise_index=exercise_index+1,
            set_number=1,
            pending_weight=NULL,
            pending_reps=NULL
            """,
        )
        if not changed:
            await show_session(query, user_id, session_id)
            return True
        await show_session(query, user_id, session_id)
        return True

    if action == "finish":
        # Don't assume "finish" means a completed workout — show progress and let
        # the user choose, with a clear warning if barely anything was logged.
        plan_now = json.loads(session["plan"])
        planned_sets = sum(int(ex["sets"]) for ex in plan_now["exercises"])
        done = await DB.fetch_one(
            "SELECT COUNT(*) AS c FROM sets "
            "WHERE session_id=? AND source != 'telegram_split_secondary'",
            (session_id,),
        )
        logged_sets = int(done["c"]) if done else 0
        progress = f"בוצעו <b>{logged_sets}</b> מתוך <b>{planned_sets}</b> סטים מתוכננים."
        warn = (
            "\n\n⚠️ <i>בוצע סט אחד או פחות — בטוח לסיים עכשיו?</i>"
            if logged_sets <= 1
            else ""
        )
        await safe_edit(
            query,
            f"<b>לסיים את האימון?</b>\n{progress}{warn}\nאיך לסמן אותו?",
            InlineKeyboardMarkup(
                [
                    [button("⏸️ אמשיך מאוחר יותר", session_action_data("wpause", session))],
                    [button("✅ סיים מלא", session_action_data("wdone", session, "full"))],
                    [button("🟡 סיים חלקי", session_action_data("wdone", session, "partial"))],
                    [button("❌ בטל אימון", session_action_data("wcancel", session))],
                    [button("↩️ חזרה לאימון", session_action_data("ready", session))],
                ]
            ),
        )
        return True

    if action == "wpause":
        # Keep the session active so it can be resumed; just exit the screen.
        await clear_split_state(user_id, session_id)
        await cancel_rest_timer(context, user_id, session_id)
        await write_audit(user_id, "pause", "workout", session_id)
        await safe_edit(
            query,
            'השהיתי את האימון ⏸️ אפשר להמשיך בכל רגע דרך "אימון".',
            home_keyboard(),
        )
        return True

    if action == "wdone":
        await clear_split_state(user_id, session_id)
        await cancel_rest_timer(context, user_id, session_id)

        plan_now = json.loads(session["plan"])
        planned_sets = sum(ex["sets"] for ex in plan_now["exercises"])
        done = await DB.fetch_one(
            "SELECT COUNT(*) AS c FROM sets "
            "WHERE session_id=? AND source != 'telegram_split_secondary'",
            (session_id,),
        )
        logged_sets = int(done["c"]) if done else 0
        # The user's explicit choice (full/partial) is honored, but a workout
        # that did not reach all planned sets — or that lasted no real time —
        # can never be marked full (P0/P1). Duration uses real timestamps.
        user_choice = session_action_arg(parts, 0) or ""
        try:
            _started = datetime.fromisoformat(session["started_at"])
            if _started.tzinfo is None:
                _started = _started.replace(tzinfo=timezone.utc)
            _now_ts = datetime.fromisoformat(utc_now())
            if _now_ts.tzinfo is None:
                _now_ts = _now_ts.replace(tzinfo=timezone.utc)
            _duration_s = (_now_ts - _started).total_seconds()
        except (TypeError, ValueError, KeyError):
            _duration_s = None
        status = training_intelligence.workout_status(
            logged_sets,
            planned_sets,
            user_choice=user_choice,
            duration_seconds=_duration_s,
        )
        is_partial = status != "completed"
        changed = await update_session_step(
            session,
            "status=?, ended_at=?",
            (status, utc_now()),
        )
        if not changed:
            await show_session(query, user_id, session_id)
            return True
        await write_audit(
            user_id,
            "finish",
            "workout",
            session_id,
            partial=is_partial,
        )
        note = "\n\n<i>סומן כאימון חלקי — מה שבוצע נשמר.</i>" if is_partial else ""
        await safe_edit(
            query,
            await workout_summary(user_id, session_id) + note,
            InlineKeyboardMarkup(
                [
                    [button("↩️ חזרה לאימון (בוטל בטעות)", session_action_data("reopen", session))],
                    [button("⬅️ תפריט", "menu:home")],
                ]
            ),
        )
        return True

    if action == "wcancel":
        await clear_split_state(user_id, session_id)
        await cancel_rest_timer(context, user_id, session_id)
        changed = await update_session_step(
            session,
            "status='cancelled', ended_at=?",
            (utc_now(),),
        )
        if not changed:
            await show_session(query, user_id, session_id)
            return True
        await write_audit(
            user_id,
            "cancel",
            "workout",
            session_id,
        )
        await safe_edit(
            query,
            "האימון בוטל. אין בעיה — נמשיך בפעם הבאה.",
            home_keyboard(),
        )
        return True
    return False

@runtime_bound(RUNTIME_NAMES)
async def handle_session_action_callback(
    query: Any,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    data: str,
) -> None:
    """Route a session-scoped workout callback to one focused action family."""
    parts = data.split(":")
    if len(parts) < 2 or not parts[1].isdigit():
        return

    action = parts[0]
    session_id = int(parts[1])
    session = await DB.fetch_one(
        "SELECT * FROM sessions WHERE id=? AND user_id=?",
        (session_id, user_id),
    )

    if action == "reopen":
        if not session:
            await safe_edit(query, "האימון אינו זמין.", home_keyboard())
            return
        if await active_session(user_id):
            await query.answer("כבר יש אימון פעיל", show_alert=False)
            return
        reopened = await DB.execute_rowcount(
            "UPDATE sessions SET status='active', ended_at=NULL "
            "WHERE id=? AND user_id=? AND status IN ('completed','partial')",
            (session_id, user_id),
        )
        if reopened:
            await write_audit(user_id, "reopen", "workout", session_id)
            await show_session(query, user_id, session_id)
        else:
            await safe_edit(query, "אי אפשר לפתוח מחדש את האימון הזה.", home_keyboard())
        return

    if not session or session["status"] != "active":
        await safe_edit(query, "האימון אינו פעיל.", home_keyboard())
        return

    plan = json.loads(session["plan"])
    current = plan["exercises"][session["exercise_index"]]
    weight, reps, _ = await recommend_load(user_id, current)

    if action in SESSION_SCOPED_ACTIONS and not is_current_session_step(parts, session):
        await query.answer(
            "המסך כבר לא עדכני — מציג את הסט הנוכחי",
            show_alert=False,
        )
        await show_session(query, user_id, session_id)
        return

    handlers = (
        _handle_session_core_actions,
        _handle_session_split_actions,
        _handle_session_adjustment_actions,
        _handle_session_safety_actions,
        _handle_session_lifecycle_actions,
    )
    for handler in handlers:
        if await handler(
            query,
            context,
            user_id,
            action,
            parts,
            session_id,
            session,
            plan,
            current,
            weight,
            reps,
        ):
            return
