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
    client_event_id: str | None = None,
) -> tuple[bool, int]:
    plan = json.loads(session["plan"])
    idx = session["exercise_index"]
    set_no = session["set_number"]
    current = plan["exercises"][idx]
    completed = False
    now = utc_now()
    event_base = client_event_id or f"telegram_split:{session['id']}:{idx}:{set_no}:{secrets.token_hex(8)}"

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
                    f"{event_base}:primary",
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
                    f"{event_base}:secondary",
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
async def persist_rest_timer(timer_data: dict[str, Any]) -> None:
    """Persist the rest deadline as wall-clock time (FIX 45).

    The JobQueue's in-memory ``ends_at`` is a ``time.monotonic()`` value that
    a process restart destroys, while the durable session silently advances
    to the next set underneath it (the timer freezes but the DB has already
    moved on). Storing a wall-clock deadline lets a restart tell the
    difference between "still resting" and "rest already ended while we were
    down" and react accordingly instead of leaving an abandoned card.
    """
    now = datetime.now(timezone.utc)
    ends_at = now + timedelta(seconds=timer_data["total_seconds"])
    step = timer_data["session_step"]
    await DB.execute(
        """
        INSERT INTO rest_timers(
            session_id, user_id, chat_id, message_id, exercise_index,
            set_number, weight, reps, rir, total_seconds, ends_at_utc,
            summary_line, created_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(session_id) DO UPDATE SET
            chat_id=excluded.chat_id, message_id=excluded.message_id,
            exercise_index=excluded.exercise_index, set_number=excluded.set_number,
            weight=excluded.weight, reps=excluded.reps, rir=excluded.rir,
            total_seconds=excluded.total_seconds, ends_at_utc=excluded.ends_at_utc,
            summary_line=excluded.summary_line, created_at=excluded.created_at
        """,
        (
            timer_data["session_id"], timer_data["user_id"], timer_data["chat_id"],
            timer_data["message_id"], int(step["exercise_index"]), int(step["set_number"]),
            timer_data["weight"], timer_data["reps"], timer_data["rir"],
            timer_data["total_seconds"], ends_at.isoformat(),
            timer_data.get("summary_line"), now.isoformat(),
        ),
    )


@runtime_bound(RUNTIME_NAMES)
async def clear_persisted_rest_timer(session_id: int) -> None:
    await DB.execute("DELETE FROM rest_timers WHERE session_id=?", (session_id,))


@runtime_bound(RUNTIME_NAMES)
async def restore_rest_timers_on_startup(job_queue: Any) -> int:
    """Restore or resolve every persisted rest timer at process startup (FIX 45).

    For each row still pointing at an active session, on the current step:
      * If the wall-clock deadline is still in the future, re-arm a real
        JobQueue tick so the card keeps counting down and auto-advances.
      * If the deadline already passed while the process was down, edit the
        stale card directly into the "rest ended" resume state instead of
        leaving it frozen at whatever remaining time it last showed.
    A row whose session is no longer active/current (finished, or the
    session moved past this step) is simply dropped -- the card's own
    session-step-embedded callback data already prevents a stale card from
    advancing the wrong step, so no action is needed on it here.
    Returns the number of timers processed (restored + resolved).
    """
    rows = await DB.fetch_all("SELECT * FROM rest_timers")
    if not rows:
        return 0

    processed = 0
    now = datetime.now(timezone.utc)
    for row in rows:
        session_id = int(row["session_id"])
        session = await DB.fetch_one(
            """
            SELECT id, user_id, exercise_index, set_number
            FROM sessions
            WHERE id=? AND status='active'
            """,
            (session_id,),
        )
        if not session:
            await clear_persisted_rest_timer(session_id)
            continue
        current_step_matches = (
            int(session["exercise_index"]) == int(row["exercise_index"])
            and int(session["set_number"]) == int(row["set_number"])
        )
        if not current_step_matches:
            # The session already advanced past this rest -- the card, if
            # still visible, is stale; its own embedded step data already
            # blocks it from mutating the current step further.
            await clear_persisted_rest_timer(session_id)
            continue

        try:
            ends_at = datetime.fromisoformat(str(row["ends_at_utc"]))
        except ValueError:
            await clear_persisted_rest_timer(session_id)
            continue

        session_step = {
            "id": session_id,
            "exercise_index": int(row["exercise_index"]),
            "set_number": int(row["set_number"]),
        }
        remaining = (ends_at - now).total_seconds()
        target = _MessageEditTarget(
            job_queue.application.bot, int(row["chat_id"]), int(row["message_id"])
        )
        if remaining > 0:
            timer_data = {
                "user_id": int(row["user_id"]),
                "session_id": session_id,
                "session_step": session_step,
                "chat_id": int(row["chat_id"]),
                "message_id": int(row["message_id"]),
                "weight": float(row["weight"]),
                "reps": int(row["reps"]),
                "rir": int(row["rir"]),
                "total_seconds": int(row["total_seconds"]),
                "ends_at": time.monotonic() + remaining,
                "last_remaining": None,
                "cancelled": False,
                "summary_line": row.get("summary_line"),
            }
            job_queue.run_repeating(
                rest_timer_tick,
                interval=1,
                first=1,
                name=rest_job_name(int(row["user_id"]), session_id),
                data=timer_data,
                chat_id=int(row["chat_id"]),
                user_id=int(row["user_id"]),
            )
            with suppress(Exception):
                await _emit_rest_event(
                    int(row["user_id"]), session_id, "restored",
                    remaining_seconds=int(remaining),
                    total_seconds=int(row["total_seconds"]),
                )
        else:
            # Rest already ended while the process was down -- resolve the
            # stale card into the resume state instead of leaving it frozen.
            with suppress(Exception):
                await target.edit_message_text(
                    rest_text(
                        float(row["weight"]), int(row["reps"]), int(row["rir"]),
                        0, int(row["total_seconds"]), row.get("summary_line"),
                    ),
                    reply_markup=rest_keyboard(session_step, finished=True),
                    parse_mode=ParseMode.HTML,
                )
            await clear_persisted_rest_timer(session_id)
        processed += 1
    return processed


@runtime_bound(RUNTIME_NAMES)
async def cancel_rest_timer(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    session_id: int,
) -> None:
    with suppress(Exception):
        await clear_persisted_rest_timer(session_id)

    if context.job_queue is None:
        return

    jobs = context.job_queue.get_jobs_by_name(rest_job_name(user_id, session_id))
    for job in jobs:
        if isinstance(job.data, dict):
            job.data["cancelled"] = True
        job.schedule_removal()
    if jobs:
        with suppress(Exception):
            await _emit_rest_event(user_id, session_id, "cancelled")


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

    # Observability O3 exemption: countdown ticks are high-frequency,
    # zero-decision-value edits of the same card — recording each one would
    # flood the trace. The rest lifecycle itself (started/restored/finished)
    # is observed at the state layer instead.
    from noam_coach.observability.telegram_egress import unobserved_delivery

    try:
        with unobserved_delivery():
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

    async def send_new(self, text: str, **kwargs: Any) -> None:
        """DEBUG_APPEND_ONLY_MESSAGES support: send a new message to the same
        chat instead of editing the existing one (see ui.safe_edit)."""
        await self._bot.send_message(chat_id=self._chat_id, text=text, **kwargs)


async def _emit_rest_event(user_id: int, session_id: int, action: str, **props: Any) -> None:
    """Observability O7: rest-timer lifecycle (started/restored/finished/
    cancelled) — the countdown TICKS themselves stay unobserved by design."""
    import coach_bot
    from noam_coach.observability import taxonomy
    from noam_coach.observability.emit import emit_event

    await emit_event(
        coach_bot.DB, user_id, taxonomy.STATE_MUTATED,
        entity="rest_timer", entity_id=session_id,
        source="workout", status="mutated", outcome=action,
        properties={"domain": "rest_timer", "action": action, "session_id": session_id, **props},
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
        with suppress(Exception):
            await _emit_rest_event(
                int(timer_data["user_id"]), int(timer_data["session_id"]), "finished",
                total_seconds=timer_data.get("total_seconds"),
            )
        with suppress(Exception):
            await clear_persisted_rest_timer(timer_data["session_id"])
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
    with suppress(Exception):
        await persist_rest_timer(timer_data)

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
    with suppress(Exception):
        await _emit_rest_event(
            user_id, session_id, "started",
            total_seconds=rest_seconds, weight=weight, reps=reps,
        )
