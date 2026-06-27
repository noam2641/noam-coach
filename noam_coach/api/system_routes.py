"""Liveness and readiness routes."""
from __future__ import annotations

import secrets
import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from config import APP_VERSION, RUNTIME_STATE, SETTINGS
from db import DB
from noam_coach.runtime_bind import runtime_bound

router = APIRouter()
_RUNTIME = ("APP_VERSION", "RUNTIME_STATE", "SETTINGS", "DB")


@router.get("/healthz")
@runtime_bound(_RUNTIME)
async def healthz() -> dict[str, str]:
    """Liveness only: the Python process can answer HTTP requests."""
    return {"status": "ok", "version": APP_VERSION}


@router.get("/readyz")
@runtime_bound(_RUNTIME)
async def readyz() -> Any:
    """Readiness: verify DB, storage, disk capacity and Telegram startup."""
    checks: dict[str, Any] = {
        "database": False,
        "storage": False,
        "disk_free_mb": 0,
        "telegram": RUNTIME_STATE.telegram_ready,
    }
    errors: list[str] = []

    try:
        row = await DB.fetch_one("SELECT 1 AS ok")
        checks["database"] = bool(row and row.get("ok") == 1)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"database:{type(exc).__name__}")

    storage = Path(SETTINGS.storage_dir)
    probe = storage / f".ready_{secrets.token_hex(4)}"
    try:
        storage.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        free_mb = shutil.disk_usage(storage).free // (1024 * 1024)
        checks["storage"] = True
        checks["disk_free_mb"] = int(free_mb)
        if free_mb < SETTINGS.readiness_min_free_mb:
            errors.append("disk:low_space")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"storage:{type(exc).__name__}")
        probe.unlink(missing_ok=True)

    ready = (
        RUNTIME_STATE.db_ready
        and checks["database"]
        and checks["storage"]
        and checks["telegram"]
        and not errors
    )
    payload = {
        "status": "ready" if ready else "not_ready",
        "version": APP_VERSION,
        "checks": checks,
        "errors": errors,
    }
    return JSONResponse(status_code=200 if ready else 503, content=payload)
