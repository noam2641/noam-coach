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

from noam_coach.bot.ui import safe_message_edit
from noam_coach.runtime_bind import runtime_bound
from noam_coach.services.profile import analyze_meal_text, get_user_plan, set_exercise_override
from noam_coach.services.nutrition_context import (
    build_nutrition_ai_request,
    build_nutrition_context,
)
from noam_coach.observability import taxonomy
from noam_coach.services import meal_observability

RUNTIME_NAMES = ('ContextTypes', 'DB', 'Exception', 'InlineKeyboardMarkup', 'LOGGER', 'MealAnalysis', 'RuntimeError', 'Update', '_CANCEL_WORDS', '_handle_meal_correction_text', 'approval_id', 'button', 'candidate_ids', 'clear_meal_fix', 'conversation', 'corrected_analysis', 'correction_text', 'count', 'decision', 'ensure_user', 'event_log', 'exc', 'fetch_approval', 'flow', 'friendly_error', 'get_meal_fix', 'handle_onboarding_text', 'home_keyboard', 'image_path', 'index', 'int', 'is_allowed', 'json', 'len', 'list', 'original_analysis', 'pc', 'planning', 'prior_locked', 'progress', 'rc', 'reanalyze_meal_with_text_and_image', 'refine_count', 'removal_corrections', 'render_meal', 'route_free_text', 'row', 'safe_message_edit', 'selected', 'set_meal_fix', 'str', 'suppress', 'text', 'track_event', 'update', 'user_id', 'write_audit')


async def _emit_meal_event(
    user_id: int,
    event: str,
    approval_id: str,
    *,
    source: str = "bot",
    properties: dict[str, Any] | None = None,
    content: dict[str, Any] | None = None,
) -> None:
    """Emit one meal lifecycle event through the canonical boundary.

    Correlation (trace/interaction/span) is supplied by the ambient
    observability scope inside ``emit_event`` — this never mints identifiers.
    Emission is best-effort by contract: ``emit_event`` contains its own
    failures, and the extra guard here covers an import-time problem so a
    telemetry fault can never break a correction.
    """
    try:
        from noam_coach.observability.emit import emit_event

        await emit_event(
            DB,
            user_id,
            event,
            entity="approval",
            entity_id=approval_id,
            source=source,
            properties=properties or {},
            content=content or None,
        )
    except Exception:  # noqa: BLE001 — observability must never break coaching
        LOGGER.debug("meal observability emit failed for %s", event, exc_info=True)


async def _emit_meal_lifecycle_evidence(
    user_id: int,
    approval_id: str,
    *,
    revision: int,
    original_analysis: Any,
    corrected_analysis: Any,
    row: Any,
) -> None:
    """Batch 7 evidence chain for one applied correction.

    Emits, in pipeline order: the per-item conversion diagnostic, the
    plausibility outcome, any clarification raised, and the final rendered
    quantity mode. Together with ``meal_correction_applied`` these make the
    correction reconstructable end to end.

    Entirely best-effort — a failure anywhere here leaves the correction and
    the card untouched.
    """
    try:
        from noam_coach.services.meal_plausibility import check_analysis
        from noam_coach.services.meal_clarification import PendingClarification
        from noam_coach.services.meal_quantity_diagnostics import (
            classify_pre_conversion,
        )

        # (1) Conversion evidence per item carrying count evidence. The
        # classifier is PURE and read-only: Batch 7 reports its verdict and
        # never re-decides it.
        for index, item in enumerate(getattr(corrected_analysis, "items", []) or []):
            if not getattr(item, "quantity_count", None):
                continue
            diagnostic = classify_pre_conversion(item)
            await _emit_meal_event(
                user_id,
                taxonomy.MEAL_QUANTITY_CONVERSION_EVALUATED,
                approval_id,
                source="deterministic",
                properties={
                    "revision": revision,
                    **meal_observability.conversion_properties(
                        diagnostic,
                        item_index=index,
                        item_name=str(getattr(item, "name", "") or ""),
                    ),
                },
            )

        # (2) Plausibility outcome for the corrected draft.
        issues = check_analysis(getattr(corrected_analysis, "items", []) or [])
        await _emit_meal_event(
            user_id,
            taxonomy.MEAL_PLAUSIBILITY_EVALUATED,
            approval_id,
            source="deterministic",
            properties={
                "revision": revision,
                **meal_observability.plausibility_properties(issues),
            },
        )

        # (3) A clarification standing on the card after this correction.
        pending = PendingClarification.from_payload(
            (row or {}).get("data", {}).get("pending_clarification")
        )
        if pending is not None and not pending.is_resolved:
            await _emit_meal_event(
                user_id,
                taxonomy.MEAL_CLARIFICATION_RAISED,
                approval_id,
                source="deterministic",
                properties={
                    "revision": revision,
                    **meal_observability.clarification_properties(
                        pending, status="raised", mutated=False
                    ),
                },
            )

        # (4) What the card will actually show — the last link in the chain.
        await _emit_meal_event(
            user_id,
            taxonomy.MEAL_RENDER_QUANTITY_MODE,
            approval_id,
            source="render",
            properties=meal_observability.render_mode_properties(
                corrected_analysis, revision=revision
            ),
        )
    except Exception:  # noqa: BLE001 — evidence must never break a correction
        LOGGER.debug("meal lifecycle evidence failed", exc_info=True)


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
                await safe_message_edit(progress, "מנתח מחדש את התמונה לפי מה שכתבת…")
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
                await safe_message_edit(progress, "מעדכן לפי מה שכתבת…")
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
        # Batch 7: this event moved to the canonical emit boundary. The NAME
        # and every non-sensitive property are unchanged, so existing readers
        # keep working; the raw correction text moved from `properties` into
        # `content`, where the OFF/METADATA/CONTENT/DEBUG policy, redaction
        # and digesting already live. `append_event` applies none of those —
        # it is deliberately a lower layer — so the text was previously
        # stored verbatim in every mode.
        await _emit_meal_event(
            user_id,
            taxonomy.MEAL_CORRECTION_APPLIED,
            approval_id,
            source="deterministic" if used_deterministic else "ai",
            properties={
                "revision": revision,
                "deterministic": used_deterministic,
                **meal_observability.correction_parse_properties(
                    corrections,
                    used_deterministic=used_deterministic,
                    revision=revision,
                    text_length=len(correction_text),
                ),
            },
            content={"text": correction_text},
        )
        await _emit_meal_lifecycle_evidence(
            user_id,
            approval_id,
            revision=revision,
            original_analysis=original_analysis,
            corrected_analysis=corrected_analysis,
            row=row,
        )
        count = refine_count + 1
        await set_meal_fix(user_id, approval_id, count)
        await render_meal(progress, user_id, approval_id, refine_count=count)
    except Exception as exc:  # noqa: BLE001
        LOGGER.exception("Meal correction failed")
        with suppress(Exception):
            # Batch 7: never record str(exc) or the correction text here — a
            # correction failure's message routinely embeds the user's own
            # words, which is exactly the leak this batch closes. The
            # exception class plus the pipeline stage is enough to triage,
            # and the text itself is already on the lifecycle event above as
            # mode-governed content.
            await _emit_meal_event(
                user_id,
                taxonomy.MEAL_CORRECTION_ERROR,
                approval_id,
                source="system",
                properties=meal_observability.error_properties(
                    exc, context="meal_correction"
                ),
            )
        # REC-MEAL-03: keep previous analysis, show recovery options
        await safe_message_edit(
            progress,
            "לא הצלחתי לעדכן את הארוחה. הניתוח הקודם נשמר.\n"
            "אפשר לנסות שוב או לחזור לארוחה.",
            InlineKeyboardMarkup([
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


@runtime_bound(RUNTIME_NAMES)
async def _workout_parameter_exercise_name(
    user_id: int, payload: dict[str, Any], code: str, exercise_index: int
) -> str:
    """Name of the exercise being edited, resolved through the catalog for a
    v2 payload (so a personalized Tier-1 exercise shows its real name) and
    through the legacy template path otherwise. Never raises -- a naming
    failure must not break the edit preview."""
    if int(payload.get("v") or 0) >= 2:
        from noam_coach.services import workout_catalog
        from noam_coach.services.workout_catalog import WorkoutSelectionRef

        try:
            ref = WorkoutSelectionRef(
                tier=str(payload.get("tier") or "plan"),
                plan_id=payload.get("plan_id"),
                fact_rev=payload.get("fact_rev"),
                session_index=int(payload.get("session_index") or 0),
            )
            resolved = await workout_catalog.resolve_selection(DB, user_id, ref)
            exercises = resolved.session.get("exercises", [])
            if 0 <= exercise_index < len(exercises):
                return str(exercises[exercise_index].get("name") or "התרגיל")
        except Exception:  # noqa: BLE001 - naming is best-effort, never fatal
            return "התרגיל"
        return "התרגיל"

    plan = await get_user_plan(user_id, code)
    if 0 <= exercise_index < len(plan["exercises"]):
        return str(plan["exercises"][exercise_index]["name"])
    return "התרגיל"


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

    # Batch 6 (workout-selection architecture): a v2 payload carries the full
    # session identity, so every button minted below returns to THAT session
    # rather than a bare code. Legacy payloads keep the old `workout:<code>`
    # buttons until the Batch 7 adapter.
    from noam_coach.bot.callback_plans import (
        _wk_back_callback_for_payload,
        _wk_edit_callback_for_payload,
    )

    back_callback = _wk_back_callback_for_payload(payload) or f"workout:{code}"

    updates = _parse_workout_parameter_text(text)
    if not updates:
        await update.effective_message.reply_text(
            "לא זיהיתי שינוי לפרמטרי האימון. אפשר לכתוב למשל: מנוחה 1:30 לכל התרגילים, משקל 22.5, או 4 סטים.",
            reply_markup=InlineKeyboardMarkup([
                [button("⬅️ חזרה לאימון", back_callback)],
                [button("❌ ביטול", "wparamtext:cancel")],
            ]),
        )
        return

    exercise_name = await _workout_parameter_exercise_name(user_id, payload, code, exercise_index)
    if any(item.get("scope") == "program" for item in updates):
        scope_label = "לכל התוכנית"
    elif any(item.get("scope") == "all" for item in updates):
        scope_label = "לכל התרגילים באימון הזה"
    else:
        scope_label = f"לתרגיל {exercise_name}"
    summary = ", ".join(str(item["label"]) for item in updates)
    # Carry the identity keys forward verbatim -- the confirm step re-validates
    # them before writing, so losing them here would silently downgrade a v2
    # edit to ambiguous bare-code targeting.
    preview_payload = {
        "code": code,
        "exercise_index": exercise_index,
        "pending_updates": updates,
        "summary": summary,
        "scope_label": scope_label,
    }
    for key in ("v", "tier", "plan_id", "fact_rev", "session_index", "exercise_id"):
        if key in payload:
            preview_payload[key] = payload[key]

    await conversation.set_active_flow(
        DB,
        user_id,
        conversation.FlowName.workout_parameter_edit,
        step="preview",
        payload=preview_payload,
    )
    rewrite_callback = _wk_edit_callback_for_payload(payload) or f"editparams:{code}:{exercise_index}"
    await update.effective_message.reply_text(
        f"הבנתי: {summary} {scope_label}.\nלא שמרתי עדיין. לאשר את השינוי?",
        reply_markup=InlineKeyboardMarkup([
            [button("✅ אשר ושמור", "wparamtext:apply")],
            [button("✏️ אכתוב תיקון אחר", rewrite_callback)],
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
