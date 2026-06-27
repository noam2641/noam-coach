"""Mini App API endpoints — extracted from coach_bot.py.

All routes are registered on a FastAPI APIRouter and included by the main
application via ``api.include_router(mini_api.router)``.
"""

from __future__ import annotations

import asyncio
import secrets
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Cookie, Depends, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

import coach_intelligence
import data_quality
import event_log
import miniapp
import planning
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
# Dashboard & Profile
# ---------------------------------------------------------------------------


@router.get("/mini/api/dashboard", include_in_schema=False)
async def mini_dashboard(user_id: int = Depends(mini_session_user)) -> JSONResponse:
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
        }
    )


@router.get("/mini/api/next-meal", include_in_schema=False)
async def mini_next_meal(user_id: int = Depends(mini_session_user)) -> JSONResponse:
    recommendation = await generate_next_meal_recommendation(DB, user_id)
    return JSONResponse(_next_meal_payload(recommendation))


@router.post("/mini/api/next-meal/workout-status", include_in_schema=False)
async def mini_next_meal_workout_status(
    payload: dict[str, Any] = Body(default_factory=dict),
    user_id: int = Depends(mini_session_user),
) -> JSONResponse:
    status = str(payload.get("status") or "").strip()
    if status not in {"later", "during", "completed", "cancelled"}:
        raise HTTPException(status_code=400, detail="Invalid workout status")
    await save_next_meal_workout_status(DB, user_id, status)
    recommendation = await generate_next_meal_recommendation(DB, user_id)
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
async def mini_profile(user_id: int = Depends(mini_session_user)) -> JSONResponse:
    snapshot = await planning.profile_snapshot(DB, user_id)
    public = await user_model.get_profile_view(DB, user_id)
    availability = await resolve_availability(DB, user_id)
    return JSONResponse(
        {
            "snapshot": snapshot,
            "profile": public,
            "availability": availability.__dict__,
        }
    )


@router.patch("/mini/api/profile", include_in_schema=False)
async def mini_update_profile(
    payload: MiniProfileUpdate,
    user_id: int = Depends(mini_session_user),
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
        slots = [
            slot.model_dump() if hasattr(slot, "model_dump") else slot
            for slot in payload.weekly_availability or []
        ]
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
    user_id: int = Depends(mini_session_user),
) -> JSONResponse:
    if plan_type not in {"nutrition", "workout", "unified"}:
        raise HTTPException(status_code=400, detail="Invalid plan type")
    candidates = await planning.list_plan_candidates(DB, user_id, plan_type)  # type: ignore[arg-type]
    active = await planning.get_active_plan(DB, user_id, plan_type)  # type: ignore[arg-type]
    return JSONResponse({"candidates": candidates, "active": active})


@router.post("/mini/api/plans/{plan_type}/generate", include_in_schema=False)
async def mini_generate_plans(
    plan_type: str,
    user_id: int = Depends(mini_session_user),
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
    user_id: int = Depends(mini_session_user),
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
    user_id: int = Depends(mini_session_user),
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
async def mini_today_meals(user_id: int = Depends(mini_session_user)) -> JSONResponse:
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
