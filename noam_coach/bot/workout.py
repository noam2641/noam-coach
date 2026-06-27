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
    start, end = today_bounds_utc()
    return await DB.fetch_all(
        """
        SELECT id, name, calories, protein, eaten_at
        FROM meals WHERE user_id=? AND eaten_at>=? AND eaten_at<?
        ORDER BY eaten_at
        """,
        (user_id, start, end),
    )


@runtime_bound(RUNTIME_NAMES)
async def build_daily_status(user_id: int) -> str:
    # Single source of truth: the same computed targets the recommendations use.
    goal = await fetch_goal(user_id)
    calories, protein = await today_consumed(user_id)
    meals = await today_meals(user_id)
    meal_count = len(meals)

    # Weight: show the 7-day moving average (smoothing) alongside the latest.
    weight = await user_model.get_value(DB, user_id, "weight_kg")
    avg7 = await DB.fetch_one(
        "SELECT AVG(value) AS v FROM health "
        "WHERE user_id=? AND sample_type='weight' "
        "AND start_time >= ?",
        (user_id, (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()),
    )

    # Steps are retrospective (no live watch). Show the last imported day, not a
    # misleading "today" that is almost always zero.
    last_steps = await DB.fetch_one(
        "SELECT value, start_time FROM health "
        "WHERE user_id=? AND sample_type='steps' "
        "ORDER BY start_time DESC LIMIT 1",
        (user_id,),
    )

    cal_remaining = goal["calories"] - calories
    prot_remaining = goal["protein"] - protein

    provisional = bool(goal.get("provisional"))
    goal_note = " <i>(יעד זמני)</i>" if provisional else ""

    lines = ["<b>מצב היום</b>", ""]

    if meal_count == 0:
        # No meals yet — do NOT present zeros as if the day is complete.
        lines += [
            "<b>תזונה שדווחה היום:</b>",
            "עדיין לא דיווחת ארוחות היום.",
            "",
            "<b>יעדים:</b>",
            f"יעד קלוריות: <b>{goal['calories']}</b>{goal_note}",
            f"יעד חלבון: <b>{goal['protein']} גרם</b>",
        ]
        if provisional:
            lines.append(
                "<i>היעד זמני כי עדיין חסרים נתוני פעילות או אישור סופי של התוכנית.</i>"
            )
        # Health freshness
        from health_service import health_export_freshness, freshness_warning_text
        freshness_info = await health_export_freshness(user_id)
        warning = freshness_warning_text(freshness_info)
        lines.append("")
        if warning:
            lines.append("<b>נתוני פעילות:</b>")
            lines.append(f"<i>{esc(warning)}</i>")
        else:
            lines.append("<b>נתוני פעילות:</b>")
            lines.append("נתוני Apple Health עדכניים ✅")
        lines.append("")
        lines.append("<i>שלח תמונה של אוכל או כתוב מה אכלת כדי להתחיל מעקב.</i>")
        return "\n".join(lines)

    if cal_remaining >= 0:
        cal_line = f"נותרו: <b>{cal_remaining:.0f}</b>"
    else:
        cal_line = f"חריגה: <b>{abs(cal_remaining):.0f} קלוריות מעל היעד</b>"

    if prot_remaining >= 0:
        prot_line = f"נותרו: <b>{prot_remaining:.0f} גרם</b>"
    else:
        prot_line = f"מעל היעד ב־<b>{abs(prot_remaining):.0f} גרם</b>"

    lines += [
        f"דווחו <b>{meal_count}</b> ארוחות היום:",
        *[
            f"• {esc(m['name'])} — {float(m['calories']):.0f} קק\"ל, {float(m['protein']):.0f}ג׳ חלבון"
            for m in meals
        ],
        "",
        f"קלוריות: <b>{calories:.0f} מתוך {goal['calories']}</b>{goal_note}",
        cal_line,
        "",
        f"חלבון: <b>{protein:.0f} מתוך {goal['protein']} גרם</b>",
        prot_line,
        "",
        "<i>הסכום מחושב מהארוחות שדווחו בלבד — ייתכן שאכלת עוד.</i>",
        "",
    ]
    if weight is not None:
        wline = f'משקל אחרון: <b>{float(weight):.1f} ק"ג</b>'
        if avg7 and avg7["v"]:
            wline += f" (ממוצע 7 ימים: {float(avg7['v']):.1f})"
        lines.append(wline)
    else:
        lines.append("משקל: <b>לא התקבל</b>")
    if last_steps and last_steps["value"]:
        day = (last_steps["start_time"] or "")[:10]
        lines.append(f"צעדים (יום אחרון שנקלט {day}): <b>{round(float(last_steps['value'])):,}</b>")
        lines.append("<i>צעדי היום אינם בזמן אמת — מחושבים מהייצוא.</i>")

    # REC-ONBOARD-02-13: health freshness section
    from health_service import health_export_freshness, freshness_warning_text
    freshness_info = await health_export_freshness(user_id)
    warning = freshness_warning_text(freshness_info)
    if warning:
        lines.append("")
        lines.append(f"<i>{esc(warning)}</i>")

    if provisional:
        lines.append("")
        lines.append("<i>היעד זמני כי עדיין חסרים נתוני פעילות או אישור סופי של התוכנית.</i>")

    return "\n".join(lines)


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
                button("מכשיר תפוס", session_action_data("occupied", session)),
                button("⚠️ כאב", session_action_data("pain", session)),
            ],
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
        f"נפח: <b>{volume:,.0f} ק״ג</b> <i>(סכום משקל×חזרות)</i>",
    ]
    if duration <= 0 or set_count <= 1:
        lines.append("")
        lines.append("<i>שים לב: האימון קצר מאוד — ודא שזה מה שהתכוונת.</i>")
    return "\n".join(lines)
