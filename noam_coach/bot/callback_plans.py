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

PENDING_PLAN_ACTION_FLOW = "pending_plan_action"
PENDING_PLAN_GENERATE_STEP = "generate_candidates"


async def _set_pending_plan_action(user_id: int, plan_type: str) -> None:
    from noam_coach.bot.onboarding import set_flow_state

    await set_flow_state(
        user_id,
        PENDING_PLAN_ACTION_FLOW,
        PENDING_PLAN_GENERATE_STEP,
        {"plan_type": plan_type},
    )


async def _clear_pending_plan_action(user_id: int) -> None:
    from noam_coach.bot.onboarding import clear_flow_state

    await clear_flow_state(user_id, PENDING_PLAN_ACTION_FLOW)


@runtime_bound(RUNTIME_NAMES)
async def _render_planning_blocked(
    query: Any,
    user_id: int,
    exc: planning.PlanningBlockedError,
    *,
    plan_type: str | None = None,
    remember: bool = False,
) -> None:
    missing = list(getattr(exc, "missing", None) or [])
    if remember and plan_type in {"nutrition", "workout"}:
        await _set_pending_plan_action(user_id, plan_type)
    labels = [planning.FACT_LABELS.get(key, user_model.display_label(key)) for key in missing]
    tail = f"\n\nחסר: {esc(', '.join(labels))}" if labels else ""
    rows = []
    if "active_goal" in missing:
        rows.append([button("אשר יעד ואז נמשיך", "menu:goal")])
    else:
        rows.append([button("השלם עכשיו", "planv2:complete_missing")])
    rows.append([button("הצג מה חסר", "planv2:profile")])
    rows.append([button("חזרה לתוכניות", "menu:smartplan")])
    await safe_edit(
        query,
        f"<b>אי אפשר לבנות את התוכנית עדיין.</b>\n{esc(str(exc))}{tail}",
        InlineKeyboardMarkup(rows),
    )


@runtime_bound(RUNTIME_NAMES)
async def resume_pending_plan_action(query: Any, user_id: int) -> bool:
    from noam_coach.bot.onboarding import get_flow_state

    state = await get_flow_state(user_id, PENDING_PLAN_ACTION_FLOW)
    if not state or state.get("step") != PENDING_PLAN_GENERATE_STEP:
        return False
    payload = state.get("payload") or {}
    plan_type = str(payload.get("plan_type") or "")
    if plan_type not in {"nutrition", "workout"}:
        await _clear_pending_plan_action(user_id)
        return False
    await safe_edit(query, "ממשיך מאיפה שעצרנו ובונה את ההצעות...", None)
    try:
        await planning.generate_candidates(DB, user_id, plan_type)  # type: ignore[arg-type]
        await event_log.append_event(
            DB,
            user_id,
            "PLAN_CANDIDATES_GENERATED",
            entity="plan",
            source="planner",
            properties={"plan_type": plan_type, "count": 3, "resumed": True},
        )
        await _clear_pending_plan_action(user_id)
        await render_candidate_list(query, user_id, plan_type)
        return True
    except planning.PlanningBlockedError as exc:
        await _render_planning_blocked(query, user_id, exc, plan_type=plan_type, remember=True)
        return True


@runtime_bound(RUNTIME_NAMES)
async def _handle_workout_menu_actions(
    query: Any,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    data: str,
) -> bool:
    if data == "menu:workout":
        session = await active_session(user_id)
        if session:
            await show_session(query, user_id, session["id"])
            return True
        code = await select_todays_workout_code(user_id)
        if code:
            # Auto-pick today's workout (or next in cycle) and show its brief.
            await render_workout_overview(query, user_id, code)
        else:
            await safe_edit(
                query,
                "האימון של היום כבר הושלם ✅\nרוצה לבחור אימון נוסף בכל זאת?",
                plans_keyboard(),
            )
        return True

    if data == "menu:weekly":
        await safe_edit(
            query,
            await build_weekly_summary_text(user_id),
            InlineKeyboardMarkup([[button("⬅️ תפריט", "menu:home")]]),
        )
        return True

    if data == "menu:chart":
        ok = await send_weight_chart(context.bot, query.message.chat_id, user_id)
        if not ok:
            await safe_edit(
                query,
                "עוד אין מספיק נתוני משקל כדי להציג גרף.",
                InlineKeyboardMarkup([[button("⬅️ תפריט", "menu:home")]]),
            )
        return True

    if data == "menu:app":
        url = mini_app_url(user_id)
        if url:
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🌐 פתח Mini App", web_app=WebAppInfo(url=url))],
                [button("⬅️ תפריט", "menu:home")],
            ])
            await safe_edit(query, "לחץ למטה לפתיחת Mini App.", kb)
        else:
            await safe_edit(
                query,
                "Mini App עדיין לא הוגדר (PUBLIC_BASE_URL חסר).",
                InlineKeyboardMarkup([[button("⬅️ תפריט", "menu:home")]]),
            )
        return True

    if data == "menu:health":
        await safe_edit(
            query,
            await build_health_status_text(user_id),
            InlineKeyboardMarkup(
                [
                    [button("📥 שלח ייצוא חדש", "menu:health_import")],
                    [button("⬅️ תפריט", "menu:home")],
                ]
            ),
        )
        return True

    if data == "menu:health_import":
        await safe_edit(
            query,
            "שלח לי כאן את קובץ ה-ZIP של ייצוא Apple Health ואעבד אותו. "
            "אני שומר נתונים של עד 18 חודשים אחורה.",
            InlineKeyboardMarkup([[button("⬅️ תפריט", "menu:home")]]),
        )
        return True

    if data == "menu:goal":
        goal = await fetch_goal(user_id)
        payload = {
            "calories": int(goal["calories"]),
            "protein": int(goal["protein"]),
            "steps": int(goal["steps"]),
            "phase": goal.get("phase", "fat_loss_muscle_retention"),
            "provisional": bool(goal.get("provisional")),
            "explanation": goal.get("explanation", ""),
        }
        approval_id = await create_approval(user_id, "goal", payload)
        missing = goal.get("missing_inputs") or []
        status_line = (
            "⚠️ <b>יעד זמני</b> — חסרים: "
            + ", ".join(esc(user_model.display_label(item)) for item in missing)
            if payload["provisional"]
            else "✅ מחושב מהפרופיל שלך"
        )
        rows = []
        if payload["provisional"]:
            rows.append([button("👤 הצג מה חסר", "planv2:profile")])
            rows.append([
                button("⏳ השתמש זמנית", f"approve_goal:{approval_id}"),
                button("❌ דחה", f"reject_goal:{approval_id}"),
            ])
        else:
            rows.append([
                button("✅ אשר כיעד פעיל", f"approve_goal:{approval_id}"),
                button("❌ דחה", f"reject_goal:{approval_id}"),
            ])
        rows.append([button("⬅️ תפריט", "menu:home")])
        await safe_edit(
            query,
            (
                "<b>הצעת יעד</b>\n\n"
                f"קלוריות: <b>{payload['calories']:,}</b>\n"
                f"חלבון: <b>{payload['protein']} גרם</b>\n"
                f"צעדים: <b>{payload['steps']:,}</b>\n\n"
                f"{status_line}\n\n"
                f"<i>{esc(payload['explanation'])}</i>\n\n"
                "היעד לא ישתנה בעתיד בלי אישור שלך."
            ),
            InlineKeyboardMarkup(rows),
        )
        return True
    return False


@runtime_bound(RUNTIME_NAMES)
async def _handle_workout_start_actions(
    query: Any,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    data: str,
) -> bool:
    if data.startswith("workout:"):
        code = data.split(":", 1)[1]
        current = await active_session(user_id)
        if current:
            await show_session(query, user_id, current["id"])
            return True

        await render_workout_overview(query, user_id, code)
        return True

    if data.startswith("startworkout:"):
        code = data.split(":", 1)[1]
        current = await active_session(user_id)
        if current:
            await show_session(query, user_id, current["id"])
            return True

        # Bake the user's overrides into the session snapshot.
        plan = await get_user_plan(user_id, code)
        try:
            session_id = await DB.execute(
                """
                INSERT INTO sessions(
                    user_id, code, name, plan, status,
                    exercise_index, set_number, started_at
                )
                VALUES(?, ?, ?, ?, 'active', 0, 1, ?)
                """,
                (
                    user_id,
                    code,
                    plan["name"],
                    json.dumps(plan, ensure_ascii=False),
                    utc_now(),
                ),
            )
        except aiosqlite.IntegrityError:
            # The unique partial index rejected a second concurrent start —
            # show the session that already opened.
            existing = await active_session(user_id)
            if existing:
                await show_session(query, user_id, existing["id"])
            return True
        await write_audit(user_id, "start", "workout", session_id, code=code)
        await show_session(query, user_id, session_id)
        return True
    return False


@runtime_bound(RUNTIME_NAMES)
async def _handle_workout_parameter_actions(
    query: Any,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    data: str,
) -> bool:
    if data.startswith("editparams_menu:"):
        code = data.split(":", 1)[1]
        await safe_edit(
            query,
            "<b>בחר תרגיל לעריכת פרמטרים</b>",
            exercise_picker_keyboard(code),
        )
        return True

    if data.startswith("editparams:"):
        _, code, exercise_index = data.split(":")
        await render_exercise_params(query, user_id, code, int(exercise_index))
        return True

    if data.startswith("param:"):
        _, code, exercise_index_text, field, delta_text = data.split(":")
        exercise_index = int(exercise_index_text)
        delta = float(delta_text)
        # Work on the user's effective plan (template + their overrides), never
        # the global template. Compute the new value and persist it per-user.
        plan = await get_user_plan(user_id, code)
        cur = plan["exercises"][exercise_index]

        new_min, new_max = cur["rmin"], cur["rmax"]
        if field == "weight":
            new_val: float = max(0, round(cur["weight"] + delta, 2))
        elif field == "sets":
            new_val = max(1, int(cur["sets"] + delta))
        elif field == "rmin":
            new_min = max(1, int(cur["rmin"] + delta))
            new_val = new_min
            if new_min > new_max:
                new_max = new_min
                await set_exercise_override(user_id, code, exercise_index, "rmax", new_max)
        elif field == "rmax":
            new_max = max(cur["rmin"], int(cur["rmax"] + delta))
            new_val = new_max
        elif field == "rest":
            new_val = max(30, int(cur["rest"] + delta))
        else:
            return True

        await set_exercise_override(user_id, code, exercise_index, field, new_val)
        await render_exercise_params(query, user_id, code, exercise_index)
        return True
    return False


@runtime_bound(RUNTIME_NAMES)
async def handle_workout_setup_callback(
    query: Any,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    data: str,
) -> bool:
    """Route workout setup callbacks to menu, start and parameter handlers."""
    handlers = (
        _handle_workout_menu_actions,
        _handle_workout_start_actions,
        _handle_workout_parameter_actions,
    )
    for handler in handlers:
        if await handler(query, context, user_id, data):
            return True
    return False

@runtime_bound(RUNTIME_NAMES)
async def handle_plan_callback(query: Any, user_id: int, data: str) -> bool:
    """Handle planning-hub callbacks (smart plan v2 + legacy plan recommend).

    Covers the profile snapshot, generating/selecting the three nutrition or
    workout candidates, building the unified week and the legacy plan
    recommend/set. Returns True when *data* was handled.
    """
    if data in {"menu:smartplan", "menu:plan"}:
        from noam_coach.bot.onboarding import (
            PLAN_COMPLETION_FLOW,
            clear_flow_state as clear_plan_flow_state,
            get_flow_state as get_plan_flow_state,
        )

        plan_flow = await get_plan_flow_state(user_id, PLAN_COMPLETION_FLOW)
        if plan_flow:
            await clear_plan_flow_state(user_id, PLAN_COMPLETION_FLOW)
            current = await conversation.get_active_flow(DB, user_id)
            if current.is_question and current.step == plan_flow.get("step"):
                await clear_pending(user_id)
        await render_smart_plan_hub(query, user_id)
        return True

    if data == "planv2:profile":
        await render_profile_snapshot(query, user_id)
        return True

    if data == "planv2:complete_missing":
        from noam_coach.bot.onboarding import ask_next_plan_completion_question

        await ask_next_plan_completion_question(query, user_id)
        return True

    if data.startswith("planv2:generate:"):
        plan_type = data.split(":", 2)[2]
        if plan_type not in {"nutrition", "workout"}:
            await query.answer("סוג תוכנית לא תקין", show_alert=True)
            return True
        readiness = await user_model.compute_readiness(DB, user_id, plan_type)
        if not readiness["ready"]:
            labels = [
                user_model.FACT_REGISTRY[key].label
                if key in user_model.FACT_REGISTRY
                else key
                for key in readiness["missing"]
            ]
            # REC-ONBOARD-02-10: grouped missing info with direct completion
            missing_display = readiness.get("missing_labels", labels)
            lines_m = [f"<b>חסרים פרטים ליצירת תוכנית {user_model.display_label(plan_type)}:</b>", ""]
            lines_m.extend(f"• {esc(label)}" for label in missing_display)
            deferred = readiness.get("deferred", [])
            if deferred:
                lines_m.append("")
                lines_m.append("<i>נדחו למילוי מאוחר:</i>")
                lines_m.extend(f"• {esc(user_model.display_label(k))}" for k in deferred)
            buttons_m = [
                [button("▶️ השלם עכשיו", "planv2:complete_missing")],
                [button("⏳ אשלים אחר כך", "menu:smartplan")],
                [button("⬅️ חזרה לתוכניות", "menu:smartplan")],
            ]
            await safe_edit(
                query,
                "\n".join(lines_m),
                InlineKeyboardMarkup(buttons_m),
            )
            return True
        await safe_edit(query, "בונה שלוש חלופות ובודק אותן מול המגבלות שלך… ⏳", None)
        try:
            await planning.generate_candidates(DB, user_id, plan_type)  # type: ignore[arg-type]
            await event_log.append_event(
                DB,
                user_id,
                "PLAN_CANDIDATES_GENERATED",
                entity="plan",
                source="planner",
                properties={"plan_type": plan_type, "count": 3},
            )
            await render_candidate_list(query, user_id, plan_type)
        except planning.PlanningBlockedError as exc:
            await _render_planning_blocked(query, user_id, exc, plan_type=plan_type, remember=True)
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("Candidate generation failed")
            await safe_edit(
                query,
                friendly_error(exc, "plan generation"),
                InlineKeyboardMarkup([[button("⬅️ לתוכניות", "menu:smartplan")]]),
            )
        return True

    if data.startswith("planv2:select:"):
        parts_v2 = data.split(":")
        if len(parts_v2) < 3 or not parts_v2[2].isdigit():
            return True
        plan_id = int(parts_v2[2])
        try:
            selected = await planning.activate_plan(DB, user_id, plan_id)
        except planning.PlanningBlockedError as exc:
            # Low readiness / missing data must not silently fail — explain it.
            labels = [planning.FACT_LABELS.get(k, k) for k in (getattr(exc, "missing", None) or [])]
            tail = f"\nחסר: {esc(', '.join(labels))}" if labels else ""
            await safe_edit(
                query,
                f"עוד אי אפשר להפעיל את התוכנית.\n{esc(str(exc))}{tail}",
                InlineKeyboardMarkup([[button("👤 הצג מה חסר", "planv2:profile")], [button("⬅️ תפריט", "menu:home")]]),
            )
            return True
        if not selected:
            await safe_edit(query, "ההצעה כבר אינה זמינה.", home_keyboard())
            return True
        await conversation.clear_active_flow(DB, user_id)
        await event_log.append_event(
            DB,
            user_id,
            "PLAN_ACTIVATED",
            entity="plan",
            entity_id=plan_id,
            source="user",
            properties={
                "plan_type": selected["plan_type"],
                "title": selected["title"],
                "fit_score": selected["fit_score"],
            },
        )
        nutrition = await planning.get_active_plan(DB, user_id, "nutrition")
        workout = await planning.get_active_plan(DB, user_id, "workout")
        rows = [[button("⬅️ לתוכניות", "menu:smartplan")]]
        extra = ""
        if nutrition and workout:
            rows.insert(0, [button("📅 בנה תוכנית שבועית מאוחדת", "planv2:unify")])
            extra = "\n\nשתי התוכניות נבחרו — אפשר עכשיו לחבר אותן לשבוע אחד."
        await safe_edit(
            query,
            f"<b>{esc(selected['title'])}</b> נבחרה כתוכנית {_plan_type_label(selected['plan_type'])} הראשית ✅{extra}",
            InlineKeyboardMarkup(rows),
        )
        return True

    if data == "planv2:unify":
        await safe_edit(query, "מחבר את התזונה, האימונים והשעות לשבוע אחד… ⏳", None)
        try:
            candidate = await planning.build_unified_week(DB, user_id)
            await event_log.append_event(
                DB,
                user_id,
                "UNIFIED_PLAN_CREATED",
                entity="plan",
                entity_id=candidate.id,
                source="planner",
            )
            await render_unified_plan(query, user_id)
        except planning.PlanningBlockedError as exc:
            await _render_planning_blocked(query, user_id, exc)
        except Exception as exc:  # noqa: BLE001
            await safe_edit(
                query,
                friendly_error(exc, "unified plan"),
                InlineKeyboardMarkup([[button("⬅️ לתוכניות", "menu:smartplan")]]),
            )
        return True

    if data == "planv2:show:unified":
        await render_unified_plan(query, user_id)
        return True

    if data == "plan:recommend":
        await clear_pending(user_id)
        # The legacy one-click plan no longer activates an unverified template.
        # Route to the constraint-driven planner and show missing information.
        try:
            await planning.generate_candidates(DB, user_id, "workout")
            await render_candidate_list(query, user_id, "workout")
        except planning.PlanningBlockedError:
            await render_profile_snapshot(query, user_id)
        return True

    if data.startswith("plan:set:"):
        frequency = int(data.split(":")[2])
        await clear_pending(user_id)
        await user_model.set_fact(
            DB,
            user_id,
            "training_days_per_week",
            frequency,
            kind=user_model.KIND_FACT,
            source=user_model.SOURCE_USER,
            confirmed=True,
            affects=("workout_plan", "weekly_plan"),
        )
        try:
            await planning.generate_candidates(DB, user_id, "workout")
            await render_candidate_list(query, user_id, "workout")
        except planning.PlanningBlockedError:
            await render_profile_snapshot(query, user_id)
        return True
    return False
