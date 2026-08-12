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

RUNTIME_NAMES = ('Any', 'BadRequest', 'DB', 'Exception', 'InlineKeyboardButton', 'InlineKeyboardMarkup', 'LOGGER', 'PLANS', 'ParseMode', 'SETTINGS', 'TZ', 'ValueError', '_is_valid_public_url', '_resolve_home_action', '_send_new_instead_of_edit', 'action', 'active_constraints', 'assignments_sql', 'banner', 'bool', 'button', 'c', 'candidate', 'changed', 'coach_intelligence', 'code', 'constraint_banner', 'constraints', 'consumed_cal', 'consumed_prot', 'cycle', 'data', 'datetime', 'dict', 'done_today', 'done_today_rows', 'end', 'enumerate', 'esc', 'exc', 'exercise_data', 'exercise_index', 'exercise_params_keyboard', 'extra', 'fatigue_banner', 'float', 'frozenset', 'get_user_plan', 'home_keyboard', 'home_keyboard_for_user', 'inc', 'index', 'int', 'keyboard', 'last', 'len', 'lines', 'list', 'muscle', 'muscle_line', 'muscle_tag', 'nxt', 'parameters', 'parts', 'plan', 'position', 'query', 'r', 'range', 'recommend_load', 'reps', 'rows', 's', 'safe_edit', 'safe_message_edit', 'session', 'sessions', 'spots', 'start', 'str', 'text', 'today_bounds_utc', 'today_consumed', 'today_wd', 'tuple', 'user_id', 'user_model', 'value', 'weight', 'where', 'workout_overview_keyboard')


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


# ---------------------------------------------------------------------------
# Workout selector / overview v2 (workout-selection architecture, Batch 4).
#
# These builders mint ONLY `wk:` callbacks, every one of which has a handler
# in callback_plans.py in this same batch (`wk:list`, `wk:sel`, `wk:fsel`,
# `wk:start`, `wk:fstart`) -- the batch never exposes a dangling button.
# The legacy builders above are deliberately left byte-identical: old
# Telegram messages keep working until the Batch 7 adapter retires them.
# ---------------------------------------------------------------------------


def _wk_ref_suffix(ref: Any) -> str:
    """The `<identity>:<session_index>` tail shared by every `wk:` callback.

    Tier-1 carries the immutable plan_versions id; Tier-2 carries the 8-hex
    content fingerprint (compute_fact_rev). Both are placed as NON-TERMINAL
    segments, so neither can occupy the last-two positions the ARCH-04
    grammar inspects for `^v\\d{1,9}$` / `^ff-\\d+-[0-9a-f]{6,}$`
    (callback_grammar.py:66-72) -- verified by test_callback_grammar_b2.
    """
    identity = ref.plan_id if ref.tier == "plan" else ref.fact_rev
    return f"{identity}:{ref.session_index}"


@runtime_bound(RUNTIME_NAMES)
def wk_select_callback(ref: Any) -> str:
    """`wk:sel:<plan_id>:<sidx>` (Tier-1) / `wk:fsel:<fact_rev>:<sidx>` (Tier-2)."""
    prefix = "wk:sel" if ref.tier == "plan" else "wk:fsel"
    return f"{prefix}:{_wk_ref_suffix(ref)}"


@runtime_bound(RUNTIME_NAMES)
def wk_start_callback(ref: Any) -> str:
    """`wk:start:<plan_id>:<sidx>` (Tier-1) / `wk:fstart:<fact_rev>:<sidx>` (Tier-2)."""
    prefix = "wk:start" if ref.tier == "plan" else "wk:fstart"
    return f"{prefix}:{_wk_ref_suffix(ref)}"


@runtime_bound(RUNTIME_NAMES)
def workout_selector_keyboard(choices: list[Any]) -> InlineKeyboardMarkup:
    """One row per selectable session, labelled with its REAL name (not the
    global template's), ⭐ on the recommended one and ✅ on anything already
    completed today. Recommendation is a hint, never a forced selection --
    every session in the plan is tappable (plan section J).
    """
    rows = []
    for choice in choices:
        label = choice.name
        if choice.recommended:
            label = f"⭐ {label}"
        if choice.done_today:
            label = f"{label} ✅"
        rows.append([button(label, wk_select_callback(choice.ref))])
    rows.append([button("⬅️ תפריט", "menu:home")])
    return InlineKeyboardMarkup(rows)


@runtime_bound(RUNTIME_NAMES)
def workout_overview_keyboard_v2(ref: Any, *, single_choice: bool = False) -> InlineKeyboardMarkup:
    """Overview keyboard whose Start button carries the SAME identity the
    overview was rendered from -- display identity == start identity, the
    W2 defect this batch closes.

    Batch 6 restored parameter editing here as `wk:exm`, carrying the same
    identity rather than the ambiguous bare `editparams_menu:<code>` route
    (which stays live only for old Telegram messages until Batch 7).
    """
    rows = [
        [button("✅ התחל אימון", wk_start_callback(ref))],
        [button("⚙️ ערוך פרמטרים", wk_exercise_menu_callback(ref))],
    ]
    # With only one selectable session there is no selector to go back TO;
    # Back goes home instead of to a one-row list (plan section J).
    rows.append([button("⬅️ חזרה", "menu:home" if single_choice else "wk:list")])
    return InlineKeyboardMarkup(rows)


@runtime_bound(RUNTIME_NAMES)
async def render_workout_overview_v2(query: Any, user_id: int, resolved: Any, *, single_choice: bool = False) -> None:
    """Overview for a catalog-resolved session (Tier-1 personalized payload
    or Tier-2 template-seeded content), replacing the template-only
    render_workout_overview on every `wk:` route."""
    text = await build_workout_overview_text_v2(user_id, resolved)
    await safe_edit(query, text, workout_overview_keyboard_v2(resolved.choice.ref, single_choice=single_choice))


@runtime_bound(RUNTIME_NAMES)
async def build_workout_overview_text_v2(user_id: int, resolved: Any) -> str:
    """Shared overview body for both surfaces (callback overview and the
    assistant's reply-message overview), so the two can never drift apart.
    Mirrors render_workout_overview's existing format exactly; the only
    difference is WHERE the exercises come from (the resolved session, not
    PLANS[code]).
    """
    lines: list[str] = []
    for index, exercise_data in enumerate(resolved.session.get("exercises", []), start=1):
        weight, reps, _ = await recommend_load(user_id, exercise_data)
        muscle = exercise_data.get("muscle")
        muscle_tag = f" <i>({muscle})</i>" if muscle else ""
        lines.append(
            f"{index}. <b>{esc(exercise_data['name'])}</b>{muscle_tag} — "
            f"{exercise_data['sets']}×{reps} במשקל {weight:g} ק״ג"
        )
    banner = await constraint_banner(user_id)
    banner += await fatigue_banner(user_id)
    if resolved.choice.done_today:
        # E2 (plan section F): an honest "already trained" note. Batch 5 adds
        # the `:again` repeat-confirmation gate; this batch only informs.
        banner += "כבר ביצעת את האימון הזה היום ✅\n\n"
    return (
        f"<b>{esc(resolved.choice.name)}</b>\n\n" + banner + "\n".join(lines) + "\n\nמוכן? לחץ <b>התחל</b>."
    )


@runtime_bound(RUNTIME_NAMES)
def wk_exercise_menu_callback(ref: Any) -> str:
    """`wk:exm:<identity>:<sidx>` -- open the exercise picker for a resolved
    session, carrying that session's identity rather than a bare code."""
    return f"wk:exm:{_wk_ref_suffix(ref)}"


@runtime_bound(RUNTIME_NAMES)
def wk_exercise_callback(ref: Any, exercise_index: int) -> str:
    """`wk:ex:<identity>:<sidx>:<exercise_index>`."""
    return f"wk:ex:{_wk_ref_suffix(ref)}:{exercise_index}"


@runtime_bound(RUNTIME_NAMES)
def wk_param_callback(ref: Any, exercise_index: int, field: str, delta: float) -> str:
    """`wk:par:<identity>:<sidx>:<exercise_index>:<field>:<delta>` -- the
    longest callback this architecture mints. Budget checked by
    tests/test_workout_param_edit_v2.py (worst case 38/64 bytes, plan
    section N)."""
    return f"wk:par:{_wk_ref_suffix(ref)}:{exercise_index}:{field}:{delta:g}"


@runtime_bound(RUNTIME_NAMES)
def exercise_picker_keyboard_v2(resolved: Any) -> InlineKeyboardMarkup:
    """Pick an exercise to edit WITHIN the selected session (Batch 6).

    Rows come from the resolved session's own normalized exercises -- the
    personalized payload for Tier-1, the template-seeded content for Tier-2 --
    so what the user edits is exactly what they saw and what Start will use.
    The legacy builder above (keyed on a bare PLANS code) stays untouched for
    old Telegram messages until Batch 7.
    """
    ref = resolved.choice.ref
    rows = [
        [button(exercise.get("name") or exercise.get("id") or f"#{index + 1}",
                wk_exercise_callback(ref, index))]
        for index, exercise in enumerate(resolved.session.get("exercises", []))
    ]
    rows.append([button("⬅️ חזרה לאימון", wk_select_callback(ref))])
    return InlineKeyboardMarkup(rows)


@runtime_bound(RUNTIME_NAMES)
def exercise_params_keyboard_v2(ref: Any, exercise_index: int) -> InlineKeyboardMarkup:
    """Stepper keyboard for one exercise inside one identified session."""
    return InlineKeyboardMarkup([
        [
            button("➖ 2.5 ק״ג", wk_param_callback(ref, exercise_index, "weight", -2.5)),
            button("➕ 2.5 ק״ג", wk_param_callback(ref, exercise_index, "weight", 2.5)),
        ],
        [
            button("➖ סט", wk_param_callback(ref, exercise_index, "sets", -1)),
            button("➕ סט", wk_param_callback(ref, exercise_index, "sets", 1)),
        ],
        [
            button("➖ מנוחה", wk_param_callback(ref, exercise_index, "rest", -15)),
            button("➕ מנוחה", wk_param_callback(ref, exercise_index, "rest", 15)),
        ],
        [button("⬅️ חזרה", wk_exercise_menu_callback(ref))],
        [button("❌ ביטול", "wparamtext:cancel")],
    ])


@runtime_bound(RUNTIME_NAMES)
async def render_exercise_params_v2(query: Any, user_id: int, resolved: Any, exercise_index: int) -> None:
    """Parameter editor for one exercise of a resolved session, and the point
    where the v2 conversation-flow payload is armed.

    The payload carries the FULL identity (tier + plan_id/fact_rev +
    session_index + exercise_index + exercise_id + code), so a free-text edit
    applied minutes later still re-validates against the same session instead
    of falling back to an ambiguous bare code. Legacy code-only payloads keep
    working -- meal_text reads the new keys defensively.
    """
    ref = resolved.choice.ref
    exercises = resolved.session.get("exercises", [])
    exercise_data = exercises[exercise_index]
    muscle = exercise_data.get("muscle")
    muscle_line = f"שריר מטרה: <b>{esc(muscle)}</b>\n" if muscle else ""
    text = (
        f"<b>עריכת פרמטרים</b>\n\n"
        f"אימון: <b>{esc(resolved.choice.name)}</b>\n"
        f"תרגיל: <b>{esc(exercise_data['name'])}</b>\n"
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
        payload={
            "v": 2,
            "tier": ref.tier,
            "plan_id": ref.plan_id,
            "fact_rev": ref.fact_rev,
            "session_index": ref.session_index,
            "exercise_index": exercise_index,
            "exercise_id": exercise_data.get("id"),
            "code": resolved.choice.code,
        },
    )
    await safe_edit(query, text, exercise_params_keyboard_v2(ref, exercise_index))


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
async def select_todays_workout_code(user_id: int, *, now: datetime | None = None) -> str | None:
    """Pick the workout to show when the user taps 'אימון'.

    Preference (per product): the session scheduled for *today* in the active
    weekly plan; otherwise the next one in the plan's cycle after the last
    workout actually performed (A→B→C→A...). Returns None if no active plan.

    Time semantic (explicit): "today" here is the LOCAL CALENDAR day — plan
    sessions are keyed by calendar weekday (``date.weekday()``, Monday-first,
    see ``noam_coach.services.weekdays``), so the split rotation follows the
    calendar week, not the sleep-anchored coaching day of
    ``noam_coach.services.coaching_day`` (FIX 41), whose consumers are
    day-KEY subsystems (meals/flags/menus). Both the weekday match and the
    done-today window derive from the single ``now`` instant (injectable for
    tests; defaults to the real clock), so a midnight rollover mid-call
    cannot make the two disagree.

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
    return (await resolve_todays_workout(user_id, now=now)).code


@dataclass(frozen=True)
class TodaysWorkout:
    """Why the workout menu shows what it shows (audit F-A3).

    ``code`` is the workout to offer (None when there is nothing to offer);
    ``reason`` explains WHY, so the menu can render an honest message
    instead of collapsing 'no plan' and 'already done' into one
    'כבר הושלם' claim. ``done_today`` lists the codes already performed
    today, from real session rows — the single source of completion truth.
    """

    code: str | None
    reason: str  # offer_today | offer_next | no_plan | all_done_today
    done_today: tuple[str, ...] = ()


async def resolve_todays_workout(user_id: int, *, now: datetime | None = None) -> "TodaysWorkout":
    """Thin I/O wrapper: reads the active_workout_plan fact mirror and the
    sessions table, then delegates the cycle/weekday decision to
    ``workout_catalog.pick_session`` -- the pure core extracted from this
    function's own former body (workout-selection architecture, Batch 3).
    Behavior is byte-equivalent to before the extraction (proven by
    tests/test_workout_catalog.py's parity test); this function's SOURCE
    (the fact mirror, not planning.get_active_plan) and its exact return
    shape are both preserved so existing direct callers/tests
    (e.g. tests/regression/test_audit_2026_07_18.py) keep working unchanged.
    """
    from noam_coach.services.workout_catalog import pick_session

    current = (now or datetime.now(TZ)).astimezone(TZ)
    plan = await user_model.get_value(DB, user_id, "active_workout_plan")
    if not plan or not plan.get("sessions"):
        return TodaysWorkout(code=None, reason="no_plan")
    sessions = plan["sessions"]

    # Which codes were already completed today? Don't offer those again.
    start, end = daily_state.local_day_bounds_utc(current)
    done_today_rows = await DB.fetch_all(
        "SELECT DISTINCT code FROM sessions WHERE user_id=? "
        "AND status IN ('completed','partial') AND ended_at>=? AND ended_at<?",
        (user_id, start, end),
    )
    done_today = {r["code"] for r in done_today_rows}
    done_tuple = tuple(sorted(done_today))

    last = await DB.fetch_one(
        "SELECT code FROM sessions WHERE user_id=? "
        "AND status IN ('completed','partial') "
        "ORDER BY ended_at DESC LIMIT 1",
        (user_id,),
    )
    last_code = last["code"] if last else None

    code, reason = pick_session(sessions, done_today, current.weekday(), last_code)
    return TodaysWorkout(code=code, reason=reason, done_today=done_tuple)


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
    """Render *text*/*keyboard* as the next screen for *query*.

    DEBUG_APPEND_ONLY_MESSAGES (config.py): when enabled, every call sends a
    NEW message instead of editing the current one, leaving the previous
    screen and its keyboard visible -- purely a debugging/traceability aid so
    a full conversation flow can be scrolled and inspected. Off by default;
    production behavior (edit-in-place, including the stale-edit recovery
    below) is this function's normal path and is unchanged.

    Returns nothing: ~190 call sites treat rendering as fire-and-continue, and
    that is the right default for them. A caller that must know whether the
    screen actually REACHED the user calls `safe_edit_delivered` below.
    """
    await safe_edit_delivered(query, text, keyboard)


async def safe_edit_delivered(
    query: Any,
    text: str,
    keyboard: InlineKeyboardMarkup | None = None,
) -> bool:
    """`safe_edit`, reporting whether the screen was actually delivered.

    Same behaviour, one extra bit of truth. `safe_edit` returns None in three
    different outcomes -- edited, recovered by a fallback reply, and
    *fallback itself failed inside `suppress(Exception)`* -- so a caller
    cannot distinguish "the user is looking at this" from "nothing reached
    them". A13 records a load decision as PRESENTED, and that claim is only
    honest if the card arrived.

    False means the user did not receive this screen.
    """
    if SETTINGS.debug_append_only_messages:
        await _send_new_instead_of_edit(query, text, keyboard)
        return True
    try:
        await query.edit_message_text(
            text,
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
        )
        return True
    except BadRequest as exc:
        if is_stale_edit_error(exc):
            message = getattr(query, "message", None)
            if message is not None and hasattr(message, "reply_text"):
                try:
                    await message.reply_text(
                        text,
                        reply_markup=keyboard,
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:  # noqa: BLE001 — recovery is best-effort.
                    # Unchanged behaviour: the exception is still contained and
                    # the caller still proceeds. The difference is that the
                    # caller can now learn nothing was delivered.
                    return False
                return True
            return False
        if "not modified" not in str(exc).lower():
            raise
        # "not modified": the screen the user is looking at already carries
        # this content, so it IS delivered.
        return True


async def _send_new_instead_of_edit(
    query: Any,
    text: str,
    keyboard: InlineKeyboardMarkup | None,
) -> None:
    """DEBUG_APPEND_ONLY_MESSAGES fan-out: send *text*/*keyboard* as a new
    message via whatever send capability *query* actually exposes, without
    touching the previous message.

    Handles the two shapes this codebase's edit targets come in: a real
    Telegram ``CallbackQuery`` (has ``.message.reply_text``), and the
    ``_MessageEditTarget`` job-context adapter used by the workout rest-timer
    auto-advance (has its own ``send_new`` -- see workout_runtime.py).
    """
    send_new = getattr(query, "send_new", None)
    if callable(send_new):
        await send_new(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
        return
    message = getattr(query, "message", None)
    if message is not None and hasattr(message, "reply_text"):
        await message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
        return
    if hasattr(query, "reply_text"):
        # query is itself a Message (the onboarding.py hasattr(target,
        # "edit_message_text") dispatch pattern always routes a real Message
        # to the else-branch instead, but stay defensive here too).
        await query.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)


@runtime_bound(RUNTIME_NAMES)
async def safe_message_edit(
    message: Any,
    text: str,
    keyboard: InlineKeyboardMarkup | None = None,
) -> None:
    """Edit-in-place counterpart to ``safe_edit`` for plain ``Message``
    objects (``.edit_text``, not a callback query's ``.edit_message_text``).

    Used by the "progress placeholder" pattern: a handler sends a temporary
    "מנתח…"/"⏳" message via ``message.reply_text(...)``, does async work, then
    calls this to turn that placeholder into the real result in place.

    Respects DEBUG_APPEND_ONLY_MESSAGES the same way ``safe_edit`` does: when
    enabled, sends a new message instead of editing the placeholder, so the
    placeholder stays visible in the conversation history.

    Only the harmless "message is not modified" BadRequest is swallowed --
    every other BadRequest propagates unchanged.
    """
    if SETTINGS.debug_append_only_messages:
        if hasattr(message, "reply_text"):
            await message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
        return
    try:
        await message.edit_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
    except BadRequest as exc:
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

    Review 2026-07-18_1 / F-03: a TEXTUAL ack (a visible toast) is user
    feedback and is recorded as a canonical delivery event
    (operation=callback_ack) — empty spinner-stopping acks are deliberately
    not evented (volume, zero decision value).
    """
    try:
        await query.answer(text=text, show_alert=show_alert)
        delivered = True
    except BadRequest as exc:
        if not is_stale_callback_error(exc):
            raise
        LOGGER.info("Ignoring stale callback ACK: %s", exc)
        delivered = False
    if text:
        await _emit_callback_ack(text, show_alert=show_alert, delivered=delivered)
    return delivered


async def _emit_callback_ack(text: str, *, show_alert: bool, delivered: bool) -> None:
    """Best-effort canonical evidence for a visible callback toast (F-03)."""
    try:
        import coach_bot
        from noam_coach.observability import taxonomy
        from noam_coach.observability.emit import emit_event
        from noam_coach.observability.obs_context import current_user_id

        user_id = current_user_id()
        if user_id is None:
            return
        await emit_event(
            coach_bot.DB,
            user_id,
            taxonomy.DELIVERY_SUCCEEDED if delivered else taxonomy.DELIVERY_FAILED,
            entity="callback_ack",
            source="telegram",
            surface="telegram",
            status="delivered" if delivered else "failed",
            outcome="toast" if delivered else "stale_query",
            properties={"operation": "callback_ack", "show_alert": show_alert},
            content={"text": text},
        )
    except Exception:  # noqa: BLE001 — observability must not break the ACK.
        pass
