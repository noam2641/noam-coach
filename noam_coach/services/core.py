# ruff: noqa: F401, F811, F821, I001
"""Core state, approvals, audit and user services.

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

RUNTIME_NAMES = ('Any', 'DB', 'Exception', 'RuntimeError', 'SETTINGS', 'Update', 'action', 'admin_chat_id', 'approval_id', 'bot', 'connection', 'conversation', 'current', 'details', 'dict', 'ensure_user_record', 'entity', 'entity_id', 'event', 'event_log', 'first_name', 'flow', 'int', 'json', 'kind', 'now', 'payload', 'properties', 'refine_count', 'restored', 'row', 'secrets', 'status', 'step', 'str', 'suppress', 'telegram_file_unique_id', 'text', 'tuple', 'update', 'user', 'user_id', 'username', 'utc_now')


@runtime_bound(RUNTIME_NAMES)
def admin_chat_id() -> int:
    return int(SETTINGS.admin_chat_id or SETTINGS.telegram_allowed_user_id)


@runtime_bound(RUNTIME_NAMES)
async def track_event(user_id: int, event: str, **properties: Any) -> None:
    """Record analytics and an immutable replay event without breaking UX."""
    with suppress(Exception):
        await DB.execute(
            """
            INSERT INTO analytics_events(user_id, event, properties, created_at)
            VALUES(?, ?, ?, ?)
            """,
            (user_id, event, json.dumps(properties, ensure_ascii=False), utc_now()),
        )
    with suppress(Exception):
        flow = await conversation.get_active_flow(DB, user_id)
        await event_log.append_event(
            DB,
            user_id,
            event.upper(),
            entity=str(properties.pop("entity", "conversation")),
            entity_id=properties.pop("entity_id", None),
            flow_id=flow.flow_id or None,
            flow_version=flow.version,
            properties=properties,
        )


@runtime_bound(RUNTIME_NAMES)
async def set_flow_state(
    user_id: int,
    flow: str,
    step: str,
    payload: dict[str, Any] | None = None,
) -> None:
    await DB.execute(
        """
        INSERT INTO conversation_state(user_id, flow, step, payload, updated_at)
        VALUES(?, ?, ?, ?, ?)
        ON CONFLICT(user_id, flow) DO UPDATE SET
            step=excluded.step,
            payload=excluded.payload,
            updated_at=excluded.updated_at
        """,
        (
            user_id,
            flow,
            step,
            json.dumps(payload or {}, ensure_ascii=False),
            utc_now(),
        ),
    )


@runtime_bound(RUNTIME_NAMES)
async def get_flow_state(user_id: int, flow: str) -> dict[str, Any] | None:
    row = await DB.fetch_one(
        "SELECT step, payload, updated_at FROM conversation_state WHERE user_id=? AND flow=?",
        (user_id, flow),
    )
    if not row:
        return None
    row["payload"] = json.loads(row["payload"]) if row.get("payload") else {}
    return row


@runtime_bound(RUNTIME_NAMES)
async def clear_flow_state(user_id: int, flow: str) -> None:
    await DB.execute(
        "DELETE FROM conversation_state WHERE user_id=? AND flow=?",
        (user_id, flow),
    )


@runtime_bound(RUNTIME_NAMES)
async def set_meal_fix(user_id: int, approval_id: str, refine_count: int = 0) -> None:
    """Open/update the meal-correction microflow in the unified engine."""
    current = await conversation.get_active_flow(DB, user_id)
    if current.name == conversation.FlowName.meal_correction and current.step == approval_id:
        await conversation.update_flow(
            DB, user_id, payload_patch={"refine_count": refine_count}
        )
        return
    await conversation.set_active_flow(
        DB,
        user_id,
        conversation.FlowName.meal_correction,
        step=approval_id,
        payload={"refine_count": refine_count},
        suspend_current=not current.is_idle,
    )


@runtime_bound(RUNTIME_NAMES)
async def get_meal_fix(user_id: int) -> tuple[str | None, int]:
    flow = await conversation.expire_if_needed(DB, user_id)
    if flow.name != conversation.FlowName.meal_correction:
        return None, 0
    return flow.step or None, int(flow.payload.get("refine_count", 0))


@runtime_bound(RUNTIME_NAMES)
async def clear_meal_fix(user_id: int) -> None:
    current = await conversation.get_active_flow(DB, user_id)
    if current.name != conversation.FlowName.meal_correction:
        return
    restored = await conversation.resume_suspended(DB, user_id)
    if restored is None:
        await conversation.clear_active_flow(DB, user_id)


@runtime_bound(RUNTIME_NAMES)
async def clear_meal_fix_for(user_id: int, approval_id: str) -> bool:
    """Entity-addressed meal-flow completion (audit F-A5).

    A decision on meal B must never close meal A's flow: the active
    meal_correction flow is cleared (resuming any suspended flow) ONLY when
    its step is exactly the decided approval id. Returns True when the flow
    was cleared/resumed, False when the active flow belongs to a different
    meal and was deliberately left untouched.
    """
    current = await conversation.get_active_flow(DB, user_id)
    if current.name != conversation.FlowName.meal_correction:
        return False
    if (current.step or "") != str(approval_id):
        return False
    restored = await conversation.resume_suspended(DB, user_id)
    if restored is None:
        await conversation.clear_active_flow(DB, user_id)
    return True


@runtime_bound(RUNTIME_NAMES)
async def notify_admin(bot: Any, text: str) -> None:
    with suppress(Exception):
        await bot.send_message(chat_id=admin_chat_id(), text=text)


@runtime_bound(RUNTIME_NAMES)
async def write_audit(
    user_id: int,
    action: str,
    entity: str,
    entity_id: Any = None,
    **details: Any,
) -> None:
    await DB.execute(
        """
        INSERT INTO audit(user_id, action, entity, entity_id, details, created_at)
        VALUES(?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            action,
            entity,
            str(entity_id) if entity_id is not None else None,
            json.dumps(details, ensure_ascii=False),
            utc_now(),
        ),
    )


@runtime_bound(RUNTIME_NAMES)
async def ensure_user_record(
    user_id: int,
    first_name: str | None = None,
    username: str | None = None,
) -> None:
    """Ensure FK parent rows exist for Telegram, HealthKit and Watch entrypoints."""
    now = utc_now()
    async with DB.transaction() as connection:
        await connection.execute(
            """
            INSERT INTO users(id, first_name, username, updated_at)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                first_name=COALESCE(excluded.first_name, users.first_name),
                username=COALESCE(excluded.username, users.username),
                updated_at=excluded.updated_at
            """,
            (user_id, first_name, username, now),
        )
        # LOG-016: the legacy `goals` table is frozen by derivation. Its sole
        # live read path is `goal_versions`; `goals` has no product readers.
        # We no longer default-insert a goals row here (it would only produce a
        # stale value that leaks into the DSAR export). No row is required —
        # `goals` is a child of `users` and parent of nothing.


@runtime_bound(RUNTIME_NAMES)
async def ensure_user(update: Update) -> int:
    user = update.effective_user
    if not user:
        raise RuntimeError("משתמש Telegram לא נמצא")

    await ensure_user_record(
        user.id,
        first_name=user.first_name,
        username=user.username,
    )
    return user.id


@runtime_bound(RUNTIME_NAMES)
async def create_approval(
    user_id: int,
    kind: str,
    payload: dict[str, Any],
    *,
    telegram_file_unique_id: str | None = None,
) -> str:
    approval_id = secrets.token_urlsafe(8)
    await DB.execute(
        """
        INSERT INTO approvals(id, user_id, kind, payload, status, created_at, telegram_file_unique_id)
        VALUES(?, ?, ?, ?, 'pending', ?, ?)
        """,
        (
            approval_id,
            user_id,
            kind,
            json.dumps(payload, ensure_ascii=False),
            utc_now(),
            telegram_file_unique_id,
        ),
    )
    return approval_id


@runtime_bound(RUNTIME_NAMES)
async def fetch_approval(
    user_id: int,
    approval_id: str,
) -> dict[str, Any] | None:
    row = await DB.fetch_one(
        """
        SELECT *
        FROM approvals
        WHERE id=? AND user_id=? AND status='pending'
        """,
        (approval_id, user_id),
    )
    if row:
        row["data"] = json.loads(row.pop("payload"))
    return row


@runtime_bound(RUNTIME_NAMES)
async def fetch_approval_any(
    user_id: int,
    approval_id: str,
) -> dict[str, Any] | None:
    """Fetch an approval REGARDLESS of status (audit F-A1).

    ``fetch_approval`` deliberately returns only pending rows; the approval
    lifecycle also needs to see decided rows so a press on a stale card can
    be answered truthfully ("already saved" / "already rejected — restore?")
    instead of being treated as nonexistent.
    """
    row = await DB.fetch_one(
        "SELECT * FROM approvals WHERE id=? AND user_id=?",
        (approval_id, user_id),
    )
    if row:
        row["data"] = json.loads(row.pop("payload"))
    return row


@runtime_bound(RUNTIME_NAMES)
async def decide_approval(approval_id: str, status: str) -> None:
    await DB.execute(
        """
        UPDATE approvals
        SET status=?, decided_at=?
        WHERE id=? AND status='pending'
        """,
        (status, utc_now(), approval_id),
    )
