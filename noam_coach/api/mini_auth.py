# ruff: noqa: F401, F811, F821, I001
"""Mini App token and public URL services.

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

RUNTIME_NAMES = ('ContextTypes', 'DB', 'Exception', 'InlineKeyboardButton', 'InlineKeyboardMarkup', 'SETTINGS', 'TypeError', 'UnicodeDecodeError', 'Update', 'ValueError', 'WebAppInfo', '_BLOCKED_HOSTS', '_LOCAL_HOSTS', '_is_valid_public_url', '_mini_secret', 'aiosqlite', 'base', 'base64', 'body', 'bool', 'bytes', 'conn', 'digest', 'ensure_user', 'expected', 'expiry', 'expiry_str', 'expiry_text', 'hashlib', 'hmac', 'host', 'int', 'is_allowed', 'len', 'make_mini_token', 'mini_app_url', 'now', 'padded', 'parsed', 'payload', 'purpose', 'secret', 'sig', 'str', 'time', 'token', 'token_purpose', 'track_event', 'ttl_seconds', 'update', 'url', 'urlparse', 'user_id', 'user_id_str', 'utc_now', 'verify_mini_token')


@runtime_bound(RUNTIME_NAMES)
def _mini_secret() -> bytes:
    secret = SETTINGS.mini_app_secret or SETTINGS.telegram_bot_token
    return secret.encode()


@runtime_bound(RUNTIME_NAMES)
def make_mini_token(
    user_id: int,
    *,
    purpose: str,
    ttl_seconds: int,
) -> str:
    """Create a purpose-bound signed token for login or an HttpOnly session."""
    expiry = int(time.time()) + ttl_seconds
    payload = f"{user_id}.{expiry}.{purpose}"
    sig = hmac.new(_mini_secret(), payload.encode(), hashlib.sha256).hexdigest()
    body = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    return f"{body}.{sig}"


@runtime_bound(RUNTIME_NAMES)
def verify_mini_token(token: str, *, purpose: str) -> int | None:
    """Return the user id only for a valid, unexpired, purpose-bound token."""
    try:
        body, sig = token.rsplit(".", 1)
        padded = body + "=" * (-len(body) % 4)
        payload = base64.urlsafe_b64decode(padded.encode()).decode()
        expected = hmac.new(
            _mini_secret(),
            payload.encode(),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        user_id_str, expiry_str, token_purpose = payload.split(".", 2)
        if token_purpose != purpose or int(expiry_str) < int(time.time()):
            return None
        return int(user_id_str)
    except Exception:  # noqa: BLE001
        return None


@runtime_bound(RUNTIME_NAMES)
async def consume_mini_login_token(token: str) -> int | None:
    """Persistently consume a signed login token exactly once.

    The token hash is stored in SQLite, so a process restart cannot make an
    already-used login link valid again.
    """
    user_id = verify_mini_token(token, purpose="login")
    if user_id is None:
        return None
    try:
        body, _sig = token.rsplit(".", 1)
        padded = body + "=" * (-len(body) % 4)
        payload = base64.urlsafe_b64decode(padded.encode()).decode()
        _uid, expiry_text, purpose = payload.split(".", 2)
        expiry = int(expiry_text)
    except (ValueError, TypeError, UnicodeDecodeError):
        return None
    digest = hashlib.sha256(token.encode()).hexdigest()
    now = utc_now()
    try:
        async with DB.transaction() as conn:
            await conn.execute(
                "DELETE FROM mini_login_tokens WHERE expires_at<?",
                (int(time.time()),),
            )
            await conn.execute(
                """
                INSERT INTO mini_login_tokens(
                    token_hash, user_id, purpose, expires_at, consumed_at, created_at
                ) VALUES(?, ?, ?, ?, ?, ?)
                """,
                (digest, user_id, purpose, expiry, now, now),
            )
    except aiosqlite.IntegrityError:
        return None
    return user_id


_BLOCKED_HOSTS = {"example.com", "coach.example.com"}


_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


@runtime_bound(RUNTIME_NAMES)
def _is_valid_public_url(url: str) -> bool:
    """Return whether Mini App access is usable in the current environment.

    Production accepts only a real HTTPS host.  Development also permits
    localhost over HTTP so the complete Mini App can be tested without buying
    a server.  A phone still needs an HTTPS tunnel because its localhost is not
    the developer's computer.
    """
    if not url:
        return False
    from urllib.parse import urlparse

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not host or host in _BLOCKED_HOSTS or host.endswith(".example.com"):
        return False
    if SETTINGS.app_env == "dev" and host in _LOCAL_HOSTS:
        return parsed.scheme in {"http", "https"}
    return parsed.scheme == "https"


@runtime_bound(RUNTIME_NAMES)
def mini_app_url(user_id: int) -> str | None:
    if not _is_valid_public_url(SETTINGS.public_base_url or ""):
        return None
    base = SETTINGS.public_base_url.rstrip("/")
    token = make_mini_token(
        user_id,
        purpose="login",
        ttl_seconds=SETTINGS.mini_app_login_ttl_seconds,
    )
    return f"{base}/mini/login?token={token}"


@runtime_bound(RUNTIME_NAMES)
async def command_app(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if not await is_allowed(update):
        return
    user_id = await ensure_user(update)
    await track_event(user_id, "command_app")
    url = mini_app_url(user_id)
    if not url:
        await update.effective_message.reply_text(
            "Mini App עדיין לא זמין.\n\n"
            "במחשב המקומי הפעל את השרת, פתח Cloudflare Tunnel אל "
            "http://127.0.0.1:8000, העתק את כתובת ה-HTTPS אל "
            "PUBLIC_BASE_URL בקובץ .env והפעל מחדש את הבוט.\n\n"
            "הוראות מלאות: docs/MINIAPP_SETUP.md"
        )
        return
    await update.effective_message.reply_text(
        "לחץ למטה לפתיחת Mini App.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🌐 פתח Mini App", web_app=WebAppInfo(url=url))],
        ]),
    )
