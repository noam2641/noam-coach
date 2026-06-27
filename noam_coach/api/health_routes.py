"""HealthKit and Apple Shortcuts ingestion routes."""
from __future__ import annotations

import hmac
from datetime import timezone

import aiosqlite
from fastapi import APIRouter, Depends, Header, HTTPException

import health_import
from config import SETTINGS, TZ
from db import DB
from health_service import (
    save_routine_profile,
    sync_health_measurements_to_facts,
    upsert_health_rows,
)
from helpers import utc_now
from models import HealthBatch, ShortcutHealthPayload
from noam_coach.runtime_bind import runtime_bound
from noam_coach.services.core import ensure_user_record

router = APIRouter()
_RUNTIME = (
    "SETTINGS",
    "TZ",
    "DB",
    "ensure_user_record",
    "upsert_health_rows",
    "sync_health_measurements_to_facts",
    "save_routine_profile",
)


@runtime_bound(_RUNTIME)
async def health_auth(authorization: str | None = Header(default=None)) -> None:
    if not SETTINGS.enable_healthkit_api:
        raise HTTPException(status_code=404, detail="Live HealthKit sync is disabled")
    expected = SETTINGS.healthkit_api_token
    if not expected or not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized")
    provided = authorization.removeprefix("Bearer ")
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")


@runtime_bound(_RUNTIME)
async def insert_health_sample(
    user_id: int,
    external_id: str,
    sample_type: str,
    value: float,
    unit: str,
    start_time: str,
    end_time: str | None = None,
    source_device: str | None = None,
) -> bool:
    try:
        await DB.execute(
            """
            INSERT INTO health(
                user_id, external_id, sample_type, value, unit,
                start_time, end_time, source_device, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                external_id,
                sample_type,
                value,
                unit,
                start_time,
                end_time,
                source_device,
                utc_now(),
            ),
        )
        return True
    except aiosqlite.IntegrityError:
        return False


@router.post("/api/healthkit/samples", dependencies=[Depends(health_auth)])
@runtime_bound(_RUNTIME)
async def healthkit_samples(batch: HealthBatch) -> dict[str, int]:
    if batch.telegram_user_id != SETTINGS.telegram_allowed_user_id:
        raise HTTPException(status_code=403, detail="Forbidden")

    await ensure_user_record(batch.telegram_user_id)
    rows = [
        health_import.HealthRow(
            external_id=sample.external_id,
            sample_type=sample.sample_type,
            value=sample.value,
            unit=sample.unit,
            start_time=sample.start_time.astimezone(timezone.utc).isoformat(),
            end_time=(
                sample.end_time.astimezone(timezone.utc).isoformat() if sample.end_time else None
            ),
            source_device=sample.source_device,
        )
        for sample in batch.samples
    ]
    inserted, duplicates = await upsert_health_rows(batch.telegram_user_id, rows)
    if inserted:
        await sync_health_measurements_to_facts(batch.telegram_user_id)
        await save_routine_profile(batch.telegram_user_id)
    return {"inserted": inserted, "duplicated": duplicates}


@router.post("/api/shortcut/health", dependencies=[Depends(health_auth)])
@runtime_bound(_RUNTIME + ("insert_health_sample",))
async def shortcut_health(payload: ShortcutHealthPayload) -> dict[str, int]:
    if payload.telegram_user_id != SETTINGS.telegram_allowed_user_id:
        raise HTTPException(status_code=403, detail="Forbidden")

    await ensure_user_record(payload.telegram_user_id)
    measured_at = payload.measured_at.astimezone(timezone.utc).isoformat()
    local_day = payload.measured_at.astimezone(TZ).date().isoformat()
    values = [
        ("weight", payload.weight_kg, "kg"),
        ("steps", payload.steps, "count"),
        ("active_calories", payload.active_calories, "kcal"),
        ("sleep_minutes", payload.sleep_minutes, "min"),
        ("resting_heart_rate", payload.resting_heart_rate, "bpm"),
        ("hrv", payload.hrv_ms, "ms"),
    ]
    cumulative = {"steps", "active_calories", "sleep_minutes"}
    inserted = 0
    updated = 0
    duplicates = 0

    for sample_type, value, unit in values:
        if value is None:
            continue
        if sample_type in cumulative:
            external_id = f"shortcut:{payload.telegram_user_id}:{sample_type}:{local_day}"
            existing = await DB.fetch_one(
                "SELECT id FROM health WHERE user_id=? AND external_id=?",
                (payload.telegram_user_id, external_id),
            )
            await DB.execute(
                """
                INSERT INTO health(
                    user_id, external_id, sample_type, value, unit,
                    start_time, end_time, source_device, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, NULL, ?, ?)
                ON CONFLICT(user_id, external_id) DO UPDATE SET
                    value=excluded.value, start_time=excluded.start_time
                """,
                (
                    payload.telegram_user_id,
                    external_id,
                    sample_type,
                    float(value),
                    unit,
                    measured_at,
                    "Apple Shortcuts",
                    utc_now(),
                ),
            )
            if existing:
                updated += 1
            else:
                inserted += 1
            continue

        external_id = f"shortcut:{payload.telegram_user_id}:{sample_type}:{measured_at}"
        saved = await insert_health_sample(
            payload.telegram_user_id,
            external_id,
            sample_type,
            float(value),
            unit,
            measured_at,
            None,
            "Apple Shortcuts",
        )
        inserted += int(saved)
        duplicates += int(not saved)

    if inserted or updated:
        await sync_health_measurements_to_facts(payload.telegram_user_id)
        await save_routine_profile(payload.telegram_user_id)
    return {"inserted": inserted, "updated": updated, "duplicated": duplicates}
