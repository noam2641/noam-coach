"""Restart-safe, single-source conversation flow engine.

All interactive state belongs to ``active_flow``.  Telegram handlers may keep
small caches, but they are never authoritative.  A user has one active flow;
short microflows can suspend a parent and resume it after completion.  Every
callback can carry the flow id and version, so stale keyboards cannot mutate
current state.
"""

from __future__ import annotations

import datetime as dt
import json
import secrets
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal

from helpers import utc_now

FLOW_EXPIRY_MINUTES = 60
LONG_FLOW_EXPIRY_MINUTES = 24 * 60


class FlowName(str, Enum):
    idle = "idle"
    onboarding_question = "onboarding_question"
    lifestyle_capture = "lifestyle_capture"
    profile_confirmation = "profile_confirmation"
    plan_frequency = "plan_frequency"
    deferred_question = "deferred_question"
    pain_location = "pain_location"
    avoidance_detail = "avoidance_detail"
    allergy_detail = "allergy_detail"
    equipment_detail = "equipment_detail"
    med_name = "med_name"
    basics_fix = "basics_fix"
    meal_logging = "meal_logging"
    meal_correction = "meal_correction"
    routine_confirm = "routine_confirm"
    confirm_number = "confirm_number"
    goal_review = "goal_review"
    nutrition_plan_selection = "nutrition_plan_selection"
    workout_plan_selection = "workout_plan_selection"
    unified_plan_review = "unified_plan_review"
    workout_parameter_edit = "workout_parameter_edit"
    workout_session = "workout_session"
    health_import = "health_import"


QUESTION_FLOWS = frozenset(
    {
        FlowName.onboarding_question,
        FlowName.lifestyle_capture,
        FlowName.plan_frequency,
        FlowName.deferred_question,
        FlowName.pain_location,
        FlowName.avoidance_detail,
        FlowName.allergy_detail,
        FlowName.equipment_detail,
        FlowName.med_name,
        FlowName.basics_fix,
        # Free-text corrections to the "describe your day" summary card
        # (pending key "__routine_confirm__") must route the same way any
        # other pending question does -- otherwise ConversationRouter.route()
        # sends the correction to the generic free-text/assistant fallback
        # instead of onboarding.handle_onboarding_text's dedicated
        # __routine_confirm__ branch, and the correction is silently lost.
        FlowName.routine_confirm,
    }
)

MICROFLOWS = frozenset(
    {
        FlowName.meal_logging,
        FlowName.meal_correction,
        FlowName.confirm_number,
        FlowName.pain_location,
        FlowName.avoidance_detail,
        FlowName.med_name,
        FlowName.health_import,
    }
)

LONG_FLOWS = frozenset(
    {
        FlowName.profile_confirmation,
        FlowName.goal_review,
        FlowName.nutrition_plan_selection,
        FlowName.workout_plan_selection,
        FlowName.unified_plan_review,
        FlowName.workout_parameter_edit,
        FlowName.workout_session,
    }
)

PENDING_KEY_TO_FLOW: dict[str, FlowName] = {
    "__plan_frequency__": FlowName.plan_frequency,
    "__pain_location__": FlowName.pain_location,
    "__avoidance_detail__": FlowName.avoidance_detail,
    "__allergy_detail__": FlowName.allergy_detail,
    "__equipment_detail__": FlowName.equipment_detail,
    "__med_name__": FlowName.med_name,
    "__basics_fix__": FlowName.basics_fix,
    "__routine_confirm__": FlowName.routine_confirm,
}


@dataclass
class ActiveFlow:
    name: FlowName = FlowName.idle
    step: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    version: int = 0
    updated_at: str = ""
    flow_id: str = ""
    status: str = "idle"
    expires_at: str | None = None
    parent_flow_id: str | None = None

    @property
    def is_idle(self) -> bool:
        return self.name == FlowName.idle

    @property
    def is_question(self) -> bool:
        return self.name in QUESTION_FLOWS

    @property
    def is_meal(self) -> bool:
        return self.name in {FlowName.meal_logging, FlowName.meal_correction}

    @property
    def suspended(self) -> dict[str, Any] | None:
        value = self.payload.get("suspended")
        return value if isinstance(value, dict) else None

    @property
    def is_expired(self) -> bool:
        if self.is_idle:
            return False
        text = self.expires_at
        if not text and self.updated_at:
            try:
                updated = dt.datetime.fromisoformat(self.updated_at)
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=dt.timezone.utc)
                expiry_minutes = (
                    LONG_FLOW_EXPIRY_MINUTES if self.name in LONG_FLOWS else FLOW_EXPIRY_MINUTES
                )
                return dt.datetime.now(dt.timezone.utc) - updated > dt.timedelta(
                    minutes=expiry_minutes
                )
            except (ValueError, TypeError):
                return False
        if not text:
            return False
        try:
            expiry = dt.datetime.fromisoformat(text)
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=dt.timezone.utc)
            return dt.datetime.now(dt.timezone.utc) >= expiry
        except (ValueError, TypeError):
            return False

    def snapshot(self) -> dict[str, Any]:
        return {
            "flow_id": self.flow_id,
            "name": self.name.value,
            "step": self.step,
            "payload": self.payload,
            "version": self.version,
            "status": self.status,
            "expires_at": self.expires_at,
            "parent_flow_id": self.parent_flow_id,
            "updated_at": self.updated_at,
        }


def _new_flow_id(user_id: int) -> str:
    return f"f-{user_id}-{secrets.token_hex(6)}"


def _expiry_for(flow: FlowName, expiry_minutes: int | None = None) -> str | None:
    if flow == FlowName.idle:
        return None
    minutes = expiry_minutes
    if minutes is None:
        minutes = LONG_FLOW_EXPIRY_MINUTES if flow in LONG_FLOWS else FLOW_EXPIRY_MINUTES
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=minutes)).isoformat()


def _safe_flow(value: str | None) -> FlowName:
    try:
        return FlowName(value or "idle")
    except ValueError:
        return FlowName.idle


async def get_active_flow(db: Any, user_id: int) -> ActiveFlow:
    row = await db.fetch_one(
        "SELECT * FROM active_flow WHERE user_id=?",
        (user_id,),
    )
    if not row:
        return ActiveFlow()
    payload = {}
    try:
        payload = json.loads(row.get("payload") or "{}")
    except (TypeError, json.JSONDecodeError):
        payload = {}
    return ActiveFlow(
        name=_safe_flow(row.get("flow")),
        step=row.get("step") or "",
        payload=payload,
        version=int(row.get("version") or 0),
        updated_at=row.get("updated_at") or "",
        flow_id=row.get("flow_id") or "",
        status=row.get("status") or "active",
        expires_at=row.get("expires_at"),
        parent_flow_id=row.get("parent_flow_id"),
    )


async def set_active_flow(
    db: Any,
    user_id: int,
    flow: FlowName,
    step: str = "",
    payload: dict[str, Any] | None = None,
    *,
    suspend_current: bool = False,
    expiry_minutes: int | None = None,
    flow_id: str | None = None,
) -> int:
    """Atomically replace the active flow and return its new version."""
    new_payload = dict(payload) if payload else {}
    current = await get_active_flow(db, user_id)
    if suspend_current and not current.is_idle:
        new_payload["suspended"] = current.snapshot()

    preserve_identity = (
        current.name == flow
        and current.flow_id
        and not suspend_current
        and flow != FlowName.idle
    )
    new_flow_id = (
        "" if flow == FlowName.idle else (flow_id or (current.flow_id if preserve_identity else _new_flow_id(user_id)))
    )
    parent_flow_id = current.flow_id if suspend_current and current.flow_id else None
    now = utc_now()
    expires_at = _expiry_for(flow, expiry_minutes)
    status = "idle" if flow == FlowName.idle else "active"

    async with db.transaction() as conn:
        cursor = await conn.execute(
            "SELECT version FROM active_flow WHERE user_id=?",
            (user_id,),
        )
        row = await cursor.fetchone()
        version = (int(row["version"]) + 1) if row else 1
        await conn.execute(
            """
            INSERT INTO active_flow(
                user_id, flow, step, payload, version, updated_at,
                flow_id, status, expires_at, parent_flow_id
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                flow=excluded.flow,
                step=excluded.step,
                payload=excluded.payload,
                version=excluded.version,
                updated_at=excluded.updated_at,
                flow_id=excluded.flow_id,
                status=excluded.status,
                expires_at=excluded.expires_at,
                parent_flow_id=excluded.parent_flow_id
            """,
            (
                user_id,
                flow.value,
                step,
                json.dumps(new_payload, ensure_ascii=False),
                version,
                now,
                new_flow_id,
                status,
                expires_at,
                parent_flow_id,
            ),
        )
        return version


async def update_flow(
    db: Any,
    user_id: int,
    *,
    step: str | None = None,
    payload_patch: dict[str, Any] | None = None,
    expiry_minutes: int | None = None,
) -> ActiveFlow:
    current = await get_active_flow(db, user_id)
    if current.is_idle:
        return current
    payload = dict(current.payload)
    if payload_patch:
        payload.update(payload_patch)
    await set_active_flow(
        db,
        user_id,
        current.name,
        step=current.step if step is None else step,
        payload=payload,
        expiry_minutes=expiry_minutes,
        flow_id=current.flow_id,
    )
    return await get_active_flow(db, user_id)


async def clear_active_flow(db: Any, user_id: int) -> int:
    return await set_active_flow(db, user_id, FlowName.idle, payload={})


async def resume_suspended(db: Any, user_id: int) -> ActiveFlow | None:
    current = await get_active_flow(db, user_id)
    suspended = current.suspended
    if not suspended:
        return None
    name = _safe_flow(str(suspended.get("name") or suspended.get("flow") or "idle"))
    payload = dict(suspended.get("payload") or {})
    payload.pop("suspended", None)
    await set_active_flow(
        db,
        user_id,
        name,
        step=str(suspended.get("step") or ""),
        payload=payload,
        flow_id=str(suspended.get("flow_id") or _new_flow_id(user_id)),
    )
    return await get_active_flow(db, user_id)


async def clear_all_flows(db: Any, user_id: int) -> int:
    return await clear_active_flow(db, user_id)


async def expire_if_needed(db: Any, user_id: int) -> ActiveFlow:
    current = await get_active_flow(db, user_id)
    if current.is_expired:
        await clear_active_flow(db, user_id)
        return ActiveFlow(version=current.version + 1)
    return current


def check_version(
    flow: ActiveFlow,
    callback_version: int | None,
    callback_flow_id: str | None = None,
) -> bool:
    if callback_version is not None and flow.version != callback_version:
        return False
    if callback_flow_id and flow.flow_id and callback_flow_id != flow.flow_id:
        return False
    return True


def encode_callback(
    prefix: str,
    *parts: str,
    version: int | None = None,
    flow_id: str | None = None,
) -> str:
    fields = [prefix, *[str(part) for part in parts]]
    if flow_id:
        fields.append(f"f{flow_id}")
    if version is not None:
        fields.append(f"v{version}")
    data = ":".join(fields)
    if len(data.encode("utf-8")) > 64:
        raise ValueError("Telegram callback_data exceeds 64 bytes")
    return data


def extract_version(data: str) -> int | None:
    for part in reversed(data.split(":")):
        if part.startswith("v") and part[1:].isdigit():
            return int(part[1:])
    return None


def extract_flow_id(data: str) -> str | None:
    for part in reversed(data.split(":")):
        if len(part) > 1 and part[0] == "f":
            return part[1:]
    return None


EventKind = Literal["text", "photo", "document", "callback", "command", "system"]


@dataclass(frozen=True)
class RouteDecision:
    handler: str
    action: Literal["consume", "interrupt", "fallback", "expired"]
    flow: ActiveFlow
    reason: str


class ConversationRouter:
    """Pure routing policy shared by Telegram and tests.

    The router does not execute domain handlers.  It selects one handler, which
    prevents the old pattern where several handlers inspected the same message
    and whichever happened to run first consumed it.
    """

    @staticmethod
    async def route(db: Any, user_id: int, kind: EventKind, *, command: str = "") -> RouteDecision:
        flow = await expire_if_needed(db, user_id)
        if command in {"cancel_all", "reset"}:
            return RouteDecision("cancel_all", "consume", flow, "explicit global cancellation")
        if command in {"home", "cancel", "back"}:
            return RouteDecision("navigation", "consume", flow, "explicit navigation")
        if flow.is_idle:
            return RouteDecision("free_text" if kind == "text" else kind, "fallback", flow, "no active flow")
        if kind == "document":
            if flow.name == FlowName.health_import:
                return RouteDecision("health_import", "consume", flow, "health import already active")
            return RouteDecision("health_import", "interrupt", flow, "health import microflow interrupts parent")
        if kind == "photo" and not flow.is_meal:
            return RouteDecision("meal_logging", "interrupt", flow, "meal microflow interrupts parent")
        if flow.is_meal:
            return RouteDecision("meal_flow", "consume", flow, "active meal flow owns the event")
        if flow.name == FlowName.workout_session:
            return RouteDecision("workout_flow", "consume", flow, "active workout owns the event")
        if flow.name == FlowName.workout_parameter_edit:
            return RouteDecision("workout_parameter_flow", "consume", flow, "active workout parameter edit owns the event")
        if flow.is_question:
            return RouteDecision("question_flow", "consume", flow, "active question owns the event")
        if flow.name in {
            FlowName.goal_review,
            FlowName.nutrition_plan_selection,
            FlowName.workout_plan_selection,
            FlowName.unified_plan_review,
            FlowName.profile_confirmation,
        }:
            return RouteDecision("selection_flow", "consume", flow, "active selection/review flow")
        return RouteDecision("free_text", "fallback", flow, "no specialized route")
