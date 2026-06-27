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

RUNTIME_NAMES = ('Any', 'DB', 'IndexError', 'ValueError', 'abs', 'action', 'bool', 'build_daily_context', 'coach_intelligence', 'ctx', 'data', 'esc', 'extra', 'flags', 'float', 'get_daily_flags', 'hh', 'hhmm', 'hour', 'int', 'kind', 'known_medications', 'last', 'latest_day', 'len', 'lines', 'med_name', 'meds', 'mini_app_url', 'mm', 'near', 'notes', 'parts', 'query', 'record_medication', 'save_medical_constraint', 'set_daily_flags', 'set_pending', 'str', 'url', 'user_id', 'value', 'when', 'window', 'x')


@runtime_bound(RUNTIME_NAMES)
async def handle_checkin_callback(query: Any, user_id: int, data: str) -> None:
    """Handle the morning check-in taps (the real-time signal source)."""
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
            await query.answer("לא נמצא")
            return
        await record_medication(user_id, med_name, source="user_button")
        await query.answer("נרשם 💊")
        extra = (
            " ביום כזה התיאבון בדרך כלל יורד — אקל על הארוחות, אדגיש חלבון ואזכיר לשתות."
            if "ריטלין" in med_name or med_name.lower() == "ritalin"
            else ""
        )
        await query.message.reply_text(f"רשמתי שלקחת {esc(med_name)}.{extra}")
        return
    if kind == "med_other":
        await set_pending(user_id, "__med_name__")
        await query.message.reply_text('איזו תרופה לקחת? כתוב לי את השם.\n\n(כתוב "ביטול" כדי לדלג.)')
        return
    if kind == "sleep":
        flags["sleep_quality"] = value  # good | ok | bad
        await set_daily_flags(user_id, flags)
        await query.answer("תודה")
        if value == "bad":
            await query.message.reply_text(
                "רשמתי שישנת פחות טוב. לא אעלה משקלים אוטומטית היום, "
                "ואם תרצה — אפשר גרסת אימון מעט קלה יותר."
            )
        return
    if kind == "energy":
        flags["energy"] = value
        await set_daily_flags(user_id, flags)
        await query.answer("נרשם")
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
            await query.message.reply_text(
                'מצטער לשמוע. איפה כואב? (למשל "ברך ימין") אתאים תרגילים '
                'בהתאם. אם הכאב חד או מתגבר — כדאי בדיקה מקצועית.\n\n(כתוב "ביטול" כדי לדלג.)'
            )
        elif value == "fasting":
            flags["fasting"] = True
            await set_daily_flags(user_id, flags)
            await query.answer("נרשם צום")
            await query.message.reply_text("רשמתי שאתה בצום היום — אתזמן את ההמלצות בהתאם.")
        else:  # normal
            flags["state"] = "normal"
            await set_daily_flags(user_id, flags)
            await query.answer("יום רגיל 👍")
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
    if last:
        when = (last["created_at"] or "")[:10]
        lines.append(f"ייבוא אחרון: {when}")
    else:
        lines.append("עוד לא יובאו נתונים. שלח קובץ ZIP כדי להתחיל.")
    if latest_day and latest_day["d"]:
        lines.append(f"היום האחרון שנקלט: {latest_day['d'][:10]}")
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
