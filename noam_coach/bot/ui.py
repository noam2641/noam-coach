# ruff: noqa: F401, F811, F821, I001
"""Telegram keyboards, rendering helpers and session callback encoding.

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
from noam_coach.services.telegram_errors import is_stale_callback_error, is_stale_edit_error

RUNTIME_NAMES = ('Any', 'BadRequest', 'DB', 'Exception', 'InlineKeyboardButton', 'InlineKeyboardMarkup', 'LOGGER', 'PLANS', 'ParseMode', 'SETTINGS', 'TZ', 'ValueError', '_is_valid_public_url', '_resolve_home_action', 'action', 'active_constraints', 'assignments_sql', 'banner', 'bool', 'button', 'c', 'candidate', 'changed', 'coach_intelligence', 'code', 'constraint_banner', 'constraints', 'consumed_cal', 'consumed_prot', 'cycle', 'data', 'datetime', 'dict', 'done_today', 'done_today_rows', 'end', 'enumerate', 'esc', 'exc', 'exercise_data', 'exercise_index', 'exercise_params_keyboard', 'extra', 'fatigue_banner', 'float', 'frozenset', 'get_user_plan', 'home_keyboard', 'home_keyboard_for_user', 'inc', 'index', 'int', 'keyboard', 'last', 'len', 'lines', 'list', 'muscle', 'muscle_line', 'muscle_tag', 'nxt', 'parameters', 'parts', 'plan', 'position', 'query', 'r', 'range', 'recommend_load', 'reps', 'rows', 's', 'safe_edit', 'session', 'sessions', 'spots', 'start', 'str', 'text', 'today_bounds_utc', 'today_consumed', 'today_wd', 'tuple', 'user_id', 'user_model', 'value', 'weight', 'where', 'workout_overview_keyboard')


@runtime_bound(RUNTIME_NAMES)
def button(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data)


@runtime_bound(RUNTIME_NAMES)
def session_action_data(
    action: str,
    session: dict[str, Any],
    *extra: Any,
) -> str:
    parts = [
        action,
        str(session["id"]),
        str(session["exercise_index"]),
        str(session["set_number"]),
    ]
    parts.extend(str(value) for value in extra)
    return ":".join(parts)


SESSION_SCOPED_ACTIONS = frozenset(
    {
        "setok",
        "different",
        "split",
        "splitw",
        "splitr",
        "splitrir",
        "weight",
        "reps",
        "rir",
        "occupied",
        "loadwhy",
        "sub",
        "pain",
        "painloc",
        "painlevel",
        "skip",
        "finish",
        "wpause",
        "wdone",
        "wcancel",
        "restadd",
        "ready",
    }
)


@runtime_bound(RUNTIME_NAMES)
def is_current_session_step(
    parts: list[str],
    session: dict[str, Any],
) -> bool:
    """Reject malformed and stale workout callbacks by default."""
    if len(parts) < 4:
        return False
    if not (parts[2].isdigit() and parts[3].isdigit()):
        return False
    return int(parts[2]) == int(session["exercise_index"]) and int(parts[3]) == int(
        session["set_number"]
    )


@runtime_bound(RUNTIME_NAMES)
def session_action_arg(parts: list[str], index: int = 0) -> str:
    position = 4 + index
    if position >= len(parts):
        raise ValueError("Workout callback is missing an argument")
    return parts[position]


@runtime_bound(RUNTIME_NAMES)
async def update_session_step(
    session: dict[str, Any],
    assignments_sql: str,
    parameters: tuple[Any, ...] = (),
) -> bool:
    """Apply a session mutation only if the displayed step is still current."""
    changed = await DB.execute_rowcount(
        f"""
        UPDATE sessions
        SET {assignments_sql}
        WHERE id=?
          AND status='active'
          AND exercise_index=?
          AND set_number=?
        """,
        (
            *parameters,
            session["id"],
            session["exercise_index"],
            session["set_number"],
        ),
    )
    return changed == 1


@runtime_bound(RUNTIME_NAMES)
def home_keyboard() -> InlineKeyboardMarkup:
    # TASK-6: a focused personal-coach home menu, not a control panel. Only the
    # day-to-day actions are top-level; secondary/system actions live behind
    # "⚙️ הגדרות ועוד". "תפריט להיום" is reached via the plan and the primary
    # real-time action is "מה לאכול עכשיו"; the daily summary is mainly an
    # automatic scheduled message, so neither is a top-level button anymore.
    rows = [
        [
            button("🍽️ מה לאכול עכשיו", "menu:nextmeal"),
            button("🏋️ אימון", "menu:workout"),
        ],
        [
            button("📊 מצב היום", "menu:status"),
            button("📅 התוכנית שלי", "menu:smartplan"),
        ],
        [
            button("☀️ עדכון בוקר", "menu:morning"),
            button("📝 עדכונים", "menu:flags"),
        ],
        [
            button("⚙️ הגדרות ועוד", "menu:settings"),
        ],
    ]
    return InlineKeyboardMarkup(rows)


@runtime_bound(RUNTIME_NAMES)
def settings_keyboard() -> InlineKeyboardMarkup:
    """TASK-6: the secondary/system actions moved off the primary home menu.

    Every callback route is preserved — the actions are relocated, not removed.
    """
    rows = [
        [
            # TASK-7: no standalone "🎯 יעד" entry point — the goal is the
            # first step of "Complete now" (planv2:complete_missing), never a
            # separate action a user can jump to independently of that flow.
            # menu:profile still surfaces the current goal for reference.
            button("👤 הפרופיל שלי", "menu:profile"),
        ],
        [
            button("🍽️ תפריט להיום", "menu:daily_menu"),
            button("🗓️ שבועי", "menu:weekly"),
        ],
        [
            button("📈 גרף", "menu:chart"),
            button("🌙 סיכום יומי", "menu:evening"),
        ],
        [
            button("⌚ Apple Health", "menu:health"),
        ],
    ]
    bottom = [button("ℹ️ אודות ופרטיות", "menu:about")]
    if _is_valid_public_url(SETTINGS.public_base_url or ""):
        bottom.insert(0, button("📱 Mini App", "menu:app"))
    rows.append(bottom)
    rows.append([button("⬅️ תפריט", "menu:home")])
    return InlineKeyboardMarkup(rows)


def focused_onboarding_keyboard() -> InlineKeyboardMarkup:
    """Small home keyboard for users who still need plan/profile answers."""
    return InlineKeyboardMarkup(
        [
            [button("🎯 השלם את התוכנית שלי", "planv2:complete_missing")],
            [
                button("➡️ מה לאכול עכשיו", "menu:nextmeal"),
                button("🍽️ רשום ארוחה", "menu:food"),
            ],
            [button("📊 מצב היום", "menu:status")],
        ]
    )


def _planning_missing_keys(readiness: dict[str, dict[str, Any]]) -> list[str]:
    keys: list[str] = []
    for profile_name in ("workout", "nutrition", "safety"):
        for key in readiness.get(profile_name, {}).get("missing", []):
            if key not in keys:
                keys.append(key)
    return keys


@runtime_bound(RUNTIME_NAMES)
async def _resolve_home_action(user_id: int) -> "coach_intelligence.NextAction | None":
    """Return the next best action for the home screen, or None on failure.

    Shared by _home_hint and home_keyboard_for_user so next_best_action is
    only called once per home-screen render.  Best-effort: returns None when
    any upstream call fails.
    """
    try:
        import onboarding as onb_mod
        if await onb_mod.is_onboarding(DB, user_id):
            return None  # during onboarding the standard action logic is n/a

        readiness = await user_model.compute_all_readiness(DB, user_id)
        workout_r = readiness.get("workout", {})
        nutrition_r = readiness.get("nutrition", {})
        if not workout_r.get("ready") or not nutrition_r.get("ready"):
            return None  # profile-incomplete branch handled separately in hint

        consumed_cal, consumed_prot = await today_consumed(user_id)
        return await coach_intelligence.next_best_action(
            DB,
            user_id,
            consumed_calories=consumed_cal,
            consumed_protein=consumed_prot,
            now_local_hour=datetime.now(TZ).hour,
        )
    except Exception:  # noqa: BLE001
        LOGGER.warning("_resolve_home_action failed for user %s", user_id, exc_info=True)
        return None


@runtime_bound(RUNTIME_NAMES)
async def _home_hint(user_id: int) -> str:
    """A short 'recommended next action' line for the home screen (P1).

    Best-effort: any failure degrades to a neutral prompt so the menu always
    renders.  REC-ONBOARD-02-14: when the primary next action is to complete
    the profile, surface that directly.
    """
    try:
        # Check if onboarding is still active
        import onboarding as onb_mod
        if await onb_mod.is_onboarding(DB, user_id):
            return (
                "👉 <b>הפעולה הבאה:</b> להשלים את ההיכרות.\n\n"
                "אפשר גם לשלוח תמונת אוכל בכל שלב."
            )

        # Check if profile is incomplete for critical decisions
        readiness = await user_model.compute_all_readiness(DB, user_id)
        missing_keys = _planning_missing_keys(readiness)
        if missing_keys:
            missing_count = len(missing_keys)
            if missing_count > 0:
                return (
                    "👉 <b>השלב הבא:</b> להשלים את הפרטים שנשארו כדי לבנות "
                    "את תוכנית האימונים והתזונה שלך.\n\n"
                    f"נשארו לך {missing_count} פרטים להשלמה.\n\n"
                    "כדי להשלים את הפרופיל, לחץ על <b>🎯 השלם את התוכנית שלי</b> "
                    "והמשך לענות על השאלות שנותרו.\n\n"
                    "אפשר לדלג על שאלה ספציפית ולחזור אליה בהמשך. אחרי שהמידע "
                    "יהיה שלם, אשתמש בנתוני HealthKit שאושרו ובתשובות שלך כדי "
                    "לבנות את התוכנית."
                )

        action = await _resolve_home_action(user_id)
        if action and action.title:
            return f"👉 <b>הפעולה הבאה:</b> {esc(action.title)}\n\nשלח תמונת אוכל או בחר פעולה."
    except Exception:  # noqa: BLE001 - the menu must always render
        LOGGER.warning("home hint failed for user %s", user_id, exc_info=True)
    return "שלח תמונת אוכל או בחר פעולה."


@runtime_bound(RUNTIME_NAMES)
async def home_keyboard_for_user(user_id: int) -> InlineKeyboardMarkup:
    """Return the home keyboard with a next-action button prepended when available.

    REC-PLAN-MEAL-03-16: the main menu must include a button whose callback
    matches the next_best_action callback so the user can act on the hint.
    Falls back to the static home_keyboard() on any failure.
    """
    try:
        readiness = await user_model.compute_all_readiness(DB, user_id)
        if _planning_missing_keys(readiness):
            return focused_onboarding_keyboard()
        action = await _resolve_home_action(user_id)
        if action and action.callback and action.title:
            next_row = [button(f"👉 {action.title}", action.callback)]
            base_rows = list(home_keyboard().inline_keyboard)
            return InlineKeyboardMarkup([next_row] + base_rows)
    except Exception:  # noqa: BLE001 - the menu must always render
        LOGGER.warning("home_keyboard_for_user failed for user %s", user_id, exc_info=True)
    return home_keyboard()


@runtime_bound(RUNTIME_NAMES)
def more_keyboard() -> InlineKeyboardMarkup:
    """Deprecated (RE14): there is one single menu now.

    Kept only so old messages whose buttons still carry ``menu:more`` (and
    any legacy caller) render the same unified menu instead of a second
    menu type.
    """
    return home_keyboard()


@runtime_bound(RUNTIME_NAMES)
def plans_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [button("A — חזה", "workout:A"), button("B — גב", "workout:B")],
            [
                button("C — כתפיים ורגליים", "workout:C"),
                button("אימון גוף מלא", "workout:F"),
            ],
            [button("⬅️ תפריט", "menu:home")],
        ]
    )


@runtime_bound(RUNTIME_NAMES)
def onboarding_frequency_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                button("1", "plan:set:1"),
                button("2", "plan:set:2"),
                button("3", "plan:set:3"),
            ],
            [
                button("4", "plan:set:4"),
                button("5", "plan:set:5"),
                button("6", "plan:set:6"),
            ],
            [button("🤔 תמליץ לי", "plan:recommend")],
            [button("⬅️ תפריט", "menu:home")],
        ]
    )


@runtime_bound(RUNTIME_NAMES)
def workout_overview_keyboard(code: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [button("✅ התחל אימון", f"startworkout:{code}")],
            [button("⚙️ ערוך פרמטרים", f"editparams_menu:{code}")],
            [button("⬅️ חזרה", "menu:workout")],
        ]
    )


@runtime_bound(RUNTIME_NAMES)
def exercise_picker_keyboard(code: str) -> InlineKeyboardMarkup:
    plan = PLANS[code]
    rows = [
        [button(exercise_data["name"], f"editparams:{code}:{index}")]
        for index, exercise_data in enumerate(plan["exercises"])
    ]
    rows.append([button("⬅️ חזרה לאימון", f"workout:{code}")])
    return InlineKeyboardMarkup(rows)


@runtime_bound(RUNTIME_NAMES)
def exercise_params_keyboard(code: str, exercise_index: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [button("✅ אישור", f"workout:{code}")],
            [button("❌ ביטול", f"workout:{code}")],
            [button("⬅️ חזרה", f"editparams_menu:{code}")],
        ]
    )


@runtime_bound(RUNTIME_NAMES)
async def select_todays_workout_code(user_id: int) -> str | None:
    """Pick the workout to show when the user taps 'אימון'.

    Preference (per product): the session scheduled for *today* in the active
    weekly plan; otherwise the next one in the plan's cycle after the last
    workout actually performed (A→B→C→A...). Returns None if no active plan.

    REC-ARCH-01 audit note: this answers a question orthogonal to
    ``user_state.resolve_workout_state``'s phase resolution — "which A/B/C
    code is today's split" (a per-CODE completion set + cycle-position
    question), not "what's the current phase of today's workout". It reads
    ``active_workout_plan`` from the ``user_model`` fact mirror (a different
    source than ``planning.get_active_plan`` used by the shared resolver) and
    needs a *set* of which codes were already done today, which the shared
    resolver's single latest-session view does not expose. Left as its own
    narrow helper rather than forced through the shared resolver, which would
    lose the per-code granularity this needs without replacing anything it
    does. Not itself a source of workout-phase disagreement with nutrition
    (it never claims "the workout is upcoming/in-progress/done" — only "here
    is which code to show").
    """
    plan = await user_model.get_value(DB, user_id, "active_workout_plan")
    if not plan or not plan.get("sessions"):
        return None
    sessions = plan["sessions"]
    cycle = [s["code"] for s in sessions]

    # Which codes were already completed today? Don't offer those again.
    start, end = daily_state.local_day_bounds_utc()
    done_today_rows = await DB.fetch_all(
        "SELECT DISTINCT code FROM sessions WHERE user_id=? "
        "AND status IN ('completed','partial') AND ended_at>=? AND ended_at<?",
        (user_id, start, end),
    )
    done_today = {r["code"] for r in done_today_rows}

    # 1) A session scheduled for today's weekday that wasn't done yet.
    today_wd = datetime.now(TZ).weekday()
    for session in sessions:
        if session.get("weekday") == today_wd and session["code"] not in done_today:
            return session["code"]

    # 2) Otherwise the next code in the cycle after the last performed workout.
    last = await DB.fetch_one(
        "SELECT code FROM sessions WHERE user_id=? "
        "AND status IN ('completed','partial') "
        "ORDER BY ended_at DESC LIMIT 1",
        (user_id,),
    )
    if last and last["code"] in cycle:
        nxt = (cycle.index(last["code"]) + 1) % len(cycle)
        candidate = cycle[nxt]
        # Skip forward over anything already done today.
        for _ in range(len(cycle)):
            if candidate not in done_today:
                return candidate
            nxt = (nxt + 1) % len(cycle)
            candidate = cycle[nxt]
        return candidate
    # Fall back to the first code not yet done today.
    for code in cycle:
        if code not in done_today:
            return code
    return None


@runtime_bound(RUNTIME_NAMES)
async def render_workout_overview(
    query: Any,
    user_id: int,
    code: str,
) -> None:
    """Short brief of the upcoming workout: type, exercises + quantities,
    then a Start button."""
    plan = await get_user_plan(user_id, code)
    lines: list[str] = []

    for index, exercise_data in enumerate(plan["exercises"], start=1):
        weight, reps, _ = await recommend_load(user_id, exercise_data)
        muscle = exercise_data.get("muscle")
        muscle_tag = f" <i>({muscle})</i>" if muscle else ""
        lines.append(
            f"{index}. <b>{exercise_data['name']}</b>{muscle_tag} — "
            f"{exercise_data['sets']}×{reps} במשקל {weight:g} ק״ג"
        )

    banner = await constraint_banner(user_id)
    banner += await fatigue_banner(user_id)
    text = f"<b>{plan['name']}</b>\n\n" + banner + "\n".join(lines) + "\n\nמוכן? לחץ <b>התחל</b>."
    await safe_edit(query, text, workout_overview_keyboard(code))


@runtime_bound(RUNTIME_NAMES)
async def constraint_banner(user_id: int) -> str:
    """A short, non-diagnostic heads-up when active constraints exist.

    Pain locations are mapped through pain_region_label so a raw English
    token from an in-workout report ("elbow") or Hebrew free text from
    onboarding ("טניס אלכן") both render as one clean Hebrew word ("מרפק"),
    instead of showing whatever mixed text was stored verbatim.
    """
    constraints = await active_constraints(user_id)
    if not constraints:
        return ""
    regions = training_intelligence.active_pain_regions(constraints)
    if regions:
        spots = [esc(region.label) for region in regions.values()]
    else:
        # No recognized region token (e.g. a medical_avoidance constraint,
        # or free text that doesn't match any known region) — fall back to
        # the raw location so the banner still says *something* concrete.
        spots = [esc(c["location"]) for c in constraints if c.get("location")]
    where = f" ({', '.join(spots)})" if spots else ""
    return (
        f"⚠️ <b>שים לב</b>: רשומה אצלי מגבלה פעילה{where}. "
        'אם תרגיל מכאיב — דלג עליו או החלף לחלופה דרך "ערוך פרמטרים". '
        "אם הכאב חד או מתגבר, עצור ופנה לבדיקה.\n\n"
    )


@runtime_bound(RUNTIME_NAMES)
async def render_exercise_params(
    query: Any,
    user_id: int,
    code: str,
    exercise_index: int,
) -> None:
    plan = await get_user_plan(user_id, code)
    exercise_data = plan["exercises"][exercise_index]
    muscle = exercise_data.get("muscle")
    muscle_line = f"שריר מטרה: <b>{muscle}</b>\n" if muscle else ""
    text = (
        f"<b>עריכת פרמטרים</b>\n\n"
        f"תרגיל: <b>{exercise_data['name']}</b>\n"
        f"{muscle_line}\n"
        f"משקל מתוכנן: <b>{exercise_data['weight']:g} ק״ג</b>\n"
        f"סטים: <b>{exercise_data['sets']}</b>\n"
        f"טווח חזרות: <b>{exercise_data['rmin']}–{exercise_data['rmax']}</b>\n"
        f"מנוחה: <b>{exercise_data['rest'] // 60}:{exercise_data['rest'] % 60:02d}</b>\n"
        f"מדרגת התקדמות: <b>{exercise_data['inc']:g}</b>\n\n"
        "כתוב את השינוי, למשל:\n"
        "\"משקל 22.5\"\n"
        "\"4 סטים\"\n"
        "\"8-12 חזרות\"\n"
        "\"מנוחה 1:30\"\n"
        "\"מנוחה 1:30 לכל התרגילים\""
    )
    await conversation.set_active_flow(
        DB,
        user_id,
        conversation.FlowName.workout_parameter_edit,
        step="awaiting_text",
        payload={"code": code, "exercise_index": exercise_index},
    )
    await safe_edit(query, text, exercise_params_keyboard(code, exercise_index))


@runtime_bound(RUNTIME_NAMES)
async def safe_edit(
    query: Any,
    text: str,
    keyboard: InlineKeyboardMarkup | None = None,
) -> None:
    try:
        await query.edit_message_text(
            text,
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
        )
    except BadRequest as exc:
        if is_stale_edit_error(exc):
            message = getattr(query, "message", None)
            if message is not None and hasattr(message, "reply_text"):
                with suppress(Exception):
                    await message.reply_text(
                        text,
                        reply_markup=keyboard,
                        parse_mode=ParseMode.HTML,
                    )
            return
        if "not modified" not in str(exc).lower():
            raise


async def safe_answer_callback(
    query: Any,
    text: str | None = None,
    *,
    show_alert: bool = False,
) -> bool:
    """ACK a Telegram callback without failing stale-button flows.

    Telegram rejects callback-query answers after a short TTL. Those errors are
    expected when a user taps an old inline keyboard, so they should be logged
    by the router at most, not shown as a Python failure to the user.
    """
    try:
        await query.answer(text=text, show_alert=show_alert)
        return True
    except BadRequest as exc:
        if is_stale_callback_error(exc):
            LOGGER.info("Ignoring stale callback ACK: %s", exc)
            return False
        raise
