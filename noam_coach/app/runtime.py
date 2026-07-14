# ruff: noqa: F401, F811, F821, I001
"""Application scheduling, Telegram bootstrap and process runtime.

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

RUNTIME_NAMES = ('AIORateLimiter', 'Application', 'ApplicationBuilder', 'CallbackContext', 'CallbackQueryHandler', 'CommandHandler', 'DB', 'Exception', 'JOB_PRIORITY_COACHING', 'JOB_PRIORITY_SCHEDULED', 'LOGGER', 'MessageHandler', 'Path', 'RUNTIME_STATE', 'RuntimeError', 'SETTINGS', 'TZ', 'api', 'application', 'asyncio', 'build_telegram_app', 'build_weekly_summary_text', 'build_workout_prompt_text', 'builder', 'cleaner', 'cleanup_photos', 'command_app', 'command_cancel', 'command_chart', 'command_flags', 'command_import', 'command_import_path', 'command_profile', 'command_start', 'command_weekly', 'context', 'datetime', 'deliver_proactive_message', 'dttime', 'ensure_user_record', 'evening', 'exc', 'expected', 'filters', 'handle_callback', 'handle_document', 'handle_photo', 'handle_text_message', 'job_calorie_watch', 'job_evening', 'job_morning', 'job_motivation', 'job_weekly_summary', 'job_workout_prompt', 'jq', 'load_pending_state', 'me', 'morning', 'on_error', 'reconcile_onboarding_stage', 'restore_rest_timers_on_startup', 'schedule_jobs', 'send_prompt', 'send_to_user', 'send_weekly', 'server', 'suppress', 'telegram', 'text', 'type', 'user_id', 'uvicorn', 'verify_bot_identity')


@runtime_bound(RUNTIME_NAMES)
async def job_workout_prompt(context: CallbackContext) -> None:
    user_id = SETTINGS.telegram_allowed_user_id
    text = await build_workout_prompt_text(user_id)
    if not text:
        return

    async def send_prompt() -> None:
        await send_to_user(context, text)

    await deliver_proactive_message(
        context,
        key="workout_prompt",
        sender=send_prompt,
        priority=JOB_PRIORITY_COACHING,
        retry_callback=job_workout_prompt,
    )


@runtime_bound(RUNTIME_NAMES)
async def job_weekly_summary(context: CallbackContext) -> None:
    user_id = SETTINGS.telegram_allowed_user_id
    if datetime.now(TZ).weekday() != 5:
        return

    async def send_weekly() -> None:
        await send_to_user(
            context,
            await build_weekly_summary_text(user_id),
        )

    await deliver_proactive_message(
        context,
        key="weekly_summary",
        sender=send_weekly,
        priority=JOB_PRIORITY_SCHEDULED,
        retry_callback=job_weekly_summary,
    )


@runtime_bound(RUNTIME_NAMES)
def schedule_jobs(application: Application) -> None:
    jq = application.job_queue
    if jq is None:
        LOGGER.warning("JobQueue לא זמין — הודעות יזומות מושבתות")
        return
    morning = dttime(hour=8, minute=0, tzinfo=TZ)
    evening = dttime(hour=22, minute=0, tzinfo=TZ)
    jq.run_daily(job_morning, time=morning, name="morning")
    jq.run_daily(job_evening, time=evening, name="evening")
    jq.run_repeating(job_calorie_watch, interval=1800, first=300, name="calorie_watch")
    jq.run_repeating(job_motivation, interval=1800, first=900, name="motivation")
    jq.run_repeating(job_workout_prompt, interval=3600, first=1200, name="workout_prompt")
    jq.run_daily(
        job_weekly_summary,
        time=dttime(hour=20, minute=30, tzinfo=TZ),
        name="weekly_summary",
    )


@runtime_bound(RUNTIME_NAMES)
def build_telegram_app() -> Application:
    builder = (
        ApplicationBuilder()
        .token(SETTINGS.telegram_bot_token)
        .rate_limiter(AIORateLimiter())
        # Large health ZIPs need long read/write windows and a bigger pool so
        # downloading a file doesn't starve the polling connection.
        .read_timeout(SETTINGS.telegram_read_timeout)
        .write_timeout(SETTINGS.telegram_write_timeout)
        .connect_timeout(SETTINGS.telegram_connect_timeout)
        .pool_timeout(SETTINGS.telegram_pool_timeout)
        .connection_pool_size(SETTINGS.telegram_connection_pool_size)
        .get_updates_read_timeout(SETTINGS.telegram_get_updates_read_timeout)
        .get_updates_write_timeout(SETTINGS.telegram_get_updates_write_timeout)
        .get_updates_connect_timeout(SETTINGS.telegram_get_updates_connect_timeout)
        .get_updates_pool_timeout(SETTINGS.telegram_get_updates_pool_timeout)
        .get_updates_connection_pool_size(SETTINGS.telegram_get_updates_connection_pool_size)
    )
    # When a Local Bot API Server is configured we can receive files far larger
    # than the 50MB cloud limit (needed for the Apple Health ZIP).
    if SETTINGS.telegram_base_url:
        builder = builder.base_url(SETTINGS.telegram_base_url).local_mode(True)
        if SETTINGS.telegram_base_file_url:
            builder = builder.base_file_url(SETTINGS.telegram_base_file_url)

    application = builder.build()
    # Observability O2: every Telegram ingress boundary is wrapped with the
    # interaction envelope (interaction_id + trace scope + pre-routing flow
    # snapshot + interaction.received), and ConversationRouter.route is
    # observed caller-side — the pure policy itself stays untouched.
    from noam_coach.observability.ai_invocation import install_ai_observability
    from noam_coach.observability.telegram_egress import install_telegram_egress
    from noam_coach.observability.telegram_ingress import (
        install_routing_observer,
        observed_handler,
    )

    install_routing_observer()
    # Observability O3: render/delivery instrumentation for safe_edit,
    # FreeTextContext.send, send_to_user, and the ExtBot transport catch-all.
    install_telegram_egress()
    # Observability O4: observable OpenAI boundary + per-call-site purposes.
    install_ai_observability()
    for command_name, command_handler in (
        ("start", command_start),
        ("import", command_import),
        ("importpath", command_import_path),
        ("flags", command_flags),
        ("profile", command_profile),
        ("weekly", command_weekly),
        ("chart", command_chart),
        ("app", command_app),
        ("cancel", command_cancel),
    ):
        application.add_handler(
            CommandHandler(
                command_name,
                observed_handler("command", command_handler, command=command_name),
            )
        )
    application.add_handler(CallbackQueryHandler(observed_handler("callback", handle_callback)))
    application.add_handler(MessageHandler(filters.PHOTO, observed_handler("photo", handle_photo)))
    application.add_handler(
        MessageHandler(filters.Document.ALL, observed_handler("document", handle_document))
    )
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            observed_handler("text", handle_text_message),
        )
    )
    application.add_error_handler(on_error)
    schedule_jobs(application)
    return application


@runtime_bound(RUNTIME_NAMES)
async def verify_bot_identity(application: Application) -> None:
    """Log the running bot identity and fail fast on an unexpected username.

    Catches the classic mistake of pointing Noam Coach at the wrong/reused token
    (e.g. an unrelated bot). The token itself is never logged.
    """
    me = await application.bot.get_me()
    LOGGER.info("Telegram bot identity: @%s (id=%s)", me.username, me.id)
    expected = (SETTINGS.expected_bot_username or "").lstrip("@").casefold()
    if expected and (me.username or "").casefold() != expected:
        raise RuntimeError(
            f"זהות הבוט אינה תואמת: צפוי @{expected}, בפועל @{me.username}. "
            "ודא שאתה משתמש ב-TELEGRAM_BOT_TOKEN הנכון של Noam Coach."
        )


async def _stop_telegram_application(application: Application) -> None:
    updater = application.updater
    if updater is not None and getattr(updater, "running", False):
        with suppress(Exception):
            await updater.stop()
    if getattr(application, "running", False):
        with suppress(Exception):
            await application.stop()


async def _stop_api_server(server: uvicorn.Server) -> None:
    server.should_exit = True
    if getattr(server, "started", False):
        await asyncio.sleep(0)


async def _cancel_task(task: asyncio.Task[Any] | None) -> None:
    if task is None or task.done():
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


@runtime_bound(RUNTIME_NAMES)
async def run() -> None:
    SETTINGS.validate_runtime()
    Path(SETTINGS.storage_dir).mkdir(parents=True, exist_ok=True)
    try:
        await DB.init()
        await ensure_user_record(SETTINGS.telegram_allowed_user_id)
        await load_pending_state()  # restore mid-flow conversation state
        await reconcile_onboarding_stage(SETTINGS.telegram_allowed_user_id)
        RUNTIME_STATE.db_ready = True
        RUNTIME_STATE.startup_error = None
    except Exception as exc:
        RUNTIME_STATE.startup_error = f"{type(exc).__name__}: {exc}"
        raise

    telegram = build_telegram_app()
    server = uvicorn.Server(
        uvicorn.Config(
            api,
            host=SETTINGS.host,
            port=SETTINGS.port,
            loop="asyncio",
            log_level="info",
            access_log=False,
        )
    )
    cleaner = asyncio.create_task(cleanup_photos())

    RUNTIME_STATE.shutting_down = False
    try:
        async with telegram:
            try:
                await telegram.start()
                # Fail fast if the token belongs to the wrong bot (identity guard).
                await verify_bot_identity(telegram)
                if getattr(telegram, "job_queue", None) is not None:
                    # FIX 45: rest timers live only in the in-memory JobQueue,
                    # keyed off time.monotonic() -- a restart destroys them
                    # while the durable session silently advances. Restore
                    # or resolve every persisted timer before accepting
                    # traffic so no card is left frozen/abandoned.
                    with suppress(Exception):
                        restored = await restore_rest_timers_on_startup(telegram.job_queue)
                        if restored:
                            LOGGER.info("Restored %d rest timer(s) after restart", restored)
                if telegram.updater is None:
                    raise RuntimeError("Telegram updater is not available")
                await telegram.updater.start_polling(drop_pending_updates=False)
                RUNTIME_STATE.telegram_ready = True
                LOGGER.info("Telegram bot and API are running")
                await server.serve()
            finally:
                RUNTIME_STATE.shutting_down = True
                RUNTIME_STATE.telegram_ready = False
                await _stop_api_server(server)
                await _stop_telegram_application(telegram)
    finally:
        RUNTIME_STATE.shutting_down = True
        RUNTIME_STATE.telegram_ready = False
        RUNTIME_STATE.db_ready = False
        await _cancel_task(cleaner)
        RUNTIME_STATE.shutting_down = False
