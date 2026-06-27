"""Data retention — periodic cleanup of expired photos and operational data.

Extracted from coach_bot.py to keep the main module focused on Telegram
and API handling.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path

import db as _db
from config import LOGGER, SETTINGS
from helpers import utc_now


async def cleanup_photos_once() -> dict[str, int]:
    """Apply photo retention to meals, abandoned approvals and orphan files."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=SETTINGS.photo_retention_days)
    cutoff_text = cutoff.isoformat()
    deleted_meal_images = 0
    expired_approvals = 0
    deleted_orphans = 0

    rows = await _db.DB.fetch_all(
        """
        SELECT id, image_path, created_at
        FROM meals
        WHERE image_path IS NOT NULL AND created_at<?
        """,
        (cutoff_text,),
    )
    for row in rows:
        path = Path(row["image_path"])
        with suppress(Exception):
            await asyncio.to_thread(path.unlink, missing_ok=True)
        await _db.DB.execute(
            "UPDATE meals SET image_path=NULL WHERE id=? AND image_path=?",
            (row["id"], row["image_path"]),
        )
        deleted_meal_images += 1

    pending = await _db.DB.fetch_all(
        """
        SELECT id, payload
        FROM approvals
        WHERE kind IN ('meal','meal_edit','meal_duplicate') AND status='pending' AND created_at<?
        """,
        (cutoff_text,),
    )
    for row in pending:
        changed = await _db.DB.execute_rowcount(
            """
            UPDATE approvals
            SET status='expired', decided_at=?
            WHERE id=? AND status='pending'
            """,
            (utc_now(), row["id"]),
        )
        if not changed:
            continue
        try:
            payload = json.loads(row["payload"])
        except (TypeError, json.JSONDecodeError):
            payload = {}
        image = payload.get("image")
        if image:
            with suppress(Exception):
                await asyncio.to_thread(Path(image).unlink, missing_ok=True)
        await _db.DB.execute(
            "DELETE FROM active_flow WHERE flow='meal_fix' AND step=?",
            (row["id"],),
        )
        expired_approvals += 1

    referenced: set[Path] = set()
    linked_rows = await _db.DB.fetch_all("SELECT image_path FROM meals WHERE image_path IS NOT NULL")
    for row in linked_rows:
        referenced.add(Path(row["image_path"]).resolve())
    approval_rows = await _db.DB.fetch_all(
        "SELECT payload FROM approvals WHERE kind IN ('meal','meal_edit','meal_duplicate') AND status='pending'"
    )
    for row in approval_rows:
        with suppress(TypeError, json.JSONDecodeError):
            image = json.loads(row["payload"]).get("image")
            if image:
                referenced.add(Path(image).resolve())

    food_dir = Path(SETTINGS.storage_dir) / "food"
    if food_dir.exists():
        for path in food_dir.iterdir():
            if not path.is_file() or path.is_symlink():
                continue
            try:
                is_old = (
                    datetime.fromtimestamp(
                        path.stat().st_mtime,
                        tz=timezone.utc,
                    )
                    < cutoff
                )
                if is_old and path.resolve() not in referenced:
                    await asyncio.to_thread(path.unlink, missing_ok=True)
                    deleted_orphans += 1
            except OSError:
                continue

    return {
        "meal_images": deleted_meal_images,
        "approvals": expired_approvals,
        "orphans": deleted_orphans,
    }


async def cleanup_operational_data_once() -> dict[str, int]:
    """Apply explicit retention to high-volume operational tables."""
    now = datetime.now(timezone.utc)
    cutoffs = {
        "health": (now - timedelta(days=SETTINGS.health_retention_days)).isoformat(),
        "analytics_events": (now - timedelta(days=SETTINGS.analytics_retention_days)).isoformat(),
        "audit": (now - timedelta(days=SETTINGS.audit_retention_days)).isoformat(),
        "job_state": (now - timedelta(days=SETTINGS.job_state_retention_days)).isoformat(),
        "approvals": (now - timedelta(days=SETTINGS.approval_retention_days)).isoformat(),
    }
    deleted: dict[str, int] = {}
    deleted["health"] = await _db.DB.execute_rowcount(
        "DELETE FROM health WHERE start_time<?",
        (cutoffs["health"],),
    )
    deleted["analytics_events"] = await _db.DB.execute_rowcount(
        "DELETE FROM analytics_events WHERE created_at<?",
        (cutoffs["analytics_events"],),
    )
    deleted["audit"] = await _db.DB.execute_rowcount(
        "DELETE FROM audit WHERE created_at<?",
        (cutoffs["audit"],),
    )
    deleted["job_state"] = await _db.DB.execute_rowcount(
        "DELETE FROM job_state WHERE created_at<?",
        (cutoffs["job_state"],),
    )
    deleted["approvals"] = await _db.DB.execute_rowcount(
        """
        DELETE FROM approvals
        WHERE status!='pending'
          AND COALESCE(decided_at, created_at)<?
        """,
        (cutoffs["approvals"],),
    )
    return deleted


async def cleanup_loop() -> None:
    """Run retention cleanup in an infinite loop (every 12 hours)."""
    while True:
        try:
            photo_result = await cleanup_photos_once()
            data_result = await cleanup_operational_data_once()
            if any(photo_result.values()) or any(data_result.values()):
                LOGGER.info(
                    "Retention cleanup: photos=%s data=%s",
                    photo_result,
                    data_result,
                )
        except Exception:  # noqa: BLE001
            LOGGER.exception("Retention cleanup failed")
        await asyncio.sleep(12 * 60 * 60)
