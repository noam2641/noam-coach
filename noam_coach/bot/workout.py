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
from noam_coach.services import daily_state, workout_slots

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

RUNTIME_NAMES = ('Any', 'DB', 'Exception', 'InlineKeyboardMarkup', 'KeyError', 'StopIteration', 'TypeError', 'ValueError', 'WEIGHT_TEXT_STEP', '_StaleSetStep', '_rir_known', 'abs', 'advance_sql', 'avg7', 'await_weight_text', 'bool', 'button', 'cal_line', 'cal_remaining', 'calories', 'clear_weight_text_flow', 'client_event_id', 'completed', 'conn', 'conversation', 'cue', 'cues', 'cur', 'current', 'cursor', 'datetime', 'day', 'dict', 'duration', 'end', 'ended', 'enumerate', 'esc', 'ex', 'exercise_index', 'explanation', 'fetch_goal', 'float', 'goal', 'goal_note', 'handle_weight_text', 'header', 'home_keyboard', 'i', 'idx', 'int', 'json', 'keyboard', 'last', 'last_steps', 'len', 'lines', 'list', 'm', 'meal_count', 'meals', 'muscle', 'muscle_line', 'next', 'plan', 'planned_sets', 'prev', 'prev_line', 'prev_rir', 'prev_rir_label', 'previous_weight_context', 'prot_line', 'prot_remaining', 'protein', 'provisional', 'query', 'recommend_load', 'record_load_type_hint', 'reps', 'reps_prompt_keyboard', 'rest_line', 'rest_seconds', 'rir', 'rir_line', 'round', 'row', 'rows', 'safe_edit', 'save_set', 'session', 'session_action_data', 'session_id', 'set_count', 'set_no', 'source', 'start', 'started', 'status', 'stored_load_type', 'str', 'sum', 'suppress', 'target_rir', 'text', 'timedelta', 'timezone', 'today_bounds_utc', 'today_consumed', 'today_meals', 'tuple', 'update_session_step', 'user_id', 'user_model', 'utc_now', 'volume', 'weight', 'why_line', 'wline')


@runtime_bound(RUNTIME_NAMES)
async def today_meals(user_id: int, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Return today's saved meals (the rows that make up the daily total).

    ``now`` pins the local-day window to one explicit instant (injectable for
    tests); ``None`` keeps the default real-clock behavior.
    """
    return await daily_state.consumed_meals(DB, user_id, now=now)


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
async def build_daily_status(user_id: int, *, now: datetime | None = None) -> str:
    """RE10-13: rich "מצב היום" built from the single nutrition-context source
    of truth (D4/D9/D10), with NO stale Apple Health activity section (that
    freshness warning belongs to the Health screens, not here).

    ``now`` is the single instant the whole dashboard is anchored to — the
    meal window, the shared workout/nutrition state, and every projection
    derived from it all see the same effective day. ``None`` (every
    production call site) means the real current time.
    """
    from noam_coach.services.next_meal import (
        build_workout_nutrition_context,
        generate_next_meal_recommendation,
    )
    from noam_coach.services.nutrition_context import build_nutrition_context
    from noam_coach.services.next_meal import build_remaining_slot_allocations
    from noam_coach.services.user_state import build_shared_state

    goal = await fetch_goal(user_id)
    meals = await today_meals(user_id, now=now)
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
    shared_state = await build_shared_state(DB, user_id, now=now)
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
            format_remaining_budget_line(
                context.remaining_calories, context.remaining_protein
            )
        )

    try:
        workout_context = await build_workout_nutrition_context(
            DB, user_id, now=shared_state.now, workout_state=shared_state.workout
        )
    except Exception:  # noqa: BLE001 - this short screen must still render on failure
        workout_context = None

    # TASK-59: ONE chronological remaining-day timeline, built by the shared
    # day_timeline service (the same semantics the next-meal detail view
    # uses — never reconstructed per surface). Every meal slot carries a
    # concrete time and its ALLOCATED share of the remaining budget; ordering
    # is minutes-from-now, so an after-midnight bedtime closes the day; B12
    # planned meals appear until their lifecycle says consumed/expired; and
    # no prose is rendered under the timeline — the timeline IS the state
    # (a future workout shows as its own event, so the old
    # "יש אימון מתוכנן היום, עדיין לפניו" line is gone; the status line
    # remains only when there is no timeline to speak for the day).
    if workout_context is not None:
        from noam_coach.services.day_timeline import (
            active_planned_meals,
            build_remaining_day_events,
            format_remaining_day_lines,
        )

        planned_meals = await active_planned_meals(DB, user_id, shared_state.now)
        events = build_remaining_day_events(
            workout_context, planned_meals=planned_meals, now=shared_state.now
        )
        timeline_lines = format_remaining_day_lines(events)
        if timeline_lines:
            lines.append("")
            lines.extend(timeline_lines)
        else:
            status_line = _WORKOUT_STATUS_LINE_BY_PHASE.get(workout_context.workout_phase.value)
            if status_line:
                lines.append("")
                lines.append(status_line)

    return "\n".join(lines)


def format_remaining_budget_line(remaining_calories: float, remaining_protein: float) -> str:
    """The post-meal 'נשאר להיום' line (review 2026-07-18_1 / F-09).

    An overshoot is phrased as חריגה — the same convention the status
    screen uses — never as a raw negative remainder ("-5 גרם חלבון"
    reads as a bug to the user; production event 598).
    """
    def part(value: float, unit: str) -> str:
        if value >= 0:
            return f"{value:.0f} {unit}"
        return f"חריגה של {-value:.0f} {unit}"

    return (
        f"נשאר להיום: {part(remaining_calories, 'קלוריות')} | "
        f"{part(remaining_protein, 'גרם חלבון')}"
    )


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
    # Imported directly rather than resolved through `runtime_bound`: A12
    # measured that the facade rebind is not reliable for names a handler adds
    # (`_substitution_callback` raised NameError under it), and an A13 failure
    # here would surface as a broken workout card.
    from noam_coach.bot.ui import safe_edit_delivered
    from noam_coach.services.training import (
        LOAD_CHANNEL_TELEGRAM,
        recommend_load_decision,
        record_load_decision,
    )

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
    load_decision = await recommend_load_decision(user_id, current)
    weight, reps, explanation = load_decision.to_tuple()

    # A typed value OVERRIDES the recommendation on the card. When it does, the
    # user is acting on their own number, not on ours, so there is no
    # recommendation-acted-on to record (A13).
    recommendation_presented = True
    if session["pending_weight"] is not None:
        weight = float(session["pending_weight"])
        recommendation_presented = False
    if session["pending_reps"] is not None:
        reps = int(session["pending_reps"])
        recommendation_presented = False

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
    #
    # Deliberately NOT routed through training.select_exercise_history: that
    # resolver answers "which SESSIONS inform the load", while this line needs
    # "the single most recent SET, excluding the current session". Same table,
    # different question. What they must share -- and now do -- is the identity
    # they key on: the canonical exercise id. When per-machine identity lands,
    # this query gains the same implementation filter and both stay in
    # agreement; until then there is only one layer and they cannot disagree.
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
    delivered = await safe_edit_delivered(query, text, keyboard)

    # A13 — AFTER the card is on screen, never before. This await touches the
    # database; in front of the render it would put recording latency between
    # the user's tap and the card they are waiting for. Recording is
    # best-effort and cannot raise (see `record_load_decision`), so the card
    # stands whatever happens here.
    #
    # `delivered` is load-bearing, not defensive. `safe_edit` swallows a failed
    # stale-message fallback, so without this the edit could fail, the fallback
    # could fail, the user could see NOTHING, and A13 would still record that a
    # recommendation was presented -- the audit row would assert something that
    # never happened, which is precisely the evidence a weight dispute relies on.
    if recommendation_presented and delivered:
        await record_load_decision(
            user_id,
            load_decision,
            exercise_id=str(current.get("id") or ""),
            session_id=session["id"],
            set_number=session["set_number"],
            channel=LOAD_CHANNEL_TELEGRAM,
        )


# ---------------------------------------------------------------------------
# TASK-WORKOUT-WEIGHT-TEXT — typed weight reporting during an active workout.
#
# The predefined weight-SELECTION buttons are gone; the user types the load.
# The three pieces below are the whole contract:
#
#   await_weight_text()        arms FlowName.workout_session for exactly the
#                              current (session, exercise_index, set_number)
#   previous_weight_context()  the optional "בסט הקודם" context line
#   handle_weight_text()       parses one typed answer and either advances to
#                              the EXISTING next step (reps) or re-asks
#
# Load type without a migration: sets.weight stays a bare REAL and receives the
# same canonical number the buttons produced. The per-hand / bodyweight HINT
# rides in the schemaless sessions.plan JSON (per exercise index), so history
# keeps its exact historical meaning and no schema change is required.
# ---------------------------------------------------------------------------

WEIGHT_TEXT_STEP = "await_weight"


def _weight_load_type_key(exercise_index: int, set_number: int) -> str:
    return f"{int(exercise_index)}:{int(set_number)}"


@runtime_bound(RUNTIME_NAMES)
async def await_weight_text(user_id: int, session: dict[str, Any]) -> None:
    """Arm the free-text weight step for THIS session/exercise/set.

    The payload carries the full step identity. The text consumer re-reads the
    live session row and refuses to act when the identity no longer matches, so
    a message typed against a stale prompt cannot write to a different set.
    """
    await conversation.set_active_flow(
        DB,
        user_id,
        conversation.FlowName.workout_session,
        step=WEIGHT_TEXT_STEP,
        payload={
            "session_id": int(session["id"]),
            "exercise_index": int(session["exercise_index"]),
            "set_number": int(session["set_number"]),
        },
    )


@runtime_bound(RUNTIME_NAMES)
async def clear_weight_text_flow(user_id: int) -> None:
    """Disarm the weight step — always paired with a save, cancel or bail-out.

    Only clears a workout_session flow: a meal or onboarding flow that started
    in the meantime must never be wiped by the workout path.
    """
    flow = await conversation.get_active_flow(DB, user_id)
    if flow.name == conversation.FlowName.workout_session:
        await conversation.clear_active_flow(DB, user_id)


@runtime_bound(RUNTIME_NAMES)
async def stored_load_type(session: dict[str, Any], exercise_index: int, set_number: int) -> str:
    """Load-type hint previously recorded in the plan JSON, if any."""
    try:
        plan = json.loads(session["plan"])
    except (TypeError, json.JSONDecodeError):
        return "total"
    hints = plan.get("load_type_hints")
    if not isinstance(hints, dict):
        return "total"
    value = hints.get(_weight_load_type_key(exercise_index, set_number))
    return value if value in ("total", "per_hand", "bodyweight") else "total"


@runtime_bound(RUNTIME_NAMES)
async def record_load_type_hint(
    session: dict[str, Any],
    exercise_index: int,
    set_number: int,
    load_type: str,
) -> None:
    """Persist the load-type hint into the schemaless sessions.plan JSON.

    Deliberately NOT a migration: sets.weight keeps the same canonical number
    the buttons wrote, and this hint only records how to READ it back
    (per-hand / bodyweight) for confirmation and future prompts. A failure here
    must never break set logging, so the write is best-effort.
    """
    if load_type == "total":
        return
    try:
        plan = json.loads(session["plan"])
    except (TypeError, json.JSONDecodeError):
        return
    hints = plan.get("load_type_hints")
    if not isinstance(hints, dict):
        hints = {}
    hints[_weight_load_type_key(exercise_index, set_number)] = load_type
    plan["load_type_hints"] = hints
    with suppress(Exception):
        await DB.execute(
            "UPDATE sessions SET plan=? WHERE id=?",
            (json.dumps(plan, ensure_ascii=False), session["id"]),
        )


@runtime_bound(RUNTIME_NAMES)
async def previous_weight_context(
    user_id: int,
    session: dict[str, Any],
    current: dict[str, Any],
) -> tuple[float | None, str]:
    """(previous_weight, previous_load_type) for the prompt's context line.

    Prefers the most recent set of THIS exercise in THIS session (the natural
    "previous set"), falling back to the last time the exercise was performed.
    Returns (None, "total") when there is nothing to show — the prompt then
    simply omits the context line and "אותו משקל" is not accepted.

    Like the comparison line in `show_session`, this asks for a single most
    recent SET rather than the sessions that inform a load, so it stays a
    direct query rather than routing through `training.select_exercise_history`.
    Both key on the canonical exercise id, which is what keeps this line and the
    recommendation beside it describing the same exercise.
    """
    row = await DB.fetch_one(
        """
        SELECT s.weight, s.set_number, s.session_id FROM sets s
        JOIN sessions ses ON ses.id = s.session_id
        WHERE ses.user_id=? AND s.exercise_id=?
          AND s.source != 'telegram_split_secondary'
        ORDER BY (s.session_id = ?) DESC, s.id DESC
        LIMIT 1
        """,
        (user_id, current["id"], session["id"]),
    )
    if not row:
        return None, "total"
    load_type = await stored_load_type(
        session, session["exercise_index"], int(row["set_number"])
    )
    return float(row["weight"]), load_type


@runtime_bound(RUNTIME_NAMES)
def reps_prompt_keyboard(session: dict[str, Any], current: dict[str, Any], reps: int) -> Any:
    """The EXISTING next step after a weight is recorded — unchanged order."""
    center = max(current["rmin"], min(current["rmax"], reps))
    choices = list(range(max(1, center - 2), center + 3))
    rows = [
        [button(str(value), session_action_data("reps", session, value)) for value in choices[i : i + 5]]
        for i in range(0, len(choices), 5)
    ]
    return center, InlineKeyboardMarkup(rows)


@runtime_bound(RUNTIME_NAMES)
async def handle_weight_text(update: Any, user_id: int, flow: Any, text: str) -> bool:
    """Consume one typed weight answer.

    Returns True when the message was owned by this step (whether it parsed or
    not). Returns False only when the flow payload is stale/unusable, letting
    the caller fall through to normal routing after clearing the flow.

    Guarantees:
      * a parse failure writes NOTHING and does NOT advance — the same step
        stays armed and the user is re-asked;
      * the live session row is re-read and its (exercise_index, set_number)
        must still equal the payload's, so a duplicate delivery of the same
        message cannot record twice or advance twice;
      * the session's exercise/set/identity are never mutated by invalid input.
    """
    from noam_coach.services import weight_text as weight_text_service

    payload = dict(flow.payload or {})
    session_id = int(payload.get("session_id") or 0)
    if not session_id:
        return False

    session = await DB.fetch_one(
        "SELECT * FROM sessions WHERE id=? AND user_id=?",
        (session_id, user_id),
    )
    if not session or session["status"] != "active":
        return False

    # Step identity: the prompt was armed for one specific set. If the session
    # has since advanced (a duplicate update already saved, or the user tapped
    # something else), this message is stale — drop the step rather than write.
    if int(session["exercise_index"]) != int(payload.get("exercise_index", -1)) or int(
        session["set_number"]
    ) != int(payload.get("set_number", -1)):
        return False

    plan = json.loads(session["plan"])
    current = plan["exercises"][session["exercise_index"]]
    previous_weight, previous_load_type = await previous_weight_context(user_id, session, current)

    report = weight_text_service.parse_weight_text(
        text,
        previous_weight=previous_weight,
        previous_load_type=previous_load_type,
    )
    if report is None:
        # Re-ask IN PLACE: no write, no advance, flow stays armed.
        await update.effective_message.reply_text(
            weight_text_service.INVALID_WEIGHT_TEXT,
            reply_markup=InlineKeyboardMarkup(
                [
                    [button("↩️ חזרה לאימון", session_action_data("ready", session))],
                    [button("סיים", session_action_data("finish", session))],
                ]
            ),
        )
        return True

    # A bodyweight report on an exercise the plan loads is almost always a
    # misparse, not a real set: in the 2026-07-26 session a barbell bench
    # press was persisted at 0.0 kg, and because sets are training history
    # that number then feeds progression for every future session. The plan's
    # own weight is the signal — a genuine bodyweight movement is planned at
    # 0, everything else carries a load — so this rejects only the
    # contradictory case and leaves real bodyweight exercises untouched.
    if report.load_type == "bodyweight" and float(current.get("weight") or 0) > 0:
        await update.effective_message.reply_text(
            weight_text_service.bodyweight_rejected_text(current.get("name") or ""),
            reply_markup=InlineKeyboardMarkup(
                [
                    [button("↩️ חזרה לאימון", session_action_data("ready", session))],
                    [button("סיים", session_action_data("finish", session))],
                ]
            ),
        )
        return True

    # Atomic, optimistic pending write guarded on the same (exercise_index,
    # set_number) — a duplicate delivery of the same text is a no-op here.
    if not await update_session_step(session, "pending_weight=?", (report.weight,)):
        await clear_weight_text_flow(user_id)
        return True

    await record_load_type_hint(
        session, session["exercise_index"], session["set_number"], report.load_type
    )

    _, reps, _ = await recommend_load(user_id, current)
    if session["pending_reps"] is not None:
        reps = int(session["pending_reps"])
    center, keyboard = reps_prompt_keyboard(session, current, reps)

    # The weight is recorded; the step is over. Clearing here is what keeps the
    # free-text grammar from capturing meal/onboarding/general chat afterwards.
    await clear_weight_text_flow(user_id)

    await update.effective_message.reply_text(
        f"{weight_text_service.format_weight_confirmation(report)}\n"
        f"כמה חזרות? (יעד ~{center})",
        reply_markup=keyboard,
    )
    return True


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
                weight, reps, rir, source, client_event_id,
                exercise_index, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                # Where the user actually was. `exercise_id` cannot answer this
                # when a plan programs the same movement twice -- see undo.
                idx,
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
            "SELECT id, exercise_id, set_number, source, exercise_index FROM sets "
            "WHERE session_id=? ORDER BY id DESC LIMIT 1",
            (session_id,),
        )
        last = await cursor.fetchone()
        if not last:
            return False

        # Prefer the position recorded when the set was logged. Reverse-mapping
        # the exercise_id answers "where does this id live now"; the pointer
        # needs "where was the user then". Those differ whenever a plan
        # programs the same movement twice -- next() would return the FIRST
        # match and rewind past work the user had already finished.
        stored_index = last["exercise_index"]
        if stored_index is not None:
            exercise_index = int(stored_index)
        else:
            # Rows written before migration 16 carry NULL and are not
            # recoverable, so they keep the original behaviour: correct
            # whenever the plan holds the exercise exactly once.
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

    # Per-entry rather than all-or-nothing. The previous form summed with a bare
    # `ex["sets"]` inside one try, so a single entry missing the key raised and
    # the except zeroed the count for the WHOLE session -- the user finishes a
    # workout and is told 0 sets were planned. `planned_sets_of` returns 0 for
    # an entry that legitimately contributes none (an unmapped or blocked slot)
    # and for a malformed one, so every well-formed exercise still counts.
    planned_sets = 0
    try:
        plan = json.loads(session["plan"])
        exercises = plan.get("exercises")
        workout_slots.observe_plan_entries(
            exercises,
            user_id=user_id,
            session_id=session_id,
            context="workout_summary",
        )
        planned_sets = sum(
            workout_slots.planned_sets_of(ex) for ex in (exercises or [])
        )
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
