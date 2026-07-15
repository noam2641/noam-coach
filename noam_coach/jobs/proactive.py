# ruff: noqa: F401, F811, F821, I001
"""Proactive delivery claims and daily context services.

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

RUNTIME_NAMES = ('Any', 'AttributeError', 'Awaitable', 'Callable', 'CallbackContext', 'DB', 'DailyContext', 'Exception', 'JOB_PRIORITY_HIGH', 'JOB_PRIORITY_URGENT', 'JobDeliveryClaim', 'LOGGER', 'RuntimeError', 'SETTINGS', 'TZ', 'ValueError', '_clock_minutes', '_within_quiet_hours', 'active_constraints', 'active_session', 'aiosqlite', 'allowance', 'allowed', 'any', 'attempts', 'bedtime', 'bool', 'bot_done', 'budget_row', 'cal_target', 'callback', 'calories', 'claim', 'claim_job_delivery', 'complete_job_delivery', 'connection', 'constraints', 'context', 'conversation', 'counts_toward_budget', 'current', 'cursor', 'data_quality', 'dataclass', 'datetime', 'day', 'delay_minutes', 'delay_seconds', 'dict', 'end', 'end_utc', 'exc', 'existing', 'existing_row', 'fail_job_delivery', 'fetch_goal', 'field', 'flags', 'float', 'flow', 'gap', 'get_daily_flags', 'goal', 'hh', 'hour', 'hour_text', 'hours_left_until_sleep', 'imported', 'int', 'is_usual_workout_day', 'job', 'key', 'last_sent', 'latest', 'list', 'load_routine_profile', 'local_now', 'max', 'min', 'minute', 'minute_text', 'mm', 'name', 'next_attempt', 'next_retry', 'notify_admin', 'now', 'now_text', 'nutrition_sensitive', 'priority', 'pro_target', 'profile', 'protein', 'reason', 'repr', 'retry_callback', 'row', 'schedule_job_retry', 'sender', 'session', 'stale_before', 'start', 'start_utc', 'status', 'str', 'timedelta', 'timezone', 'today_bounds_utc', 'today_consumed', 'token', 'tuple', 'used', 'user_id', 'utc_now', 'value', 'weekday', 'workout_completed_today', 'x')


JOB_PRIORITY_LOW = 10


JOB_PRIORITY_COACHING = 30


JOB_PRIORITY_SCHEDULED = 50


JOB_PRIORITY_HIGH = 80


JOB_PRIORITY_URGENT = 100


NUTRITION_GOAL_QUALITY_KEYS = frozenset(
    {
        "morning_menu",
        "evening",
        "weekly_summary",
        "overpace_alert",
        "intraday_nudge",
    }
)


NUTRITION_DAY_QUALITY_KEYS = frozenset(
    {
        "evening",
        "overpace_alert",
        "intraday_nudge",
    }
)


@dataclass(frozen=True)
class JobDeliveryClaim:
    user_id: int
    day: str
    key: str
    attempt_count: int


@runtime_bound(RUNTIME_NAMES)
def _clock_minutes(value: str) -> int:
    try:
        hour_text, minute_text = value.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
    except (ValueError, AttributeError) as exc:
        raise RuntimeError(f"Invalid HH:MM setting: {value!r}") from exc
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise RuntimeError(f"Invalid HH:MM setting: {value!r}")
    return hour * 60 + minute


@runtime_bound(RUNTIME_NAMES)
def _within_quiet_hours(now: datetime) -> bool:
    current = now.hour * 60 + now.minute
    start = _clock_minutes(SETTINGS.proactive_quiet_start)
    end = _clock_minutes(SETTINGS.proactive_quiet_end)
    if start == end:
        return False
    if start < end:
        return start <= current < end
    return current >= start or current < end


@runtime_bound(RUNTIME_NAMES)
async def claim_job_delivery(
    user_id: int,
    key: str,
    *,
    priority: int,
    counts_toward_budget: bool = True,
) -> JobDeliveryClaim | None:
    """Atomically claim one proactive delivery.

    A sent delivery is never repeated. Failed or abandoned claims may be
    retried after their retry/claim TTL. Low-value messages respect quiet
    hours, a daily budget and a minimum gap.
    """
    local_now = datetime.now(TZ)
    if priority < JOB_PRIORITY_URGENT and _within_quiet_hours(local_now):
        return None

    # Defer non-urgent messages when an interactive flow is active.
    if priority < JOB_PRIORITY_URGENT:
        flow = await conversation.get_active_flow(DB, user_id)
        if flow is not None and not flow.is_idle:
            return None

    day = local_now.date().isoformat()
    now = datetime.now(timezone.utc)
    now_text = now.isoformat()
    stale_before = (now - timedelta(minutes=SETTINGS.proactive_claim_ttl_minutes)).isoformat()

    async with DB.transaction() as connection:
        cursor = await connection.execute(
            """
            SELECT *
            FROM job_state
            WHERE user_id=? AND day=? AND key=?
            """,
            (user_id, day, key),
        )
        existing_row = await cursor.fetchone()
        existing = dict(existing_row) if existing_row else None

        if existing:
            status = existing.get("status") or "sent"
            attempts = int(existing.get("attempt_count") or 0)
            if status == "sent":
                return None
            if attempts >= SETTINGS.proactive_max_attempts:
                return None
            if (
                status == "claimed"
                and existing.get("claimed_at")
                and existing["claimed_at"] > stale_before
            ):
                return None
            if (
                status == "failed"
                and existing.get("next_retry_at")
                and existing["next_retry_at"] > now_text
            ):
                return None

        if counts_toward_budget and priority < JOB_PRIORITY_URGENT:
            cursor = await connection.execute(
                """
                SELECT COUNT(*) AS c
                FROM job_state
                WHERE user_id=?
                  AND day=?
                  AND counts_toward_budget=1
                  AND status IN ('claimed', 'sent')
                """,
                (user_id, day),
            )
            budget_row = await cursor.fetchone()
            used = int(budget_row["c"]) if budget_row else 0
            allowance = SETTINGS.proactive_daily_limit
            if priority >= JOB_PRIORITY_HIGH:
                allowance += 1
            if used >= allowance:
                return None

            if priority < JOB_PRIORITY_HIGH:
                cursor = await connection.execute(
                    """
                    SELECT sent_at
                    FROM job_state
                    WHERE user_id=?
                      AND day=?
                      AND counts_toward_budget=1
                      AND status='sent'
                      AND sent_at IS NOT NULL
                    ORDER BY sent_at DESC
                    LIMIT 1
                    """,
                    (user_id, day),
                )
                latest = await cursor.fetchone()
                if latest and latest["sent_at"]:
                    last_sent = datetime.fromisoformat(latest["sent_at"])
                    if last_sent.tzinfo is None:
                        last_sent = last_sent.replace(tzinfo=timezone.utc)
                    else:
                        last_sent = last_sent.astimezone(timezone.utc)
                    gap = now - last_sent
                    if gap < timedelta(minutes=SETTINGS.proactive_min_gap_minutes):
                        return None

        next_attempt = int(existing.get("attempt_count") or 0) + 1 if existing else 1
        if existing:
            cursor = await connection.execute(
                """
                UPDATE job_state
                SET status='claimed',
                    priority=?,
                    attempt_count=?,
                    claimed_at=?,
                    sent_at=NULL,
                    last_error=NULL,
                    next_retry_at=NULL,
                    counts_toward_budget=?,
                    updated_at=?
                WHERE user_id=? AND day=? AND key=?
                  AND status!='sent'
                """,
                (
                    priority,
                    next_attempt,
                    now_text,
                    int(counts_toward_budget),
                    now_text,
                    user_id,
                    day,
                    key,
                ),
            )
            if cursor.rowcount != 1:
                return None
        else:
            try:
                await connection.execute(
                    """
                    INSERT INTO job_state(
                        user_id, day, key, value, status, priority,
                        attempt_count, claimed_at, sent_at, last_error,
                        next_retry_at, counts_toward_budget,
                        created_at, updated_at
                    ) VALUES(
                        ?, ?, ?, '1', 'claimed', ?, ?,
                        ?, NULL, NULL, NULL, ?, ?, ?
                    )
                    """,
                    (
                        user_id,
                        day,
                        key,
                        priority,
                        next_attempt,
                        now_text,
                        int(counts_toward_budget),
                        now_text,
                        now_text,
                    ),
                )
            except aiosqlite.IntegrityError:
                return None

    return JobDeliveryClaim(
        user_id=user_id,
        day=day,
        key=key,
        attempt_count=next_attempt,
    )


@runtime_bound(RUNTIME_NAMES)
async def complete_job_delivery(claim: JobDeliveryClaim) -> None:
    now = utc_now()
    await DB.execute_rowcount(
        """
        UPDATE job_state
        SET status='sent',
            sent_at=?,
            last_error=NULL,
            next_retry_at=NULL,
            updated_at=?
        WHERE user_id=? AND day=? AND key=?
          AND status='claimed'
          AND attempt_count=?
        """,
        (
            now,
            now,
            claim.user_id,
            claim.day,
            claim.key,
            claim.attempt_count,
        ),
    )


@runtime_bound(RUNTIME_NAMES)
async def fail_job_delivery(
    claim: JobDeliveryClaim,
    exc: Exception,
) -> int:
    delay_minutes = min(
        60,
        5 * (2 ** max(0, claim.attempt_count - 1)),
    )
    now = datetime.now(timezone.utc)
    next_retry = now + timedelta(minutes=delay_minutes)
    await DB.execute_rowcount(
        """
        UPDATE job_state
        SET status='failed',
            last_error=?,
            next_retry_at=?,
            updated_at=?
        WHERE user_id=? AND day=? AND key=?
          AND status='claimed'
          AND attempt_count=?
        """,
        (
            repr(exc)[:1000],
            next_retry.isoformat(),
            now.isoformat(),
            claim.user_id,
            claim.day,
            claim.key,
            claim.attempt_count,
        ),
    )
    return delay_minutes * 60


@runtime_bound(RUNTIME_NAMES)
def schedule_job_retry(
    context: CallbackContext,
    callback: Callable[[CallbackContext], Awaitable[None]],
    key: str,
    delay_seconds: int,
) -> None:
    if context.job_queue is None:
        return
    name = f"delivery_retry:{key}"
    for job in context.job_queue.get_jobs_by_name(name):
        job.schedule_removal()
    context.job_queue.run_once(
        callback,
        when=delay_seconds,
        name=name,
    )


@runtime_bound(RUNTIME_NAMES)
async def deliver_proactive_message(
    context: CallbackContext,
    *,
    key: str,
    sender: Callable[[], Awaitable[None]],
    priority: int,
    counts_toward_budget: bool = True,
    retry_callback: Callable[[CallbackContext], Awaitable[None]] | None = None,
) -> bool:
    user_id = SETTINGS.telegram_allowed_user_id
    goal_quality_sensitive = key in NUTRITION_GOAL_QUALITY_KEYS
    nutrition_sensitive = key in NUTRITION_DAY_QUALITY_KEYS
    start_utc = end_utc = None
    if nutrition_sensitive:
        # B3/ARCH-02: proactive NUTRITION-quality gating reads the coaching
        # day's window (send-time scheduling itself stays wall-clock).
        start_utc, end_utc = await daily_state.coaching_day_bounds_utc(DB, user_id)
    allowed, reason = await data_quality.can_send_proactive(
        DB,
        user_id,
        require_goal_quality=goal_quality_sensitive,
        require_nutrition_quality=nutrition_sensitive,
        start_utc=start_utc,
        end_utc=end_utc,
    )
    if not allowed:
        LOGGER.info("Proactive message deferred (%s): %s", key, reason)
        return False
    claim = await claim_job_delivery(
        user_id,
        key,
        priority=priority,
        counts_toward_budget=counts_toward_budget,
    )
    if claim is None:
        return False

    # Observability O3: one trace per proactive delivery — the job-level
    # attempt/outcome (operation="proactive_job") plus every message-level
    # render/delivery event the sender produces correlate to the same trace.
    from noam_coach.observability import emit_event, interaction_scope, taxonomy

    with interaction_scope(user_id=user_id):
        await emit_event(
            DB, user_id, taxonomy.DELIVERY_ATTEMPTED,
            entity="proactive_job", entity_id=key,
            source="job", surface="telegram", status="attempted",
            properties={
                "operation": "proactive_job", "key": key,
                "priority": priority, "attempt": claim.attempt_count,
            },
        )
        try:
            await sender()
        except Exception as exc:  # noqa: BLE001
            delay_seconds = await fail_job_delivery(claim, exc)
            LOGGER.exception("Proactive delivery failed: %s", key)
            await emit_event(
                DB, user_id, taxonomy.DELIVERY_FAILED,
                entity="proactive_job", entity_id=key,
                source="job", surface="telegram", status="failed",
                outcome=type(exc).__name__,
                properties={
                    "operation": "proactive_job", "key": key,
                    "error_type": type(exc).__name__,
                    "retry_scheduled": bool(
                        retry_callback is not None
                        and claim.attempt_count < SETTINGS.proactive_max_attempts
                    ),
                },
            )
            await notify_admin(
                context.bot,
                f"Proactive delivery failed ({key}): {exc!r}",
            )
            if retry_callback is not None and claim.attempt_count < SETTINGS.proactive_max_attempts:
                schedule_job_retry(
                    context,
                    retry_callback,
                    key,
                    delay_seconds,
                )
            return False

        await complete_job_delivery(claim)
        await emit_event(
            DB, user_id, taxonomy.DELIVERY_SUCCEEDED,
            entity="proactive_job", entity_id=key,
            source="job", surface="telegram", status="succeeded",
            outcome="delivered",
            properties={"operation": "proactive_job", "key": key},
        )
    return True


@runtime_bound(RUNTIME_NAMES)
async def today_consumed(user_id: int) -> tuple[float, float]:
    return await daily_state.consumed_totals(DB, user_id)


@runtime_bound(RUNTIME_NAMES)
async def today_meal_items(user_id: int) -> list[dict[str, Any]]:
    return await daily_state.consumed_meal_items(DB, user_id)


@runtime_bound(RUNTIME_NAMES)
async def workout_completed_today(user_id: int) -> bool:
    """True only if a workout was actually done today — in the bot or imported.

    Distinct from "today is a usual training day" (a probabilistic guess) so
    callers don't conflate "already trained" with "tends to train".
    """
    return await daily_state.workout_completed_today(DB, user_id)


@runtime_bound(RUNTIME_NAMES)
async def is_usual_workout_day(user_id: int) -> bool:
    """True if today is a weekday the user typically trains on (a guess)."""
    profile = await load_routine_profile(user_id)
    weekday = datetime.now(TZ).weekday()
    return weekday in (profile.get("workout", {}).get("common_weekdays") or [])


@runtime_bound(RUNTIME_NAMES)
async def today_has_workout(user_id: int) -> bool:
    """Workout context for *recommendations* (already done OR a usual day).

    Used by meal builders to bias toward a training-day macro split. For "what
    to do now", use ``workout_completed_today`` instead.
    """
    return await workout_completed_today(user_id) or await is_usual_workout_day(user_id)


@runtime_bound(RUNTIME_NAMES)
def hours_left_until_sleep(profile: dict[str, Any]) -> float:
    bedtime = (profile.get("sleep", {}) or {}).get("typical_bedtime")
    now = datetime.now(TZ)
    if not bedtime:
        end = now.replace(hour=23, minute=0, second=0, microsecond=0)
    else:
        hh, mm = (int(x) for x in bedtime.split(":"))
        end = now.replace(hour=hh % 24, minute=mm, second=0, microsecond=0)
        if end <= now:
            end = end + timedelta(days=1)
    return min(18.0, max(0.5, (end - now).total_seconds() / 3600.0))


@dataclass
class DailyContext:
    user_id: int
    now: datetime
    local_date: str
    # nutrition
    calories_consumed: float
    protein_consumed: float
    calorie_target: int
    protein_target: int
    calories_remaining: float
    protein_remaining: float
    hours_left: float
    # reported-today signals
    sleep_quality: str | None  # good | ok | bad | None
    fasting: bool
    medications_today: list[str]
    flags: dict[str, Any]
    active_constraints: list[dict[str, Any]]
    # workout
    workout_active: bool
    workout_completed: bool
    usual_workout_time: str | None
    is_usual_workout_day: bool
    # data provenance
    latest_health_date: str | None
    goal_computed: bool
    profile: dict[str, Any] = field(default_factory=dict)
    goal: dict[str, Any] = field(default_factory=dict)

    @property
    def workout_self_reported(self) -> bool:
        """FIX 39: True when the user tapped an explicit "I finished the
        workout" clarification today, regardless of whether ``workout_completed``
        (strict bot-session/HealthKit evidence) agrees. ``workout_completed``
        intentionally stays evidence-only (see daily_state.workout_completed_today's
        docstring) -- this property does not change that policy, it only
        exposes the self-report so a consumer CAN acknowledge it instead of
        flatly contradicting what the user just told the coach.
        """
        return str(self.flags.get("next_meal_workout_status") or "") in (
            "during", "completed",
        )

    @property
    def workout_cancelled_today(self) -> bool:
        """FIX 39: True when the user explicitly cancelled today's workout
        via the next-meal clarification buttons. Unlike a simple "not yet
        completed" state, a cancellation is a claim about the whole day, not
        an open question -- consumers (workout prompts, motivation nudges)
        should suppress "you still haven't worked out" framing entirely
        rather than nag about something the user already said is not
        happening today.
        """
        return str(self.flags.get("next_meal_workout_status") or "") == "cancelled"


@runtime_bound(RUNTIME_NAMES)
async def build_daily_context(user_id: int) -> DailyContext:
    """Build the shared daily snapshot once. Consumers read from this instead of
    each querying the DB separately."""
    now = datetime.now(TZ)
    profile = await load_routine_profile(user_id)
    goal = await fetch_goal(user_id)
    flags = await get_daily_flags(user_id)
    calories, protein = await today_consumed(user_id)
    constraints = await active_constraints(user_id)
    session = await active_session(user_id)
    latest = await DB.fetch_one(
        "SELECT MAX(start_time) AS d FROM health WHERE user_id=?", (user_id,)
    )
    cal_target = int(goal["calories"])
    pro_target = int(goal["protein"])
    return DailyContext(
        user_id=user_id,
        now=now,
        local_date=now.date().isoformat(),
        calories_consumed=calories,
        protein_consumed=protein,
        calorie_target=cal_target,
        protein_target=pro_target,
        calories_remaining=max(0.0, cal_target - calories),
        protein_remaining=max(0.0, pro_target - protein),
        hours_left=hours_left_until_sleep(profile),
        sleep_quality=flags.get("sleep_quality"),
        fasting=bool(flags.get("fasting")),
        medications_today=flags.get("medications", []),
        flags=flags,
        active_constraints=constraints,
        workout_active=session is not None,
        workout_completed=await workout_completed_today(user_id),
        usual_workout_time=(profile.get("workout") or {}).get("typical_hour"),
        is_usual_workout_day=await is_usual_workout_day(user_id),
        latest_health_date=(latest["d"][:10] if latest and latest["d"] else None),
        goal_computed=bool(goal.get("computed")),
        profile=profile,
        goal=goal,
    )
