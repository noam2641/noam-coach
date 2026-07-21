"""The one boundary through which a standalone daily menu is delivered (G2.3B-2).

Every production path that sends the pinnable standalone daily-menu message goes
through :func:`deliver_standalone_menu`. Claim logic lives here once; no handler
repeats it.

Duplicate suppression is *semantic*: the delivery identity is
``(coaching_day_key, menu_id, plan_id, delivery_type)``. The UI action that asked
for the menu is metadata (``requested_by``) and deliberately plays no part in it,
so a menu displayed through the ordinary button after having been delivered by
some other path is recognised as the SAME delivery and suppressed. Elapsed time
is never consulted -- there is no debounce window and no process-local cache.

Ordering is deliberate and load-bearing:

    claim -> send -> complete

The claim is durable and happens BEFORE the send, so two concurrent requests
cannot both reach Telegram. Completion happens only AFTER Telegram returns a
message, so a crash mid-send never records a delivery that did not happen. The
cost of that ordering is stated honestly in the module contract: a crash between
a successful send and a durable completion may deliver one extra message on a
later takeover, and repeated crash cycles may do so repeatedly. Delivery is
at-least-once; it is not exactly-once, and nothing here claims otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable

from config import LOGGER
from noam_coach.observability import taxonomy
from noam_coach.services import daily_menu_operations as ops

# Result of asking the boundary to deliver.
SENT = "sent"
SUPPRESSED_ALREADY_COMPLETED = "suppressed_already_completed"
SUPPRESSED_IN_FLIGHT = "suppressed_in_flight"
SUPPRESSED_CONFLICT = "suppressed_conflict"
FAILED = "failed"


@dataclass(frozen=True)
class DeliveryResult:
    status: str
    identity: dict[str, str] | None = None
    attempt_id: str | None = None
    message_id: int | None = None
    reason: str | None = None

    @property
    def was_sent(self) -> bool:
        return self.status == SENT

    @property
    def suppressed(self) -> bool:
        return self.status.startswith("suppressed")


async def resolve_delivery_identity(
    db: Any,
    user_id: int,
    *,
    now: datetime | None = None,
) -> tuple[str, dict[str, str]]:
    """Authoritative ``(coaching_day_key, identity)`` for today's menu.

    ``menu_id`` comes from the persisted active menu; a legacy text-only record
    without one falls back to its ``revision``, and a day with no menu at all
    yields the ``none`` sentinel rather than an empty string, so the identity is
    always well formed and comparable.
    """
    from noam_coach.services.daily_menu_state import (
        _nutrition_day,
        get_active_daily_menu,
    )

    day = await _nutrition_day(db, user_id, now)

    menu_id: Any = None
    try:
        active = await get_active_daily_menu(db, user_id, now=now)
    except Exception:  # noqa: BLE001 -- identity resolution must not break delivery
        active = None
    if isinstance(active, dict):
        menu_id = active.get("menu_id")
        if not menu_id and active.get("revision") is not None:
            menu_id = f"rev-{active['revision']}"

    plan_id: Any = None
    try:
        import planning

        plan = await planning.get_active_plan(db, user_id, "nutrition")
        if plan:
            plan_id = plan.get("id")
    except Exception:  # noqa: BLE001
        plan_id = None

    identity = ops.build_delivery_identity(
        coaching_day_key=day, menu_id=menu_id, plan_id=plan_id,
    )
    return day, identity


async def _emit(
    db: Any,
    user_id: int,
    event: str,
    *,
    identity: dict[str, str],
    attempt_id: str | None,
    status: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Structured observability. Never carries menu text or user content."""
    try:
        from noam_coach.observability.emit import emit_event

        await emit_event(
            db,
            user_id,
            event,
            entity="daily_menu",
            entity_id=identity.get("menu_id"),
            source="bot",
            status=status,
            properties={
                "operation_kind": ops.KIND_DELIVERY,
                "semantic_key": ops.canonical_key(identity),
                "coaching_day_key": identity.get("coaching_day_key"),
                "menu_id": identity.get("menu_id"),
                "plan_id": identity.get("plan_id"),
                "delivery_type": identity.get("delivery_type"),
                "attempt_id": attempt_id,
                **(extra or {}),
            },
        )
    except Exception:  # noqa: BLE001 -- observability must never break delivery
        LOGGER.debug("daily-menu delivery event %s failed", event, exc_info=True)


async def deliver_standalone_menu(
    db: Any,
    user_id: int,
    *,
    send: Callable[[], Awaitable[Any]],
    requested_by: str,
    source: str,
    now: datetime | None = None,
    identity: dict[str, str] | None = None,
    day: str | None = None,
) -> DeliveryResult:
    """Send the standalone menu at most once per semantic delivery identity.

    ``send`` is an async callable performing the actual Telegram send and
    returning the sent message. It is invoked **only** by the attempt that wins
    the durable claim.

    The caller keeps full control of the message it sends -- text, HTML parse
    mode, keyboard and pin-friendliness are unchanged by this boundary.
    """
    if identity is None or day is None:
        day, identity = await resolve_delivery_identity(db, user_id, now=now)

    attempt_id = ops.new_attempt_id()
    claim = await ops.claim_operation(
        db, user_id, day,
        kind=ops.KIND_DELIVERY,
        identity=identity,
        attempt_id=attempt_id,
        now=now,
        requested_by=requested_by,
    )

    if claim.status == ops.ALREADY_COMPLETED:
        await _emit(
            db, user_id, taxonomy.DAILY_MENU_DUPLICATE_SUPPRESSED,
            identity=identity, attempt_id=attempt_id, status="suppressed",
            extra={"reason": "already_completed", "requested_by": requested_by},
        )
        return DeliveryResult(
            status=SUPPRESSED_ALREADY_COMPLETED, identity=identity,
            message_id=(claim.record or {}).get("message_id"),
            reason="already_completed",
        )

    if claim.status == ops.IN_FLIGHT:
        await _emit(
            db, user_id, taxonomy.DAILY_MENU_DUPLICATE_SUPPRESSED,
            identity=identity, attempt_id=attempt_id, status="suppressed",
            extra={"reason": "in_flight_lease_active", "requested_by": requested_by},
        )
        return DeliveryResult(
            status=SUPPRESSED_IN_FLIGHT, identity=identity,
            reason="in_flight_lease_active",
        )

    if claim.status != ops.ACQUIRED:
        # ownership_lost / stale_source / conflict -- never fall through to an
        # unguarded send.
        await _emit(
            db, user_id, taxonomy.DAILY_MENU_DUPLICATE_SUPPRESSED,
            identity=identity, attempt_id=attempt_id, status="refused",
            extra={"reason": claim.reason or claim.status,
                   "requested_by": requested_by},
        )
        return DeliveryResult(
            status=SUPPRESSED_CONFLICT, identity=identity,
            reason=claim.reason or claim.status,
        )

    await _emit(
        db, user_id, taxonomy.DAILY_MENU_SEND_CLAIMED,
        identity=identity, attempt_id=attempt_id, status="claimed",
        extra={"requested_by": requested_by},
    )

    try:
        sent = await send()
    except Exception as exc:  # noqa: BLE001 -- record, then let the caller decide
        await ops.fail_operation(
            db, user_id, day,
            kind=ops.KIND_DELIVERY, identity=identity, attempt_id=attempt_id,
            category=_failure_category(exc), error_code=exc.__class__.__name__,
            now=now,
        )
        await _emit(
            db, user_id, taxonomy.DAILY_MENU_SEND_FAILED,
            identity=identity, attempt_id=attempt_id, status="failed",
            extra={"failure_category": _failure_category(exc),
                   "error_code": exc.__class__.__name__},
        )
        raise

    message_id = getattr(sent, "message_id", None)
    chat_id = getattr(getattr(sent, "chat", None), "id", None)

    # Completion happens only now, after Telegram actually returned a message,
    # and only for the attempt that still owns the claim.
    #
    # Completion status, the archived last-completed record and the message
    # identity are ONE CAS mutation: a crash can never leave a delivery marked
    # completed while DAILY_MENU_MESSAGE_KEY is missing, because the two facts
    # are written together or not at all.
    completion = await ops.complete_delivery_atomically(
        db, user_id, day,
        identity=identity, attempt_id=attempt_id,
        message_id=message_id, chat_id=chat_id, source=source, now=now,
    )

    if completion.status != ops.ACQUIRED:
        # The message exists but the durable record does not name it. Emit the
        # high-severity reconciliation signal carrying the message id, and do
        # NOT touch the newer owner's completion or message metadata.
        await _emit(
            db, user_id, taxonomy.DAILY_MENU_COMPLETION_PERSIST_FAILED,
            identity=identity, attempt_id=attempt_id, status="inconsistent",
            extra={"reason": completion.reason or completion.status,
                   "message_id": message_id, "chat_id": chat_id},
        )
        return DeliveryResult(
            status=SENT, identity=identity, attempt_id=attempt_id,
            message_id=message_id, reason=completion.reason or completion.status,
        )

    await _emit(
        db, user_id, taxonomy.DAILY_MENU_SEND_COMPLETED,
        identity=identity, attempt_id=attempt_id, status="completed",
        extra={"message_id": message_id, "requested_by": requested_by},
    )
    return DeliveryResult(
        status=SENT, identity=identity, attempt_id=attempt_id, message_id=message_id,
    )


def _failure_category(exc: BaseException) -> str:
    name = exc.__class__.__name__.lower()
    if "badrequest" in name:
        return "telegram_bad_request"
    if "retryafter" in name or "rate" in name:
        return "telegram_rate_limit"
    if "network" in name or "timedout" in name or "timeout" in name:
        return "telegram_network"
    return "unknown"


SUPPRESSION_TOAST = {
    SUPPRESSED_ALREADY_COMPLETED: "התפריט כבר נשלח ✅",
    SUPPRESSED_IN_FLIGHT: "התפריט כבר בהכנה ✅",
    SUPPRESSED_CONFLICT: "נסה שוב עוד רגע",
}


def suppression_toast(result: DeliveryResult) -> str:
    """Short callback-answer text for a suppressed delivery.

    A suppressed duplicate is answered with a toast, never with a second
    ordinary chat message -- the whole point is not to add noise.
    """
    return SUPPRESSION_TOAST.get(result.status, "התפריט כבר נשלח ✅")
