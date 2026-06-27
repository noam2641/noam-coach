"""Apple Watch workout companion routes."""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from config import SETTINGS
from db import DB
from models import WatchSetPayload
from noam_coach.api.health_routes import health_auth
from noam_coach.bot.workout import try_save_set
from noam_coach.runtime_bind import runtime_bound
from noam_coach.services.core import ensure_user_record
from noam_coach.services.training import RIR_UNKNOWN, active_session, recommend_load

router = APIRouter()
_RUNTIME = (
    "SETTINGS",
    "DB",
    "ensure_user_record",
    "active_session",
    "recommend_load",
    "try_save_set",
    "RIR_UNKNOWN",
)


@router.get("/api/watch/current/{user_id}", dependencies=[Depends(health_auth)])
@runtime_bound(_RUNTIME)
async def watch_current(user_id: int) -> dict[str, Any]:
    if user_id != SETTINGS.telegram_allowed_user_id:
        raise HTTPException(status_code=403, detail="Forbidden")

    session = await active_session(user_id)
    if not session:
        return {"active": False}

    plan = json.loads(session["plan"])
    exercises = plan["exercises"]
    exercise_index = session["exercise_index"]
    if exercise_index < 0 or exercise_index >= len(exercises):
        return {
            "active": False,
            "error": "Session exercise_index is out of range; session may be in an invalid state.",
        }
    current = exercises[exercise_index]
    weight, reps, _ = await recommend_load(user_id, current)
    if session.get("pending_weight") is not None:
        weight = float(session["pending_weight"])
    if session.get("pending_reps") is not None:
        reps = int(session["pending_reps"])

    return {
        "active": True,
        "session_id": session["id"],
        "workout_name": session["name"],
        "exercise_name": current["name"],
        "muscle": current.get("muscle"),
        "set_number": session["set_number"],
        "set_count": current["sets"],
        "weight": weight,
        "reps": reps,
        "reps_min": current["rmin"],
        "reps_max": current["rmax"],
        "rest_seconds": current["rest"],
    }


@router.post("/api/watch/set", dependencies=[Depends(health_auth)])
@runtime_bound(_RUNTIME)
async def watch_set(payload: WatchSetPayload) -> dict[str, Any]:
    if payload.telegram_user_id != SETTINGS.telegram_allowed_user_id:
        raise HTTPException(status_code=403, detail="Forbidden")

    await ensure_user_record(payload.telegram_user_id)
    existing = await DB.fetch_one(
        "SELECT id FROM sets WHERE client_event_id=?",
        (payload.client_event_id,),
    )
    if existing:
        return {"saved": True, "duplicate": True}

    session = await active_session(payload.telegram_user_id)
    if not session:
        raise HTTPException(status_code=409, detail="No active workout")

    plan = json.loads(session["plan"])
    exercises = plan["exercises"]
    exercise_index = session["exercise_index"]
    if exercise_index < 0 or exercise_index >= len(exercises):
        raise HTTPException(
            status_code=409,
            detail="Session exercise_index is out of range; session may be in an invalid state.",
        )
    current = exercises[exercise_index]
    recommended_weight, _, _ = await recommend_load(payload.telegram_user_id, current)
    result = await try_save_set(
        session,
        payload.weight if payload.weight is not None else recommended_weight,
        payload.reps,
        payload.rir if payload.rir is not None else RIR_UNKNOWN,
        "apple_watch",
        payload.client_event_id,
    )
    if result is None:
        return {"saved": True, "duplicate": True}
    completed, rest = result
    return {
        "saved": True,
        "duplicate": False,
        "completed": completed,
        "rest_seconds": rest,
    }
