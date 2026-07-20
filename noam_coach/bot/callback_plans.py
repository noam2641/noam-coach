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

RUNTIME_NAMES = ('APP_VERSION', 'Any', 'CALLBACK_DEBOUNCE_SECONDS', 'CONFIRM_PENDING', 'ContextTypes', 'DB', 'EXERCISE_MUSCLES', 'Exception', 'GOAL_STATUS_PROPOSED', 'GOAL_STATUS_PROVISIONAL', 'InlineKeyboardButton', 'InlineKeyboardMarkup', 'KeyError', 'LOGGER', 'MealAnalysis', 'OPENAI_CLIENT', 'PENDING_QUESTION', 'Path', 'RIR_UNKNOWN', 'SESSION_SCOPED_ACTIONS', 'SETTINGS', 'TypeError', 'Update', 'ValueError', 'WebAppInfo', '_DEBOUNCE_PREFIXES', '_LAST_CALLBACK', '_StaleSetStep', '_duration_s', '_home_hint', '_is_duplicate_tap', '_plan_type_label', '_started', 'action', 'activate_goal_version_provisional', 'active_flow', 'active_session', 'actual_reps', 'actual_rir', 'actual_weight', 'aiosqlite', 'alt', 'alt_muscle', 'alternative', 'alternative_index', 'analysis', 'analyze_duplicate_candidate', 'apply_reconcile_proposal', 'approval_id', 'bool', 'build_daily_status', 'build_evening_summary_text', 'build_health_status_text', 'build_morning_menu_text', 'build_next_meal_text', 'build_now_action_text', 'build_weekly_summary_text', 'button', 'buttons', 'callback_flow_id', 'callback_version', 'cancel_rest_timer', 'candidate', 'center', 'changed', 'check_duplicate_meal', 'choices', 'chosen_reps', 'chosen_weight', 'claimed', 'clear_confirm_pending', 'clear_meal_fix', 'clear_pending', 'clear_split_state', 'code', 'command_profile_query', 'completed', 'conn', 'connection', 'constraint_id', 'context', 'conversation', 'create_approval', 'create_goal_version', 'create_meal_edit_approval', 'cur', 'current', 'cursor', 'cutoff', 'data', 'datetime', 'decide_approval', 'deleted', 'delta', 'delta_text', 'dict', 'done', 'draft', 'dup', 'duplicate_approval_id', 'edit_approval_id', 'ensure_user', 'enumerate', 'error_id', 'esc', 'event_log', 'ex', 'exc', 'exercise_index', 'exercise_index_text', 'exercise_picker_keyboard', 'existing', 'extra', 'extra_seconds', 'fetch_approval', 'fetch_goal', 'field', 'final_rir', 'first_reps', 'first_weight', 'flags', 'float', 'frequency', 'friendly_error', 'get_daily_flags', 'get_meal_fix', 'get_split_state', 'get_user_plan', 'getattr', 'goal', 'goal_id', 'gv_id', 'handle_checkin_callback', 'handle_flags_callback', 'handle_goal_callback', 'handle_meal_callback', 'handle_menu_callback', 'handle_onboarding_callback', 'handle_plan_callback', 'handle_session_action_callback', 'handle_workout_setup_callback', 'home_keyboard', 'index', 'int', 'is_allowed', 'is_current_session_step', 'is_partial', 'is_provisional', 'isinstance', 'item', 'item_index', 'item_index_text', 'job', 'jobs', 'json', 'k', 'kb', 'key', 'kind', 'label', 'labels', 'last', 'len', 'level', 'list', 'logged_sets', 'max', 'meal', 'meal_id', 'meal_id_text', 'min', 'mini_app_url', 'missing', 'missing_labels', 'more_keyboard', 'msg', 'new_grams', 'new_max', 'new_min', 'new_val', 'new_weight', 'note', 'notify_admin', 'now', 'nutrition', 'object', 'ok', 'old_grams', 'option', 'option_index', 'pain_location', 'part', 'parts', 'parts_v2', 'payload', 'persist_meal', 'plan', 'plan_id', 'plan_now', 'plan_type', 'planned_sets', 'planning', 'plans_keyboard', 'progress', 'quality', 'query', 'range', 'ratio', 'rc', 'readiness', 'recommend_load', 'refreshed', 'render_candidate_list', 'render_exercise_params', 'render_meal', 'render_profile_snapshot', 'render_quantity_editor', 'render_smart_plan_hub', 'render_unified_plan', 'render_workout_overview', 'reopened', 'replacement', 'reps', 'reps_value', 'rest', 'rest_job_name', 'result', 'round', 'route_decision', 'row', 'rows', 'safe_edit', 'save_medical_constraint', 'save_split_set', 'second_base', 'second_reps', 'second_weight', 'secrets', 'select_todays_workout_code', 'selected', 'send_weight_chart', 'session', 'session_action_arg', 'session_action_data', 'session_id', 'set', 'set_daily_flags', 'set_exercise_override', 'set_goal_weight', 'set_meal_fix', 'set_pending', 'set_split_state', 'severity', 'show_session', 'split_reps_keyboard', 'split_rir_keyboard', 'split_state', 'split_summary_line', 'split_weight_keyboard', 'start_rest_timer', 'status', 'status_line', 'step', 'str', 'sum', 'summary_line', 'suppress', 't', 'tail', 'target_change_note', 'text', 'time', 'total_reps', 'track_event', 'training_intelligence', 'try_save_set', 'tuple', 'undo_last_set', 'undone', 'update', 'update_rest_message', 'update_session_step', 'url', 'user_choice', 'user_id', 'user_model', 'utc_now', 'value', 'value_text', 'warn', 'weight', 'workout', 'workout_summary', 'write_audit')

PENDING_PLAN_ACTION_FLOW = "pending_plan_action"
PENDING_PLAN_GENERATE_STEP = "generate_candidates"
PENDING_CALLBACK_STEP = "callback"


def goal_status_line(provisional: bool, missing: list) -> str:
    """The daily-goal precision line (extracted for review F-05).

    Review 2026-07-18_1 / F-05: a provisional goal with NOTHING left on the
    soft precision list must not render a dangling 'נדרש עוד: ' with an
    empty list after the colon (production event 370).
    """
    if not provisional:
        return "✅ מחושב מהפרופיל שלך"
    if missing:
        return (
            "⚠️ <b>יעד לפי הערכה חלקית</b> — לדיוק מלא נדרש עוד: "
            + ", ".join(esc(user_model.display_label(item)) for item in missing)
        )
    return "⚠️ <b>יעד לפי הערכה חלקית</b> — טרם אושר סופית"


async def _set_pending_plan_action(user_id: int, plan_type: str) -> None:
    from noam_coach.bot.onboarding import set_flow_state

    callback_data = f"planv2:generate:{plan_type}"
    await set_flow_state(
        user_id,
        PENDING_PLAN_ACTION_FLOW,
        PENDING_PLAN_GENERATE_STEP,
        {"plan_type": plan_type, "callback_data": callback_data},
    )


async def set_pending_callback_action(
    user_id: int,
    callback_data: str,
    *,
    plan_type: str | None = None,
) -> None:
    from noam_coach.bot.onboarding import set_flow_state

    await set_flow_state(
        user_id,
        PENDING_PLAN_ACTION_FLOW,
        PENDING_CALLBACK_STEP,
        {"callback_data": callback_data, "plan_type": plan_type},
    )


async def _clear_pending_plan_action(user_id: int) -> None:
    from noam_coach.bot.onboarding import clear_flow_state

    await clear_flow_state(user_id, PENDING_PLAN_ACTION_FLOW)


@runtime_bound(RUNTIME_NAMES)
async def render_prerequisite_completion_prompt(
    query: Any,
    user_id: int,
    *,
    callback_data: str,
    plan_type: str | None,
    missing: list[str],
    title: str,
) -> None:
    await set_pending_callback_action(user_id, callback_data, plan_type=plan_type)
    missing_display = [
        planning.FACT_LABELS.get(key)
        or user_model.display_label(key)
        or str(key)
        for key in missing
    ]
    lines = [f"<b>{esc(title)}</b>"]
    if missing_display:
        lines.append("")
        lines.extend(f"• {esc(label)}" for label in missing_display)
    rows = [
        [button("▶️ השלם עכשיו", f"planv2:complete_missing:{plan_type}" if plan_type else "planv2:complete_missing")],
        [button("⏳ אשלים אחר כך", "menu:smartplan")],
        [button("⬅️ חזרה לתוכניות", "menu:smartplan")],
    ]
    await safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(rows))


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
    # RE10-8: translate every missing key through the same Hebrew label map
    # used everywhere else — "active_goal" must never leak as a raw key.
    labels = [planning.FACT_LABELS.get(key) or user_model.display_label(key) for key in missing]
    tail = f"\n\nחסר: {esc(', '.join(labels))}" if labels else ""
    rows = []
    rows.append([button("▶️ השלם עכשיו", f"planv2:complete_missing:{plan_type}" if plan_type else "planv2:complete_missing")])
    rows.append([button("👤 הצג מה חסר", "planv2:profile")])
    rows.append([button("⬅️ חזרה לתוכניות", "menu:smartplan")])
    await safe_edit(
        query,
        f"<b>אי אפשר לבנות את התוכנית עדיין.</b>\n{esc(str(exc))}{tail}",
        InlineKeyboardMarkup(rows),
    )


@runtime_bound(RUNTIME_NAMES)
async def resume_pending_plan_action(query: Any, user_id: int) -> bool:
    from noam_coach.bot.onboarding import get_flow_state

    state = await get_flow_state(user_id, PENDING_PLAN_ACTION_FLOW)
    if not state or state.get("step") not in {PENDING_PLAN_GENERATE_STEP, PENDING_CALLBACK_STEP}:
        return False
    payload = state.get("payload") or {}
    plan_type = str(payload.get("plan_type") or "")
    callback_data = str(payload.get("callback_data") or "")
    original_updated_at = state.get("updated_at")
    if not callback_data and plan_type in {"nutrition", "workout"}:
        callback_data = f"planv2:generate:{plan_type}"

    if callback_data in {"menu:daily_menu", "menu:refresh_daily_menu", "menu:nextmeal"}:
        await safe_edit(query, "ממשיך מאיפה שעצרנו...", None)
        from noam_coach.bot.callback_menu import handle_menu_callback

        handled = await handle_menu_callback(query, user_id, callback_data)
        if handled:
            current = await get_flow_state(user_id, PENDING_PLAN_ACTION_FLOW)
            current_payload = (current or {}).get("payload") or {}
            if (
                current
                and current.get("step") == PENDING_CALLBACK_STEP
                and current.get("updated_at") != original_updated_at
                and current_payload.get("callback_data") == callback_data
            ):
                return True
            await _clear_pending_plan_action(user_id)
        return handled

    if callback_data.startswith("planv2:generate:"):
        plan_type = callback_data.split(":", 2)[2]
    if plan_type not in {"nutrition", "workout"}:
        await _clear_pending_plan_action(user_id)
        return False
    await safe_edit(query, "ממשיך מאיפה שעצרנו ובונה את ההצעות...", None)
    try:
        generated = await planning.generate_candidates(DB, user_id, plan_type)  # type: ignore[arg-type]
        await event_log.append_event(
            DB,
            user_id,
            "PLAN_CANDIDATES_GENERATED",
            entity="plan",
            source="planner",
            # TASK-19: real count, not a hardcoded 3.
            properties={"plan_type": plan_type, "count": len(generated or []), "resumed": True},
        )
        await _clear_pending_plan_action(user_id)
        await render_candidate_list(query, user_id, plan_type)
        return True
    except planning.PlanningBlockedError as exc:
        await _render_planning_blocked(query, user_id, exc, plan_type=plan_type, remember=True)
        return True


@runtime_bound(RUNTIME_NAMES)
async def render_goal_proposal(query: Any, user_id: int) -> None:
    """Render the goal proposal screen (RE10-9).

    Called once the goal wizard (height/goal weight/timeframe) has nothing
    left to ask — either because the user already had all the facts, or
    because they just finished answering. D3: superseding any earlier
    pending "goal" approval before creating a new one prevents dangling
    approvals from stale re-renders of this screen (repeated menu:goal taps
    used to each mint a new approval id).
    """
    await DB.execute(
        "UPDATE approvals SET status='superseded' WHERE user_id=? AND kind='goal' AND status='pending'",
        (user_id,),
    )
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

    # RE10-10: rephrase the (already-decided) numbers via AI for a warmer,
    # more complete explanation than the fixed template. The persisted
    # payload keeps the deterministic explanation as the source of truth;
    # this only affects what is shown on screen, and always falls back to
    # the deterministic text on any AI failure or number mismatch.
    from noam_coach.services.goal_explainer import explain_targets_with_ai
    from noam_coach.services.goals import compute_personal_targets

    display_explanation = payload["explanation"]
    computed = await compute_personal_targets(user_id)
    feasibility = None
    if computed is not None:
        display_explanation = await explain_targets_with_ai(
            computed, openai_client=OPENAI_CLIENT, model=SETTINGS.openai_model
        )
        # TASK-18: before presenting the calorie target as goal-compatible,
        # validate it against the requested goal weight + timeline.  When the
        # (safety-clamped) target cannot reach the goal by the deadline, say so
        # and offer explicit decisions instead of implying it will.
        import targets as targets_mod

        feasibility = targets_mod.assess_goal_feasibility(computed)

    missing = goal.get("missing_inputs") or []
    # RE10-9 / D2: "missing" here is the SOFT list (sex/age/height/avg_steps)
    # that only affects precision, not the hard GOAL_REQUIRED_FACTS gate —
    # label it as "for more precision" so it isn't confused with a blocker.
    status_line = goal_status_line(bool(payload["provisional"]), missing)
    # TASK-4: the daily target is decision-oriented. When the requested goal +
    # timeline is infeasible, lead with a single direct recommendation and
    # replace the vague "❌ דחה" with explicit decisions (approve the recommended
    # target / change target weight / change timeline / change calories). Goal
    # feasibility is kept separate from the data-quality precision note above.
    infeasible = bool(
        feasibility is not None and feasibility.applicable and not feasibility.feasible
    )
    if infeasible:
        rows = [
            [button("✅ אשר יעד מומלץ", f"approve_goal:{approval_id}")],
            [
                button("🎯 שנה יעד משקל", "onb:edit:goal_weight_kg"),
                button("⏳ שנה טווח זמן", "onb:edit:goal_timeframe_weeks"),
            ],
            [button("✏️ שנה קלוריות", "goal:manual")],
        ]
    else:
        rows = [
            [
                button("✅ אשר יעד" if not payload["provisional"] else "⏳ השתמש זמנית", f"approve_goal:{approval_id}"),
                button("✏️ שנה קלוריות", "goal:manual"),
            ],
        ]

    feasibility_block = ""
    if feasibility is not None and feasibility.applicable:
        if feasibility.feasible:
            feasibility_block = f"\n\n✅ <i>{esc(feasibility.message)}</i>"
        else:
            # Requested outcome + gap stated once, then the direct recommendation
            # (the recommended target is the one behind "✅ אשר יעד מומלץ").
            feasibility_block = (
                f"\n\n⚠️ {esc(feasibility.message)}"
                f"\n\n💡 <b>{esc(feasibility.recommendation)}</b>"
            )
    # RE10-9: the missing items are already spelled out in status_line above,
    # so a redundant "מה חסר" button here would just repeat the same
    # information — only offer it when there is nothing already shown.
    if not missing:
        rows.append([button("👤 הצג פרופיל מלא", "planv2:profile")])
    rows.append([button("⬅️ תפריט", "menu:home")])

    # When the goal is infeasible we lead with the recommendation and skip the
    # long AI explanation, so the screen does not repeat the same reasoning.
    explanation_block = "" if infeasible else f"\n\n<i>{esc(display_explanation)}</i>"
    await safe_edit(
        query,
        (
            "<b>יעד יומי</b>\n\n"
            f"קלוריות: <b>{payload['calories']:,}</b>\n"
            f"חלבון: <b>{payload['protein']} גרם</b>\n"
            f"צעדים: <b>{payload['steps']:,}</b>\n\n"
            f"{status_line}"
            f"{feasibility_block}"
            f"{explanation_block}\n\n"
            "היעד לא ישתנה בעתיד בלי אישור שלך."
        ),
        InlineKeyboardMarkup(rows),
    )


@runtime_bound(RUNTIME_NAMES)
async def _handle_workout_menu_actions(
    query: Any,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    data: str,
) -> bool:
    if data in ("menu:workout", "wk:list"):
        session = await active_session(user_id)
        if session:
            await show_session(query, user_id, session["id"])
            return True
        # Batch 4 (workout-selection architecture): the menu now resolves
        # through the two-tier catalog instead of resolve_todays_workout +
        # the template-only render_workout_overview. This is the pin #1 flip
        # (plan section M.1) -- menu:workout renders the SELECTED active-plan
        # (or Tier-2) session's content, never PLANS[recommended_code].
        # Recommendation becomes a hint (⭐), not a forced selection.
        #
        # Audit F-A3 is preserved: 'כבר הושלם ✅' is still never claimed for a
        # user who simply has no plan -- the two states stay distinguished by
        # provenance from real session rows.
        await _render_workout_selector(query, user_id)
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
        from noam_coach.bot.onboarding import ask_next_goal_wizard_question

        # RE10-9: complete the facts that make the proposal meaningful
        # (height, goal weight, timeframe) with a guided Q&A BEFORE showing
        # a provisional proposal that immediately says "⚠️ missing: height".
        if await ask_next_goal_wizard_question(query, user_id):
            return True
        await render_goal_proposal(query, user_id)
        return True

    if data == "goal:manual":
        # RE10-9: explicit button for a manual calorie override, instead of
        # only being reachable by typing a bare number.
        await safe_edit(
            query,
            "כתוב את יעד הקלוריות היומי שאתה רוצה (מספר, למשל 2100).",
            InlineKeyboardMarkup([[button("⬅️ ביטול", "menu:goal")]]),
        )
        from noam_coach.bot.onboarding import set_pending

        await set_pending(user_id, "__manual_goal_calories__")
        return True
    return False


# ---------------------------------------------------------------------------
# Workout selector / overview / start v2 (workout-selection architecture,
# Batch 4). The complete minimal callback graph lands together here --
# `wk:list`, `wk:sel`, `wk:fsel`, `wk:start`, `wk:fstart` -- so no batch ever
# ships a user-visible Start button whose handler arrives later (plan §M.3).
# ---------------------------------------------------------------------------


async def _refuse_stale_workout_ref(query: Any, user_id: int, *, ref: Any = None, detail: str = "") -> None:
    """One intentional refusal for a `wk:` callback whose carried identity no
    longer matches the live plan/fact, then a FRESH selector so the user is
    never left staring at a dead screen. Never mutates product state -- a
    stale reference must refuse, never silently resolve to another workout.
    """
    from noam_coach.services.control_refusal import refuse_control

    extra: dict[str, Any] = {"detail": detail} if detail else {}
    if ref is not None:
        extra.update({"tier": ref.tier, "plan_id": ref.plan_id, "fact_rev": ref.fact_rev,
                      "session_index": ref.session_index})
    await refuse_control(
        query,
        user_id,
        reason="stale_plan_reference",
        toast="התוכנית התעדכנה",
        source="workout_selector",
        extra=extra or None,
    )
    await _render_workout_selector(query, user_id)


@runtime_bound(RUNTIME_NAMES)
async def _render_workout_selector(query: Any, user_id: int) -> None:
    """Render the workout selector for whichever tier the user has.

    0 choices -> the honest no-plan / all-done fallback (F-A3 preserved);
    1 choice  -> straight to that session's overview (no pointless one-row
                 list), Back goes home;
    >=2       -> the selector with real names, ⭐ recommended, ✅ done-today.
    """
    from noam_coach.bot.ui import render_workout_overview_v2, workout_selector_keyboard
    from noam_coach.services import workout_catalog

    choices = await workout_catalog.list_selectable_workouts(DB, user_id)
    if not choices:
        # No plan of EITHER tier. Distinguish "never had one" from "all of
        # today's are done" exactly as the pre-Batch-4 menu did.
        from noam_coach.bot.ui import resolve_todays_workout

        todays = await resolve_todays_workout(user_id)
        if todays.reason == "all_done_today":
            done = ", ".join(todays.done_today)
            await safe_edit(
                query,
                f"כל האימונים של היום כבר בוצעו ({esc(done)}) ✅\n"
                "רוצה לבחור אימון נוסף בכל זאת?",
                plans_keyboard(),
            )
        else:
            await safe_edit(
                query,
                "עוד אין לך תוכנית אימונים פעילה.\n"
                "אפשר ליצור אחת דרך \"התוכנית שלי\", או לבחור אימון ידני:",
                plans_keyboard(),
            )
        return

    if len(choices) == 1:
        try:
            resolved = await workout_catalog.resolve_selection(DB, user_id, choices[0].ref)
        except workout_catalog.StalePlanReference as exc:
            # The plan changed between listing and resolving (a very narrow
            # window). Refuse rather than render something unverified; do not
            # recurse into the selector again.
            from noam_coach.services.control_refusal import refuse_control

            await refuse_control(
                query, user_id, reason="stale_plan_reference", toast="התוכנית התעדכנה",
                source="workout_selector", extra={"detail": str(exc)},
            )
            return
        await _apply_overrides_to_resolved(user_id, resolved)
        await render_workout_overview_v2(query, user_id, resolved, single_choice=True)
        return

    first = choices[0]
    # E2 (plan section F, closed in Batch 8): two SEPARATE concepts.
    #
    # (a) Per-session ✅ is identity-specific -- `choice.done_today` matches a
    #     completion to THIS session's code, so only the session actually
    #     performed is marked.
    # (b) The user-level banner must be truthful regardless of identity. Before
    #     this fix the banner was derived from (a), so regenerating into a plan
    #     with DIFFERENT codes made the selector silently claim the user had not
    #     trained today -- the system appearing unaware of a real completion.
    #     It now asks the completion-evidence source directly (real session rows
    #     / HealthKit), which no plan change can invalidate.
    #
    # Valid alternative sessions are never suppressed: the banner informs, and
    # every session stays selectable. Starting one already completed today
    # still goes through the Batch-5 `:again` confirmation.
    from noam_coach.services import daily_state as _daily_state

    banner = ""
    if await _daily_state.workout_completed_today(DB, user_id):
        banner = "כבר התאמנת היום ✅\n\n"
    await safe_edit(
        query,
        f"{banner}<b>איזה אימון נעשה?</b>",
        workout_selector_keyboard(choices),
    )
    await track_event(
        user_id, "workout_selector_rendered",
        tier=first.ref.tier, session_count=len(choices), reason=first.reason,
    )


def _parse_wk_ref(data: str) -> Any:
    """Parse `wk:{sel,fsel,start,fstart}:<identity>:<sidx>[:again]` into a
    WorkoutSelectionRef. Returns None for any malformed payload (a corrupted
    or hand-crafted callback must refuse, never raise).

    The optional trailing `:again` (Batch 5) is a repeat-confirmation marker
    on the START forms only; it is stripped here and read separately by
    _wk_is_again, so identity parsing stays one function with one shape.
    """
    from noam_coach.services.workout_catalog import WorkoutSelectionRef

    parts = data.split(":")
    if len(parts) == 5:
        # Only the start forms accept the repeat marker; `wk:sel:...:again`
        # is not a callback this codebase mints and must not be honored.
        if parts[4] != "again" or parts[1] not in ("start", "fstart"):
            return None
        parts = parts[:4]
    if len(parts) != 4:
        return None
    _, action, identity, index_text = parts
    try:
        session_index = int(index_text)
    except (TypeError, ValueError):
        return None
    if session_index < 0:
        return None
    if action in ("sel", "start"):
        try:
            plan_id = int(identity)
        except (TypeError, ValueError):
            return None
        return WorkoutSelectionRef(tier="plan", plan_id=plan_id, fact_rev=None, session_index=session_index)
    if action in ("fsel", "fstart"):
        if not identity:
            return None
        return WorkoutSelectionRef(tier="fact", plan_id=None, fact_rev=identity, session_index=session_index)
    return None


@runtime_bound(RUNTIME_NAMES)
async def _apply_overrides_to_resolved(user_id: int, resolved: Any) -> None:
    """Overlay the user's stored overrides onto a resolved session IN PLACE.

    The catalog resolves plan/template content; overrides are a separate,
    per-user layer. Every read-side edit screen must show the effective value
    (what Start would snapshot), so this applies exactly the same
    collect_overrides result materialize_snapshot would -- code-scoped and
    id-verified per plan section G, never by bare index.
    """
    from noam_coach.services import workout_catalog

    overrides = await workout_catalog.collect_overrides(DB, user_id, resolved.session)
    if not overrides:
        return
    for exercise in resolved.session.get("exercises", []):
        for override_field, value in overrides.get(exercise.get("id"), {}).items():
            exercise[override_field] = value


@runtime_bound(RUNTIME_NAMES)
async def _apply_workout_param_delta(
    user_id: int, resolved: Any, exercise_index: int, field: str, delta: float
) -> None:
    """Persist ONE stepper delta against the identified session's exercise.

    Writes are code-scoped and carry the stable ``exercise_id`` (plan section
    G.6), so a later read verifies identity rather than trusting position.
    The positional ``exercise_index`` still goes into the row because it is
    part of the table's primary key -- but note it is the index within THIS
    session, which for Tier-1 is the personalized payload's ordering. That is
    exactly why the id is written alongside: collect_overrides matches on the
    id first and only falls back to a verified template position for legacy
    NULL-id rows.
    """
    exercises = resolved.session.get("exercises", [])
    current = exercises[exercise_index]
    code = resolved.choice.code
    exercise_id = current.get("id")

    new_min, new_max = int(current["rmin"]), int(current["rmax"])
    if field == "weight":
        new_value: float = max(0.0, round(float(current["weight"]) + delta, 2))
    elif field == "sets":
        new_value = max(1, int(current["sets"] + delta))
    elif field == "rmin":
        new_min = max(1, int(current["rmin"] + delta))
        new_value = new_min
        if new_min > new_max:
            new_max = new_min
            await set_exercise_override(
                user_id, code, exercise_index, "rmax", new_max, exercise_id=exercise_id
            )
    elif field == "rmax":
        new_max = max(int(current["rmin"]), int(current["rmax"] + delta))
        new_value = new_max
    elif field == "rest":
        new_value = max(30, int(current["rest"] + delta))
    else:
        return

    await set_exercise_override(
        user_id, code, exercise_index, field, new_value, exercise_id=exercise_id
    )


def _wk_back_callback_for_payload(payload: dict[str, Any] | None) -> str | None:
    """The `wk:sel`/`wk:fsel` callback that returns to the overview a v2 edit
    flow was started from, or None for a legacy (code-only) payload.

    Kept here rather than in ui.py so both callback_plans and meal_text share
    one definition of "where does Back go" -- the selection must survive the
    whole edit loop (plan section M, Batch 6 objective).
    """
    payload = payload or {}
    if int(payload.get("v") or 0) < 2:
        return None
    tier = str(payload.get("tier") or "")
    session_index = payload.get("session_index")
    if session_index is None:
        return None
    if tier == "plan" and payload.get("plan_id") is not None:
        return f"wk:sel:{payload['plan_id']}:{session_index}"
    if tier == "fact" and payload.get("fact_rev"):
        return f"wk:fsel:{payload['fact_rev']}:{session_index}"
    return None


def _wk_edit_callback_for_payload(payload: dict[str, Any] | None) -> str | None:
    """The `wk:ex:...` callback that reopens the SAME exercise editor, used by
    the 'write a different correction' button."""
    payload = payload or {}
    back = _wk_back_callback_for_payload(payload)
    if not back:
        return None
    exercise_index = payload.get("exercise_index")
    if exercise_index is None:
        return None
    # wk:sel:<identity>:<sidx> -> wk:ex:<identity>:<sidx>:<exercise_index>
    identity_and_index = back.split(":", 2)[2]
    return f"wk:ex:{identity_and_index}:{exercise_index}"


@runtime_bound(RUNTIME_NAMES)
async def _apply_workout_param_text_v2(
    query: Any, user_id: int, payload: dict[str, Any], pending_updates: list[Any]
) -> bool:
    """Apply a confirmed free-text parameter edit against a v2 flow payload.

    Re-validates the carried identity immediately before writing (the
    compose->confirm window is exactly where a regeneration can land), then
    applies each update with the plan's approved scope rules:

      * scope "current"  -> the one edited exercise, by stable id
      * scope "all"      -> every exercise in THIS session only
      * scope "program"  -> every exercise of every session in the user's
                            current plan, each resolved through the catalog
                            so writes stay code-scoped and id-verified

    Returns True when the edit was applied (or explicitly refused and
    rendered); the caller always returns True afterwards.
    """
    from noam_coach.services import workout_catalog
    from noam_coach.services.workout_catalog import WorkoutSelectionRef

    ref = WorkoutSelectionRef(
        tier=str(payload.get("tier") or "plan"),
        plan_id=payload.get("plan_id"),
        fact_rev=payload.get("fact_rev"),
        session_index=int(payload.get("session_index") or 0),
    )
    exercise_index = int(payload.get("exercise_index") or 0)
    expected_exercise_id = payload.get("exercise_id")

    try:
        resolved = await workout_catalog.resolve_selection(DB, user_id, ref)
    except workout_catalog.StalePlanReference as exc:
        await conversation.clear_active_flow(DB, user_id)
        await _refuse_stale_workout_ref(query, user_id, ref=ref, detail=str(exc))
        return True

    exercises = resolved.session.get("exercises", [])
    if not 0 <= exercise_index < len(exercises):
        await conversation.clear_active_flow(DB, user_id)
        await _refuse_stale_workout_ref(query, user_id, ref=ref, detail="exercise_index_out_of_range")
        return True

    # Identity cross-check: the exercise this edit was composed against must
    # still be the exercise at that position. Immutable payloads make this
    # nearly always true, but a Tier-2 template revision could change it --
    # and applying a "bench" edit to whatever now sits at index 2 is exactly
    # the class of bug this architecture exists to prevent.
    actual_exercise_id = exercises[exercise_index].get("id")
    if expected_exercise_id and actual_exercise_id != expected_exercise_id:
        await conversation.clear_active_flow(DB, user_id)
        await _refuse_stale_workout_ref(query, user_id, ref=ref, detail="exercise_identity_changed")
        return True

    await _apply_overrides_to_resolved(user_id, resolved)

    # Build the target list per scope. "program" walks the user's whole plan
    # via the catalog (never a bare code list), so every write is scoped to a
    # real resolved session and carries that session's own exercise ids.
    async def _targets(scope: str) -> list[tuple[Any, list[int]]]:
        if scope == "program":
            out: list[tuple[Any, list[int]]] = []
            for choice in await workout_catalog.list_selectable_workouts(DB, user_id):
                try:
                    other = await workout_catalog.resolve_selection(DB, user_id, choice.ref)
                except workout_catalog.StalePlanReference:
                    continue
                await _apply_overrides_to_resolved(user_id, other)
                out.append((other, list(range(len(other.session.get("exercises", []))))))
            return out
        if scope == "all":
            return [(resolved, list(range(len(exercises))))]
        return [(resolved, [exercise_index])]

    applied: list[str] = []
    affected_count = 0
    for update_item in pending_updates:
        if not isinstance(update_item, dict):
            continue
        scope = str(update_item.get("scope") or "current")
        field = str(update_item.get("field") or "")
        for target_resolved, indices in await _targets(scope):
            target_exercises = target_resolved.session.get("exercises", [])
            target_code = target_resolved.choice.code
            for idx in indices:
                if not 0 <= idx < len(target_exercises):
                    continue
                target_id = target_exercises[idx].get("id")
                if not target_id:
                    # Batch 8 (Scope 3): skip exercises with no stable identity
                    # rather than writing a NULL-id row that would later be
                    # re-applied by template POSITION to whatever sits at that
                    # index. Silently skipping one malformed entry is correct
                    # here -- an "all"/"program" scope edit should still apply
                    # to every well-formed exercise -- and the count reported
                    # to the user below reflects only what was really written.
                    continue
                if field == "reps":
                    rmin = int(update_item.get("rmin") or target_exercises[idx]["rmin"])
                    rmax = int(update_item.get("rmax") or target_exercises[idx]["rmax"])
                    await set_exercise_override(
                        user_id, target_code, idx, "rmin", rmin, exercise_id=target_id
                    )
                    await set_exercise_override(
                        user_id, target_code, idx, "rmax", max(rmin, rmax), exercise_id=target_id
                    )
                    affected_count += 1
                elif field in {"weight", "sets", "rest"}:
                    await set_exercise_override(
                        user_id, target_code, idx, field, float(update_item["value"]),
                        exercise_id=target_id,
                    )
                    affected_count += 1
        label = str(update_item.get("label") or field)
        if label:
            applied.append(label)

    await conversation.clear_active_flow(DB, user_id)
    summary = ", ".join(applied) if applied else "השינוי"
    scope_label = str(payload.get("scope_label") or "")
    if affected_count:
        scope_label = f"{scope_label} ({affected_count} תרגילים הושפעו)"
    from noam_coach.bot.ui import wk_exercise_menu_callback, wk_select_callback

    await safe_edit(
        query,
        f"שמרתי: {summary} {scope_label} ✅",
        InlineKeyboardMarkup([
            [button("⬅️ חזרה לאימון", wk_select_callback(ref))],
            [button("✏️ ערוך עוד", wk_exercise_menu_callback(ref))],
        ]),
    )
    return True


def _parse_wk_edit_ref(data: str) -> tuple[Any, int, str | None, float | None] | None:
    """Parse the Batch-6 edit callbacks into
    (ref, exercise_index, field, delta):

        wk:exm:<identity>:<sidx>                              -> (ref, -1, None, None)
        wk:ex:<identity>:<sidx>:<exercise_index>              -> (ref, idx, None, None)
        wk:par:<identity>:<sidx>:<exercise_index>:<field>:<d> -> (ref, idx, field, d)

    Returns None for any malformed payload -- a corrupted or hand-crafted
    callback must refuse, never raise, and never fall back to a bare code.
    """
    from exercise_plans import OVERRIDE_FIELDS
    from noam_coach.services.workout_catalog import WorkoutSelectionRef

    parts = data.split(":")
    if len(parts) < 4:
        return None
    action, identity, index_text = parts[1], parts[2], parts[3]
    if action not in ("exm", "ex", "par"):
        return None
    try:
        session_index = int(index_text)
    except (TypeError, ValueError):
        return None
    if session_index < 0:
        return None

    # `exm` and `par` are minted from the SAME identity segment shape as the
    # selector: numeric plan_id for Tier-1, 8-hex fingerprint for Tier-2. The
    # action name alone decides the tier, exactly as with sel/fsel.
    if identity.lstrip("-").isdigit():
        ref = WorkoutSelectionRef(tier="plan", plan_id=int(identity), fact_rev=None,
                                  session_index=session_index)
    elif identity:
        ref = WorkoutSelectionRef(tier="fact", plan_id=None, fact_rev=identity,
                                  session_index=session_index)
    else:
        return None

    if action == "exm":
        return (ref, -1, None, None) if len(parts) == 4 else None

    if len(parts) < 5:
        return None
    try:
        exercise_index = int(parts[4])
    except (TypeError, ValueError):
        return None
    if exercise_index < 0:
        return None

    if action == "ex":
        return (ref, exercise_index, None, None) if len(parts) == 5 else None

    if len(parts) != 7:
        return None
    field = parts[5]
    if field not in OVERRIDE_FIELDS:
        return None
    try:
        delta = float(parts[6])
    except (TypeError, ValueError):
        return None
    return ref, exercise_index, field, delta


def _wk_is_again(data: str) -> bool:
    """True when a start callback carries the Batch-5 repeat-confirmation
    marker. `:again` skips ONLY the done-today interstitial -- identity
    re-validation and the active-session/unique-index guards all still run."""
    return data.endswith(":again")


@runtime_bound(RUNTIME_NAMES)
async def _handle_workout_v2_actions(
    query: Any,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    data: str,
) -> bool:
    """`wk:` family: selector, overview (both tiers), and Start (both tiers)."""
    if not data.startswith("wk:"):
        return False

    from noam_coach.bot.ui import render_workout_overview_v2
    from noam_coach.services import workout_catalog

    # `wk:list` is handled by _handle_workout_menu_actions alongside
    # menu:workout (same screen, same active-session reopen semantics).

    if data.startswith(("wk:sel:", "wk:fsel:")):
        # Selection is a pure navigation/read action: reopen an active session
        # if one exists (existing product behavior), else render the overview.
        session = await active_session(user_id)
        if session:
            await show_session(query, user_id, session["id"])
            return True
        ref = _parse_wk_ref(data)
        if ref is None:
            await _refuse_stale_workout_ref(query, user_id, detail="malformed_ref")
            return True
        try:
            resolved = await workout_catalog.resolve_selection(DB, user_id, ref)
        except workout_catalog.StalePlanReference as exc:
            await _refuse_stale_workout_ref(query, user_id, ref=ref, detail=str(exc))
            return True
        # Show EFFECTIVE values (Batch 6): the overview must display what
        # Start will actually snapshot, overrides included, or the user edits
        # a number and sees the un-edited one on the way back.
        await _apply_overrides_to_resolved(user_id, resolved)
        await render_workout_overview_v2(query, user_id, resolved)
        await track_event(
            user_id, "workout_selection_activated",
            tier=ref.tier, session_index=ref.session_index,
            followed_recommendation=resolved.choice.recommended,
        )
        return True

    if data.startswith(("wk:exm:", "wk:ex:", "wk:par:")):
        # Batch 6: identity-safe parameter editing. Every one of these
        # re-resolves the carried identity BEFORE doing anything, so an edit
        # can never be applied to a session other than the one displayed --
        # and a regenerated plan refuses instead of silently retargeting.
        from noam_coach.bot.ui import (
            exercise_picker_keyboard_v2,
            render_exercise_params_v2,
        )

        parsed = _parse_wk_edit_ref(data)
        if parsed is None:
            await _refuse_stale_workout_ref(query, user_id, detail="malformed_edit_ref")
            return True
        ref, exercise_index, field, delta = parsed

        try:
            resolved = await workout_catalog.resolve_selection(DB, user_id, ref)
        except workout_catalog.StalePlanReference as exc:
            await _refuse_stale_workout_ref(query, user_id, ref=ref, detail=str(exc))
            return True

        await _apply_overrides_to_resolved(user_id, resolved)
        exercises = resolved.session.get("exercises", [])
        if data.startswith("wk:exm:"):
            await safe_edit(
                query,
                f"<b>בחר תרגיל לעריכת פרמטרים</b>\n{esc(resolved.choice.name)}",
                exercise_picker_keyboard_v2(resolved),
            )
            return True

        if not 0 <= exercise_index < len(exercises):
            # The session no longer has that many exercises (a regenerated
            # plan of the same id is impossible -- payloads are immutable --
            # but a hand-crafted index must still refuse).
            await _refuse_stale_workout_ref(query, user_id, ref=ref, detail="exercise_index_out_of_range")
            return True

        if data.startswith("wk:ex:"):
            await render_exercise_params_v2(query, user_id, resolved, exercise_index)
            return True

        # Batch 8 (Scope 3): refuse to write an override against an exercise
        # with no usable stable identity. A historical/malformed payload can
        # carry an exercise with no `id` (or an empty one); writing a NULL-id
        # row for it would later be re-applied BY TEMPLATE POSITION, i.e. to
        # whatever exercise happens to sit at that index -- the same-index
        # accidental mutation the approved override model forbids. Rendering
        # such a session is fine (normalization repairs it for display); only
        # the WRITE is refused.
        if not resolved.session["exercises"][exercise_index].get("id"):
            await safe_answer_callback(
                query, "לא ניתן לשמור שינוי לתרגיל הזה", show_alert=True
            )
            await render_exercise_params_v2(query, user_id, resolved, exercise_index)
            return True

        # wk:par -- apply one stepper delta, then re-render from freshly
        # resolved+overridden state so the screen always reflects storage.
        await _apply_workout_param_delta(
            user_id, resolved, exercise_index, str(field), float(delta)
        )
        refreshed = await workout_catalog.resolve_selection(DB, user_id, ref)
        await _apply_overrides_to_resolved(user_id, refreshed)
        await render_exercise_params_v2(query, user_id, refreshed, exercise_index)
        return True

    if data.startswith(("wk:start:", "wk:fstart:")):
        # Active-session guard FIRST (unchanged product behavior: one active
        # session per user; a second start reopens the existing one).
        current = await active_session(user_id)
        if current:
            await show_session(query, user_id, current["id"])
            return True

        ref = _parse_wk_ref(data)
        if ref is None:
            await _refuse_stale_workout_ref(query, user_id, detail="malformed_ref")
            return True

        # Re-read and RE-VALIDATE immediately before the INSERT. This is the
        # narrow overview->start TOCTOU window: the plan may have been
        # regenerated (Tier-1) or the weekly fact replaced (Tier-2, caught by
        # the recomputed 8-hex fingerprint even when the replacement has the
        # SAME number of sessions) while the overview sat on screen.
        try:
            resolved = await workout_catalog.resolve_selection(DB, user_id, ref)
        except workout_catalog.StalePlanReference as exc:
            await _refuse_stale_workout_ref(query, user_id, ref=ref, detail=str(exc))
            return True

        # Batch 5: repeat-today confirmation. Starting a session already
        # completed today is legitimate (a second leg day, a redo) but must be
        # DELIBERATE -- otherwise a stale overview left open from this morning
        # silently starts a duplicate. The interstitial is shown once; the
        # `:again` variant of this exact callback skips ONLY this check.
        # Identity re-validation above and the active-session/unique-index
        # guards below still run for `:again`, so confirming a repeat can
        # never bypass staleness or concurrency protection.
        if resolved.choice.done_today and not _wk_is_again(data):
            await safe_edit(
                query,
                f"כבר ביצעת היום את <b>{esc(resolved.choice.name)}</b> ✅\n\n"
                "לחזור עליו שוב?",
                InlineKeyboardMarkup([
                    [button("🔁 כן, להתחיל שוב", f"{data}:again")],
                    [button("⬅️ חזרה", "wk:list")],
                ]),
            )
            await track_event(
                user_id, "workout_repeat_confirm_shown",
                tier=ref.tier, session_index=ref.session_index,
            )
            return True

        overrides = await workout_catalog.collect_overrides(DB, user_id, resolved.session)
        snapshot = workout_catalog.materialize_snapshot(user_id, resolved, overrides)
        code = resolved.choice.code
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
                    snapshot["name"],
                    json.dumps(snapshot, ensure_ascii=False),
                    utc_now(),
                ),
            )
        except aiosqlite.IntegrityError:
            # The unique partial index rejected a second concurrent start --
            # show the session that already opened (same recovery as the
            # legacy startworkout path, callback_plans.py:534-569).
            existing = await active_session(user_id)
            if existing:
                await show_session(query, user_id, existing["id"])
            return True
        await write_audit(user_id, "start", "workout", session_id, code=code)
        await track_event(
            user_id, "workout_session_started",
            session_id=session_id, tier=ref.tier, session_index=ref.session_index,
            source=snapshot["provenance"]["source"],
            overrides_applied_count=len(snapshot["provenance"]["overrides_applied"]),
            defaults_filled_count=len(snapshot["provenance"]["defaults_filled"]),
            repeat=_wk_is_again(data),
        )
        await show_session(query, user_id, session_id)
        return True

    return False


@runtime_bound(RUNTIME_NAMES)
async def _handle_legacy_workout_callback(
    query: Any,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    data: str,
) -> bool:
    """Route one already-issued legacy workout callback through the Batch-7
    compatibility adapter.

    Returns True when the adapter fully handled it (mapped to the v2 flow,
    refused as ambiguous, or refused as stale). Returns False ONLY for
    `template_fallback` -- the caller then runs the preserved legacy template
    path. Every outcome emits exactly one workout_legacy_callback event.
    """
    from noam_coach.bot import workout_compat

    parsed = workout_compat.parse_legacy_callback(data)
    if parsed is None:
        return False  # not a legacy workout callback, or malformed -- caller decides

    action = parsed["action"]
    code = parsed["code"]
    resolution = await workout_compat.resolve_legacy_code(DB, user_id, code)
    outcome = resolution["outcome"]
    ref = resolution["ref"]

    await workout_compat.emit_legacy_event(
        DB, user_id, prefix=parsed["prefix"], action=action, code=code,
        outcome=outcome, ref=ref,
        source=(
            "plan_version" if (ref is not None and ref.tier == "plan")
            else "weekly_fact_template" if (ref is not None and ref.tier == "fact")
            else "template_fallback" if outcome == workout_compat.OUTCOME_TEMPLATE_FALLBACK
            else None
        ),
    )

    if outcome == workout_compat.OUTCOME_TEMPLATE_FALLBACK:
        return False  # caller runs the explicit legacy template path

    if outcome == workout_compat.OUTCOME_AMBIGUOUS:
        # Two or more sessions share this code. A bare code cannot say which,
        # and picking one would be exactly the silent-wrong-workout defect
        # this architecture exists to remove. Ask.
        await safe_answer_callback(query, "בחר את האימון המבוקש")
        await _render_workout_selector(query, user_id)
        return True

    if outcome == workout_compat.OUTCOME_STALE or ref is None:
        await _refuse_stale_workout_ref(
            query, user_id, ref=ref, detail=f"legacy_{parsed['prefix']}_code_{code}"
        )
        return True

    # outcome == mapped: hand off to the v2 handler with the real identity, so
    # legacy taps inherit every v2 guard (active session, re-validation before
    # INSERT, done-today `:again` confirmation, id-verified overrides).
    from noam_coach.bot.ui import (
        wk_exercise_callback,
        wk_exercise_menu_callback,
        wk_param_callback,
        wk_select_callback,
        wk_start_callback,
    )

    if action == workout_compat.ACTION_OVERVIEW:
        v2 = wk_select_callback(ref)
    elif action == workout_compat.ACTION_START:
        v2 = wk_start_callback(ref)
    elif action == workout_compat.ACTION_EDIT_MENU:
        v2 = wk_exercise_menu_callback(ref)
    elif action == workout_compat.ACTION_EDIT_EXERCISE:
        v2 = wk_exercise_callback(ref, int(parsed["exercise_index"]))
    elif action == workout_compat.ACTION_EDIT_PARAM:
        v2 = wk_param_callback(
            ref, int(parsed["exercise_index"]), str(parsed["field"]), float(parsed["delta"])
        )
    else:  # pragma: no cover - parse_legacy_callback yields no other action
        return False

    return await _handle_workout_v2_actions(query, context, user_id, v2)


@runtime_bound(RUNTIME_NAMES)
async def _handle_workout_start_actions(
    query: Any,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    data: str,
) -> bool:
    if data.startswith(("workout:", "startworkout:")):
        # Batch 7: already-issued legacy buttons route through the
        # compatibility adapter so they resolve with the SAME catalog
        # identity, normalization, override and staleness rules as `wk:`.
        # A unique code match becomes a real identity and is handled by the
        # v2 path; ambiguity refuses to the selector rather than guessing;
        # only a genuinely plan-less user reaches the template path below.
        handled = await _handle_legacy_workout_callback(query, context, user_id, data)
        if handled:
            return True

        # template_fallback only: no active plan of either tier, known
        # PLANS code. This is the honest pre-v2 behavior, preserved.
        code = data.split(":", 1)[1]
        if data.startswith("workout:"):
            await render_workout_overview(query, user_id, code)
            return True

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
    if data == "wparamtext:cancel":
        flow = await conversation.get_active_flow(DB, user_id)
        payload = flow.payload if flow.name == conversation.FlowName.workout_parameter_edit else {}
        code = str(payload.get("code") or "A")
        await conversation.clear_active_flow(DB, user_id)
        # Batch 6: cancel returns to the SAME selected overview when the flow
        # carried a v2 identity, instead of the ambiguous bare-code screen.
        back = _wk_back_callback_for_payload(payload) or f"workout:{code}"
        await safe_edit(
            query,
            "ביטלתי את עריכת הפרמטרים. לא שמרתי שינוי.",
            InlineKeyboardMarkup([[button("⬅️ חזרה לאימון", back)]]),
        )
        return True

    if data == "wparamtext:apply":
        flow = await conversation.get_active_flow(DB, user_id)
        if flow.name != conversation.FlowName.workout_parameter_edit:
            await safe_edit(
                query,
                "העריכה כבר לא פעילה. פתחתי את מסך האימון מחדש.",
                InlineKeyboardMarkup([[button("🏠 תפריט", "menu:home")]]),
            )
            return True
        payload = dict(flow.payload or {})
        code = str(payload.get("code") or "")
        exercise_index = int(payload.get("exercise_index") or 0)
        pending_updates = list(payload.get("pending_updates") or [])
        if not code or not pending_updates:
            await conversation.clear_active_flow(DB, user_id)
            await safe_edit(
                query,
                "לא מצאתי שינוי שממתין לאישור. אפשר לפתוח עריכת פרמטרים מחדש.",
                InlineKeyboardMarkup([[button("🏠 תפריט", "menu:home")]]),
            )
            return True
        # Batch 6: a v2 payload carries the full session identity, so a
        # free-text edit confirmed minutes later still applies to the SAME
        # session -- re-validated here, immediately before the write. A
        # regenerated plan refuses rather than retargeting the edit. Legacy
        # code-only payloads (v absent) keep the old positional behavior
        # untouched until Batch 7 retires them.
        if int(payload.get("v") or 0) >= 2:
            applied_v2 = await _apply_workout_param_text_v2(query, user_id, payload, pending_updates)
            if applied_v2:
                return True
            # _apply_workout_param_text_v2 already rendered a refusal.
            return True

        plan = await get_user_plan(user_id, code)
        active_plan = await user_model.get_value(DB, user_id, "active_workout_plan")
        program_codes = []
        if isinstance(active_plan, dict):
            program_codes = [str(s.get("code")) for s in active_plan.get("sessions", []) if s.get("code")]
        program_codes = list(dict.fromkeys(program_codes or [code]))
        applied: list[str] = []
        affected_count = 0
        for update_item in pending_updates:
            if not isinstance(update_item, dict):
                continue
            scope = str(update_item.get("scope") or "current")
            field = str(update_item.get("field") or "")
            target_codes = program_codes if scope == "program" else [code]
            for target_code in target_codes:
                target_plan = await get_user_plan(user_id, target_code)
                indices = range(len(target_plan["exercises"])) if scope in {"all", "program"} else [exercise_index]
                for idx in indices:
                    if not 0 <= idx < len(target_plan["exercises"]):
                        continue
                    if field == "reps":
                        rmin = int(update_item.get("rmin") or target_plan["exercises"][idx]["rmin"])
                        rmax = int(update_item.get("rmax") or target_plan["exercises"][idx]["rmax"])
                        await set_exercise_override(user_id, target_code, idx, "rmin", rmin)
                        await set_exercise_override(user_id, target_code, idx, "rmax", max(rmin, rmax))
                        affected_count += 1
                    elif field in {"weight", "sets", "rest"}:
                        await set_exercise_override(user_id, target_code, idx, field, float(update_item["value"]))
                        affected_count += 1
            label = str(update_item.get("label") or field)
            if label:
                applied.append(label)
        await conversation.clear_active_flow(DB, user_id)
        summary = ", ".join(applied) if applied else "השינוי"
        scope_label = str(payload.get("scope_label") or "")
        if affected_count:
            scope_label = f"{scope_label} ({affected_count} תרגילים הושפעו)"
        await safe_edit(
            query,
            f"שמרתי: {summary} {scope_label} ✅",
            InlineKeyboardMarkup([
                [button("⬅️ חזרה לאימון", f"workout:{code}")],
                [button("✏️ ערוך עוד", f"editparams_menu:{code}")],
            ]),
        )
        return True

    # Batch 7: legacy edit callbacks route through the adapter first, so an
    # old "edit parameters" button targets the exercise by verified identity
    # in the user's CURRENT plan instead of a bare (code, index) pair. Only a
    # plan-less user (template_fallback) reaches the preserved legacy paths.
    if data.startswith(("editparams_menu:", "editparams:", "param:")):
        if await _handle_legacy_workout_callback(query, context, user_id, data):
            return True

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
        # Batch 4: the `wk:` graph is tried before the legacy `workout:`/
        # `startworkout:` handlers. The prefixes are disjoint, so ordering is
        # a readability choice, not a correctness one -- legacy callbacks
        # from old Telegram messages keep reaching their existing handler
        # untouched until the Batch 7 adapter.
        _handle_workout_v2_actions,
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

        await _clear_pending_plan_action(user_id)
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

    if data == "planv2:complete_missing" or data.startswith("planv2:complete_missing:"):
        from noam_coach.bot.onboarding import ask_next_plan_completion_question

        # RE10-6: carry which plan the user was trying to build so the
        # completion questions prioritize that profile first.
        plan_type = data.split(":", 2)[2] if data.count(":") >= 2 else None
        await ask_next_plan_completion_question(query, user_id, plan_type)
        return True

    if data.startswith("planv2:generate:"):
        plan_type = data.split(":", 2)[2]
        if plan_type not in {"nutrition", "workout"}:
            await safe_answer_callback(query, "סוג תוכנית לא תקין", show_alert=True)
            return True
        readiness = await user_model.compute_readiness(DB, user_id, plan_type)
        # RE10-8: a nutrition plan also requires an approved goal before it can
        # be built (see build_nutrition_candidates). That prerequisite used to
        # surface only on a second screen, after the user completed the facts
        # below — show it here too so the FIRST screen already lists everything
        # that blocks the plan.
        needs_goal = plan_type == "nutrition" and await planning.active_goal(DB, user_id) is None
        if needs_goal:
            # Remember what the user was trying to build so approving the goal
            # (menu:goal) can auto-resume generation, same as the old
            # PlanningBlockedError(missing=["active_goal"]) path did.
            await _set_pending_plan_action(user_id, plan_type)
        if not readiness["ready"] or needs_goal:
            labels = [
                user_model.FACT_REGISTRY[key].label
                if key in user_model.FACT_REGISTRY
                else key
                for key in readiness["missing"]
            ]
            # REC-ONBOARD-02-10: grouped missing info with direct completion
            missing_display = list(readiness.get("missing_labels", labels))
            if needs_goal and readiness["ready"]:
                missing_display.append(user_model.display_label("active_goal"))
            lines_m = [f"<b>חסרים פרטים ליצירת תוכנית {user_model.display_label(plan_type)}:</b>", ""]
            lines_m.extend(f"• {esc(label)}" for label in missing_display)
            deferred = readiness.get("deferred", [])
            if deferred:
                lines_m.append("")
                lines_m.append("<i>נדחו למילוי מאוחר:</i>")
                lines_m.extend(f"• {esc(user_model.display_label(k))}" for k in deferred)
            buttons_m = [[button("▶️ השלם עכשיו", f"planv2:complete_missing:{plan_type}")]]
            buttons_m.append([button("⏳ אשלים אחר כך", "menu:smartplan")])
            buttons_m.append([button("⬅️ חזרה לתוכניות", "menu:smartplan")])
            await safe_edit(
                query,
                "\n".join(lines_m),
                InlineKeyboardMarkup(buttons_m),
            )
            return True
        await safe_edit(query, "בונה שלוש חלופות ובודק אותן מול המגבלות שלך… ⏳", None)
        try:
            generated = await planning.generate_candidates(DB, user_id, plan_type)  # type: ignore[arg-type]
            await event_log.append_event(
                DB,
                user_id,
                "PLAN_CANDIDATES_GENERATED",
                entity="plan",
                source="planner",
                # TASK-19: report the real number of generated candidates, not a
                # hardcoded 3, so the event log never overstates the count.
                properties={"plan_type": plan_type, "count": len(generated or [])},
            )
            if plan_type == "workout":
                from noam_coach.bot.onboarding import render_workout_type_choice

                # RE10-11: workouts go through the 3-step wizard (type ->
                # structure -> exercises) instead of the flat 3-candidate list.
                await render_workout_type_choice(query, user_id)
            else:
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

    if data.startswith("planv2:wiz_type:"):
        from noam_coach.bot.onboarding import render_workout_structure_choice

        strategy = data.split(":", 2)[2].split(":")[0]  # strip trailing f.../v... suffix
        await render_workout_structure_choice(query, user_id, strategy)
        return True

    if data.startswith("planv2:wiz_back_type:"):
        from noam_coach.bot.onboarding import render_workout_type_choice

        await render_workout_type_choice(query, user_id)
        return True

    if data.startswith("planv2:wiz_review:"):
        from noam_coach.bot.onboarding import render_workout_exercise_review

        raw_id = data.split(":", 2)[2].split(":")[0]
        if not raw_id.isdigit():
            return True
        await render_workout_exercise_review(query, user_id, int(raw_id))
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
            rows.insert(0, [button("🗓️ השבוע שלי", "planv2:my_week")])
            extra = "\n\nשתי התוכניות נבחרו — אפשר עכשיו לראות את השבוע המאוחד שלך."
        await safe_edit(
            query,
            f"<b>{esc(selected['title'])}</b> נבחרה כתוכנית {_plan_type_label(selected['plan_type'])} הראשית ✅{extra}",
            InlineKeyboardMarkup(rows),
        )
        if selected["plan_type"] == "nutrition" and getattr(query, "message", None) is not None:
            with suppress(Exception):
                from noam_coach.services.daily_menu_state import remember_daily_menu_message

                menu_text = await build_morning_menu_text(user_id)
                menu_keyboard = InlineKeyboardMarkup([
                    [button("🔄 רענן תפריט", "menu:refresh_daily_menu"), button("🍽 מה לאכול עכשיו", "menu:nextmeal")],
                    [button("✏️ החלף ארוחה", "menu:replace_daily_meal"), button("📊 מצב היום", "menu:status")],
                ])
                sent = await query.message.reply_text(
                    menu_text,
                    reply_markup=menu_keyboard,
                    parse_mode=ParseMode.HTML,
                )
                await remember_daily_menu_message(
                    DB,
                    user_id,
                    chat_id=getattr(getattr(sent, "chat", None), "id", user_id),
                    message_id=getattr(sent, "message_id", None),
                    source="nutrition_strategy_selected",
                )
        return True

    # TASK-17: one weekly-plan action.  "planv2:my_week" shows the current week
    # when it is still valid, builds it when missing, and rebuilds it only when
    # the underlying nutrition/workout plans changed.  "planv2:unify" and
    # "planv2:show:unified" remain as backward-compatible aliases for any stale
    # keyboard still on-screen.
    if data in ("planv2:my_week", "planv2:unify", "planv2:show:unified"):
        try:
            existing = await planning.get_active_plan(DB, user_id, "unified")
            nutrition = await planning.get_active_plan(DB, user_id, "nutrition")
            workout = await planning.get_active_plan(DB, user_id, "workout")
            if planning.unified_week_is_current(existing, nutrition, workout):
                # Valid week already exists — display it without regenerating.
                await render_unified_plan(query, user_id)
                return True
            await safe_edit(query, "מחבר את התזונה, האימונים והשעות לשבוע אחד… ⏳", None)
            candidate, rebuilt = await planning.get_or_build_unified_week(DB, user_id)
            if rebuilt:
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
