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

RUNTIME_NAMES = ('APP_VERSION', 'Any', 'CALLBACK_DEBOUNCE_SECONDS', 'CONFIRM_PENDING', 'ContextTypes', 'DB', 'EXERCISE_MUSCLES', 'Exception', 'GOAL_STATUS_PROPOSED', 'GOAL_STATUS_PROVISIONAL', 'InlineKeyboardButton', 'InlineKeyboardMarkup', 'KeyError', 'LOGGER', 'MealAnalysis', 'PENDING_QUESTION', 'Path', 'RIR_UNKNOWN', 'SESSION_SCOPED_ACTIONS', 'SETTINGS', 'TypeError', 'Update', 'ValueError', 'WebAppInfo', '_DEBOUNCE_PREFIXES', '_LAST_CALLBACK', '_StaleSetStep', '_duration_s', '_home_hint', '_is_duplicate_tap', '_plan_type_label', '_started', 'action', 'activate_goal_version_provisional', 'active_flow', 'active_session', 'actual_reps', 'actual_rir', 'actual_weight', 'aiosqlite', 'alt', 'alt_muscle', 'alternative', 'alternative_index', 'analysis', 'analyze_duplicate_candidate', 'apply_reconcile_proposal', 'approval_id', 'bool', 'build_daily_status', 'build_evening_summary_text', 'build_health_status_text', 'build_morning_menu_text', 'build_next_meal_text', 'build_now_action_text', 'build_weekly_summary_text', 'button', 'buttons', 'callback_flow_id', 'callback_version', 'cancel_rest_timer', 'candidate', 'center', 'changed', 'check_duplicate_meal', 'choices', 'chosen_reps', 'chosen_weight', 'claimed', 'clear_confirm_pending', 'clear_meal_fix', 'clear_pending', 'clear_split_state', 'code', 'command_profile_query', 'completed', 'conn', 'connection', 'constraint_id', 'context', 'conversation', 'create_approval', 'create_goal_version', 'create_meal_edit_approval', 'cur', 'current', 'cursor', 'cutoff', 'data', 'datetime', 'decide_approval', 'deleted', 'delta', 'delta_text', 'dict', 'done', 'draft', 'dup', 'duplicate_approval_id', 'edit_approval_id', 'ensure_user', 'enumerate', 'error_id', 'esc', 'event_log', 'ex', 'exc', 'exercise_index', 'exercise_index_text', 'exercise_picker_keyboard', 'existing', 'extra', 'extra_seconds', 'fetch_approval', 'fetch_goal', 'field', 'final_rir', 'first_reps', 'first_weight', 'flags', 'float', 'frequency', 'friendly_error', 'get_daily_flags', 'get_meal_fix', 'get_split_state', 'get_user_plan', 'getattr', 'goal', 'goal_id', 'gv_id', 'handle_checkin_callback', 'handle_flags_callback', 'handle_goal_callback', 'handle_meal_callback', 'handle_menu_callback', 'handle_onboarding_callback', 'handle_plan_callback', 'handle_session_action_callback', 'handle_workout_setup_callback', 'home_keyboard', 'home_keyboard_for_user', 'index', 'int', 'is_allowed', 'is_current_session_step', 'is_partial', 'is_provisional', 'isinstance', 'item', 'item_index', 'item_index_text', 'job', 'jobs', 'json', 'k', 'kb', 'key', 'kind', 'label', 'labels', 'last', 'len', 'level', 'list', 'logged_sets', 'max', 'meal', 'meal_id', 'meal_id_text', 'min', 'mini_app_url', 'missing', 'missing_labels', 'more_keyboard', 'msg', 'new_grams', 'new_max', 'new_min', 'new_val', 'new_weight', 'note', 'notify_admin', 'now', 'nutrition', 'object', 'ok', 'old_grams', 'option', 'option_index', 'pain_location', 'part', 'parts', 'parts_v2', 'payload', 'persist_meal', 'plan', 'plan_id', 'plan_now', 'plan_type', 'planned_sets', 'planning', 'plans_keyboard', 'progress', 'quality', 'query', 'range', 'ratio', 'rc', 'readiness', 'recommend_load', 'refreshed', 'render_candidate_list', 'render_exercise_params', 'render_meal', 'render_profile_snapshot', 'render_quantity_editor', 'render_smart_plan_hub', 'render_unified_plan', 'render_workout_overview', 'reopened', 'replacement', 'reps', 'reps_value', 'rest', 'rest_job_name', 'result', 'round', 'route_decision', 'row', 'rows', 'safe_edit', 'save_medical_constraint', 'save_split_set', 'second_base', 'second_reps', 'second_weight', 'secrets', 'select_todays_workout_code', 'selected', 'send_weight_chart', 'session', 'session_action_arg', 'session_action_data', 'session_id', 'set', 'set_daily_flags', 'set_exercise_override', 'set_goal_weight', 'set_meal_fix', 'set_pending', 'set_split_state', 'severity', 'show_session', 'split_reps_keyboard', 'split_rir_keyboard', 'split_state', 'split_summary_line', 'split_weight_keyboard', 'start_rest_timer', 'status', 'status_line', 'step', 'str', 'sum', 'summary_line', 'suppress', 't', 'tail', 'target_change_note', 'text', 'time', 'total_reps', 'track_event', 'training_intelligence', 'try_save_set', 'tuple', 'undo_last_set', 'undone', 'update', 'update_rest_message', 'update_session_step', 'url', 'user_choice', 'user_id', 'user_model', 'utc_now', 'value', 'value_text', 'warn', 'weight', 'workout', 'workout_summary', 'write_audit')

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
    keyboard_rows = [
        [button(label, callback_data) for label, callback_data in row]
        for row in next_meal_action_rows(recommendation)
    ]
    keyboard_rows.append([button("⬅️ חזרה למצב היום", "menu:status"), button("🏠 תפריט", "menu:home")])
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
        from noam_coach.services.next_meal import generate_next_meal_recommendation

        try:
            option_number = int(data.rsplit(":", 1)[1])
            recommendation = await generate_next_meal_recommendation(DB, user_id)
            option = recommendation.options[option_number - 1]
        except (TypeError, ValueError, IndexError):
            await _render_next_meal_screen(query, user_id, prefix="לא מצאתי את האפשרות. הנה שוב ההמלצה.")
            return True
        # Choosing does NOT log the meal as eaten — only "save as meal" does.
        nutrition = recommendation.context.nutrition
        after_cal = (nutrition.calorie_balance - option.calories) if nutrition.calorie_balance is not None else None
        impact = (
            f"\nאחרי הארוחה יישארו לך כ-{after_cal} קלוריות להיום." if after_cal is not None else ""
        )
        await safe_edit(
            query,
            (
                f"<b>{esc(option.title)}</b>\n"
                f"{esc(', '.join(option.ingredients))}\n"
                f"כ-{option.calories} קל׳ | כ-{option.protein} גרם חלבון{impact}\n\n"
                "רוצה שאשמור את זה כארוחה שאכלת?"
            ),
            InlineKeyboardMarkup([
                [button("💾 שמור כארוחה", f"nextmeal:save:{option_number}")],
                [button("⬅️ חזרה להמלצה", "menu:nextmeal")],
            ]),
        )
        return True

    if data.startswith("nextmeal:save:"):
        from noam_coach.services.next_meal import (
            clear_active_recommendation,
            generate_next_meal_recommendation,
            save_chosen_meal,
        )

        try:
            option_number = int(data.rsplit(":", 1)[1])
            recommendation = await generate_next_meal_recommendation(DB, user_id)
            option = recommendation.options[option_number - 1]
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
        # Quantity editing reuses the existing per-item editor entry point.
        await safe_edit(
            query,
            "כדי לכוונן כמויות מדויקות, בחר ״שמור כארוחה״ ואז ניתן לערוך פריטים, "
            "או כתוב לי למשל ״תוסיף 50 גרם אורז״.",
            InlineKeyboardMarkup([[button("⬅️ חזרה להמלצה", "menu:nextmeal")]]),
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

    if data == "menu:home":
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

    if data == "menu:more":
        await safe_edit(query, "<b>עוד פעולות והגדרות</b>", more_keyboard())
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
            InlineKeyboardMarkup([[button("⬅️ חזרה", "menu:more")], [button("🏠 תפריט ראשי", "menu:home")]]),
        )
        return True

    if data == "menu:status":
        await safe_edit(
            query,
            await build_daily_status(user_id),
            InlineKeyboardMarkup([[button("⬅️ תפריט", "menu:home")]]),
        )
        return True

    if data in ("menu:morning", "menu:nextmeal", "menu:evening"):
        await safe_edit(query, "רגע, מכין לך… ⏳", None)
        try:
            if data == "menu:morning":
                text = await build_morning_menu_text(user_id)
            elif data == "menu:nextmeal":
                await _render_next_meal_screen(query, user_id)
                return True
            else:
                text = await build_evening_summary_text(user_id)
        except Exception as exc:  # noqa: BLE001
            text = friendly_error(exc, "on-demand recommendation")
        await safe_edit(
            query,
            text,
            InlineKeyboardMarkup([[button("⬅️ תפריט", "menu:home")]]),
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
