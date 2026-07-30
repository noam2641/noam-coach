# ruff: noqa: F401, E402, I001
from __future__ import annotations

"""Compatibility facade and application composition root.

The implementation lives in the structured ``noam_coach`` package. This module
keeps the historical public API stable for deployments, scripts and tests while
assembling the FastAPI and Telegram applications in one predictable place.
"""

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
    assert_safe_database_path,
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


from noam_coach.services.core import (
    admin_chat_id,
    track_event,
    set_flow_state,
    get_flow_state,
    clear_flow_state,
    set_meal_fix,
    get_meal_fix,
    clear_meal_fix,
    notify_admin,
    write_audit,
    ensure_user_record,
    ensure_user,
    create_approval,
    fetch_approval,
    decide_approval,
)


# ---- Meal-fix flow state (DB-backed so it survives a restart) ----


from exercise_plans import (  # noqa: F401, E402
    EXERCISE_MUSCLES,
    MAX_FREQUENCY,
    MIN_FREQUENCY,
    OVERRIDE_FIELDS,
    PLANS,
    SPLIT_BY_FREQUENCY,
    WEEKDAY_NAMES_HE,
    exercise,
    weekday_he,
)


from noam_coach.services.profile import (
    set_exercise_override,
    get_user_plan,
    analyze_meal_image,
    analyze_meal_text,
    _QUANTITY_RE,
    _enforce_user_quantities,
    reanalyze_meal_with_text_and_image,
    extract_daily_routine,
    format_routine_confirmation,
    save_routine_extraction,
    ROUTINE_ESTIMATE_KEYS,
    confirm_routine_facts,
    discard_unconfirmed_routine_facts,
)


from noam_coach.services.training import (
    LoadRecommendation,
    active_session,
    RIR_UNKNOWN,
    _rir_known,
    _known_rirs,
    format_load_decision_details,
    recommend_load,
    recommend_load_decision,
    _SLEEP_FLAG_TO_ENGINE,
    _ENERGY_FLAG_TO_ENGINE,
    build_fatigue_assessment,
    fatigue_banner,
)


# RIR is "reps in reserve" the user reports after a set. The DB column is NOT
# NULL, so we use -1 as an explicit "unknown / not reported" sentinel instead of
# inventing a value. Progression logic must treat unknown RIR as unknown — never
# as a real RIR (P0: the system must not fabricate RIR).


# Map the bot's daily-flag vocabulary to the words the fatigue engine expects.


from noam_coach.bot.ui import (
    button,
    session_action_data,
    SESSION_SCOPED_ACTIONS,
    is_current_session_step,
    session_action_arg,
    update_session_step,
    home_keyboard,
    home_keyboard_for_user,
    _resolve_home_action,
    _home_hint,
    more_keyboard,
    plans_keyboard,
    onboarding_frequency_keyboard,
    workout_overview_keyboard,
    exercise_picker_keyboard,
    exercise_params_keyboard,
    select_todays_workout_code,
    render_workout_overview,
    constraint_banner,
    render_exercise_params,
    safe_answer_callback,
    safe_edit,
    safe_message_edit,
)


from noam_coach.bot.onboarding import (
    is_allowed,
    command_start,
    onboarding_open_keyboard,
    start_onboarding,
    compute_basics_extras,
    show_onboarding_basics,
    show_onboarding_patterns,
    ask_next_question,
    PENDING_QUESTION,
    CONFIRM_PENDING,
    set_confirm_pending,
    clear_confirm_pending,
    set_pending,
    clear_pending,
    load_pending_state,
    handle_onboarding_callback,
    context_pending_fix,
    handle_safety_answer,
    save_medical_constraint,
    resolve_medical_constraints,
    active_constraints,
    PlanConstraint,
    gather_plan_constraints,
    format_constraints_summary,
    _format_fact_value,
    build_profile_text,
    command_cancel,
    command_profile,
    command_profile_query,
    _plan_type_label,
    _format_candidate,
    render_smart_plan_hub,
    render_candidate_list,
    render_profile_snapshot,
    render_unified_plan,
    render_plan_builder,
    ask_deferred_for_plan,
    check_plan_readiness,
    build_weekly_plan,
    format_weekly_plan,
    _CANCEL_WORDS,
    handle_onboarding_text,
    apply_basics_fix,
    finish_onboarding,
    reconcile_onboarding_stage,
)


# Tracks which question (if any) we're waiting on per user (free-text answers).
# Backed by the DB (key 'pending_prompt') so it survives a restart mid-flow.


# ---------------------------------------------------------------------------
# Constraint engine — hard/soft constraints for plan generation
# ---------------------------------------------------------------------------


# ---- Constraint-driven nutrition/workout planning ----


# ---- Legacy schedule-aware workout plan generation ----


from noam_coach.bot.workout import (
    today_meals,
    build_daily_status,
    render_post_meal_confirmation_day_status,
    show_session,
    save_set,
    undo_last_set,
    _StaleSetStep,
    try_save_set,
    workout_summary,
    WEIGHT_TEXT_STEP,
    await_weight_text,
    clear_weight_text_flow,
    previous_weight_context,
    record_load_type_hint,
    reps_prompt_keyboard,
    stored_load_type,
    handle_weight_text,
)
from noam_coach.services.weight_text import (
    parse_weight_text,
    format_weight_confirmation,
    weight_prompt_text,
    INVALID_WEIGHT_TEXT,
)


from noam_coach.bot.meals import (
    create_meal_edit_approval,
    analyze_duplicate_candidate,
    handle_photo,
    quantity_menu_keyboard,
    render_quantity_editor,
    _AlreadyDecided,
    check_duplicate_meal,
    persist_meal,
    should_auto_approve,
    auto_save_meal,
    render_meal,
)


# ---- Split-set flow state (DB-backed so it survives a restart mid-set) ----


from noam_coach.bot.workout_runtime import (
    _split_flow,
    get_split_state,
    set_split_state,
    clear_split_state,
    split_weight_keyboard,
    split_reps_keyboard,
    split_rir_keyboard,
    split_summary_line,
    save_split_set,
    effort_to_rir,
    rest_job_name,
    rest_keyboard,
    rest_text,
    resolve_rest_next_action,
    cancel_rest_timer,
    update_rest_message,
    _MessageEditTarget,
    rest_timer_tick,
    start_rest_timer,
    restore_rest_timers_on_startup,
)


# Debounce: drop a repeated identical callback from the same user within this
# window, ONLY for actions where a fast double-tap would apply the mutation
# twice (e.g. "-25 גרם", "+30 שניות", approving a meal). Actions like logging a
# set ("setok:") legitimately repeat with identical callback_data across sets,
# so they must NOT be debounced.

from noam_coach.bot.callbacks import (
    _LAST_CALLBACK,
    CALLBACK_DEBOUNCE_SECONDS,
    _DEBOUNCE_PREFIXES,
    _is_duplicate_tap,
    handle_callback,
    handle_goal_callback,
    handle_menu_callback,
    handle_flags_callback,
    handle_workout_setup_callback,
    handle_plan_callback,
    handle_meal_callback,
    handle_session_action_callback,
    on_error,
)


api = FastAPI(title="Noam Coach", version=APP_VERSION, docs_url=None, redoc_url=None)

import mini_api  # noqa: E402

api.include_router(mini_api.router)


from noam_coach.api.health_routes import (
    health_auth,
    healthkit_samples,
    insert_health_sample,
    router as health_router,
    shortcut_health,
)
from noam_coach.api.security import (
    API_RATE_LIMITER,
    APIBodyLimitMiddleware,
    FixedWindowRateLimiter,
    install_security,
    protect_private_api,
)
from noam_coach.api.system_routes import healthz, readyz, router as system_router
from noam_coach.api.watch_routes import router as watch_router, watch_current, watch_set

install_security(api)
api.include_router(system_router)
api.include_router(health_router)
api.include_router(watch_router)

# Mini login replay protection is process-local by design. The deployment uses
# one application process while Telegram polling and SQLite are active.
USED_MINI_LOGIN_TOKENS: dict[str, int] = {}
MINI_LOGIN_LOCK = asyncio.Lock()


from noam_coach.bot.meal_text import (
    _handle_meal_correction_text,
    handle_text_message,
)


# ---------------------------------------------------------------------------
# Health services extracted to health_service.py — re-exported here
# ---------------------------------------------------------------------------
from health_service import (  # noqa: E402
    HEALTH_BATCH_SIZE,  # noqa: F401
    all_medication_names,
    get_daily_flags,
    known_medications,
    load_routine_profile,
    local_day_str,
    medication_name_from_text,
    record_medication,
    save_routine_profile,
    set_daily_flags,
    sync_health_measurements_to_facts,
    sync_routine_to_facts,  # noqa: F401
    upsert_health_rows,
)


from noam_coach.bot.checkins import (
    handle_checkin_callback,
    build_health_status_text,
    build_now_action_text,
)


from noam_coach.jobs.proactive import (
    JOB_PRIORITY_LOW,
    JOB_PRIORITY_COACHING,
    JOB_PRIORITY_SCHEDULED,
    JOB_PRIORITY_HIGH,
    JOB_PRIORITY_URGENT,
    JobDeliveryClaim,
    _clock_minutes,
    _within_quiet_hours,
    claim_job_delivery,
    complete_job_delivery,
    fail_job_delivery,
    schedule_job_retry,
    deliver_proactive_message,
    today_consumed,
    today_meal_items,
    workout_completed_today,
    is_usual_workout_day,
    today_has_workout,
    hours_left_until_sleep,
    DailyContext,
    build_daily_context,
)


# ---------------------------------------------------------------------------
# DailyContext — one consistent snapshot of "today" that every consumer shares,
# so two screens never disagree (one thinks there's a workout, another doesn't).
# Reported data wins over historical (Apple Health is retrospective).
# ---------------------------------------------------------------------------


from noam_coach.services.goals import (
    VALID_GOAL_STRATEGIES,
    _goal_type_from_fact,
    set_goal_weight,
    GOAL_STATUS_PROVISIONAL,
    GOAL_STATUS_PROPOSED,
    GOAL_STATUS_APPROVED,
    GOAL_STATUS_ACTIVE,
    GOAL_STATUS_ACTIVE_PROVISIONAL,
    GOAL_STATUS_SUPERSEDED,
    create_goal_version,
    activate_goal_version,
    activate_goal_version_provisional,
    get_active_goal_version,
    get_goal_history,
    compute_personal_targets,
    target_calories,
    target_change_note,
    fetch_goal,
    format_morning_menu,
    format_next_meals,
    format_evening_summary,
)


# ---------------------------------------------------------------------------
# Goal versioning — provisional → proposed → approved → active → superseded
# ---------------------------------------------------------------------------

# A goal the user chose to use while mandatory data is still missing. It is the
# current goal for display, but is flagged so strong alerts treat it cautiously.


# ---- Commands ----


from noam_coach.services.health_jobs import (
    command_import,
    command_import_path,
    command_flags,
    handle_document,
    try_handle_local_health_path,
    import_health_export_file,
    run_post_import_reconciliation,
    apply_reconcile_proposal,
    send_to_user,
    morning_checkin_keyboard,
    job_morning,
    job_evening,
    job_calorie_watch,
    job_motivation,
    _ctx_has_workout,
    _data_quality_disclaimer,
    build_morning_menu_text,
    build_morning_briefing_text,
    build_next_meal_text,
    build_evening_summary_text,
)


# ---- Proactive jobs ----


# ---- On-demand builders (shared by jobs and menu buttons) ----


from noam_coach.bot.assistant import (
    assistant_profile_summary,
    route_free_text,
    render_workout_overview_reply,
    build_progress_text,
    _clean_pref_item,
    record_dietary_preference,
    log_meal_from_text,
    build_workout_prompt_text,
    build_weekly_summary_text,
    send_weight_chart,
    command_weekly,
    command_chart,
)


from noam_coach.api.mini_auth import (
    _mini_secret,
    make_mini_token,
    verify_mini_token,
    consume_mini_login_token,
    _BLOCKED_HOSTS,
    _LOCAL_HOSTS,
    _is_valid_public_url,
    mini_app_url,
    command_app,
)


from noam_coach.app.runtime import (
    job_workout_prompt,
    job_weekly_summary,
    schedule_jobs,
    build_telegram_app,
    verify_bot_identity,
    run,
)


# --- Mini App API endpoints extracted to mini_api.py ---
# Re-export for backward compatibility with tests:
mini_update_profile = mini_api.mini_update_profile


if __name__ == "__main__":
    # Register this module under its canonical name so that
    # runtime_bind._sync can resolve names from the facade.
    import sys as _sys
    _sys.modules.setdefault("coach_bot", _sys.modules[__name__])
    asyncio.run(run())
