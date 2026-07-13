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

RUNTIME_NAMES = ('APP_VERSION', 'Any', 'CALLBACK_DEBOUNCE_SECONDS', 'CONFIRM_PENDING', 'ContextTypes', 'DB', 'EXERCISE_MUSCLES', 'Exception', 'GOAL_STATUS_PROPOSED', 'GOAL_STATUS_PROVISIONAL', 'InlineKeyboardButton', 'InlineKeyboardMarkup', 'KeyError', 'LOGGER', 'MealAnalysis', 'PENDING_QUESTION', 'Path', 'RIR_UNKNOWN', 'SESSION_SCOPED_ACTIONS', 'SETTINGS', 'TypeError', 'Update', 'ValueError', 'WebAppInfo', '_DEBOUNCE_PREFIXES', '_LAST_CALLBACK', '_StaleSetStep', '_duration_s', '_home_hint', '_is_duplicate_tap', '_plan_type_label', '_started', 'action', 'activate_goal_version_provisional', 'active_flow', 'active_session', 'actual_reps', 'actual_rir', 'actual_weight', 'aiosqlite', 'alt', 'alt_muscle', 'alternative', 'alternative_index', 'analysis', 'analyze_duplicate_candidate', 'apply_reconcile_proposal', 'approval_id', 'bool', 'build_daily_status', 'build_evening_summary_text', 'build_health_status_text', 'build_morning_briefing_text', 'build_morning_menu_text', 'build_next_meal_text', 'build_now_action_text', 'build_weekly_summary_text', 'button', 'buttons', 'callback_flow_id', 'callback_version', 'cancel_rest_timer', 'candidate', 'center', 'changed', 'check_duplicate_meal', 'choices', 'chosen_reps', 'chosen_weight', 'claimed', 'clear_confirm_pending', 'clear_meal_fix', 'clear_pending', 'clear_split_state', 'code', 'command_profile_query', 'completed', 'conn', 'connection', 'constraint_id', 'context', 'conversation', 'create_approval', 'create_goal_version', 'create_meal_edit_approval', 'cur', 'current', 'cursor', 'cutoff', 'data', 'datetime', 'decide_approval', 'deleted', 'delta', 'delta_text', 'dict', 'done', 'draft', 'dup', 'duplicate_approval_id', 'edit_approval_id', 'ensure_user', 'enumerate', 'error_id', 'esc', 'event_log', 'ex', 'exc', 'exercise_index', 'exercise_index_text', 'exercise_picker_keyboard', 'existing', 'extra', 'extra_seconds', 'fetch_approval', 'fetch_goal', 'field', 'final_rir', 'first_reps', 'first_weight', 'flags', 'float', 'frequency', 'friendly_error', 'get_daily_flags', 'get_meal_fix', 'get_split_state', 'get_user_plan', 'getattr', 'goal', 'goal_id', 'gv_id', 'handle_checkin_callback', 'handle_flags_callback', 'handle_goal_callback', 'handle_meal_callback', 'handle_menu_callback', 'handle_onboarding_callback', 'handle_plan_callback', 'handle_session_action_callback', 'handle_workout_setup_callback', 'home_keyboard', 'home_keyboard_for_user', 'index', 'int', 'is_allowed', 'is_current_session_step', 'is_partial', 'is_provisional', 'isinstance', 'item', 'item_index', 'item_index_text', 'job', 'jobs', 'json', 'k', 'kb', 'key', 'kind', 'label', 'labels', 'last', 'len', 'level', 'list', 'logged_sets', 'max', 'meal', 'meal_id', 'meal_id_text', 'min', 'mini_app_url', 'missing', 'missing_labels', 'more_keyboard', 'msg', 'new_grams', 'new_max', 'new_min', 'new_val', 'new_weight', 'note', 'notify_admin', 'now', 'nutrition', 'object', 'ok', 'old_grams', 'option', 'option_index', 'pain_location', 'part', 'parts', 'parts_v2', 'payload', 'persist_meal', 'plan', 'plan_id', 'plan_now', 'plan_type', 'planned_sets', 'planning', 'plans_keyboard', 'progress', 'quality', 'query', 'range', 'ratio', 'rc', 'readiness', 'recommend_load', 'refreshed', 'render_candidate_list', 'render_exercise_params', 'render_meal', 'render_profile_snapshot', 'render_quantity_editor', 'render_smart_plan_hub', 'render_unified_plan', 'render_workout_overview', 'reopened', 'replacement', 'reps', 'reps_value', 'rest', 'rest_job_name', 'result', 'round', 'route_decision', 'row', 'rows', 'safe_edit', 'save_medical_constraint', 'save_split_set', 'second_base', 'second_reps', 'second_weight', 'secrets', 'select_todays_workout_code', 'selected', 'send_weight_chart', 'session', 'session_action_arg', 'session_action_data', 'session_id', 'set', 'set_daily_flags', 'set_exercise_override', 'set_goal_weight', 'set_meal_fix', 'set_pending', 'set_split_state', 'severity', 'show_session', 'split_reps_keyboard', 'split_rir_keyboard', 'split_state', 'split_summary_line', 'split_weight_keyboard', 'start_rest_timer', 'status', 'status_line', 'step', 'str', 'sum', 'summary_line', 'suppress', 't', 'tail', 'target_change_note', 'text', 'time', 'total_reps', 'track_event', 'training_intelligence', 'try_save_set', 'tuple', 'undo_last_set', 'undone', 'update', 'update_rest_message', 'update_session_step', 'url', 'user_choice', 'user_id', 'user_model', 'utc_now', 'value', 'value_text', 'warn', 'weight', 'workout', 'workout_summary', 'write_audit')

@runtime_bound(RUNTIME_NAMES)
async def handle_goal_callback(query: Any, user_id: int, data: str) -> bool:
    """Handle goal approve/reject callbacks.

    A provisional goal (missing mandatory data) is kept as an explicit
    temporary goal rather than promoted to a strong active goal. Returns
    True when *data* was handled.
    """
    if data.startswith("approve_goal:"):
        approval_id = data.split(":", 1)[1]
        row = await fetch_approval(user_id, approval_id)
        if not row:
            return True
        payload = row["data"]
        is_provisional = bool(payload.get("provisional"))
        gv_id = await create_goal_version(
            user_id,
            int(payload["calories"]),
            int(payload["protein"]),
            int(payload["steps"]),
            payload["phase"],
            status=(GOAL_STATUS_PROVISIONAL if is_provisional else GOAL_STATUS_PROPOSED),
            source=("user_approved_provisional" if is_provisional else "user_approved"),
            explanation=str(payload.get("explanation") or "אושר על ידי המשתמש"),
        )
        if is_provisional:
            # A goal with missing mandatory data is kept as an explicit temporary
            # goal — it is NOT promoted to a full active goal that drives strong
            # alerts (P0). The user is told what to complete.
            await activate_goal_version_provisional(user_id, gv_id)
            missing = await planning.missing_goal_inputs(DB, user_id)
            await decide_approval(approval_id, "approved")
            await write_audit(user_id, "approve_provisional", "goal", gv_id, **payload)
            # A provisional goal is a real active goal for planning purposes
            # (planning.active_goal includes 'active_provisional'), so a request
            # that was blocked on "no active goal" can continue automatically.
            from noam_coach.bot.callback_plans import resume_pending_plan_action

            if await resume_pending_plan_action(query, user_id):
                return True
            await safe_edit(
                query,
                "סימנתי יעד <b>זמני</b> ⏳ אשתמש בו בזהירות ולא אתבסס עליו "
                "להתראות חזקות.\n"
                + (f"כדי לקבל יעד מדויק, השלם: {esc(', '.join(missing))}.\n" if missing else "")
                + "אפשר להשלים נתונים בכל רגע ואז אחשב יעד מאושר.",
                InlineKeyboardMarkup(
                    [
                        [button("👤 השלם נתונים", "planv2:profile")],
                        [button("⬅️ תפריט", "menu:home")],
                    ]
                ),
            )
            return True
        try:
            await planning.activate_goal(DB, user_id, gv_id)
        except planning.GoalNotReady as exc:
            await safe_edit(
                query,
                "עדיין אי אפשר לקבוע יעד פעיל — חסרים נתוני חובה: "
                f"{esc(', '.join(exc.missing))}.\nהשלם אותם ואז אחשב יעד מאושר.",
                InlineKeyboardMarkup([[button("👤 השלם נתונים", "planv2:profile")], [button("⬅️ תפריט", "menu:home")]]),
            )
            return True
        await user_model.set_fact(
            DB,
            user_id,
            "approved_goal",
            {"goal_version_id": gv_id},
            kind=user_model.KIND_FACT,
            source=user_model.SOURCE_USER,
            confirmed=True,
            affects=("calorie_target", "protein_target"),
        )
        await decide_approval(approval_id, "approved")
        await write_audit(user_id, "approve", "goal", gv_id, **payload)
        await event_log.append_event(
            DB,
            user_id,
            "GOAL_ACTIVATED",
            entity="goal",
            entity_id=gv_id,
            source="user",
            properties={k: payload[k] for k in ("calories", "protein", "steps", "phase")},
        )
        from noam_coach.bot.callback_plans import resume_pending_plan_action

        if await resume_pending_plan_action(query, user_id):
            return True
        await safe_edit(query, "היעד נשמר כיעד הפעיל היחיד ✅", home_keyboard())
        return True

    if data.startswith("reject_goal:"):
        approval_id = data.split(":", 1)[1]
        await decide_approval(approval_id, "rejected")
        await safe_edit(query, "היעד לא שונה.", home_keyboard())
        return True
    return False


async def _invalidate_nutrition_snapshot(user_id: int) -> None:
    """Clear per-day next-meal cache so a new goal recomputes everything."""
    from noam_coach.services.next_meal import invalidate_daily_nutrition_cache

    await invalidate_daily_nutrition_cache(DB, user_id)


async def _render_next_meal_screen(
    query: Any,
    user_id: int,
    *,
    prefix: str = "",
    recommendation: Any | None = None,
) -> None:
    from noam_coach.services.next_meal import (
        format_next_meal_recommendation,
        generate_next_meal_recommendation,
        next_meal_action_rows,
        record_next_meal_served,
        remember_active_recommendation,
    )

    recommendation = recommendation or await generate_next_meal_recommendation(DB, user_id)
    # TASK-03: next_meal_action_rows already ends with a "חזור לסיכום היום"
    # button — no extra status/home row needed on top of the 4-button cap.
    keyboard_rows = [
        [button(label, callback_data) for label, callback_data in row]
        for row in next_meal_action_rows(recommendation)
    ]
    text = format_next_meal_recommendation(recommendation)
    if prefix:
        text = f"{prefix}\n\n{text}"
    await safe_edit(query, text, InlineKeyboardMarkup(keyboard_rows))
    await record_next_meal_served(DB, user_id, recommendation)
    message_id = getattr(getattr(query, "message", None), "message_id", None)
    await remember_active_recommendation(DB, user_id, recommendation, message_id=message_id)


@runtime_bound(RUNTIME_NAMES)
async def handle_menu_callback(query: Any, user_id: int, data: str) -> bool:
    """Handle home/menu navigation and inline confirmations.

    Covers the now/home/more/about/status/morning/nextmeal/evening/profile
    menu screens plus reconcile accept/decline and confirm:* (manual goal,
    weight, goal-weight). Returns True when *data* was handled.
    """
    if data == "menu:now":
        await safe_edit(
            query,
            await build_now_action_text(user_id),
            InlineKeyboardMarkup(
                [
                    [
                        button("🍽️ מה לאכול עכשיו", "menu:nextmeal"),
                        button("🏋️ אימון", "menu:workout"),
                    ],
                    [button("⬅️ תפריט", "menu:home")],
                ]
            ),
        )
        return True

    if data.startswith("nextmeal:wkt:"):
        from noam_coach.services.next_meal import save_next_meal_workout_status

        status = data.rsplit(":", 1)[1]
        status_map = {
            "later": "later",
            "during": "during",
            "done": "completed",
            "cancel": "cancelled",
        }
        if status not in status_map:
            return True
        await save_next_meal_workout_status(DB, user_id, status_map[status])
        await _render_next_meal_screen(query, user_id, prefix="עדכנתי את מצב האימון ורעננתי את ההמלצה.")
        return True

    if data.startswith("nextmeal:dislike:"):
        from noam_coach.services.next_meal import save_next_meal_option_feedback

        try:
            option_number = int(data.rsplit(":", 1)[1])
            disliked_item, recommendation = await save_next_meal_option_feedback(DB, user_id, option_number)
        except (TypeError, ValueError):
            await _render_next_meal_screen(query, user_id, prefix="לא מצאתי את האפשרות הזו, אז רעננתי את ההמלצה.")
            return True
        await _render_next_meal_screen(
            query,
            user_id,
            prefix=(
                f"רשמתי שלא מתאים לך עכשיו {esc(disliked_item)} (דחייה זמנית, לא העדפה קבועה) "
                "ורעננתי את ההמלצה."
            ),
            recommendation=recommendation,
        )
        return True

    if data == "nextmeal:refresh":
        await _render_next_meal_screen(
            query,
            user_id,
            prefix="רעננתי את ההצעה. אפשר לאשר, לשנות כמויות, או לבקש רענון נוסף.",
        )
        return True

    if data.startswith(("nextmeal:smaller:", "nextmeal:bigger:")):
        from noam_coach.services.next_meal import regenerate_with_size

        smaller = data.startswith("nextmeal:smaller:")
        try:
            option_number = int(data.rsplit(":", 1)[1])
        except (TypeError, ValueError):
            option_number = 1
        recommendation = await regenerate_with_size(DB, user_id, option_number, smaller=smaller)
        prefix = "הקטנתי את ההצעה." if smaller else "הגדלתי מעט את ההצעה — שים לב להשפעה על סוף היום."
        await _render_next_meal_screen(query, user_id, prefix=prefix, recommendation=recommendation)
        return True

    if data.startswith("nextmeal:nostock:"):
        from noam_coach.services.next_meal import save_next_meal_unavailable_item

        try:
            option_number = int(data.rsplit(":", 1)[1])
            item, recommendation = await save_next_meal_unavailable_item(DB, user_id, option_number)
        except (TypeError, ValueError):
            await _render_next_meal_screen(query, user_id, prefix="רעננתי את ההמלצה.")
            return True
        await _render_next_meal_screen(
            query, user_id,
            prefix=f"סימנתי שחסר לך כרגע {esc(item)} (זמני) והחלפתי את ההצעה.",
            recommendation=recommendation,
        )
        return True

    if data.startswith("nextmeal:dislikeitem:"):
        from noam_coach.services.food_preferences import record_food_preference_from_slots
        from noam_coach.services.next_meal import generate_next_meal_recommendation

        try:
            option_number = int(data.rsplit(":", 1)[1])
            current = await generate_next_meal_recommendation(DB, user_id)
            title = current.options[option_number - 1].title if 0 < option_number <= len(current.options) else ""
        except (TypeError, ValueError, IndexError):
            title = ""
        if title:
            await record_food_preference_from_slots(
                DB, user_id,
                {"kind": "preference", "polarity": "avoid", "item": title, "note": title},
                title,
            )
        await _render_next_meal_screen(
            query, user_id,
            prefix="שמרתי את ההעדפה הקבועה והחלפתי את ההצעה." if title else "רעננתי את ההמלצה.",
        )
        return True

    if data.startswith("nextmeal:choose:"):
        from noam_coach.services.next_meal import (
            generate_next_meal_recommendation,
            get_active_recommendation_options,
            mark_active_recommendation_selection,
        )

        try:
            option_number = int(data.rsplit(":", 1)[1])
            recommendation = await generate_next_meal_recommendation(DB, user_id)
            active_options = await get_active_recommendation_options(DB, user_id)
            option = (active_options or recommendation.options)[option_number - 1]
        except (TypeError, ValueError, IndexError):
            await _render_next_meal_screen(query, user_id, prefix="לא מצאתי את האפשרות. הנה שוב ההמלצה.")
            return True
        # Choosing does NOT log the meal as eaten — only "save as meal" does.
        nutrition = recommendation.context.nutrition
        after_cal = (nutrition.calorie_balance - option.calories) if nutrition.calorie_balance is not None else None
        impact = (
            f"\nאחרי הארוחה יישארו לך כ-{after_cal} קלוריות להיום." if after_cal is not None else ""
        )
        await mark_active_recommendation_selection(DB, user_id, option_number)
        await safe_edit(
            query,
            (
                f"<b>{esc(option.title)}</b>\n"
                f"{esc(', '.join(option.ingredients))}\n"
                f"כ-{option.calories} קל׳ | כ-{option.protein} גרם חלבון{impact}\n\n"
                "רוצה לשנות משהו לפני השמירה?\n"
                "אפשר לכתוב חופשי, למשל:\n"
                "\"בלי טורטיה\"\n"
                "\"קוטג׳ 100 גרם\"\n"
                "\"יותר גדול\"\n"
                "\"אין לי ביצים\"\n\n"
                "רק אחרי אישור מפורש אשמור את זה כארוחה."
            ),
            InlineKeyboardMarkup([
                [button("✅ אשר שאכלתי", f"nextmeal:save:{option_number}")],
                [button("🔄 רענן הצעה", "nextmeal:refresh"), button("✏️ שנה כמויות", f"nextmeal:editqty:{option_number}")],
                [button("📊 חזור לסיכום היום", "menu:status")],
            ]),
        )
        return True

    if data.startswith("nextmeal:plan:"):
        from noam_coach.services.next_meal import (
            clear_active_recommendation,
            generate_next_meal_recommendation,
            get_active_recommendation_options,
            plan_chosen_meal,
        )

        try:
            option_number = int(data.rsplit(":", 1)[1])
            recommendation = await generate_next_meal_recommendation(DB, user_id)
            active_options = await get_active_recommendation_options(DB, user_id)
            option = (active_options or recommendation.options)[option_number - 1]
        except (TypeError, ValueError, IndexError):
            await safe_edit(query, "לא מצאתי את האפשרות לתכנון.", home_keyboard())
            return True
        planned = await plan_chosen_meal(DB, user_id, option)
        await clear_active_recommendation(DB, user_id)
        note = (
            "כבר תכננתי את זה להמשך היום." if not planned
            else f"תכננתי את {esc(option.title)} להמשך היום 📅\nזה עדיין לא נספר כארוחה שאכלת."
        )
        await safe_edit(
            query,
            note,
            InlineKeyboardMarkup([[button("📊 מצב היום", "menu:status"), button("🏠 תפריט", "menu:home")]]),
        )
        return True

    if data.startswith("nextmeal:save:"):
        from noam_coach.services.next_meal import (
            clear_active_recommendation,
            generate_next_meal_recommendation,
            get_active_recommendation_options,
            save_chosen_meal,
        )

        try:
            option_number = int(data.rsplit(":", 1)[1])
            recommendation = await generate_next_meal_recommendation(DB, user_id)
            active_options = await get_active_recommendation_options(DB, user_id)
            option = (active_options or recommendation.options)[option_number - 1]
        except (TypeError, ValueError, IndexError):
            await safe_edit(query, "לא מצאתי את האפשרות לשמירה.", home_keyboard())
            return True
        saved = await save_chosen_meal(DB, user_id, option)
        await clear_active_recommendation(DB, user_id)
        if not saved:
            await safe_edit(query, "כבר שמרתי את הארוחה הזו — לא כפלתי אותה.", home_keyboard())
            return True
        await safe_edit(
            query,
            f"שמרתי את {esc(option.title)} כארוחה ✅\nמצב היום עודכן.",
            InlineKeyboardMarkup([[button("📊 מצב היום", "menu:status"), button("🏠 תפריט", "menu:home")]]),
        )
        return True

    if data.startswith("dailymenu:save:"):
        # Finding 9: this is the ONLY confirm-ate path for a daily-menu meal.
        # It must never fall through to next-meal recommendation state — the
        # daily menu and "what should I eat now" are different state domains
        # (a user may have an active next-meal recommendation from an earlier
        # tap that has nothing to do with the daily-menu meal they are
        # confirming right now).
        from noam_coach.services.daily_menu_state import get_active_daily_menu, is_structured_menu, structured_meals
        from noam_coach.services.next_meal import MealIngredient, MealOption, save_chosen_meal

        parts = data.split(":", 3)
        menu_id = parts[2] if len(parts) > 2 else ""
        meal_id = parts[3] if len(parts) > 3 else ""
        active_menu = await get_active_daily_menu(DB, user_id)
        if not is_structured_menu(active_menu) or str((active_menu or {}).get("menu_id") or "") != menu_id:
            await safe_edit(
                query,
                "התפריט התעדכן מאז שהוצג לך — פתח את תפריט היום המעודכן ונסה שוב.",
                InlineKeyboardMarkup([[button("📋 תפריט היום", "menu:daily_menu"), button("🏠 תפריט", "menu:home")]]),
            )
            return True
        meal = next(
            (m for m in structured_meals(active_menu) if str(m.get("meal_id") or "") == meal_id),
            None,
        )
        if meal is None:
            await safe_edit(query, "לא מצאתי את הארוחה הזו בתפריט הפעיל.", home_keyboard())
            return True
        ingredients = meal.get("ingredients") or []
        ingredient_details = [
            MealIngredient(
                food_id=str(item.get("name") or "item"),
                display_name=str(item.get("name") or ""),
                # Only claim a real gram quantity when one was actually
                # recorded; otherwise use a neutral "1 מנה" instead of the
                # misleading "1.0 יחידה" default, which used to make e.g.
                # "אורז — 220 קלוריות" read back as "1 יחידה אורז" in history.
                quantity=float(item["grams"]) if item.get("grams") else 1.0,
                unit="גרם" if item.get("grams") else "מנה",
                calories=float(item.get("calories") or 0),
                protein_g=float(item.get("protein") or 0),
                carbs_g=float(item["carbs"]) if item.get("carbs") is not None else None,
                fat_g=float(item["fat"]) if item.get("fat") is not None else None,
            )
            for item in ingredients
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        ]
        # Meal identity fix: the saved meal's NAME must be the actual food
        # composition (what was eaten), not the behavioral slot label — a
        # meal saved as "ארוחת בוקר" is useless to learned_foods/repetition/
        # routine analysis, which key off the meal name. role stays as
        # metadata elsewhere (the structured menu record); only the
        # composition reaches meals.name.
        meal_note = str(meal.get("note") or "").strip()
        meal_role = str(meal.get("role") or "ארוחה").strip()
        display_title = meal_note or meal_role
        option = MealOption(
            title=display_title,
            ingredients=[meal_note or meal_role] if not ingredient_details else [],
            calories=int(float(meal.get("calories") or 0)),
            protein=int(float(meal.get("protein") or 0)),
            rationale="מהתפריט היומי הפעיל",
            ingredient_details=ingredient_details,
        )
        saved = await save_chosen_meal(DB, user_id, option)
        if not saved:
            await safe_edit(query, "כבר שמרתי את הארוחה הזו — לא כפלתי אותה.", home_keyboard())
            return True
        await safe_edit(
            query,
            f"שמרתי את {esc(option.title)} כארוחה ✅\nמצב היום עודכן.",
            InlineKeyboardMarkup([[button("📊 מצב היום", "menu:status"), button("🏠 תפריט", "menu:home")]]),
        )
        return True

    if data.startswith("nextmeal:qty:"):
        from noam_coach.services.next_meal import adjust_next_meal_quantity

        try:
            _, _, option_text, scale_text = data.split(":", 3)
            option_number = int(option_text)
            scale = float(scale_text)
            recommendation = await adjust_next_meal_quantity(DB, user_id, option_number, scale)
        except (TypeError, ValueError):
            await _render_next_meal_screen(query, user_id, prefix="לא הצלחתי לעדכן את הכמות. רעננתי את ההמלצה.")
            return True
        prefix = "עדכנתי כמויות וחישבתי מחדש את הקלוריות והחלבון."
        await _render_next_meal_screen(query, user_id, prefix=prefix, recommendation=recommendation)
        return True

    if data == "nextmeal:why":
        from noam_coach.services.next_meal import (
            format_next_meal_explanation,
            generate_next_meal_recommendation,
        )

        recommendation = await generate_next_meal_recommendation(DB, user_id)
        await safe_edit(
            query,
            format_next_meal_explanation(recommendation),
            InlineKeyboardMarkup([[button("⬅️ חזרה להמלצה", "menu:nextmeal")]]),
        )
        return True

    if data.startswith("nextmeal:editqty:"):
        from noam_coach.services.next_meal import generate_next_meal_recommendation, get_active_recommendation_options

        try:
            option_number = int(data.rsplit(":", 1)[1])
            recommendation = await generate_next_meal_recommendation(DB, user_id)
            active_options = await get_active_recommendation_options(DB, user_id)
            option = (active_options or recommendation.options)[option_number - 1]
        except (TypeError, ValueError, IndexError):
            await _render_next_meal_screen(query, user_id, prefix="לא מצאתי את האפשרות הזו. רעננתי את ההמלצה.")
            return True
        rows = [
            [
                button("➖ 20%", f"nextmeal:qty:{option_number}:0.8"),
                button("➕ 20%", f"nextmeal:qty:{option_number}:1.2"),
            ],
            [button("💾 בחר ושמור", f"nextmeal:choose:{option_number}")],
            [button("⬅️ חזרה להמלצה", "menu:nextmeal")],
        ]
        await safe_edit(
            query,
            (
                f"<b>עריכת כמויות: {esc(option.title)}</b>\n"
                f"{esc(', '.join(option.ingredients))}\n"
                f"כ-{option.calories} קל׳ | כ-{option.protein} גרם חלבון\n\n"
                "בחר שינוי כמות. הערכים יחושבו מחדש מהמרכיבים לפני בחירה או שמירה."
            ),
            InlineKeyboardMarkup(rows),
        )
        return True

    if data.startswith("reconcile_ok:"):
        action = data.split(":", 1)[1]
        msg = await apply_reconcile_proposal(user_id, action)
        await track_event(user_id, "reconcile_accepted", action=action)
        await safe_edit(query, f"✅ {msg}", home_keyboard())
        return True

    if data.startswith("reconcile_no:"):
        await track_event(user_id, "reconcile_declined", key=data.split(":", 1)[1])
        await safe_edit(query, "בסדר גמור, לא אשנה כלום.", home_keyboard())
        return True

    if data == "health:activate":
        from noam_coach.services.health_jobs import activate_imported_health_facts

        activated = await activate_imported_health_facts(user_id)
        await track_event(user_id, "health_facts_activated", count=activated)
        if activated:
            note = (
                f"✅ הפעלתי {activated} נתונים מ-Apple Health.\n"
                "מעכשיו הם משפיעים על התוכנית וההמלצות שלך."
            )
        else:
            note = "לא נשארו נתונים שממתינים להפעלה — הכול כבר פעיל."
        await safe_edit(query, note, home_keyboard())
        return True

    if data == "health:review":
        from noam_coach.services.health_jobs import pending_import_facts

        pending = await pending_import_facts(user_id)
        if not pending:
            await safe_edit(query, "אין כרגע נתונים שממתינים לתיקון.", home_keyboard())
            return True
        lines = ["<b>נתונים שזוהו וממתינים לאישור</b>", ""]
        for row in pending[:8]:
            label = user_model.display_label(str(row["key"]))
            source = user_model.SOURCE_LABELS.get(row.get("source"), row.get("source") or "")
            lines.append(f"• {esc(label)}" + (f" ({esc(source)})" if source else ""))
        lines.append("")
        lines.append("אפשר לתקן כל פרט דרך הפרופיל, או להפעיל את מה שזוהה.")
        await safe_edit(
            query,
            "\n".join(lines),
            InlineKeyboardMarkup([
                [button("✅ הפעל את מה שזוהה", "health:activate")],
                [button("👤 פרופיל", "menu:profile"), button("🏠 תפריט", "menu:home")],
            ]),
        )
        return True

    if data.startswith("health:confirm:") and ":trend:" in data:
        # RE11: user picked one of the trend-proposal buttons (stay at N /
        # go to M) for a Health-derived metric instead of accepting the raw
        # detected value or typing a correction. RE12: this only settles the
        # FREQUENCY sub-step — the wizard continues to the remaining
        # confirmations (training days, typical hour) instead of swallowing
        # them with the whole pattern.
        from noam_coach.services.health_jobs import (
            apply_health_wizard_trend_choice,
            ask_next_health_confirm_step,
            finish_health_confirm_wizard,
        )

        _, _, key, _, value_text = data.split(":", 4)
        try:
            trend_value = float(value_text)
        except ValueError:
            await safe_answer_callback(query, "ערך לא תקין", show_alert=True)
            return True
        ack = await apply_health_wizard_trend_choice(user_id, key, trend_value)
        await track_event(user_id, "health_wizard_trend_choice", key=key, value=trend_value)
        if not await ask_next_health_confirm_step(query, user_id, ack_text=ack):
            await finish_health_confirm_wizard(query, user_id, ack_text=ack)
        return True

    if data.startswith("health:confirm:"):
        from noam_coach.services.health_jobs import (
            ask_next_health_confirm_step,
            confirm_health_wizard_step,
            finish_health_confirm_wizard,
        )

        step_id = data.split(":", 2)[2]
        ack = await confirm_health_wizard_step(user_id, step_id)
        await track_event(user_id, "health_wizard_fact_confirmed", key=step_id)
        if not await ask_next_health_confirm_step(query, user_id, ack_text=ack):
            await finish_health_confirm_wizard(query, user_id, ack_text=ack)
        return True

    if data.startswith("health:edit:"):
        from noam_coach.services.health_jobs import prompt_health_wizard_edit

        step_id = data.split(":", 2)[2]
        await prompt_health_wizard_edit(query, user_id, step_id)
        return True

    if data == "health:steps_breakdown":
        from noam_coach.services.health_jobs import show_steps_daily_breakdown

        await show_steps_daily_breakdown(query, user_id)
        return True

    if data == "health:skip_item":
        from noam_coach.services.health_jobs import (
            HEALTH_CONFIRM_FLOW,
            ask_next_health_confirm_step,
            finish_health_confirm_wizard,
            skip_health_wizard_item,
        )
        from noam_coach.bot.onboarding import get_flow_state, clear_pending

        state = await get_flow_state(user_id, HEALTH_CONFIRM_FLOW)
        if state and state.get("step"):
            await skip_health_wizard_item(user_id, str(state["step"]))
        await clear_pending(user_id)
        if not await ask_next_health_confirm_step(query, user_id):
            await finish_health_confirm_wizard(query, user_id)
        return True

    if data == "health:skip_wizard":
        from noam_coach.services.health_jobs import finish_health_confirm_wizard
        from noam_coach.bot.onboarding import clear_pending

        await clear_pending(user_id)
        await track_event(user_id, "health_wizard_skipped")
        await finish_health_confirm_wizard(query, user_id)
        return True

    if data.startswith("confirm:"):
        _, kind, value_text = data.split(":", 2)
        await clear_confirm_pending(user_id)
        if kind == "cancel":
            await safe_edit(query, "בוטל. שום דבר לא שונה.", home_keyboard())
            return True
        value = float(value_text)
        if kind == "goal_cal":
            # Manual changes are still versioned goals; never create a second
            # source of truth in user_facts or the legacy goals table.
            current = await fetch_goal(user_id)
            async with DB.transaction() as conn:
                cursor = await conn.execute(
                    """
                    INSERT INTO goal_versions(
                        user_id, calories, protein, steps, phase, status, source,
                        explanation, created_at
                    ) VALUES(?, ?, ?, ?, ?, 'proposed', 'manual', ?, ?)
                    """,
                    (
                        user_id,
                        int(value),
                        int(current.get("protein") or SETTINGS.default_protein),
                        int(current.get("steps") or SETTINGS.default_steps),
                        str(current.get("phase") or "fat_loss_muscle_retention"),
                        "שינוי ידני שאושר על ידי המשתמש",
                        utc_now(),
                    ),
                )
                goal_id = int(cursor.lastrowid or 0)
            try:
                await planning.activate_goal(DB, user_id, goal_id)
            except planning.GoalNotReady as exc:
                await safe_edit(
                    query,
                    "כדי לקבוע יעד צריך עוד נתוני חובה: "
                    f"{esc(', '.join(exc.missing))}.",
                    home_keyboard(),
                )
                return True
            await event_log.append_event(
                DB,
                user_id,
                "GOAL_MANUALLY_CHANGED",
                entity="goal",
                entity_id=goal_id,
                properties={"calories": int(value)},
                source="user",
            )
            # re7 P0-5: a new active goal invalidates any per-day nutrition
            # snapshot/cache so every screen recomputes from the new target.
            await event_log.append_event(
                DB, user_id, "goal_change_confirmed",
                entity="goal", entity_id=goal_id, source="user",
                properties={"calories": int(value), "previous": int(current.get("calories") or 0)},
            )
            await _invalidate_nutrition_snapshot(user_id)
            await event_log.append_event(
                DB, user_id, "goal_snapshot_invalidated",
                entity="goal", entity_id=goal_id, source="system",
            )
            await safe_edit(
                query,
                f"עודכן יעד הקלוריות ל-{int(value):,} ✅\nהיעד הקודם נשמר בהיסטוריה.",
                home_keyboard(),
            )
        elif kind == "weight":
            await user_model.set_fact(
                DB,
                user_id,
                "weight_kg",
                value,
                kind=user_model.KIND_FACT,
                source=user_model.SOURCE_USER,
                confirmed=True,
            )
            note = await target_change_note(user_id, None)
            await safe_edit(query, f"עודכן משקל ל-{value:g} ✅\n{note}", home_keyboard())
        elif kind == "goal_weight":
            await set_goal_weight(user_id, value)
            await safe_edit(query, f'רשמתי יעד משקל של {value:g} ק"ג ✅', home_keyboard())
        return True

    if data in ("menu:home", "menu:more"):
        # RE14: one single menu. "menu:more" is a legacy callback from old
        # messages — it renders the same unified home menu.
        await conversation.clear_all_flows(DB, user_id)
        PENDING_QUESTION.pop(user_id, None)
        CONFIRM_PENDING.pop(user_id, None)
        # REC-PLAN-MEAL-03-16: keyboard must include a button whose callback
        # matches the next_best_action callback surfaced in the hint text.
        await safe_edit(
            query,
            "<b>המאמן האישי שלך</b>\n\n" + await _home_hint(user_id),
            await home_keyboard_for_user(user_id),
        )
        return True

    if data == "menu:settings":
        # TASK-6: the secondary/system actions moved off the primary home menu.
        from noam_coach.bot.ui import settings_keyboard

        await safe_edit(
            query,
            "<b>⚙️ הגדרות ועוד</b>\n\nפרופיל, יעד, תפריט להיום, שבועי, גרפים ונתוני בריאות.",
            settings_keyboard(),
        )
        return True

    if data == "menu:about":
        await safe_edit(
            query,
            (
                "<b>אודות הבוט</b>\n\n"
                "מאמן אישי לתזונה, אימונים ובריאות. גרסת בטא.\n\n"
                "⚠️ <b>גילוי נאות</b>\n"
                "• הערכות מזון מתמונה הן אומדן, לא מדידה מעבדתית.\n"
                "• המלצות אימון וכאב אינן תחליף לרופא או פיזיותרפיסט.\n"
                "• נתוני הבריאות אינם משמשים לאבחון.\n\n"
                "<b>פרטיות</b>\n"
                "המידע נשמר באופן מקומי. אפשר לייצא או למחוק את הנתונים שלך — "
                "פנה למפעיל הבוט.\n\n"
                f"גרסה: {esc(APP_VERSION)}"
            ),
            InlineKeyboardMarkup([[button("⬅️ תפריט", "menu:home")]]),
        )
        return True

    if data == "menu:food":
        # TASK-21: offer the two clearly-separate logging methods.
        await safe_edit(
            query,
            "איך תרצה לרשום את הארוחה?",
            InlineKeyboardMarkup([
                [button("📷 צילום ארוחה", "menu:food_photo")],
                [button("✍️ הוספת אוכל בטקסט", "menu:food_text")],
                [button("⬅️ תפריט", "menu:home")],
            ]),
        )
        return True

    if data == "menu:food_photo":
        await safe_edit(
            query,
            "שלח תמונת אוכל ואנתח את הארוחה לפני שמירה.",
            InlineKeyboardMarkup([[button("⬅️ תפריט", "menu:home")]]),
        )
        return True

    if data == "menu:food_text":
        # TASK-21: start the dedicated manual food-entry flow. The next free-text
        # message is analyzed through the normal meal pipeline.
        from noam_coach.bot.onboarding import set_pending

        await set_pending(user_id, "__manual_meal__")
        await safe_edit(
            query,
            "✍️ כתוב מה אכלת בשפה חופשית, ואערוך הערכה לפני שמירה.\n\n"
            "דוגמאות:\n"
            "• 3 קציצות, קצת אורז וסלט\n"
            "• טוסט עם גבינה וקפה\n"
            "• יוגורט חלבון ובננה\n\n"
            "לא צריך לדעת כמויות מדויקות או קלוריות — פשוט מה שאכלת.",
            InlineKeyboardMarkup([[button("⬅️ תפריט", "menu:home")]]),
        )
        return True

    if data == "menu:status":
        await safe_edit(
            query,
            await build_daily_status(user_id),
            InlineKeyboardMarkup([[button("⬅️ תפריט", "menu:home")]]),
        )
        return True

    if data == "menu:morning":
        # TASK-16: "☀️ עדכון בוקר" is a SHORT current-day briefing — not the
        # full pinnable daily menu (that lives on menu:daily_menu / menu:today).
        # It never re-sends the active menu and ends with focused next actions.
        await safe_edit(query, "רגע, מכין לך… ⏳", None)
        _ctx = None
        with suppress(Exception):
            from noam_coach.jobs.proactive import build_daily_context

            _ctx = await build_daily_context(user_id)
        try:
            text = await build_morning_briefing_text(user_id, _ctx)
        except Exception as exc:  # noqa: BLE001
            text = friendly_error(exc, "morning briefing")
        rows = [[button("🍽 מה לאכול עכשיו", "menu:nextmeal"), button("📊 מצב היום", "menu:status")]]
        ctx_workout = bool(
            _ctx is not None
            and (_ctx.is_usual_workout_day or _ctx.workout_active or _ctx.workout_completed)
        )
        if ctx_workout:
            rows.append([button("🏋️ אימון היום", "menu:workout")])
        rows.append([button("⬅️ תפריט", "menu:home")])
        await safe_edit(query, text, InlineKeyboardMarkup(rows))
        return True

    if data in ("menu:today", "menu:daily_menu", "menu:refresh_daily_menu", "menu:nextmeal", "menu:evening"):
        if data in {"menu:daily_menu", "menu:refresh_daily_menu", "menu:nextmeal"}:
            readiness = await user_model.compute_readiness(DB, user_id, "nutrition")
            needs_goal = await planning.active_goal(DB, user_id) is None
            if not readiness["ready"] or needs_goal:
                missing = list(readiness.get("missing", []))
                if needs_goal and "active_goal" not in missing:
                    missing.append("active_goal")
                from noam_coach.bot.callback_plans import render_prerequisite_completion_prompt

                await render_prerequisite_completion_prompt(
                    query,
                    user_id,
                    callback_data=data,
                    plan_type="nutrition",
                    missing=missing,
                    title="חסרים פרטים כדי להמשיך לתזונה האישית",
                )
                return True
        # "menu:today" is a legacy alias for the full daily menu (old keyboards
        # may still carry it) — it renders the same pinnable daily menu screen
        # as menu:daily_menu.  (menu:morning is now the short briefing above.)
        await safe_edit(query, "רגע, מכין לך… ⏳", None)
        try:
            if data in ("menu:today", "menu:daily_menu"):
                # TASK-11: "show me today's menu" must not silently discard a
                # standing free-text edit ("בלי ביצים היום") by regenerating
                # from scratch — reuse today's active menu (daily_menu_state)
                # when one already exists. Only the explicit
                # "🔄 רענן תפריט" (menu:refresh_daily_menu) action below is
                # allowed to build a genuinely fresh menu.
                from noam_coach.services.daily_menu_state import get_active_daily_menu

                active = await get_active_daily_menu(DB, user_id)
                text = active["text"] if active else await build_morning_menu_text(user_id)
            elif data == "menu:refresh_daily_menu":
                text = await build_morning_menu_text(user_id)
            elif data == "menu:nextmeal":
                await _render_next_meal_screen(query, user_id)
                return True
            else:
                text = await build_evening_summary_text(user_id)
        except Exception as exc:  # noqa: BLE001
            text = friendly_error(exc, "on-demand recommendation")
        if data in ("menu:today", "menu:daily_menu", "menu:refresh_daily_menu"):
            keyboard = InlineKeyboardMarkup([
                [button("🔄 רענן תפריט", "menu:refresh_daily_menu"), button("🍽 מה לאכול עכשיו", "menu:nextmeal")],
                [button("✏️ החלף ארוחה", "menu:replace_daily_meal"), button("📊 מצב היום", "menu:status")],
                [button("⬅️ תפריט", "menu:home")],
            ])
            if data == "menu:daily_menu" and getattr(query, "message", None) is not None:
                # TASK-05: explicit daily-menu tap creates a standalone message
                # the user can pin.  The current menu screen is only acknowledged.
                from noam_coach.services.daily_menu_state import remember_daily_menu_message

                sent = await query.message.reply_text(text, reply_markup=keyboard, parse_mode="HTML")
                await remember_daily_menu_message(
                    DB,
                    user_id,
                    chat_id=getattr(getattr(sent, "chat", None), "id", user_id),
                    message_id=getattr(sent, "message_id", None),
                    source="menu_callback",
                )
                await safe_edit(
                    query,
                    "שלחתי לך את תפריט היום כהודעה עצמאית שאפשר לנעוץ ✅",
                    InlineKeyboardMarkup([[button("📊 מצב היום", "menu:status"), button("⬅️ תפריט", "menu:home")]]),
                )
                return True
        else:
            keyboard = InlineKeyboardMarkup([[button("⬅️ תפריט", "menu:home")]])
        await safe_edit(query, text, keyboard)
        return True

    if data == "menu:replace_daily_meal":
        await safe_edit(
            query,
            "איזו ארוחה בתפריט תרצה להחליף? כתוב למשל: 'תחליף לי את ארוחת הבוקר לחלבון אחר'.\n\nבינתיים אפשר גם לקבל המלצה מיידית שמתחשבת במצב היום.",
            InlineKeyboardMarkup([
                [button("🍽 מה לאכול עכשיו", "menu:nextmeal"), button("🔄 רענן תפריט", "menu:refresh_daily_menu")],
                [button("📊 מצב היום", "menu:status"), button("⬅️ תפריט", "menu:home")],
            ]),
        )
        return True

    if data == "menu:profile":
        await command_profile_query(query, user_id)
        return True
    return False

@runtime_bound(RUNTIME_NAMES)
async def handle_flags_callback(query: Any, user_id: int, data: str) -> bool:
    """Handle the daily check-in flags menu and flag:* toggles.

    Covers opening the flags screen and the normal/fasting/med/pain/done
    flag actions. Returns True when *data* was handled.
    """
    if data == "menu:flags":
        await safe_edit(
            query,
            "<b>איך אתה היום?</b>\n\n"
            "סמן מה רלוונטי ואתאים את ההמלצות:",
            InlineKeyboardMarkup([
                [button("😴 שינה טובה", "flag:sleep:good"), button("😐 שינה סבירה", "flag:sleep:ok"), button("😫 שינה גרועה", "flag:sleep:bad")],
                [button("⚡ אנרגיה גבוהה", "flag:energy:high"), button("🔋 אנרגיה רגילה", "flag:energy:normal"), button("🪫 אנרגיה נמוכה", "flag:energy:low")],
                [button("💊 לקחתי תרופה", "flag:med"), button("🕐 בצום", "flag:fasting"), button("🤕 יש כאב", "flag:pain")],
                [button("✅ הכול רגיל", "flag:normal")],
                [button("⬅️ תפריט", "menu:home")],
            ]),
        )
        return True

    if data.startswith("flag:"):
        parts = data.split(":")
        flags = await get_daily_flags(user_id)
        if data == "flag:normal":
            flags["morning_update"] = True
            await set_daily_flags(user_id, flags)
            await safe_edit(query, "נרשם, יום רגיל. אתאים את ההמלצות 👍", home_keyboard())
        elif data == "flag:fasting":
            flags["fasting"] = True
            flags["morning_update"] = True
            await set_daily_flags(user_id, flags)
            await safe_edit(query, "נרשם יום צום. אתאים את ההמלצות בהתאם 🕐", home_keyboard())
        elif data == "flag:med":
            await set_pending(user_id, "__med_name__")
            await safe_edit(query, "איזו תרופה לקחת? כתוב את השם.", None)
        elif data == "flag:pain":
            await set_pending(user_id, "__pain_location__")
            await save_medical_constraint(
                user_id, kind="pain", note="reported via morning update", affects=("exercise_selection",),
            )
            await safe_edit(query, 'איפה כואב? (למשל "ברך ימין")', None)
        elif parts[1] == "sleep":
            quality = parts[2] if len(parts) > 2 else "ok"
            flags["sleep_quality"] = quality
            flags["morning_update"] = True
            await set_daily_flags(user_id, flags)
            await safe_edit(query, "נרשם. עוד משהו?", InlineKeyboardMarkup([
                [button("⚡ אנרגיה גבוהה", "flag:energy:high"), button("🔋 רגילה", "flag:energy:normal"), button("🪫 נמוכה", "flag:energy:low")],
                [button("💊 תרופה", "flag:med"), button("🕐 צום", "flag:fasting"), button("🤕 כאב", "flag:pain")],
                [button("✅ זהו", "flag:done")],
            ]))
        elif parts[1] == "energy":
            level = parts[2] if len(parts) > 2 else "normal"
            flags["energy"] = level
            flags["morning_update"] = True
            await set_daily_flags(user_id, flags)
            await safe_edit(query, "נרשם, אתאים את ההמלצות בהתאם 👍", home_keyboard())
        elif data == "flag:done":
            await safe_edit(query, "מצוין, אתאים את ההמלצות לפי מה שעדכנת 👍", home_keyboard())
        else:
            await safe_edit(query, "נרשם 👍", home_keyboard())
        return True
    return False
