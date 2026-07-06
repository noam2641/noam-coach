# ruff: noqa: F401, F811, F821, I001
"""Goal versioning, targets and goal formatting.

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

RUNTIME_NAMES = ('Any', 'DB', 'GOAL_STATUS_ACTIVE', 'GOAL_STATUS_ACTIVE_PROVISIONAL', 'GOAL_STATUS_PROVISIONAL', 'GOAL_STATUS_SUPERSEDED', 'SETTINGS', 'VALID_GOAL_STRATEGIES', '_goal_type_from_fact', 'active', 'age', 'avg_steps', 'body_fat', 'calories', 'compute_personal_targets', 'computed', 'current', 'dict', 'esc', 'existing', 'explanation', 'f', 'float', 'goal_type', 'goal_weight', 'height', 'int', 'isinstance', 'limit', 'lines', 'list', 'load_routine_profile', 'meal', 'menu', 'now', 'phase', 'planning', 'previous', 'primary_goal', 'profile', 'protein', 'provisional', 'recommendations', 's', 'sex', 'source', 'status', 'steps', 'str', 'strat', 'suggestion', 'summary', 'target_kg', 'targets', 'user_id', 'user_model', 'utc_now', 'version_id', 'weight', 'workouts')


VALID_GOAL_STRATEGIES = {
    "fat_loss_muscle_retention",
    "muscle_gain",
    "strength",
    "general_health",
}


@runtime_bound(RUNTIME_NAMES)
def _goal_type_from_fact(primary_goal: Any) -> str:
    """Normalize the primary_goal fact into a valid goal *strategy* key.

    A goal *weight* is not a strategy — older data stored
    {"type":"goal_weight"} which is not a valid deficit strategy. Such values
    map to fat-loss (the usual intent of a goal weight) rather than to
    maintenance.
    """
    if isinstance(primary_goal, dict):
        strat = primary_goal.get("strategy") or primary_goal.get("type")
        if strat in VALID_GOAL_STRATEGIES:
            return strat
        return "fat_loss_muscle_retention"
    if isinstance(primary_goal, str) and primary_goal in VALID_GOAL_STRATEGIES:
        return primary_goal
    return "fat_loss_muscle_retention"


@runtime_bound(RUNTIME_NAMES)
async def set_goal_weight(user_id: int, target_kg: float) -> None:
    """Store a goal weight as its own fact and ensure a real cut/gain strategy.

    A goal weight below current implies fat loss; above implies a lean gain.
    The strategy (used for the calorie deficit) is kept separate from the
    target weight number.
    """
    await user_model.set_fact(
        DB,
        user_id,
        "goal_weight_kg",
        float(target_kg),
        kind=user_model.KIND_FACT,
        source=user_model.SOURCE_USER,
        confirmed=True,
        affects=("calorie_target", "rate_of_loss"),
    )
    current = await user_model.get_value(DB, user_id, "weight_kg")
    existing = await user_model.get_value(DB, user_id, "primary_goal")
    # Only set a strategy if none is recorded, or the old one was the bogus
    # "goal_weight" placeholder.
    strat = _goal_type_from_fact(existing)
    if existing is None or (isinstance(existing, dict) and existing.get("type") == "goal_weight"):
        if current is not None and target_kg < float(current):
            strat = "fat_loss_muscle_retention"
        elif current is not None and target_kg > float(current):
            strat = "muscle_gain"
    await user_model.set_fact(
        DB,
        user_id,
        "primary_goal",
        strat,
        kind=user_model.KIND_FACT,
        source=user_model.SOURCE_USER,
        confirmed=True,
    )


GOAL_STATUS_PROVISIONAL = "provisional"


GOAL_STATUS_PROPOSED = "proposed"


GOAL_STATUS_APPROVED = "approved"


GOAL_STATUS_ACTIVE = "active"


GOAL_STATUS_ACTIVE_PROVISIONAL = "active_provisional"


GOAL_STATUS_SUPERSEDED = "superseded"


@runtime_bound(RUNTIME_NAMES)
async def create_goal_version(
    user_id: int,
    calories: int,
    protein: int,
    steps: int,
    phase: str,
    *,
    status: str = GOAL_STATUS_PROVISIONAL,
    source: str = "computed",
    explanation: str = "",
) -> int:
    """Create a new goal version. Returns the new goal_version id."""
    now = utc_now()
    return await DB.execute(
        """
        INSERT INTO goal_versions(user_id, calories, protein, steps, phase,
                                  status, source, explanation, created_at)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (user_id, calories, protein, steps, phase, status, source, explanation, now),
    )


@runtime_bound(RUNTIME_NAMES)
async def activate_goal_version(user_id: int, version_id: int) -> None:
    """Mark a goal version as active and supersede all previous active versions."""
    now = utc_now()
    async with DB.transaction() as conn:
        await conn.execute(
            "UPDATE goal_versions SET status=?, decided_at=? "
            "WHERE user_id=? AND status IN (?, ?)",
            (GOAL_STATUS_SUPERSEDED, now, user_id, GOAL_STATUS_ACTIVE, GOAL_STATUS_ACTIVE_PROVISIONAL),
        )
        await conn.execute(
            "UPDATE goal_versions SET status=?, decided_at=? WHERE id=? AND user_id=?",
            (GOAL_STATUS_ACTIVE, now, version_id, user_id),
        )


@runtime_bound(RUNTIME_NAMES)
async def activate_goal_version_provisional(user_id: int, version_id: int) -> None:
    """Make a provisional goal the current goal WITHOUT promoting it to full active.

    Supersedes any previous current goal (active or active_provisional) and marks
    this one ``active_provisional`` so it is used for display but flagged as
    temporary for alerting (P0: a goal with missing mandatory data is never a
    full active goal).
    """
    now = utc_now()
    async with DB.transaction() as conn:
        await conn.execute(
            "UPDATE goal_versions SET status=?, decided_at=? "
            "WHERE user_id=? AND status IN (?, ?)",
            (GOAL_STATUS_SUPERSEDED, now, user_id, GOAL_STATUS_ACTIVE, GOAL_STATUS_ACTIVE_PROVISIONAL),
        )
        await conn.execute(
            "UPDATE goal_versions SET status=?, decided_at=? WHERE id=? AND user_id=?",
            (GOAL_STATUS_ACTIVE_PROVISIONAL, now, version_id, user_id),
        )


@runtime_bound(RUNTIME_NAMES)
async def get_active_goal_version(user_id: int) -> dict[str, Any] | None:
    """Return the current goal version (active or temporary), or None."""
    return await DB.fetch_one(
        "SELECT * FROM goal_versions WHERE user_id=? AND status IN (?, ?) "
        "ORDER BY created_at DESC LIMIT 1",
        (user_id, GOAL_STATUS_ACTIVE, GOAL_STATUS_ACTIVE_PROVISIONAL),
    )


@runtime_bound(RUNTIME_NAMES)
async def get_goal_history(user_id: int, limit: int = 10) -> list[dict[str, Any]]:
    """Return recent goal versions for audit/display."""
    return await DB.fetch_all(
        "SELECT * FROM goal_versions WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
        (user_id, limit),
    )


@runtime_bound(RUNTIME_NAMES)
async def compute_personal_targets(user_id: int) -> targets.Targets | None:
    """Derive personalized targets from the user model, or None if no weight."""
    weight = await user_model.get_value(DB, user_id, "weight_kg")
    if weight is None:
        return None
    avg_steps = await user_model.get_value(DB, user_id, "avg_steps")
    height = await user_model.get_value(DB, user_id, "height_cm")
    sex = await user_model.get_value(DB, user_id, "sex")
    age = await user_model.get_value(DB, user_id, "age")
    goal_weight = await user_model.get_value(DB, user_id, "goal_weight_kg")
    body_fat = await user_model.get_value(DB, user_id, "body_fat_pct")
    timeframe = await user_model.get_value(DB, user_id, "goal_timeframe_weeks")
    profile = await load_routine_profile(user_id)
    workouts = (profile.get("workout") or {}).get("weekly_frequency")
    goal_type = _goal_type_from_fact(await user_model.get_value(DB, user_id, "primary_goal"))
    return targets.compute_targets(
        float(weight),
        avg_steps=float(avg_steps) if avg_steps is not None else None,
        goal_type=goal_type,
        sex=sex,
        height_cm=float(height) if height is not None else None,
        age=int(age) if age is not None else None,
        workouts_per_week=workouts,
        goal_weight_kg=float(goal_weight) if goal_weight is not None else None,
        body_fat_pct=float(body_fat) if body_fat is not None else None,
        goal_timeframe_weeks=float(timeframe) if timeframe is not None else None,
    )


@runtime_bound(RUNTIME_NAMES)
async def target_calories(user_id: int) -> int | None:
    """Current computed calorie target (None if not enough data)."""
    computed = await compute_personal_targets(user_id)
    return computed.calories if computed else None


@runtime_bound(RUNTIME_NAMES)
async def target_change_note(user_id: int, previous: int | None) -> str:
    """Explain how a fact change moved the calorie target (chapter 14)."""
    computed = await compute_personal_targets(user_id)
    if computed is None:
        return "אעדכן את ההמלצות בהתאם."
    if previous and previous != computed.calories:
        return (
            f"יעד הקלוריות עודכן ל-{computed.calories:,} (היה {previous:,}). "
            f"{targets.explain_targets(computed)}"
        )
    return f"יעד הקלוריות שלך: {computed.calories:,} קל׳. {targets.explain_targets(computed)}"


@runtime_bound(RUNTIME_NAMES)
async def fetch_goal(user_id: int) -> dict[str, Any]:
    """Return the single authoritative active goal.

    A fresh computation is only a proposal; it never silently replaces the
    approved target.  This prevents the historical split-brain where ``goals``
    and ``user_facts.calorie_target`` exposed different numbers.
    """
    active = await planning.active_goal(DB, user_id)
    if active:
        source = active.get("source", "approved")
        provisional = (
            source in {"default", "computed_provisional", "user_approved_provisional"}
            or active.get("status") == GOAL_STATUS_ACTIVE_PROVISIONAL
        )
        return {
            "id": active["id"],
            "calories": int(active["calories"]),
            "protein": int(active["protein"]),
            "steps": int(active["steps"]),
            "phase": active["phase"],
            "status": "active_provisional" if provisional else "active",
            "source": source,
            "explanation": active.get("explanation", ""),
            "computed": source not in {"legacy", "user_approved", "manual"},
            "provisional": provisional,
        }

    computed = await compute_personal_targets(user_id)
    if computed is not None:
        await user_model.set_fact(
            DB,
            user_id,
            "calorie_target",
            {
                "calories": computed.calories,
                "protein": computed.protein,
                "explanation": targets.explain_targets(computed),
                "status": "proposal_only",
            },
            kind=user_model.KIND_ESTIMATE,
            source=user_model.SOURCE_DERIVED,
            confidence=0.8 if not computed.provisional else 0.55,
        )
        return {
            "calories": computed.calories,
            "protein": computed.protein,
            "steps": computed.steps,
            "phase": _goal_type_from_fact(
                await user_model.get_value(DB, user_id, "primary_goal")
            ),
            "computed": True,
            "provisional": True,
            "status": "provisional",
            "missing_inputs": computed.missing_inputs or [],
            "explanation": targets.explain_targets(computed),
        }

    return {
        "calories": SETTINGS.default_calories,
        "protein": SETTINGS.default_protein,
        "steps": SETTINGS.default_steps,
        "phase": "fat_loss_muscle_retention",
        "computed": False,
        "provisional": True,
        "status": "default",
        "missing_inputs": ["weight_kg", "primary_goal", "sex", "age"],
        "explanation": "יעד זמני בלבד — חסרים נתונים בסיסיים.",
    }


@runtime_bound(RUNTIME_NAMES)
def format_morning_menu(menu: recommendations.MorningMenu) -> str:
    lines = [f"<b>{esc(menu.headline)}</b>", ""]
    for meal in menu.meals:
        lines.append(
            f"• <b>{esc(meal.name)}</b> ({esc(meal.time_hint)}) — "
            f"{meal.calories:.0f} קל׳, {meal.protein:.0f} ג׳ חלבון"
        )
        if meal.note:
            lines.append(f"   <i>{esc(meal.note)}</i>")
    if menu.training_advice:
        lines += ["", f"🏋️ {esc(menu.training_advice)}"]
    if menu.closing:
        lines += ["", esc(menu.closing)]
    return "\n".join(lines)


@runtime_bound(RUNTIME_NAMES)
def format_next_meals(suggestion: recommendations.NextMealSuggestion) -> str:
    lines = [f"<b>{esc(suggestion.headline)}</b>", ""]
    for meal in suggestion.suggestions:
        lines.append(
            f"• <b>{esc(meal.name)}</b> ({esc(meal.time_hint)}) — "
            f"{meal.calories:.0f} קל׳, {meal.protein:.0f} ג׳ חלבון"
        )
    if suggestion.warning:
        lines += ["", f"⚠️ {esc(suggestion.warning)}"]
    return "\n".join(lines)


@runtime_bound(RUNTIME_NAMES)
def format_evening_summary(summary: recommendations.EveningSummary) -> str:
    lines = [f"<b>{esc(summary.headline)}</b>", ""]
    if summary.strengths:
        lines.append("<b>איפה היית חזק:</b>")
        lines += [f"✅ {esc(s)}" for s in summary.strengths]
        lines.append("")
    if summary.improvements:
        lines.append("<b>מה אפשר לשפר:</b>")
        lines += [f"🔸 {esc(s)}" for s in summary.improvements]
        lines.append("")
    if summary.food_flags:
        lines.append("<b>שים לב למאכלים:</b>")
        lines += [f"⚠️ {esc(f.food)} — {esc(f.reason)}" for f in summary.food_flags]
        lines.append("")
    if summary.closing:
        lines.append(esc(summary.closing))
    return "\n".join(lines)
