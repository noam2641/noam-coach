"""Durable orchestration of an explicit daily-menu refresh (G2.3B-3).

One boundary, :func:`refresh_daily_menu`, owns the whole sequence:

    claim -> generate (heartbeat) -> persist atomically -> deliver

The claim comes FIRST, before any AI call. That ordering is the whole point:
two concurrent refresh taps compute the same refresh identity (it is keyed on
the menu that exists *now*), exactly one wins, and the loser never reaches the
pipeline. Without it, both would generate, each would mint its own revision,
and each would then legitimately deliver -- two AI calls and two menus.

The target menu identity is reserved at claim time and is sticky across
takeover, so a recovering attempt regenerates under the SAME identity rather
than inventing a new one. The generated result is written by
``persist_refresh_result``: one CAS mutation carrying both the menu revision
and the operation record that names it, so a crash can never leave a persisted
revision no operation points at.

Ownership is re-checked inside every mutator. A worker whose lease expired
mid-generation writes nothing at all when its AI call finally returns -- no
revision, no produced identity, no delivery.

Delivery is never performed here directly: the persisted menu is handed to the
B-2 durable delivery boundary, so a refresh and an ordinary display of the same
menu produce one standalone delivery in total.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable

from config import LOGGER
from noam_coach.observability import taxonomy
from noam_coach.services import daily_menu_operations as ops

# Outcomes of a refresh request.
REFRESHED = "refreshed"
RESUMED = "resumed"
SUPPRESSED_IN_FLIGHT = "suppressed_in_flight"
SUPPRESSED_ALREADY_COMPLETED = "suppressed_already_completed"
REFUSED_CONFLICT = "refused_conflict"
REFUSED_STALE_SOURCE = "refused_stale_source"
GENERATION_FAILED = "generation_failed"

# Failure taxonomy (mirrors the gate's required categories).
CLAIM_CONFLICT = "claim_conflict"
IN_FLIGHT = "in_flight"
OWNERSHIP_LOST = "ownership_lost"
STALE_SOURCE = "stale_source"
LEASE_RENEWAL_FAILED = "lease_renewal_failed"
GENERATION_ERROR = "generation_failed"
RESULT_PERSISTENCE_FAILED = "result_persistence_failed"
DELIVERY_FAILED = "delivery_failed"
COMPLETION_PERSISTENCE_FAILED = "completion_persistence_failed"
MALFORMED_OPERATION_STATE = "malformed_operation_state"


@dataclass(frozen=True)
class RefreshResult:
    status: str
    menu_id: str | None = None
    revision: int | None = None
    attempt_id: str | None = None
    generated: bool = False
    delivered: bool = False
    reason: str | None = None

    @property
    def suppressed(self) -> bool:
        return self.status.startswith("suppressed") or self.status.startswith("refused")


class _Fence:
    """Local mirror of ownership loss.

    Purely an optimisation so a fenced worker stops early; the CAS ownership
    check inside each mutator remains authoritative, because ownership can be
    lost between the last heartbeat and the write.
    """

    def __init__(self) -> None:
        self.lost = False


async def _heartbeat(
    db: Any,
    user_id: int,
    day: str,
    *,
    identity: dict[str, str],
    attempt_id: str,
    fence: _Fence,
    clock: Callable[[], datetime],
    interval: float,
) -> None:
    """Renew the lease while generation runs. Only the owner can renew."""
    try:
        while True:
            await asyncio.sleep(interval)
            outcome = await ops.renew_operation(
                db, user_id, day,
                kind=ops.KIND_REFRESH_REQUEST, identity=identity,
                attempt_id=attempt_id, now=clock(),
            )
            if outcome.status != ops.ACQUIRED:
                fence.lost = True
                return
    except asyncio.CancelledError:  # normal shutdown when generation finishes
        raise
    except Exception:  # noqa: BLE001 -- a heartbeat fault must not break refresh
        LOGGER.debug("daily-menu lease heartbeat failed", exc_info=True)
        fence.lost = True


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
    try:
        from noam_coach.observability.emit import emit_event

        await emit_event(
            db, user_id, event,
            entity="daily_menu", entity_id=identity.get("current_menu_id"),
            source="bot", status=status,
            properties={
                "operation_kind": ops.KIND_REFRESH_REQUEST,
                "semantic_key": ops.canonical_key(identity),
                "coaching_day_key": identity.get("coaching_day_key"),
                "current_menu_id": identity.get("current_menu_id"),
                "plan_id": identity.get("plan_id"),
                "attempt_id": attempt_id,
                **(extra or {}),
            },
        )
    except Exception:  # noqa: BLE001
        LOGGER.debug("daily-menu refresh event %s failed", event, exc_info=True)


async def resolve_refresh_identity(
    db: Any,
    user_id: int,
    *,
    now: datetime | None = None,
) -> tuple[str, dict[str, str], str | None, int]:
    """``(day, refresh_identity, current_menu_id, current_revision)``.

    Keyed on the menu that exists NOW, so concurrent refreshes of the same
    menu collide on one identity.
    """
    from noam_coach.services.daily_menu_state import (
        _nutrition_day,
        get_active_daily_menu,
    )

    day = await _nutrition_day(db, user_id, now)
    current_menu_id: str | None = None
    current_revision = 0
    try:
        active = await get_active_daily_menu(db, user_id, now=now)
    except Exception:  # noqa: BLE001
        active = None
    if isinstance(active, dict):
        current_menu_id = active.get("menu_id")
        current_revision = int(active.get("revision") or 0)

    plan_id: Any = None
    try:
        import planning

        plan = await planning.get_active_plan(db, user_id, "nutrition")
        if plan:
            plan_id = plan.get("id")
    except Exception:  # noqa: BLE001
        plan_id = None

    identity = ops.build_refresh_identity(
        coaching_day_key=day, current_menu_id=current_menu_id, plan_id=plan_id,
    )
    return day, identity, current_menu_id, current_revision


async def refresh_daily_menu(
    db: Any,
    user_id: int,
    *,
    generate: Callable[[], Awaitable[dict[str, Any]]],
    deliver: Callable[[str, str], Awaitable[Any]] | None = None,
    requested_by: str,
    now: datetime | None = None,
    clock: Callable[[], datetime] | None = None,
    renew_every: float | None = None,
) -> RefreshResult:
    """Refresh today's menu at most once per refresh identity.

    ``generate`` must return ``{"text", "meals", "strategy"}`` and must NOT
    persist anything -- persistence is this function's atomic responsibility.

    ``deliver(menu_id, text)`` hands the persisted menu to the B-2 delivery
    boundary. It is optional so the orchestration can be exercised on its own.
    """
    moment = now or datetime.now(ops.TZ)
    tick = clock or (lambda: moment)
    day, identity, current_menu_id, current_revision = await resolve_refresh_identity(
        db, user_id, now=moment,
    )

    # RECOVERY FIRST. The refresh identity is keyed on the menu that exists
    # now, so a successful refresh ROTATES it: once the new revision is
    # active, a fresh caller computes a different key. An in-flight record
    # left behind by a crashed attempt must therefore be adopted by its own
    # stored identity rather than by the freshly computed one -- otherwise
    # recovery would silently start a second refresh instead of resuming the
    # first, regenerating and double-bumping the revision.
    stored = await ops.read_operation(db, user_id, day, kind=ops.KIND_REFRESH_REQUEST)
    if (
        isinstance(stored, dict)
        and stored.get("status") == ops.IN_FLIGHT
        and stored.get("produced_menu_id")
    ):
        resumed = await _resume_produced(
            db, user_id, day, stored, moment, deliver, requested_by,
        )
        if resumed is not None:
            return resumed

    target_revision = current_revision + 1
    target_menu_id = f"menu-{user_id}-{day}-{target_revision}"
    attempt_id = ops.new_attempt_id()

    claim = await ops.claim_operation(
        db, user_id, day,
        kind=ops.KIND_REFRESH_REQUEST,
        identity=identity,
        attempt_id=attempt_id,
        now=moment,
        source_identity={
            "current_menu_id": current_menu_id or ops.NONE_SENTINEL,
            "current_revision": current_revision,
        },
        target_menu_id=target_menu_id,
        target_revision=target_revision,
        requested_by=requested_by,
    )

    if claim.status == ops.IN_FLIGHT:
        await _emit(db, user_id, taxonomy.DAILY_MENU_DUPLICATE_SUPPRESSED,
                    identity=identity, attempt_id=attempt_id, status="suppressed",
                    extra={"reason": IN_FLIGHT, "requested_by": requested_by})
        return RefreshResult(status=SUPPRESSED_IN_FLIGHT, reason=IN_FLIGHT)

    if claim.status == ops.ALREADY_COMPLETED:
        # A completed refresh: re-display its produced menu, never regenerate.
        produced = (claim.record or {}).get("produced_menu_id")
        await _emit(db, user_id, taxonomy.DAILY_MENU_DUPLICATE_SUPPRESSED,
                    identity=identity, attempt_id=attempt_id, status="suppressed",
                    extra={"reason": "already_completed", "produced_menu_id": produced})
        if produced and deliver is not None:
            text = await _load_persisted_text(db, user_id, moment)
            if text is not None:
                await deliver(produced, text)
                return RefreshResult(status=RESUMED, menu_id=produced,
                                     delivered=True, reason="already_completed")
        return RefreshResult(status=SUPPRESSED_ALREADY_COMPLETED,
                             menu_id=produced, reason="already_completed")

    if claim.status != ops.ACQUIRED:
        await _emit(db, user_id, taxonomy.DAILY_MENU_DUPLICATE_SUPPRESSED,
                    identity=identity, attempt_id=attempt_id, status="refused",
                    extra={"reason": claim.reason or claim.status})
        return RefreshResult(status=REFUSED_CONFLICT,
                             reason=claim.reason or CLAIM_CONFLICT)

    record = claim.record or {}
    # Reservation is authoritative -- a takeover inherits the ORIGINAL target.
    target_menu_id = record.get("target_menu_id") or target_menu_id
    target_revision = int(record.get("target_revision") or target_revision)
    produced_menu_id = record.get("produced_menu_id")

    await _emit(db, user_id, taxonomy.DAILY_MENU_SEND_CLAIMED,
                identity=identity, attempt_id=attempt_id, status="claimed",
                extra={"target_menu_id": target_menu_id,
                       "target_revision": target_revision,
                       "requested_by": requested_by})

    # --- RESUME: the result already exists; never regenerate -----------------
    if produced_menu_id or await _target_already_active(db, user_id, target_menu_id, moment):
        resumed_id = produced_menu_id or target_menu_id
        await _emit(db, user_id, taxonomy.DAILY_MENU_STALE_CLAIM_RECOVERED,
                    identity=identity, attempt_id=attempt_id, status="resumed",
                    extra={"resumed_menu_id": resumed_id})
        await ops.complete_operation(
            db, user_id, day, kind=ops.KIND_REFRESH_REQUEST,
            identity=identity, attempt_id=attempt_id, now=moment,
        )
        delivered = False
        if deliver is not None:
            text = await _load_persisted_text(db, user_id, moment)
            if text is not None:
                await deliver(resumed_id, text)
                delivered = True
        return RefreshResult(status=RESUMED, menu_id=resumed_id,
                             revision=target_revision, attempt_id=attempt_id,
                             generated=False, delivered=delivered)

    # Captured before generation, only for the text_identical telemetry below.
    previous_text = await _load_persisted_text(db, user_id, moment)

    # --- GENERATE under a renewed lease --------------------------------------
    fence = _Fence()
    interval = (
        renew_every
        if renew_every is not None
        else float(ops.MENU_LEASE_RENEW_EVERY_SECONDS)
    )
    beat: asyncio.Task | None = None
    if interval > 0:
        beat = asyncio.create_task(
            _heartbeat(db, user_id, day, identity=identity, attempt_id=attempt_id,
                       fence=fence, clock=tick, interval=interval)
        )
    try:
        payload = await generate()
    except Exception as exc:  # noqa: BLE001
        await ops.fail_operation(
            db, user_id, day, kind=ops.KIND_REFRESH_REQUEST, identity=identity,
            attempt_id=attempt_id, category=GENERATION_ERROR,
            error_code=exc.__class__.__name__, now=moment,
        )
        await _emit(db, user_id, taxonomy.DAILY_MENU_SEND_FAILED,
                    identity=identity, attempt_id=attempt_id, status="failed",
                    extra={"failure_category": GENERATION_ERROR,
                           "error_code": exc.__class__.__name__})
        return RefreshResult(status=GENERATION_FAILED, attempt_id=attempt_id,
                             reason=GENERATION_ERROR)
    finally:
        if beat is not None:
            beat.cancel()
            with suppress(asyncio.CancelledError):
                await beat

    if fence.lost:
        # Ownership was lost during generation: persist nothing at all.
        await _emit(db, user_id, taxonomy.DAILY_MENU_DUPLICATE_SUPPRESSED,
                    identity=identity, attempt_id=attempt_id, status="fenced",
                    extra={"reason": LEASE_RENEWAL_FAILED})
        return RefreshResult(status=REFUSED_CONFLICT, attempt_id=attempt_id,
                             generated=True, reason=LEASE_RENEWAL_FAILED)

    # --- PERSIST atomically ---------------------------------------------------
    persisted = await ops.persist_refresh_result(
        db, user_id, day,
        identity=identity, attempt_id=attempt_id,
        text=str(payload.get("text") or ""),
        meals=payload.get("meals"),
        strategy=payload.get("strategy"),
        produced_plan_id=identity.get("plan_id"),
        now=moment,
    )

    if persisted.status != ops.ACQUIRED:
        category = {
            ops.STALE_SOURCE: STALE_SOURCE,
            ops.OWNERSHIP_LOST: OWNERSHIP_LOST,
        }.get(persisted.status, RESULT_PERSISTENCE_FAILED)
        await _emit(db, user_id, taxonomy.DAILY_MENU_SEND_FAILED,
                    identity=identity, attempt_id=attempt_id, status="refused",
                    extra={"failure_category": category,
                           "reason": persisted.reason or persisted.status})
        status = (
            REFUSED_STALE_SOURCE if persisted.status == ops.STALE_SOURCE
            else REFUSED_CONFLICT
        )
        return RefreshResult(status=status, attempt_id=attempt_id,
                             generated=True, reason=category)

    # DECISION-R is observable here: an explicit refresh mints a new revision
    # and sends even when the rendered text is byte-identical, and
    # ``text_identical`` records how often that happens in production.
    await _emit(db, user_id, taxonomy.DAILY_MENU_EXPLICIT_REFRESH,
                identity=identity, attempt_id=attempt_id, status="persisted",
                extra={"previous_menu_id": current_menu_id or ops.NONE_SENTINEL,
                       "new_menu_id": target_menu_id,
                       "text_identical": bool(
                           previous_text is not None
                           and payload.get("text") == previous_text
                       )})

    await ops.complete_operation(
        db, user_id, day, kind=ops.KIND_REFRESH_REQUEST,
        identity=identity, attempt_id=attempt_id, now=moment,
    )

    # --- DELIVER through the B-2 boundary ------------------------------------
    delivered = False
    if deliver is not None:
        await deliver(target_menu_id, str(payload.get("text") or ""))
        delivered = True

    return RefreshResult(status=REFRESHED, menu_id=target_menu_id,
                         revision=target_revision, attempt_id=attempt_id,
                         generated=True, delivered=delivered)


async def _resume_produced(
    db: Any,
    user_id: int,
    day: str,
    stored: dict[str, Any],
    now: datetime,
    deliver: Callable[[str, str], Awaitable[Any]] | None,
    requested_by: str,
) -> RefreshResult | None:
    """Adopt a crashed attempt's ALREADY-PERSISTED result.

    Uses the record's own stored identity, because the freshly computed one
    has rotated past it. Never regenerates and never mints a revision: the
    work is already durable, only completion and delivery remain.
    """
    identity = stored.get("identity")
    produced = stored.get("produced_menu_id")
    if not isinstance(identity, dict) or not produced:
        return None

    attempt_id = ops.new_attempt_id()
    claim = await ops.claim_operation(
        db, user_id, day, kind=ops.KIND_REFRESH_REQUEST,
        identity=identity, attempt_id=attempt_id, now=now,
        requested_by=requested_by,
    )
    if claim.status == ops.IN_FLIGHT:
        return RefreshResult(status=SUPPRESSED_IN_FLIGHT, menu_id=produced,
                             reason=IN_FLIGHT)
    if claim.status != ops.ACQUIRED:
        return None

    await _emit(db, user_id, taxonomy.DAILY_MENU_STALE_CLAIM_RECOVERED,
                identity=identity, attempt_id=attempt_id, status="resumed",
                extra={"resumed_menu_id": produced,
                       "previous_attempt_id": stored.get("attempt_id")})
    await ops.complete_operation(
        db, user_id, day, kind=ops.KIND_REFRESH_REQUEST,
        identity=identity, attempt_id=attempt_id, now=now,
    )

    delivered = False
    if deliver is not None:
        text = await _load_persisted_text(db, user_id, now)
        if text is not None:
            await deliver(produced, text)
            delivered = True
    return RefreshResult(status=RESUMED, menu_id=produced,
                         revision=stored.get("target_revision"),
                         attempt_id=attempt_id, generated=False,
                         delivered=delivered)


async def _target_already_active(
    db: Any, user_id: int, target_menu_id: str, now: datetime,
) -> bool:
    """Defensive D6 check: the reserved menu is already the active one.

    Under the atomic persistence primitive this cannot happen, but treating it
    as persisted (rather than regenerating blindly) keeps recovery correct if a
    future refactor ever splits that write.
    """
    from noam_coach.services.daily_menu_state import get_active_daily_menu

    try:
        active = await get_active_daily_menu(db, user_id, now=now)
    except Exception:  # noqa: BLE001
        return False
    return isinstance(active, dict) and active.get("menu_id") == target_menu_id


async def _load_persisted_text(db: Any, user_id: int, now: datetime) -> str | None:
    from noam_coach.services.daily_menu_state import get_active_daily_menu

    try:
        active = await get_active_daily_menu(db, user_id, now=now)
    except Exception:  # noqa: BLE001
        return None
    if isinstance(active, dict):
        return active.get("text")
    return None


REFRESH_TOAST = {
    SUPPRESSED_IN_FLIGHT: "התפריט כבר מתעדכן ✅",
    SUPPRESSED_ALREADY_COMPLETED: "התפריט כבר מעודכן ✅",
    REFUSED_CONFLICT: "נסה שוב עוד רגע",
    REFUSED_STALE_SOURCE: "התפריט השתנה בינתיים — פתח אותו שוב",
    GENERATION_FAILED: "לא הצלחתי לרענן כרגע",
}


def refresh_toast(result: RefreshResult) -> str:
    return REFRESH_TOAST.get(result.status, "התפריט כבר מעודכן ✅")
