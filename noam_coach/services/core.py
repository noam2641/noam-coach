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


# LOG-012 (P4 privacy): the audit trail is exported verbatim in the user's
# DSAR ZIP, so `details` must never carry raw free-text (medical notes, goal
# explanations, meal-correction prose, medication names). write_audit is the
# single choke point for every audit INSERT, so the redaction/allowlist policy
# lives here rather than being duplicated (and forgotten) at each call site.
#
# The allowlist is keyed by (action, entity) and names ONLY the bounded,
# structured keys that may survive — codes, ids, numeric metadata, booleans.
# Every other supplied key is DROPPED before storage. Unknown (action, entity)
# pairs fall through to a conservative default that keeps only scalar
# ints/bools/short ids, so a NEW call site that forgets to be careful still
# fails safe. Whatever survives the allowlist is additionally passed through
# the canonical `redact` backstop (defense in depth) before the INSERT.
_AUDIT_MAX_SCALAR_STR = 64

# Free-text medication names must be coded to a coarse category (OWNER
# DECISION): store the CATEGORY, never the raw drug name. We keep the taxonomy
# deliberately tiny — a stimulant bucket the coaching logic already special
# cases, plus a generic fallback — rather than inventing a medical ontology.
_STIMULANT_HINTS = ("ritalin", "ריטלין", "concerta", "adderall", "vyvanse", "elvanse")


def _medication_kind_code(name: Any) -> str:
    text = str(name or "").strip().lower()
    for hint in _STIMULANT_HINTS:
        if hint in text:
            return "stimulant"
    return "medication"


def _location_code(value: Any) -> str | None:
    """Coerce a free-text body-location note to a bounded code."""
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    buckets = {
        "knee": ("knee", "ברך", "ברכיים"),
        "back": ("back", "lower back", "גב", "גב תחתון"),
        "shoulder": ("shoulder", "כתף", "כתפיים"),
        "wrist": ("wrist", "hand", "שורש", "יד", "כף יד"),
        "ankle": ("ankle", "foot", "קרסול", "כף רגל"),
        "hip": ("hip", "ירך", "אגן"),
        "neck": ("neck", "צוואר"),
    }
    for code, hints in buckets.items():
        if any(hint in text for hint in hints):
            return code
    return "other"


def _scalar_only(value: Any) -> bool:
    """A conservative 'is this a bounded, non-free-text value' test."""
    if isinstance(value, bool) or value is None:
        return True
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, str):
        return len(value) <= _AUDIT_MAX_SCALAR_STR
    return False


# Per (action, entity): a mapping of stored_key -> extractor(details) -> value.
# Any key NOT produced here is dropped. `None` results are omitted from output.
def _pass(key: str):
    return lambda d: d.get(key)


_AUDIT_ALLOWLIST: dict[tuple[str, str], dict[str, Any]] = {
    # Saved-plan weekday realignment (A9 / W1-44). The before/after weekday sets
    # arrive as short sorted STRINGS ("0,2,4"), not lists: an unregistered or
    # list-valued detail is dropped by `_scalar_only` below without any error,
    # so a list here would vanish while its test still passed. `outcome` and
    # `reason` are bounded codes, never prose, and the plan payload never
    # appears -- an audit row must not carry the user's programme.
    ("realign_weekdays", "plan"): {
        "outcome": _pass("outcome"),
        "reason": _pass("reason"),
        "new_plan_id": _pass("new_plan_id"),
        "before_days": _pass("before_days"),
        "after_days": _pass("after_days"),
    },
    # Degraded-safety plan proposal/confirmation (A10). Every field here is
    # already a bounded scalar, so the `_scalar_only` fallback would preserve
    # them -- but only incidentally. Registering the pair makes that a stated
    # guarantee: it pins WHICH fields are allowed, so a later field carrying a
    # limitation string or a body region is dropped by rule rather than
    # surviving because nobody re-checked the fallback. The user's answer and
    # the affected body part must never reach an audit row.
    ("propose_degraded_plan", "plan"): {
        "outcome": _pass("outcome"),
        "safety_unknown": _pass("safety_unknown"),
        "gap_count": _pass("gap_count"),
        "approval_id": _pass("approval_id"),
        "mutation_outcome": _pass("mutation_outcome"),
    },
    # Goal approvals: keep the bounded targets; DROP the `explanation` prose.
    ("approve", "goal"): {
        "calories": _pass("calories"),
        "protein": _pass("protein"),
        "steps": _pass("steps"),
        "phase": _pass("phase"),
        "gv_id": _pass("gv_id"),
    },
    ("approve_provisional", "goal"): {
        "calories": _pass("calories"),
        "protein": _pass("protein"),
        "steps": _pass("steps"),
        "phase": _pass("phase"),
        "gv_id": _pass("gv_id"),
    },
    # Meal-text correction: keep revision + optional bounded code; DROP prose.
    ("meal_text_correction", "approval"): {
        "revision": _pass("revision"),
        "correction_kind_code": _pass("correction_kind_code"),
    },
    # Meal approve/edit: already bounded numbers + ids.
    ("approve", "meal"): {
        "approval_id": _pass("approval_id"),
        "calories": _pass("calories"),
        "protein": _pass("protein"),
        "carbs": _pass("carbs"),
        "fat": _pass("fat"),
    },
    ("edit", "meal"): {
        "approval_id": _pass("approval_id"),
        "calories": _pass("calories"),
        "protein": _pass("protein"),
        "carbs": _pass("carbs"),
        "fat": _pass("fat"),
    },
    # Safety alert: preserve investigation value as CODES; coerce location.
    ("safety_alert", "constraint"): {
        "constraint_id": _pass("constraint_id"),
        # accept either an already-coded kind or a raw `kind` short token
        "kind_code": lambda d: d.get("kind_code") or d.get("kind"),
        "location_code": lambda d: _location_code(
            d.get("location_code") if d.get("location_code") is not None else d.get("location")
        ),
    },
    # Daily flags: keep ONLY derived booleans; DROP the raw `text` note.
    ("daily_flags", "flags"): {
        "ritalin": _pass("ritalin"),
        "fasting": _pass("fasting"),
        "has_note": _pass("has_note"),
    },
    # Health import counts (numeric only).
    ("health_import", "health"): {
        "inserted": _pass("inserted"),
        "duplicates": _pass("duplicates"),
    },
    ("health_facts_activated", "health"): {
        "count": _pass("count"),
    },
    # Medication: store the CATEGORY code, never the raw drug name.
    ("medication", "med_event"): {
        "kind_code": lambda d: d.get("kind_code") or _medication_kind_code(d.get("name")),
    },
}


def _allowlist_audit_details(action: str, entity: str, details: dict[str, Any]) -> dict[str, Any]:
    spec = _AUDIT_ALLOWLIST.get((action, entity))
    if spec is not None:
        out: dict[str, Any] = {}
        for stored_key, extractor in spec.items():
            value = extractor(details)
            if value is not None:
                out[stored_key] = value
        return out
    # Unknown (action, entity): fail safe — keep only bounded scalars, drop
    # anything that looks like free text or a nested structure.
    return {key: value for key, value in details.items() if _scalar_only(value)}


@runtime_bound(RUNTIME_NAMES)
async def write_audit(
    user_id: int,
    action: str,
    entity: str,
    entity_id: Any = None,
    **details: Any,
) -> None:
    # Local import guards against an import cycle: observability may import
    # from services during startup wiring.
    from noam_coach.observability.redaction import redact

    safe_details = _allowlist_audit_details(action, entity, details)
    # Defense in depth: even allowlisted values pass through the canonical
    # redactor (bounds strings, strips known secret shapes, rejects binary).
    safe_details = redact(safe_details)
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
            json.dumps(safe_details, ensure_ascii=False),
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
