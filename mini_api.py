"""Mini App API endpoints — extracted from coach_bot.py.

All routes are registered on a FastAPI APIRouter and included by the main
application via ``api.include_router(mini_api.router)``.
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Cookie, Depends, Header, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

import coach_intelligence
import data_quality
import event_log
import miniapp
import planning
import training_intelligence
import user_model
from config import LOGGER, SETTINGS, TZ
from db import DB
from helpers import _safe_html_block, friendly_error, today_bounds_utc
from models import MiniProfileUpdate
from noam_coach.services.availability import resolve_availability
from noam_coach.services.next_meal import (
    format_next_meal_recommendation,
    generate_next_meal_recommendation,
    save_next_meal_workout_status,
    workout_clarification_actions,
)
from noam_coach.services.weekdays import (
    monday_first_to_sunday_first,
    sunday_first_to_monday_first,
    with_weekday_schema,
)

router = APIRouter()


# ---------------------------------------------------------------------------
# Session dependency
# ---------------------------------------------------------------------------


async def mini_session_user(
    noam_mini_session: str | None = Cookie(default=None),
) -> int:
    import coach_bot  # deferred to avoid circular import

    user_id = coach_bot.verify_mini_token(noam_mini_session or "", purpose="session")
    if user_id is None or user_id != SETTINGS.telegram_allowed_user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return user_id


# ---------------------------------------------------------------------------
# Observability O8 — client/server correlation + semantic view telemetry
# ---------------------------------------------------------------------------

_CLIENT_INTERACTION_RE = re.compile(r"^ci_[a-z0-9]{6,40}$")
_CLIENT_RENDER_RE = re.compile(r"^rn_[a-z0-9]{6,40}$")

# Only these semantic views/actions/events may be reported by the browser.
_MINI_VIEWS = frozenset({
    "dashboard", "operations", "next_meal", "today_meals",
    "plan_candidates", "profile", "health_upload",
})
_MINI_ACTIONS = frozenset({
    "next_meal_workout_status", "refresh_next_meal", "refresh_today_meals",
    "generate_plans", "activate_plan", "build_unified_plan", "save_profile",
    "health_file_selected", "health_import_submitted",
})
_MINI_TRIGGERS = frozenset({
    "initial_load", "user_action", "focus_refresh",
    "visibility_refresh", "post_mutation_refresh", "unknown",
})
_MAX_OBS_EVENTS_PER_BATCH = 20
_MAX_OBS_EVENT_CHARS = 4000


def _safe_client_interaction_id(raw: Any) -> str | None:
    if isinstance(raw, str) and _CLIENT_INTERACTION_RE.match(raw):
        return raw
    return None


def _client_trace_id(client_interaction_id: str) -> str:
    """Deterministic trace id for one Mini App client action — the action
    event, the API-side processing, and the resulting view render all derive
    the same trace without server-side state (never clock matching)."""
    return "tr_mini_" + client_interaction_id[3:]


async def mini_obs_scope(
    user_id: int = Depends(mini_session_user),
    x_obs_client_interaction: str | None = Header(default=None),
):
    """Session dependency that additionally joins the server-side trace to
    the client action that triggered this request (when the header carries a
    valid client interaction id)."""
    from noam_coach.observability.obs_context import interaction_scope

    client_id = _safe_client_interaction_id(x_obs_client_interaction)
    if client_id is None:
        with interaction_scope(user_id=user_id):
            yield user_id
    else:
        with interaction_scope(
            trace_id=_client_trace_id(client_id),
            interaction_id=client_id,
            user_id=user_id,
        ):
            yield user_id


@router.post("/mini/api/obs/events", include_in_schema=False)
async def mini_obs_events(
    payload: dict[str, Any] = Body(...),
    user_id: int = Depends(mini_session_user),
) -> JSONResponse:
    """Allowlisted, bounded, authenticated semantic view/action reporting.

    The browser may only report the two client event kinds, with allowlisted
    view/action/trigger vocabulary and a hard per-event size bound; anything
    else is rejected (counted, never persisted). Failures never break the
    Mini App — the endpoint always answers 200 with accept/reject counts.
    """
    from noam_coach.observability import taxonomy
    from noam_coach.observability.emit import emit_event

    raw_events = payload.get("events")
    if not isinstance(raw_events, list) or len(raw_events) > _MAX_OBS_EVENTS_PER_BATCH:
        raise HTTPException(status_code=422, detail="invalid events batch")

    accepted = 0
    rejected = 0
    for item in raw_events:
        try:
            if not isinstance(item, dict) or len(json.dumps(item, ensure_ascii=False)) > _MAX_OBS_EVENT_CHARS:
                rejected += 1
                continue
            kind = item.get("event")
            client_id = _safe_client_interaction_id(
                item.get("client_interaction_id")
                or item.get("caused_by_client_interaction_id")
            )
            trace_id = _client_trace_id(client_id) if client_id else None
            render_id = item.get("render_id")
            if not (isinstance(render_id, str) and _CLIENT_RENDER_RE.match(render_id)):
                render_id = None
            trigger = item.get("trigger")
            if trigger not in _MINI_TRIGGERS:
                trigger = "unknown"

            if kind == "ui.view.rendered":
                view = item.get("view")
                if view not in _MINI_VIEWS:
                    rejected += 1
                    continue
                await emit_event(
                    DB, user_id, taxonomy.UI_VIEW_RENDERED,
                    entity="mini_view", entity_id=view,
                    source="mini_app", surface="mini_app", status="rendered",
                    outcome=trigger,
                    trace_id=trace_id, interaction_id=client_id,
                    properties={
                        "view": view,
                        "render_id": render_id,
                        "trigger": trigger,
                        "caused_by_client_interaction_id": client_id,
                        "client_ts": item.get("client_ts"),
                    },
                    content={"state": item.get("state")} if item.get("state") else None,
                )
                accepted += 1
            elif kind == "ui.action.activated":
                action = item.get("action")
                source_view = item.get("source_view")
                if action not in _MINI_ACTIONS or source_view not in _MINI_VIEWS:
                    rejected += 1
                    continue
                source_render_id = item.get("source_render_id")
                if not (isinstance(source_render_id, str) and _CLIENT_RENDER_RE.match(source_render_id)):
                    source_render_id = None
                await emit_event(
                    DB, user_id, taxonomy.UI_ACTION_ACTIVATED,
                    entity="mini_action", entity_id=action,
                    source="mini_app", surface="mini_app", status="activated",
                    outcome=action,
                    trace_id=trace_id, interaction_id=client_id,
                    properties={
                        "action": action,
                        "source_view": source_view,
                        "source_render_id": source_render_id,
                        "client_interaction_id": client_id,
                        "client_ts": item.get("client_ts"),
                    },
                    content={"payload": item.get("payload")} if item.get("payload") else None,
                )
                accepted += 1
            else:
                rejected += 1
        except Exception:  # noqa: BLE001 — telemetry must not break the app.
            rejected += 1
    return JSONResponse({"accepted": accepted, "rejected": rejected})


# ---------------------------------------------------------------------------
# Dashboard & Profile
# ---------------------------------------------------------------------------


@router.get("/mini/api/dashboard", include_in_schema=False)
async def mini_dashboard(user_id: int = Depends(mini_obs_scope)) -> JSONResponse:
    import coach_bot

    snapshot = await planning.profile_snapshot(DB, user_id)
    goal = await planning.active_goal(DB, user_id)
    nutrition = await planning.get_active_plan(DB, user_id, "nutrition")
    workout = await planning.get_active_plan(DB, user_id, "workout")
    unified = await planning.get_active_plan(DB, user_id, "unified")
    availability = await resolve_availability(DB, user_id)
    calories, protein = await coach_bot.today_consumed(user_id)
    brief = await coach_intelligence.build_coaching_brief(
        DB,
        user_id,
        consumed_calories=calories,
        consumed_protein=protein,
        now_local_hour=datetime.now(TZ).hour,
    )
    return JSONResponse(
        {
            "today": {
                "calories": calories,
                "protein": protein,
                "goal": goal,
            },
            "readiness": snapshot["readiness"],
            "active_plans": {
                "nutrition": nutrition,
                "workout": workout,
                "unified": unified,
            },
            "missing": snapshot["missing"],
            "coaching": brief.to_dict(),
            "availability": availability.__dict__,
            "operations": await _operational_snapshot(user_id),
        }
    )


async def _operational_snapshot(user_id: int) -> dict[str, Any]:
    """Compact support/debug snapshot for the Mini App dashboard.

    This intentionally avoids raw logs or free-form message bodies. It exposes
    enough state to understand the user's current coaching situation without
    leaking extra Telegram/chat context.
    """
    import coach_bot

    pain_rows = await DB.fetch_all(
        "SELECT * FROM medical_constraints WHERE user_id=? AND kind='pain'",
        (user_id,),
    )
    active_pain = training_intelligence.active_pain_regions(pain_rows)
    latest_session = await DB.fetch_one(
        """
        SELECT id, code, name, status, exercise_index, set_number, started_at, ended_at
        FROM sessions
        WHERE user_id=?
        ORDER BY COALESCE(ended_at, started_at) DESC, id DESC
        LIMIT 1
        """,
        (user_id,),
    )
    active = await coach_bot.active_session(user_id)
    current_load = None
    if active:
        try:
            plan = json.loads(active["plan"])
            current = plan["exercises"][active["exercise_index"]]
            decision = await coach_bot.recommend_load_decision(user_id, current)
            current_load = decision.to_audit_dict()
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            current_load = {"error": "active_session_plan_unreadable"}
    return {
        "active_pain": [
            {
                "region": region.region,
                "label": region.label,
                "severity": region.severity,
                "age_days": round(region.age_days, 1),
            }
            for region in active_pain.values()
        ],
        "latest_session": dict(latest_session) if latest_session else None,
        "active_session": {
            "id": active["id"],
            "code": active["code"],
            "name": active["name"],
            "exercise_index": active["exercise_index"],
            "set_number": active["set_number"],
        }
        if active
        else None,
        "current_load_decision": current_load,
    }


@router.get("/mini/api/next-meal", include_in_schema=False)
async def mini_next_meal(user_id: int = Depends(mini_obs_scope)) -> JSONResponse:
    recommendation = await generate_next_meal_recommendation(DB, user_id)
    return JSONResponse(_next_meal_payload(recommendation))


@router.post("/mini/api/next-meal/workout-status", include_in_schema=False)
async def mini_next_meal_workout_status(
    payload: dict[str, Any] = Body(default_factory=dict),
    user_id: int = Depends(mini_obs_scope),
) -> JSONResponse:
    status = str(payload.get("status") or "").strip()
    if status not in {"later", "during", "completed", "cancelled"}:
        raise HTTPException(status_code=400, detail="Invalid workout status")
    # Resolve "now" once and reuse it for both the save and the immediately
    #-following recommendation rebuild. save_next_meal_workout_status stamps
    # next_meal_workout_status_at with this instant, and
    # user_state._explicit_clarification_candidate compares that stamp
    # against the recommendation's own "now" to decide whether the just-saved
    # clarification is still fresh (REC-ARCH-01 pass 3). Two independent
    # datetime.now() calls are consistent to within microseconds in real
    # production, but callers that build the recommendation from a
    # non-wall-clock "now" (tests, replay/simulation) need the save to use
    # that exact same instant or the clarification can read as already stale.
    now = datetime.now(TZ)
    await save_next_meal_workout_status(DB, user_id, status, now=now)
    recommendation = await generate_next_meal_recommendation(DB, user_id, now=now)
    return JSONResponse(_next_meal_payload(recommendation))


def _next_meal_payload(recommendation: Any) -> dict[str, Any]:
    status_map = {
        "later": "later",
        "during": "during",
        "done": "completed",
        "cancel": "cancelled",
    }
    action_rows = []
    for row in workout_clarification_actions(recommendation):
        action_rows.append(
            [
                {
                    "label": label,
                    "status": status_map.get(callback_data.rsplit(":", 1)[-1], ""),
                }
                for label, callback_data in row
            ]
        )
    return {
        "recommendation": recommendation.to_dict(),
        "text": format_next_meal_recommendation(recommendation),
        "actions": action_rows,
    }


@router.get("/mini/api/profile", include_in_schema=False)
async def mini_profile(user_id: int = Depends(mini_obs_scope)) -> JSONResponse:
    snapshot = await planning.profile_snapshot(DB, user_id)
    public = await user_model.get_profile_view(DB, user_id)
    availability = await resolve_availability(DB, user_id)
    availability_view = availability.__dict__.copy()
    availability_view["preferred_days"] = [
        monday_first_to_sunday_first(d) for d in availability_view.get("preferred_days") or []
    ]
    weekly_availability_fact = snapshot.get("facts", {}).get("weekly_availability")
    if isinstance(weekly_availability_fact, dict) and isinstance(weekly_availability_fact.get("value"), list):
        # The Mini App's day picker is Sunday-first (0=Sunday); convert the
        # Monday-first stored schema for display, mirroring the write-side
        # conversion in mini_update_profile.
        weekly_availability_fact = dict(weekly_availability_fact)
        weekly_availability_fact["value"] = [
            {**slot, "weekday": monday_first_to_sunday_first(slot["weekday"])}
            if isinstance(slot, dict) and "weekday" in slot
            else slot
            for slot in weekly_availability_fact["value"]
        ]
        snapshot = dict(snapshot)
        snapshot["facts"] = {**snapshot["facts"], "weekly_availability": weekly_availability_fact}
    return JSONResponse(
        {
            "snapshot": snapshot,
            "profile": public,
            "availability": availability_view,
        }
    )


@router.patch("/mini/api/profile", include_in_schema=False)
async def mini_update_profile(
    payload: MiniProfileUpdate,
    user_id: int = Depends(mini_obs_scope),
) -> JSONResponse:
    """Update only whitelisted user facts and keep plans versioned."""
    values = payload.model_dump(exclude_unset=True)
    changed: list[str] = []

    async def save(key: str, value: Any, affects: tuple[str, ...]) -> None:
        if value is None:
            return
        did_change = await user_model.set_fact(
            DB,
            user_id,
            key,
            value,
            kind=user_model.KIND_FACT,
            source=user_model.SOURCE_USER,
            confirmed=True,
            affects=affects,
        )
        if did_change:
            changed.append(key)

    if any(key in values for key in ("work_start", "work_end", "work_type")):
        current = await user_model.get_value(DB, user_id, "work_schedule")
        schedule = dict(current) if isinstance(current, dict) else {}
        if "work_start" in values:
            schedule["start"] = values.get("work_start")
        if "work_end" in values:
            schedule["end"] = values.get("work_end")
        if "work_type" in values:
            schedule["type"] = values.get("work_type")
        await save("work_schedule", schedule, ("meal_timing", "workout_timing", "weekly_plan"))

    if any(key in values for key in ("meal_break_time", "has_fridge", "has_microwave")):
        current = await user_model.get_value(DB, user_id, "meal_break_info")
        meal_break = dict(current) if isinstance(current, dict) else {}
        if "meal_break_time" in values:
            meal_break["time"] = values.get("meal_break_time")
        if "has_fridge" in values:
            meal_break["has_fridge"] = values.get("has_fridge")
        if "has_microwave" in values:
            meal_break["has_microwave"] = values.get("has_microwave")
        await save("meal_break_info", meal_break, ("meal_timing", "nutrition_plan"))

    direct = {
        "commute_minutes": ("workout_timing", "weekly_plan"),
        "cooking_capacity": ("nutrition_plan",),
        "food_budget_level": ("nutrition_plan", "shopping_plan"),
        "meal_structure_preference": ("nutrition_plan", "meal_timing"),
        "training_location": ("workout_plan",),
        "equipment": ("exercise_selection", "workout_plan"),
        "session_minutes": ("workout_plan", "weekly_plan"),
        "strength_experience": ("exercise_selection", "progression"),
        "diet_restrictions": ("nutrition_plan", "meal_analysis"),
        "allergies": ("nutrition_plan", "meal_analysis", "safety"),
        "coaching_style": ("message_style",),
        "notification_preference": ("notifications",),
    }
    for key, affects in direct.items():
        if key in values:
            await save(key, values[key], affects)
    if "weekly_availability" in values:
        # The Mini App's day picker is Sunday-first (0=Sunday), matching the
        # Israeli week, while every backend consumer stores/expects the
        # Monday-first schema (0=Monday) from noam_coach.services.weekdays.
        # Convert at this boundary so downstream planning never sees a
        # mismatched index.
        slots = []
        for slot in payload.weekly_availability or []:
            data = slot.model_dump() if hasattr(slot, "model_dump") else dict(slot)
            data["weekday"] = sunday_first_to_monday_first(data["weekday"])
            slots.append(with_weekday_schema(data))
        await save("weekly_availability", slots, ("workout_plan", "weekly_plan"))

    review_required = bool(changed) and bool(
        await planning.get_active_plan(DB, user_id, "nutrition")
        or await planning.get_active_plan(DB, user_id, "workout")
    )
    await event_log.append_event(
        DB,
        user_id,
        "PROFILE_UPDATED",
        entity="profile",
        source="mini_app",
        properties={"changed": changed, "review_required": review_required},
    )
    snapshot = await planning.profile_snapshot(DB, user_id)
    return JSONResponse(
        {
            "changed": changed,
            "review_required": review_required,
            "readiness": snapshot["readiness"],
            "missing": snapshot["missing"],
        }
    )


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------


@router.get("/mini/api/plans/{plan_type}", include_in_schema=False)
async def mini_list_plans(
    plan_type: str,
    user_id: int = Depends(mini_obs_scope),
) -> JSONResponse:
    if plan_type not in {"nutrition", "workout", "unified"}:
        raise HTTPException(status_code=400, detail="Invalid plan type")
    candidates = await planning.list_plan_candidates(DB, user_id, plan_type)  # type: ignore[arg-type]
    active = await planning.get_active_plan(DB, user_id, plan_type)  # type: ignore[arg-type]
    return JSONResponse({"candidates": candidates, "active": active})


@router.post("/mini/api/plans/{plan_type}/generate", include_in_schema=False)
async def mini_generate_plans(
    plan_type: str,
    user_id: int = Depends(mini_obs_scope),
) -> JSONResponse:
    if plan_type not in {"nutrition", "workout"}:
        raise HTTPException(status_code=400, detail="Invalid plan type")
    try:
        candidates = await planning.generate_candidates(DB, user_id, plan_type)  # type: ignore[arg-type]
    except planning.PlanningBlockedError as exc:
        raise HTTPException(
            status_code=409,
            detail={"message": str(exc), "missing": exc.missing},
        ) from exc
    await event_log.append_event(
        DB,
        user_id,
        "PLAN_CANDIDATES_GENERATED",
        entity="plan",
        source="mini_app",
        properties={"plan_type": plan_type, "count": len(candidates)},
    )
    return JSONResponse({"candidates": [candidate.to_dict() for candidate in candidates]})


@router.post("/mini/api/plans/{plan_id}/activate", include_in_schema=False)
async def mini_activate_plan(
    plan_id: int,
    user_id: int = Depends(mini_obs_scope),
) -> JSONResponse:
    try:
        selected = await planning.activate_plan(DB, user_id, plan_id)
    except planning.PlanningBlockedError as exc:
        raise HTTPException(
            status_code=409,
            detail={"message": str(exc), "missing": exc.missing},
        ) from exc
    if not selected:
        raise HTTPException(status_code=404, detail="Plan not found")
    await event_log.append_event(
        DB,
        user_id,
        "PLAN_ACTIVATED",
        entity="plan",
        entity_id=plan_id,
        source="mini_app",
        properties={"plan_type": selected["plan_type"]},
    )
    return JSONResponse({"active": selected})


@router.post("/mini/api/plans/unified/build", include_in_schema=False)
async def mini_build_unified_plan(
    user_id: int = Depends(mini_obs_scope),
) -> JSONResponse:
    try:
        candidate = await planning.build_unified_week(DB, user_id)
    except (ValueError, planning.PlanningBlockedError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse({"plan": candidate.to_dict()})


# ---------------------------------------------------------------------------
# Meals
# ---------------------------------------------------------------------------


@router.get("/mini/api/meals/today", include_in_schema=False)
async def mini_today_meals(user_id: int = Depends(mini_obs_scope)) -> JSONResponse:
    start, end = today_bounds_utc()
    rows = await DB.fetch_all(
        "SELECT * FROM meals WHERE user_id=? AND eaten_at>=? AND eaten_at<? ORDER BY eaten_at",
        (user_id, start, end),
    )
    quality = await data_quality.assess_day(DB, user_id, start, end)
    return JSONResponse({
        "meals": rows,
        "quality": {
            "score": quality.score,
            "usable": quality.usable,
            "confidence_label": quality.confidence_label,
            "issues": [issue.__dict__ for issue in quality.issues],
            "metrics": quality.metrics,
        },
    })


# ---------------------------------------------------------------------------
# Login & Static
# ---------------------------------------------------------------------------


@router.get("/mini/login", include_in_schema=False)
async def mini_login(token: str = "") -> RedirectResponse:
    import coach_bot

    user_id = await coach_bot.consume_mini_login_token(token)
    if user_id is None or user_id != SETTINGS.telegram_allowed_user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")
    session = coach_bot.make_mini_token(
        user_id,
        purpose="session",
        ttl_seconds=SETTINGS.mini_app_token_ttl_seconds,
    )
    response = RedirectResponse(url="/mini", status_code=303)
    response.set_cookie(
        "noam_mini_session",
        session,
        max_age=SETTINGS.mini_app_token_ttl_seconds,
        httponly=True,
        secure=SETTINGS.app_env == "production",
        samesite="strict",
        path="/mini",
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


_MINI_STATIC_TYPES = {
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}


@router.get("/mini/static/{filename}", include_in_schema=False)
async def mini_static(filename: str) -> Response:
    """Serve the Mini App's static JS/CSS from the package's static dir."""
    if filename not in {"app.js", "styles.css"}:
        raise HTTPException(status_code=404, detail="Not found")
    path = (miniapp.STATIC_DIR / filename).resolve()
    if miniapp.STATIC_DIR.resolve() not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    media_type = _MINI_STATIC_TYPES.get(path.suffix, "application/octet-stream")
    return Response(
        content=path.read_text(encoding="utf-8"),
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=300", "X-Content-Type-Options": "nosniff"},
    )


@router.get("/mini", response_class=HTMLResponse, include_in_schema=False)
async def mini_app(
    noam_mini_session: str | None = Cookie(default=None),
) -> HTMLResponse:
    import coach_bot

    user_id = coach_bot.verify_mini_token(noam_mini_session or "", purpose="session")
    if user_id is None or user_id != SETTINGS.telegram_allowed_user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")
    status = await coach_bot.build_daily_status(user_id)
    weekly = await coach_bot.build_weekly_summary_text(user_id)
    status_html = _safe_html_block(status)
    weekly_html = _safe_html_block(weekly)
    html_body = miniapp.render_index(status_html, weekly_html)
    return HTMLResponse(
        content=html_body,
        headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Frame-Options": "SAMEORIGIN",
        },
    )


# ---------------------------------------------------------------------------
# Health Upload
# ---------------------------------------------------------------------------


@router.post("/mini/upload", include_in_schema=False)
async def mini_upload(
    file: UploadFile,
    noam_mini_session: str | None = Cookie(default=None),
) -> JSONResponse:
    """Accept a ZIP/XML Apple Health export uploaded via the Mini App."""
    import coach_bot

    user_id = coach_bot.verify_mini_token(noam_mini_session or "", purpose="session")
    if user_id is None or user_id != SETTINGS.telegram_allowed_user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")

    name = (file.filename or "").lower()
    if not (name.endswith(".zip") or name.endswith(".xml")):
        raise HTTPException(status_code=400, detail="צריך קובץ ZIP או XML של Apple Health.")

    max_bytes = SETTINGS.health_import_max_upload_mb * 1024 * 1024
    folder = Path(SETTINGS.storage_dir) / "health_import"
    folder.mkdir(parents=True, exist_ok=True)
    suffix = ".zip" if name.endswith(".zip") else ".xml"
    saved_path = folder / f"{user_id}_{secrets.token_hex(8)}{suffix}"

    try:
        written = 0
        with open(saved_path, "wb") as destination:
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if written > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=(
                            "הקובץ גדול מדי. המגבלה היא "
                            f"{SETTINGS.health_import_max_upload_mb}MB."
                        ),
                    )
                await asyncio.to_thread(destination.write, chunk)

        await coach_bot.track_event(
            user_id,
            "health_import_started",
            source="mini_app",
        )
        try:
            outcome = await coach_bot.import_health_export_file(
                user_id,
                saved_path,
                max_bytes=max_bytes,
                audit_source="mini_app",
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        await coach_bot.track_event(
            user_id,
            "health_import_completed",
            source="mini_app",
            inserted=outcome.inserted,
            duplicates=outcome.duplicates,
        )
        msg = (
            "הנתונים נקלטו ✅\n"
            f"נשמרו {outcome.inserted} רשומות חדשות "
            f"({outcome.duplicates} כבר היו קיימות).\n"
            f"טווח: {outcome.summary.min_date} עד {outcome.summary.max_date}.\n"
            f"אימונים: {outcome.summary.workouts} | "
            f"לילות שינה: {outcome.summary.sleep_sessions}"
        )
        return JSONResponse({"message": msg})
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        LOGGER.exception("Health import via Mini App failed")
        await coach_bot.track_event(
            user_id,
            "health_import_failed",
            source="mini_app",
        )
        return JSONResponse(
            status_code=500,
            content={"detail": friendly_error(exc, "health import")},
        )
    finally:
        with suppress(Exception):
            saved_path.unlink(missing_ok=True)
