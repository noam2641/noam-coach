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


@dataclass(frozen=True)
class LoadRecommendation:
    weight: float
    reps: int
    explanation: str
    decision: str
    signals: tuple[str, ...] = ()
    missing_context: tuple[str, ...] = ()
    confidence: int = 80
    data_completeness: int = 80

    def to_tuple(self) -> tuple[float, int, str]:
        return self.weight, self.reps, self.explanation

    def to_audit_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "recommended_weight": self.weight,
            "recommended_reps": self.reps,
            "explanation": self.explanation,
            "signals": list(self.signals),
            "missing_context": list(self.missing_context),
            "confidence": self.confidence,
            "data_completeness": self.data_completeness,
        }


def format_load_decision_details(decision: LoadRecommendation) -> str:
    """Render an auditable load decision for the workout "how was this decided" UI."""
    lines = [
        "<b>איך חושב?</b>",
        f"משקל מומלץ: <b>{decision.weight:g} ק״ג</b>",
        f"חזרות מומלצות: <b>{decision.reps}</b>",
        f"סיבה: {decision.explanation}",
    ]
    if decision.signals:
        labels = {
            "active_pain": "כאב פעיל",
            "hard_sessions": "אימונים קשים לאחרונה",
            "sleep_quality": "שינה",
            "energy": "אנרגיה",
            "mastered_top_range": "שליטה בטווח העליון",
            "recovery_hold": "שמירה להתאוששות",
            "mixed_load_latest_session": "עומסים מעורבים באימון האחרון",
            "split_or_drop_set": "סט מפוצל/ירידת משקל",
            "rir_allows_rep_progression": "RIR מאפשר התקדמות בחזרות",
            "rir_missing": "RIR חסר",
        }
        rendered_signals: list[str] = []
        for signal in decision.signals:
            key, _, value = signal.partition(":")
            label = labels.get(key, key.replace("_", " "))
            rendered_signals.append(f"{label}: {value}" if value else label)
        lines.extend(["", "<b>נתונים שהשפיעו</b>"])
        lines.extend(f"• {signal}" for signal in rendered_signals)
    if decision.missing_context:
        missing_labels = {
            "exercise_history": "אין עדיין היסטוריית ביצוע לתרגיל",
            "comparable_sets": "אין סטים בני השוואה",
        }
        lines.extend(["", "<b>מה חסר כדי לדייק</b>"])
        lines.extend(f"• {missing_labels.get(item, item)}" for item in decision.missing_context)
    lines.extend(
        [
            "",
            f"<i>ביטחון: {decision.confidence}/100 · שלמות מידע: {decision.data_completeness}/100</i>",
        ]
    )
    return "\n".join(lines)


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
async def _exercise_pain_caution(
    user_id: int, current_exercise: dict[str, Any]
) -> training_intelligence.ActivePainRegion | None:
    """Return the active pain region (if any) that this exercise loads.

    Reads medical_constraints directly (kind='pain', status='active', within
    the TTL window) rather than only the onboarding-time active_pain fact, so
    a pain report made mid-workout also caps progression on the very next
    session for the same joint, not just on plans generated after it.
    """
    profile = training_intelligence.CATALOG.get(str(current_exercise.get("id")))
    if profile is None or not profile.joint_load:
        return None
    rows = await DB.fetch_all(
        "SELECT * FROM medical_constraints WHERE user_id=? AND kind='pain'",
        (user_id,),
    )
    if not rows:
        return None
    regions = training_intelligence.active_pain_regions(rows)
    for joint in profile.joint_load:
        if joint in regions:
            return regions[joint]
    return None


@runtime_bound(RUNTIME_NAMES)
async def recommend_load_decision(
    user_id: int,
    current_exercise: dict[str, Any],
) -> LoadRecommendation:
    """Recommend the next working load with an auditable decision record.

    Only the latest completed/partial session is used for the immediate
    recommendation, so a partial workout can never be mixed with older sets.
    Three consecutive clearly hard sessions trigger a small exercise-specific
    load reduction. Split/drop sets never trigger an automatic progression.
    An active, reported pain in a region this exercise loads (per
    training_intelligence.CATALOG joint_load) also blocks any weight/rep
    increase, regardless of RIR history — pain caution always outranks a
    "mastered" reading.
    """
    pain_caution = await _exercise_pain_caution(user_id, current_exercise)
    missing_context: list[str] = []
    signals: list[str] = []
    if pain_caution is not None:
        signals.append(f"active_pain:{pain_caution.region}")

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
        missing_context.append("exercise_history")
        return LoadRecommendation(
            float(current_exercise["weight"]),
            int(current_exercise["rmin"]),
            "משקל מתוכנן",
            "planned_load",
            tuple(signals),
            tuple(missing_context),
            confidence=55,
            data_completeness=45,
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
        missing_context.append("comparable_sets")
        return LoadRecommendation(
            float(current_exercise["weight"]),
            int(current_exercise["rmin"]),
            "משקל מתוכנן",
            "planned_load",
            tuple(signals),
            tuple(missing_context),
            confidence=55,
            data_completeness=45,
        )

    latest = history[0]
    last_weight = float(latest[0]["weight"])
    planned_sets = int(current_exercise["sets"])
    rmin = int(current_exercise["rmin"])
    rmax = int(current_exercise["rmax"])
    increment = max(0.25, float(current_exercise["inc"]))

    flags = await get_daily_flags(user_id)
    hold_for_recovery = flags.get("sleep_quality") == "bad" or flags.get("energy") == "low"
    if flags.get("sleep_quality") == "bad":
        signals.append("sleep_quality:bad")
    if flags.get("energy") == "low":
        signals.append("energy:low")

    comparable_load = all(abs(float(row["weight"]) - last_weight) < 0.01 for row in latest)
    used_split_set = any(row["source"] == "telegram_split_primary" for row in latest)
    if not comparable_load:
        signals.append("mixed_load_latest_session")
    if used_split_set:
        signals.append("split_or_drop_set")
    # Mastery requires hitting the top rep range AND a *reported* RIR >= 2 on
    # every planned set. An unknown RIR never counts as proof of mastery, so we
    # do not auto-progress on fabricated data. An active, reported pain in a
    # region this exercise loads blocks mastery outright — RIR history can
    # never justify a load increase while that pain is still active.
    mastered = (
        pain_caution is None
        and len(latest) >= planned_sets
        and comparable_load
        and not used_split_set
        and all(
            int(row["reps"]) >= rmax and _rir_known(row.get("rir")) and int(row["rir"]) >= 2
            for row in latest[:planned_sets]
        )
    )

    if mastered and hold_for_recovery:
        signals.append("mastered_top_range")
        signals.append("recovery_hold")
        return LoadRecommendation(
            last_weight,
            rmin,
            "שלטת בטווח, אבל היום שומרים עומס בגלל שינה או אנרגיה נמוכה",
            "hold_for_recovery",
            tuple(signals),
            tuple(missing_context),
            confidence=82,
            data_completeness=90,
        )

    if mastered:
        signals.append("mastered_top_range")
        return LoadRecommendation(
            round(last_weight + increment, 2),
            rmin,
            "השלמת את כל הסטים בטווח העליון עם RIR מתאים — עולים מדרגה",
            "increase_load",
            tuple(signals),
            tuple(missing_context),
            confidence=88,
            data_completeness=92,
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
    if hard_sessions:
        signals.append(f"hard_sessions:{hard_sessions}")

    if hard_sessions >= 3:
        reduction_steps = max(
            1,
            round((last_weight * 0.075) / increment),
        )
        reduced_weight = max(
            0.0,
            round(last_weight - reduction_steps * increment, 2),
        )
        if pain_caution is not None:
            return LoadRecommendation(
                reduced_weight,
                rmin,
                f"דיווחת לאחרונה על כאב ב{pain_caution.label} וגם הביצועים היו קשים — מורידים מעט עומס",
                "reduce_load_for_pain_and_hard_history",
                tuple(signals),
                tuple(missing_context),
                confidence=90,
                data_completeness=92,
            )
        return LoadRecommendation(
            reduced_weight,
            rmin,
            "שלושה אימונים רצופים היו קשים בתחתית הטווח — מורידים מעט עומס כדי לבנות מחדש",
            "reduce_load_for_hard_history",
            tuple(signals),
            tuple(missing_context),
            confidence=86,
            data_completeness=90,
        )

    if pain_caution is not None:
        # Never raise weight or reps while a reported pain is active in a
        # region this exercise loads. If recent hard sessions already meet the
        # normal deload rule above, that reduction still wins.
        return LoadRecommendation(
            last_weight,
            rmin,
            f"דיווחת לאחרונה על כאב ב{pain_caution.label} — שומר עומס שמרני בתרגיל הזה",
            "hold_for_active_pain",
            tuple(signals),
            tuple(missing_context),
            confidence=86,
            data_completeness=88,
        )

    average_reps = sum(int(row["reps"]) for row in latest) / len(latest)
    known_latest = _known_rirs(latest)
    target_reps = int(round(average_reps))
    # Only nudge reps up when the user actually reported RIR >= 2 (reps in
    # reserve). With no reported RIR we keep the current target.
    if known_latest and (sum(known_latest) / len(known_latest)) >= 2 and not used_split_set:
        signals.append("rir_allows_rep_progression")
        target_reps += 1
    elif not known_latest:
        signals.append("rir_missing")
    target_reps = max(rmin, min(rmax, target_reps))

    if used_split_set:
        explanation = "הסט האחרון כלל ירידת משקל, לכן לא מעלים עומס אוטומטית"
    elif len(latest) < planned_sets:
        explanation = "האימון האחרון היה חלקי — שומרים עומס עד שיש ביצוע מלא להשוואה"
    else:
        explanation = "נשארים באותו עומס ומתקדמים בהדרגה בתוך טווח החזרות"

    decision = "hold_after_split_set" if used_split_set else "hold_or_progress_reps"
    return LoadRecommendation(
        last_weight,
        target_reps,
        explanation,
        decision,
        tuple(signals),
        tuple(missing_context),
        confidence=78 if known_latest else 68,
        data_completeness=86 if known_latest else 70,
    )


@runtime_bound(RUNTIME_NAMES)
async def recommend_load(
    user_id: int,
    current_exercise: dict[str, Any],
) -> tuple[float, int, str]:
    """Backward-compatible tuple wrapper for the auditable load decision."""
    decision = await recommend_load_decision(user_id, current_exercise)
    return decision.to_tuple()


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
