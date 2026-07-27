# ruff: noqa: F401, F811, F821, I001
"""Health import commands and scheduled coaching jobs.

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
import routine
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
from noam_coach.services.daily_coaching import (
    calculate_daily_score,
    choose_daily_mission,
    format_daily_mission,
    format_daily_score,
)
from noam_coach.services.meal_followup import planned_meal_followup
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

from noam_coach.bot.ui import safe_message_edit
from noam_coach.runtime_bind import runtime_bound
from noam_coach.services.local_health_path import (
    LocalHealthPathError,
    allowed_roots_from_text,
    looks_like_local_health_path,
    resolve_local_health_export,
)
from noam_coach.services.health_quality import export_freshness_status
from noam_coach.services.nutrition_context import (
    build_nutrition_ai_request,
    build_nutrition_context,
)
from noam_coach.services.weekdays import (
    WEEKDAY_SCHEMA_VERSION,
    local_weekday,
    normalize_weekday,
    sunday_first_order,
    weekday_labels_he,
    with_weekday_schema,
)

RUNTIME_NAMES = ('Any', 'CallbackContext', 'ContextTypes', 'DB', 'Exception', 'InlineKeyboardButton', 'InlineKeyboardMarkup', 'JOB_PRIORITY_COACHING', 'JOB_PRIORITY_HIGH', 'JOB_PRIORITY_LOW', 'JOB_PRIORITY_SCHEDULED', 'LOGGER', 'OPENAI_CLIENT', 'ParseMode', 'Path', 'RuntimeError', 'SETTINGS', 'TZ', 'Update', 'ValueError', '_ctx_has_workout', '_data_quality_disclaimer', 'abs', 'action', 'actual_bytes', 'any', 'asyncio', 'at', 'bool', 'build_daily_context', 'build_evening_summary_text', 'build_morning_briefing_text', 'build_morning_menu_text', 'build_next_meal_text', 'button', 'context', 'conversation', 'ctx', 'current_flow', 'datetime', 'deliver_proactive_message', 'document', 'duplicates', 'ensure_user', 'enumerate', 'esc', 'exc', 'extract_dir', 'fasting_negated', 'flags', 'float', 'folder', 'format_evening_summary', 'format_morning_menu', 'format_next_meals', 'fraction_used', 'friendly_error', 'get_daily_flags', 'health_import', 'hh', 'hhmm', 'hint', 'hour', 'idx', 'inserted', 'insights', 'int', 'is_allowed', 'items', 'job_calorie_watch', 'job_evening', 'job_morning', 'job_motivation', 'keyboard', 'known_medications', 'learned', 'learned_block', 'lines', 'list', 'load_routine_profile', 'local_day_str', 'lowered', 'max_bytes', 'med', 'meds', 'menu', 'message', 'mm', 'moment', 'morning_checkin_keyboard', 'name', 'near', 'notify_admin', 'now', 'onboarding', 'parse_to_rows', 'profile', 'progress', 'random', 're', 'recommendations', 'reconcile', 'resumed', 'ritalin_negated', 'route_decision', 'rows', 'run_post_import_reconciliation', 'safe_message_edit', 'save_routine_profile', 'saved_path', 'secrets', 'send_checkin', 'send_menu', 'send_motivation', 'send_nudge', 'send_overpace', 'send_summary', 'send_to_user', 'sent', 'set_daily_flags', 'show_onboarding_basics', 'shutil', 'sleep', 'snack_hours', 'str', 'suffix', 'suggestion', 'summary', 'suppress', 'sync_health_measurements_to_facts', 'target', 'target_cal', 'telegram_file', 'text', 'today_meal_items', 'top', 'track_event', 'tuple', 'update', 'upsert_health_rows', 'user_id', 'user_model', 'value', 'weekly', 'window', 'workout', 'workout_hour', 'write_audit', 'x', 'xml_path')


@dataclass(frozen=True)
class HealthImportOutcome:
    """Persisted result shared by Telegram, Mini App and local-path imports."""

    inserted: int
    duplicates: int
    updated: int
    invalid: int
    summary: Any
    profile: dict[str, Any]
    source_file: str  # filename only — no full path for privacy
    total_stored: int  # total health rows for user after import
    import_started: str  # ISO-8601 UTC
    import_completed: str  # ISO-8601 UTC
    dataset_end_date: str | None = None


def _health_import_staleness_warning(max_date: Any, *, today: datetime | None = None) -> str:
    freshness = export_freshness_status(max_date, now=today, tz=TZ)
    if freshness["parse_error"] or not freshness["is_stale"]:
        return ""
    newest = freshness["latest_sample_date"]
    days_old = freshness["days_old"]
    return (
        "⚠️ הקובץ יובא בהצלחה, אבל הנתונים אינם טריים: "
        f"הרשומה האחרונה היא מ-{newest} "
        f"({days_old} ימים אחורה). כדי לדייק את השבוע האחרון צריך ZIP חדש."
    )


def _health_import_display_max_date(outcome: HealthImportOutcome) -> str | None:
    return outcome.dataset_end_date or outcome.summary.max_date


def _health_import_success_text(outcome: HealthImportOutcome) -> str:
    s = outcome.summary
    max_date = _health_import_display_max_date(outcome)

    # --- Source file section ---
    lines = ["<b>ייבוא Apple Health הושלם ✅</b>", ""]
    lines.append("<b>בקובץ שנבחר:</b>")
    if s.min_date and max_date:
        lines.append(f"• טווח נתונים: {s.min_date} עד {max_date}")
        lines.append(f"• הרשומות החדשות ביותר הן מתאריך: {max_date}")
    lines.append(f"• רשומות בקובץ: {s.rows:,}")
    if s.min_date and max_date:
        warning = _health_import_staleness_warning(max_date)
        if warning:
            lines.append(warning)
    lines.append("")

    # --- Current import section ---
    lines.append("<b>בייבוא הנוכחי:</b>")
    lines.append(f"• נוספו: {outcome.inserted:,}")
    if outcome.updated:
        lines.append(f"• עודכנו: {outcome.updated:,}")
    lines.append(f"• כפילויות שלא נשמרו שוב: {outcome.duplicates:,}")
    if outcome.invalid:
        lines.append(f"• רשומות לא תקינות: {outcome.invalid:,}")
    lines.append("")

    # --- Database totals section ---
    lines.append("<b>במאגר שלך כעת:</b>")
    lines.append(f"• אימונים: {s.workouts:,}")
    lines.append(f"• רשומות שינה מנורמלות: {s.sleep_sessions:,}")
    if s.weight_records:
        lines.append(f"• רשומות משקל: {s.weight_records:,}")
    if s.activity_records:
        lines.append(f"• רשומות פעילות: {s.activity_records:,}")

    return "\n".join(lines)


async def pending_import_facts(user_id: int) -> list[dict[str, Any]]:
    """RE9-009: facts derived from a Health import that await explicit activation."""
    return await DB.fetch_all(
        """
        SELECT key, source
        FROM user_facts
        WHERE user_id=?
          AND valid=1
          AND confirmed=0
          AND kind!='gap'
        ORDER BY updated_at DESC
        """,
        (user_id,),
    )


def health_activation_keyboard(pending_count: int) -> InlineKeyboardMarkup | None:
    """RE9-009: explicit confirm/correct/activate gate after a Health import.

    Data is imported but not silently applied — the user sees the baseline and
    decides. Returns None when there is nothing pending to activate.
    """
    if pending_count <= 0:
        return None
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"✅ הפעל את מה שזוהה ({pending_count})", callback_data="health:activate")],
            [InlineKeyboardButton("✏️ תקן לפני הפעלה", callback_data="health:review")],
            [InlineKeyboardButton("⬅️ עדיין לא", callback_data="menu:home")],
        ]
    )


async def activate_imported_health_facts(user_id: int) -> int:
    """Confirm all pending imported facts so they become active. Returns count."""
    pending = await pending_import_facts(user_id)
    for row in pending:
        await user_model.confirm_fact(DB, user_id, str(row["key"]))
    if pending:
        with suppress(Exception):
            await write_audit(
                user_id, "health_facts_activated", "health", None, count=len(pending),
            )
        with suppress(Exception):
            await event_log.append_event(
                DB, user_id, "health_facts_activated",
                entity="health", source="user",
                properties={"count": len(pending)},
            )
    return len(pending)


# RE10-4 / RE12: per-item confirmation wizard shown right after a Health
# import, replacing the old single "activate everything at once" gate.
# RE12 splits the bundled "workout pattern" approval into three SEPARATE
# confirmations — weekly frequency, training days, typical hour — so each
# data point is approved on its own, in the order a coach would actually
# confirm data with a client (frequency feeds the day step, then the time),
# then weight, sleep, and any remaining imported item (steps, body fat...).
HEALTH_CONFIRM_FLOW = "health_confirm"

WIZARD_STEP_WORKOUT_FREQUENCY = "workout_pattern.frequency"
WIZARD_STEP_WORKOUT_DAYS = "workout_pattern.days"
WIZARD_STEP_WORKOUT_HOUR = "workout_pattern.hour"
# TASK-2: a representative typical workout duration inferred from the imported
# workout records is confirmed here, so the later "כמה דקות יש לך לאימון" plan
# question is skipped when session_minutes is already confirmed.
WIZARD_STEP_WORKOUT_DURATION = "workout_pattern.duration"
_WORKOUT_SUBSTEPS = (
    WIZARD_STEP_WORKOUT_FREQUENCY,
    WIZARD_STEP_WORKOUT_DAYS,
    WIZARD_STEP_WORKOUT_HOUR,
    WIZARD_STEP_WORKOUT_DURATION,
)
_WIZARD_STEP_ORDER = (*_WORKOUT_SUBSTEPS, "weight_kg", "sleep_schedule")

# Facts that are COMPUTED plan outputs, never HealthKit observations, so the
# import wizard must never offer them for confirmation. Presenting a calorie
# target before its prerequisites (height, goal weight, timeframe, goal type)
# exist is meaningless — and rendered "יעד קלוריות: יעד קלוריות" because the
# fact carries no user-facing numeric value yet. These are decided by the goal
# flow, not by confirming an import.
_WIZARD_EXCLUDED_KEYS = frozenset(
    {
        "calorie_target",
        "manual_calorie_override",
        "approved_goal",
        "weight_trend",
    }
)

# The three workout sub-steps all share the fact key "workout_pattern", so
# their edit hints must be keyed by step id (see prompt_health_wizard_edit).
# The days step has its own frequency-aware prompt and is handled separately.
_WORKOUT_SUBSTEP_EDIT_HINTS: dict[str, str] = {
    WIZARD_STEP_WORKOUT_FREQUENCY: "כמה אימונים בשבוע (למשל: 3)",
    WIZARD_STEP_WORKOUT_HOUR: "שעת אימון מועדפת (למשל: 18:30)",
    WIZARD_STEP_WORKOUT_DURATION: "משך אימון טיפוסי בדקות (למשל: 55)",
}

# What to type when the detected value is wrong — per fact key.
_WIZARD_EDIT_HINTS: dict[str, str] = {
    "weight_kg": 'משקל עדכני בק"ג (למשל: 82.5)',
    "sleep_schedule": "שעות שינה רצויות (למשל: 23:00-07:00)",
    "avg_steps": "ממוצע צעדים יומי (למשל: 9000)",
    "body_fat_pct": "אחוז שומן עדכני (למשל: 22)",
    "resting_hr": "דופק מנוחה עדכני (למשל: 55)",
    "eating_windows": "שעות אכילה (למשל: 09:00, 13:00, 19:00)",
}


def wizard_step_fact_key(step_id: str) -> str:
    """Map a wizard step id (possibly ``fact.sub``) to its user_facts key."""
    return step_id.split(".", 1)[0]


def _workout_substep_applicable(step_id: str, value: Any) -> bool:
    """A workout sub-step is only shown when the import actually detected
    that piece of data (no empty confirmations)."""
    if not isinstance(value, dict):
        return False
    if step_id == WIZARD_STEP_WORKOUT_FREQUENCY:
        return value.get("weekly_frequency") is not None
    if step_id == WIZARD_STEP_WORKOUT_DAYS:
        return bool(value.get("common_weekdays"))
    if step_id == WIZARD_STEP_WORKOUT_HOUR:
        return bool(value.get("typical_hour"))
    if step_id == WIZARD_STEP_WORKOUT_DURATION:
        # Only offer the duration confirmation when the import actually inferred
        # a representative duration from the workout records.
        return value.get("avg_duration_minutes") is not None
    return True


def _workout_days_indices(value: dict[str, Any]) -> list[int]:
    """Normalized (Monday-first) detected training-day indices."""
    schema = value.get("weekday_schema") or WEEKDAY_SCHEMA_VERSION
    days: set[int] = set()
    for raw in value.get("common_weekdays") or []:
        normalized = normalize_weekday(raw, schema)
        if normalized.weekday is not None:
            days.add(normalized.weekday)
    return sorted(days)


def _workout_days_labels(value: dict[str, Any]) -> list[str]:
    """Hebrew day names for the detected training days, in Israeli order."""
    return weekday_labels_he(
        value.get("common_weekdays") or [],
        value.get("weekday_schema") or WEEKDAY_SCHEMA_VERSION,
    )


def _parse_health_start(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value).strip()
        if not raw:
            return None
        if raw.endswith("Z"):
            raw = f"{raw[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


_MIN_RECURRING_WORKOUTS_PER_WEEKDAY = 2

# A weekday only counts as "recurring" if it shows up in at least this share of
# the weeks the window covers. The old absolute floor of 2 did not scale: over a
# 90-day window *every* weekday clears 2 hits for anyone who trains regularly,
# so widening the window turned "found a pattern" into an unconditional yes and
# manufactured training days the user never picked. Requiring ~1 workout every
# 3 weeks keeps sparse-but-real habits while rejecting the noise a long window
# accumulates.
_MIN_RECURRING_WEEKDAY_RATE_PER_WEEK = 1 / 3

# Ceiling on the scaled floor. Without it the floor outruns the evidence a
# genuinely sparse routine can ever accumulate: someone training a fixed weekday
# once a month clears the 60-day floor but NOT the 90- or 180-day one, so
# widening the window would *shrink* the answer to nothing and a real (if
# infrequent) pattern would be reported as no pattern at all. Capping keeps
# widening monotonic — a wider window may never admit fewer weekdays than a
# narrower one.
_MAX_RECURRING_WORKOUTS_PER_WEEKDAY = 4

# Window the detector is allowed to use before the answer needs a caveat. Past
# this the evidence is older than the user's current routine, so the caller must
# say so rather than presenting it as "the days you trained most".
_DEFAULT_WEEKDAY_WINDOW_DAYS = 28

_WEEKDAY_WINDOW_LADDER = (28, 60, 90, 180)


def _min_recurring_workouts_for_window(window_days: int) -> int:
    """Recurrence floor scaled to the window, clamped at both ends.

    Grows with the window so a long lookback demands proportionally more
    evidence, but never below the historical absolute floor and never above
    ``_MAX_RECURRING_WORKOUTS_PER_WEEKDAY`` (see its comment: an uncapped floor
    makes widening non-monotonic and strands sparse users with no answer).
    """
    weeks = max(1.0, window_days / 7.0)
    scaled = math.ceil(_MIN_RECURRING_WEEKDAY_RATE_PER_WEEK * weeks)
    return max(
        _MIN_RECURRING_WORKOUTS_PER_WEEKDAY,
        min(scaled, _MAX_RECURRING_WORKOUTS_PER_WEEKDAY),
    )


def weekday_source_is_widened(source: str) -> bool:
    """True when the weekday answer needed more than the default window.

    The caller uses this to decide whether the proposal must be presented with
    a "I had to look further back" caveat. Previously only ``full_history`` was
    flagged, so ``last_60_days``/``last_90_days``/``last_180_days`` rendered
    identically to the default window and silently overstated the evidence.
    """
    if source in {"full_history", "insufficient_history"}:
        return True
    if not source.startswith("last_") or not source.endswith("_days"):
        return False
    try:
        window_days = int(source[len("last_"):-len("_days")])
    except ValueError:
        return False
    return window_days > _DEFAULT_WEEKDAY_WINDOW_DAYS


def weekday_source_window_days(source: str) -> int | None:
    """Window length a weekday source represents, or None when unbounded."""
    if source.startswith("last_") and source.endswith("_days"):
        try:
            return int(source[len("last_"):-len("_days")])
        except ValueError:
            return None
    return None


def _rank_weekdays_from_dates(dates: list[datetime]) -> list[int]:
    counts: dict[int, int] = defaultdict(int)
    newest_by_day: dict[int, datetime] = {}
    for date in dates:
        weekday = date.weekday()
        counts[weekday] += 1
        if weekday not in newest_by_day or date > newest_by_day[weekday]:
            newest_by_day[weekday] = date
    ranked = sorted(
        counts,
        key=lambda day: (-counts[day], -newest_by_day[day].timestamp(), day),
    )
    return ranked


def _recurring_weekdays_from_dates(
    dates: list[datetime], window_days: int | None = None
) -> list[int]:
    """Weekdays that recur often enough to be a pattern, best-ranked first.

    ``window_days`` is the length of the window ``dates`` was drawn from; the
    recurrence floor scales with it so a longer window demands proportionally
    more evidence. Omitting it keeps the legacy absolute floor.
    """
    counts: dict[int, int] = defaultdict(int)
    for date in dates:
        counts[date.weekday()] += 1
    ranked = _rank_weekdays_from_dates(dates)
    minimum = (
        _MIN_RECURRING_WORKOUTS_PER_WEEKDAY
        if window_days is None
        else _min_recurring_workouts_for_window(window_days)
    )
    return [day for day in ranked if counts[day] >= minimum]


async def _historical_workout_weekdays(user_id: int, target_count: int) -> tuple[list[int], str]:
    """Pick training weekdays from actual imported workout history.

    Real HealthKit workout rows are preferred over template guesses, even when
    the best usable pattern is older than the latest export window.
    """
    if target_count <= 0:
        return [], "invalid_target"

    rows = await DB.fetch_all(
        """
        SELECT start_time
        FROM health
        WHERE user_id = ? AND sample_type = 'workout'
        ORDER BY start_time DESC
        """,
        (user_id,),
    )
    dates = [
        parsed
        for row in rows
        if (parsed := _parse_health_start(row["start_time"] if isinstance(row, dict) else row[0]))
    ]
    if not dates:
        return [], "no_history"

    newest = max(dates)
    for window_days in _WEEKDAY_WINDOW_LADDER:
        cutoff = newest - timedelta(days=window_days)
        window_dates = [date for date in dates if date >= cutoff]
        ranked = _recurring_weekdays_from_dates(window_dates, window_days)
        if len(ranked) >= target_count:
            return sunday_first_order(ranked[:target_count]), f"last_{window_days}_days"

    history_days = max(1, (newest - min(dates)).days)
    ranked = _recurring_weekdays_from_dates(dates, history_days)
    if len(ranked) >= target_count:
        return sunday_first_order(ranked[:target_count]), "full_history"
    return sunday_first_order(ranked), "insufficient_history"


def _format_pending_fact_value(key: str, value: Any) -> str:
    """Human-readable "what was detected" text for one pending fact.

    RE12: every fact shows its actual VALUE (steps, body fat, resting HR...)
    — never just the label, so the user always sees what they are approving.
    """
    if key == "workout_pattern" and isinstance(value, dict):
        freq = value.get("weekly_frequency")
        hour = value.get("typical_hour")
        parts = []
        if freq is not None:
            parts.append(f"~{freq:g} אימונים בשבוע")
        days = _workout_days_labels(value)
        if days:
            parts.append(f"בימים {', '.join(days)}")
        if hour:
            parts.append(f"בדרך כלל בסביבות {hour}")
        return ", ".join(parts) or "דפוס אימונים"
    if key == "sleep_schedule" and isinstance(value, dict):
        bedtime = value.get("typical_bedtime") or value.get("bedtime")
        wake = value.get("typical_wake_time") or value.get("wake_time")
        if bedtime and wake:
            return f"שינה {bedtime}–{wake}"
        return "שגרת שינה"
    if key == "eating_windows" and isinstance(value, dict):
        hours = value.get("typical_meal_hours") or []
        if hours:
            return f"ארוחות בדרך כלל סביב {', '.join(hours)}"
        first, last = value.get("first_meal_time"), value.get("last_meal_time")
        if first and last:
            return f"אכילה בין {first} ל-{last}"
        return "חלונות אכילה"
    display = user_model.display_value(key, value)
    if display and display != "לא צוין":
        return display
    return user_model.display_label(key)


def _wizard_step_prompt(
    step_id: str, fact: dict[str, Any], quality: dict[str, Any] | None = None
) -> tuple[str, str, str]:
    """Build (detected_line, plan_scope, correction_hint) for one wizard step.

    All prompts share one format:
    "זוהה X. האם לאשר גם עבור <scope>? אם לא — ציין <hint>."

    RE13: when a data-quality report is available, the prompt also explains
    what the number is based on (how many weeks/days were counted and why),
    so the user approves a value with known reliability, not a bare number.
    """
    key = wizard_step_fact_key(step_id)
    value = fact.get("value")
    if step_id == WIZARD_STEP_WORKOUT_FREQUENCY and isinstance(value, dict):
        freq = value.get("weekly_frequency")
        detected = f"זוהתה שגרת אימונים של כ-{float(freq):g} אימונים בשבוע"
        wq = (quality or {}).get("workout_frequency") or {}
        if wq.get("policy") == "strict_7_of_7" and wq.get("valid_weeks"):
            detected += (
                f".\nהחישוב מבוסס על {int(wq['valid_weeks'])} שבועות מלאים "
                "עם נתוני שעון"
            )
        elif value.get("wear_filtered") and value.get("valid_weeks_sampled"):
            weeks_n = int(value["valid_weeks_sampled"])
            weeks_text = "שבוע אחד" if weeks_n == 1 else f"{weeks_n} שבועות"
            detected += f" (על בסיס {weeks_text} עם נתוני שעון מלאים)"
        return detected, "תוכנית האימונים", "כמות אימונים רצויה בשבוע (למשל: 3)"
    if step_id == WIZARD_STEP_WORKOUT_DAYS and isinstance(value, dict):
        days = ", ".join(_workout_days_labels(value))
        return (
            f"זוהו ימי אימון קבועים מהנתונים: {days}",
            "תוכנית האימונים",
            "את ימי האימון הרצויים (למשל: ראשון, שלישי, חמישי)",
        )
    if step_id == WIZARD_STEP_WORKOUT_HOUR and isinstance(value, dict):
        return (
            f"זוהתה שעת אימון טיפוסית סביב {value.get('typical_hour')}",
            "תוכנית האימונים",
            "שעה רצויה (למשל: 18:30)",
        )
    if step_id == WIZARD_STEP_WORKOUT_DURATION and isinstance(value, dict):
        minutes = int(round(float(value.get("avg_duration_minutes") or 0)))
        return (
            f"משך אימון טיפוסי: כ־{minutes} דקות",
            "תוכנית האימונים",
            "משך אימון טיפוסי בדקות (למשל: 55)",
        )
    if key == "avg_steps":
        sq = (quality or {}).get("steps") or {}
        if sq.get("days_used"):
            display = user_model.display_value(key, value)
            detected = f"זוהה ממוצע צעדים יומי: {display}"
            if sq.get("explanation_he"):
                detected += f".\n{sq['explanation_he']}"
            if sq.get("warning_he"):
                detected += f"\n{sq['warning_he']}"
            return (
                detected.rstrip("."),
                "התוכנית וההמלצות",
                _WIZARD_EDIT_HINTS.get(key, "ערך אחר"),
            )
    if key == "sleep_schedule" and isinstance(value, dict):
        nights = value.get("nights_sampled")
        detected = f"זוהה {user_model.display_label(key)}: {_format_pending_fact_value(key, value)}"
        if nights is not None and 0 < int(nights) < 10:
            detected += f".\nמבוסס על {int(nights)} לילות בלבד — אמינות בינונית"
        return (
            detected,
            "התוכנית וההמלצות",
            _WIZARD_EDIT_HINTS.get(key, "ערך אחר"),
        )
    label = user_model.display_label(key)
    display = _format_pending_fact_value(key, value)
    return (
        f"זוהה {label}: {display}",
        "התוכנית וההמלצות",
        _WIZARD_EDIT_HINTS.get(key, "ערך אחר"),
    )


async def _wizard_done_steps(user_id: int) -> list[str]:
    from noam_coach.bot.onboarding import get_flow_state

    state = await get_flow_state(user_id, HEALTH_CONFIRM_FLOW)
    payload = (state or {}).get("payload") or {}
    done = payload.get("done") or []
    return [str(step) for step in done if isinstance(step, str)]


async def _wizard_deferred_steps(user_id: int) -> list[str]:
    from noam_coach.bot.onboarding import get_flow_state

    state = await get_flow_state(user_id, HEALTH_CONFIRM_FLOW)
    payload = (state or {}).get("payload") or {}
    deferred = payload.get("deferred") or []
    return [str(step) for step in deferred if isinstance(step, str)]


async def _wizard_skipped_steps(user_id: int) -> list[str]:
    """Steps the user declined TWICE — terminal for this wizard run.

    Review 2026-07-18_1 / F-01: deferral used to have no terminal state, so
    "דלג כרגע" on the last remaining item re-asked the identical question
    forever. A first skip defers (the item returns once, after everything
    else); a second skip on the same item ends its run — the wizard can then
    finish without it. The fact itself stays pending/unconfirmed for future
    imports; nothing is applied or invalidated.
    """
    from noam_coach.bot.onboarding import get_flow_state

    state = await get_flow_state(user_id, HEALTH_CONFIRM_FLOW)
    payload = (state or {}).get("payload") or {}
    skipped = payload.get("skipped") or []
    return [str(step) for step in skipped if isinstance(step, str)]


def _wizard_payload(
    done: list[str], deferred: list[str], skipped: list[str]
) -> dict[str, Any]:
    """The ONE shape every wizard-state writer persists — a writer that
    forgets a list (the F-01 bug class) can no longer exist."""
    payload: dict[str, Any] = {"done": done}
    if deferred:
        payload["deferred"] = deferred
    if skipped:
        payload["skipped"] = skipped
    return payload


# --- provenance of wizard-confirmed values (W1-6) --------------------------
#
# A "✅ אשר" tap on a health-wizard step is real evidence: the user looked at a
# number and accepted it. But it is *confirmation of a derivation*, not a
# statement the user made. Writing it as source=SOURCE_USER (as this module
# used to) manufactures the exact token that _confirmed_fact_value treats as
# "the user really said this", so a value computed from a single workout
# became indistinguishable from one the user typed.
#
# The honest representation is source=SOURCE_DERIVED + kind=KIND_FACT +
# confirmed=True:
#   * source stays DERIVED         → provenance is not laundered; user_model's
#                                    provenance_kind() keeps reporting
#                                    "inferred", and any consumer asking "did
#                                    the user tell me this?" gets the truth.
#   * kind promoted to KIND_FACT   → it is no longer an open estimate awaiting
#                                    confirmation; it is a settled planning
#                                    input.
#   * confirmed=True               → the tap is recorded. user_model already
#                                    treats DERIVED+confirmed as trustworthy
#                                    (see fact_is_actionable), so readiness and
#                                    plan activation keep working.
# No new SOURCE_* constant is needed — SOURCE_DERIVED already exists and this
# combination is already meaningful to user_model.
_WIZARD_CONFIRMED_SOURCE = user_model.SOURCE_DERIVED
_WIZARD_CONFIRMED_KIND = user_model.KIND_FACT

# Sub-key stamped into the stored value so the derivation stays recoverable.
DERIVED_PROVENANCE_KEY = "derived_from"


def _confirmed_fact_value(fact: dict[str, Any] | None) -> Any | None:
    """Return a usable value for a fact the user has actually signed off on.

    Health import estimates are useful as suggestions, but they must not count
    as the "manual override" that suppresses future prompts.  This is the guard
    that keeps HealthKit from overriding the screenshot case: user wrote
    Sun/Mon/Wed/Fri, so old/partial Health data may be shown as detected data
    but can no longer reopen/replace those days.

    Two things qualify, and only two:
      * a typed answer (source=SOURCE_USER, confirmed) — the user authored it;
      * a derived value the user explicitly confirmed in the wizard
        (source=SOURCE_DERIVED, kind=KIND_FACT, confirmed) — the user did not
        author the number but did accept it, which is the same act of consent
        this gate exists to detect.

    An *un*confirmed derivation still returns None, which is the case the gate
    was written for.  Requiring KIND_FACT on the derived branch keeps a merely
    hardened estimate (KIND_ESTIMATE) from sneaking through: only the wizard's
    deliberate promotion counts.
    """
    if not fact or fact.get("kind") == user_model.KIND_GAP:
        return None
    if not fact.get("confirmed"):
        return None
    source = fact.get("source")
    if source == user_model.SOURCE_USER:
        return fact.get("value")
    if (
        source == _WIZARD_CONFIRMED_SOURCE
        and fact.get("kind") == _WIZARD_CONFIRMED_KIND
    ):
        return fact.get("value")
    return None


def _wizard_derivation_note(
    step_id: str, pattern: dict[str, Any], field: str
) -> dict[str, Any]:
    """Describe where a wizard-confirmed value came from.

    Recorded so the derivation is recoverable *after* the fact. ``set_fact``
    only writes a ``user_fact_history`` row for a key that already exists, and
    these planning keys are usually CREATED by the confirm tap — so history
    cannot be relied on to preserve the origin. Stamping the provenance into
    the stored value itself is the only representation that survives the very
    first write, which is exactly the write that loses the information today.
    """
    note: dict[str, Any] = {
        "source_fact": "workout_pattern",
        "source_field": field,
        "wizard_step": step_id,
        "confirmed_by_user": True,
    }
    raw = pattern.get(field)
    if raw is not None:
        note["raw_value"] = raw
    # Carry the evidence base forward when the detector recorded it, so a value
    # backed by a single workout stays legible as such.
    for meta in (
        "sessions_sampled",
        "recent_sessions_sampled",
        "valid_weeks_sampled",
        "weekday_selection_source",
        "weekday_selection_widened",
        "weekday_selection_window_days",
    ):
        if pattern.get(meta) is not None:
            note[meta] = pattern[meta]
    return note


async def _set_wizard_confirmed_fact(
    user_id: int,
    key: str,
    value: Any,
    *,
    provenance: dict[str, Any],
) -> None:
    """Persist a wizard-confirmed derived value with its provenance intact.

    ``confidence`` is passed explicitly at the user-confirmation level (0.85):
    the tap is genuine evidence. When the derivation rests on a thin sample the
    stored ``*_sampled`` metadata lets ``set_fact`` attenuate that number back
    down, which is the correct outcome — a value computed from one workout
    should not end up as confident as one computed from thirty.
    """
    await user_model.set_fact(
        DB, user_id, key, value,
        kind=_WIZARD_CONFIRMED_KIND,
        source=_WIZARD_CONFIRMED_SOURCE,
        confidence=0.85,
        confirmed=True,
    )
    await _record_wizard_provenance(user_id, key, provenance)


async def _record_wizard_provenance(
    user_id: int, key: str, provenance: dict[str, Any]
) -> None:
    """Append a history row capturing the derivation behind a confirmed value.

    set_fact writes history only when the key already existed, so for a key the
    wizard creates the origin would otherwise vanish. Writing the note here
    guarantees one recoverable row per confirm tap regardless of whether the
    key is new.
    """
    # Provenance is an audit aid, never a reason for a confirm tap to fail.
    with suppress(Exception):
        await DB.execute(
            """
            INSERT INTO user_fact_history(
                user_id, key, value, source, confidence, recorded_at
            ) VALUES(?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                f"{key}:{DERIVED_PROVENANCE_KEY}",
                json.dumps(provenance, ensure_ascii=False),
                _WIZARD_CONFIRMED_SOURCE,
                0.85,
                utc_now(),
            ),
        )


async def read_wizard_provenance(
    user_id: int, key: str
) -> dict[str, Any] | None:
    """Return the recorded derivation behind a wizard-confirmed fact, if any."""
    row = await DB.fetch_one(
        """
        SELECT value FROM user_fact_history
        WHERE user_id=? AND key=?
        ORDER BY recorded_at DESC, id DESC LIMIT 1
        """,
        (user_id, f"{key}:{DERIVED_PROVENANCE_KEY}"),
    )
    if not row:
        return None
    try:
        parsed = json.loads(row["value"])
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


async def _has_manual_training_frequency(user_id: int) -> bool:
    """Whether the user already supplied a plan frequency we should trust."""
    for key in ("active_training_days", "preferred_training_days"):
        value = _confirmed_fact_value(await user_model.get_fact(DB, user_id, key))
        if isinstance(value, list) and value:
            return True
    value = _confirmed_fact_value(
        await user_model.get_fact(DB, user_id, "training_days_per_week")
    )
    if value is None:
        return False
    try:
        return 1 <= int(float(value)) <= 7
    except (TypeError, ValueError):
        return False


async def _desired_weekly_frequency(user_id: int) -> int | None:
    """The number of weekly workouts the user asked to PLAN for (their goal),
    independent of what HealthKit detected. Used to size the training-day
    proposal so it always matches what the user requested."""
    value = _confirmed_fact_value(
        await user_model.get_fact(DB, user_id, "training_days_per_week")
    )
    if value is None:
        return None
    try:
        freq = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return freq if 1 <= freq <= 7 else None


async def _has_manual_training_days(user_id: int) -> bool:
    """Whether the user already supplied concrete training days."""
    for key in (
        "active_training_days",
        "preferred_training_days",
        "weekly_availability",
    ):
        value = _confirmed_fact_value(await user_model.get_fact(DB, user_id, key))
        if isinstance(value, list) and value:
            return True
    return False


async def _has_manual_training_hour(user_id: int) -> bool:
    """Whether the user already supplied a preferred workout time/window."""
    value = _confirmed_fact_value(
        await user_model.get_fact(DB, user_id, "workout_window")
    )
    return isinstance(value, str) and bool(value.strip())


async def _next_wizard_step(
    user_id: int,
    done: list[str],
    deferred: list[str] | None = None,
    skipped: list[str] | None = None,
) -> tuple[str, dict[str, Any]] | None:
    """Return (step_id, fact) for the next confirmation, honoring sub-steps.

    ``skipped`` steps (declined twice — F-01) are terminal for this run and
    are never offered again, in EITHER selection loop.
    """
    deferred_set = set(deferred or [])
    skipped_set = set(skipped or [])
    first_deferred: tuple[str, dict[str, Any]] | None = None
    pending = await pending_import_facts(user_id)
    # Never surface computed plan outputs (e.g. calorie_target) as a HealthKit
    # confirmation — they are not imported observations and have no value to
    # approve here.
    pending_keys = [
        str(row["key"]) for row in pending if str(row["key"]) not in _WIZARD_EXCLUDED_KEYS
    ]
    ordered_keys = {wizard_step_fact_key(step) for step in _WIZARD_STEP_ORDER}

    facts: dict[str, dict[str, Any] | None] = {}

    async def fact_for(key: str) -> dict[str, Any] | None:
        if key not in facts:
            facts[key] = await user_model.get_fact(DB, user_id, key)
        return facts[key]

    for step_id in _WIZARD_STEP_ORDER:
        key = wizard_step_fact_key(step_id)
        if key not in pending_keys or step_id in done:
            continue
        fact = await fact_for(key)
        if fact is None:
            continue
        if key == "workout_pattern" and not _workout_substep_applicable(
            step_id, fact.get("value")
        ):
            continue
        # Do not ask the user to approve weak HealthKit workout inferences when
        # a confirmed manual answer already exists. This keeps the screenshot
        # case sane: an old/partial Health export may be insufficient, but it
        # must not re-open “כמה אימונים בשבוע?” after the user already supplied
        # active training days/frequency.
        if (
            step_id == WIZARD_STEP_WORKOUT_FREQUENCY
            and await _has_manual_training_frequency(user_id)
        ):
            continue
        if (
            step_id == WIZARD_STEP_WORKOUT_DAYS
            and await _has_manual_training_days(user_id)
        ):
            continue
        if (
            step_id == WIZARD_STEP_WORKOUT_HOUR
            and await _has_manual_training_hour(user_id)
        ):
            continue
        if step_id in skipped_set:
            continue
        if step_id in deferred_set:
            first_deferred = first_deferred or (step_id, fact)
            continue
        return step_id, fact

    # Anything imported but not in the known wizard order still gets reviewed
    # (steps average, body fat, resting HR, eating windows...), so nothing
    # silently skips confirmation.
    for key in pending_keys:
        if key in done:
            continue
        fact = await fact_for(key)
        if fact is None:
            continue
        if key in ordered_keys:
            # workout_pattern with no applicable sub-step falls back to a
            # generic single confirmation; otherwise the ordered loop owns it.
            if key != "workout_pattern" or any(
                _workout_substep_applicable(s, fact.get("value"))
                for s in _WORKOUT_SUBSTEPS
            ):
                continue
        # Never confirm a fact with no real user-facing value — that would show
        # a placeholder (the label repeated as its own value). Skip it instead.
        if not _has_confirmable_value(key, fact.get("value")):
            continue
        if key in skipped_set:
            continue
        if key in deferred_set:
            first_deferred = first_deferred or (key, fact)
            continue
        return key, fact
    return first_deferred


def _has_confirmable_value(key: str, value: Any) -> bool:
    """True when a pending fact has a real value worth confirming (not a
    placeholder). Dict-shaped detected patterns are checked by their own
    formatter; scalar facts must render to something other than the bare label.
    """
    if value is None:
        return False
    if isinstance(value, dict):
        display = _format_pending_fact_value(key, value)
        return bool(display) and display != user_model.display_label(key)
    display = user_model.display_value(key, value)
    return bool(display) and display != "לא צוין" and display != user_model.display_label(key)


HEALTH_POST_WIZARD_FLOW = "health_post_wizard"


async def _wizard_quality_report(user_id: int) -> dict[str, Any] | None:
    """Data-quality report for wizard prompts; never breaks the wizard."""
    with suppress(Exception):
        from noam_coach.services.health_quality import build_health_quality_report

        return await build_health_quality_report(
            DB, user_id, TZ, SETTINGS.routine_window_days
        )
    return None


async def _send_wizard_screen(
    target: Any, text: str, keyboard: InlineKeyboardMarkup
) -> None:
    if hasattr(target, "edit_message_text"):
        from noam_coach.bot.ui import safe_edit

        await safe_edit(target, text, keyboard)
    else:
        await target.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)


_SKIP_WIZARD_BUTTON_ROW = [
    InlineKeyboardButton("⏩ דלג על שאר האישורים", callback_data="health:skip_wizard")
]

_DEFER_WIZARD_BUTTON_ROW = [
    InlineKeyboardButton("⏭️ דלג כרגע", callback_data="health:skip_item")
]


async def ask_next_health_confirm_step(
    target: Any, user_id: int, *, ack_text: str | None = None
) -> bool:
    """Ask the user to confirm the next pending imported data point.

    RE12: each item is confirmed SEPARATELY (frequency / training days /
    typical hour are three independent steps) and every prompt shows the
    detected value in one uniform format. ``ack_text`` (what was just
    approved) is echoed above the next prompt so the user always sees the
    information that was confirmed.

    RE11: the prompt itself accepts a typed correction directly — pending is
    set to the step's edit key from the start. "✅ אשר" remains the one-tap
    fast path when the detected value is already correct.

    For the frequency step, if the recent Health data has drifted from the
    long-run average by a meaningful amount, a trend-aware proposal (recent
    value + a recommended step-up) is shown with structured choice buttons
    instead of the plain confirm prompt — typing a number still always works.

    Returns False once nothing is left to review — the caller should then
    show the final import summary.
    """
    from noam_coach.bot.onboarding import set_flow_state, clear_flow_state, set_pending

    done = await _wizard_done_steps(user_id)
    deferred = await _wizard_deferred_steps(user_id)
    skipped = await _wizard_skipped_steps(user_id)
    next_step = await _next_wizard_step(user_id, done, deferred, skipped)
    if next_step is None:
        await clear_flow_state(user_id, HEALTH_CONFIRM_FLOW)
        return False

    step_id, fact = next_step
    await set_flow_state(
        user_id, HEALTH_CONFIRM_FLOW, step_id, _wizard_payload(done, deferred, skipped)
    )
    await set_pending(user_id, f"__health_edit_{step_id}__")

    quality = await _wizard_quality_report(user_id)

    prefix = f"{ack_text}\n\n" if ack_text else ""
    # F-01: a deferred item returning for its second (final) pass must never
    # be a byte-identical re-render — say it is a return visit and name the
    # way out, so "דלג כרגע" can never read as the bot ignoring the tap.
    if step_id in deferred:
        prefix += "🔁 חוזר לפריט שדחית קודם. דילוג נוסף יסיים את האשף בלעדיו.\n\n"
    # RE13: a stale export changes what every number means — warn once, on
    # the very first wizard screen, before any value is approved.
    if quality and not done and not ack_text:
        stale_warning = (quality.get("freshness") or {}).get("warning_he")
        if stale_warning:
            prefix += f"⚠️ {esc(stale_warning)}\n\n"
    header = f"{prefix}<b>אישור נתונים מהייבוא</b>\n\n"

    if step_id == WIZARD_STEP_WORKOUT_FREQUENCY and isinstance(fact.get("value"), dict):
        import routine

        key = wizard_step_fact_key(step_id)
        wq = (quality or {}).get("workout_frequency") or {}

        freshness = (quality or {}).get("freshness") or {}

        # RE13: with too few usable weeks there is no trustworthy number to
        # approve — ask the user directly instead of dressing a guess up as
        # a detected routine.
        if wq.get("policy") == routine.POLICY_INSUFFICIENT:
            text = (
                f"{header}"
                f"{esc(wq.get('warning_he') or 'אין מספיק שבועות עם נתוני שעון כדי לזהות שגרת אימונים אמינה.')}\n"
                "אין לי מספיק מידע אמין מהשעון כדי לבחור עבורך מספר. "
                "כתוב כמה אימונים בשבוע תרצה לתכנן, למשל: 3."
            )
            # UX cleanup: do not show generic 2/3/4 suggestion buttons for a
            # weak HealthKit estimate. When data is not trustworthy, a typed
            # answer is clearer and avoids nudging the user into a wrong plan.
            # Keep only the explicit wizard escape hatch.
            keyboard = InlineKeyboardMarkup([_DEFER_WIZARD_BUTTON_ROW, _SKIP_WIZARD_BUTTON_ROW])
            await _send_wizard_screen(target, text, keyboard)
            return True

        value = fact["value"]
        pattern = routine.WorkoutPattern(
            weekly_frequency=value.get("weekly_frequency"),
            sessions_sampled=value.get("sessions_sampled", 0),
            recent_weekly_frequency=value.get("recent_weekly_frequency"),
            recent_sessions_sampled=value.get("recent_sessions_sampled", 0),
        )
        proposal = routine.build_frequency_trend_proposal(pattern)
        if proposal is not None:
            text = f"{header}{esc(proposal.message)}"
            rows = [
                [InlineKeyboardButton(label, callback_data=f"health:confirm:{key}:trend:{choice:g}")]
                for label, choice in proposal.choices
            ]
            rows.append(_DEFER_WIZARD_BUTTON_ROW)
            rows.append(_SKIP_WIZARD_BUTTON_ROW)
            await _send_wizard_screen(target, text, InlineKeyboardMarkup(rows))
            return True

        # RE13: relaxed policy — the shown value is the NORMALIZED estimate
        # (weeks with ≥5 worn days scaled to 7), so approving must apply that
        # exact number, not the strict raw value stored in the pattern.
        if wq.get("policy") == routine.POLICY_RELAXED and wq.get("frequency") is not None:
            approved = max(1, min(7, round(float(wq["frequency"]))))
            # Frame the number as what was actually PERFORMED (workout records),
            # not as "weeks with enough watch wear" — wear affects confidence,
            # not whether a logged workout counts.
            stale_prefix = ""
            if freshness.get("is_stale") and freshness.get("latest_sample_date"):
                stale_prefix = (
                    "⚠️ שים לב: קובץ הבריאות האחרון מסתיים ב-"
                    f"{esc(str(freshness['latest_sample_date']))}, ולכן השבועות האחרונים לא נכנסו לחישוב. "
                    "מומלץ לייצא ZIP חדש מהאייפון.\n\n"
                )
            text = (
                f"{stale_prefix}"
                "<b>אישור נתונים מהייבוא</b>\n\n"
                "אפשר ללמוד מהתקופה האחרונה שתועדה בקובץ ששגרת האימונים שלך "
                f"נראית סביב {approved} אימונים בשבוע.\n\n"
                "האם להשתמש בזה כבסיס לתוכנית? או רשום את כמות האימונים השבועית "
                "לבניית התוכנית."
            )
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    f"✅ השתמש לפי מה שתועד — {approved}",
                    callback_data=f"health:confirm:{key}:trend:{approved}",
                )],
                _DEFER_WIZARD_BUTTON_ROW,
                _SKIP_WIZARD_BUTTON_ROW,
            ])
            await _send_wizard_screen(target, text, keyboard)
            return True

    # Sleep uses all valid HealthKit sleep history and is not gated by watch
    # wear. Only ask manually when the sleep records themselves are too sparse.
    if step_id == "sleep_schedule" and isinstance(fact.get("value"), dict):
        from noam_coach.services.health_quality import (
            SLEEP_MIN_NIGHTS_FOR_CONFIRMATION,
        )

        value = fact["value"]
        nights = value.get("nights_sampled")
        if nights is not None and int(nights) < SLEEP_MIN_NIGHTS_FOR_CONFIRMATION:
            text = (
                f"{header}"
                "לא זיהיתי דפוס שינה מספיק ברור מתוך היסטוריית השינה בקובץ.\n"
                "אפשר לכתוב ידנית שעת שינה וקימה ממוצעת (למשל: 23:00-07:00), "
                "או לדלג כרגע ולחזור לזה לפני סיום בניית התוכנית."
            )
            keyboard = InlineKeyboardMarkup([
                _DEFER_WIZARD_BUTTON_ROW,
                _SKIP_WIZARD_BUTTON_ROW,
            ])
            await _send_wizard_screen(target, text, keyboard)
            return True
        if nights is not None:
            bedtime = value.get("typical_bedtime") or value.get("bedtime")
            wake = value.get("typical_wake_time") or value.get("wake_time")
            duration = value.get("avg_duration_minutes")
            lines = [
                "<b>אישור נתונים מהייבוא</b>",
                "",
                "לפי היסטוריית השינה שתועדה בקובץ, השגרה שלך נראית סביב:",
            ]
            if bedtime:
                lines.append(f"שעת שינה: {esc(str(bedtime))}")
            if wake:
                lines.append(f"שעת קימה: {esc(str(wake))}")
            if duration:
                hours = float(duration) / 60.0
                lines.append(f"משך שינה ממוצע: {hours:.1f} שעות")
            lines.extend([
                "",
                "נתתי משקל גבוה יותר לרשומות החדשות יותר, והשתמשתי גם בהיסטוריית שינה ישנה יותר כדי לזהות את הדפוס הכללי.",
                "",
                "להשתמש בזה כשגרת השינה שלך לתוכנית?",
            ])
            text = "\n".join(lines)
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ אשר", callback_data=f"health:confirm:{step_id}")],
                _DEFER_WIZARD_BUTTON_ROW,
                _SKIP_WIZARD_BUTTON_ROW,
            ])
            await _send_wizard_screen(target, text, keyboard)
            return True

    if step_id == "avg_steps":
        sq = (quality or {}).get("steps") or {}
        if sq.get("average") is not None:
            start = sq.get("window_start") or "?"
            end = sq.get("window_end") or "?"
            days = int(sq.get("days_used") or 0)
            raw = sq.get("raw_all_sources_average")
            conservative = sq.get("dominant_or_priority_source_average")
            baseline = sq.get("selected_planning_baseline") or sq.get("average")
            sources = sq.get("sources_found") or []
            lines = [
                header.rstrip(),
                f"ניתחתי את 28 הימים הקלנדריים המלאים האחרונים בקובץ: {esc(str(start))} עד {esc(str(end))}.",
                f"נמצאו נתוני צעדים עבור {days} ימים.",
                "",
            ]
            if raw is not None:
                lines.append(f"לפי כל מקורות StepCount יחד: בערך {int(raw):,} צעדים ביום.")
            if conservative is not None:
                lines.append(
                    f"לפי החישוב השמרני שמפחית סיכון לכפל בין מקורות: בערך {int(conservative):,} צעדים ביום."
                )
            lines.append(
                f"לבינתיים אשתמש בכ-{int(baseline):,} צעדים ביום כבסיס זמני לתוכנית."
            )
            if sources:
                lines.append(f"מקורות StepCount שנמצאו: {esc(', '.join(map(str, sources)))}.")
            lines.append("\nלהשתמש בזה כבסיס לתוכנית?")
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ השתמש כבסיס זמני", callback_data="health:confirm:avg_steps")],
                [InlineKeyboardButton("📊 הצג פירוט יומי", callback_data="health:steps_breakdown")],
                _DEFER_WIZARD_BUTTON_ROW,
                _SKIP_WIZARD_BUTTON_ROW,
            ])
            await _send_wizard_screen(target, "\n".join(lines), keyboard)
            return True

    # Training days: choose from real imported workout history. If the file
    # does not contain enough actual weekday evidence, ask instead of guessing.
    if step_id == WIZARD_STEP_WORKOUT_DAYS and isinstance(fact.get("value"), dict):
        value = fact["value"]
        desired = await _desired_weekly_frequency(user_id)
        target_count = desired if desired is not None else max(1, len(_workout_days_indices(value)))
        proposed, history_source = await _historical_workout_weekdays(user_id, target_count)

        if len(proposed) < target_count:
            lines = [header.rstrip()]
            if desired is not None:
                lines.append(f"עדכנת שאתה רוצה {desired} אימונים בשבוע ✅\n")
            lines.append(
                "לא זיהיתי דפוס מספיק ברור של ימי אימון מתוך היסטוריית האימונים בקובץ."
            )
            lines.append(
                f"כדי לבנות תוכנית ל-{target_count} אימונים בשבוע, כתוב את הימים המועדפים עליך "
                "(לדוגמה: ראשון, שני, רביעי, שישי)."
            )
            lines.append("אפשר גם לדלג כרגע ולחזור לשאלה הזו לפני סיום בניית התוכנית.")
            text = "\n".join(lines)
            keyboard = InlineKeyboardMarkup([
                _DEFER_WIZARD_BUTTON_ROW,
                _SKIP_WIZARD_BUTTON_ROW,
            ])
            await _send_wizard_screen(target, text, keyboard)
            return True

        # Keep display AND the eventual confirm consistent: the proposal becomes
        # the value that gets approved.
        widened = weekday_source_is_widened(history_source)
        value["common_weekdays"] = proposed
        value["weekday_selection_basis"] = "health_workout_history"
        value["weekday_selection_source"] = history_source
        # Record how much evidence actually backs the answer so downstream
        # consumers (and any later correction) can tell a fresh 28-day pattern
        # apart from one scraped out of a widened window.
        value["weekday_selection_widened"] = widened
        value["weekday_selection_window_days"] = weekday_source_window_days(history_source)
        await user_model.set_fact(
            DB, user_id, "workout_pattern", value,
            kind=fact.get("kind", user_model.KIND_ESTIMATE),
            source=fact.get("source", user_model.SOURCE_DERIVED),
            confirmed=False,
        )

        proposed_labels = weekday_labels_he(proposed)
        lines = [header.rstrip()]
        if desired is not None:
            lines.append(f"עדכנת שאתה רוצה {desired} אימונים בשבוע ✅\n")
        if widened:
            # Do NOT claim "the days you trained most" when the default window
            # did not actually support that: say the window was widened and that
            # the answer is a guess the user should correct.
            lines.append(
                "לפי היסטוריית האימונים שתועדה בקובץ, ההערכה שלי לימי האימון היא:\n"
                f"{', '.join(proposed_labels)}."
            )
            window_days = weekday_source_window_days(history_source)
            if window_days is not None:
                lines.append(
                    f"שים לב: ב-{_DEFAULT_WEEKDAY_WINDOW_DAYS} הימים האחרונים לא היה דפוס מספיק ברור, "
                    f"אז הרחבתי את הבדיקה ל-{window_days} הימים האחרונים. "
                    "יכול להיות שזה לא מדויק לשגרה שלך היום."
                )
            else:
                lines.append(
                    f"שים לב: ב-{_DEFAULT_WEEKDAY_WINDOW_DAYS} הימים האחרונים לא היה דפוס מספיק ברור, "
                    "אז השתמשתי בכל היסטוריית האימונים שבקובץ. "
                    "יכול להיות שזה לא מדויק לשגרה שלך היום."
                )
        else:
            lines.append(
                "לפי היסטוריית האימונים שתועדה בקובץ, הימים שבהם התאמנת הכי הרבה הם:\n"
                f"{', '.join(proposed_labels)}."
            )
        lines.append("\nלאשר את הימים האלה לתוכנית האימונים?")
        if desired is not None:
            lines.append(
                f"אם תרצה לשנות — פשוט כתוב {desired} ימים מופרדים בפסיק "
                "(למשל: ראשון, שני, רביעי, שישי)."
            )
        else:
            lines.append(
                "אם תרצה לשנות — פשוט כתוב את הימים הרצויים מופרדים בפסיק "
                "(למשל: ראשון, שלישי, חמישי)."
            )
        text = "\n".join(lines)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ אשר", callback_data=f"health:confirm:{step_id}")],
            _DEFER_WIZARD_BUTTON_ROW,
            _SKIP_WIZARD_BUTTON_ROW,
        ])
        await _send_wizard_screen(target, text, keyboard)
        return True

    detected, scope, hint = _wizard_step_prompt(step_id, fact, quality)
    text = (
        f"{header}{esc(detected)}.\n"
        f"האם לאשר גם עבור {esc(scope)}?\n"
        f"אם לא — ציין {esc(hint)}."
    )
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ אשר", callback_data=f"health:confirm:{step_id}")],
            _DEFER_WIZARD_BUTTON_ROW,
            _SKIP_WIZARD_BUTTON_ROW,
        ]
    )
    await _send_wizard_screen(target, text, keyboard)
    return True


async def _mark_wizard_substep_done(
    user_id: int, step_id: str, value: Any
) -> None:
    """Record one workout sub-step as handled; once every applicable sub-step
    is handled the workout_pattern fact itself is confirmed."""
    from noam_coach.bot.onboarding import set_flow_state

    done = await _wizard_done_steps(user_id)
    if step_id not in done:
        done.append(step_id)
    deferred = [step for step in await _wizard_deferred_steps(user_id) if step != step_id]
    skipped = [step for step in await _wizard_skipped_steps(user_id) if step != step_id]
    await set_flow_state(
        user_id, HEALTH_CONFIRM_FLOW, step_id, _wizard_payload(done, deferred, skipped)
    )
    remaining = [
        s
        for s in _WORKOUT_SUBSTEPS
        if s not in done and _workout_substep_applicable(s, value)
    ]
    if not remaining:
        await user_model.confirm_fact(DB, user_id, "workout_pattern")


async def confirm_health_wizard_step(user_id: int, step_id: str) -> str:
    """Apply one "✅ אשר" tap: harden the data point AND feed it into the
    training plan facts (that is what the approval means). Returns the
    "here is what was approved" line echoed above the next prompt."""
    key = wizard_step_fact_key(step_id)
    fact = await user_model.get_fact(DB, user_id, key)
    value = (fact or {}).get("value")

    if step_id in _WORKOUT_SUBSTEPS and isinstance(value, dict):
        if step_id == WIZARD_STEP_WORKOUT_FREQUENCY:
            freq = value.get("weekly_frequency")
            approved = max(1, min(7, round(float(freq or 1))))
            await _set_wizard_confirmed_fact(
                user_id, "training_days_per_week", approved,
                provenance=_wizard_derivation_note(
                    step_id, value, "weekly_frequency"
                ),
            )
            ack = f"✅ אושר: {approved} אימונים בשבוע"
        elif step_id == WIZARD_STEP_WORKOUT_DAYS:
            indices = _workout_days_indices(value)
            slots = [
                with_weekday_schema({
                    "weekday": day,
                    "start": value.get("typical_hour"),
                    "available": True,
                })
                for day in indices
            ]
            await _set_wizard_confirmed_fact(
                user_id, "weekly_availability", slots,
                provenance=_wizard_derivation_note(
                    step_id, value, "common_weekdays"
                ),
            )
            ack = f"✅ אושר: ימי אימון — {', '.join(_workout_days_labels(value))}"
        elif step_id == WIZARD_STEP_WORKOUT_DURATION:
            minutes = int(round(float(value.get("avg_duration_minutes") or 0)))
            minutes = max(10, min(180, minutes))
            await _set_wizard_confirmed_fact(
                user_id, "session_minutes", minutes,
                provenance=_wizard_derivation_note(
                    step_id, value, "avg_duration_minutes"
                ),
            )
            ack = f"✅ אושר: משך אימון טיפוסי — כ־{minutes} דקות"
        else:  # WIZARD_STEP_WORKOUT_HOUR
            hour = str(value.get("typical_hour"))
            await _set_wizard_confirmed_fact(
                user_id, "workout_window", hour,
                provenance=_wizard_derivation_note(
                    step_id, value, "typical_hour"
                ),
            )
            ack = f"✅ אושר: שעת אימון סביב {hour}"
        await _mark_wizard_substep_done(user_id, step_id, value)
        return ack

    await user_model.confirm_fact(DB, user_id, key)
    label = user_model.display_label(key)
    display = _format_pending_fact_value(key, value)
    return f"✅ אושר: {esc(label)} — {esc(display)}"


async def apply_health_wizard_trend_choice(
    user_id: int, key: str, trend_value: float
) -> str:
    """Apply a trend-proposal button tap on the frequency step: record the
    chosen weekly frequency for the training plan and inside the detected
    pattern, then let the wizard continue to the remaining sub-steps
    (days, hour) instead of swallowing them."""
    fact = await user_model.get_fact(DB, user_id, key)
    current = fact.get("value") if fact and isinstance(fact.get("value"), dict) else {}
    updated = {**current, "weekly_frequency": trend_value}
    await user_model.set_fact(
        DB, user_id, key, updated,
        kind=(fact or {}).get("kind") or user_model.KIND_ESTIMATE,
        source=(fact or {}).get("source") or user_model.SOURCE_DERIVED,
    )
    # A trend button is still a machine proposal the user accepted, not a
    # number the user produced — same provenance treatment as the confirm tap.
    provenance = _wizard_derivation_note(
        WIZARD_STEP_WORKOUT_FREQUENCY, current, "weekly_frequency"
    )
    provenance["source_field"] = "trend_proposal"
    provenance["raw_value"] = trend_value
    await _set_wizard_confirmed_fact(
        user_id, "training_days_per_week", trend_value, provenance=provenance,
    )
    if key == "workout_pattern":
        await _mark_wizard_substep_done(user_id, WIZARD_STEP_WORKOUT_FREQUENCY, updated)
    return f"✅ אושר: {trend_value:g} אימונים בשבוע"


async def prompt_health_wizard_edit(target: Any, user_id: int, step_id: str) -> None:
    """Show a "type your correction" prompt for a wizard step (✏️ שנה ימים).

    Pending edit state is already active from ask_next_health_confirm_step, so
    the user's next free-text message is captured. For training days the prompt
    spells out that exactly the requested number of days must be entered.
    """
    from noam_coach.bot.onboarding import set_pending

    await set_pending(user_id, f"__health_edit_{step_id}__")
    if step_id == WIZARD_STEP_WORKOUT_DAYS:
        desired = await _desired_weekly_frequency(user_id)
        if desired is not None:
            text = (
                f"כתוב בדיוק {desired} ימי אימון, מופרדים בפסיק.\n"
                "למשל: ראשון, שני, רביעי, שישי."
            )
        else:
            text = "כתוב את ימי האימון הרצויים, מופרדים בפסיק.\nלמשל: ראשון, שלישי, חמישי."
    elif step_id in _WORKOUT_SUBSTEP_EDIT_HINTS:
        # The three workout sub-steps share one fact key (workout_pattern), so
        # the hint must be keyed by the step, not the fact key — otherwise they
        # all fall back to the meaningless generic "ערך אחר".
        text = f"כתוב {_WORKOUT_SUBSTEP_EDIT_HINTS[step_id]}."
    else:
        hint = _WIZARD_EDIT_HINTS.get(wizard_step_fact_key(step_id), "ערך אחר")
        text = f"כתוב {hint}."
    keyboard = InlineKeyboardMarkup([_SKIP_WIZARD_BUTTON_ROW])
    await _send_wizard_screen(target, text, keyboard)


async def show_steps_daily_breakdown(target: Any, user_id: int) -> None:
    report = await _wizard_quality_report(user_id)
    steps = (report or {}).get("steps") or {}
    rows = steps.get("daily_breakdown") or []
    if not rows:
        await _send_wizard_screen(
            target,
            "אין פירוט צעדים זמין כרגע.",
            InlineKeyboardMarkup([_DEFER_WIZARD_BUTTON_ROW, _SKIP_WIZARD_BUTTON_ROW]),
        )
        return
    lines = [
        "<b>פירוט יומי לצעדים</b>",
        f"חלון: {esc(str(steps.get('window_start') or '?'))} עד {esc(str(steps.get('window_end') or '?'))}",
        "",
        "תאריך | כל המקורות | שמרני | מקור | ביטחון",
    ]
    for item in rows[-28:]:
        selected = item.get("selected_step_count_for_baseline")
        raw = item.get("total_step_count_all_sources")
        selected_text = "-" if selected is None else f"{int(float(selected)):,}"
        raw_text = "-" if raw is None else f"{int(float(raw)):,}"
        source = item.get("dominant_source") or "-"
        confidence = item.get("confidence") or "-"
        lines.append(
            f"{item.get('date')} | {raw_text} | {selected_text} | {esc(str(source))} | {esc(str(confidence))}"
        )
    lines.extend([
        "",
        f"raw_all_sources_average: {steps.get('raw_all_sources_average')}",
        f"dominant_or_priority_source_average: {steps.get('dominant_or_priority_source_average')}",
        f"selected_planning_baseline: {steps.get('selected_planning_baseline')}",
    ])
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ השתמש כבסיס זמני", callback_data="health:confirm:avg_steps")],
        _DEFER_WIZARD_BUTTON_ROW,
        _SKIP_WIZARD_BUTTON_ROW,
    ])
    await _send_wizard_screen(target, "\n".join(lines), keyboard)


async def apply_health_wizard_text_edit(
    user_id: int, step_id: str, text: str
) -> tuple[bool, str]:
    """Apply a typed correction for a workout sub-step. Returns (handled_ok,
    reply): on parse failure the reply is a retry hint and pending stays."""
    key = wizard_step_fact_key(step_id)
    fact = await user_model.get_fact(DB, user_id, key)
    current = fact.get("value") if fact and isinstance(fact.get("value"), dict) else {}
    kind = (fact or {}).get("kind") or user_model.KIND_ESTIMATE
    source = (fact or {}).get("source") or user_model.SOURCE_DERIVED

    if step_id == WIZARD_STEP_WORKOUT_FREQUENCY:
        match = re.search(r"\d+", text)
        if not match:
            return False, "כתוב מספר אימונים בשבוע, למשל: 3."
        approved = max(1, min(7, int(match.group(0))))
        updated = {**current, "weekly_frequency": float(approved)}
        await user_model.set_fact(DB, user_id, key, updated, kind=kind, source=source)
        await user_model.set_fact(
            DB, user_id, "training_days_per_week", approved,
            kind=user_model.KIND_FACT, source=user_model.SOURCE_USER, confirmed=True,
        )
        await _mark_wizard_substep_done(user_id, step_id, updated)
        return True, f"עודכן: {approved} אימונים בשבוע ✅"

    if step_id == WIZARD_STEP_WORKOUT_DAYS:
        from noam_coach.services.availability import (
            parse_hebrew_availability_answer,
            save_user_training_availability,
        )

        parsed = parse_hebrew_availability_answer(text)
        slots = [
            {**slot, "start": slot.get("start") or current.get("typical_hour")}
            for slot in parsed.weekly_availability
        ]
        if not slots:
            return False, "לא זיהיתי ימים. כתוב למשל: ראשון, שלישי, חמישי."
        indices = sorted({int(slot["weekday"]) for slot in slots})
        # The chosen days must match the requested weekly frequency — otherwise
        # the plan can't be built for the number of workouts the user asked for.
        desired = await _desired_weekly_frequency(user_id)
        if desired is not None and len(indices) != desired:
            return (
                False,
                f"בחרת {len(indices)} ימים, אבל הגדרת {desired} אימונים בשבוע. "
                f"צריך לבחור בדיוק {desired} ימים.",
            )
        parsed = type(parsed)(slots, parsed.workout_window, parsed.session_minutes)
        await user_model.set_fact(
            DB, user_id, "weekly_availability", slots,
            kind=user_model.KIND_FACT, source=user_model.SOURCE_USER, confirmed=True,
        )
        await save_user_training_availability(DB, user_id, parsed)
        updated = {
            **current,
            "common_weekdays": indices,
            "weekday_schema": WEEKDAY_SCHEMA_VERSION,
        }
        await user_model.set_fact(DB, user_id, key, updated, kind=kind, source=source)
        await _mark_wizard_substep_done(user_id, step_id, updated)
        labels = weekday_labels_he(indices)
        return True, f"עודכן: ימי אימון — {', '.join(labels)} ✅"

    if step_id == WIZARD_STEP_WORKOUT_HOUR:
        match = re.search(r"\b(\d{1,2})(?::(\d{2}))?\b", text)
        hour = int(match.group(1)) if match else -1
        minute = int(match.group(2) or 0) if match else 0
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return False, "כתוב שעה, למשל: 18:30."
        hhmm = f"{hour:02d}:{minute:02d}"
        await user_model.set_fact(
            DB, user_id, "workout_window", hhmm,
            kind=user_model.KIND_FACT, source=user_model.SOURCE_USER, confirmed=True,
        )
        updated = {**current, "typical_hour": hhmm}
        await user_model.set_fact(DB, user_id, key, updated, kind=kind, source=source)
        await _mark_wizard_substep_done(user_id, step_id, updated)
        return True, f"עודכן: שעת אימון {hhmm} ✅"

    if step_id == WIZARD_STEP_WORKOUT_DURATION:
        match = re.search(r"\d+", text)
        if not match:
            return False, "כתוב משך אימון בדקות, למשל: 55."
        minutes = max(10, min(180, int(match.group(0))))
        # TASK-2: a manual correction is persisted as the confirmed planning
        # preference (session_minutes) so the later duration question is skipped.
        await user_model.set_fact(
            DB, user_id, "session_minutes", minutes,
            kind=user_model.KIND_FACT, source=user_model.SOURCE_USER, confirmed=True,
        )
        updated = {**current, "avg_duration_minutes": float(minutes)}
        await user_model.set_fact(DB, user_id, key, updated, kind=kind, source=source)
        await _mark_wizard_substep_done(user_id, step_id, updated)
        return True, f"עודכן: משך אימון טיפוסי — כ־{minutes} דקות ✅"

    if key == "avg_steps":
        match = re.search(r"\d[\d,]*", text)
        if not match:
            return False, "כתוב מספר צעדים יומי, למשל: 7000."
        steps = int(match.group(0).replace(",", ""))
        if not (500 <= steps <= 50000):
            return False, "מספר הצעדים לא נראה תקין. כתוב למשל: 7000."
        await user_model.set_fact(
            DB, user_id, "avg_steps", steps,
            kind=user_model.KIND_FACT, source=user_model.SOURCE_USER, confirmed=True,
        )
        return True, f"עודכן: {steps:,} צעדים ביום ✅"

    return False, "לא הצלחתי לעדכן את הפריט הזה."


# ---------------------------------------------------------------------------
# W1-8: standalone schedule correction (no wizard step pending)
# ---------------------------------------------------------------------------
#
# The defect this closes: the user wrote "לדעתי אמרתי לו שאני מתאמן בשישי לא
# בשבת" as ordinary free text. The bot answered "צודק, אשתמש במידע שכבר יש לי"
# and mutated nothing — weekly_availability still decoded to Mon/Wed/Sat/Sun.
# The parsing was never the problem: apply_health_wizard_text_edit already
# reads a typed weekday correction correctly, but it is only reachable while a
# health-wizard step is pending. This section makes the SAME parsing reachable
# from free text, and nothing else about the wizard path changes.

# (1) How a correction is recognised.
#
# Recognition has to be specific enough that a weekday mentioned in passing —
# "אכלתי בשבת סלט עם טונה" — can never rewrite the training schedule. A bare
# weekday token is therefore NOT a trigger. We require BOTH:
#
#   * a training predicate (מתאמן / אימון / ...), so the sentence is about
#     training rather than about food, sleep or an appointment; and
#   * a correction/assertion frame (לא / במקום / אמרתי / טעות / ...), so we act
#     on "I train on Friday NOT Saturday" but stay out of the way of a plain
#     scheduling request that other flows own.
#
# Both lists are deliberately narrow. A sentence that fails either test returns
# `applied=False` and the caller must not claim agreement.

_SCHEDULE_TRAINING_MARKERS: tuple[str, ...] = (
    "מתאמן", "מתאמנת", "אימון", "אימונים", "להתאמן", "מתאמנים", "אתאמן",
)

# Frames that make the utterance a *correction or restatement* of the schedule.
_SCHEDULE_CORRECTION_MARKERS: tuple[str, ...] = (
    "לא ב", "לא ה", "ולא ", " לא ", "במקום", "אמרתי", "טעות", "התכוונתי",
    "תקן", "שיניתי", "בעצם", "לדעתי",
)

# (2) Negation. The existing parser is intentionally additive: on the live
# utterance it returns BOTH Friday(4) and Saturday(5), because "לא בשבת" still
# contains a weekday token. Feeding that straight through would have ADDED
# Friday while keeping Saturday — a different wrong answer, not a fix. So the
# text is split on the negation marker first: days before it are asserted, days
# after it are removed.
_NEGATION_SPLIT_RE = re.compile(r"(?:\bולא\b|\bלא\b)")


def parse_schedule_correction(text: str) -> dict[str, Any] | None:
    """Parse a free-text training-day correction, or None if this is not one.

    Returns ``{"asserted": [...], "removed": [...], "window": str | None,
    "session_minutes": int | None}`` with weekday indices in
    ``monday_first_v1``. Returning None means "not a schedule correction" —
    callers must then leave the schedule alone.
    """
    from noam_coach.services.availability import parse_hebrew_availability_answer

    raw = (text or "").strip()
    if not raw:
        return None
    padded = f" {raw} "
    if not any(marker in padded for marker in _SCHEDULE_TRAINING_MARKERS):
        return None
    if not any(marker in padded for marker in _SCHEDULE_CORRECTION_MARKERS):
        return None

    # Split on the negation so "בשישי לא בשבת" yields asserted=[4], removed=[5].
    parts = _NEGATION_SPLIT_RE.split(padded, maxsplit=1)
    head = parts[0]
    tail = parts[1] if len(parts) > 1 else ""

    asserted_parsed = parse_hebrew_availability_answer(head)
    asserted = sorted({int(s["weekday"]) for s in asserted_parsed.weekly_availability})
    removed = sorted(
        {
            int(s["weekday"])
            for s in parse_hebrew_availability_answer(tail).weekly_availability
        }
        if tail.strip()
        else set()
    )
    # A day cannot be both asserted and removed; the assertion wins.
    removed = [day for day in removed if day not in asserted]

    if not asserted and not removed:
        return None
    return {
        "asserted": asserted,
        "removed": removed,
        "window": asserted_parsed.workout_window,
        "session_minutes": asserted_parsed.session_minutes,
    }


async def apply_schedule_correction(user_id: int, text: str) -> tuple[bool, str]:
    """Apply a free-text training-day correction outside the health wizard.

    Returns ``(applied, reply)``. When ``applied`` is False the reply says so
    explicitly — the W1-8 defect was a reply claiming agreement ("צודק,
    אשתמש במידע שכבר יש לי") over a discarded input, and a silent no-op here
    would reproduce exactly that.
    """
    from noam_coach.services.availability import (
        ParsedAvailabilityAnswer,
        save_user_training_availability,
    )

    parsed = parse_schedule_correction(text)
    if parsed is None:
        return False, "לא זיהיתי כאן שינוי בימי האימון. כתוב למשל: אני מתאמן בשישי, לא בשבת."

    asserted: list[int] = parsed["asserted"]
    removed: list[int] = parsed["removed"]

    # Start from the currently active weekday set so a *partial* correction
    # ("בשישי לא בשבת") edits the schedule instead of replacing it: Mon/Wed/Sun
    # were never in dispute and must survive.
    current = await _current_training_weekdays(user_id)
    updated = sorted((set(current) | set(asserted)) - set(removed))

    if not updated:
        return (
            False,
            "לא עדכנתי — התיקון הזה מרוקן את כל ימי האימון. "
            "כתוב אילו ימים כן מתאימים לך.",
        )
    if updated == sorted(set(current)):
        labels = ", ".join(weekday_labels_he(updated))
        return True, f"ימי האימון כבר מעודכנים: {labels} ✅"

    # (3) Provenance. This is a weekday set the user STATED, so it is written as
    # source=user_report / kind=fact / confirmed=True. That is deliberately
    # different from W1-6, which stores wizard-confirmed *derivations* as
    # source=derived + confirmed=True precisely so they are not mistaken for
    # user statements. Here there is no derivation involved at all.
    typical_hour = parsed["window"] or await _current_training_hour(user_id)
    minutes = parsed["session_minutes"]
    slots = [
        with_weekday_schema({
            "weekday": day,
            "start": typical_hour,
            "minutes": minutes or _correction_default_minutes(current, day),
            "available": True,
        })
        for day in updated
    ]

    # (2) Which facts move together — all of these carry the weekday set, and
    # W1-2 showed what happens when one is updated and another is not (training
    # frequency read 0.2 in one store and 4.0 in another, costing 130 kcal/day).
    # They are written as ONE unit:
    #   weekly_availability      — the per-day slot list planners read;
    #   preferred/active_training_days + training_days_per_week — written by
    #       save_user_training_availability, the same call the wizard path uses;
    #   detected_training_days   — the Health-derived mirror; left stale it
    #       would keep re-proposing Saturday as an observed training day;
    #   active_workout_plan.sessions — the built plan still lists a Saturday
    #       session otherwise, so the plan and the availability disagree.
    await user_model.set_fact(
        DB, user_id, "weekly_availability", slots,
        kind=user_model.KIND_FACT, source=user_model.SOURCE_USER, confirmed=True,
    )
    await save_user_training_availability(
        DB,
        user_id,
        ParsedAvailabilityAnswer(
            weekly_availability=slots,
            workout_window=parsed["window"],
            session_minutes=minutes,
        ),
    )
    # detected_training_days is normally a Health *estimate*. Once the user has
    # stated the days, leaving the old inference in place is what lets a later
    # import resurrect the removed day, so it is realigned and marked as coming
    # from the user rather than from inference.
    await user_model.set_fact(
        DB, user_id, "detected_training_days", updated,
        kind=user_model.KIND_FACT, source=user_model.SOURCE_USER, confirmed=True,
    )
    await _realign_active_workout_plan_weekdays(user_id, updated)

    labels = ", ".join(weekday_labels_he(updated))
    reply = f"עודכן: ימי אימון — {labels} ✅"
    if removed:
        reply += f" (הסרתי: {', '.join(weekday_labels_he(sorted(removed)))})"
    return True, reply


def _correction_default_minutes(current_days: list[int], day: int) -> int:
    """Session length for a newly asserted day. Kept simple on purpose: the
    correction is about WHICH day, not how long, so we do not invent a new
    duration — 45 matches the availability parser's own default."""
    del current_days, day
    return 45


async def _current_training_weekdays(user_id: int) -> list[int]:
    """The weekday set currently in force, preferring the most authoritative
    store. Reads only; never writes, so an unrecognised utterance is inert."""
    for key in ("weekly_availability", "active_training_days", "preferred_training_days"):
        value = await user_model.get_value(DB, user_id, key)
        if not value or not isinstance(value, list):
            continue
        days: list[int] = []
        for item in value:
            raw = item.get("weekday") if isinstance(item, dict) else item
            if isinstance(item, dict) and not item.get("available", True):
                continue
            schema = (
                item.get("weekday_schema") if isinstance(item, dict) else None
            ) or WEEKDAY_SCHEMA_VERSION
            normalized = normalize_weekday(raw, schema)
            if normalized.weekday is not None:
                days.append(normalized.weekday)
        if days:
            return sorted(set(days))
    return []


async def _current_training_hour(user_id: int) -> str | None:
    """Existing typical workout time, so a day-only correction keeps the hour."""
    for key in ("workout_window", "weekly_availability"):
        value = await user_model.get_value(DB, user_id, key)
        if isinstance(value, str) and len(value) == 5 and value[2] == ":":
            return value
        if isinstance(value, list):
            for slot in value:
                start = slot.get("start") if isinstance(slot, dict) else None
                if isinstance(start, str) and len(start) == 5 and start[2] == ":":
                    return start
    return None


async def _realign_active_workout_plan_weekdays(user_id: int, weekdays: list[int]) -> None:
    """Re-pin the fact-tier weekly plan's sessions onto the corrected weekdays.

    Only the ``weekday`` field moves — session codes, names and order are the
    user's plan and are not this function's to rewrite. If the plan has more
    sessions than corrected days (or none at all) it is left untouched and the
    mismatch is surfaced by the normal plan-rebuild path rather than being
    papered over here.
    """
    plan = await user_model.get_value(DB, user_id, "active_workout_plan")
    if not isinstance(plan, dict):
        return
    sessions = plan.get("sessions")
    if not isinstance(sessions, list) or not sessions:
        return
    if len(sessions) != len(weekdays):
        return
    realigned = [
        {**session, "weekday": day}
        for session, day in zip(sessions, weekdays)
        if isinstance(session, dict)
    ]
    if len(realigned) != len(sessions):
        return
    await user_model.set_fact(
        DB, user_id, "active_workout_plan",
        {**plan, "sessions": realigned, "weekday_schema": WEEKDAY_SCHEMA_VERSION},
        kind=user_model.KIND_FACT, source=user_model.SOURCE_USER, confirmed=True,
    )


async def skip_health_wizard_item(user_id: int, step_id: str) -> None:
    """Defer one wizard item without applying or invalidating it.

    "דלג כרגע" is intentionally different from "דלג על שאר האישורים": the
    FIRST skip moves the current question behind the other pending questions
    and it returns once more before the wizard can finish. A SECOND skip of
    the same item (review 2026-07-18_1 / F-01) is terminal for this run —
    deferral with no terminal state trapped the user in an identical-screen
    loop on the last remaining item. The fact stays pending/unconfirmed.
    """
    from noam_coach.bot.onboarding import set_flow_state

    done = await _wizard_done_steps(user_id)
    deferred = await _wizard_deferred_steps(user_id)
    skipped = await _wizard_skipped_steps(user_id)
    if step_id in deferred:
        deferred = [step for step in deferred if step != step_id]
        if step_id not in skipped:
            skipped.append(step_id)
    elif step_id not in done and step_id not in skipped:
        deferred.append(step_id)
    await set_flow_state(
        user_id, HEALTH_CONFIRM_FLOW, step_id, _wizard_payload(done, deferred, skipped)
    )


async def start_health_confirm_wizard(
    message: Any, user_id: int, *, next_step: str, summary_text: str
) -> None:
    """Kick off the RE10-4 wizard right after import, before the summary.

    ``next_step`` ("onboarding" | "reconciliation") is remembered so that
    whichever handler ends the wizard (a confirm/edit reaching the end, or
    "skip the rest") knows what used to run right after the old bulk gate —
    finish_health_confirm_wizard performs that continuation. ``summary_text``
    is the pre-rendered import summary (counts/date-range) to show once the
    wizard completes — rendered once here since HealthImportOutcome itself
    is not safely JSON-serializable for flow-state storage.
    """
    from noam_coach.bot.onboarding import set_flow_state, clear_flow_state

    # A fresh import starts a fresh wizard — forget sub-steps handled in a
    # previous run so every newly-detected item is confirmed again.
    await clear_flow_state(user_id, HEALTH_CONFIRM_FLOW)
    await set_flow_state(
        user_id, HEALTH_POST_WIZARD_FLOW, next_step, {"summary_text": summary_text}
    )
    started = await ask_next_health_confirm_step(message, user_id)
    if not started:
        await finish_health_confirm_wizard(message, user_id)


async def finish_health_confirm_wizard(
    target: Any, user_id: int, *, ack_text: str | None = None
) -> None:
    """Show the final import summary + run the step that used to follow the
    old bulk health:activate gate immediately (onboarding basics or
    reconciliation), then clear the post-wizard continuation marker.
    ``ack_text`` echoes the last approval above the summary so the user sees
    what was just confirmed even on the final step.
    """
    from noam_coach.bot.onboarding import get_flow_state, clear_flow_state
    from noam_coach.bot.ui import home_keyboard_for_user

    state = await get_flow_state(user_id, HEALTH_POST_WIZARD_FLOW)
    payload = state or {}
    next_step = payload.get("step") or "reconciliation"
    summary_text = (payload.get("payload") or {}).get("summary_text", "")
    await clear_flow_state(user_id, HEALTH_POST_WIZARD_FLOW)

    # LOG-015: the confirm wizard sets its own pending question
    # (__health_edit_*__), which suspends the active health_import microflow,
    # which in turn had already suspended the parent onboarding question that
    # was interrupted by the import (a safety-critical training_limitations
    # question included). Now that the wizard is finished, deterministically
    # unwind that whole nested chain by flow identity so the original parent
    # question is auto-restored — otherwise a suspended safety question is
    # silently orphaned and never re-asked.
    await _restore_parent_after_health_flow(user_id)

    planning_summary = await _health_confirmed_planning_summary_text(user_id)
    text = (
        (summary_text + "\n\n" + planning_summary)
        if summary_text
        else ("<b>סיכום הייבוא</b>\n\n" + planning_summary)
    )
    if ack_text:
        text = f"{ack_text}\n\n{text}"
    keyboard = await home_keyboard_for_user(user_id)
    await _send_wizard_screen(target, text, keyboard)

    message = getattr(target, "message", target)
    # TASK-1: the new post-import summary above already shows the confirmed
    # planning data and ends with the reduced "🎯 השלם את התוכנית שלי" menu, so
    # for the onboarding path we must NOT trigger the legacy base-data
    # confirmation sequence (show_onboarding_basics: "אישור נתוני בסיס" /
    # "זיהיתי מהנתונים שיובאו" / "✅ הכול נכון" with מקור/אמינות) — it duplicated
    # weight/body-fat/base data and exposed obsolete source/confidence metadata.
    # The reconciliation path stays: it surfaces one actionable reported-vs-actual
    # insight and is not a duplicate confirmation.
    if next_step != "onboarding":
        await run_post_import_reconciliation(message, user_id)


def _confirmed_planning_value(fact: dict[str, Any] | None) -> Any | None:
    """Return a fact value only when it can be used as an accepted plan input."""
    if not fact or fact.get("kind") == user_model.KIND_GAP:
        return None
    if not fact.get("confirmed"):
        return None
    return fact.get("value")


def _format_weekly_availability(value: Any) -> str | None:
    if not isinstance(value, list):
        return None
    days: list[int] = []
    for slot in value:
        if not isinstance(slot, dict) or not slot.get("available", True):
            continue
        normalized = normalize_weekday(
            slot.get("weekday"),
            slot.get("weekday_schema") or WEEKDAY_SCHEMA_VERSION,
        )
        if normalized.weekday is not None:
            days.append(normalized.weekday)
    if not days:
        return None
    return ", ".join(weekday_labels_he(sorted(set(days))))


def _format_sleep_schedule(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    bedtime = value.get("typical_bedtime") or value.get("bedtime")
    wake = value.get("typical_wake_time") or value.get("wake_time")
    if bedtime and wake:
        return f"{bedtime}–{wake}"
    return None


async def _health_confirmed_planning_summary_text(user_id: int) -> str:
    """Final Health wizard summary: only confirmed/user-overridden plan inputs.

    Imported estimates can be excellent hints, but the final screen must not
    make them look like decisions. This mirrors the facts consumed by the plan
    readiness/generation path: confirmed user facts and explicitly confirmed
    Health values are shown; unconfirmed estimates stay pending.
    """
    fact_keys = (
        "training_days_per_week",
        "weekly_availability",
        "workout_window",
        "weight_kg",
        "avg_steps",
        "sleep_schedule",
        "training_limitations",
    )
    facts = {
        key: (
            await user_model.get_training_limitations_fact(DB, user_id)
            if key == "training_limitations"
            else await user_model.get_fact(DB, user_id, key)
        )
        for key in fact_keys
    }

    lines = [
        "<b>נתוני תכנון שאושרו עד עכשיו</b>",
    ]

    confirmed_any = False

    frequency = _confirmed_planning_value(facts["training_days_per_week"])
    if frequency is not None:
        lines.append(f"• אימונים בשבוע: {esc(user_model.display_value('training_days_per_week', frequency))}")
        confirmed_any = True

    days = _format_weekly_availability(_confirmed_planning_value(facts["weekly_availability"]))
    if days:
        lines.append(f"• ימי אימון: {esc(days)}")
        confirmed_any = True

    workout_window = _confirmed_planning_value(facts["workout_window"])
    if workout_window:
        lines.append(f"• שעת אימון מועדפת: {esc(str(workout_window))}")
        confirmed_any = True

    weight = _confirmed_planning_value(facts["weight_kg"])
    if weight is not None:
        lines.append(f"• משקל נוכחי: {esc(user_model.display_value('weight_kg', weight))}")
        confirmed_any = True

    steps = _confirmed_planning_value(facts["avg_steps"])
    if steps is not None:
        lines.append(f"• בסיס צעדים יומי: {esc(user_model.display_value('avg_steps', steps))}")
        confirmed_any = True

    sleep_value = _confirmed_planning_value(facts["sleep_schedule"])
    sleep_display = _format_sleep_schedule(sleep_value)
    if sleep_display:
        lines.append(f"• שינה: {esc(sleep_display)}")
        confirmed_any = True
    elif facts["sleep_schedule"] and facts["sleep_schedule"].get("kind") != user_model.KIND_GAP:
        lines.append("• שינה: עדיין לא אושרה")

    limitations = _confirmed_planning_value(facts["training_limitations"])
    if limitations and limitations != "none":
        lines.append(f"• כאב, פציעה או מגבלה: {esc(user_model.display_value('training_limitations', limitations))}")
        confirmed_any = True

    if not confirmed_any:
        lines.append("• עדיין אין נתוני תכנון מאושרים מהייבוא.")

    readiness = await user_model.compute_all_readiness(DB, user_id)
    missing_labels: list[str] = []
    for profile_name in ("safety", "workout", "nutrition"):
        for label in readiness.get(profile_name, {}).get("missing_labels", []):
            if label not in missing_labels:
                missing_labels.append(label)

    lines.extend(["", "<b>עדיין חסר לפני סיום בניית התוכנית</b>"])
    if missing_labels:
        lines.extend([
            "",
            "👉 <b>השלב הבא:</b> להשלים את הפרטים שנשארו כדי לבנות את תוכנית האימונים והתזונה שלך.",
            f"נשארו לך {len(missing_labels)} פרטים להשלמה.",
            "כדי להשלים את הפרופיל, לחץ על <b>🎯 השלם את התוכנית שלי</b> והמשך לענות על השאלות שנותרו.",
            "אפשר לדלג על שאלה ספציפית ולחזור אליה בהמשך.",
            "אחרי שהמידע יהיה שלם, אשתמש בנתוני HealthKit שאושרו ובתשובות שלך כדי לבנות את התוכנית.",
            "",
        ])
        lines.extend(f"• {esc(label)}" for label in missing_labels[:8])
        if len(missing_labels) > 8:
            lines.append(f"• ועוד {len(missing_labels) - 8} פריטים")
    else:
        lines.append("• אין פריטי חובה חסרים כרגע.")

    return "\n".join(lines)


async def _health_import_followup_text(user_id: int) -> str:
    """RE10-4 / D15: the "דורש אישור" section moved into the per-fact wizard
    (ask_next_health_confirm_step), shown BEFORE this summary now — so this
    text only lists what is still genuinely missing, not what is pending
    the wizard's own confirmation.
    """
    readiness = await user_model.compute_all_readiness(DB, user_id)
    missing_labels: list[str] = []
    for profile_name in ("safety", "workout", "nutrition"):
        for label in readiness.get(profile_name, {}).get("missing_labels", []):
            if label not in missing_labels:
                missing_labels.append(label)

    lines = ["", "<b>מה עדיין צריך כדי להשלים תמונה מלאה?</b>"]

    if missing_labels:
        lines.append("<b>עדיין חסר:</b>")
        lines.extend(f"• {esc(label)}" for label in missing_labels[:8])
        if len(missing_labels) > 8:
            lines.append(f"• ועוד {len(missing_labels) - 8} פריטים")
    else:
        lines.append("• אין פריטי חובה חסרים כרגע.")
    return "\n".join(lines)


@runtime_bound(RUNTIME_NAMES)
async def import_health_export_file(
    user_id: int,
    source_path: Path,
    *,
    max_bytes: int,
    audit_source: str,
) -> HealthImportOutcome:
    """Validate, parse and persist one ZIP/XML export without deleting it."""
    path = Path(source_path).resolve(strict=True)
    if path.suffix.casefold() not in {".zip", ".xml"}:
        raise ValueError("צריך קובץ ZIP או XML של ייצוא Apple Health")
    actual_bytes = path.stat().st_size
    if actual_bytes <= 0:
        raise ValueError("קובץ הייצוא ריק")
    if actual_bytes > max_bytes:
        raise ValueError(f"Health export exceeds {max_bytes // (1024 * 1024)}MB")
    if path.suffix.casefold() == ".zip":
        if not health_import.is_valid_zip(path):
            raise ValueError("הקובץ אינו ZIP תקין")
    elif not health_import.looks_like_xml(path):
        raise ValueError("הקובץ אינו XML תקין")

    folder = Path(SETTINGS.storage_dir) / "health_import"
    folder.mkdir(parents=True, exist_ok=True)
    extract_dir = folder / f"extract_{secrets.token_hex(8)}"
    try:
        def parse_to_rows() -> tuple[list[health_import.HealthRow], Any]:
            if path.suffix.casefold() == ".zip":
                xml_path = health_import.extract_zip(
                    path,
                    extract_dir,
                    max_members=SETTINGS.health_import_max_zip_members,
                    max_xml_bytes=SETTINGS.health_import_max_xml_mb * 1024 * 1024,
                    max_compression_ratio=SETTINGS.health_import_max_compression_ratio,
                )
            else:
                xml_path = path
            if not xml_path:
                raise RuntimeError("לא נמצא קובץ XML בייצוא")
            return health_import.summarize(
                health_import.iter_health_rows(
                    xml_path,
                    retention_days=SETTINGS.health_retention_days,
                )
            )

        import_started = datetime.now(timezone.utc).isoformat()
        rows, summary = await asyncio.to_thread(parse_to_rows)
        inserted, duplicates = await upsert_health_rows(user_id, rows)
        await sync_health_measurements_to_facts(user_id)
        profile = await save_routine_profile(user_id)
        if inserted > 0:
            # FIX 54: a HealthKit import (e.g. today's workout) can change
            # the current-day reality (workout completion, weight, sleep
            # routine) while old decisions -- the active daily menu, an
            # active next-meal recommendation built from pre-import state --
            # remain actionable. Reuse the same invalidation contract meal
            # events already trigger (Batch A/E). Gated on inserted>0 so a
            # duplicate-only import (nothing new) causes no invalidation,
            # matching the addendum's explicit required behavior.
            with suppress(Exception):
                from noam_coach.services.day_state_invalidation import invalidate_day_projections

                await invalidate_day_projections(DB, user_id, reason="health_import")
        dataset_end = await routine.newest_health_sample_date(DB, user_id, TZ)
        dataset_end_date = dataset_end.isoformat() if dataset_end else summary.max_date
        import_completed = datetime.now(timezone.utc).isoformat()
        # Get total stored records for user
        total_row = await DB.fetch_one(
            "SELECT COUNT(*) AS cnt FROM health WHERE user_id=?",
            (user_id,),
        )
        total_stored = int(total_row["cnt"]) if total_row else 0
        await write_audit(
            user_id,
            "health_import",
            "health",
            None,
            inserted=inserted,
            duplicates=duplicates,
            source=audit_source,
        )
        await event_log.append_event(
            DB,
            user_id,
            "local_health_import_completed",
            entity="health",
            source=audit_source,
            properties={
                "inserted": inserted,
                "duplicates": duplicates,
                "total_rows_in_file": summary.rows,
                "source_date_range": f"{summary.min_date}..{dataset_end_date}",
                "total_stored": total_stored,
                "newest_record": dataset_end_date,
            },
        )
        return HealthImportOutcome(
            inserted=inserted,
            duplicates=duplicates,
            updated=0,
            invalid=summary.invalid_records,
            summary=summary,
            profile=profile,
            source_file=path.name,
            total_stored=total_stored,
            import_started=import_started,
            import_completed=import_completed,
            dataset_end_date=dataset_end_date,
        )
    finally:
        with suppress(Exception):
            if extract_dir.exists():
                shutil.rmtree(extract_dir, ignore_errors=True)


def _chain_has_health_import(flow: Any) -> bool:
    """True if ``flow`` is the health-import microflow or has it anywhere in
    its suspended chain — i.e. a health import is the reason this flow (or an
    ancestor of it) exists and still owes a parent-resume."""
    if flow.name == conversation.FlowName.health_import:
        return True
    snapshot = flow.suspended
    depth = 0
    while isinstance(snapshot, dict) and depth < 16:
        name = snapshot.get("name") or snapshot.get("flow")
        if name == conversation.FlowName.health_import.value:
            return True
        snapshot = (snapshot.get("payload") or {}).get("suspended")
        depth += 1
    return False


async def _restore_parent_after_health_flow(user_id: int) -> None:
    """Unwind the full nested suspension chain created by the health-import
    microflow (and its confirm wizard) back to the original parent question.

    Runs after the wizard finishes, or when the import ends without a wizard.
    Uses ``resume_all_suspended`` so a 2-deep nest (parent question ->
    health_import -> wizard __health_edit_*__) is fully restored to the parent,
    never left one level short.  When nothing was suspended (import started
    from idle), the active flow is simply cleared.
    """
    with suppress(Exception):
        resumed = await conversation.resume_all_suspended(DB, user_id)
        if resumed is None:
            await conversation.clear_active_flow(DB, user_id)


async def _finish_health_import_flow(user_id: int) -> None:
    with suppress(Exception):
        current_flow = await conversation.get_active_flow(DB, user_id)
        # LOG-015: the guard used to require the active flow to still literally
        # be ``health_import``. Once the confirm wizard runs, the active flow is
        # the wizard's own onboarding_question (__health_edit_*__) with
        # health_import suspended one level below — so the old guard was already
        # false here and the parent question was never restored. Key off flow
        # identity (the health_import anywhere in the chain) instead. But while
        # the confirm wizard is still pending it OWNS the resume (it will call
        # _restore_parent_after_health_flow on finish); tearing it down here
        # would destroy an in-progress safety question. Detect that via the
        # persisted post-wizard marker and defer to the wizard in that case.
        from noam_coach.bot.onboarding import get_flow_state

        wizard_pending = await get_flow_state(user_id, HEALTH_POST_WIZARD_FLOW)
        if wizard_pending:
            return
        if _chain_has_health_import(current_flow):
            await _restore_parent_after_health_flow(user_id)


@runtime_bound(RUNTIME_NAMES)
async def _begin_health_import_flow(user_id: int, *, source: str) -> bool:
    route_decision = await conversation.ConversationRouter.route(DB, user_id, "document")
    if route_decision.flow.name == conversation.FlowName.health_import:
        return False
    await conversation.set_active_flow(
        DB,
        user_id,
        conversation.FlowName.health_import,
        step="processing",
        payload={"source": source},
        suspend_current=not route_decision.flow.is_idle,
        expiry_minutes=120,
    )
    return True


@runtime_bound(RUNTIME_NAMES)
async def command_import(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if not await is_allowed(update):
        return
    user_id = await ensure_user(update)
    await track_event(user_id, "command_import")
    roots = allowed_roots_from_text(SETTINGS.local_health_import_allowed_roots)
    roots_text = "\n".join(f"• <code>{esc(str(root))}</code>" for root in roots)
    await update.effective_message.reply_text(
        "<b>ייבוא Apple Health בדיעבד</b>\n\n"
        "אפשר באחת משתי דרכים:\n"
        "1. לצרף כאן קובץ ZIP/XML.\n"
        "2. כשהבוט רץ על אותו מחשב Windows — לשלוח בצ'אט את הנתיב המלא "
        "לקובץ או לתיקייה שמכילה אותו.\n\n"
        "דוגמה:\n"
        "<code>C:\\Users\\user\\Desktop\\Apple Health Export</code>\n\n"
        "התיקיות המורשות כרגע:\n"
        f"{roots_text}\n\n"
        "הייבוא הוא תקופתי בלבד; סנכרון HealthKit שוטף כבוי כברירת מחדל.",
        parse_mode=ParseMode.HTML,
    )


@runtime_bound(RUNTIME_NAMES)
async def command_import_path(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_allowed(update):
        return
    user_id = await ensure_user(update)
    text = (update.effective_message.text or "").partition(" ")[2].strip()
    if not text:
        await command_import(update, context)
        return
    await try_handle_local_health_path(update, context, user_id, text, force=True)


@runtime_bound(RUNTIME_NAMES)
async def command_flags(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if not await is_allowed(update):
        return
    user_id = await ensure_user(update)
    await track_event(user_id, "command_flags")
    text = (update.effective_message.text or "").partition(" ")[2].strip()
    if not text:
        flags = await get_daily_flags(user_id)
        await update.effective_message.reply_text(
            "כתוב אחרי /flags את האינדיקציות להיום, למשל:\n"
            "<code>/flags ריטלין, צום עד 14:00, כתף תפוסה</code>\n\n"
            f"אינדיקציות נוכחיות להיום: {flags or 'אין'}",
            parse_mode=ParseMode.HTML,
        )
        return
    flags = await get_daily_flags(user_id)
    flags["notes"] = text
    lowered = text.casefold()
    ritalin_negated = bool(re.search(r"(?:לא|בלי|טרם)\s+(?:לקחתי\s+)?ריטלין", lowered))
    fasting_negated = bool(re.search(r"(?:לא|בלי|איני|אני לא)\s+(?:ב)?צום", lowered))
    if "ריטלין" in text:
        flags["ritalin"] = not ritalin_negated
    if "צום" in text:
        flags["fasting"] = not fasting_negated
    await set_daily_flags(user_id, flags)
    # LOG-012: store derived booleans only — never the raw note free-text.
    await write_audit(
        user_id,
        "daily_flags",
        "flags",
        None,
        ritalin=bool(flags.get("ritalin")),
        fasting=bool(flags.get("fasting")),
        has_note=bool(text),
    )
    await update.effective_message.reply_text("נרשם להיום. אתאים את ההמלצות בהתאם 👍")


@runtime_bound(RUNTIME_NAMES)
async def try_handle_local_health_path(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    text: str,
    *,
    force: bool = False,
) -> bool:
    """Import a local export path sent in chat; return whether it owned the turn."""
    if not force and not looks_like_local_health_path(text):
        return False
    message = update.effective_message
    if not SETTINGS.enable_local_health_path_import:
        await message.reply_text("ייבוא מנתיב מקומי כבוי בהגדרות.")
        return True
    chat = update.effective_chat
    if chat is not None and getattr(chat, "type", "private") != "private":
        await message.reply_text("מטעמי פרטיות, שלח נתיב מקומי רק בצ'אט הפרטי עם הבוט.")
        return True
    if not await _begin_health_import_flow(user_id, source="local_path"):
        await message.reply_text("כבר מתבצע ייבוא Health. אמתין לסיום לפני ייבוא נוסף.")
        return True

    progress = await message.reply_text("📂 בודק את הנתיב ומייבא את קובץ הייצוא…")
    await track_event(user_id, "health_import_started", source="local_path")
    await event_log.append_event(
        DB, user_id, "local_health_import_started",
        entity="health", source="local_path",
    )
    try:
        roots = allowed_roots_from_text(SETTINGS.local_health_import_allowed_roots)
        selection = resolve_local_health_export(
            text,
            allowed_roots=roots,
            max_bytes=SETTINGS.health_import_max_local_mb * 1024 * 1024,
            recursive=SETTINGS.local_health_import_recursive,
        )
        outcome = await import_health_export_file(
            user_id,
            selection.path,
            max_bytes=SETTINGS.health_import_max_local_mb * 1024 * 1024,
            audit_source="local_path",
        )
        selected_note = (
            f"\nנבחר הקובץ: <code>{esc(selection.path.name)}</code>"
            if selection.from_directory
            else ""
        )
        await event_log.append_event(
            DB,
            user_id,
            "health_export_freshness_calculated",
            entity="health",
            source="local_path",
            properties={
                "newest_record": _health_import_display_max_date(outcome),
                "inserted": outcome.inserted,
            },
        )
        await track_event(
            user_id,
            "health_import_completed",
            source="local_path",
            inserted=outcome.inserted,
            duplicates=outcome.duplicates,
            candidate_count=selection.candidate_count,
        )
        # RE10-4: per-fact confirmation wizard before the final summary.
        next_step = "onboarding" if await onboarding.is_onboarding(DB, user_id) else "reconciliation"
        await start_health_confirm_wizard(
            progress, user_id,
            next_step=next_step,
            summary_text=_health_import_success_text(outcome) + selected_note,
        )
    except LocalHealthPathError as exc:
        await track_event(user_id, "health_import_failed", source="local_path", kind="path")
        await event_log.append_event(
            DB, user_id, "local_health_import_failed",
            entity="health", source="local_path",
            properties={"kind": "path", "error": str(exc)[:200]},
        )
        await safe_message_edit(
            progress,
            "<b>לא הצלחתי לקרוא את הנתיב.</b>\n\n"
            f"{esc(str(exc))}\n\n"
            "הנתיב חייב להיות קיים על אותו מחשב שמריץ את הבוט.",
        )
    except Exception as exc:  # noqa: BLE001
        LOGGER.exception("Local Health export import failed")
        await track_event(user_id, "health_import_failed", source="local_path", kind="import")
        await event_log.append_event(
            DB, user_id, "local_health_import_failed",
            entity="health", source="local_path",
            properties={"kind": "import", "error": type(exc).__name__},
        )
        if context is not None:
            with suppress(Exception):
                await notify_admin(
                    context.bot,
                    f"Local Health import failed for {user_id}: {exc!r}",
                )
        await safe_message_edit(progress, friendly_error(exc, "health import"))
    finally:
        await _finish_health_import_flow(user_id)
    return True


@runtime_bound(RUNTIME_NAMES)
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_allowed(update):
        return
    user_id = await ensure_user(update)
    message = update.effective_message
    document = message.document
    name = (document.file_name or "").lower()
    if not (name.endswith(".zip") or name.endswith(".xml")):
        await message.reply_text("צריך קובץ ZIP (או XML) של ייצוא Apple Health.")
        return
    max_bytes = SETTINGS.health_import_max_upload_mb * 1024 * 1024
    if document.file_size and document.file_size > max_bytes:
        await message.reply_text(
            f"הקובץ גדול מדי. המגבלה בהעלאה דרך Telegram היא "
            f"{SETTINGS.health_import_max_upload_mb}MB. "
            "אפשר במקום זאת לשלוח את נתיב התיקייה המקומית."
        )
        return
    if not await _begin_health_import_flow(user_id, source="document"):
        await message.reply_text("כבר מתבצע ייבוא Health. אמתין לסיום לפני קובץ נוסף.")
        return

    progress = await message.reply_text("📥 מקבל את הקובץ ומפענח… זה עשוי לקחת דקה.")
    await track_event(user_id, "health_import_started", source="telegram_document")
    folder = Path(SETTINGS.storage_dir) / "health_import"
    folder.mkdir(parents=True, exist_ok=True)
    suffix = ".zip" if name.endswith(".zip") else ".xml"
    saved_path = folder / f"{user_id}_{secrets.token_hex(8)}{suffix}"

    try:
        telegram_file = await document.get_file()
        await telegram_file.download_to_drive(custom_path=str(saved_path))
        outcome = await import_health_export_file(
            user_id,
            saved_path,
            max_bytes=max_bytes,
            audit_source="telegram_document",
        )
        await track_event(
            user_id,
            "health_import_completed",
            source="telegram_document",
            inserted=outcome.inserted,
            duplicates=outcome.duplicates,
        )
        # RE10-4: a per-fact confirmation wizard now runs BEFORE the final
        # summary, replacing the old single "activate everything" gate.
        next_step = "onboarding" if await onboarding.is_onboarding(DB, user_id) else "reconciliation"
        await start_health_confirm_wizard(
            progress, user_id,
            next_step=next_step,
            summary_text=_health_import_success_text(outcome),
        )
    except Exception as exc:  # noqa: BLE001
        LOGGER.exception("Health import failed")
        await track_event(user_id, "health_import_failed", source="telegram_document")
        with suppress(Exception):
            await notify_admin(context.bot, f"Health import failed for {user_id}: {exc!r}")
        await safe_message_edit(progress, friendly_error(exc, "health import"))
    finally:
        with suppress(Exception):
            saved_path.unlink(missing_ok=True)
        await _finish_health_import_flow(user_id)


@runtime_bound(RUNTIME_NAMES)
async def run_post_import_reconciliation(message: Any, user_id: int) -> None:
    """Surface at most one explainable insight from comparing reported vs actual
    data. Actionable proposals come with approve/decline buttons — never silent.
    """
    with suppress(Exception):
        insights = await reconcile.run_reconciliation(DB, user_id, TZ)
        if not insights:
            return
        top = insights[0]
        await track_event(
            user_id,
            "reconcile_insight",
            key=top.key,
            observations=top.observations,
        )
        lines = ["<b>🔍 מה ראיתי בנתונים</b>", "", top.summary]
        if top.proposal and top.proposal_action:
            lines += ["", top.proposal]
            keyboard = InlineKeyboardMarkup(
                [
                    [button("✅ כן, הפעל", f"reconcile_ok:{top.proposal_action}")],
                    [button("❌ לא, תודה", f"reconcile_no:{top.key}")],
                ]
            )
        else:
            keyboard = InlineKeyboardMarkup([[button("⬅️ תפריט", "menu:home")]])
        await message.reply_text("\n".join(lines), reply_markup=keyboard, parse_mode=ParseMode.HTML)


@runtime_bound(RUNTIME_NAMES)
async def apply_reconcile_proposal(user_id: int, action: str) -> str:
    """Apply an approved reconciliation proposal. Returns a confirmation line."""
    if action == "auto_hold_on_bad_sleep":
        # recommend_load already holds weight when sleep_quality == 'bad'. Record
        # the user's explicit opt-in so it's a confirmed preference.
        await user_model.set_fact(
            DB,
            user_id,
            "auto_hold_on_bad_sleep",
            True,
            kind=user_model.KIND_FACT,
            source=user_model.SOURCE_USER,
            confirmed=True,
            affects=("workout_volume",),
        )
        return "מעכשיו, בימים שתדווח על שינה גרועה, אשמור על המשקל במקום לעלות. אפשר לבטל בכל רגע."
    return "עודכן."


@runtime_bound(RUNTIME_NAMES)
async def send_to_user(
    context: CallbackContext,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> Any:
    return await context.bot.send_message(
        chat_id=SETTINGS.telegram_allowed_user_id,
        text=text,
        reply_markup=reply_markup,
        parse_mode=ParseMode.HTML,
    )


@runtime_bound(RUNTIME_NAMES)
async def morning_checkin_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Quick morning check-in — the real-time source (data is retrospective).

    Buttons are built from the user's known medications so reporting is a single
    tap, not a typed command.
    """
    rows: list[list[InlineKeyboardButton]] = []
    meds = await known_medications(user_id)
    # Use the index into the known-meds list as the callback id (med names can
    # be long or contain ':' which would break callback_data).
    for idx, med in enumerate(meds[:3]):
        rows.append([button(f"💊 לקחתי {med}", f"chk:med:{idx}")])
    rows.append([button("💊 תרופה אחרת", "chk:med_other")])
    rows.append(
        [
            button("😴 ישנתי טוב", "chk:sleep:good"),
            button("😐 בינוני", "chk:sleep:ok"),
            button("🥱 רע", "chk:sleep:bad"),
        ]
    )
    rows.append(
        [
            button("✅ יום רגיל", "chk:state:normal"),
            button("🤕 יש כאב", "chk:state:pain"),
            button("🕒 צום", "chk:state:fasting"),
        ]
    )
    rows.append(
        [
            button("⚡ אנרגיה טובה", "chk:energy:high"),
            button("😵 אנרגיה נמוכה", "chk:energy:low"),
        ]
    )
    rows.append([button("🎯 מה לעשות עכשיו", "menu:now")])
    return InlineKeyboardMarkup(rows)


@runtime_bound(RUNTIME_NAMES)
async def job_morning(context: CallbackContext) -> None:
    user_id = SETTINGS.telegram_allowed_user_id

    async def send_checkin() -> None:
        await context.bot.send_message(
            chat_id=user_id,
            text=("בוקר טוב! שלושה עדכונים קצרים יעזרו לי להתאים את היום — מה רלוונטי?"),
            reply_markup=await morning_checkin_keyboard(user_id),
            parse_mode=ParseMode.HTML,
        )

    await deliver_proactive_message(
        context,
        key="morning_checkin",
        sender=send_checkin,
        priority=JOB_PRIORITY_SCHEDULED,
        retry_callback=job_morning,
    )

    async def send_menu() -> None:
        # TASK-05: send the daily menu as a separate pin-friendly message and
        # remember its Telegram id for refresh/replace flows.
        #
        # G2.3B-2: routed through the durable delivery boundary so the morning
        # job cannot re-deliver a menu the user already received today (e.g.
        # after a retry or a restart). A background suppression is silent --
        # the user is never told that a duplicate was avoided.
        from noam_coach.services.daily_menu_delivery import deliver_standalone_menu

        menu_text = await build_morning_menu_text(user_id)
        menu_keyboard = InlineKeyboardMarkup([
            [button("🔄 רענן תפריט", "menu:refresh_daily_menu"), button("🍽 מה לאכול עכשיו", "menu:nextmeal")],
            [button("✏️ החלף ארוחה", "menu:replace_daily_meal"), button("📊 מצב היום", "menu:status")],
        ])

        async def _send_morning_menu() -> Any:
            return await send_to_user(context, menu_text, reply_markup=menu_keyboard)

        await deliver_standalone_menu(
            DB,
            user_id,
            send=_send_morning_menu,
            requested_by="job_morning",
            source="morning_job",
        )

    await deliver_proactive_message(
        context,
        key="morning_menu",
        sender=send_menu,
        priority=JOB_PRIORITY_SCHEDULED,
        counts_toward_budget=False,
        retry_callback=job_morning,
    )


@runtime_bound(RUNTIME_NAMES)
async def job_evening(context: CallbackContext) -> None:
    user_id = SETTINGS.telegram_allowed_user_id
    now = datetime.now(TZ)

    # On Saturday the weekly report replaces the daily recap when it was
    # delivered successfully, avoiding two similar summaries close together.
    if now.weekday() == 5:
        weekly = await DB.fetch_one(
            """
            SELECT 1
            FROM job_state
            WHERE user_id=? AND day=? AND key='weekly_summary'
              AND status='sent'
            """,
            (user_id, local_day_str()),
        )
        if weekly:
            with suppress(Exception):
                await save_routine_profile(user_id)
            return

    async def send_summary() -> None:
        await send_to_user(
            context,
            await build_evening_summary_text(user_id),
        )

    await deliver_proactive_message(
        context,
        key="evening",
        sender=send_summary,
        priority=JOB_PRIORITY_SCHEDULED,
        retry_callback=job_evening,
    )

    with suppress(Exception):
        await save_routine_profile(user_id)


@runtime_bound(RUNTIME_NAMES)
async def job_calorie_watch(context: CallbackContext) -> None:
    """Send the single most valuable nutrition intervention for this moment."""
    user_id = SETTINGS.telegram_allowed_user_id
    ctx = await build_daily_context(user_id)
    nutrition_context = await build_nutrition_context(
        DB,
        user_id,
        "planned_meal_followup",
        daily_ctx=ctx,
    )
    followup = planned_meal_followup(nutrition_context, now=ctx.now)
    if followup:
        async def send_planned_meal_followup() -> None:
            await send_to_user(
                context,
                followup.text,
                reply_markup=InlineKeyboardMarkup(
                    [[button("🍽 מה לאכול עכשיו", "menu:nextmeal")]]
                ),
            )

        await deliver_proactive_message(
            context,
            key=followup.key,
            sender=send_planned_meal_followup,
            priority=JOB_PRIORITY_COACHING,
            retry_callback=job_calorie_watch,
        )
        return
    if ctx.calories_consumed <= 0:
        return

    target_cal = float(ctx.calorie_target)
    fraction_used = ctx.calories_consumed / target_cal if target_cal else 0

    # Over-pace is more actionable than a generic meal suggestion, so it wins
    # when both conditions become true in the same run.
    if fraction_used >= 0.8 and ctx.hours_left >= 5:

        async def send_overpace() -> None:
            await send_to_user(
                context,
                "⚠️ <b>שים לב לקצב</b>\nכבר צרכת "
                f"{ctx.calories_consumed:.0f} מתוך "
                f"{target_cal:.0f} קלוריות, ועוד "
                f"~{ctx.hours_left:.0f} שעות לפנינו היום. "
                "בוא נשמור על ארוחות קלות ועתירות חלבון "
                "מכאן עד הערב.",
            )

        sent = await deliver_proactive_message(
            context,
            key="overpace_alert",
            sender=send_overpace,
            priority=JOB_PRIORITY_HIGH,
            retry_callback=job_calorie_watch,
        )
        if sent:
            return

    if ctx.calories_consumed >= SETTINGS.intraday_calorie_trigger:

        async def send_nudge() -> None:
            await send_to_user(
                context,
                await build_next_meal_text(user_id, ctx),
            )

        await deliver_proactive_message(
            context,
            key="intraday_nudge",
            sender=send_nudge,
            priority=JOB_PRIORITY_COACHING,
            retry_callback=job_calorie_watch,
        )


@runtime_bound(RUNTIME_NAMES)
async def job_motivation(context: CallbackContext) -> None:
    """Send motivation only at a meaningful moment and within the message budget."""
    user_id = SETTINGS.telegram_allowed_user_id
    profile = await load_routine_profile(user_id)
    now = datetime.now(TZ)
    hour = now.hour + now.minute / 60.0

    def near(hhmm: str | None, window: float = 0.75) -> bool:
        if not hhmm:
            return False
        hh, mm = (int(x) for x in hhmm.split(":"))
        target = hh + mm / 60.0
        return abs(hour - target) <= window

    workout_hour = (profile.get("workout", {}) or {}).get("typical_hour")
    snack_hours = (profile.get("eating", {}) or {}).get("typical_meal_hours") or []

    if near(workout_hour, 1.0):
        moment = "pre_workout"
        hint = "כשעה לפני האימון הטיפוסי"
    elif any(near(value, 0.5) for value in snack_hours):
        moment = "snack_time"
        hint = "סביב שעת נשנוש טיפוסית"
    else:
        if random.random() > 0.08:
            return
        moment = "random"
        hint = ""

    # RE9-036/037: enrich the hint with the live workout context via the unified
    # Prompt Builder, so motivation is contextual and shares the same envelope.
    with suppress(Exception):
        from noam_coach.services.next_meal import build_workout_nutrition_context
        from noam_coach.services.prompt_builder import build_workout_request

        wctx = await build_workout_nutrition_context(DB, user_id, now=now)
        request = build_workout_request(wctx, "Write a short motivational line")
        label = request["context"].get("workout_label")
        if label and moment == "pre_workout":
            hint = f"{hint} ({label})"

    async def send_motivation() -> None:
        message = await recommendations.motivation_message(
            OPENAI_CLIENT,
            SETTINGS.openai_model,
            hint,
        )
        await send_to_user(context, f"💪 {esc(message)}")

    await deliver_proactive_message(
        context,
        key=f"motivation_{moment}",
        sender=send_motivation,
        priority=JOB_PRIORITY_LOW,
        retry_callback=job_motivation,
    )


@runtime_bound(RUNTIME_NAMES)
def _ctx_has_workout(ctx: "DailyContext") -> bool:
    return ctx.workout_completed or ctx.is_usual_workout_day


@runtime_bound(RUNTIME_NAMES)
def _data_quality_disclaimer(ctx: "DailyContext") -> str:
    # TASK-7: do not show "the targets haven't been computed yet" when the user
    # already has a confirmed/active goal (e.g. a manually approved goal whose
    # goal_computed flag is False). The warning is only for the true no-goal /
    # default-values case (status "default"/"proposal_only").
    goal = getattr(ctx, "goal", None) or {}
    goal_status = str(goal.get("status") or "")
    has_confirmed_goal = goal_status in {"active", "active_provisional"}
    if not ctx.goal_computed and not has_confirmed_goal:
        return (
            "\n\n⚠️ <i>היעדים עדיין לא חושבו מהנתונים שלך — "
            "ההמלצות מבוססות על ערכי ברירת מחדל. "
            "ככל שתדווח יותר, ההמלצות ישתפרו.</i>"
        )
    return ""


@runtime_bound(RUNTIME_NAMES)
def _daily_coach_brief_lines(ctx: "DailyContext") -> list[str]:
    workout_line = "אימון כבר תועד" if ctx.workout_completed else "אין אימון מתועד עדיין"
    if ctx.workout_active:
        workout_line = "אימון פעיל עכשיו"
    elif ctx.usual_workout_time and (ctx.is_usual_workout_day or not ctx.workout_completed):
        workout_line = f"אימון סביב {ctx.usual_workout_time}"
    protein_action = (
        "כדאי שהארוחה הבאה תתבסס על מקור חלבון איכותי."
        if ctx.protein_remaining >= 35
        else "נשאר מעט חלבון, אפשר להשלים אותו בארוחה קלה."
    )
    if ctx.calories_remaining <= 350:
        calorie_action = "נשאר מעט תקציב קלורי, אז עדיף לבחור משהו קל ומדויק."
    elif ctx.workout_completed or ctx.is_usual_workout_day:
        calorie_action = "שמור חלק מהתקציב לארוחה סביב האימון."
    else:
        calorie_action = "אפשר לפזר את היתרה על שתי ארוחות רגועות."
    lines = [
        "<b>בוקר טוב נועם 👋</b>",
        "<b>מצב היום</b>",
        f"🔥 נשארו {ctx.calories_remaining:.0f} קלוריות",
        f"🥩 נשארו {ctx.protein_remaining:.0f} גרם חלבון",
        f"🏋️ {workout_line}",
        "",
        *format_daily_mission(choose_daily_mission(ctx)),
        "",
        "<b>מומלץ עכשיו</b>",
        f"• {protein_action}",
        f"• {calorie_action}",
    ]
    return lines


@runtime_bound(RUNTIME_NAMES)
def _evening_coach_review_lines(ctx: "DailyContext") -> list[str]:
    calorie_delta = ctx.calorie_target - ctx.calories_consumed
    protein_done = ctx.protein_consumed >= ctx.protein_target * 0.95
    calorie_line = (
        f"גרעון של {calorie_delta:.0f} קלוריות"
        if calorie_delta >= 0
        else f"חריגה של {abs(calorie_delta):.0f} קלוריות"
    )
    # FIX 39: strict evidence (ctx.workout_completed) stays authoritative for
    # the checkmark, but an explicit self-report or cancellation must not be
    # flatly contradicted by the wording -- "no workout recorded" when the
    # user said "I finished" minutes earlier reads as the coach forgetting
    # what it was just told.
    if ctx.workout_completed:
        workout_line = "התאמנת"
    elif ctx.workout_cancelled_today:
        workout_line = "האימון בוטל היום"
    elif ctx.workout_self_reported:
        workout_line = "דיווחת שהתאמנת (עדיין ללא אישוש מהשעון/מהאפליקציה)"
    else:
        workout_line = "לא תועד אימון"
    tomorrow = (
        "מחר כדאי לפתוח עם חלבון מוקדם כדי לשמור על הקצב."
        if not protein_done
        else "מחר כדאי לשמור על אותו קצב חלבון."
    )
    return [
        "<b>סיכום היום</b>",
        f"{'✅' if protein_done else '🟡'} חלבון: {ctx.protein_consumed:.0f}/{ctx.protein_target:.0f} גרם",
        f"{'✅' if calorie_delta >= 0 else '🟡'} {calorie_line}",
        f"{'✅' if ctx.workout_completed else '🟡'} {workout_line}",
        "",
        *format_daily_score(calculate_daily_score(ctx)),
        "",
        f"<b>מחר מומלץ</b>\n• {tomorrow}",
    ]


@runtime_bound(RUNTIME_NAMES)
async def build_morning_menu_text(
    user_id: int,
    ctx: "DailyContext | None" = None,
    *,
    persist: bool = True,
    _sink: dict[str, Any] | None = None,
) -> str:
    """Return the standalone daily menu message.

    TASK-05/06: the morning check-in is already delivered as its own message in
    ``job_morning``.  The nutrition menu must therefore be a pin-friendly,
    self-contained daily menu — not a long blended morning briefing/status
    message.  Legacy callback name ``menu:morning`` still calls this function,
    but its product meaning is now "תפריט להיום".

    ``persist`` (G2.3B-3) defaults to True, so every existing caller keeps its
    current behaviour of saving the generated menu as a new revision. The
    durable refresh orchestrator passes ``persist=False`` because it must
    allocate the revision itself: the identity is RESERVED at claim time and
    written by one atomic mutation together with the operation record, and a
    second self-allocating write here would defeat that. ``_sink`` then
    receives the generated ``meals``/``strategy`` so that caller can persist
    them under the reserved identity without generating twice.
    """
    if ctx is None:
        ctx = await build_daily_context(user_id)
    nutrition_context = await build_nutrition_context(
        DB,
        user_id,
        "morning_menu",
        daily_ctx=ctx,
    )
    nutrition_request = build_nutrition_ai_request(
        nutrition_context,
        "Build today's standalone nutrition menu",
    )
    # TASK-6/7: generate -> deterministically validate -> repair-or-fallback
    # (noam_coach.services.morning_menu_pipeline) instead of showing the raw
    # AI output. This is the operational enforcement of prompt_builder's
    # SAFETY_CONTRACT for the daily menu.
    from noam_coach.services.morning_menu_pipeline import (
        MenuGenerationBlocked,
        build_personalized_morning_menu,
    )

    try:
        pipeline_result = await build_personalized_morning_menu(
            DB,
            user_id,
            profile=ctx.profile,
            goal=ctx.goal,
            today_has_workout=_ctx_has_workout(ctx),
            daily_flags=ctx.flags,
            openai_client=OPENAI_CLIENT,
            openai_model=SETTINGS.openai_model,
            now=ctx.now,
            # Finding 12: reuse THIS snapshot — one authoritative
            # NutritionContext drives remaining budget, meal intents, AI
            # context, validation and the personalization-strength text
            # below, instead of two independently-timed database reads.
            nutrition_context=nutrition_context,
        )
    except MenuGenerationBlocked:
        # Finding 7 hard invariant: an invalid menu must never be rendered.
        return (
            "<b>תפריט להיום</b>\n\n"
            "לא הצלחתי לבנות כרגע תפריט שעומד בכל ההגבלות והיעדים שלך. "
            "אני לא רוצה להציג לך הצעה לא אמינה.\n\n"
            "אפשר לנסות שוב, או לבדוק \"מה לאכול עכשיו\" לארוחה בודדת בינתיים."
        )
    menu = pipeline_result.menu
    workout_note = "יום אימון" if _ctx_has_workout(ctx) else "יום ללא אימון מתוכנן"
    target_line = f"יעד: {ctx.calorie_target:.0f} קל׳ | {ctx.protein_target:.0f} ג׳ חלבון"
    text = "\n".join([
        f"<b>תפריט להיום — {esc(workout_note)}</b>",
        esc(target_line),
        "",
        format_morning_menu(menu),
    ])
    if not ctx.flags:
        text += "\n\n<i>ההצעה נבנתה לפי השגרה שלך, כי עדיין לא התקבל עדכון בוקר להיום.</i>"
    # TASK-5: be honest about personalization strength. When little learned-food
    # history exists yet, say the menu is based on targets + confirmed nutrition
    # info and will improve as more meals are logged — do not imply strong
    # personalization that was not actually available.
    learned_foods = getattr(nutrition_context, "learned_foods", None) or []
    if len(learned_foods) < 3:
        text += (
            "\n\n<i>ההצעה נבנתה לפי היעדים והמידע התזונתי שאושר עד עכשיו. "
            "ככל שיתועדו יותר ארוחות, התפריט יותאם טוב יותר להרגלים שלך.</i>"
        )
    from noam_coach.services.decision_engine import context_completeness_gate

    gate = context_completeness_gate("nutrition", nutrition_request["context_quality"])
    if gate.based_on_partial_info and gate.tag():
        text += f"\n\n<i>{esc(gate.tag())}</i>"
    text += _data_quality_disclaimer(ctx)
    with suppress(Exception):
        active_plan = await planning.get_active_plan(DB, user_id, "nutrition")
        if persist:
            from noam_coach.services.daily_menu_state import remember_active_daily_menu

            await remember_active_daily_menu(
                DB,
                user_id,
                text=text,
                strategy=(active_plan or {}).get("strategy"),
                source="build_morning_menu_text",
                meals=pipeline_result.meal_records,
            )
        elif _sink is not None:
            _sink["meals"] = list(pipeline_result.meal_records or [])
            _sink["strategy"] = (active_plan or {}).get("strategy")
    return text


@runtime_bound(RUNTIME_NAMES)
async def generate_daily_menu_payload(
    user_id: int,
    ctx: "DailyContext | None" = None,
) -> dict[str, Any]:
    """Generate a daily menu WITHOUT persisting it (G2.3B-3).

    Returns ``{"text", "meals", "strategy"}`` for a caller that owns revision
    allocation -- the durable refresh orchestrator, which reserved the menu
    identity before generation and writes it atomically together with the
    operation record.

    Exactly ONE generation happens: ``build_morning_menu_text`` is called once
    with ``persist=False``, which skips only its self-allocating save. The
    structured meals are collected into ``sink`` by that same call, so no
    second AI request is made and no module-level state is involved.
    """
    sink: dict[str, Any] = {}
    text = await build_morning_menu_text(user_id, ctx, persist=False, _sink=sink)
    return {
        "text": text,
        "meals": list(sink.get("meals") or []),
        "strategy": sink.get("strategy"),
    }


@runtime_bound(RUNTIME_NAMES)
async def _todays_workout_time(user_id: int, now: datetime) -> str | None:
    """Resolve today's day-specific workout time from the active workout plan.

    TASK-16/14: the morning briefing must show the *actual* time for today's
    weekday (e.g. a Friday morning slot), not one global typical hour.  Falls
    back to ``None`` when the plan has no session for today.
    """
    with suppress(Exception):
        plan = await planning.get_active_plan(DB, user_id, "workout")
        sessions = ((plan or {}).get("payload") or {}).get("sessions") or []
        today = local_weekday(now)
        candidates = [s for s in sessions if int(s.get("weekday", -1)) == today]
        if candidates:
            chosen = sorted(candidates, key=lambda s: str(s.get("time") or "23:59"))[0]
            value = str(chosen.get("time") or "").strip()[:5]
            if value and value[2:3] == ":":
                return value
    return None


@runtime_bound(RUNTIME_NAMES)
async def build_morning_briefing_text(user_id: int, ctx: "DailyContext | None" = None) -> str:
    """Return the short morning-update briefing (TASK-16).

    ``menu:morning`` (☀️ עדכון בוקר) is a short current-day briefing — NOT the
    full pinnable daily menu (``menu:daily_menu`` → ``build_morning_menu_text``).
    It summarises today's weekday, remaining calorie/protein budget, today's
    workout status using the day-specific workout time, one or two immediate
    priorities, and points the user to the focused next action.  It never
    re-sends the full active menu.
    """
    if ctx is None:
        ctx = await build_daily_context(user_id)

    # TASK-19 audit correction: reuse the canonical day-type classification
    # (nutrition_context.day_type) instead of re-deriving weekday info from
    # scratch — the weekday NAME shown in the headline still needs the full
    # label, but "is today a weekend day" (for the guidance line below) is
    # now the same single source of truth the daily menu uses.
    from noam_coach.services.nutrition_context import day_type as _classify_day_type

    # Audit F-A6: weekday_labels_he returns a LIST — interpolating it raw
    # rendered the Python literal "['שבת']" in the headline. Join to a
    # localized string; the headline is always exactly one day.
    weekday_label = "".join(weekday_labels_he([local_weekday(ctx.now)])) or "היום"
    today_type = _classify_day_type(ctx.now)
    workout_time = await _todays_workout_time(user_id, ctx.now)
    is_workout_day = ctx.is_usual_workout_day or bool(workout_time)

    # Resolve an explicit workout status line + the immediate priority.
    if ctx.workout_completed:
        workout_line = "🏋️ האימון של היום כבר תועד כבוצע"
        priority = "המשך היום מתמקד בהתאוששות ובהשלמת חלבון."
    elif ctx.workout_active:
        workout_line = "🏋️ אימון פעיל עכשיו"
        priority = "אחרי האימון כדאי להשלים חלבון ופחמימה."
    elif is_workout_day and workout_time:
        morning_workout = int(workout_time[:2]) < 12
        workout_line = f"🏋️ מתוכנן היום אימון בשעה {workout_time}"
        if morning_workout:
            priority = "לפני אימון בוקר עדיף משהו קל לעיכול; ראה 'מה לאכול עכשיו'."
        else:
            priority = "האימון מאוחר יותר — אכול מאוזן ושמור מספיק קלוריות וחלבון לסביבת האימון."
    elif is_workout_day:
        workout_line = "🏋️ יום אימון (השעה עדיין לא נקבעה)"
        priority = "כשתדע מתי האימון, בנה סביבו את הארוחות; בינתיים אכול מאוזן."
    else:
        workout_line = "🌿 היום ללא אימון מתוכנן"
        priority = "התמקד בחלבון, בתקציב הקלורי ובפעילות כללית לאורך היום."

    lines = [
        f"<b>☀️ עדכון בוקר — {esc(weekday_label)}</b>",
        "",
        f"🔥 נשארו {ctx.calories_remaining:.0f} קל׳ מתוך {ctx.calorie_target:.0f}",
        f"🥩 נשארו {ctx.protein_remaining:.0f} ג׳ חלבון מתוך {ctx.protein_target:.0f}",
        workout_line,
        "",
        "<b>עדיפות עכשיו</b>",
        f"• {priority}",
    ]
    # TASK-19: weekend guidance, using the SAME day_type classification the
    # daily menu builds around (nutrition_context.day_type) rather than a
    # separately-maintained weekend check.
    if today_type in {"friday", "saturday"}:
        lines.append(
            "• סוף השבוע — שגרת הארוחות עשויה להיות שונה; זה בסדר לסטות מהתזמון הרגיל כל עוד היעד היומי נשמר."
        )
    text = "\n".join(lines)
    text += _data_quality_disclaimer(ctx)
    return text


@runtime_bound(RUNTIME_NAMES)
async def build_next_meal_text(user_id: int, ctx: "DailyContext | None" = None) -> str:
    del ctx
    from noam_coach.services.next_meal import build_next_meal_response_text

    return await build_next_meal_response_text(DB, user_id)


@runtime_bound(RUNTIME_NAMES)
async def build_evening_summary_text(user_id: int, ctx: "DailyContext | None" = None) -> str:
    if ctx is None:
        ctx = await build_daily_context(user_id)
    nutrition_context = await build_nutrition_context(
        DB,
        user_id,
        "evening_summary",
        daily_ctx=ctx,
    )
    nutrition_request = build_nutrition_ai_request(
        nutrition_context,
        "Summarize today's nutrition",
    )
    items = await today_meal_items(user_id)
    summary = await recommendations.evening_summary(
        OPENAI_CLIENT,
        SETTINGS.openai_model,
        ctx.profile,
        ctx.goal,
        ctx.calories_consumed,
        ctx.protein_consumed,
        items,
        ctx.flags,
        nutrition_request["context"],
    )
    return "\n".join([*_evening_coach_review_lines(ctx), "", format_evening_summary(summary)]) + _data_quality_disclaimer(ctx)
