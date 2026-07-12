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


def _goal_source_line(goal: dict[str, Any]) -> str:
    """One explicit provenance line for the daily target (Codex audit round):
    the user must always see whether the numbers are an approved goal, a
    provisional computation, or a default."""
    if goal.get("provisional"):
        return "מקור היעד: חישוב זמני מהנתונים — טרם אושר סופית."
    if goal.get("source") in {"manual", "user_approved", "legacy"}:
        return "מקור היעד: יעד שאישרת."
    return "מקור היעד: יעד פעיל מחושב."


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
    from noam_coach.services.user_state import build_shared_state

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
        lines.append(f"<i>{_goal_source_line(goal)}</i>")
        if provisional:
            lines.append("<i>אשר את היעד דרך \"יעדים\" כדי שההמלצות יהיו מדויקות.</i>")
        lines.append("")
        lines.append("<i>שלח תמונה של אוכל או כתוב מה אכלת כדי להתחיל מעקב.</i>")
        return "\n".join(lines)

    # REC-ARCH-01: one shared snapshot for the whole handler — nutrition
    # context, the workout view, and the next-meal recommendation all read
    # the SAME resolved workout state instead of each independently
    # re-querying sessions/plan/routine (three DB round-trips for the same
    # fact before this fix).
    shared_state = await build_shared_state(DB, user_id)
    context = await build_nutrition_context(DB, user_id, "daily_status", shared_state=shared_state)
    workout_context = await build_workout_nutrition_context(
        DB, user_id, now=shared_state.now, workout_state=shared_state.workout
    )

    # TASK-13: a concise dashboard — current state, what happened, next action.
    # Remaining calories/protein appear exactly ONCE; no duplicate workout
    # status, no duplicate next-meal planning sections, no internal engine text.
    goal_note = " <i>(יעד זמני)</i>" if provisional else ""

    # TASK-16: shared UI-format helpers keep macro/heading/time formatting
    # consistent across the coach's nutrition/summary views.
    from noam_coach.bot import ui_format as uf

    # 1) State — consumed / target / remaining, once.
    now_label = uf.local_hhmm(context.current_local_time)
    lines[0] = uf.heading("📊", "מצב היום", suffix=now_label)
    lines.append(
        f"{context.consumed_calories:.0f} / {uf.cal(goal['calories'])}{goal_note}"
    )
    lines.append(f"{context.consumed_protein:.0f} / {uf.protein(goal['protein'])}")
    cal_remaining = context.remaining_calories
    prot_remaining = context.remaining_protein
    if cal_remaining is not None and prot_remaining is not None:
        lines.append("")
        lines.append("<b>נשאר:</b>")
        lines.append(uf.cal(cal_remaining) if cal_remaining >= 0 else f"חריגה של {uf.cal(abs(cal_remaining))}")
        lines.append(uf.protein(prot_remaining) if prot_remaining >= 0 else f"חריגה של {uf.protein(abs(prot_remaining))}")

    # 2) What happened — logged meals, concisely.
    lines.append("")
    lines.append(uf.heading("🍽️", "נאכל היום"))
    for m in meals:
        lines.append(f"• {esc(m['name'])}")
        lines.append(uf.macros(float(m["calories"]), float(m["protein"])))

    # 3) Rest of the day — only meaningful chronological next events, without a
    # duplicated macro block or internal explanation.
    from noam_coach.services.next_meal import _hhmm_from_iso

    allocations = build_remaining_slot_allocations(workout_context)
    timeline: list[tuple[str, str]] = []
    phase = workout_context.workout_phase.value
    if phase.startswith("pre_workout") or (
        phase == "rest_day" and workout_context.minutes_until_workout not in (None, 0)
    ):
        wk = _hhmm_from_iso(workout_context.planned_workout_start)
        if wk:
            timeline.append((wk, f"🏋️ {esc(wk)} אימון"))
    for allocation in allocations:
        time_hint = allocation.time_hint or ""
        prefix = f"{esc(time_hint)} · " if time_hint else ""
        timeline.append((
            time_hint or "99:99",
            f"🍽️ {prefix}{esc(allocation.label)} — {uf.macros(allocation.calories, allocation.protein, approx=True)}",
        ))
    if timeline:
        timeline.sort(key=lambda item: item[0])
        lines.append("")
        lines.append(uf.heading("⏱️", "המשך היום"))
        lines.extend(text for _t, text in timeline)

    # 4) One actionable recommendation — the immediate meal title + macros only,
    # not the full next-meal screen (which repeats state/timeline).
    try:
        recommendation = await generate_next_meal_recommendation(DB, user_id, shared_state=shared_state)
    except Exception:  # noqa: BLE001 - the day summary must render even if the recommender fails
        recommendation = None
    if recommendation is not None and recommendation.options:
        option = recommendation.options[0]
        lines.append("")
        lines.append(uf.heading("🍽️", "מומלץ עכשיו"))
        lines.append(f"{esc(option.title)} — {uf.macros(option.calories, option.protein, approx=True)}")

    return "\n".join(lines)


_WORKOUT_STATUS_LINE_BY_PHASE = {
    "pre_workout_early": "יש אימון מתוכנן היום, עדיין לפני.",
    "pre_workout_near": "יש אימון מתוכנן היום, עדיין לפני.",
    "pre_workout_immediate": "יש אימון מתוכנן היום, עדיין לפני.",
    "during_workout": "האימון של היום פעיל כרגע.",
    "post_workout_immediate": "האימון של היום דווח.",
    "post_workout_later": "האימון של היום דווח.",
    "workout_completed_earlier": "האימון של היום דווח.",
    "workout_planned_time_passed": "היה אימון מתוכנן היום שעדיין לא דווח.",
    "workout_status_unknown": "היה אימון מתוכנן היום שעדיין לא דווח.",
}


@runtime_bound(RUNTIME_NAMES)
async def render_post_meal_confirmation_day_status(user_id: int, totals: dict[str, float]) -> str:
    """TASK-14: short status shown right after a meal is confirmed.

    Deliberately NOT build_daily_status (the long "מצב היום" summary), a menu,
    or a next-meal recommendation — just: saved-confirmation, the meal's own
    macros, today's running totals/remaining budget, current time, and what's
    left of the day (remaining meal slots + workout status).
    """
    from noam_coach.services.next_meal import build_remaining_slot_allocations, build_workout_nutrition_context
    from noam_coach.services.nutrition_context import build_nutrition_context
    from noam_coach.services.user_state import build_shared_state

    # REC-ARCH-01: one shared snapshot for both the nutrition context and the
    # workout view below, instead of two independent workout-state resolutions.
    shared_state = await build_shared_state(DB, user_id)
    context = await build_nutrition_context(DB, user_id, "post_meal_status", shared_state=shared_state)

    lines = ["נשמר ✅", "הארוחה נוספה ליומן:"]
    lines.append(f"≈{totals['calories']:.0f} קלוריות | ≈{totals['protein']:.0f} גרם חלבון")
    lines.append("")

    now_label = context.current_local_time[11:16] if len(context.current_local_time) >= 16 else context.current_local_time
    lines.append(f"מצב היום עכשיו — {now_label}")
    lines.append(
        f"נאכל עד עכשיו: {context.consumed_calories:.0f} קלוריות | "
        f"{context.consumed_protein:.0f} גרם חלבון"
    )
    if context.remaining_calories is not None and context.remaining_protein is not None:
        lines.append(
            f"נשאר להיום: {context.remaining_calories:.0f} קלוריות | "
            f"{context.remaining_protein:.0f} גרם חלבון"
        )

    try:
        workout_context = await build_workout_nutrition_context(
            DB, user_id, now=shared_state.now, workout_state=shared_state.workout
        )
    except Exception:  # noqa: BLE001 - this short screen must still render on failure
        workout_context = None

    # TASK-22: replace the generic "המשך היום: ארוחה" line with a real
    # chronological timeline for the rest of the day — approximate time, meal
    # role, and calorie/protein allocation per remaining meal, plus the
    # workout event (at its day-specific time) and a valid sleep event. The
    # allocations are recomputed from the CURRENT remaining budget on every
    # call, so each approved meal shrinks the plan.
    from noam_coach.services.next_meal import _hhmm_from_iso

    # TASK-11: build the continuation as (time, text) events and sort them by
    # full clock time. Times are always HH:MM — never a raw ISO/RFC3339 stamp.
    # "99:99" is a sentinel that keeps untimed items at the end, stably.
    events: list[tuple[str, str]] = []
    status_line: str | None = None
    if workout_context is not None:
        # A future workout appears in the timeline at its scheduled time, even
        # when it is many hours away (so it is not the immediate meal context);
        # a completed/uncertain one is surfaced via the status line, not as a
        # future event. TASK-11/12.
        phase = workout_context.workout_phase.value
        future_workout = (
            phase.startswith("pre_workout")
            or (phase == "rest_day" and workout_context.minutes_until_workout not in (None, 0))
        )
        if future_workout:
            workout_hhmm = _hhmm_from_iso(workout_context.planned_workout_start)
            if workout_hhmm:
                events.append((workout_hhmm, f"🏋️ {esc(workout_hhmm)} אימון"))

        for allocation in build_remaining_slot_allocations(workout_context):
            time_hint = allocation.time_hint or ""
            time_part = f"{esc(time_hint)} · " if time_hint else ""
            events.append((
                time_hint or "99:99",
                f"🍽️ {time_part}{esc(allocation.label)}: "
                f"כ-{allocation.calories} קל׳ | כ-{allocation.protein} ג׳ חלבון",
            ))

        status_line = _WORKOUT_STATUS_LINE_BY_PHASE.get(workout_context.workout_phase.value)

        # Only show a concrete bedtime when the sleep time is actually known /
        # confirmed — never present an unconfirmed inference as fact.
        if (
            workout_context.sleep_reference in {"confirmed_fact", "routine_profile"}
            and workout_context.hours_until_bedtime is not None
            and workout_context.hours_until_bedtime > 0
        ):
            bedtime = _bedtime_clock(context.current_local_time, workout_context.hours_until_bedtime)
            if bedtime:
                events.append((bedtime, f"😴 {esc(bedtime)} שינה"))

    if events:
        events.sort(key=lambda item: item[0])
        lines.append("")
        lines.append("המשך היום:")
        lines.extend(text for _time, text in events)
        if status_line:
            lines.append(status_line)

    return "\n".join(lines)


def _bedtime_clock(current_local_time: str, hours_until_bedtime: float) -> str | None:
    """Return an approximate HH:MM bedtime from the current local time plus the
    hours-until-bedtime estimate (TASK-22 timeline sleep event)."""
    from datetime import datetime, timedelta

    try:
        now = datetime.fromisoformat(current_local_time)
    except (TypeError, ValueError):
        return None
    bedtime = now + timedelta(hours=hours_until_bedtime)
    return bedtime.strftime("%H:%M")


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
