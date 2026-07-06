# ruff: noqa: F401, F811, F821, I001
"""Workout display, set persistence and workout summaries.

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
from noam_coach.services import daily_state

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

RUNTIME_NAMES = ('Any', 'DB', 'Exception', 'InlineKeyboardMarkup', 'KeyError', 'StopIteration', 'TypeError', 'ValueError', '_StaleSetStep', '_rir_known', 'abs', 'advance_sql', 'avg7', 'bool', 'button', 'cal_line', 'cal_remaining', 'calories', 'client_event_id', 'completed', 'conn', 'cue', 'cues', 'cur', 'current', 'cursor', 'datetime', 'day', 'dict', 'duration', 'end', 'ended', 'enumerate', 'esc', 'ex', 'exercise_index', 'explanation', 'fetch_goal', 'float', 'goal', 'goal_note', 'header', 'home_keyboard', 'i', 'idx', 'int', 'json', 'keyboard', 'last', 'last_steps', 'len', 'lines', 'list', 'm', 'meal_count', 'meals', 'muscle', 'muscle_line', 'next', 'plan', 'planned_sets', 'prev', 'prev_line', 'prev_rir', 'prev_rir_label', 'prot_line', 'prot_remaining', 'protein', 'provisional', 'query', 'recommend_load', 'reps', 'rest_line', 'rest_seconds', 'rir', 'rir_line', 'round', 'row', 'rows', 'safe_edit', 'save_set', 'session', 'session_action_data', 'session_id', 'set_count', 'set_no', 'source', 'start', 'started', 'status', 'str', 'sum', 'target_rir', 'text', 'timedelta', 'timezone', 'today_bounds_utc', 'today_consumed', 'today_meals', 'tuple', 'user_id', 'user_model', 'utc_now', 'volume', 'weight', 'why_line', 'wline')


@runtime_bound(RUNTIME_NAMES)
async def today_meals(user_id: int) -> list[dict[str, Any]]:
    """Return today's saved meals (the rows that make up the daily total)."""
    return await daily_state.consumed_meals(DB, user_id)


@runtime_bound(RUNTIME_NAMES)
async def build_daily_status(user_id: int) -> str:
    """RE10-13: rich "מצב היום" built from the single nutrition-context source
    of truth (D4/D9/D10), with NO stale Apple Health activity section (that
    freshness warning belongs to the Health screens, not here).
    """
    from noam_coach.services.next_meal import (
        build_workout_nutrition_context,
        generate_next_meal_recommendation,
    )
    from noam_coach.services.nutrition_context import build_nutrition_context
    from noam_coach.services.next_meal import build_remaining_slot_allocations

    goal = await fetch_goal(user_id)
    meals = await today_meals(user_id)
    meal_count = len(meals)
    provisional = bool(goal.get("provisional"))

    lines = ["<b>מצב היום</b>", ""]

    if meal_count == 0:
        # No meals yet — do NOT present zeros as if the day is complete.
        lines += [
            "<b>תזונה שדווחה היום:</b>",
            "עדיין לא דיווחת ארוחות היום.",
            "",
            "<b>יעדים:</b>",
        ]
        goal_note = " <i>(יעד זמני — עוד לא אושר)</i>" if provisional else ""
        lines.append(f"יעד קלוריות: <b>{goal['calories']}</b>{goal_note}")
        lines.append(f"יעד חלבון: <b>{goal['protein']} גרם</b>")
        if provisional:
            lines.append("<i>אשר את היעד דרך \"יעדים\" כדי שההמלצות יהיו מדויקות.</i>")
        lines.append("")
        lines.append("<i>שלח תמונה של אוכל או כתוב מה אכלת כדי להתחיל מעקב.</i>")
        return "\n".join(lines)

    context = await build_nutrition_context(DB, user_id, "daily_status")
    workout_context = await build_workout_nutrition_context(DB, user_id)

    cal_remaining = context.remaining_calories
    prot_remaining = context.remaining_protein
    goal_note = " <i>(יעד זמני — עוד לא אושר)</i>" if provisional else ""

    if cal_remaining is None:
        cal_line = "לא ניתן לחשב יתרה (אין יעד פעיל)."
    elif cal_remaining >= 0:
        cal_line = f"נשארו לך היום <b>{cal_remaining:.0f}</b> קלוריות"
    else:
        cal_line = f"חריגה של <b>{abs(cal_remaining):.0f}</b> קלוריות מעל היעד"

    if prot_remaining is not None:
        if prot_remaining >= 0:
            cal_line += f" ו-<b>{prot_remaining:.0f}</b> גרם חלבון{goal_note}."
        else:
            cal_line += f", וחריגה של <b>{abs(prot_remaining):.0f}</b> גרם חלבון מעל היעד{goal_note}."
    else:
        cal_line += f"{goal_note}."

    lines.append(cal_line)

    if context.hours_until_sleep is not None:
        bedtime_line = f"עד שינה נשאר כ-{context.hours_until_sleep:.1f} שעות."
        lines.append(bedtime_line)

    # RE10-13: "planned workout that has not been reported" gets a single,
    # explicit assumption line — never silently assumed without saying so.
    workout_assumed_pre = (
        workout_context.workout_source == "active_workout_plan"
        and workout_context.workout_phase.value.startswith("pre_workout")
    )
    if workout_assumed_pre:
        lines.append("תוכנן אימון היום שעדיין לא דווח — אניח שאתה לפני אימון.")

    lines.append("")
    lines.append(f"דווחו <b>{meal_count}</b> ארוחות היום:")
    lines.extend(
        f"• {esc(m['name'])} — {float(m['calories']):.0f} קק\"ל, {float(m['protein']):.0f}ג׳ חלבון"
        for m in meals
    )
    lines.append("<i>הסכום שדווחו בלבד — ייתכן שאכלת עוד.</i>")

    allocations = build_remaining_slot_allocations(workout_context)
    if allocations:
        lines.append("")
        lines.append("<b>ארוחות עד סוף היום:</b>")
        for allocation in allocations:
            if allocation.is_night_meal:
                lines.append(f"{esc(allocation.label)} — כ-{allocation.calories} קל'")
            else:
                lines.append(
                    f"{esc(allocation.label)} — כ-{allocation.calories} קל' | כ-{allocation.protein} גרם חלבון"
                )
        lines.append("<i>(הקלוריות והחלבון לפי היתרה שנותרה, ומתעדכנים ככל שמדווחים ארוחות)</i>")

    try:
        recommendation = await generate_next_meal_recommendation(DB, user_id)
    except Exception:  # noqa: BLE001 - the day summary must render even if the recommender fails
        recommendation = None
    if recommendation is not None and recommendation.options:
        from noam_coach.services.next_meal import format_next_meal_recommendation

        lines.append("")
        lines.append("<b>הארוחה הבאה שלך:</b>")
        lines.append(format_next_meal_recommendation(recommendation))

    if provisional:
        lines.append("")
        lines.append("<i>היעד זמני כי עדיין חסר אישור סופי — אפשר לאשר דרך \"יעדים\".</i>")

    return "\n".join(lines)


@runtime_bound(RUNTIME_NAMES)
async def _exercise_pain_warning_line(user_id: int, current: dict[str, Any]) -> str:
    """A specific, per-exercise warning when this exercise loads a region the
    user recently reported pain in — shown on the exercise card itself
    (not just a general banner at the start of the workout), and offered
    before the user has to tap "⚠️ כאב" again."""
    profile = training_intelligence.CATALOG.get(str(current.get("id")))
    if profile is None or not profile.joint_load:
        return ""
    rows = await DB.fetch_all(
        "SELECT * FROM medical_constraints WHERE user_id=? AND kind='pain'",
        (user_id,),
    )
    if not rows:
        return ""
    regions = training_intelligence.active_pain_regions(rows)
    hit = next((regions[j] for j in profile.joint_load if j in regions), None)
    if hit is None:
        return ""
    return f"⚠️ <i>{esc(training_intelligence.pain_safety_guidance(hit))}</i>\n"


@runtime_bound(RUNTIME_NAMES)
async def show_session(query: Any, user_id: int, session_id: int) -> None:
    session = await DB.fetch_one(
        """
        SELECT *
        FROM sessions
        WHERE id=? AND user_id=?
        """,
        (session_id, user_id),
    )
    if not session or session["status"] != "active":
        await safe_edit(query, "האימון אינו פעיל.", home_keyboard())
        return

    plan = json.loads(session["plan"])
    current = plan["exercises"][session["exercise_index"]]
    weight, reps, explanation = await recommend_load(user_id, current)

    if session["pending_weight"] is not None:
        weight = float(session["pending_weight"])
    if session["pending_reps"] is not None:
        reps = int(session["pending_reps"])

    cues = "\n".join(f"• {cue}" for cue in current["cues"])
    muscle = current.get("muscle")
    muscle_line = f"🎯 שריר מטרה: <b>{muscle}</b>\n" if muscle else ""
    pain_warning_line = await _exercise_pain_warning_line(user_id, current)

    # Progression rationale comes from recommend_load — surface it so the weight
    # is explained rather than appearing arbitrary (P1).
    why_line = f"<i>{esc(explanation)}</i>\n" if explanation else ""

    # Rest + RIR target give the user the full prescription on the card (P1).
    rest_seconds = int(current.get("rest") or 0)
    rest_line = (
        f"מנוחה מומלצת: <b>{rest_seconds // 60}:{rest_seconds % 60:02d} דק׳</b>\n"
        if rest_seconds
        else ""
    )
    target_rir = current.get("target_rir")
    rir_line = (
        f"יעד RIR: <b>{target_rir}</b> (כמה חזרות נשארו ברזרבה)\n"
        if target_rir is not None
        else ""
    )

    # Previous performance on this exercise, for comparison (P1).
    prev = await DB.fetch_one(
        """
        SELECT s.weight, s.reps, s.rir FROM sets s
        JOIN sessions ses ON ses.id = s.session_id
        WHERE ses.user_id=? AND s.exercise_id=? AND s.session_id != ?
          AND s.source != 'telegram_split_secondary'
        ORDER BY s.id DESC LIMIT 1
        """,
        (user_id, current["id"], session_id),
    )
    if prev:
        prev_rir = prev["rir"]
        prev_rir_label = "" if not _rir_known(prev_rir) else f" · RIR {int(prev_rir)}"
        prev_line = f"פעם קודמת: {prev['weight']:g} ק״ג × {prev['reps']}{prev_rir_label}\n"
    else:
        prev_line = ""

    text = (
        f"<b>{current['name']}</b>\n"
        f"תרגיל {session['exercise_index'] + 1}/{len(plan['exercises'])} · "
        f"סט <b>{session['set_number']}/{current['sets']}</b>\n\n"
        f"{muscle_line}"
        f"משקל: <b>{weight:g} ק״ג</b>\n"
        f"חזרות: <b>{reps}</b> (טווח {current['rmin']}–{current['rmax']})\n"
        f"{rir_line}"
        f"{rest_line}"
        f"{why_line}"
        f"{prev_line}"
        f"{pain_warning_line}"
        f"\n<b>דגשים</b>\n{cues}"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                button(
                    f"✅ בוצע {weight:g} × {reps}",
                    session_action_data("setok", session),
                )
            ],
            [
                button("ביצעתי אחרת", session_action_data("different", session)),
                button("סט מפוצל", session_action_data("split", session)),
            ],
            [
                button("ציוד/מכשיר תפוס", session_action_data("occupied", session)),
                button("⚠️ כאב", session_action_data("pain", session)),
            ],
            [button("איך חושב?", session_action_data("loadwhy", session))],
            [button("סיים", session_action_data("finish", session))],
        ]
    )
    await safe_edit(query, text, keyboard)


@runtime_bound(RUNTIME_NAMES)
async def save_set(
    session: dict[str, Any],
    weight: float,
    reps: int,
    rir: int,
    source: str,
    client_event_id: str | None = None,
) -> tuple[bool, int]:
    """Log a set and advance the session ATOMICALLY.

    The session advance is optimistic: it only fires if the session is still at
    the (exercise_index, set_number) we read. If a concurrent or duplicate
    save already advanced it, our advance affects 0 rows and we skip inserting
    the duplicate set — so a double-tap or a Telegram+watch race logs the set
    exactly once.
    """
    plan = json.loads(session["plan"])
    idx = session["exercise_index"]
    set_no = session["set_number"]
    current = plan["exercises"][idx]
    completed = False

    async with DB.transaction() as conn:
        if set_no < current["sets"]:
            advance_sql = (
                "UPDATE sessions SET set_number=set_number+1, "
                "pending_weight=NULL, pending_reps=NULL "
                "WHERE id=? AND status='active' AND exercise_index=? "
                "AND set_number=?"
            )
        elif idx + 1 < len(plan["exercises"]):
            advance_sql = (
                "UPDATE sessions SET exercise_index=exercise_index+1, "
                "set_number=1, pending_weight=NULL, pending_reps=NULL "
                "WHERE id=? AND status='active' AND exercise_index=? "
                "AND set_number=?"
            )
        else:
            completed = True
            advance_sql = (
                "UPDATE sessions SET status='completed', ended_at=? "
                "WHERE id=? AND status='active' AND exercise_index=? "
                "AND set_number=?"
            )

        if completed:
            cur = await conn.execute(advance_sql, (utc_now(), session["id"], idx, set_no))
        else:
            cur = await conn.execute(advance_sql, (session["id"], idx, set_no))

        if cur.rowcount != 1:
            # Someone already advanced this step — don't double-log the set.
            raise _StaleSetStep()

        await conn.execute(
            """
            INSERT INTO sets(
                session_id, exercise_id, exercise_name, set_number,
                weight, reps, rir, source, client_event_id, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session["id"],
                current["id"],
                current["name"],
                set_no,
                weight,
                reps,
                rir,
                source,
                client_event_id,
                utc_now(),
            ),
        )

    return completed, current["rest"]


@runtime_bound(RUNTIME_NAMES)
async def undo_last_set(user_id: int, session_id: int) -> bool:
    """Delete the most recently logged set and rewind the session pointer.

    Returns True if a set was removed. The delete + rewind are wrapped in one
    transaction so an undo can never leave the pointer and the sets out of sync.
    Split secondary sets are removed together with their primary so a split set
    undoes as one unit.
    """
    async with DB.transaction() as conn:
        cursor = await conn.execute(
            "SELECT * FROM sessions WHERE id=? AND user_id=?",
            (session_id, user_id),
        )
        session = await cursor.fetchone()
        if not session:
            return False

        cursor = await conn.execute(
            "SELECT id, exercise_id, set_number, source FROM sets "
            "WHERE session_id=? ORDER BY id DESC LIMIT 1",
            (session_id,),
        )
        last = await cursor.fetchone()
        if not last:
            return False

        # Map the set's exercise_id back to its index in the plan so we can
        # rewind the pointer (the sets table stores exercise_id, not the index).
        try:
            plan = json.loads(session["plan"])
            exercise_index = next(
                i for i, ex in enumerate(plan["exercises"]) if ex["id"] == last["exercise_id"]
            )
        except (KeyError, StopIteration, TypeError, json.JSONDecodeError):
            exercise_index = session["exercise_index"]

        # Remove the last set (and a split-secondary partner logged with it).
        await conn.execute("DELETE FROM sets WHERE id=?", (last["id"],))
        if last["source"] == "telegram_split_primary":
            await conn.execute(
                "DELETE FROM sets WHERE session_id=? AND source='telegram_split_secondary' "
                "AND exercise_id=? AND set_number=?",
                (session_id, last["exercise_id"], last["set_number"]),
            )

        # Rewind the pointer to where this set was performed, and reactivate the
        # session if it had auto-completed on the final set.
        await conn.execute(
            "UPDATE sessions SET exercise_index=?, set_number=?, status='active', "
            "ended_at=NULL, pending_weight=NULL, pending_reps=NULL WHERE id=?",
            (exercise_index, last["set_number"], session_id),
        )
    return True


class _StaleSetStep(Exception):
    """The session already advanced past this set (duplicate/concurrent save)."""


@runtime_bound(RUNTIME_NAMES)
async def try_save_set(
    session: dict[str, Any],
    weight: float,
    reps: int,
    rir: int,
    source: str,
    client_event_id: str | None = None,
) -> tuple[bool, int] | None:
    """save_set that returns None instead of raising when the step is stale."""
    try:
        return await save_set(session, weight, reps, rir, source, client_event_id)
    except _StaleSetStep:
        return None


@runtime_bound(RUNTIME_NAMES)
async def workout_summary(user_id: int, session_id: int) -> str:
    async with DB.transaction() as conn:
        cursor = await conn.execute(
            "SELECT * FROM sessions WHERE id=? AND user_id=?",
            (session_id, user_id),
        )
        session_row = await cursor.fetchone()
        session = dict(session_row) if session_row else None
        cursor = await conn.execute(
            "SELECT weight, reps, source FROM sets WHERE session_id=?",
            (session_id,),
        )
        rows = [dict(r) for r in await cursor.fetchall()]
    volume = sum(row["weight"] * row["reps"] for row in rows)
    set_count = sum(1 for row in rows if row["source"] != "telegram_split_secondary")
    started = datetime.fromisoformat(session["started_at"])
    ended = datetime.fromisoformat(session["ended_at"] or utc_now())
    duration = round((ended - started).total_seconds() / 60)

    planned_sets = 0
    try:
        plan = json.loads(session["plan"])
        planned_sets = sum(int(ex["sets"]) for ex in plan["exercises"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        planned_sets = 0

    status = session["status"]
    # Header reflects the real status — a short or 1-set workout is never shown
    # as a full completion (P0).
    if status == "completed":
        header = "<b>האימון הושלם ✅</b>"
    elif status == "partial":
        header = "<b>האימון נשמר כחלקי ⏳</b>"
    elif status == "cancelled":
        header = "<b>האימון בוטל ❌</b>"
    else:
        header = "<b>האימון נשמר ✅</b>"

    lines = [
        header,
        "",
        f"משך: <b>{duration} דקות</b>",
        f"סטים: <b>{set_count}" + (f" מתוך {planned_sets}" if planned_sets else "") + "</b>",
        f"נפח עבודה: <b>{volume:,.0f} ק״ג×חזרות</b> <i>(סכום משקל×חזרות)</i>",
    ]
    if duration <= 0 or set_count <= 1:
        lines.append("")
        lines.append("<i>שים לב: האימון קצר מאוד — ודא שזה מה שהתכוונת.</i>")
    return "\n".join(lines)
