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

from noam_coach.runtime_bind import runtime_bound
from noam_coach.services.local_health_path import (
    LocalHealthPathError,
    allowed_roots_from_text,
    looks_like_local_health_path,
    resolve_local_health_export,
)
from noam_coach.services.nutrition_context import (
    build_nutrition_ai_request,
    build_nutrition_context,
)
from noam_coach.services.weekdays import (
    WEEKDAY_SCHEMA_VERSION,
    normalize_weekday,
    sunday_first_order,
    weekday_he,
    with_weekday_schema,
)

RUNTIME_NAMES = ('Any', 'CallbackContext', 'ContextTypes', 'DB', 'Exception', 'InlineKeyboardButton', 'InlineKeyboardMarkup', 'JOB_PRIORITY_COACHING', 'JOB_PRIORITY_HIGH', 'JOB_PRIORITY_LOW', 'JOB_PRIORITY_SCHEDULED', 'LOGGER', 'OPENAI_CLIENT', 'ParseMode', 'Path', 'RuntimeError', 'SETTINGS', 'TZ', 'Update', 'ValueError', '_ctx_has_workout', '_data_quality_disclaimer', 'abs', 'action', 'actual_bytes', 'any', 'asyncio', 'at', 'bool', 'build_daily_context', 'build_evening_summary_text', 'build_morning_menu_text', 'build_next_meal_text', 'button', 'context', 'conversation', 'ctx', 'current_flow', 'datetime', 'deliver_proactive_message', 'document', 'duplicates', 'ensure_user', 'enumerate', 'esc', 'exc', 'extract_dir', 'fasting_negated', 'flags', 'float', 'folder', 'format_evening_summary', 'format_morning_menu', 'format_next_meals', 'fraction_used', 'friendly_error', 'get_daily_flags', 'health_import', 'hh', 'hhmm', 'hint', 'hour', 'idx', 'inserted', 'insights', 'int', 'is_allowed', 'items', 'job_calorie_watch', 'job_evening', 'job_morning', 'job_motivation', 'keyboard', 'known_medications', 'learned', 'learned_block', 'lines', 'list', 'load_routine_profile', 'local_day_str', 'lowered', 'max_bytes', 'med', 'meds', 'menu', 'message', 'mm', 'moment', 'morning_checkin_keyboard', 'name', 'near', 'notify_admin', 'now', 'onboarding', 'parse_to_rows', 'profile', 'progress', 'random', 're', 'recommendations', 'reconcile', 'resumed', 'ritalin_negated', 'route_decision', 'rows', 'run_post_import_reconciliation', 'save_routine_profile', 'saved_path', 'secrets', 'send_checkin', 'send_menu', 'send_motivation', 'send_nudge', 'send_overpace', 'send_summary', 'send_to_user', 'sent', 'set_daily_flags', 'show_onboarding_basics', 'shutil', 'sleep', 'snack_hours', 'str', 'suffix', 'suggestion', 'summary', 'suppress', 'sync_health_measurements_to_facts', 'target', 'target_cal', 'telegram_file', 'text', 'today_meal_items', 'top', 'track_event', 'tuple', 'update', 'upsert_health_rows', 'user_id', 'user_model', 'value', 'weekly', 'window', 'workout', 'workout_hour', 'write_audit', 'x', 'xml_path')


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


def _health_import_staleness_warning(max_date: Any, *, today: datetime | None = None) -> str:
    if not max_date:
        return ""
    try:
        newest = datetime.fromisoformat(str(max_date)).date()
    except ValueError:
        return ""
    current = (today or datetime.now(TZ)).date()
    days_old = max(0, (current - newest).days)
    if days_old <= 7:
        return ""
    return (
        "⚠️ הקובץ יובא בהצלחה, אבל הנתונים אינם טריים: "
        f"הרשומה האחרונה היא מ-{newest.isoformat()} "
        f"({days_old} ימים אחורה). כדי לדייק את השבוע האחרון צריך ZIP חדש."
    )


def _health_import_success_text(outcome: HealthImportOutcome) -> str:
    s = outcome.summary
    sleep = outcome.profile.get("sleep", {}) or {}
    workout = outcome.profile.get("workout", {}) or {}

    # --- Source file section ---
    lines = ["<b>ייבוא Apple Health הושלם ✅</b>", ""]
    lines.append("<b>בקובץ שנבחר:</b>")
    if s.min_date and s.max_date:
        lines.append(f"• טווח נתונים: {s.min_date} עד {s.max_date}")
        lines.append(f"• הרשומות החדשות ביותר הן מתאריך: {s.max_date}")
    lines.append(f"• רשומות בקובץ: {s.rows:,}")
    if s.min_date and s.max_date:
        warning = _health_import_staleness_warning(s.max_date)
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
    lines.append(f"• לילות שינה: {s.sleep_sessions:,}")
    if s.weight_records:
        lines.append(f"• רשומות משקל: {s.weight_records:,}")
    if s.activity_records:
        lines.append(f"• רשומות פעילות: {s.activity_records:,}")
    lines.append("")

    # --- Learned patterns ---
    learned: list[str] = []
    if sleep.get("typical_bedtime") and sleep.get("typical_wake_time"):
        learned.append(f"😴 שינה ~{sleep['typical_bedtime']}–{sleep['typical_wake_time']}")
    if workout.get("weekly_frequency"):
        hour = workout.get("typical_hour")
        at = f", בדרך כלל ~{hour}" if hour else ""
        learned.append(f"🏋️ ~{workout['weekly_frequency']} אימונים בשבוע{at}")
        day_labels = _workout_days_labels(workout)
        if day_labels:
            learned.append(f"📅 ימי אימון נפוצים: {', '.join(day_labels)}")
    if learned:
        lines.append("<b>מה למדתי על השגרה שלך:</b>")
        lines.extend(learned)
    else:
        lines.append(
            "עוד לא הצטברו מספיק נתוני שינה/אימונים בחלון הזמן כדי לזהות "
            "שגרה ברורה — נמשיך ונלמד תוך כדי."
        )

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
_WORKOUT_SUBSTEPS = (
    WIZARD_STEP_WORKOUT_FREQUENCY,
    WIZARD_STEP_WORKOUT_DAYS,
    WIZARD_STEP_WORKOUT_HOUR,
)
_WIZARD_STEP_ORDER = (*_WORKOUT_SUBSTEPS, "weight_kg", "sleep_schedule")

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
    return [weekday_he(d) for d in sunday_first_order(_workout_days_indices(value))]


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


async def _next_wizard_step(
    user_id: int, done: list[str]
) -> tuple[str, dict[str, Any]] | None:
    """Return (step_id, fact) for the next confirmation, honoring sub-steps."""
    pending = await pending_import_facts(user_id)
    pending_keys = [str(row["key"]) for row in pending]
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
        return key, fact
    return None


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
        await target.edit_message_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
    else:
        await target.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)


_SKIP_WIZARD_BUTTON_ROW = [
    InlineKeyboardButton("⏭️ דלג על שאר האישורים", callback_data="health:skip_wizard")
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
    next_step = await _next_wizard_step(user_id, done)
    if next_step is None:
        await clear_flow_state(user_id, HEALTH_CONFIRM_FLOW)
        return False

    step_id, fact = next_step
    await set_flow_state(user_id, HEALTH_CONFIRM_FLOW, step_id, {"done": done})
    await set_pending(user_id, f"__health_edit_{step_id}__")

    quality = await _wizard_quality_report(user_id)

    prefix = f"{ack_text}\n\n" if ack_text else ""
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

        # RE13: with too few usable weeks there is no trustworthy number to
        # approve — ask the user directly instead of dressing a guess up as
        # a detected routine.
        if wq.get("policy") == routine.POLICY_INSUFFICIENT:
            text = (
                f"{header}"
                f"{esc(wq.get('warning_he') or 'אין מספיק שבועות עם נתוני שעון כדי לזהות שגרת אימונים אמינה.')}\n"
                "כמה אימונים בשבוע תרצה לתכנן? בחר או כתוב מספר."
            )
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(str(n), callback_data=f"health:confirm:{key}:trend:{n}")
                    for n in (2, 3, 4)
                ],
                _SKIP_WIZARD_BUTTON_ROW,
            ])
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
            rows.append(_SKIP_WIZARD_BUTTON_ROW)
            await _send_wizard_screen(target, text, InlineKeyboardMarkup(rows))
            return True

        # RE13: relaxed policy — the shown value is the NORMALIZED estimate
        # (weeks with ≥5 worn days scaled to 7), so approving must apply that
        # exact number, not the strict raw value stored in the pattern.
        if wq.get("policy") == routine.POLICY_RELAXED and wq.get("frequency") is not None:
            approved = max(1, min(7, round(float(wq["frequency"]))))
            text = (
                f"{header}"
                f"{esc(f'זוהתה הערכה של כ-{approved} אימונים בשבוע.')}\n"
                f"{esc(wq.get('warning_he') or '')}\n"
                "זה מספיק להערכה ראשונית, אבל כדאי לאשר ידנית.\n"
                "האם לאשר גם עבור תוכנית האימונים?\n"
                "אם לא — ציין כמות אימונים רצויה בשבוע."
            )
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    f"✅ אשר {approved} בשבוע",
                    callback_data=f"health:confirm:{key}:trend:{approved}",
                )],
                _SKIP_WIZARD_BUTTON_ROW,
            ])
            await _send_wizard_screen(target, text, keyboard)
            return True

    # RE13: a sleep schedule detected from too few nights is an anecdote,
    # not a routine — never offer it as a regular confirmation.
    if step_id == "sleep_schedule" and isinstance(fact.get("value"), dict):
        from noam_coach.services.health_quality import (
            SLEEP_MIN_NIGHTS_FOR_CONFIRMATION,
        )

        nights = fact["value"].get("nights_sampled")
        if nights is not None and int(nights) < SLEEP_MIN_NIGHTS_FOR_CONFIRMATION:
            text = (
                f"{header}"
                f"{esc(f'זוהתה שינה רק ב-{int(nights)} לילות, ולכן זה לא מספיק כדי לקבוע שגרת שינה.')}\n"
                "אפשר לכתוב ידנית שעת שינה וקימה ממוצעת (למשל: 23:00-07:00), "
                "או להמשיך בלי."
            )
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("⏭️ השאר ריק והמשך", callback_data="health:skip_item")],
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
    await set_flow_state(user_id, HEALTH_CONFIRM_FLOW, step_id, {"done": done})
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
            await user_model.set_fact(
                DB, user_id, "training_days_per_week", approved,
                kind=user_model.KIND_FACT, source=user_model.SOURCE_USER,
                confirmed=True,
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
            await user_model.set_fact(
                DB, user_id, "weekly_availability", slots,
                kind=user_model.KIND_FACT, source=user_model.SOURCE_USER,
                confirmed=True,
            )
            ack = f"✅ אושר: ימי אימון — {', '.join(_workout_days_labels(value))}"
        else:  # WIZARD_STEP_WORKOUT_HOUR
            hour = str(value.get("typical_hour"))
            await user_model.set_fact(
                DB, user_id, "workout_window", hour,
                kind=user_model.KIND_FACT, source=user_model.SOURCE_USER,
                confirmed=True,
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
    await user_model.set_fact(
        DB, user_id, "training_days_per_week", trend_value,
        kind=user_model.KIND_FACT, source=user_model.SOURCE_USER, confirmed=True,
    )
    if key == "workout_pattern":
        await _mark_wizard_substep_done(user_id, WIZARD_STEP_WORKOUT_FREQUENCY, updated)
    return f"✅ אושר: {trend_value:g} אימונים בשבוע"


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
        from noam_coach.services.availability import parse_hebrew_availability_answer

        parsed = parse_hebrew_availability_answer(text)
        slots = [
            {**slot, "start": slot.get("start") or current.get("typical_hour")}
            for slot in parsed.weekly_availability
        ]
        if not slots:
            return False, "לא זיהיתי ימים. כתוב למשל: ראשון, שלישי, חמישי."
        indices = sorted({int(slot["weekday"]) for slot in slots})
        await user_model.set_fact(
            DB, user_id, "weekly_availability", slots,
            kind=user_model.KIND_FACT, source=user_model.SOURCE_USER, confirmed=True,
        )
        updated = {
            **current,
            "common_weekdays": indices,
            "weekday_schema": WEEKDAY_SCHEMA_VERSION,
        }
        await user_model.set_fact(DB, user_id, key, updated, kind=kind, source=source)
        await _mark_wizard_substep_done(user_id, step_id, updated)
        labels = [weekday_he(d) for d in sunday_first_order(indices)]
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

    return False, "לא הצלחתי לעדכן את הפריט הזה."


async def skip_health_wizard_item(user_id: int, step_id: str) -> None:
    """Skip one wizard item without applying it: a workout sub-step is only
    marked as handled (no plan fact written); a whole fact is invalidated."""
    if step_id in _WORKOUT_SUBSTEPS:
        done = await _wizard_done_steps(user_id)
        if step_id not in done:
            done.append(step_id)
        from noam_coach.bot.onboarding import set_flow_state

        await set_flow_state(user_id, HEALTH_CONFIRM_FLOW, step_id, {"done": done})
        return
    await user_model.invalidate_fact(DB, user_id, wizard_step_fact_key(step_id))


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
    from noam_coach.bot.onboarding import get_flow_state, clear_flow_state, show_onboarding_basics

    state = await get_flow_state(user_id, HEALTH_POST_WIZARD_FLOW)
    payload = state or {}
    next_step = payload.get("step") or "reconciliation"
    summary_text = (payload.get("payload") or {}).get("summary_text", "")
    await clear_flow_state(user_id, HEALTH_POST_WIZARD_FLOW)

    followup = await _health_import_followup_text(user_id)
    # RE13: close the wizard with a short data-quality summary — what was
    # counted, what was left out and whether a fresher export is needed.
    quality_section = ""
    with suppress(Exception):
        report = await _wizard_quality_report(user_id)
        if report and (report.get("freshness") or {}).get("latest_sample_date"):
            from noam_coach.services.health_quality import quality_summary_lines_he

            quality_section = "\n\n" + "\n".join(quality_summary_lines_he(report))
    text = (
        (summary_text + quality_section + followup)
        if summary_text
        else ("<b>סיכום הייבוא</b>" + quality_section + followup)
    )
    if ack_text:
        text = f"{ack_text}\n\n{text}"
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ תפריט", callback_data="menu:home")]])
    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
    else:
        await target.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)

    message = getattr(target, "message", target)
    if next_step == "onboarding":
        await show_onboarding_basics(message, user_id)
    else:
        await run_post_import_reconciliation(message, user_id)


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
                "source_date_range": f"{summary.min_date}..{summary.max_date}",
                "total_stored": total_stored,
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
        )
    finally:
        with suppress(Exception):
            if extract_dir.exists():
                shutil.rmtree(extract_dir, ignore_errors=True)


async def _finish_health_import_flow(user_id: int) -> None:
    with suppress(Exception):
        current_flow = await conversation.get_active_flow(DB, user_id)
        if current_flow.name == conversation.FlowName.health_import:
            resumed = await conversation.resume_suspended(DB, user_id)
            if resumed is None:
                await conversation.clear_active_flow(DB, user_id)


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
    await write_audit(user_id, "daily_flags", "flags", None, text=text)
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
                "newest_record": outcome.summary.max_date,
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
        await progress.edit_text(
            "<b>לא הצלחתי לקרוא את הנתיב.</b>\n\n"
            f"{esc(str(exc))}\n\n"
            "הנתיב חייב להיות קיים על אותו מחשב שמריץ את הבוט.",
            parse_mode=ParseMode.HTML,
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
        await progress.edit_text(friendly_error(exc, "health import"))
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
        await progress.edit_text(friendly_error(exc, "health import"))
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
) -> None:
    await context.bot.send_message(
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
        await send_to_user(
            context,
            await build_morning_menu_text(user_id),
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
    if not ctx.goal_computed:
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
    workout_line = "התאמנת" if ctx.workout_completed else "לא תועד אימון"
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
async def build_morning_menu_text(user_id: int, ctx: "DailyContext | None" = None) -> str:
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
        "Build today's nutrition menu",
    )
    menu = await recommendations.morning_menu(
        OPENAI_CLIENT,
        SETTINGS.openai_model,
        ctx.profile,
        ctx.goal,
        _ctx_has_workout(ctx),
        ctx.flags,
        nutrition_request["context"],
    )
    text = "\n".join([*_daily_coach_brief_lines(ctx), "", format_morning_menu(menu)])
    if not ctx.flags:
        text += "\n\n<i>המלצה זו נבנתה לפי השגרה שלך, כי עדיין לא התקבל עדכון בוקר להיום.</i>"
    # RE9-X2: if critical nutrition context is missing, say the recommendation
    # is based on partial info instead of presenting a guess as certainty.
    from noam_coach.services.decision_engine import context_completeness_gate

    gate = context_completeness_gate("nutrition", nutrition_request["context_quality"])
    if gate.based_on_partial_info and gate.tag():
        text += f"\n\n<i>{esc(gate.tag())}</i>"
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
