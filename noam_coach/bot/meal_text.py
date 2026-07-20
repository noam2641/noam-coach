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
from noam_coach.services.profile import analyze_meal_text, get_user_plan, set_exercise_override
from noam_coach.services.nutrition_context import (
    build_nutrition_ai_request,
    build_nutrition_context,
)

RUNTIME_NAMES = ('ContextTypes', 'DB', 'Exception', 'InlineKeyboardMarkup', 'LOGGER', 'MealAnalysis', 'RuntimeError', 'Update', '_CANCEL_WORDS', '_handle_meal_correction_text', 'approval_id', 'button', 'candidate_ids', 'clear_meal_fix', 'conversation', 'corrected_analysis', 'correction_text', 'count', 'decision', 'ensure_user', 'event_log', 'exc', 'fetch_approval', 'flow', 'friendly_error', 'get_meal_fix', 'handle_onboarding_text', 'home_keyboard', 'image_path', 'index', 'int', 'is_allowed', 'json', 'len', 'list', 'original_analysis', 'pc', 'planning', 'prior_locked', 'progress', 'rc', 'reanalyze_meal_with_text_and_image', 'refine_count', 'removal_corrections', 'render_meal', 'route_free_text', 'row', 'selected', 'set_meal_fix', 'str', 'suppress', 'text', 'track_event', 'update', 'user_id', 'write_audit')


def _apply_quantity_clarification_stage(
    row: Any,
    analysis: "MealAnalysis",
    correction_text: str,
) -> "MealAnalysis":
    """Resolve an answered quantity question, then ask the next one if needed.

    Mutates ``row["data"]["pending_clarification"]`` so the approval payload
    can reconstruct both the pending question and its resolution. Never raises
    into the correction handler: any failure leaves the analysis untouched and
    the card renders as it would have before Batch 6.
    """
    from noam_coach.services.meal_clarification import (
        PendingClarification,
        build_clarification_options,
        detect_quantity_clarification,
        resolve_typed_grams,
    )
    from models import ClarificationOption

    try:
        pending = PendingClarification.from_payload(
            row["data"].get("pending_clarification")
        )

        # (1) The user typed grams while a question was open. parse_locked_
        # quantities already applied them to a MATCHED item above; this
        # closes the question so the same answer can never be applied twice
        # and the card stops asking.
        if pending is not None and not pending.is_resolved:
            locked = meal_intelligence.parse_locked_quantities(correction_text)
            typed_grams = None
            for entry in locked or []:
                grams_value = float(getattr(entry, "grams", 0) or 0)
                if grams_value > 0:
                    typed_grams = grams_value
                    break
            if typed_grams is not None:
                resolve_typed_grams(analysis, pending, typed_grams)
                row["data"]["pending_clarification"] = pending.to_payload()
                if pending.is_resolved:
                    analysis.question = None
                    analysis.options = []
                    return analysis

        # (2) Ask about unresolved quantity evidence — but never overwrite a
        # question the analyzer itself is already asking.
        if analysis.question:
            return analysis

        next_pending = detect_quantity_clarification(analysis)
        if next_pending is None:
            return analysis

        # Do not re-ask a question this payload already answered.
        if (
            pending is not None
            and pending.is_resolved
            and pending.token == next_pending.token
        ):
            return analysis

        # Preserve an in-flight question's resolution state across re-renders
        # so its token (and idempotency) survives an unrelated correction.
        if pending is not None and pending.token == next_pending.token:
            next_pending.resolved_token = pending.resolved_token
            next_pending.resolution = pending.resolution
            next_pending.resolved_grams = pending.resolved_grams

        analysis.question = next_pending.question
        analysis.options = [
            ClarificationOption(
                label=str(opt.get("label") or ""),
                item_index=opt.get("item_index"),
                item_name=next_pending.item_name,
                apply_kind=opt.get("apply_kind"),
                set_grams=opt.get("set_grams"),
                set_size=opt.get("set_size"),
            )
            for opt in build_clarification_options(next_pending)
        ]
        row["data"]["pending_clarification"] = next_pending.to_payload()
    except Exception:  # noqa: BLE001 — clarification must never break a correction
        LOGGER.exception("quantity clarification stage failed")
    return analysis


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
        count_corrections = [c for c in corrections if c.kind == "count"]
        scale_corrections = [c for c in corrections if c.kind == "scale"]

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

        if count_corrections:
            # Batch 4: record count/portion evidence ("3 שניצלים", "חצי
            # שניצל") on the matched item WITHOUT touching grams — a count is
            # never a weight. Deterministic-to-grams conversion is Batch 5.
            for cc in count_corrections:
                corrected_analysis = meal_intelligence.apply_count_correction(
                    corrected_analysis, cc,
                )
            used_deterministic = True

        if scale_corrections:
            for sc in scale_corrections:
                corrected_analysis = meal_intelligence.apply_scale_correction(
                    corrected_analysis, sc,
                )
            used_deterministic = True

        replace_corrections = [c for c in corrections if c.kind == "replace"]

        if replace_corrections:
            for rc in replace_corrections:
                corrected_analysis = meal_intelligence.apply_item_replacement_correction(
                    corrected_analysis, rc,
                )
            used_deterministic = True

        if not used_deterministic:
            # Fall back to AI reanalysis only when deterministic parser
            # did not recognize the correction.
            # Pass all previously locked corrections so the AI respects them
            # even when re-analysing from scratch (REC-PLAN-MEAL-03-12).
            prior_locked: list[str] = list(row["data"].get("locked_corrections") or [])
            if image_path:
                await progress.edit_text("מנתח מחדש את התמונה לפי מה שכתבת…")
                nutrition_payload: dict[str, Any] | None = None
                with suppress(Exception):
                    nutrition_payload = build_nutrition_ai_request(
                        await build_nutrition_context(DB, user_id, "meal_correction"),
                        "Re-analyze meal correction",
                    )["context"]
                corrected_analysis = await reanalyze_meal_with_text_and_image(
                    image_path=image_path,
                    correction_text=correction_text,
                    locked_corrections=prior_locked,
                    nutrition_context=nutrition_payload,
                )
            else:
                # TASK-20 audit correction: a manual-text-logged meal
                # (assistant.log_meal_from_text, source="manual_text") has no
                # source image, so the image-reanalysis path above cannot run
                # — this used to raise and silently fail every non-
                # deterministic correction for such a meal (any phrase the
                # deterministic parser didn't recognize as remove/prep/qty/
                # scale/replace). Re-describe the CURRENT meal in text plus
                # the correction and re-run the same text-only analyzer
                # manual logging itself uses (analyze_meal_text), so a
                # manually-logged meal's correction path is not silently
                # worse than its logging path.
                await progress.edit_text("מעדכן לפי מה שכתבת…")
                current_description = "; ".join(
                    f"{item.name} {item.grams:g} גרם" for item in original_analysis.items
                )
                locked_text = " ".join(prior_locked)
                combined_description = " | ".join(
                    part for part in (current_description, locked_text, correction_text) if part
                )
                corrected_analysis = await analyze_meal_text(combined_description, user_id=user_id)

        corrected_analysis.notes = (
            original_analysis.notes
            + corrected_analysis.notes
            + [f"תיקון: {correction_text}"]
        )[-10:]

        # --- Batch 6: quantity clarification ------------------------------
        # Two directions, in this order:
        #   1. the user was ASKED for grams and just typed them → resolve the
        #      open question through the same gram-locking write a button uses;
        #   2. the (possibly corrected) analysis still has quantity evidence we
        #      must not resolve alone → ask, instead of guessing.
        corrected_analysis = _apply_quantity_clarification_stage(
            row, corrected_analysis, correction_text
        )

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


def _format_rest_seconds(seconds: int) -> str:
    minutes, remainder = divmod(max(0, int(seconds)), 60)
    return f"{minutes}:{remainder:02d}"


def _parse_rest_seconds(text: str) -> int | None:
    normalized = text.strip().lower()
    match = re.search(r"\b(\d{1,2}):([0-5]\d)\b", normalized)
    if match:
        return max(30, int(match.group(1)) * 60 + int(match.group(2)))
    match = re.search(r"\b(\d{2,3})\s*(?:שניות|שניה|שנ׳|שנ'|sec|seconds?)\b", normalized)
    if match:
        return max(30, int(match.group(1)))
    if any(variant in normalized for variant in ("דקה וחצי", "דקה חצי", "1.5 דקות", "1.5 דקה")):
        return 90
    match = re.search(r"\b(\d+(?:\.\d+)?)\s*(?:דקות|דקה|mins?|minutes?)\b", normalized)
    if match:
        return max(30, int(round(float(match.group(1)) * 60)))
    return None


def _parse_workout_parameter_text(text: str) -> list[dict[str, Any]]:
    normalized = text.strip().lower()
    if any(marker in normalized for marker in ("כל התוכנית", "לכל התוכנית", "התוכנית כולה", "whole plan", "entire plan")):
        scope = "program"
    elif any(marker in normalized for marker in ("כל התרגילים", "כולם", "לכולם", "all exercises")):
        scope = "all"
    else:
        scope = "current"
    updates: list[dict[str, Any]] = []

    if any(marker in normalized for marker in ("מנוחה", "rest")):
        rest_seconds = _parse_rest_seconds(normalized)
        if rest_seconds is not None:
            updates.append({
                "field": "rest",
                "value": rest_seconds,
                "scope": scope,
                "label": f"מנוחה {_format_rest_seconds(rest_seconds)}",
            })

    match = re.search(r"(?:משקל|weight)\s*(\d+(?:\.\d+)?)", normalized)
    if match:
        weight = max(0.0, round(float(match.group(1)), 2))
        updates.append({"field": "weight", "value": weight, "scope": "current", "label": f"משקל {weight:g} קג"})

    match = re.search(r"\b(\d{1,2})\s*(?:סטים|סט|sets?)\b", normalized)
    if match:
        sets = max(1, int(match.group(1)))
        updates.append({"field": "sets", "value": sets, "scope": scope, "label": f"{sets} סטים"})

    match = re.search(r"\b(\d{1,2})\s*[-–]\s*(\d{1,2})\s*(?:חזרות|reps?)?\b", normalized)
    if match:
        rmin = max(1, int(match.group(1)))
        rmax = max(rmin, int(match.group(2)))
        updates.append({"field": "reps", "rmin": rmin, "rmax": rmax, "scope": scope, "label": f"{rmin}-{rmax} חזרות"})

    return updates


async def _handle_workout_parameter_text(
    update: Update,
    user_id: int,
    flow: conversation.ActiveFlow,
    text: str,
) -> None:
    payload = dict(flow.payload or {})
    code = str(payload.get("code") or "")
    exercise_index = int(payload.get("exercise_index") or 0)
    if not code:
        await conversation.clear_active_flow(DB, user_id)
        await route_free_text(update, user_id)
        return

    updates = _parse_workout_parameter_text(text)
    if not updates:
        await update.effective_message.reply_text(
            "לא זיהיתי שינוי לפרמטרי האימון. אפשר לכתוב למשל: מנוחה 1:30 לכל התרגילים, משקל 22.5, או 4 סטים.",
            reply_markup=InlineKeyboardMarkup([
                [button("⬅️ חזרה לאימון", f"workout:{code}")],
                [button("❌ ביטול", "wparamtext:cancel")],
            ]),
        )
        return

    plan = await get_user_plan(user_id, code)
    exercise_name = plan["exercises"][exercise_index]["name"] if 0 <= exercise_index < len(plan["exercises"]) else "התרגיל"
    if any(item.get("scope") == "program" for item in updates):
        scope_label = "לכל התוכנית"
    elif any(item.get("scope") == "all" for item in updates):
        scope_label = "לכל התרגילים באימון הזה"
    else:
        scope_label = f"לתרגיל {exercise_name}"
    summary = ", ".join(str(item["label"]) for item in updates)
    await conversation.set_active_flow(
        DB,
        user_id,
        conversation.FlowName.workout_parameter_edit,
        step="preview",
        payload={
            "code": code,
            "exercise_index": exercise_index,
            "pending_updates": updates,
            "summary": summary,
            "scope_label": scope_label,
        },
    )
    await update.effective_message.reply_text(
        f"הבנתי: {summary} {scope_label}.\nלא שמרתי עדיין. לאשר את השינוי?",
        reply_markup=InlineKeyboardMarkup([
            [button("✅ אשר ושמור", "wparamtext:apply")],
            [button("✏️ אכתוב תיקון אחר", f"editparams:{code}:{exercise_index}")],
            [button("❌ ביטול", "wparamtext:cancel")],
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

    if decision.handler == "workout_parameter_flow":
        flow = await conversation.get_active_flow(DB, user_id)
        if flow.name == conversation.FlowName.workout_parameter_edit:
            await _handle_workout_parameter_text(update, user_id, flow, text)
            return
        await conversation.clear_active_flow(DB, user_id)

    # re7 P1-13/14 (FIX 50): when a next-meal recommendation is active,
    # interpret a correction ("אבל נשאר לי 269", "זה גדול מדי", "אין לי
    # ביצים") against it FIRST, before the daily-menu editor. The comment
    # above always said this was the intended precedence, but the code
    # called try_build_daily_menu_edit_reply() first -- so with an active
    # daily menu AND an active next-meal card, a correction meant for the
    # next-meal card could be silently applied to the menu's snack slot
    # instead. handle_recommendation_correction() already returns None
    # immediately when there is no active recommendation, so trying it
    # first never swallows text that should reach the menu editor.
    from noam_coach.services.next_meal import (
        format_next_meal_recommendation,
        handle_recommendation_correction,
        next_meal_action_rows,
        remember_active_recommendation,
    )

    correction = await handle_recommendation_correction(DB, user_id, text)
    if correction is not None:
        prefix, recommendation = correction
        keyboard_rows = [
            [button(label, cb) for label, cb in row]
            for row in next_meal_action_rows(recommendation)
        ]
        body = format_next_meal_recommendation(recommendation)
        if prefix:
            body = f"{prefix}\n\n{body}"
        sent = await update.effective_message.reply_text(
            body,
            reply_markup=InlineKeyboardMarkup(keyboard_rows),
            parse_mode=ParseMode.HTML,
        )
        await remember_active_recommendation(
            DB, user_id, recommendation, message_id=getattr(sent, "message_id", None)
        )
        return

    from noam_coach.services.daily_menu_edit import try_build_daily_menu_edit_reply

    daily_menu_edit = await try_build_daily_menu_edit_reply(DB, user_id, text)
    if daily_menu_edit is not None:
        reply_text, reply_rows = daily_menu_edit
        sent = await update.effective_message.reply_text(
            reply_text,
            reply_markup=InlineKeyboardMarkup([[button(label, cb) for label, cb in row] for row in reply_rows]),
            parse_mode=ParseMode.HTML,
        )
        from noam_coach.services.daily_menu_state import remember_daily_menu_message

        await remember_daily_menu_message(
            DB,
            user_id,
            chat_id=getattr(getattr(sent, "chat", None), "id", user_id),
            message_id=getattr(sent, "message_id", None),
            source="daily_menu_revision",
        )
        return

    # Workout text and ordinary free text both go through the natural-language
    # intent router, but only after the conversation engine assigned ownership.
    await route_free_text(update, user_id)
