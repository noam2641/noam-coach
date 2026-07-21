"""Durable idempotency operations for daily-menu work (G2.3B-1).

Two distinct operation kinds share one record shape and one set of primitives:

``refresh_request``
    Claims the *intent to advance beyond a particular menu*, keyed on the
    menu identity that existed when the refresh was requested. Two concurrent
    refresh callbacks therefore compute the SAME key and exactly one wins, so
    only one AI generation and one menu revision ever happen.

``delivery``
    Claims the *delivery of a particular menu*, keyed on the menu identity
    itself. The requesting UI action is deliberately NOT part of that
    identity: a menu delivered after an explicit refresh and then re-requested
    through the ordinary "today's menu" button is the SAME delivery, and the
    second request must be suppressed.

Every record lives in the per-day ``daily_flags`` document and is written
through :mod:`noam_coach.services.daily_flags_cas`, whose entire persistence is
one conditional ``UPDATE`` carrying the whole flags dict. That is what makes
:func:`persist_refresh_result` genuinely atomic: the menu revision and the
operation record that names it are written by ONE mutator, so a crash can never
leave a persisted revision that no operation record points at.

Ownership is enforced by ``attempt_id`` INSIDE the CAS mutator, not merely at
completion. A worker whose lease expired while its AI request was still running
cannot persist its result, cannot change the active menu, and cannot complete or
fail a newer attempt -- the mutator sees a different ``attempt_id`` and writes
nothing.

Nothing here is wired into the production refresh/delivery handlers yet; that is
G2.3B-2 and G2.3B-3.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from config import TZ
from noam_coach.services.daily_menu_state import (
    ACTIVE_DAILY_MENU_KEY,
    DAILY_MENU_SCHEMA_VERSION,
)

# --- storage keys (bounded: exactly one record per key, never a list) --------

DAILY_MENU_SEND_OP_KEY = "daily_menu_send_op"
DAILY_MENU_LAST_COMPLETED_KEY = "daily_menu_last_completed_op"
DAILY_MENU_REFRESH_OP_KEY = "daily_menu_refresh_op"

OPERATION_SCHEMA_VERSION = 1

KIND_DELIVERY = "delivery"
KIND_REFRESH_REQUEST = "refresh_request"

_KIND_TO_KEY = {
    KIND_DELIVERY: DAILY_MENU_SEND_OP_KEY,
    KIND_REFRESH_REQUEST: DAILY_MENU_REFRESH_OP_KEY,
}

# The one delivery type this module currently arbitrates. It is a constant, not
# a UI action: every path that sends the standalone pinnable menu shares it.
DELIVERY_TYPE_STANDALONE = "standalone_daily_menu"

# Lease: the renewal BASIS, not a generation budget. Menu generation calls the
# AI client, which carries no explicit timeout in this codebase, so a long but
# healthy generation must hold its claim by renewing rather than by fitting
# inside a fixed window.
MENU_SEND_LEASE_SECONDS = 120
MENU_LEASE_RENEW_EVERY_SECONDS = 40

# Identity components are joined with this separator only AFTER validating that
# no component contains it. All decisions compare structured fields; the joined
# string exists for logging and equality convenience.
_KEY_SEPARATOR = "|"

# Normalised placeholder for "this identity component is absent". Never "" and
# never None, so an absent plan or an absent current menu still yields a stable,
# comparable identity.
NONE_SENTINEL = "none"

# --- claim outcomes ---------------------------------------------------------

ACQUIRED = "acquired"
ALREADY_COMPLETED = "already_completed"
IN_FLIGHT = "in_flight"
OWNERSHIP_LOST = "ownership_lost"
STALE_SOURCE = "stale_source"
CONFLICT = "conflict"


@dataclass(frozen=True)
class OperationOutcome:
    """Structured result of a claim/renew/complete/fail/persist attempt."""

    status: str
    record: dict[str, Any] | None = None
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == ACQUIRED


def new_attempt_id() -> str:
    return secrets.token_hex(8)


def _now(now: datetime | None = None) -> datetime:
    return now or datetime.now(TZ)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _validate_component(value: Any, *, field: str) -> str:
    """Normalise one identity component and reject an ambiguous separator.

    A component containing the separator would make the canonical string form
    ambiguous, so it is rejected outright rather than silently producing a key
    that two different identities could share.
    """
    if value is None or value == "":
        return NONE_SENTINEL
    text = str(value)
    if _KEY_SEPARATOR in text:
        raise ValueError(
            f"daily-menu identity component {field!r} may not contain "
            f"{_KEY_SEPARATOR!r}: {text!r}"
        )
    return text


def build_delivery_identity(
    *,
    coaching_day_key: str,
    menu_id: str | None,
    plan_id: Any = None,
    delivery_type: str = DELIVERY_TYPE_STANDALONE,
) -> dict[str, str]:
    """Identity of ONE standalone-menu delivery.

    ``requested_by`` (the UI action that asked for it) is deliberately absent:
    the same menu delivered through a different button is the same delivery.
    """
    return {
        "coaching_day_key": _validate_component(coaching_day_key, field="coaching_day_key"),
        "menu_id": _validate_component(menu_id, field="menu_id"),
        "plan_id": _validate_component(plan_id, field="plan_id"),
        "delivery_type": _validate_component(delivery_type, field="delivery_type"),
    }


def build_refresh_identity(
    *,
    coaching_day_key: str,
    current_menu_id: str | None,
    plan_id: Any = None,
) -> dict[str, str]:
    """Identity of ONE refresh request.

    Keyed on the menu that exists NOW, so two concurrent refreshes of the same
    menu collide on one key and only one of them regenerates.
    """
    return {
        "coaching_day_key": _validate_component(coaching_day_key, field="coaching_day_key"),
        "current_menu_id": _validate_component(current_menu_id, field="current_menu_id"),
        "plan_id": _validate_component(plan_id, field="plan_id"),
        "request_type": KIND_REFRESH_REQUEST,
    }


def canonical_key(identity: dict[str, str]) -> str:
    """Deterministic string form of a structured identity.

    Sorted by field name so the same identity always serialises identically
    regardless of dict insertion order.
    """
    return _KEY_SEPARATOR.join(
        _validate_component(identity[field], field=field) for field in sorted(identity)
    )


def identities_match(left: dict[str, Any] | None, right: dict[str, Any] | None) -> bool:
    """Structured comparison -- never a string-prefix or substring test."""
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    return {str(k): str(v) for k, v in left.items()} == {
        str(k): str(v) for k, v in right.items()
    }


def _record_is_supported(record: Any) -> bool:
    """True only for a record this module knows how to reason about.

    An unknown/missing schema version is treated as unsupported and therefore
    NEVER suppresses a new claim (fail-open): on a deployment boundary a user
    must not lose their menu because of a record written by older code.
    """
    if not isinstance(record, dict):
        return False
    if record.get("schema_version") != OPERATION_SCHEMA_VERSION:
        return False
    return isinstance(record.get("identity"), dict) and bool(record.get("attempt_id"))


def _lease_is_live(record: dict[str, Any], now: datetime) -> bool:
    raw = record.get("lease_expires_at")
    if not raw:
        return False
    try:
        return now < datetime.fromisoformat(str(raw))
    except ValueError:
        # An unparseable lease is treated as expired: recoverable rather than
        # permanently wedged.
        return False


def _new_record(
    *,
    kind: str,
    identity: dict[str, str],
    source_identity: dict[str, Any] | None,
    attempt_id: str,
    attempt: int,
    now: datetime,
    target_menu_id: str | None,
    target_revision: int | None,
    requested_by: str | None,
) -> dict[str, Any]:
    stamp = _iso(now)
    return {
        "schema_version": OPERATION_SCHEMA_VERSION,
        "operation_kind": kind,
        "identity": dict(identity),
        "semantic_key": canonical_key(identity),
        "source_identity": dict(source_identity) if source_identity else None,
        "attempt_id": attempt_id,
        "attempt": attempt,
        "status": IN_FLIGHT,
        "lease_expires_at": _iso(now + timedelta(seconds=MENU_SEND_LEASE_SECONDS)),
        "heartbeat_at": stamp,
        "target_menu_id": target_menu_id,
        "target_revision": target_revision,
        "produced_menu_id": None,
        "produced_plan_id": None,
        "message_id": None,
        "chat_id": None,
        "requested_by": requested_by,
        "failure": None,
        "created_at": stamp,
        "updated_at": stamp,
    }


async def _patch(db: Any, user_id: int, day: str, mutator: Any, key: str) -> dict[str, Any]:
    from noam_coach.services.daily_flags_cas import patch_daily_flags

    return await patch_daily_flags(
        db, user_id, day, mutator, owner="daily_menu_ops", touched_keys=[key],
    )


async def claim_operation(
    db: Any,
    user_id: int,
    day: str,
    *,
    kind: str,
    identity: dict[str, str],
    attempt_id: str,
    now: datetime | None = None,
    source_identity: dict[str, Any] | None = None,
    target_menu_id: str | None = None,
    target_revision: int | None = None,
    requested_by: str | None = None,
) -> OperationOutcome:
    """Claim ``identity`` for ``kind``, or report why the claim was refused.

    The decision is made INSIDE the CAS mutator against freshly re-read state,
    so two concurrent claimants cannot both win: the loser's mutator is
    re-applied after the winner's write and then observes a live lease.

    A takeover of an expired lease deliberately KEEPS the previous attempt's
    ``target_menu_id``/``target_revision``, so recovery never invents a new
    menu identity.
    """
    if kind not in _KIND_TO_KEY:
        raise ValueError(f"unsupported operation kind: {kind!r}")
    key = _KIND_TO_KEY[kind]
    moment = _now(now)
    outcome: dict[str, Any] = {"status": CONFLICT, "record": None, "reason": None}

    def _mutate(current: dict[str, Any]) -> dict[str, Any]:
        existing = current.get(key)
        supported = _record_is_supported(existing)
        same = supported and identities_match(existing.get("identity"), identity)

        if same and existing.get("status") == "completed":
            outcome.update(status=ALREADY_COMPLETED, record=existing,
                           reason="already_completed")
            return current
        if same and existing.get("status") == IN_FLIGHT and _lease_is_live(existing, moment):
            outcome.update(status=IN_FLIGHT, record=existing,
                           reason="in_flight_lease_active")
            return current

        # Archive a completed delivery under a different identity so a late tap
        # of the PREVIOUS menu is still recognised and suppressed.
        if (
            kind == KIND_DELIVERY
            and supported
            and not same
            and existing.get("status") == "completed"
        ):
            current[DAILY_MENU_LAST_COMPLETED_KEY] = existing

        taking_over = same and supported and existing.get("status") == IN_FLIGHT
        record = _new_record(
            kind=kind,
            identity=identity,
            source_identity=source_identity,
            attempt_id=attempt_id,
            attempt=int(existing.get("attempt") or 0) + 1 if taking_over else 1,
            now=moment,
            # Reservation is sticky across takeover.
            target_menu_id=(
                existing.get("target_menu_id") if taking_over else target_menu_id
            ),
            target_revision=(
                existing.get("target_revision") if taking_over else target_revision
            ),
            requested_by=requested_by,
        )
        if taking_over:
            record["produced_menu_id"] = existing.get("produced_menu_id")
            record["produced_plan_id"] = existing.get("produced_plan_id")
            record["previous_attempt_id"] = existing.get("attempt_id")
        current[key] = record
        outcome.update(status=ACQUIRED, record=record, reason=None)
        return current

    try:
        await _patch(db, user_id, day, _mutate, key)
    except Exception as exc:  # noqa: BLE001 -- CAS exhaustion must not send
        return OperationOutcome(status=CONFLICT, reason=f"cas_conflict:{exc.__class__.__name__}")
    return OperationOutcome(**outcome)


async def renew_operation(
    db: Any,
    user_id: int,
    day: str,
    *,
    kind: str,
    identity: dict[str, str],
    attempt_id: str,
    now: datetime | None = None,
) -> OperationOutcome:
    """Extend the lease -- only for the current owner of an in-flight record."""
    key = _KIND_TO_KEY[kind]
    moment = _now(now)
    outcome: dict[str, Any] = {"status": OWNERSHIP_LOST, "record": None,
                               "reason": "ownership_lost"}

    def _mutate(current: dict[str, Any]) -> dict[str, Any]:
        existing = current.get(key)
        if not _record_is_supported(existing):
            return current
        if (
            existing.get("attempt_id") != attempt_id
            or existing.get("operation_kind") != kind
            or existing.get("status") != IN_FLIGHT
            or not identities_match(existing.get("identity"), identity)
        ):
            return current
        renewed = dict(existing)
        renewed["lease_expires_at"] = _iso(
            moment + timedelta(seconds=MENU_SEND_LEASE_SECONDS)
        )
        renewed["heartbeat_at"] = _iso(moment)
        renewed["updated_at"] = _iso(moment)
        current[key] = renewed
        outcome.update(status=ACQUIRED, record=renewed, reason=None)
        return current

    try:
        await _patch(db, user_id, day, _mutate, key)
    except Exception as exc:  # noqa: BLE001
        return OperationOutcome(status=CONFLICT, reason=f"cas_conflict:{exc.__class__.__name__}")
    return OperationOutcome(**outcome)


async def complete_operation(
    db: Any,
    user_id: int,
    day: str,
    *,
    kind: str,
    identity: dict[str, str],
    attempt_id: str,
    now: datetime | None = None,
    message_id: int | None = None,
    chat_id: int | None = None,
) -> OperationOutcome:
    """Mark the operation completed -- owner only. Completion is terminal."""
    key = _KIND_TO_KEY[kind]
    moment = _now(now)
    outcome: dict[str, Any] = {"status": OWNERSHIP_LOST, "record": None,
                               "reason": "ownership_lost"}

    def _mutate(current: dict[str, Any]) -> dict[str, Any]:
        existing = current.get(key)
        if not _record_is_supported(existing):
            return current
        if existing.get("attempt_id") != attempt_id or not identities_match(
            existing.get("identity"), identity
        ):
            return current
        done = dict(existing)
        done["status"] = "completed"
        done["completed_at"] = _iso(moment)
        done["updated_at"] = _iso(moment)
        if message_id is not None:
            done["message_id"] = int(message_id)
        if chat_id is not None:
            done["chat_id"] = int(chat_id)
        current[key] = done
        outcome.update(status=ACQUIRED, record=done, reason=None)
        return current

    try:
        await _patch(db, user_id, day, _mutate, key)
    except Exception as exc:  # noqa: BLE001
        return OperationOutcome(status=CONFLICT, reason=f"cas_conflict:{exc.__class__.__name__}")
    return OperationOutcome(**outcome)


async def fail_operation(
    db: Any,
    user_id: int,
    day: str,
    *,
    kind: str,
    identity: dict[str, str],
    attempt_id: str,
    category: str,
    error_code: str | None = None,
    now: datetime | None = None,
) -> OperationOutcome:
    """Record a failure -- owner only -- so a retry may re-claim immediately."""
    key = _KIND_TO_KEY[kind]
    moment = _now(now)
    outcome: dict[str, Any] = {"status": OWNERSHIP_LOST, "record": None,
                               "reason": "ownership_lost"}

    def _mutate(current: dict[str, Any]) -> dict[str, Any]:
        existing = current.get(key)
        if not _record_is_supported(existing):
            return current
        if existing.get("attempt_id") != attempt_id or not identities_match(
            existing.get("identity"), identity
        ):
            return current
        failed = dict(existing)
        failed["status"] = "failed"
        failed["failure"] = {"category": category, "error_code": error_code}
        failed["updated_at"] = _iso(moment)
        current[key] = failed
        outcome.update(status=ACQUIRED, record=failed, reason=None)
        return current

    try:
        await _patch(db, user_id, day, _mutate, key)
    except Exception as exc:  # noqa: BLE001
        return OperationOutcome(status=CONFLICT, reason=f"cas_conflict:{exc.__class__.__name__}")
    return OperationOutcome(**outcome)


async def persist_refresh_result(
    db: Any,
    user_id: int,
    day: str,
    *,
    identity: dict[str, str],
    attempt_id: str,
    text: str,
    meals: list[Any] | None = None,
    strategy: str | None = None,
    produced_plan_id: Any = None,
    context_version: str | None = None,
    now: datetime | None = None,
) -> OperationOutcome:
    """Persist the generated menu AND its produced identity in ONE mutation.

    This is the primitive that closes the "revision persisted but the operation
    record never updated" crash window: ``ACTIVE_DAILY_MENU_KEY`` and the
    refresh record's ``produced_menu_id`` are set by the same mutator, so the
    single CAS ``UPDATE`` writes both or neither.

    Every precondition is re-checked INSIDE the mutator against freshly re-read
    state -- ownership, in-flight status, the reserved target identity, and the
    source menu the claim was based on. A stale owner returning from a long AI
    call therefore writes nothing at all, and can never overwrite a newer
    active menu.
    """
    key = DAILY_MENU_REFRESH_OP_KEY
    moment = _now(now)
    outcome: dict[str, Any] = {"status": OWNERSHIP_LOST, "record": None,
                               "reason": "ownership_lost"}

    def _mutate(current: dict[str, Any]) -> dict[str, Any]:
        existing = current.get(key)
        if not _record_is_supported(existing):
            return current
        # (1)(2) ownership + identity
        if existing.get("attempt_id") != attempt_id or not identities_match(
            existing.get("identity"), identity
        ):
            return current
        # (3) state still permits a result
        if existing.get("status") != IN_FLIGHT:
            outcome.update(status=CONFLICT, record=existing, reason="not_in_flight")
            return current

        target_menu_id = existing.get("target_menu_id")
        target_revision = existing.get("target_revision")
        # (5) the reservation must exist -- otherwise identity is not deterministic
        if not target_menu_id or target_revision is None:
            outcome.update(status=CONFLICT, record=existing, reason="missing_reservation")
            return current

        active = current.get(ACTIVE_DAILY_MENU_KEY)
        active_menu_id = active.get("menu_id") if isinstance(active, dict) else None
        active_revision = int(active.get("revision") or 0) if isinstance(active, dict) else 0

        # (6) idempotent replay: our reserved result is already the active menu
        if active_menu_id == target_menu_id:
            done = dict(existing)
            done["produced_menu_id"] = target_menu_id
            done["updated_at"] = _iso(moment)
            current[key] = done
            outcome.update(status=ACQUIRED, record=done, reason="already_persisted")
            return current

        # (4) the source menu the claim was based on must still be the active one
        source = existing.get("source_identity") or {}
        expected_source_menu_id = source.get("current_menu_id")
        if expected_source_menu_id is not None:
            observed = active_menu_id if active_menu_id is not None else NONE_SENTINEL
            if str(expected_source_menu_id) != str(observed):
                outcome.update(status=STALE_SOURCE, record=existing, reason="stale_source")
                return current

        # (6) the active revision must not have advanced past our reservation
        if active_revision >= int(target_revision):
            outcome.update(status=CONFLICT, record=existing, reason="revision_advanced")
            return current

        # (7) persist the menu under the RESERVED identity
        stamp = _iso(moment)
        menu_state: dict[str, Any] = {
            "text": text,
            "strategy": strategy,
            "revision": int(target_revision),
            "source": "refresh_operation",
            "updated_at": stamp,
        }
        if meals is not None:
            menu_state["schema_version"] = DAILY_MENU_SCHEMA_VERSION
            menu_state["menu_id"] = target_menu_id
            menu_state["generated_at"] = stamp
            menu_state["context_version"] = context_version
            menu_state["meals"] = [
                item.to_dict() if hasattr(item, "to_dict") else dict(item)
                for item in meals
            ]
        else:
            menu_state["menu_id"] = target_menu_id
        current[ACTIVE_DAILY_MENU_KEY] = menu_state

        # (8)(9) record produced identity in the SAME mutation
        done = dict(existing)
        done["produced_menu_id"] = target_menu_id
        done["produced_plan_id"] = (
            None if produced_plan_id is None else str(produced_plan_id)
        )
        done["updated_at"] = stamp
        current[key] = done
        outcome.update(status=ACQUIRED, record=done, reason=None)
        return current

    try:
        # (10) one UPDATE carries both keys
        await _patch(db, user_id, day, _mutate, key)
    except Exception as exc:  # noqa: BLE001
        return OperationOutcome(status=CONFLICT, reason=f"cas_conflict:{exc.__class__.__name__}")
    return OperationOutcome(**outcome)


async def read_operation(
    db: Any,
    user_id: int,
    day: str,
    *,
    kind: str,
) -> dict[str, Any] | None:
    """Read one operation record (read-only; no CAS, no mutation)."""
    from noam_coach.services.daily_flags_cas import get_daily_flags_with_revision

    flags, _revision = await get_daily_flags_with_revision(db, user_id, day)
    record = flags.get(_KIND_TO_KEY[kind])
    return record if _record_is_supported(record) else None
