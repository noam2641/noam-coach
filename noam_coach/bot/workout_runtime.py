# ruff: noqa: F401, F811, F821, I001
"""Split-set and rest-timer runtime.

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

RUNTIME_NAMES = ('Any', 'BadRequest', 'CallbackContext', 'ContextTypes', 'DB', 'Exception', 'InlineKeyboardMarkup', 'LOGGER', 'NetworkError', 'ParseMode', 'RetryAfter', 'RuntimeError', 'TimedOut', '_MessageEditTarget', '_StaleSetStep', '_rir_known', '_split_flow', 'advance_sql', 'base_weight', 'bool', 'bot', 'button', 'buttons', 'cancel_rest_timer', 'candidate', 'center', 'chat_id', 'choices', 'clear_flow_state', 'completed', 'completed_blocks', 'conn', 'context', 'cur', 'current', 'delta', 'dict', 'divmod', 'exc', 'finished', 'first_reps', 'first_weight', 'float', 'get_flow_state', 'home_keyboard', 'idx', 'increment', 'index', 'int', 'isinstance', 'job', 'jobs', 'json', 'kwargs', 'len', 'list', 'math', 'max', 'message_id', 'min', 'minutes', 'now', 'params', 'part', 'plan', 'progress', 'progress_bar', 'query', 'range', 'refreshed', 'remaining', 'reps', 'rest_job_name', 'rest_keyboard', 'rest_seconds', 'rest_text', 'rest_timer_tick', 'rir', 'rir_label', 'round', 'rows', 'safe_edit', 'second_reps', 'second_weight', 'seconds', 'self', 'session', 'session_action_data', 'session_id', 'session_step', 'set_flow_state', 'set_line', 'set_no', 'should_update', 'show_session', 'split_state', 'state', 'step', 'str', 'summary_line', 'suppress', 'target', 'text', 'time', 'timer_data', 'total', 'total_reps', 'total_seconds', 'tuple', 'update_rest_message', 'user_id', 'utc_now', 'value', 'values', 'weight', 'workout_summary')


@runtime_bound(RUNTIME_NAMES)
def _split_flow(session_id: int) -> str:
    return f"split:{session_id}"


@runtime_bound(RUNTIME_NAMES)
async def get_split_state(user_id: int, session_id: int) -> dict[str, Any]:
    state = await get_flow_state(user_id, _split_flow(session_id))
    return state["payload"] if state else {}


@runtime_bound(RUNTIME_NAMES)
async def set_split_state(user_id: int, session_id: int, split_state: dict[str, Any]) -> None:
    await set_flow_state(user_id, _split_flow(session_id), "active", split_state)


@runtime_bound(RUNTIME_NAMES)
async def clear_split_state(user_id: int, session_id: int) -> None:
    await clear_flow_state(user_id, _split_flow(session_id))


@runtime_bound(RUNTIME_NAMES)
def split_weight_keyboard(
    session: dict[str, Any],
    part: int,
    base_weight: float,
    increment: float,
) -> InlineKeyboardMarkup:
    values: list[float] = []
    step = max(0.5, float(increment))
    for delta in (-2 * step, -step, 0.0, step, 2 * step):
        candidate = round(max(0.0, base_weight + delta), 2)
        if candidate not in values:
            values.append(candidate)

    buttons = [
        button(
            f"{value:g} ק״ג{' ✓' if value == round(base_weight, 2) else ''}",
            session_action_data("splitw", session, part, value),
        )
        for value in values
    ]

    rows = [buttons[:3], buttons[3:]] if len(buttons) > 3 else [buttons]
    rows.append([button("❌ ביטול", session_action_data("ready", session))])
    return InlineKeyboardMarkup(rows)


@runtime_bound(RUNTIME_NAMES)
def split_reps_keyboard(
    session: dict[str, Any],
    part: int,
    center: int,
) -> InlineKeyboardMarkup:
    choices = list(range(max(1, center - 2), center + 3))
    rows = [
        [
            button(
                str(value),
                session_action_data("splitr", session, part, value),
            )
            for value in choices[index : index + 4]
        ]
        for index in range(0, len(choices), 4)
    ]
    rows.append([button("❌ ביטול", session_action_data("ready", session))])
    return InlineKeyboardMarkup(rows)


@runtime_bound(RUNTIME_NAMES)
def split_rir_keyboard(
    session: dict[str, Any],
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                button("3+", session_action_data("splitrir", session, 3)),
                button("2", session_action_data("splitrir", session, 2)),
                button("1", session_action_data("splitrir", session, 1)),
                button("0/כשל", session_action_data("splitrir", session, 0)),
            ],
            [button("❌ ביטול", session_action_data("ready", session))],
        ]
    )


@runtime_bound(RUNTIME_NAMES)
def split_summary_line(
    first_weight: float,
    first_reps: int,
    second_weight: float,
    second_reps: int,
    rir: int,
) -> str:
    total_reps = first_reps + second_reps
    rir_label = "0 / כשל" if rir == 0 else str(rir)
    return (
        f"{first_weight:g}×{first_reps} + "
        f"{second_weight:g}×{second_reps} | "
        f"סה״כ {total_reps} | RIR {rir_label}"
    )


@runtime_bound(RUNTIME_NAMES)
async def save_split_set(
    session: dict[str, Any],
    first_weight: float,
    first_reps: int,
    second_weight: float,
    second_reps: int,
    rir: int,
) -> tuple[bool, int]:
    plan = json.loads(session["plan"])
    idx = session["exercise_index"]
    set_no = session["set_number"]
    current = plan["exercises"][idx]
    completed = False
    now = utc_now()

    async with DB.transaction() as conn:
        if set_no < current["sets"]:
            advance_sql = (
                "UPDATE sessions SET set_number=set_number+1, "
                "pending_weight=NULL, pending_reps=NULL "
                "WHERE id=? AND status='active' AND exercise_index=? AND set_number=?"
            )
            params = (session["id"], idx, set_no)
        elif idx + 1 < len(plan["exercises"]):
            advance_sql = (
                "UPDATE sessions SET exercise_index=exercise_index+1, set_number=1, "
                "pending_weight=NULL, pending_reps=NULL "
                "WHERE id=? AND status='active' AND exercise_index=? AND set_number=?"
            )
            params = (session["id"], idx, set_no)
        else:
            completed = True
            advance_sql = (
                "UPDATE sessions SET status='completed', ended_at=? "
                "WHERE id=? AND status='active' AND exercise_index=? AND set_number=?"
            )
            params = (now, session["id"], idx, set_no)

        cur = await conn.execute(advance_sql, params)
        if cur.rowcount != 1:
            raise _StaleSetStep()

        await conn.executemany(
            """
            INSERT INTO sets(
                session_id, exercise_id, exercise_name, set_number,
                weight, reps, rir, source, client_event_id, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    session["id"],
                    current["id"],
                    current["name"],
                    set_no,
                    first_weight,
                    first_reps,
                    rir,
                    "telegram_split_primary",
                    None,
                    now,
                ),
                (
                    session["id"],
                    current["id"],
                    current["name"],
                    set_no,
                    second_weight,
                    second_reps,
                    rir,
                    "telegram_split_secondary",
                    None,
                    now,
                ),
            ],
        )

    return completed, current["rest"]


@runtime_bound(RUNTIME_NAMES)
def rest_job_name(user_id: int, session_id: int) -> str:
    return f"rest:{user_id}:{session_id}"


@runtime_bound(RUNTIME_NAMES)
def rest_keyboard(
    session_step: dict[str, Any],
    finished: bool = False,
) -> InlineKeyboardMarkup:
    if finished:
        return InlineKeyboardMarkup(
            [
                [
                    button(
                        "הצג סט הבא",
                        session_action_data("ready", session_step),
                    )
                ]
            ]
        )

    return InlineKeyboardMarkup(
        [
            [
                button(
                    "✅ מוכן עכשיו",
                    session_action_data("ready", session_step),
                ),
                button(
                    "➕ 30 שניות",
                    session_action_data("restadd", session_step, 30),
                ),
            ],
            [
                button(
                    "↩️ בטל את הסט האחרון",
                    session_action_data("undoset", session_step),
                ),
            ],
        ]
    )


@runtime_bound(RUNTIME_NAMES)
def rest_text(
    weight: float,
    reps: int,
    rir: int,
    remaining: int,
    total_seconds: int,
    summary_line: str | None = None,
) -> str:
    minutes, seconds = divmod(max(0, remaining), 60)
    progress = 1 - (remaining / max(1, total_seconds))
    completed_blocks = min(10, max(0, round(progress * 10)))
    progress_bar = "█" * completed_blocks + "░" * (10 - completed_blocks)
    rir_label = "לא דווח" if not _rir_known(rir) else ("0 / כשל" if rir == 0 else str(rir))
    set_line = summary_line or f"{weight:g} ק״ג × {reps} | RIR {rir_label}"

    if remaining <= 0:
        return f"<b>המנוחה הסתיימה 🔔</b>\n\n{set_line}\n\nמוכן לסט הבא."

    return (
        "<b>הסט נשמר ✅</b>\n\n"
        f"{set_line}\n\n"
        f"⏱ מנוחה: <b>{minutes:02d}:{seconds:02d}</b>\n"
        f"<code>{progress_bar}</code>"
    )


@runtime_bound(RUNTIME_NAMES)
async def cancel_rest_timer(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    session_id: int,
) -> None:
    if context.job_queue is None:
        return

    jobs = context.job_queue.get_jobs_by_name(rest_job_name(user_id, session_id))
    for job in jobs:
        if isinstance(job.data, dict):
            job.data["cancelled"] = True
        job.schedule_removal()


@runtime_bound(RUNTIME_NAMES)
async def update_rest_message(
    context: CallbackContext,
    timer_data: dict[str, Any],
) -> int:
    remaining = max(
        0,
        math.ceil(timer_data["ends_at"] - time.monotonic()),
    )

    total = timer_data["total_seconds"]
    should_update = (
        remaining == total
        or remaining == 0
        or remaining <= 10
        or (total >= 60 and remaining == total // 2)
    )
    if not should_update:
        return remaining

    if remaining == timer_data.get("last_remaining"):
        return remaining

    timer_data["last_remaining"] = remaining

    try:
        await context.bot.edit_message_text(
            chat_id=timer_data["chat_id"],
            message_id=timer_data["message_id"],
            text=rest_text(
                timer_data["weight"],
                timer_data["reps"],
                timer_data["rir"],
                remaining,
                timer_data["total_seconds"],
                timer_data.get("summary_line"),
            ),
            reply_markup=rest_keyboard(
                timer_data["session_step"],
                finished=remaining <= 0,
            ),
            parse_mode=ParseMode.HTML,
        )
    except RetryAfter as exc:
        LOGGER.warning("Telegram rate limit during rest timer: %s", exc)
    except (TimedOut, NetworkError) as exc:
        LOGGER.warning("Temporary timer network error: %s", exc)
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            LOGGER.warning("Unable to update rest timer: %s", exc)

    return remaining


class _MessageEditTarget:
    """Adapter so show_session/safe_edit can edit an existing message from a job
    context (which has chat_id/message_id but no callback query)."""

    def __init__(self, bot: Any, chat_id: int, message_id: int):
        self._bot = bot
        self._chat_id = chat_id
        self._message_id = message_id

    async def edit_message_text(self, text: str, **kwargs: Any) -> None:
        await self._bot.edit_message_text(
            chat_id=self._chat_id,
            message_id=self._message_id,
            text=text,
            **kwargs,
        )


@runtime_bound(RUNTIME_NAMES)
async def rest_timer_tick(context: CallbackContext) -> None:
    job = context.job
    if job is None:
        return

    timer_data = job.data
    if not isinstance(timer_data, dict) or timer_data.get("cancelled"):
        job.schedule_removal()
        return

    remaining = await update_rest_message(context, timer_data)
    if remaining <= 0:
        job.schedule_removal()
        # Auto-advance: when rest ends, show the next set in the same card —
        # no need to tap "הצג סט הבא". Skip if a pain stop cancelled the flow.
        with suppress(Exception):
            target = _MessageEditTarget(
                context.bot, timer_data["chat_id"], timer_data["message_id"]
            )
            await show_session(target, timer_data["user_id"], timer_data["session_id"])


@runtime_bound(RUNTIME_NAMES)
async def start_rest_timer(
    context: ContextTypes.DEFAULT_TYPE,
    query: Any,
    user_id: int,
    session_id: int,
    weight: float,
    reps: int,
    rir: int,
    rest_seconds: int,
    summary_line: str | None = None,
) -> None:
    await cancel_rest_timer(context, user_id, session_id)

    refreshed = await DB.fetch_one(
        """
        SELECT id, exercise_index, set_number
        FROM sessions
        WHERE id=? AND user_id=? AND status='active'
        """,
        (session_id, user_id),
    )
    if not refreshed:
        await safe_edit(
            query,
            await workout_summary(user_id, session_id),
            home_keyboard(),
        )
        return

    session_step = {
        "id": int(refreshed["id"]),
        "exercise_index": int(refreshed["exercise_index"]),
        "set_number": int(refreshed["set_number"]),
    }
    timer_data = {
        "user_id": user_id,
        "session_id": session_id,
        "session_step": session_step,
        "chat_id": query.message.chat_id,
        "message_id": query.message.message_id,
        "weight": weight,
        "reps": reps,
        "rir": rir,
        "total_seconds": rest_seconds,
        "ends_at": time.monotonic() + rest_seconds,
        "last_remaining": None,
        "cancelled": False,
        "summary_line": summary_line,
    }

    await safe_edit(
        query,
        rest_text(
            weight,
            reps,
            rir,
            rest_seconds,
            rest_seconds,
            summary_line,
        ),
        rest_keyboard(session_step),
    )

    if context.job_queue is None:
        raise RuntimeError("JobQueue אינו פעיל")

    context.job_queue.run_repeating(
        rest_timer_tick,
        interval=1,
        first=1,
        name=rest_job_name(user_id, session_id),
        data=timer_data,
        chat_id=query.message.chat_id,
        user_id=user_id,
    )
