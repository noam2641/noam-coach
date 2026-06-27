# ruff: noqa: F401, F811, F821, I001
"""Health import commands and scheduled coaching jobs.

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
from noam_coach.services.local_health_path import (
    LocalHealthPathError,
    allowed_roots_from_text,
    looks_like_local_health_path,
    resolve_local_health_export,
)
from noam_coach.services.nutrition_context import (
    build_nutrition_ai_request,
    build_nutrition_context,
)

RUNTIME_NAMES = ('Any', 'CallbackContext', 'ContextTypes', 'DB', 'Exception', 'InlineKeyboardButton', 'InlineKeyboardMarkup', 'JOB_PRIORITY_COACHING', 'JOB_PRIORITY_HIGH', 'JOB_PRIORITY_LOW', 'JOB_PRIORITY_SCHEDULED', 'LOGGER', 'OPENAI_CLIENT', 'ParseMode', 'Path', 'RuntimeError', 'SETTINGS', 'TZ', 'Update', 'ValueError', '_ctx_has_workout', '_data_quality_disclaimer', 'abs', 'action', 'actual_bytes', 'any', 'asyncio', 'at', 'bool', 'build_daily_context', 'build_evening_summary_text', 'build_morning_menu_text', 'build_next_meal_text', 'button', 'context', 'conversation', 'ctx', 'current_flow', 'datetime', 'deliver_proactive_message', 'document', 'duplicates', 'ensure_user', 'enumerate', 'esc', 'exc', 'extract_dir', 'fasting_negated', 'flags', 'float', 'folder', 'format_evening_summary', 'format_morning_menu', 'format_next_meals', 'fraction_used', 'friendly_error', 'get_daily_flags', 'health_import', 'hh', 'hhmm', 'hint', 'hour', 'idx', 'inserted', 'insights', 'int', 'is_allowed', 'items', 'job_calorie_watch', 'job_evening', 'job_morning', 'job_motivation', 'keyboard', 'known_medications', 'learned', 'learned_block', 'lines', 'list', 'load_routine_profile', 'local_day_str', 'lowered', 'max_bytes', 'med', 'meds', 'menu', 'message', 'mm', 'moment', 'morning_checkin_keyboard', 'name', 'near', 'notify_admin', 'now', 'onboarding', 'parse_to_rows', 'profile', 'progress', 'random', 're', 'recommendations', 'reconcile', 'resumed', 'ritalin_negated', 'route_decision', 'rows', 'run_post_import_reconciliation', 'save_routine_profile', 'saved_path', 'secrets', 'send_checkin', 'send_menu', 'send_motivation', 'send_nudge', 'send_overpace', 'send_summary', 'send_to_user', 'sent', 'set_daily_flags', 'show_onboarding_basics', 'shutil', 'sleep', 'snack_hours', 'str', 'suffix', 'suggestion', 'summary', 'suppress', 'sync_health_measurements_to_facts', 'target', 'target_cal', 'telegram_file', 'text', 'today_meal_items', 'top', 'track_event', 'tuple', 'update', 'upsert_health_rows', 'user_id', 'user_model', 'value', 'weekly', 'window', 'workout', 'workout_hour', 'write_audit', 'x', 'xml_path')


@dataclass(frozen=True)
class HealthImportOutcome:
    """Persisted result shared by Telegram, Mini App and local-path imports."""

    inserted: int
    duplicates: int
    updated: int
    invalid: int
    summary: Any
    profile: dict[str, Any]
    source_file: str  # filename only — no full path for privacy
    total_stored: int  # total health rows for user after import
    import_started: str  # ISO-8601 UTC
    import_completed: str  # ISO-8601 UTC


def _health_import_success_text(outcome: HealthImportOutcome) -> str:
    s = outcome.summary
    sleep = outcome.profile.get("sleep", {}) or {}
    workout = outcome.profile.get("workout", {}) or {}

    # --- Source file section ---
    lines = ["<b>ייבוא Apple Health הושלם ✅</b>", ""]
    lines.append("<b>בקובץ שנבחר:</b>")
    if s.min_date and s.max_date:
        lines.append(f"• טווח נתונים: {s.min_date} עד {s.max_date}")
        lines.append(f"• הרשומות החדשות ביותר הן מתאריך: {s.max_date}")
    lines.append(f"• רשומות בקובץ: {s.rows:,}")
    lines.append("")

    # --- Current import section ---
    lines.append("<b>בייבוא הנוכחי:</b>")
    lines.append(f"• נוספו: {outcome.inserted:,}")
    if outcome.updated:
        lines.append(f"• עודכנו: {outcome.updated:,}")
    lines.append(f"• כפילויות שלא נשמרו שוב: {outcome.duplicates:,}")
    if outcome.invalid:
        lines.append(f"• רשומות לא תקינות: {outcome.invalid:,}")
    lines.append("")

    # --- Database totals section ---
    lines.append("<b>במאגר שלך כעת:</b>")
    lines.append(f"• אימונים: {s.workouts:,}")
    lines.append(f"• לילות שינה: {s.sleep_sessions:,}")
    if s.weight_records:
        lines.append(f"• רשומות משקל: {s.weight_records:,}")
    if s.activity_records:
        lines.append(f"• רשומות פעילות: {s.activity_records:,}")
    lines.append("")

    # --- Learned patterns ---
    learned: list[str] = []
    if sleep.get("typical_bedtime") and sleep.get("typical_wake_time"):
        learned.append(f"😴 שינה ~{sleep['typical_bedtime']}–{sleep['typical_wake_time']}")
    if workout.get("weekly_frequency"):
        hour = workout.get("typical_hour")
        at = f", בדרך כלל ~{hour}" if hour else ""
        learned.append(f"🏋️ ~{workout['weekly_frequency']} אימונים בשבוע{at}")
    if learned:
        lines.append("<b>מה למדתי על השגרה שלך:</b>")
        lines.extend(learned)
    else:
        lines.append(
            "עוד לא הצטברו מספיק נתוני שינה/אימונים בחלון הזמן כדי לזהות "
            "שגרה ברורה — נמשיך ונלמד תוך כדי."
        )

    return "\n".join(lines)


async def _health_import_followup_text(user_id: int) -> str:
    readiness = await user_model.compute_all_readiness(DB, user_id)
    missing_labels: list[str] = []
    for profile_name in ("safety", "workout", "nutrition"):
        for label in readiness.get(profile_name, {}).get("missing_labels", []):
            if label not in missing_labels:
                missing_labels.append(label)

    rows = await DB.fetch_all(
        """
        SELECT key, source
        FROM user_facts
        WHERE user_id=?
          AND valid=1
          AND confirmed=0
          AND kind!='gap'
        ORDER BY updated_at DESC
        LIMIT 6
        """,
        (user_id,),
    )
    approval_labels = []
    for row in rows:
        label = user_model.display_label(str(row["key"]))
        source = user_model.SOURCE_LABELS.get(row.get("source"), row.get("source") or "")
        approval_labels.append(f"{label} ({source})" if source else label)

    lines = ["", "<b>מה עדיין צריך כדי להשלים תמונה מלאה?</b>"]
    if approval_labels:
        lines.append("<b>דורש אישור:</b>")
        lines.extend(f"• {esc(label)}" for label in approval_labels)
    else:
        lines.append("• אין כרגע נתונים מיובאים שממתינים לאישור.")

    if missing_labels:
        lines.append("<b>עדיין חסר:</b>")
        lines.extend(f"• {esc(label)}" for label in missing_labels[:8])
        if len(missing_labels) > 8:
            lines.append(f"• ועוד {len(missing_labels) - 8} פריטים")
    else:
        lines.append("• אין פריטי חובה חסרים כרגע.")
    return "\n".join(lines)


@runtime_bound(RUNTIME_NAMES)
async def import_health_export_file(
    user_id: int,
    source_path: Path,
    *,
    max_bytes: int,
    audit_source: str,
) -> HealthImportOutcome:
    """Validate, parse and persist one ZIP/XML export without deleting it."""
    path = Path(source_path).resolve(strict=True)
    if path.suffix.casefold() not in {".zip", ".xml"}:
        raise ValueError("צריך קובץ ZIP או XML של ייצוא Apple Health")
    actual_bytes = path.stat().st_size
    if actual_bytes <= 0:
        raise ValueError("קובץ הייצוא ריק")
    if actual_bytes > max_bytes:
        raise ValueError(f"Health export exceeds {max_bytes // (1024 * 1024)}MB")
    if path.suffix.casefold() == ".zip":
        if not health_import.is_valid_zip(path):
            raise ValueError("הקובץ אינו ZIP תקין")
    elif not health_import.looks_like_xml(path):
        raise ValueError("הקובץ אינו XML תקין")

    folder = Path(SETTINGS.storage_dir) / "health_import"
    folder.mkdir(parents=True, exist_ok=True)
    extract_dir = folder / f"extract_{secrets.token_hex(8)}"
    try:
        def parse_to_rows() -> tuple[list[health_import.HealthRow], Any]:
            if path.suffix.casefold() == ".zip":
                xml_path = health_import.extract_zip(
                    path,
                    extract_dir,
                    max_members=SETTINGS.health_import_max_zip_members,
                    max_xml_bytes=SETTINGS.health_import_max_xml_mb * 1024 * 1024,
                    max_compression_ratio=SETTINGS.health_import_max_compression_ratio,
                )
            else:
                xml_path = path
            if not xml_path:
                raise RuntimeError("לא נמצא קובץ XML בייצוא")
            return health_import.summarize(
                health_import.iter_health_rows(
                    xml_path,
                    retention_days=SETTINGS.health_retention_days,
                )
            )

        import_started = datetime.now(timezone.utc).isoformat()
        rows, summary = await asyncio.to_thread(parse_to_rows)
        inserted, duplicates = await upsert_health_rows(user_id, rows)
        await sync_health_measurements_to_facts(user_id)
        profile = await save_routine_profile(user_id)
        import_completed = datetime.now(timezone.utc).isoformat()
        # Get total stored records for user
        total_row = await DB.fetch_one(
            "SELECT COUNT(*) AS cnt FROM health WHERE user_id=?",
            (user_id,),
        )
        total_stored = int(total_row["cnt"]) if total_row else 0
        await write_audit(
            user_id,
            "health_import",
            "health",
            None,
            inserted=inserted,
            duplicates=duplicates,
            source=audit_source,
        )
        await event_log.append_event(
            DB,
            user_id,
            "local_health_import_completed",
            entity="health",
            source=audit_source,
            properties={
                "inserted": inserted,
                "duplicates": duplicates,
                "total_rows_in_file": summary.rows,
                "source_date_range": f"{summary.min_date}..{summary.max_date}",
                "total_stored": total_stored,
            },
        )
        return HealthImportOutcome(
            inserted=inserted,
            duplicates=duplicates,
            updated=0,
            invalid=summary.invalid_records,
            summary=summary,
            profile=profile,
            source_file=path.name,
            total_stored=total_stored,
            import_started=import_started,
            import_completed=import_completed,
        )
    finally:
        with suppress(Exception):
            if extract_dir.exists():
                shutil.rmtree(extract_dir, ignore_errors=True)


async def _finish_health_import_flow(user_id: int) -> None:
    with suppress(Exception):
        current_flow = await conversation.get_active_flow(DB, user_id)
        if current_flow.name == conversation.FlowName.health_import:
            resumed = await conversation.resume_suspended(DB, user_id)
            if resumed is None:
                await conversation.clear_active_flow(DB, user_id)


@runtime_bound(RUNTIME_NAMES)
async def _begin_health_import_flow(user_id: int, *, source: str) -> bool:
    route_decision = await conversation.ConversationRouter.route(DB, user_id, "document")
    if route_decision.flow.name == conversation.FlowName.health_import:
        return False
    await conversation.set_active_flow(
        DB,
        user_id,
        conversation.FlowName.health_import,
        step="processing",
        payload={"source": source},
        suspend_current=not route_decision.flow.is_idle,
        expiry_minutes=120,
    )
    return True


@runtime_bound(RUNTIME_NAMES)
async def command_import(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if not await is_allowed(update):
        return
    user_id = await ensure_user(update)
    await track_event(user_id, "command_import")
    roots = allowed_roots_from_text(SETTINGS.local_health_import_allowed_roots)
    roots_text = "\n".join(f"• <code>{esc(str(root))}</code>" for root in roots)
    await update.effective_message.reply_text(
        "<b>ייבוא Apple Health בדיעבד</b>\n\n"
        "אפשר באחת משתי דרכים:\n"
        "1. לצרף כאן קובץ ZIP/XML.\n"
        "2. כשהבוט רץ על אותו מחשב Windows — לשלוח בצ'אט את הנתיב המלא "
        "לקובץ או לתיקייה שמכילה אותו.\n\n"
        "דוגמה:\n"
        "<code>C:\\Users\\user\\Desktop\\Apple Health Export</code>\n\n"
        "התיקיות המורשות כרגע:\n"
        f"{roots_text}\n\n"
        "הייבוא הוא תקופתי בלבד; סנכרון HealthKit שוטף כבוי כברירת מחדל.",
        parse_mode=ParseMode.HTML,
    )


@runtime_bound(RUNTIME_NAMES)
async def command_import_path(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_allowed(update):
        return
    user_id = await ensure_user(update)
    text = (update.effective_message.text or "").partition(" ")[2].strip()
    if not text:
        await command_import(update, context)
        return
    await try_handle_local_health_path(update, context, user_id, text, force=True)


@runtime_bound(RUNTIME_NAMES)
async def command_flags(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if not await is_allowed(update):
        return
    user_id = await ensure_user(update)
    await track_event(user_id, "command_flags")
    text = (update.effective_message.text or "").partition(" ")[2].strip()
    if not text:
        flags = await get_daily_flags(user_id)
        await update.effective_message.reply_text(
            "כתוב אחרי /flags את האינדיקציות להיום, למשל:\n"
            "<code>/flags ריטלין, צום עד 14:00, כתף תפוסה</code>\n\n"
            f"אינדיקציות נוכחיות להיום: {flags or 'אין'}",
            parse_mode=ParseMode.HTML,
        )
        return
    flags = await get_daily_flags(user_id)
    flags["notes"] = text
    lowered = text.casefold()
    ritalin_negated = bool(re.search(r"(?:לא|בלי|טרם)\s+(?:לקחתי\s+)?ריטלין", lowered))
    fasting_negated = bool(re.search(r"(?:לא|בלי|איני|אני לא)\s+(?:ב)?צום", lowered))
    if "ריטלין" in text:
        flags["ritalin"] = not ritalin_negated
    if "צום" in text:
        flags["fasting"] = not fasting_negated
    await set_daily_flags(user_id, flags)
    await write_audit(user_id, "daily_flags", "flags", None, text=text)
    await update.effective_message.reply_text("נרשם להיום. אתאים את ההמלצות בהתאם 👍")


@runtime_bound(RUNTIME_NAMES)
async def try_handle_local_health_path(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    text: str,
    *,
    force: bool = False,
) -> bool:
    """Import a local export path sent in chat; return whether it owned the turn."""
    if not force and not looks_like_local_health_path(text):
        return False
    message = update.effective_message
    if not SETTINGS.enable_local_health_path_import:
        await message.reply_text("ייבוא מנתיב מקומי כבוי בהגדרות.")
        return True
    chat = update.effective_chat
    if chat is not None and getattr(chat, "type", "private") != "private":
        await message.reply_text("מטעמי פרטיות, שלח נתיב מקומי רק בצ'אט הפרטי עם הבוט.")
        return True
    if not await _begin_health_import_flow(user_id, source="local_path"):
        await message.reply_text("כבר מתבצע ייבוא Health. אמתין לסיום לפני ייבוא נוסף.")
        return True

    progress = await message.reply_text("📂 בודק את הנתיב ומייבא את קובץ הייצוא…")
    await track_event(user_id, "health_import_started", source="local_path")
    await event_log.append_event(
        DB, user_id, "local_health_import_started",
        entity="health", source="local_path",
    )
    try:
        roots = allowed_roots_from_text(SETTINGS.local_health_import_allowed_roots)
        selection = resolve_local_health_export(
            text,
            allowed_roots=roots,
            max_bytes=SETTINGS.health_import_max_local_mb * 1024 * 1024,
            recursive=SETTINGS.local_health_import_recursive,
        )
        outcome = await import_health_export_file(
            user_id,
            selection.path,
            max_bytes=SETTINGS.health_import_max_local_mb * 1024 * 1024,
            audit_source="local_path",
        )
        selected_note = (
            f"\nנבחר הקובץ: <code>{esc(selection.path.name)}</code>"
            if selection.from_directory
            else ""
        )
        await progress.edit_text(
            _health_import_success_text(outcome)
            + await _health_import_followup_text(user_id)
            + selected_note,
            parse_mode=ParseMode.HTML,
        )
        await event_log.append_event(
            DB,
            user_id,
            "health_export_freshness_calculated",
            entity="health",
            source="local_path",
            properties={
                "newest_record": outcome.summary.max_date,
                "inserted": outcome.inserted,
            },
        )
        await track_event(
            user_id,
            "health_import_completed",
            source="local_path",
            inserted=outcome.inserted,
            duplicates=outcome.duplicates,
            candidate_count=selection.candidate_count,
        )
        if await onboarding.is_onboarding(DB, user_id):
            await show_onboarding_basics(message, user_id)
        else:
            await run_post_import_reconciliation(message, user_id)
    except LocalHealthPathError as exc:
        await track_event(user_id, "health_import_failed", source="local_path", kind="path")
        await event_log.append_event(
            DB, user_id, "local_health_import_failed",
            entity="health", source="local_path",
            properties={"kind": "path", "error": str(exc)[:200]},
        )
        await progress.edit_text(
            "<b>לא הצלחתי לקרוא את הנתיב.</b>\n\n"
            f"{esc(str(exc))}\n\n"
            "הנתיב חייב להיות קיים על אותו מחשב שמריץ את הבוט.",
            parse_mode=ParseMode.HTML,
        )
    except Exception as exc:  # noqa: BLE001
        LOGGER.exception("Local Health export import failed")
        await track_event(user_id, "health_import_failed", source="local_path", kind="import")
        await event_log.append_event(
            DB, user_id, "local_health_import_failed",
            entity="health", source="local_path",
            properties={"kind": "import", "error": type(exc).__name__},
        )
        if context is not None:
            with suppress(Exception):
                await notify_admin(
                    context.bot,
                    f"Local Health import failed for {user_id}: {exc!r}",
                )
        await progress.edit_text(friendly_error(exc, "health import"))
    finally:
        await _finish_health_import_flow(user_id)
    return True


@runtime_bound(RUNTIME_NAMES)
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_allowed(update):
        return
    user_id = await ensure_user(update)
    message = update.effective_message
    document = message.document
    name = (document.file_name or "").lower()
    if not (name.endswith(".zip") or name.endswith(".xml")):
        await message.reply_text("צריך קובץ ZIP (או XML) של ייצוא Apple Health.")
        return
    max_bytes = SETTINGS.health_import_max_upload_mb * 1024 * 1024
    if document.file_size and document.file_size > max_bytes:
        await message.reply_text(
            f"הקובץ גדול מדי. המגבלה בהעלאה דרך Telegram היא "
            f"{SETTINGS.health_import_max_upload_mb}MB. "
            "אפשר במקום זאת לשלוח את נתיב התיקייה המקומית."
        )
        return
    if not await _begin_health_import_flow(user_id, source="document"):
        await message.reply_text("כבר מתבצע ייבוא Health. אמתין לסיום לפני קובץ נוסף.")
        return

    progress = await message.reply_text("📥 מקבל את הקובץ ומפענח… זה עשוי לקחת דקה.")
    await track_event(user_id, "health_import_started", source="telegram_document")
    folder = Path(SETTINGS.storage_dir) / "health_import"
    folder.mkdir(parents=True, exist_ok=True)
    suffix = ".zip" if name.endswith(".zip") else ".xml"
    saved_path = folder / f"{user_id}_{secrets.token_hex(8)}{suffix}"

    try:
        telegram_file = await document.get_file()
        await telegram_file.download_to_drive(custom_path=str(saved_path))
        outcome = await import_health_export_file(
            user_id,
            saved_path,
            max_bytes=max_bytes,
            audit_source="telegram_document",
        )
        await progress.edit_text(
            _health_import_success_text(outcome)
            + await _health_import_followup_text(user_id),
            parse_mode=ParseMode.HTML,
        )
        await track_event(
            user_id,
            "health_import_completed",
            source="telegram_document",
            inserted=outcome.inserted,
            duplicates=outcome.duplicates,
        )
        if await onboarding.is_onboarding(DB, user_id):
            await show_onboarding_basics(message, user_id)
        else:
            await run_post_import_reconciliation(message, user_id)
    except Exception as exc:  # noqa: BLE001
        LOGGER.exception("Health import failed")
        await track_event(user_id, "health_import_failed", source="telegram_document")
        with suppress(Exception):
            await notify_admin(context.bot, f"Health import failed for {user_id}: {exc!r}")
        await progress.edit_text(friendly_error(exc, "health import"))
    finally:
        with suppress(Exception):
            saved_path.unlink(missing_ok=True)
        await _finish_health_import_flow(user_id)


@runtime_bound(RUNTIME_NAMES)
async def run_post_import_reconciliation(message: Any, user_id: int) -> None:
    """Surface at most one explainable insight from comparing reported vs actual
    data. Actionable proposals come with approve/decline buttons — never silent.
    """
    with suppress(Exception):
        insights = await reconcile.run_reconciliation(DB, user_id, TZ)
        if not insights:
            return
        top = insights[0]
        await track_event(
            user_id,
            "reconcile_insight",
            key=top.key,
            observations=top.observations,
        )
        lines = ["<b>🔍 מה ראיתי בנתונים</b>", "", top.summary]
        if top.proposal and top.proposal_action:
            lines += ["", top.proposal]
            keyboard = InlineKeyboardMarkup(
                [
                    [button("✅ כן, הפעל", f"reconcile_ok:{top.proposal_action}")],
                    [button("❌ לא, תודה", f"reconcile_no:{top.key}")],
                ]
            )
        else:
            keyboard = InlineKeyboardMarkup([[button("⬅️ תפריט", "menu:home")]])
        await message.reply_text("\n".join(lines), reply_markup=keyboard, parse_mode=ParseMode.HTML)


@runtime_bound(RUNTIME_NAMES)
async def apply_reconcile_proposal(user_id: int, action: str) -> str:
    """Apply an approved reconciliation proposal. Returns a confirmation line."""
    if action == "auto_hold_on_bad_sleep":
        # recommend_load already holds weight when sleep_quality == 'bad'. Record
        # the user's explicit opt-in so it's a confirmed preference.
        await user_model.set_fact(
            DB,
            user_id,
            "auto_hold_on_bad_sleep",
            True,
            kind=user_model.KIND_FACT,
            source=user_model.SOURCE_USER,
            confirmed=True,
            affects=("workout_volume",),
        )
        return "מעכשיו, בימים שתדווח על שינה גרועה, אשמור על המשקל במקום לעלות. אפשר לבטל בכל רגע."
    return "עודכן."


@runtime_bound(RUNTIME_NAMES)
async def send_to_user(context: CallbackContext, text: str) -> None:
    await context.bot.send_message(
        chat_id=SETTINGS.telegram_allowed_user_id,
        text=text,
        parse_mode=ParseMode.HTML,
    )


@runtime_bound(RUNTIME_NAMES)
async def morning_checkin_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Quick morning check-in — the real-time source (data is retrospective).

    Buttons are built from the user's known medications so reporting is a single
    tap, not a typed command.
    """
    rows: list[list[InlineKeyboardButton]] = []
    meds = await known_medications(user_id)
    # Use the index into the known-meds list as the callback id (med names can
    # be long or contain ':' which would break callback_data).
    for idx, med in enumerate(meds[:3]):
        rows.append([button(f"💊 לקחתי {med}", f"chk:med:{idx}")])
    rows.append([button("💊 תרופה אחרת", "chk:med_other")])
    rows.append(
        [
            button("😴 ישנתי טוב", "chk:sleep:good"),
            button("😐 בינוני", "chk:sleep:ok"),
            button("🥱 רע", "chk:sleep:bad"),
        ]
    )
    rows.append(
        [
            button("✅ יום רגיל", "chk:state:normal"),
            button("🤕 יש כאב", "chk:state:pain"),
            button("🕒 צום", "chk:state:fasting"),
        ]
    )
    rows.append(
        [
            button("⚡ אנרגיה טובה", "chk:energy:high"),
            button("😵 אנרגיה נמוכה", "chk:energy:low"),
        ]
    )
    rows.append([button("🎯 מה לעשות עכשיו", "menu:now")])
    return InlineKeyboardMarkup(rows)


@runtime_bound(RUNTIME_NAMES)
async def job_morning(context: CallbackContext) -> None:
    user_id = SETTINGS.telegram_allowed_user_id

    async def send_checkin() -> None:
        await context.bot.send_message(
            chat_id=user_id,
            text=("בוקר טוב! שלושה עדכונים קצרים יעזרו לי להתאים את היום — מה רלוונטי?"),
            reply_markup=await morning_checkin_keyboard(user_id),
            parse_mode=ParseMode.HTML,
        )

    await deliver_proactive_message(
        context,
        key="morning_checkin",
        sender=send_checkin,
        priority=JOB_PRIORITY_SCHEDULED,
        retry_callback=job_morning,
    )

    async def send_menu() -> None:
        await send_to_user(
            context,
            await build_morning_menu_text(user_id),
        )

    await deliver_proactive_message(
        context,
        key="morning_menu",
        sender=send_menu,
        priority=JOB_PRIORITY_SCHEDULED,
        counts_toward_budget=False,
        retry_callback=job_morning,
    )


@runtime_bound(RUNTIME_NAMES)
async def job_evening(context: CallbackContext) -> None:
    user_id = SETTINGS.telegram_allowed_user_id
    now = datetime.now(TZ)

    # On Saturday the weekly report replaces the daily recap when it was
    # delivered successfully, avoiding two similar summaries close together.
    if now.weekday() == 5:
        weekly = await DB.fetch_one(
            """
            SELECT 1
            FROM job_state
            WHERE user_id=? AND day=? AND key='weekly_summary'
              AND status='sent'
            """,
            (user_id, local_day_str()),
        )
        if weekly:
            with suppress(Exception):
                await save_routine_profile(user_id)
            return

    async def send_summary() -> None:
        await send_to_user(
            context,
            await build_evening_summary_text(user_id),
        )

    await deliver_proactive_message(
        context,
        key="evening",
        sender=send_summary,
        priority=JOB_PRIORITY_SCHEDULED,
        retry_callback=job_evening,
    )

    with suppress(Exception):
        await save_routine_profile(user_id)


@runtime_bound(RUNTIME_NAMES)
async def job_calorie_watch(context: CallbackContext) -> None:
    """Send the single most valuable nutrition intervention for this moment."""
    user_id = SETTINGS.telegram_allowed_user_id
    ctx = await build_daily_context(user_id)
    if ctx.calories_consumed <= 0:
        return

    target_cal = float(ctx.calorie_target)
    fraction_used = ctx.calories_consumed / target_cal if target_cal else 0

    # Over-pace is more actionable than a generic meal suggestion, so it wins
    # when both conditions become true in the same run.
    if fraction_used >= 0.8 and ctx.hours_left >= 5:

        async def send_overpace() -> None:
            await send_to_user(
                context,
                "⚠️ <b>שים לב לקצב</b>\nכבר צרכת "
                f"{ctx.calories_consumed:.0f} מתוך "
                f"{target_cal:.0f} קלוריות, ועוד "
                f"~{ctx.hours_left:.0f} שעות לפנינו היום. "
                "בוא נשמור על ארוחות קלות ועתירות חלבון "
                "מכאן עד הערב.",
            )

        sent = await deliver_proactive_message(
            context,
            key="overpace_alert",
            sender=send_overpace,
            priority=JOB_PRIORITY_HIGH,
            retry_callback=job_calorie_watch,
        )
        if sent:
            return

    if ctx.calories_consumed >= SETTINGS.intraday_calorie_trigger:

        async def send_nudge() -> None:
            await send_to_user(
                context,
                await build_next_meal_text(user_id, ctx),
            )

        await deliver_proactive_message(
            context,
            key="intraday_nudge",
            sender=send_nudge,
            priority=JOB_PRIORITY_COACHING,
            retry_callback=job_calorie_watch,
        )


@runtime_bound(RUNTIME_NAMES)
async def job_motivation(context: CallbackContext) -> None:
    """Send motivation only at a meaningful moment and within the message budget."""
    user_id = SETTINGS.telegram_allowed_user_id
    profile = await load_routine_profile(user_id)
    now = datetime.now(TZ)
    hour = now.hour + now.minute / 60.0

    def near(hhmm: str | None, window: float = 0.75) -> bool:
        if not hhmm:
            return False
        hh, mm = (int(x) for x in hhmm.split(":"))
        target = hh + mm / 60.0
        return abs(hour - target) <= window

    workout_hour = (profile.get("workout", {}) or {}).get("typical_hour")
    snack_hours = (profile.get("eating", {}) or {}).get("typical_meal_hours") or []

    if near(workout_hour, 1.0):
        moment = "pre_workout"
        hint = "כשעה לפני האימון הטיפוסי"
    elif any(near(value, 0.5) for value in snack_hours):
        moment = "snack_time"
        hint = "סביב שעת נשנוש טיפוסית"
    else:
        if random.random() > 0.08:
            return
        moment = "random"
        hint = ""

    async def send_motivation() -> None:
        message = await recommendations.motivation_message(
            OPENAI_CLIENT,
            SETTINGS.openai_model,
            hint,
        )
        await send_to_user(context, f"💪 {esc(message)}")

    await deliver_proactive_message(
        context,
        key=f"motivation_{moment}",
        sender=send_motivation,
        priority=JOB_PRIORITY_LOW,
        retry_callback=job_motivation,
    )


@runtime_bound(RUNTIME_NAMES)
def _ctx_has_workout(ctx: "DailyContext") -> bool:
    return ctx.workout_completed or ctx.is_usual_workout_day


@runtime_bound(RUNTIME_NAMES)
def _data_quality_disclaimer(ctx: "DailyContext") -> str:
    if not ctx.goal_computed:
        return (
            "\n\n⚠️ <i>היעדים עדיין לא חושבו מהנתונים שלך — "
            "ההמלצות מבוססות על ערכי ברירת מחדל. "
            "ככל שתדווח יותר, ההמלצות ישתפרו.</i>"
        )
    return ""


@runtime_bound(RUNTIME_NAMES)
async def build_morning_menu_text(user_id: int, ctx: "DailyContext | None" = None) -> str:
    if ctx is None:
        ctx = await build_daily_context(user_id)
    nutrition_context = await build_nutrition_context(
        DB,
        user_id,
        "morning_menu",
        daily_ctx=ctx,
    )
    nutrition_request = build_nutrition_ai_request(
        nutrition_context,
        "Build today's nutrition menu",
    )
    menu = await recommendations.morning_menu(
        OPENAI_CLIENT,
        SETTINGS.openai_model,
        ctx.profile,
        ctx.goal,
        _ctx_has_workout(ctx),
        ctx.flags,
        nutrition_request["context"],
    )
    text = format_morning_menu(menu)
    if not ctx.flags:
        text += "\n\n<i>המלצה זו נבנתה לפי השגרה שלך, כי עדיין לא התקבל עדכון בוקר להיום.</i>"
    text += _data_quality_disclaimer(ctx)
    return text


@runtime_bound(RUNTIME_NAMES)
async def build_next_meal_text(user_id: int, ctx: "DailyContext | None" = None) -> str:
    del ctx
    from noam_coach.services.next_meal import build_next_meal_response_text

    return await build_next_meal_response_text(DB, user_id)


@runtime_bound(RUNTIME_NAMES)
async def build_evening_summary_text(user_id: int, ctx: "DailyContext | None" = None) -> str:
    if ctx is None:
        ctx = await build_daily_context(user_id)
    nutrition_context = await build_nutrition_context(
        DB,
        user_id,
        "evening_summary",
        daily_ctx=ctx,
    )
    nutrition_request = build_nutrition_ai_request(
        nutrition_context,
        "Summarize today's nutrition",
    )
    items = await today_meal_items(user_id)
    summary = await recommendations.evening_summary(
        OPENAI_CLIENT,
        SETTINGS.openai_model,
        ctx.profile,
        ctx.goal,
        ctx.calories_consumed,
        ctx.protein_consumed,
        items,
        ctx.flags,
        nutrition_request["context"],
    )
    return format_evening_summary(summary) + _data_quality_disclaimer(ctx)
