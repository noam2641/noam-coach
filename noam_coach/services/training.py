# ruff: noqa: F401, F811, F821, I001
"""Training recommendation and fatigue services.

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

RUNTIME_NAMES = ('Any', 'DB', 'TypeError', 'ValueError', '_ENERGY_FLAG_TO_ENGINE', '_SLEEP_FLAG_TO_ENGINE', '_known_rirs', '_rir_known', 'abs', 'all', 'any', 'assessment', 'average_reps', 'avg_rir', 'best_e1rm', 'bool', 'build_fatigue_assessment', 'comparable_load', 'current_exercise', 'dict', 'energy', 'esc', 'explanation', 'flags', 'float', 'get_daily_flags', 'hard_sessions', 'history', 'hold_for_recovery', 'increment', 'int', 'isinstance', 'known', 'known_latest', 'last_weight', 'latest', 'len', 'list', 'mastered', 'max', 'min', 'planned_sets', 'reason', 'reduced_weight', 'reduction_steps', 'reversed', 'rir_hard', 'rmax', 'rmin', 'round', 'row', 'rows', 's', 'session_row', 'session_rows', 'session_sets', 'session_weight', 'sessions', 'sets', 'sleep', 'soreness', 'srow', 'str', 'sum', 'summaries', 'target_reps', 'training_intelligence', 'tuple', 'used_split_set', 'user_id', 'value')


@runtime_bound(RUNTIME_NAMES)
async def active_session(user_id: int) -> dict[str, Any] | None:
    return await DB.fetch_one(
        """
        SELECT *
        FROM sessions
        WHERE user_id=? AND status='active'
        ORDER BY id DESC
        LIMIT 1
        """,
        (user_id,),
    )


RIR_UNKNOWN = -1


@runtime_bound(RUNTIME_NAMES)
def _rir_known(value: Any) -> bool:
    try:
        return int(value) >= 0
    except (TypeError, ValueError):
        return False


@runtime_bound(RUNTIME_NAMES)
def _known_rirs(rows: list[dict[str, Any]]) -> list[int]:
    return [int(row["rir"]) for row in rows if _rir_known(row.get("rir"))]


@runtime_bound(RUNTIME_NAMES)
async def recommend_load(
    user_id: int,
    current_exercise: dict[str, Any],
) -> tuple[float, int, str]:
    """Recommend the next working load from complete, comparable sessions.

    Only the latest completed/partial session is used for the immediate
    recommendation, so a partial workout can never be mixed with older sets.
    Three consecutive clearly hard sessions trigger a small exercise-specific
    load reduction. Split/drop sets never trigger an automatic progression.
    """
    session_rows = await DB.fetch_all(
        """
        SELECT ws.id, COALESCE(ws.ended_at, ws.started_at) AS performed_at
        FROM sessions ws
        JOIN sets s ON s.session_id=ws.id
        WHERE ws.user_id=?
          AND s.exercise_id=?
          AND ws.status IN ('completed', 'partial')
        GROUP BY ws.id
        ORDER BY performed_at DESC, ws.id DESC
        LIMIT 3
        """,
        (user_id, current_exercise["id"]),
    )

    if not session_rows:
        return (
            float(current_exercise["weight"]),
            int(current_exercise["rmin"]),
            "משקל פתיחה",
        )

    history: list[list[dict[str, Any]]] = []
    for session_row in session_rows:
        rows = await DB.fetch_all(
            """
            SELECT weight, reps, rir, source, set_number
            FROM sets
            WHERE session_id=?
              AND exercise_id=?
              AND source != 'telegram_split_secondary'
            ORDER BY set_number, id
            """,
            (session_row["id"], current_exercise["id"]),
        )
        if rows:
            history.append(rows)

    if not history:
        return (
            float(current_exercise["weight"]),
            int(current_exercise["rmin"]),
            "משקל פתיחה",
        )

    latest = history[0]
    last_weight = float(latest[0]["weight"])
    planned_sets = int(current_exercise["sets"])
    rmin = int(current_exercise["rmin"])
    rmax = int(current_exercise["rmax"])
    increment = max(0.25, float(current_exercise["inc"]))

    flags = await get_daily_flags(user_id)
    hold_for_recovery = flags.get("sleep_quality") == "bad" or flags.get("energy") == "low"

    comparable_load = all(abs(float(row["weight"]) - last_weight) < 0.01 for row in latest)
    used_split_set = any(row["source"] == "telegram_split_primary" for row in latest)
    # Mastery requires hitting the top rep range AND a *reported* RIR >= 2 on
    # every planned set. An unknown RIR never counts as proof of mastery, so we
    # do not auto-progress on fabricated data.
    mastered = (
        len(latest) >= planned_sets
        and comparable_load
        and not used_split_set
        and all(
            int(row["reps"]) >= rmax and _rir_known(row.get("rir")) and int(row["rir"]) >= 2
            for row in latest[:planned_sets]
        )
    )

    if mastered and hold_for_recovery:
        return (
            last_weight,
            rmin,
            "שלטת בטווח, אבל היום שומרים עומס בגלל שינה או אנרגיה נמוכה",
        )

    if mastered:
        return (
            round(last_weight + increment, 2),
            rmin,
            "השלמת את כל הסטים בטווח העליון עם RIR מתאים — עולים מדרגה",
        )

    hard_sessions = 0
    for session_sets in history:
        if len(session_sets) < min(2, planned_sets):
            break
        session_weight = float(session_sets[0]["weight"])
        if abs(session_weight - last_weight) > increment / 2:
            break
        average_reps = sum(int(row["reps"]) for row in session_sets) / len(session_sets)
        known = _known_rirs(session_sets)
        # A "hard" session needs low reps AND, when RIR was reported, a low RIR.
        # If RIR was never reported we fall back to reps alone rather than
        # inventing an RIR of 0.
        rir_hard = (sum(known) / len(known)) <= 1 if known else True
        if average_reps <= rmin and rir_hard:
            hard_sessions += 1
        else:
            break

    if hard_sessions >= 3:
        reduction_steps = max(
            1,
            round((last_weight * 0.075) / increment),
        )
        reduced_weight = max(
            0.0,
            round(last_weight - reduction_steps * increment, 2),
        )
        return (
            reduced_weight,
            rmin,
            "שלושה אימונים רצופים היו קשים בתחתית הטווח — מורידים מעט עומס כדי לבנות מחדש",
        )

    average_reps = sum(int(row["reps"]) for row in latest) / len(latest)
    known_latest = _known_rirs(latest)
    target_reps = int(round(average_reps))
    # Only nudge reps up when the user actually reported RIR >= 2 (reps in
    # reserve). With no reported RIR we keep the current target.
    if known_latest and (sum(known_latest) / len(known_latest)) >= 2 and not used_split_set:
        target_reps += 1
    target_reps = max(rmin, min(rmax, target_reps))

    if used_split_set:
        explanation = "הסט האחרון כלל ירידת משקל, לכן לא מעלים עומס אוטומטית"
    elif len(latest) < planned_sets:
        explanation = "האימון האחרון היה חלקי — שומרים עומס עד שיש ביצוע מלא להשוואה"
    else:
        explanation = "נשארים באותו עומס ומתקדמים בהדרגה בתוך טווח החזרות"

    return last_weight, target_reps, explanation


_SLEEP_FLAG_TO_ENGINE = {"bad": "poor", "ok": "average", "good": "good"}


_ENERGY_FLAG_TO_ENGINE = {"low": "low", "ok": "average", "high": "high"}


@runtime_bound(RUNTIME_NAMES)
async def build_fatigue_assessment(
    user_id: int,
) -> training_intelligence.FatigueAssessment | None:
    """Assess fatigue/plateau from real recent sessions + today's check-in.

    Builds the engine input from the user's last few completed/partial sessions:
    per-session ``avg_rir`` (only over *reported* RIR — unknown RIR is ignored,
    never treated as 0) and best ``e1rm``. Returns None when there is not enough
    history to say anything useful.
    """
    sessions = await DB.fetch_all(
        "SELECT id FROM sessions WHERE user_id=? AND status IN ('completed','partial') "
        "ORDER BY COALESCE(ended_at, started_at) DESC LIMIT 6",
        (user_id,),
    )
    if len(sessions) < 2:
        return None

    summaries: list[dict[str, Any]] = []
    # Oldest-first so the engine's "last N" slicing sees chronological order.
    for srow in reversed(sessions):
        sets = await DB.fetch_all(
            "SELECT weight, reps, rir FROM sets WHERE session_id=? "
            "AND source != 'telegram_split_secondary'",
            (srow["id"],),
        )
        if not sets:
            continue
        known = _known_rirs(sets)
        avg_rir = (sum(known) / len(known)) if known else 3.0  # unknown -> "fresh"
        best_e1rm = max(
            (training_intelligence.epley_1rm(float(s["weight"]), int(s["reps"])) for s in sets),
            default=0.0,
        )
        summaries.append({"avg_rir": avg_rir, "e1rm": best_e1rm})

    if len(summaries) < 2:
        return None

    flags = await get_daily_flags(user_id)
    sleep = _SLEEP_FLAG_TO_ENGINE.get(flags.get("sleep_quality"))
    energy = _ENERGY_FLAG_TO_ENGINE.get(flags.get("energy"))
    soreness = flags.get("soreness")
    return training_intelligence.assess_fatigue(
        summaries,
        sleep_quality=sleep,
        energy=energy,
        soreness=int(soreness) if isinstance(soreness, (int, float)) else None,
    )


@runtime_bound(RUNTIME_NAMES)
async def fatigue_banner(user_id: int) -> str:
    """A short, non-diagnostic deload/plateau heads-up, or '' if none needed."""
    assessment = await build_fatigue_assessment(user_id)
    if not assessment:
        return ""
    if assessment.deload_recommended:
        reason = f" ({', '.join(assessment.reasons)})" if assessment.reasons else ""
        return (
            "🟠 <b>מומלץ שבוע הקלה (deload)</b>"
            f"{esc(reason)}.\n"
            "כדאי להוריד עומס/נפח באימון הזה ולתת לגוף להתאושש. "
            "לא חובה — זו המלצה לפי המגמה האחרונה.\n\n"
        )
    if assessment.plateau:
        return (
            "🟡 <b>נראה שהביצועים נתקעו</b> בכמה אימונים אחרונים. "
            "שקול שינוי קטן בתרגיל, בנפח או מנוחה נוספת.\n\n"
        )
    return ""
