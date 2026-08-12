"""Apple Watch workout companion routes."""
from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from config import SETTINGS
from db import DB
from models import WatchSetPayload
from noam_coach.api.health_routes import health_auth
from noam_coach.bot.workout import try_save_set
from noam_coach.runtime_bind import runtime_bound
from noam_coach.services import workout_slots
from noam_coach.services.core import ensure_user_record
from noam_coach.services.training import (
    LOAD_CHANNEL_WATCH,
    RIR_UNKNOWN,
    active_session,
    recommend_load,
    recommend_load_decision,
    record_load_decision,
)

router = APIRouter()
_RUNTIME = (
    "SETTINGS",
    "DB",
    "ensure_user_record",
    "active_session",
    "recommend_load",
    "recommend_load_decision",
    "record_load_decision",
    "try_save_set",
    "RIR_UNKNOWN",
    "workout_slots",
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
    # The watch is an external contract: a half-populated exercise object
    # renders as garbage on a device we do not control. A slot with no
    # implementation, or a malformed entry, is reported as a typed state rather
    # than passed through to recommend_load -- which would read `current["id"]`
    # and raise inside an API handler.
    entry_state = workout_slots.classify_entry(current)
    if not workout_slots.is_performable(current):
        workout_slots.observe_plan_entries(
            [current], user_id=user_id, session_id=session["id"],
            context="watch_current",
        )
        return {
            "active": True,
            "exercise_ready": False,
            "reason": entry_state,
            "exercise_index": exercise_index,
            "set_number": session["set_number"],
        }
    load_decision = await recommend_load_decision(user_id, current)
    weight, reps, _ = load_decision.to_tuple()
    recommendation_presented = True
    if session.get("pending_weight") is not None:
        weight = float(session["pending_weight"])
        recommendation_presented = False
    if session.get("pending_reps") is not None:
        reps = int(session["pending_reps"])
        recommendation_presented = False

    # A13 — this endpoint PRESENTS a prescription: the payload below is what
    # the Watch face shows and the athlete lifts to. It is therefore an
    # actionable recommendation like the Telegram card, in a different channel.
    #
    # Unlike the card, there is no "after presentation" inside this function --
    # the `return` IS the presentation. An awaited write here would sit in
    # front of every Watch poll, so recording is scheduled as an independent
    # task and the response is not held for it. `record_load_decision` cannot
    # raise, so the task cannot surface as an unretrieved exception.
    if recommendation_presented:
        asyncio.ensure_future(
            record_load_decision(
                user_id,
                load_decision,
                exercise_id=str(current.get("id") or ""),
                session_id=session["id"],
                set_number=session["set_number"],
                channel=LOAD_CHANNEL_WATCH,
            )
        )

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
