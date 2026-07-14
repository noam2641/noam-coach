"""Telegram ingress + routing observability (Observability O2).

Two stable boundaries, instrumented WITHOUT touching the pure routing policy
or the (protected) handler modules:

1. INGRESS — :func:`observed_handler` wraps each python-telegram-bot handler
   at registration time (``noam_coach/app/runtime.py``). It opens the
   interaction correlation scope, captures the pre-routing flow snapshot,
   and emits ``interaction.received`` (plus ``ui.control.activated`` for
   callbacks) before delegating to the real handler. Every ``emit_event``
   deeper in the same asyncio task inherits the correlation automatically.

2. ROUTING — :func:`install_routing_observer` wraps
   ``ConversationRouter.route`` and records ``routing.decided`` from the
   RETURNED decision (caller-side observation: the policy function itself
   stays pure and unit-testable; tests that don't install the observer see
   zero behavior change).

Trace creation/continuation policy (O2):
- text / photo / document / command → each interaction starts a NEW trace.
- callback → the interaction CONTINUES the trace of the render that
  presented the control, when the source-render lookup (batch O3) can prove
  it; until then — and whenever correlation cannot be proven — the callback
  starts its own trace and the ``ui.control.activated`` event explicitly
  says ``source_render_id=None`` (unresolved is represented, never guessed).
"""

from __future__ import annotations

import functools
from contextlib import suppress
from typing import Any, Awaitable, Callable

import conversation
from noam_coach.observability import taxonomy
from noam_coach.observability.emit import emit_event
from noam_coach.observability.obs_context import interaction_scope, span_scope

TelegramHandler = Callable[..., Awaitable[Any]]

SURFACE_TELEGRAM = "telegram"


def _facade_db() -> Any:
    """The live application Database via the patchable coach_bot facade."""
    import coach_bot

    return coach_bot.DB


def _allowed_user_id() -> int | None:
    from config import SETTINGS

    value = getattr(SETTINGS, "telegram_allowed_user_id", None)
    return int(value) if value is not None else None


def _bounded_flow_identity(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Flow identity for event properties — bounded, without the payload."""
    return {
        "flow_id": snapshot.get("flow_id"),
        "name": snapshot.get("name"),
        "step": snapshot.get("step"),
        "version": snapshot.get("version"),
        "status": snapshot.get("status"),
    }


async def _pre_routing_flow_snapshot(db: Any, user_id: int) -> dict[str, Any] | None:
    """The active flow BEFORE any routing/expiry side effects run."""
    try:
        flow = await conversation.get_active_flow(db, user_id)
        return flow.snapshot()
    except Exception:  # noqa: BLE001 — observability must not block ingress.
        return None


async def _trace_for_callback(
    db: Any, user_id: int, source_message_id: Any
) -> str | None:
    """Continuation lookup: trace of the render that presented this control.

    Batch O3: resolved from the render registry (the latest successfully
    delivered render on the callback's source message). Returning ``None``
    means "cannot prove" → the callback starts a fresh trace.
    """
    from noam_coach.observability.render_registry import find_render_for_message

    render = await find_render_for_message(db, user_id, source_message_id)
    return render.trace_id if render is not None else None


def _media_identity(message: Any) -> dict[str, Any] | None:
    """Provider media identity — references only, never bytes."""
    photos = list(getattr(message, "photo", None) or [])
    if photos:
        largest = photos[-1]
        return {
            "media_kind": "photo",
            "provider": "telegram",
            "file_id": getattr(largest, "file_id", None),
            "file_unique_id": getattr(largest, "file_unique_id", None),
            "width": getattr(largest, "width", None),
            "height": getattr(largest, "height", None),
            "byte_size": getattr(largest, "file_size", None),
            "variants": len(photos),
        }
    document = getattr(message, "document", None)
    if document is not None:
        return {
            "media_kind": "document",
            "provider": "telegram",
            "file_id": getattr(document, "file_id", None),
            "file_unique_id": getattr(document, "file_unique_id", None),
            "mime_type": getattr(document, "mime_type", None),
            "byte_size": getattr(document, "file_size", None),
            "file_name": getattr(document, "file_name", None),
        }
    return None


async def _emit_interaction_received(
    db: Any,
    user_id: int,
    kind: str,
    update: Any,
    command: str | None,
    flow_before: dict[str, Any] | None,
) -> None:
    message = getattr(update, "effective_message", None)
    callback_query = getattr(update, "callback_query", None)
    callback_data = getattr(callback_query, "data", None) if callback_query else None

    properties: dict[str, Any] = {"kind": kind, "surface": SURFACE_TELEGRAM}
    if command:
        properties["command"] = command
    chat = getattr(update, "effective_chat", None)
    if chat is not None:
        properties["chat_id"] = getattr(chat, "id", None)
    if message is not None:
        properties["message_id"] = getattr(message, "message_id", None)
        media = _media_identity(message)
        if media:
            properties["media"] = media
    if callback_data is not None:
        properties["callback_data"] = callback_data
        source_message = getattr(callback_query, "message", None)
        if source_message is not None:
            properties["source_message_id"] = getattr(source_message, "message_id", None)
    if flow_before is not None:
        properties["flow_before"] = _bounded_flow_identity(flow_before)

    content: dict[str, Any] = {}
    text = getattr(message, "text", None) if message is not None else None
    caption = getattr(message, "caption", None) if message is not None else None
    if text:
        content["text"] = text
    if caption:
        content["caption"] = caption
    if flow_before is not None and flow_before.get("payload"):
        content["flow_payload_before"] = flow_before["payload"]

    await emit_event(
        db,
        user_id,
        taxonomy.INTERACTION_RECEIVED,
        entity="interaction",
        source="telegram",
        surface=SURFACE_TELEGRAM,
        status="received",
        properties=properties,
        content=content or None,
        flow_id=(flow_before or {}).get("flow_id"),
    )

    if kind == "callback" and callback_data is not None:
        source_render_id = None
        label = None
        correlation = "unresolved"
        source_message_id = properties.get("source_message_id")
        try:
            from noam_coach.observability.render_registry import resolve_control

            resolved = await resolve_control(db, user_id, source_message_id, callback_data)
            if resolved is not None:
                source_render_id = resolved.render_id
                label = resolved.label
                correlation = "resolved"
        except Exception:  # noqa: BLE001 — unresolved stays explicit.
            pass
        await emit_event(
            db,
            user_id,
            taxonomy.UI_CONTROL_ACTIVATED,
            entity="ui_control",
            source="telegram",
            surface=SURFACE_TELEGRAM,
            status="activated",
            properties={
                "callback_data": callback_data,
                "source_message_id": source_message_id,
                # Resolved from the render registry when evidence exists;
                # unresolved correlation is explicit, never inferred.
                "source_render_id": source_render_id,
                "label": label,
                "correlation": correlation,
            },
        )


def observed_handler(
    kind: str,
    handler: TelegramHandler,
    *,
    command: str | None = None,
) -> TelegramHandler:
    """Wrap a python-telegram-bot handler with the O2 interaction envelope."""

    @functools.wraps(handler)
    async def wrapper(update: Any, context: Any) -> Any:
        user = getattr(update, "effective_user", None)
        allowed = _allowed_user_id()
        if user is None or (allowed is not None and int(user.id) != allowed):
            # Unauthorized/anonymous traffic is not part of the coaching
            # trace; the handler's own is_allowed gate replies to it.
            return await handler(update, context)
        user_id = int(user.id)
        db = _facade_db()

        callback_query = getattr(update, "callback_query", None)
        trace_id: str | None = None
        if kind == "callback" and callback_query is not None:
            source_message = getattr(callback_query, "message", None)
            with suppress(Exception):
                trace_id = await _trace_for_callback(
                    db, user_id, getattr(source_message, "message_id", None)
                )

        with interaction_scope(trace_id=trace_id, user_id=user_id):
            with suppress(Exception):
                await _ensure_user_row(user)
            flow_before = await _pre_routing_flow_snapshot(db, user_id)
            await _emit_interaction_received(db, user_id, kind, update, command, flow_before)
            return await handler(update, context)

    return wrapper


async def _ensure_user_row(user: Any) -> None:
    """FK parent row for the very first interaction of a new user."""
    from noam_coach.services.core import ensure_user_record

    await ensure_user_record(
        int(user.id),
        first_name=getattr(user, "first_name", None),
        username=getattr(user, "username", None),
    )


# ---------------------------------------------------------------------------
# routing.decided — caller-side observation of the returned RouteDecision
# ---------------------------------------------------------------------------

_original_route: Any = None


def install_routing_observer() -> None:
    """Wrap ``ConversationRouter.route`` so every routing decision is recorded.

    Installed explicitly at application build time (and by tests that want
    it). The pure policy keeps its exact contract; the wrapper only observes
    the returned decision.
    """
    global _original_route
    if _original_route is not None:
        return
    _original_route = conversation.ConversationRouter.route

    @functools.wraps(_original_route)
    async def observed_route(
        db: Any, user_id: int, kind: str, *, command: str = ""
    ) -> Any:
        with span_scope():
            decision = await _original_route(db, user_id, kind, command=command)
            await emit_event(
                db,
                user_id,
                taxonomy.ROUTING_DECIDED,
                entity="routing",
                source="router",
                surface=SURFACE_TELEGRAM,
                status="decided",
                outcome=decision.handler,
                properties={
                    "input_kind": kind,
                    "command": command or None,
                    "handler": decision.handler,
                    "action": decision.action,
                    "reason": decision.reason,
                    "flow": _bounded_flow_identity(decision.flow.snapshot()),
                },
                flow_id=decision.flow.flow_id,
                flow_version=decision.flow.version,
            )
            return decision

    conversation.ConversationRouter.route = staticmethod(observed_route)


def uninstall_routing_observer() -> None:
    """Restore the pure router (test isolation)."""
    global _original_route
    if _original_route is not None:
        conversation.ConversationRouter.route = staticmethod(_original_route)
        _original_route = None
