# ruff: noqa: F401, F811, F821, I001
"""Meal-correction and primary text message ownership.

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

RUNTIME_NAMES = ('ContextTypes', 'DB', 'Exception', 'InlineKeyboardMarkup', 'LOGGER', 'MealAnalysis', 'RuntimeError', 'Update', '_CANCEL_WORDS', '_handle_meal_correction_text', 'approval_id', 'button', 'candidate_ids', 'clear_meal_fix', 'conversation', 'corrected_analysis', 'correction_text', 'count', 'decision', 'ensure_user', 'event_log', 'exc', 'fetch_approval', 'flow', 'friendly_error', 'get_meal_fix', 'handle_onboarding_text', 'home_keyboard', 'image_path', 'index', 'int', 'is_allowed', 'json', 'len', 'list', 'original_analysis', 'pc', 'planning', 'prior_locked', 'progress', 'rc', 'reanalyze_meal_with_text_and_image', 'refine_count', 'removal_corrections', 'render_meal', 'route_free_text', 'row', 'selected', 'set_meal_fix', 'str', 'suppress', 'text', 'track_event', 'update', 'user_id', 'write_audit')


@runtime_bound(RUNTIME_NAMES)
async def _handle_meal_correction_text(
    update: Update,
    user_id: int,
    approval_id: str,
    refine_count: int,
) -> None:
    row = await fetch_approval(user_id, approval_id)
    if not row:
        await clear_meal_fix(user_id)
        await route_free_text(update, user_id)
        return

    correction_text = (update.effective_message.text or "").strip()
    if not correction_text:
        await update.effective_message.reply_text("לא התקבל טקסט לתיקון.")
        return
    if correction_text in _CANCEL_WORDS:
        await clear_meal_fix(user_id)
        await update.effective_message.reply_text(
            "תיקון הארוחה בוטל. חזרתי לתהליך הקודם.",
            reply_markup=home_keyboard(),
        )
        return

    progress = await update.effective_message.reply_text(
        "מעדכן את הארוחה…"
    )
    try:
        original_analysis = MealAnalysis.model_validate(row["data"]["analysis"])
        image_path = row["data"].get("image")

        # --- Deterministic correction first (REC-MEAL-01 / REC-PLAN-MEAL-03-12) ---
        corrections = meal_intelligence.parse_meal_correction(correction_text)
        removal_corrections = [c for c in corrections if c.kind == "remove"]
        prep_corrections = [c for c in corrections if c.kind == "preparation"]
        qty_corrections = [c for c in corrections if c.kind == "quantity"]

        used_deterministic = False
        corrected_analysis = original_analysis

        if removal_corrections:
            # Apply item-removal deterministically (e.g. "בלי שמן")
            for rc in removal_corrections:
                corrected_analysis = meal_intelligence.apply_item_removal_correction(
                    corrected_analysis, rc,
                )
            used_deterministic = True

        if prep_corrections:
            # Apply preparation changes deterministically without AI
            for pc in prep_corrections:
                corrected_analysis = meal_intelligence.apply_preparation_correction(
                    corrected_analysis, pc,
                )
            used_deterministic = True

        if qty_corrections:
            # Apply quantity changes from parse_locked_quantities
            locked = meal_intelligence.parse_locked_quantities(correction_text)
            if locked:
                corrected_analysis, _unmatched = meal_intelligence.apply_locked_quantities(
                    corrected_analysis, locked,
                )
                used_deterministic = True

        if not used_deterministic:
            # Fall back to AI reanalysis only when deterministic parser
            # did not recognize the correction.
            # Pass all previously locked corrections so the AI respects them
            # even when re-analysing from scratch (REC-PLAN-MEAL-03-12).
            if not image_path:
                raise RuntimeError("לא נמצאה תמונת מקור לניתוח חוזר")
            await progress.edit_text("מנתח מחדש את התמונה לפי מה שכתבת…")
            prior_locked: list[str] = list(row["data"].get("locked_corrections") or [])
            corrected_analysis = await reanalyze_meal_with_text_and_image(
                image_path=image_path,
                correction_text=correction_text,
                locked_corrections=prior_locked,
            )

        corrected_analysis.notes = (
            original_analysis.notes
            + corrected_analysis.notes
            + [f"תיקון: {correction_text}"]
        )[-10:]

        # Bump revision in payload
        revision = row["data"].get("revision", 0) + 1
        row["data"]["analysis"] = corrected_analysis.model_dump()
        row["data"]["revision"] = revision
        row["data"].setdefault("locked_corrections", []).append(correction_text)
        await DB.execute(
            "UPDATE approvals SET payload=? WHERE id=? AND user_id=? AND status='pending'",
            (json.dumps(row["data"], ensure_ascii=False), approval_id, user_id),
        )
        await write_audit(
            user_id,
            "meal_text_correction",
            "approval",
            approval_id,
            correction_text=correction_text,
        )
        await event_log.append_event(
            DB,
            user_id,
            "meal_correction_applied",
            entity="approval",
            entity_id=approval_id,
            source="deterministic" if used_deterministic else "ai",
            properties={
                "text": correction_text,
                "revision": revision,
                "deterministic": used_deterministic,
            },
        )
        count = refine_count + 1
        await set_meal_fix(user_id, approval_id, count)
        await render_meal(progress, user_id, approval_id, refine_count=count)
    except Exception as exc:  # noqa: BLE001
        LOGGER.exception("Meal correction failed")
        with suppress(Exception):
            await event_log.append_event(
                DB, user_id, "meal_correction_error",
                entity="approval", entity_id=approval_id,
                source="system",
                properties={"error": str(exc)[:200], "context": "meal correction", "correction_text": correction_text[:100]},
            )
        # REC-MEAL-03: keep previous analysis, show recovery options
        await progress.edit_text(
            "לא הצלחתי לעדכן את הארוחה. הניתוח הקודם נשמר.\n"
            "אפשר לנסות שוב או לחזור לארוחה.",
            reply_markup=InlineKeyboardMarkup([
                [button("🔄 נסה שוב", f"backmeal:{approval_id}")],
                [button("⬅️ חזרה לארוחה", f"backmeal:{approval_id}")],
            ]),
        )


@runtime_bound(RUNTIME_NAMES)
async def handle_text_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not await is_allowed(update):
        return

    user_id = await ensure_user(update)
    text = (update.effective_message.text or "").strip()

    # A local Windows path is handled before intent classification. This only
    # works when the bot process runs on the same computer that owns the path.
    from noam_coach.services.health_jobs import try_handle_local_health_path

    if await try_handle_local_health_path(update, context, user_id, text):
        return

    await track_event(user_id, "USER_MESSAGE", text_length=len(text))
    decision = await conversation.ConversationRouter.route(DB, user_id, "text")

    if decision.handler == "meal_flow":
        approval_id, refine_count = await get_meal_fix(user_id)
        if approval_id:
            await _handle_meal_correction_text(
                update,
                user_id,
                approval_id,
                refine_count,
            )
            return
        await conversation.clear_active_flow(DB, user_id)

    if decision.handler == "question_flow":
        if await handle_onboarding_text(update, user_id):
            return
        # A stale question flow must not trap the user.
        await conversation.clear_active_flow(DB, user_id)

    if decision.handler == "selection_flow":
        # REC-PLAN-MEAL-03-03: Only consume exact selection input (1/2/3).
        # General questions like "מה עם הארוחות?" must fall through to
        # the intent router, not be captured by stale plan selection.
        if text in {"1", "2", "3"}:
            flow = await conversation.get_active_flow(DB, user_id)
            candidate_ids = flow.payload.get("candidate_ids") or []
            index = int(text) - 1
            if 0 <= index < len(candidate_ids):
                try:
                    selected = await planning.activate_plan(DB, user_id, int(candidate_ids[index]))
                except planning.PlanningBlockedError as exc:
                    await update.effective_message.reply_text(
                        f"עוד אי אפשר להפעיל את התוכנית. {exc}",
                        reply_markup=home_keyboard(),
                    )
                    return
                if selected:
                    await conversation.clear_active_flow(DB, user_id)
                    await event_log.append_event(
                        DB, user_id, "proposal_selected",
                        entity="plan", entity_id=str(candidate_ids[index]),
                        source="user_text",
                    )
                    await update.effective_message.reply_text(
                        f"{selected['title']} נבחרה כתוכנית הראשית ✅",
                        reply_markup=home_keyboard(),
                    )
                    return
            await update.effective_message.reply_text(
                "אפשר לבחור אחת מההצעות בכפתורים, או לכתוב 1, 2 או 3.",
            )
            return
        # Not a valid selection number — release to intent router
        # so meal questions, status requests etc. are handled normally.
        await event_log.append_event(
            DB, user_id, "unrelated_message_released_to_intent_router",
            entity="plan_selection", source="router",
            properties={"text_preview": text[:60]},
        )

    # Workout text and ordinary free text both go through the natural-language
    # intent router, but only after the conversation engine assigned ownership.
    await route_free_text(update, user_id)
