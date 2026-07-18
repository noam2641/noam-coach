# ruff: noqa: F401, F811, F821, I001
"""Meal photo, correction, persistence and rendering flows.

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
from noam_coach.services.nutrition_context import (
    build_nutrition_ai_request,
    build_nutrition_context,
)
from noam_coach.services.meal_validation import (
    MealValidationResult,
    validate_meal_analysis,
    validate_meal_analysis_for_user,
)

RUNTIME_NAMES = ('Any', 'ContextTypes', 'DB', 'Exception', 'FoodItem', 'InlineKeyboardButton', 'InlineKeyboardMarkup', 'LOGGER', 'MealAnalysis', 'ParseMode', 'Path', 'SETTINGS', 'Update', 'ValueError', '_AlreadyDecided', '_dt', '_item_line', 'abs', 'allergies_val', 'analysis', 'analyze_meal_image', 'any', 'approval', 'approval_id', 'asyncio', 'auto_save_meal', 'bool', 'button', 'bytes', 'cal', 'caption', 'clear_meal_fix', 'conn', 'conversation', 'count', 'create_approval', 'cur', 'cursor', 'cutoff', 'data_quality', 'datetime', 'decide_approval', 'dict', 'diet_restrictions', 'diff', 'duplicate_approval_id', 'duplicate_id', 'edit_meal_id', 'edited_existing', 'ensure_user', 'enumerate', 'esc', 'event_log', 'exc', 'fetch_approval', 'file_unique_id', 'float', 'folder', 'friendly_error', 'handed_off', 'hasattr', 'high', 'home_keyboard', 'i', 'image_bytes', 'image_path', 'index', 'int', 'is_allowed', 'item', 'item_count', 'item_index', 'item_name_lower', 'items', 'json', 'keyboard', 'line', 'list', 'low', 'macro_cal', 'macro_diff', 'max', 'meal', 'meal_id', 'meal_intelligence', 'message', 'now', 'option', 'option_rows', 'path', 'payload', 'pending_dup', 'persist_meal', 'photo_obj', 'progress', 'query', 'range', 'reanalyze_meal_with_text_and_image', 'recent', 'refine_count', 'refine_hint', 'render_meal', 'report', 'restriction', 'restriction_block', 'restriction_lower', 'restriction_warnings', 'restricted_items', 'row', 'rows', 'safe_edit', 'saved_dup', 'secrets', 'set_meal_fix', 'should_auto_approve', 'str', 'suppress', 'target', 'telegram_file', 'telegram_file_unique_id', 'text', 'timedelta', 'timezone', 'totals', 'update', 'user_id', 'user_model', 'utc_now', 'write_audit')


async def _meal_analysis_context(user_id: int, purpose: str, request: str) -> dict[str, Any] | None:
    """Build the one canonical nutrition context used by meal analysis paths."""
    with suppress(Exception):
        return build_nutrition_ai_request(
            await build_nutrition_context(DB, user_id, purpose),
            request,
        )["context"]
    return None


@runtime_bound(RUNTIME_NAMES)
async def create_meal_edit_approval(user_id: int, meal_id: int) -> str | None:
    """Create a new editable approval from an already-saved meal."""
    meal = await DB.fetch_one(
        "SELECT * FROM meals WHERE id=? AND user_id=?",
        (meal_id, user_id),
    )
    if not meal:
        return None
    items = await DB.fetch_all(
        "SELECT * FROM meal_items WHERE meal_id=? ORDER BY id",
        (meal_id,),
    )
    if not items:
        return None
    analysis = MealAnalysis(
        meal_name=meal["name"],
        items=[
            FoodItem(
                name=item["name"],
                grams=float(item["grams"]),
                calories=float(item["calories"]),
                protein=float(item["protein"]),
                carbs=float(item["carbs"]),
                fat=float(item["fat"]),
                confidence=float(item["confidence"]),
            )
            for item in items
        ],
        confidence=float(meal["confidence"]),
        notes=["עריכת ארוחה שמורה"],
    )
    return await create_approval(
        user_id,
        "meal_edit",
        {
            "analysis": analysis.model_dump(),
            "image": meal.get("image_path"),
            "eaten_at": meal["eaten_at"],
            "edit_meal_id": meal_id,
        },
    )


@runtime_bound(RUNTIME_NAMES)
async def analyze_duplicate_candidate(
    target: Any,
    user_id: int,
    duplicate_approval_id: str,
) -> None:
    """Analyze an intentionally repeated photo as a separate new meal."""
    row = await fetch_approval(user_id, duplicate_approval_id)
    if not row or row.get("kind") != "meal_duplicate":
        await safe_edit(target, "הטיוטה כבר אינה זמינה.", home_keyboard())
        return
    payload = row["data"]
    image_path = payload.get("image")
    if not image_path or not Path(image_path).exists():
        await decide_approval(duplicate_approval_id, "rejected")
        await safe_edit(target, "תמונת המקור כבר אינה זמינה.", home_keyboard())
        return
    image_bytes = await asyncio.to_thread(Path(image_path).read_bytes)
    caption = str(payload.get("caption") or "").strip()
    nutrition_payload = await _meal_analysis_context(
        user_id,
        "meal_photo_caption" if caption else "meal_photo",
        "Analyze duplicate meal photo with user caption" if caption else "Analyze duplicate meal photo",
    )
    # TASK-15: caption guides full image analysis; never replaces it.
    analysis = await analyze_meal_image(
        image_bytes,
        user_id=user_id,
        nutrition_context=nutrition_payload,
        caption=caption or None,
    )
    approval_id = await create_approval(
        user_id,
        "meal",
        {
            "analysis": analysis.model_dump(),
            "image": image_path,
            "eaten_at": payload.get("eaten_at") or utc_now(),
            "duplicate_override": True,
        },
        telegram_file_unique_id=payload.get("telegram_file_unique_id"),
    )
    await decide_approval(duplicate_approval_id, "approved")
    await render_meal(target, user_id, approval_id)
    await set_meal_fix(user_id, approval_id, 0)


@runtime_bound(RUNTIME_NAMES)
async def handle_photo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not await is_allowed(update):
        return

    user_id = await ensure_user(update)
    message = update.effective_message
    caption = (message.caption or "").strip()
    progress = await message.reply_text("📷 התמונה בתהליך ניתוח…")

    path: Path | None = None
    handed_off = False
    try:
        await conversation.ConversationRouter.route(DB, user_id, "photo")
        photo_obj = message.photo[-1]
        file_unique_id = photo_obj.file_unique_id
        telegram_file = await photo_obj.get_file()
        image_bytes = bytes(await telegram_file.download_as_bytearray())

        folder = Path(SETTINGS.storage_dir) / "food"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (
            f"{user_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(3)}.jpg"
        )
        # Audit F-A9: photo persistence is one evidenced operation — the
        # storage path, content hash and provider id are recorded, so a
        # later 'missing image' is always explainable (classify_missing_image).
        from noam_coach.services.media_persistence import persist_meal_photo

        await persist_meal_photo(
            user_id, image_bytes, path, provider_file_unique_id=file_unique_id,
        )

        # Detect both exact Telegram duplicates and visually similar saved meals.
        pending_dup = await DB.fetch_one(
            """
            SELECT id, status FROM approvals
            WHERE user_id=? AND telegram_file_unique_id=?
              AND created_at>=? AND kind IN ('meal','meal_edit')
            ORDER BY created_at DESC LIMIT 1
            """,
            (
                user_id,
                file_unique_id,
                (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat(),
            ),
        )
        saved_dup = await meal_intelligence.find_image_duplicate(
            DB,
            user_id=user_id,
            image_bytes=image_bytes,
            telegram_file_unique_id=file_unique_id,
        )
        if pending_dup or saved_dup:
            duplicate_id = await create_approval(
                user_id,
                "meal_duplicate",
                {
                    "image": str(path),
                    "caption": caption,
                    "eaten_at": utc_now(),
                    "telegram_file_unique_id": file_unique_id,
                    "existing_approval_id": pending_dup.get("id") if pending_dup else None,
                    "existing_meal_id": saved_dup.get("meal_id") if saved_dup else None,
                },
                telegram_file_unique_id=file_unique_id,
            )
            handed_off = True
            rows: list[list[InlineKeyboardButton]] = []
            if pending_dup and pending_dup.get("status") == "pending":
                rows.append([button("✏️ פתח את הניתוח הקיים", f"backmeal:{pending_dup['id']}")])
            if saved_dup and saved_dup.get("meal_id"):
                rows.append([button("✏️ ערוך את הארוחה הקיימת", f"editmeal:{saved_dup['meal_id']}")])
            rows.extend(
                [
                    [button("➕ כן, זו ארוחה נוספת", f"dup:new:{duplicate_id}")],
                    [button("❌ לא לשמור", f"reject_dup:{duplicate_id}")],
                ]
            )
            await progress.edit_text(
                "זיהיתי שהתמונה דומה מאוד לארוחה שכבר נשלחה. "
                "האם זו ארוחה נוספת או שתרצה לעדכן את הקיימת?",
                reply_markup=InlineKeyboardMarkup(rows),
            )
            return

        nutrition_payload = await _meal_analysis_context(
            user_id,
            "meal_photo_caption" if caption else "meal_photo",
            "Analyze meal photo with user caption" if caption else "Analyze meal photo",
        )
        # TASK-15: a caption GUIDES full image analysis — it must not replace it.
        # Route captioned photos through analyze_meal_image with the caption as
        # guidance (not through the text-correction reanalysis path, which biased
        # the result toward the caption tokens and skipped visible components).
        analysis = await analyze_meal_image(
            image_bytes,
            user_id=user_id,
            nutrition_context=nutrition_payload,
            caption=caption or None,
        )
        if caption:
            analysis.notes = (analysis.notes + [f"תיאור מהמשתמש: {caption}"])[-10:]
        if not analysis.is_meaningful():
            await progress.edit_text("לא זוהתה ארוחה (אין מזון או ערכים תזונתיים). נסה תמונה ברורה יותר.")
            return

        approval_id = await create_approval(
            user_id,
            "meal",
            {
                "analysis": analysis.model_dump(),
                "image": str(path),
                "eaten_at": utc_now(),
            },
            telegram_file_unique_id=file_unique_id,
        )
        handed_off = True
        await event_log.append_event(
            DB,
            user_id,
            "MEAL_ANALYSIS_COMPLETED",
            entity="approval",
            entity_id=approval_id,
            source="vision",
            properties={"quality": data_quality.assess_meal(analysis).score},
        )
        if await should_auto_approve(user_id, analysis):
            await auto_save_meal(progress, user_id, approval_id)
            await clear_meal_fix(user_id)
        else:
            await render_meal(progress, user_id, approval_id)
            await set_meal_fix(user_id, approval_id, 0)
    except Exception as exc:
        LOGGER.exception("Photo processing failed")
        with suppress(Exception):
            await event_log.append_event(
                DB, user_id, "photo_analysis_error",
                entity="photo", entity_id="",
                source="system",
                properties={"error": str(exc)[:200], "context": "photo analysis"},
            )
        await progress.edit_text(friendly_error(exc, "photo analysis"))
    finally:
        if path is not None and not handed_off:
            with suppress(Exception):
                path.unlink(missing_ok=True)


@runtime_bound(RUNTIME_NAMES)
async def quantity_menu_keyboard(approval_id: str, item_count: int) -> InlineKeyboardMarkup:
    rows = [
        [button(f"{index + 1}. פריט {index + 1}", f"editqty:{approval_id}:{index}")]
        for index in range(item_count)
    ]
    rows.append([button("⬅️ חזרה לארוחה", f"backmeal:{approval_id}")])
    return InlineKeyboardMarkup(rows)


@runtime_bound(RUNTIME_NAMES)
async def render_quantity_editor(
    query: Any,
    user_id: int,
    approval_id: str,
    item_index: int,
) -> None:
    row = await fetch_approval(user_id, approval_id)
    if not row:
        return

    analysis = MealAnalysis.model_validate(row["data"]["analysis"])
    item = analysis.items[item_index]
    text = (
        f"<b>עריכת כמות</b>\n\n"
        f"פריט: <b>{esc(item.name)}</b>\n"
        f"כמות נוכחית: <b>{item.grams:g} גרם</b>\n"
        f"קלוריות: <b>{item.calories:.0f}</b>\n"
        f"חלבון: <b>{item.protein:.0f}</b> | "
        f"פחמימות: <b>{item.carbs:.0f}</b> | "
        f"שומן: <b>{item.fat:.0f}</b>"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                button("-25 גרם", f"qtydelta:{approval_id}:{item_index}:-25"),
                button("+25 גרם", f"qtydelta:{approval_id}:{item_index}:25"),
            ],
            [
                button("-50 גרם", f"qtydelta:{approval_id}:{item_index}:-50"),
                button("+50 גרם", f"qtydelta:{approval_id}:{item_index}:50"),
            ],
            [button("⬅️ כל הפריטים", f"editqtymenu:{approval_id}")],
        ]
    )
    await safe_edit(query, text, keyboard)


class _AlreadyDecided(Exception):
    """Internal sentinel: an approval was already claimed by another caller."""


@runtime_bound(RUNTIME_NAMES)
async def check_duplicate_meal(user_id: int, analysis: MealAnalysis) -> dict[str, Any] | None:
    """Check whether this is probably the SAME consumed meal being logged twice.

    TASK-10: a duplicate warning must reflect item/product identity, not mere
    nutritional or timing similarity. A protein drink and cottage cheese with
    vegetables are different meals even if their calories are close or they were
    logged minutes apart. We therefore require a strong normalized-item-identity
    overlap between the new meal and a recent one; nutrition/time proximity
    alone never triggers. Prefer missing a weak duplicate over warning on
    clearly different foods.
    """
    from datetime import datetime as _dt
    from datetime import timedelta, timezone

    from noam_coach.services.learned_foods import normalize_food_key

    cutoff = (_dt.now(timezone.utc) - timedelta(minutes=30)).isoformat()

    def _item_keys(names: "list[str]") -> set[str]:
        keys = {normalize_food_key(n) for n in names if str(n).strip()}
        return {k for k in keys if k}

    new_keys = _item_keys([item.name for item in analysis.items])
    if not new_keys:
        return None

    recent = await DB.fetch_all(
        "SELECT * FROM meals WHERE user_id=? AND created_at > ? ORDER BY created_at DESC",
        (user_id, cutoff),
    )
    new_totals = analysis.totals()
    for meal in recent:
        item_rows = await DB.fetch_all(
            "SELECT name FROM meal_items WHERE meal_id=?", (meal["id"],)
        )
        existing_keys = _item_keys([row["name"] for row in item_rows])
        if not existing_keys:
            # Fall back to the stored meal title when there are no item rows.
            existing_keys = _item_keys([meal["name"]])
        if not existing_keys:
            continue
        overlap = new_keys & existing_keys
        union = new_keys | existing_keys
        # Strong identity overlap: the primary foods substantially match.
        jaccard = len(overlap) / len(union) if union else 0.0
        strong_identity = jaccard >= 0.6 or (
            len(overlap) >= 1 and overlap == new_keys and overlap == existing_keys
        )
        if not strong_identity:
            continue
        # Given matching identity, a close quantity/calorie total confirms it is
        # the same meal rather than a legitimate second serving of the same food.
        if new_totals["calories"] > 0 and meal["calories"] > 0:
            diff = abs(meal["calories"] - new_totals["calories"])
            if diff / max(meal["calories"], new_totals["calories"]) <= 0.35:
                return meal
        else:
            return meal
    return None


@runtime_bound(RUNTIME_NAMES)
async def persist_meal(user_id: int, approval_id: str) -> int | None:
    """Persist a meal atomically and idempotently.

    The approval payload is read *inside* the write transaction, so a text or
    quantity correction cannot race with approval.  An approval may either
    create a new meal or update an existing meal (``edit_meal_id``).  Audit,
    event logging and fingerprints are best-effort after the durable commit and
    can never turn a successful save into a false user-facing error.
    """
    meal_id = 0
    totals: dict[str, float] = {}
    image_path: str | None = None
    telegram_file_unique_id: str | None = None
    edited_existing = False
    try:
        diet_restrictions = await user_model.get_value(DB, user_id, "diet_restrictions")
        allergies_val = await user_model.get_value(DB, user_id, "allergies")
        async with DB.transaction() as conn:
            cursor = await conn.execute(
                "SELECT * FROM approvals WHERE id=? AND user_id=? AND status='pending'",
                (approval_id, user_id),
            )
            approval = await cursor.fetchone()
            if not approval:
                raise _AlreadyDecided()
            payload = json.loads(approval["payload"] or "{}")
            analysis = MealAnalysis.model_validate(payload["analysis"])
            if not analysis.is_meaningful():
                # Final gate: never persist an empty / all-zero "meal" (P0).
                raise ValueError("אי אפשר לשמור ארוחה ללא מזון או ערכים תזונתיים")
            validation = validate_meal_analysis(
                analysis,
                diet_restrictions=diet_restrictions,
                allergies=allergies_val,
            )
            if validation.blocked:
                raise ValueError(validation.issues[0].message)
            totals = analysis.totals()
            image_path = payload.get("image")
            telegram_file_unique_id = approval["telegram_file_unique_id"]
            edit_meal_id = payload.get("edit_meal_id")
            now = utc_now()

            cur = await conn.execute(
                "UPDATE approvals SET status='approved', decided_at=? "
                "WHERE id=? AND user_id=? AND status='pending'",
                (now, approval_id, user_id),
            )
            if cur.rowcount != 1:
                raise _AlreadyDecided()

            if edit_meal_id is not None:
                cur = await conn.execute(
                    """
                    UPDATE meals SET
                        name=?, calories=?, protein=?, carbs=?, fat=?,
                        confidence=?, image_path=COALESCE(?, image_path),
                        eaten_at=?, approval_id=?
                    WHERE id=? AND user_id=?
                    """,
                    (
                        analysis.meal_name,
                        totals["calories"], totals["protein"], totals["carbs"], totals["fat"],
                        analysis.confidence, image_path,
                        payload.get("eaten_at", now), approval_id,
                        int(edit_meal_id), user_id,
                    ),
                )
                if cur.rowcount != 1:
                    raise ValueError("הארוחה לעריכה כבר אינה קיימת")
                meal_id = int(edit_meal_id)
                edited_existing = True
                await conn.execute("DELETE FROM meal_items WHERE meal_id=?", (meal_id,))
            else:
                cursor = await conn.execute(
                    """
                    INSERT INTO meals(
                        user_id, name, calories, protein, carbs, fat,
                        confidence, image_path, approval_id, eaten_at, created_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id, analysis.meal_name,
                        totals["calories"], totals["protein"], totals["carbs"], totals["fat"],
                        analysis.confidence, image_path, approval_id,
                        payload.get("eaten_at", now), now,
                    ),
                )
                meal_id = int(cursor.lastrowid or 0)

            await conn.executemany(
                """
                INSERT INTO meal_items(
                    meal_id, name, grams, calories, protein, carbs, fat, confidence
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        meal_id, item.name, item.grams, item.calories, item.protein,
                        item.carbs, item.fat, item.confidence,
                    )
                    for item in analysis.items
                ],
            )
    except _AlreadyDecided:
        return None

    with suppress(Exception):
        # FIX 43: meal create/edit is one domain event that must invalidate
        # every dependent day-state projection (active menu, active
        # next-meal recommendation), not just update the meals table.
        from noam_coach.services.day_state_invalidation import invalidate_day_projections

        await invalidate_day_projections(
            DB, user_id, reason="meal_edited" if edited_existing else "meal_created"
        )
    with suppress(Exception):
        await meal_intelligence.register_meal_fingerprint(
            DB,
            user_id=user_id,
            meal_id=meal_id,
            image_path=image_path,
            telegram_file_unique_id=telegram_file_unique_id,
        )
    with suppress(Exception):
        await write_audit(
            user_id,
            "edit" if edited_existing else "approve",
            "meal",
            meal_id,
            approval_id=approval_id,
            **totals,
        )
    with suppress(Exception):
        await event_log.append_event(
            DB,
            user_id,
            "MEAL_UPDATED" if edited_existing else "MEAL_APPROVED",
            entity="meal",
            entity_id=meal_id,
            source="user",
            properties={"approval_id": approval_id, **totals},
        )
    return meal_id


@runtime_bound(RUNTIME_NAMES)
async def should_auto_approve(user_id: int, analysis: MealAnalysis) -> bool:
    """Auto-approve only after learning and a strict data-quality gate."""
    validation = await validate_meal_analysis_for_user(DB, user_id, analysis)
    if validation.blocked:
        return False
    report = data_quality.assess_meal(analysis)
    if not report.usable or report.score < 0.9:
        return False
    if analysis.question or analysis.options:
        return False
    if any(item.confidence < 0.85 for item in analysis.items):
        return False
    count = await DB.fetch_one("SELECT COUNT(*) AS c FROM meals WHERE user_id=?", (user_id,))
    return int((count or {}).get("c") or 0) >= 10


@runtime_bound(RUNTIME_NAMES)
async def auto_save_meal(progress: Any, user_id: int, approval_id: str) -> None:
    """Save a high-confidence meal automatically, with a prominent Undo."""
    row = await fetch_approval(user_id, approval_id)
    if not row:
        return
    analysis = MealAnalysis.model_validate(row["data"]["analysis"])
    totals = analysis.totals()
    meal_id = await persist_meal(user_id, approval_id)
    if meal_id is None:
        return
    await progress.edit_text(
        f"נרשם ✅ {esc(analysis.meal_name)} — "
        f"{totals['calories']:.0f} קל׳, {totals['protein']:.0f} ג׳ חלבון.\n"
        "<i>(זוהה בביטחון גבוה ונשמר אוטומטית)</i>",
        reply_markup=InlineKeyboardMarkup(
            [
                [button("↩️ בטל", f"undo_meal:{meal_id}")],
            ]
        ),
        parse_mode=ParseMode.HTML,
    )


@runtime_bound(RUNTIME_NAMES)
async def render_meal(target: Any, user_id: int, approval_id: str, refine_count: int = 0) -> None:
    row = await fetch_approval(user_id, approval_id)
    if not row:
        return

    analysis = MealAnalysis.model_validate(row["data"]["analysis"])
    totals = analysis.totals()
    # Audit F-A1: decision controls carry the payload revision, so a press
    # on a card that predates a correction is detected as stale instead of
    # deciding content the user is no longer looking at.
    decision_rev = int(row["data"].get("revision", 0) or 0)
    approve_cb = f"approve_meal:{approval_id}:r{decision_rev}"
    reject_cb = f"reject_meal:{approval_id}:r{decision_rev}"

    validation: MealValidationResult = await validate_meal_analysis_for_user(DB, user_id, analysis)
    restriction_warnings = []
    for issue in validation.issues:
        icon = "⛔" if issue.severity == "block" else "⚠️"
        restriction_warnings.append(f"{icon} {esc(issue.message)}")

    def _quantity_unit(item: "FoodItem") -> str:
        # TASK-9: drinks are measured in volume — render "מ״ל" not "גרם".
        name = str(item.name or "")
        if any(word in name for word in ("שייק", "משקה", "שתייה", "מיץ", "קפה", "חלב", "מ״ל", 'מ"ל')):
            return "מ״ל"
        return "גרם"

    def _quantity_text(item: "FoodItem") -> str:
        # TASK-17: when the visible evidence is a count and the gram weight is an
        # estimate (not derived from a package label / known product / user),
        # render the count ("3 יחידות") rather than an unsupported exact gram
        # value. quantity_source is internal and never shown.
        count = getattr(item, "quantity_count", None)
        source = str(getattr(item, "quantity_source", "") or "")
        if count and source in {"", "visual_count", "estimate"}:
            unit = str(getattr(item, "quantity_unit", "") or "יחידות")
            count_str = f"{count:g}"
            return f"{count_str} {esc(unit)}"
        return f"{item.grams:g} {_quantity_unit(item)}"

    def _item_line(index: int, item: "FoodItem") -> str:
        # TASK-22: meal components are not an ordered sequence — use a plain
        # bullet, not database-style "1." / "2." numbering.
        del index
        line = (
            f"• {esc(item.name)} — {_quantity_text(item)} | "
            f"{item.calories:.0f} קל׳ | {item.protein:.0f} חלבון"
        )
        if item.confidence < 0.6:
            line += " ⚠️"
        return line

    # TASK-9: a single-item meal is rendered simply — the item's own quantity and
    # macros, with no duplicated identical "meal total" line below it.
    single_item = len(analysis.items) == 1
    if single_item:
        only = analysis.items[0]
        items = (
            f"{_quantity_text(only)}\n"
            f"{only.calories:.0f} קל׳ | {only.protein:.0f} ג׳ חלבון"
        )
    else:
        items = "\n".join(_item_line(i, item) for i, item in enumerate(analysis.items))

    if analysis.confidence < 0.7:
        cal = totals["calories"]
        low, high = int(cal * 0.8), int(cal * 1.2)
        items += f"\n\n⚠️ <i>רמת ודאות נמוכה — טווח משוער: {low}–{high} קלוריות</i>"
    # Macro consistency check.
    macro_cal = totals["protein"] * 4 + totals["carbs"] * 4 + totals["fat"] * 9
    if totals["calories"] > 50 and macro_cal > 50:
        macro_diff = abs(totals["calories"] - macro_cal)
        if macro_diff > max(100, totals["calories"] * 0.25):
            items += (
                f"\n⚠️ <i>סכום מאקרו ({macro_cal:.0f}) שונה מסה״כ "
                f"({totals['calories']:.0f}) — בדוק הכמויות</i>"
            )
    # After a streak of refinements, acknowledge the effort and reassure that
    # plain text keeps refining the same dish until approval.
    if refine_count >= 2:
        refine_hint = (
            f"\n\n<i>עדכנתי לפי {refine_count} ההבהרות שלך 🙏 אפשר להמשיך לדייק "
            "בכתיבה חופשית, וכשזה מדויק — פשוט אשר.</i>"
        )
    elif refine_count == 1:
        refine_hint = "\n\n<i>עדכנתי. אם עדיין לא מדויק — כתוב לי עוד תיקון, או אשר.</i>"
    else:
        refine_hint = (
            '\n\n<i>אם משהו לא מדויק — פשוט כתוב לי (למשל "חצי מנה", "בלי שמן") ואדייק.</i>'
        )

    restriction_block = ""
    if restriction_warnings:
        restriction_block = "\n" + "\n".join(restriction_warnings) + "\n"

    if analysis.question and analysis.options:
        text = (
            f"<b>{esc(analysis.meal_name)}</b>\n\n"
            f"{items}\n\n"
            f"הערכה: <b>{totals['calories']:.0f} קלוריות</b> | "
            f"<b>{totals['protein']:.0f} חלבון</b>\n\n"
            f"<b>{esc(analysis.question)}</b>\n\n"
            "אם הניתוח אינו מדויק — אפשר לבחור מהרשימה, לכתוב לי תיאור נוסף, "
            "או פשוט לאשר אם זה תקין." + restriction_block + refine_hint
        )
        option_rows = [
            [
                button(
                    option.label,
                    f"clarify:{approval_id}:{index}",
                )
            ]
            for index, option in enumerate(analysis.options[:4])
        ]
        # TASK-22: no dedicated "correct by text" button — the user can just
        # write a correction directly while this meal is awaiting approval.
        if validation.blocked:
            text += "\n\n<b>אי אפשר לשמור עד שמתקנים את זה.</b>"
            # F-A2: a blocked card must offer the fix path, not only דחה —
            # implausible quantities are repaired in the quantity editor
            # (or by typing a correction).
            option_rows.append([button("⚖️ ערוך כמויות", f"editqtymenu:{approval_id}")])
            option_rows.append([button("❌ דחה", reject_cb)])
        else:
            option_rows.append(
                [
                    button("✅ אישור", approve_cb),
                    button("❌ דחה", reject_cb),
                ]
            )
        keyboard = InlineKeyboardMarkup(option_rows)
    else:
        if single_item:
            # TASK-9: no duplicate totals for a one-item meal — the item line
            # already shows calories/protein; only add carbs/fat once.
            totals_block = (
                f"\n\nפחמימות: {totals['carbs']:.0f} ג׳ | שומן: {totals['fat']:.0f} ג׳"
            )
        else:
            totals_block = (
                f"\n\n<b>סה״כ</b>\n"
                f"קלוריות: <b>{totals['calories']:.0f}</b>\n"
                f"חלבון: <b>{totals['protein']:.0f} גרם</b>\n"
                f"פחמימות: <b>{totals['carbs']:.0f} גרם</b>\n"
                f"שומן: <b>{totals['fat']:.0f} גרם</b>"
            )
        text = (
            f"<b>{esc(analysis.meal_name)}</b>\n\n"
            f"{items}"
            f"{totals_block}\n\n"
            "הארוחה תיספר רק לאחר אישור." + restriction_block + refine_hint
        )
        if validation.blocked:
            text += "\n\n<b>אי אפשר לשמור עד שמתקנים את זה.</b>"
            keyboard = InlineKeyboardMarkup(
                [
                    [button("⚖️ ערוך כמויות", f"editqtymenu:{approval_id}")],
                    [button("❌ דחה", reject_cb)],
                ]
            )
        else:
            # TASK-22: focused actions — Save / edit quantities / cancel. The
            # dedicated "correct by text" button is gone; the user writes any
            # correction directly while the meal is awaiting approval.
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        button("✅ שמור", approve_cb),
                        button("⚖️ ערוך כמויות", f"editqtymenu:{approval_id}"),
                    ],
                    [button("❌ דחה", reject_cb)],
                ]
            )

    if hasattr(target, "edit_message_text"):
        await safe_edit(target, text, keyboard)
    else:
        await target.edit_text(
            text,
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
        )
