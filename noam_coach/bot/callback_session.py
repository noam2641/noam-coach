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

RUNTIME_NAMES = ('APP_VERSION', 'Any', 'CALLBACK_DEBOUNCE_SECONDS', 'CONFIRM_PENDING', 'ContextTypes', 'DB', 'EXERCISE_MUSCLES', 'Exception', 'GOAL_STATUS_PROPOSED', 'GOAL_STATUS_PROVISIONAL', 'InlineKeyboardButton', 'InlineKeyboardMarkup', 'KeyError', 'LOGGER', 'MealAnalysis', 'PENDING_QUESTION', 'Path', 'RIR_UNKNOWN', 'SESSION_SCOPED_ACTIONS', 'SETTINGS', 'TypeError', 'Update', 'ValueError', 'WebAppInfo', '_DEBOUNCE_PREFIXES', '_LAST_CALLBACK', '_StaleSetStep', '_duration_s', '_home_hint', '_is_duplicate_tap', '_plan_type_label', '_started', 'action', 'activate_goal_version_provisional', 'active_flow', 'active_session', 'actual_reps', 'actual_rir', 'actual_weight', 'aiosqlite', 'alt', 'alt_muscle', 'alternative', 'alternative_index', 'analysis', 'analyze_duplicate_candidate', 'apply_reconcile_proposal', 'approval_id', 'await_weight_text', 'bool', 'build_daily_status', 'build_evening_summary_text', 'build_health_status_text', 'build_morning_menu_text', 'build_next_meal_text', 'build_now_action_text', 'build_weekly_summary_text', 'button', 'buttons', 'callback_flow_id', 'callback_version', 'cancel_rest_timer', 'candidate', 'center', 'changed', 'check_duplicate_meal', 'choices', 'chosen_reps', 'chosen_weight', 'claimed', 'clear_confirm_pending', 'clear_meal_fix', 'clear_pending', 'clear_split_state', 'code', 'command_profile_query', 'completed', 'conn', 'connection', 'constraint_id', 'context', 'conversation', 'create_approval', 'create_goal_version', 'create_meal_edit_approval', 'cur', 'current', 'cursor', 'cutoff', 'data', 'datetime', 'decide_approval', 'deleted', 'delta', 'delta_text', 'dict', 'done', 'draft', 'dup', 'duplicate_approval_id', 'edit_approval_id', 'ensure_user', 'enumerate', 'error_id', 'esc', 'event_log', 'ex', 'exc', 'effort_to_rir', 'exercise_index', 'exercise_index_text', 'exercise_picker_keyboard', 'existing', 'extra', 'extra_seconds', 'fetch_approval', 'fetch_goal', 'field', 'final_rir', 'first_reps', 'first_weight', 'flags', 'float', 'format_load_decision_details', 'frequency', 'friendly_error', 'get_daily_flags', 'get_meal_fix', 'get_split_state', 'get_user_plan', 'getattr', 'goal', 'goal_id', 'gv_id', 'handle_checkin_callback', 'handle_flags_callback', 'handle_goal_callback', 'handle_meal_callback', 'handle_menu_callback', 'handle_onboarding_callback', 'handle_plan_callback', 'handle_session_action_callback', 'handle_workout_setup_callback', 'home_keyboard', 'index', 'int', 'is_allowed', 'is_current_session_step', 'is_partial', 'is_provisional', 'isinstance', 'item', 'item_index', 'item_index_text', 'job', 'jobs', 'json', 'k', 'kb', 'key', 'kind', 'label', 'labels', 'last', 'len', 'level', 'list', 'logged_sets', 'max', 'meal', 'meal_id', 'meal_id_text', 'min', 'mini_app_url', 'missing', 'missing_labels', 'more_keyboard', 'msg', 'new_grams', 'new_max', 'new_min', 'new_val', 'new_weight', 'note', 'notify_admin', 'now', 'nutrition', 'object', 'ok', 'old_grams', 'option', 'option_index', 'pain_location', 'part', 'parts', 'parts_v2', 'payload', 'persist_meal', 'plan', 'plan_id', 'plan_now', 'plan_type', 'planned_sets', 'planning', 'plans_keyboard', 'previous_weight_context', 'progress', 'quality', 'query', 'range', 'ratio', 'rc', 'readiness', 'recommend_load', 'recommend_load_decision', 'refreshed', 'render_candidate_list', 'render_exercise_params', 'render_meal', 'render_profile_snapshot', 'render_quantity_editor', 'render_smart_plan_hub', 'render_unified_plan', 'render_workout_overview', 'reopened', 'replacement', 'reps', 'reps_value', 'rest', 'rest_job_name', 'result', 'round', 'route_decision', 'row', 'rows', 'safe_edit', 'save_medical_constraint', 'save_split_set', 'second_base', 'second_reps', 'second_weight', 'secrets', 'select_todays_workout_code', 'selected', 'send_weight_chart', 'session', 'session_action_arg', 'session_action_data', 'session_id', 'set', 'set_daily_flags', 'set_exercise_override', 'set_goal_weight', 'set_meal_fix', 'set_pending', 'set_split_state', 'severity', 'show_session', 'split_reps_keyboard', 'split_rir_keyboard', 'split_state', 'split_summary_line', 'split_weight_keyboard', 'start_rest_timer', 'status', 'status_line', 'step', 'str', 'sum', 'summary_line', 'suppress', 't', 'tail', 'target_change_note', 'text', 'time', 'total_reps', 'track_event', 'training_intelligence', 'try_save_set', 'tuple', 'undo_last_set', 'undone', 'update', 'update_rest_message', 'update_session_step', 'url', 'user_choice', 'user_id', 'user_model', 'utc_now', 'value', 'value_text', 'warn', 'weight', 'weight_prompt_text', 'workout', 'workout_summary', 'write_audit')

async def _reject_unhandled_callback(query: Any, user_id: int, data: str) -> None:
    """Terminal handler for a callback that matched nothing anywhere.

    ``handle_session_action_callback`` is the END of the dispatch chain, so
    anything reaching it that is not a session-scoped action matched no
    handler at all. It used to ``return`` bare -- no reply, no event, no log
    -- which made a mis-wired button indistinguishable from a working one
    both to the user (the Telegram message did not even change, so it read as
    a frozen app) and to us (the event stream records routing.decided BEFORE
    dispatch and never records the outcome).

    That is how a singular/plural typo in one callback name -- the bot's own
    top-priority CTA -- went unnoticed while it blocked every nutrition
    feature in the product.
    """
    await _emit_unhandled_callback(user_id, data)
    await safe_answer_callback(
        query,
        "הכפתור הזה לא זמין כרגע — נסה דרך התפריט.",
        show_alert=False,
    )


async def _emit_unhandled_callback(user_id: int, data: str) -> None:
    """Record a callback that matched no handler.

    Without this the only trace of a dead button is an absence -- a
    routing.decided with no render after it -- which is invisible to any
    query. Emitting it makes "this button does nothing" a fact you can
    search for instead of a pattern someone has to notice.

    Only the callback PREFIX is recorded: the tail can carry ids.
    """
    with suppress(Exception):
        from noam_coach.observability import taxonomy
        from noam_coach.observability.emit import emit_event

        await emit_event(
            DB,
            user_id,
            taxonomy.DECISION_FALLBACK_SELECTED,
            entity="callback",
            entity_id=(data or "").split(":")[0] or "unknown",
            source="bot",
            status="failed",
            outcome="unhandled_callback",
            properties={
                "domain": "callback_routing",
                "reason": "no_handler_matched",
                "callback_prefix": (data or "").split(":")[0],
            },
        )


async def _emit_session_event(
    user_id: int,
    session_id: int,
    action: str,
    **props: Any,
) -> None:
    """Session-lifecycle observability, mirroring ``_emit_rest_event``.

    Skipping an exercise and reporting pain both mutate a live workout, and
    neither left any trace: in the 2026-07-26 session a skip produced no
    event at all, and a pain report reached ``medical_constraints`` and
    ``audit`` but no domain event, so neither is reconstructable from the
    canonical event stream.

    Emission is best-effort by contract -- observability must never break a
    workout in progress.
    """
    with suppress(Exception):
        from noam_coach.observability import taxonomy
        from noam_coach.observability.emit import emit_event

        await emit_event(
            DB,
            user_id,
            taxonomy.STATE_MUTATED,
            entity="workout_session",
            entity_id=session_id,
            source="workout",
            status="mutated",
            outcome=action,
            properties={
                "domain": "workout_session",
                "action": action,
                "session_id": session_id,
                **props,
            },
        )


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
            await safe_answer_callback(query, "הסט האחרון בוטל", show_alert=False)
        else:
            await safe_answer_callback(query, "אין סט לביטול", show_alert=False)
        await show_session(query, user_id, session_id)
        return True

    if action == "seteffort":
        # Reported from the rest screen, after the row is committed and the
        # pointer has already advanced, so this can only ever be an UPDATE of a
        # set that is already durable. That is what makes the CTA non-blocking
        # by construction rather than by convention.
        parts_effort = parts[4:]
        if len(parts_effort) < 2:
            return True
        try:
            set_id = int(parts_effort[0])
        except (TypeError, ValueError):
            return True
        rir_value = effort_to_rir(str(parts_effort[1]))
        if rir_value is None:
            # An unrecognised token is dropped rather than defaulted: an
            # invented RIR feeds progression, an absent one does not.
            return True

        # Ownership is re-checked against the session join, never trusted from
        # the callback -- set_id arrives from the client. The RIR_UNKNOWN guard
        # makes a double-tap idempotent and stops a late tap from overwriting a
        # number the user gave explicitly on the set card.
        updated = await DB.execute_rowcount(
            """
            UPDATE sets SET rir=?
            WHERE id=? AND rir=?
              AND session_id IN (SELECT id FROM sessions WHERE id=? AND user_id=?)
            """,
            (rir_value, set_id, RIR_UNKNOWN, session_id, user_id),
        )
        if updated:
            await safe_answer_callback(query, "נרשם, תודה", show_alert=False)
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

# ---------------------------------------------------------------------------
# Substitution identity and reason (A11b)
#
# Two facts drove this design, both measured:
#
# 1. `current["alts"].index(alt)` matched by VALUE. Two alternatives with equal
#    content resolved to the same index, and a re-ranked list redirected a tap
#    to a different-but-valid exercise. The bounds check at the handler passed,
#    so the wrong substitution applied silently.
# 2. The equipment path and the pain path minted BYTE-IDENTICAL callbacks. By
#    the time the handler ran, "my knee hurts" and "the rack was busy" were
#    indistinguishable -- so A12 could not tell a safety-driven change from a
#    convenience one, and would promote the wrong pattern.
#
# Both are fixed at the minting site, because that is the only place where the
# reason still exists and where the alternative's identity is unambiguous.
# ---------------------------------------------------------------------------
#: Bounded reason codes. Never free text, never a body region -- the region is
#: already recorded on the medical_constraints row that the pain flow writes.
SUB_REASON_PAIN = "pain"
SUB_REASON_EQUIPMENT = "equipment"
SUB_REASON_UNKNOWN = "unspecified"
SUB_REASONS = frozenset({SUB_REASON_PAIN, SUB_REASON_EQUIPMENT, SUB_REASON_UNKNOWN})


def _substitution_callback(
    session: dict[str, Any],
    current: dict[str, Any],
    alternative: dict[str, Any],
    reason: str,
) -> str:
    """Callback data for one substitution offer.

    Grammar: `sub:<session_id>:<exercise_index>:<set_number>:<alt_id>:<reason>`

    Three constraints the grammar has to respect, each verified:

    * field 1 stays numeric, or `handle_session_action_callback` rejects the
      callback before any handler sees it;
    * the terminal fields must never match `^v\\d{1,9}$`, or the strict grammar
      reads one as a flow version and the router refuses the tap as stale. The
      reason codes are alphabetic, so the last field is structurally safe;
    * total length stays under Telegram's 64-byte limit -- exercise ids are
      short slugs, and this is asserted by test rather than assumed.
    """
    alt_id = str(alternative.get("id") or "")
    return session_action_data(
        "sub",
        session,
        alt_id,
        reason if reason in SUB_REASONS else SUB_REASON_UNKNOWN,
    )


@runtime_bound(RUNTIME_NAMES)
async def _pain_safe_alternatives(
    user_id: int, current: dict[str, Any], *, fallback_when_all_blocked: bool = True
) -> list[dict[str, Any]]:
    """Filter current["alts"] to ones that don't also load an active pain
    region, so a substitution never trades one painful movement for another
    just because they share the same target muscle (e.g. a row/pull-up is
    not automatically a safe swap for elbow pain even though the target
    muscle is back).

    Alternatives with no CATALOG entry (most of the plan's hand-authored
    alts, which use plan-only ids like "hack"/"hip_thrust") are always kept,
    since there's no joint_load data to filter them by — this only narrows
    the list, it never invents new exercises or drops everything.
    """
    alts = current.get("alts") or []
    rows = await DB.fetch_all(
        "SELECT * FROM medical_constraints WHERE user_id=? AND kind='pain'",
        (user_id,),
    )
    if not rows:
        return alts
    pain_regions = set(training_intelligence.active_pain_regions(rows))
    if not pain_regions:
        return alts
    safer = []
    for alt in alts:
        profile = training_intelligence.CATALOG.get(str(alt.get("id")))
        if profile is not None and pain_regions.intersection(profile.joint_load):
            continue
        safer.append(alt)
    if safer:
        return safer
    if fallback_when_all_blocked:
        # Equipment-occupied is not a pain report; keep the older fallback
        # there so the user still gets practical swaps when no pain is active.
        return alts
    return []


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
    if action == "loadwhy":
        decision = await recommend_load_decision(user_id, current)
        await safe_edit(
            query,
            format_load_decision_details(decision),
            InlineKeyboardMarkup(
                [[button("↩️ חזרה לאימון", session_action_data("ready", session))]]
            ),
        )
        return True

    if action == "different":
        # TASK-WORKOUT-WEIGHT-TEXT: the predefined weight-SELECTION buttons are
        # gone. The user TYPES the load actually lifted; the free-text answer is
        # consumed by the workout_session flow (meal_text.handle_text_message).
        # Navigation and safety controls below are deliberately preserved.
        await await_weight_text(user_id, session)
        await safe_edit(
            query,
            weight_prompt_text(*(await previous_weight_context(user_id, session, current))),
            InlineKeyboardMarkup(
                [
                    [button("↩️ חזרה לאימון", session_action_data("ready", session))],
                    [button("סיים", session_action_data("finish", session))],
                ]
            ),
        )
        return True

    if action == "weight":
        # TASK-WORKOUT-WEIGHT-TEXT: no keyboard mints `weight:` any more — the
        # load is typed. This branch survives ONLY to absorb a tap on a stale
        # pre-upgrade keyboard still sitting in a user's chat history: it
        # re-asks with the text prompt instead of applying a delta, so an old
        # button can never silently write a load the user did not report.
        await await_weight_text(user_id, session)
        await safe_edit(
            query,
            weight_prompt_text(*(await previous_weight_context(user_id, session, current))),
            InlineKeyboardMarkup(
                [
                    [button("↩️ חזרה לאימון", session_action_data("ready", session))],
                    [button("סיים", session_action_data("finish", session))],
                ]
            ),
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
            "כמה חזרות הרגשת שנשארו? (RIR)",
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
        safe_alts = await _pain_safe_alternatives(user_id, current)
        # A11b: identity, not position. `current["alts"].index(alt)` matched by
        # VALUE, so two alternatives with equal content collapsed to the first
        # index and a re-ranked list redirected the tap to a different — valid,
        # therefore undetectable — exercise. The callback now carries the
        # alternative's own id, resolved by identity at tap time.
        buttons = []
        for position, alt in enumerate(safe_alts[:3], start=1):
            alt_muscle = EXERCISE_MUSCLES.get(alt["id"], current.get("muscle", ""))
            label = f"{position}. {alt['name']}"
            if alt_muscle:
                label += f" ({alt_muscle})"
            buttons.append([
                button(
                    label,
                    _substitution_callback(session, current, alt, SUB_REASON_EQUIPMENT),
                )
            ])
        buttons.append([button("דלג", session_action_data("skip", session))])
        await safe_edit(query, "<b>שלוש חלופות</b>", InlineKeyboardMarkup(buttons))
        return True

    if action == "sub":
        from noam_coach.services import workout_slots

        # A11b: resolve by IDENTITY. The old code read a position out of the
        # callback and indexed into `current["alts"]`, checking only that the
        # number was in range -- so a re-ranked list of the same length applied
        # a different exercise than the one the user tapped. Matching on the
        # alternative's own id means a list that no longer offers it yields
        # nothing, which is a stale callback: refused, never redirected.
        alt_id = str(session_action_arg(parts))
        try:
            sub_reason = str(session_action_arg(parts, 1))
        except ValueError:
            # A keyboard minted before A11b carries no reason field. Accept the
            # tap -- refusing would strand an in-flight workout -- and record
            # the reason as unspecified rather than guessing one.
            sub_reason = SUB_REASON_UNKNOWN
        if sub_reason not in SUB_REASONS:
            sub_reason = SUB_REASON_UNKNOWN
        alternative = next(
            (
                alt
                for alt in (current.get("alts") or [])
                if isinstance(alt, dict) and str(alt.get("id") or "") == alt_id
            ),
            None,
        )
        if alternative is None:
            await safe_answer_callback(query, "החלופה אינה זמינה", show_alert=False)
            await show_session(query, user_id, session_id)
            return True
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
            # A11b: the reason A12 needs, carried from the button that knew it.
            # Bounded code only -- the body region lives on the
            # medical_constraints row the pain flow already writes, and must not
            # be duplicated into an audit row.
            reason=sub_reason,
            # Slot identity, so a pattern can be keyed on the professional need
            # rather than on whichever exercise happened to implement it.
            slot_id=workout_slots.slot_id_of(current) or "",
        )
        # A11b: say which plan this changed. The swap is written to the SESSION
        # snapshot, so it applies to today's workout and the saved plan is
        # untouched -- deliberately, because `sessions.plan` is what makes a
        # live workout immune to plan edits. Without this line the user cannot
        # tell whether next week is fixed too, and would reasonably assume it is.
        await safe_answer_callback(
            query,
            "החלפתי לאימון הזה. התוכנית הקבועה לא השתנתה.",
            show_alert=False,
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
            await safe_answer_callback(query, "בחירת הכאב אינה תקינה", show_alert=False)
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
                            "1-2 קל",
                            session_action_data("painlevel", session, 1),
                        ),
                        button(
                            "3 זהיר",
                            session_action_data("painlevel", session, 3),
                        ),
                    ],
                    [
                        button(
                            "4-6 לעצור",
                            session_action_data("painlevel", session, 4),
                        ),
                        button(
                            "7-10 חד/חזק",
                            session_action_data("painlevel", session, 7),
                        ),
                    ],
                ]
            ),
        )
        return True

    if action == "painlevel":
        severity = int(session_action_arg(parts))
        if severity < 1 or severity > 10:
            await safe_answer_callback(query, "עוצמת הכאב אינה תקינה", show_alert=False)
            return True
        pain_location = session.get("pain_location")
        if not pain_location:
            await safe_answer_callback(query,
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

        # Emitted only after the transaction commits, so the event can never
        # claim a pain report that was rolled back.
        await _emit_session_event(
            user_id,
            session_id,
            "pain_reported",
            exercise_index=session["exercise_index"],
            exercise_id=current.get("id"),
            set_number=session["set_number"],
            location=pain_location,
            severity=severity,
            constraint_id=constraint_id,
        )

        # Mirror into the canonical planning fact so future plan generation
        # and load recommendations see pain reported mid-workout.
        #
        # This runs AFTER the transaction above has committed, so it cannot be
        # rolled back with it. That makes it a projection, not part of the
        # safety write: `medical_constraints` is what actually gates load and
        # exercise selection, and it is already durable by the time we get here.
        # Left unguarded, a failure here escaped the handler and told the user
        # their pain was not recorded -- while the constraint row said otherwise.
        # A projection failing is not a reason to report a successful pain
        # report as broken, so it is isolated and logged instead.
        # Recomputed from the constraint rows that are CURRENTLY active, rather
        # than appended to. The previous form merged the new region into a
        # free-text string, so regions accumulated with nothing able to remove
        # them: one elbow report and one knee report left the planning fact
        # saying "elbow, knee" permanently. Deriving makes expiry free -- a
        # region that ages out of the 14-day window simply stops appearing --
        # and makes a repeated report idempotent.
        try:
            from noam_coach.services import pain_persistence

            await pain_persistence.sync_training_limitations(DB, user_id)
        except Exception:
            # Divergence is real and must be visible to operators: the
            # constraint is active while planning has not yet learned about it.
            LOGGER.exception(
                "training_limitations mirror failed after pain report "
                "(user=%s constraint=%s location=%s severity=%s); "
                "medical_constraints is authoritative and was written",
                user_id, constraint_id, pain_location, severity,
            )

        if severity >= 4:
            await safe_edit(
                query,
                "<b>כאב 4/10 ומעלה הוא סימן לעצור.</b> אל תמשיך את התרגיל עכשיו. "
                "אם הכאב מתגבר, מופיעה נפיחות, הקרנה או מגבלה בתנועה — כדאי בדיקה מקצועית.",
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
        safe_alts = await _pain_safe_alternatives(
            user_id,
            current,
            fallback_when_all_blocked=False,
        )
        # A11b: identity, not position -- see the equipment path above. The
        # stakes are higher here: the filter exists so a painful movement is not
        # swapped for another painful one, yet the old index pointed into the
        # UNFILTERED list, so a re-rank could resolve an alternative that was
        # never pain-screened.
        offered_alts = list(safe_alts[:3])
        if not offered_alts:
            await safe_edit(
                query,
                "אין לי חלופה מספיק בטוחה לפי הכאב שדיווחת. עדיף לדלג על התרגיל או לסיים את האימון.",
                InlineKeyboardMarkup(
                    [
                        [
                            button("דלג", session_action_data("skip", session)),
                            button("סיים", session_action_data("finish", session)),
                        ]
                    ]
                ),
            )
            return True
        buttons = []
        for alt in offered_alts:
            alt_muscle = EXERCISE_MUSCLES.get(
                alt["id"],
                current.get("muscle", ""),
            )
            label = alt["name"] + (f" ({alt_muscle})" if alt_muscle else "")
            buttons.append(
                [
                    button(
                        label,
                        _substitution_callback(
                            session, current, alt, SUB_REASON_PAIN
                        ),
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
            await _emit_session_event(
                user_id,
                session_id,
                "exercise_skipped",
                exercise_index=session["exercise_index"],
                exercise_id=current.get("id"),
                set_number=session["set_number"],
                was_last_exercise=True,
                session_status=status,
            )
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
        await _emit_session_event(
            user_id,
            session_id,
            "exercise_skipped",
            exercise_index=session["exercise_index"],
            exercise_id=current.get("id"),
            set_number=session["set_number"],
            was_last_exercise=False,
        )
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
        # Audit F-A4: the workout_status truth rule demotes an incomplete
        # workout to "partial" regardless of the button pressed. Offering
        # "✅ סיים מלא" when the sets are incomplete therefore lied — the
        # user pressed "full" and got "partial". When the workout cannot be
        # marked full, the dialog says so and does not offer a false "full".
        all_sets_done = logged_sets >= max(1, planned_sets)
        rows = [[button("⏸️ אמשיך מאוחר יותר", session_action_data("wpause", session))]]
        if all_sets_done:
            warn = ""
            # Every planned set logged → "full" is honest and offered.
            rows.append([button("✅ סיים מלא", session_action_data("wdone", session, "full"))])
            rows.append([button("🟡 סיים חלקי", session_action_data("wdone", session, "partial"))])
        else:
            warn = (
                f"\n\n⚠️ <i>לא בוצעו כל הסטים ({logged_sets}/{planned_sets}) — "
                "האימון ייסמן כ<b>חלקי</b>.</i>"
            )
            # No false "full": the only completion option is the truthful
            # "partial"; the user can also return to finish the remaining sets.
            rows.append([button("🟡 סיים כחלקי", session_action_data("wdone", session, "partial"))])
            rows.append([button("↩️ המשך לסטים שנותרו", session_action_data("ready", session))])
        rows.append([button("❌ בטל אימון", session_action_data("wcancel", session))])
        rows.append([button("↩️ חזרה לאימון", session_action_data("ready", session))])
        await safe_edit(
            query,
            f"<b>לסיים את האימון?</b>\n{progress}{warn}\nאיך לסמן אותו?",
            InlineKeyboardMarkup(rows),
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
        await _reject_unhandled_callback(query, user_id, data)
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
            await safe_answer_callback(query, "כבר יש אימון פעיל", show_alert=False)
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

    # A weight or rep count the user typed during this set OVERRIDES the
    # recommendation. Without this, handlers that trust the seeded values --
    # `setok` above all -- persisted the plan default instead of what the user
    # typed: in the 2026-07-26 session a typed weight was silently replaced by
    # the plan's 50 kg, while show_session displayed the typed value, so the
    # card showed one number and the sets table stored another.
    #
    # This mirrors the override show_session already performs, and the one the
    # `rir` branch does by re-reading the row; doing it once here means every
    # handler sees the user's own input rather than each having to remember.
    if session["pending_weight"] is not None:
        weight = float(session["pending_weight"])
    if session["pending_reps"] is not None:
        reps = int(session["pending_reps"])

    if action in SESSION_SCOPED_ACTIONS and not is_current_session_step(parts, session):
        await safe_answer_callback(query,
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
