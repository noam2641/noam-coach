# ruff: noqa: F401, F811, F821, I001
"""Check-in callbacks and current-status text builders.

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
from noam_coach.bot.ui import button, safe_answer_callback, safe_edit

RUNTIME_NAMES = ('Any', 'DB', 'IndexError', 'InlineKeyboardMarkup', 'ValueError', '_checkin_more_keyboard', 'abs', 'action', 'bool', 'build_daily_context', 'button', 'coach_intelligence', 'ctx', 'data', 'esc', 'extra', 'flags', 'float', 'get_daily_flags', 'hh', 'hhmm', 'hour', 'int', 'kind', 'known_medications', 'last', 'latest_day', 'len', 'lines', 'med_name', 'meds', 'mini_app_url', 'mm', 'near', 'notes', 'parts', 'query', 'record_medication', 'safe_edit', 'save_medical_constraint', 'set_daily_flags', 'set_pending', 'str', 'url', 'user_id', 'value', 'when', 'window', 'x')


# Follow-up keyboard shown after a check-in category is answered, so the
# same card can keep collecting the other categories (med/sleep/energy/state
# are independent flags) instead of going dead after one tap.
def _checkin_more_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [button("😴 שינה טובה", "chk:sleep:good"), button("😐 שינה סבירה", "chk:sleep:ok"), button("😫 שינה גרועה", "chk:sleep:bad")],
        [button("⚡ אנרגיה גבוהה", "chk:energy:high"), button("🔋 אנרגיה רגילה", "chk:energy:normal"), button("🪫 אנרגיה נמוכה", "chk:energy:low")],
        [button("💊 לקחתי תרופה", "chk:med_other"), button("🕐 בצום", "chk:state:fasting"), button("🤕 יש כאב", "chk:state:pain")],
        [button("✅ זהו", "chk:state:normal")],
    ])


@runtime_bound(RUNTIME_NAMES)
async def handle_checkin_callback(query: Any, user_id: int, data: str) -> None:
    """Handle the morning check-in taps (the real-time signal source).

    Every branch edits the tapped check-in card in place (via ``safe_edit``)
    instead of sending a new message — tapping several categories updates the
    same card each time rather than stacking up separate replies below an
    unchanged, still-fully-tappable original keyboard.

    ``safe_edit`` is the single UI boundary here: DEBUG_APPEND_ONLY_MESSAGES
    is honoured there, so this module never needs its own debug branch.
    """
    parts = data.split(":")
    kind = parts[1]
    value = parts[2] if len(parts) > 2 else ""
    flags = await get_daily_flags(user_id)

    if kind == "med":
        # value is the index into the known-medications list.
        meds = await known_medications(user_id)
        try:
            med_name = meds[int(value)]
        except (ValueError, IndexError):
            await safe_answer_callback(query, "לא נמצא")
            return
        await record_medication(user_id, med_name, source="user_button")
        await safe_answer_callback(query, "נרשם 💊")
        extra = (
            " ביום כזה התיאבון בדרך כלל יורד — אקל על הארוחות, אדגיש חלבון ואזכיר לשתות."
            if "ריטלין" in med_name or med_name.lower() == "ritalin"
            else ""
        )
        await safe_edit(query, f"רשמתי שלקחת {esc(med_name)}. 💊{extra}", _checkin_more_keyboard())
        return
    if kind == "med_other":
        await set_pending(user_id, "__med_name__")
        await safe_answer_callback(query)
        await safe_edit(query, 'איזו תרופה לקחת? כתוב לי את השם.\n\n(כתוב "ביטול" כדי לדלג.)', None)
        return
    if kind == "sleep":
        flags["sleep_quality"] = value  # good | ok | bad
        await set_daily_flags(user_id, flags)
        await safe_answer_callback(query, "תודה")
        if value == "bad":
            await safe_edit(
                query,
                "רשמתי שישנת פחות טוב. לא אעלה משקלים אוטומטית היום, "
                "ואם תרצה — אפשר גרסת אימון מעט קלה יותר.",
                _checkin_more_keyboard(),
            )
        else:
            await safe_edit(query, "תודה, נרשם.", _checkin_more_keyboard())
        return
    if kind == "energy":
        flags["energy"] = value
        await set_daily_flags(user_id, flags)
        await safe_answer_callback(query, "נרשם")
        await safe_edit(query, "נרשם, אתאים את ההמלצות בהתאם 👍", _checkin_more_keyboard())
        return
    if kind == "state":
        if value == "pain":
            await set_pending(user_id, "__pain_location__")
            await save_medical_constraint(
                user_id,
                kind="pain",
                note="reported in morning check-in",
                affects=("exercise_selection",),
            )
            await safe_answer_callback(query)
            await safe_edit(
                query,
                'מצטער לשמוע. איפה כואב? (למשל "ברך ימין") אתאים תרגילים '
                'בהתאם. אם הכאב חד או מתגבר — כדאי בדיקה מקצועית.\n\n(כתוב "ביטול" כדי לדלג.)',
                None,
            )
        elif value == "fasting":
            flags["fasting"] = True
            await set_daily_flags(user_id, flags)
            await safe_answer_callback(query, "נרשם צום")
            await safe_edit(
                query,
                "רשמתי שאתה בצום היום — אתזמן את ההמלצות בהתאם. 🕐",
                _checkin_more_keyboard(),
            )
        else:  # normal
            flags["state"] = "normal"
            await set_daily_flags(user_id, flags)
            await safe_answer_callback(query, "יום רגיל 👍")
            await safe_edit(query, "יום רגיל 👍", None)
        return


@runtime_bound(RUNTIME_NAMES)
async def build_health_status_text(user_id: int) -> str:
    """Honest status of the Health data — it is retrospective, not live."""
    last = await DB.fetch_one(
        "SELECT details, created_at FROM audit "
        "WHERE user_id=? AND action='health_import' "
        "ORDER BY id DESC LIMIT 1",
        (user_id,),
    )
    latest_day = await DB.fetch_one(
        "SELECT MAX(start_time) AS d FROM health WHERE user_id=?",
        (user_id,),
    )
    lines = ["<b>⌚ נתוני Apple Health</b>", ""]
    # Audit F-A8: these two dates measure DIFFERENT things — when the file
    # was imported vs. how recent the newest sample INSIDE it is. They can
    # legitimately differ (a stale export imported today), but the old
    # labels didn't say so, reading as a contradiction. Label each by its
    # meaning and, when the newest sample lags the import, say the export
    # itself was stale — one honest source of truth.
    import_day = (last["created_at"] or "")[:10] if last else None
    data_day = latest_day["d"][:10] if latest_day and latest_day["d"] else None
    if import_day:
        lines.append(f"תאריך הייבוא האחרון (מתי נטען הקובץ): {import_day}")
    else:
        lines.append("עוד לא יובאו נתונים. שלח קובץ ZIP כדי להתחיל.")
    if data_day:
        lines.append(f"הנתון העדכני ביותר בקובץ (תאריך המדידה): {data_day}")
        if import_day and data_day < import_day:
            lines.append(
                "⚠️ <i>הקובץ שיובא אינו עדכני — המדידה החדשה ביותר בו מוקדמת "
                "מיום הייבוא. ייצא קובץ עדכני כדי לשפר את הדיוק.</i>"
            )
    lines += [
        "",
        "<b>משמש ל:</b>",
        "✅ מגמות משקל",
        "✅ למידת שינה ואימונים",
        "✅ דפוסי פעילות",
        "✅ ניתוח בדיעבד ושיפור המלצות",
        "",
        "<b>לא משמש כרגע ל:</b>",
        "❌ צעדים/קלוריות בזמן אמת",
        "❌ זיהוי תרופה בזמן אמת",
        "❌ מוכנות חיה לאימון",
        "",
        "<i>הנתונים מחושבים בדיעבד מתוך הייצוא — לא בחיבור רציף לשעון. "
        "במהלך היום אני מסתמך על מה שאתה מעדכן לי.</i>",
    ]
    url = mini_app_url(user_id)
    if url:
        lines += ["", f"Mini App: {url}"]
    return "\n".join(lines)


@runtime_bound(RUNTIME_NAMES)
async def build_now_action_text(user_id: int, ctx: "DailyContext | None" = None) -> str:
    """The single 'what to do now' answer, derived from the shared DailyContext.

    Priority of sources (data is retrospective, so reported-today wins):
    today's reported flags/meals/workout → the plan → learned routine →
    clearly-labelled assumptions. Never invents live watch data.
    """
    if ctx is None:
        ctx = await build_daily_context(user_id)
    hour = ctx.now.hour + ctx.now.minute / 60.0

    # First resolve structural blockers (safety/profile/goal/plan).  Daily
    # coaching only takes over once the product foundation is ready.
    action = await coach_intelligence.next_best_action(
        DB,
        user_id,
        consumed_calories=ctx.calories_consumed,
        consumed_protein=ctx.protein_consumed,
        now_local_hour=ctx.now.hour,
    )
    if action.priority >= 72:
        return (
            f"<b>{esc(action.title)}</b>\n\n"
            f"{esc(action.reason)}"
        )

    # 1) Mid-workout → continue it.
    if ctx.workout_active:
        return 'אתה באמצע אימון 🏋️ — בוא נמשיך אותו. פתח "אימון".'

    def near(hhmm: str | None, window: float = 1.0) -> bool:
        if not hhmm:
            return False
        hh, mm = (int(x) for x in hhmm.split(":"))
        return abs(hour - (hh + mm / 60.0)) <= window

    lines = ["<b>מה לעשות עכשיו</b>", ""]
    if near(ctx.usual_workout_time) and not ctx.workout_completed:
        lines.append(
            f"זה בערך הזמן שבו אתה בדרך כלל מתאמן (~{ctx.usual_workout_time}). רוצה להתחיל אימון?"
        )
    elif ctx.calories_remaining <= 0:
        lines.append("סגרת את מכסת הקלוריות להיום — עדיף לעצור או ללכת קצת.")
    elif ctx.hours_left <= 3 and ctx.protein_remaining > 30:
        lines.append(
            f"מתקרב הערב ונשארו לך {ctx.protein_remaining:.0f} ג׳ חלבון. עדיף "
            "לסגור עם מקור חלבון רזה (טונה/קוטג׳/שייק)."
        )
    else:
        lines.append(
            f"נשארו לך כ-{ctx.calories_remaining:.0f} קל׳ ו-"
            f"{ctx.protein_remaining:.0f} ג׳ חלבון להיום (עוד "
            f"~{ctx.hours_left:.0f} שעות). אפשר לתכנן ארוחה הבאה."
        )

    # Uncertainty note — be honest that today's activity isn't live.
    notes = []
    if ctx.flags.get("ritalin"):
        notes.append("ריטלין היום")
    if ctx.fasting:
        notes.append("צום")
    if ctx.sleep_quality == "bad":
        notes.append("שינה לא טובה")
    if notes:
        lines += ["", f"<i>לפי מה שעדכנת היום: {', '.join(notes)}.</i>"]
    return "\n".join(lines)
