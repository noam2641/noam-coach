"""Unhandled-error evidence at real execution boundaries (Review batch R1).

An unhandled exception used to leave a trace that simply *stopped* — the
interaction envelope emitted ingress evidence and then awaited the handler
with no capture, so the canonical stream held no ``error.captured`` row for
a crash (the taxonomy constant existed; the only emitter was the
daily-flags CAS conflict path). This module is the single capture
mechanism, shared by every boundary:

1. ``observed_handler`` (telegram_ingress) — capture INSIDE the interaction
   scope (fully correlated), then re-raise so python-telegram-bot's error
   handling is unchanged.
2. The PTB dispatch/error boundary (``observed_error_callback`` wrapping
   ``on_error`` at registration time) — the safety net for scheduled jobs
   and any path PTB routes to error handling. Runs OUTSIDE any interaction
   scope, so its events are explicitly uncorrelated; transient/stale
   Telegram conditions (already classified by
   ``noam_coach.services.telegram_errors``) are NOT crashes and are never
   recorded here.
3. ``mini_obs_scope`` (mini_api) — API-boundary capture that excludes
   ``HTTPException`` (expected domain rejection, not a crash).

Duplicate suppression: the first boundary to record an exception marks the
exception OBJECT; nested/outer boundaries seeing the same object skip it.
Cancellation (`asyncio.CancelledError`) is a BaseException — the capture
paths catch ``Exception`` only, so cancellation always propagates untouched
and is never misclassified as a crash.

Privacy: the event carries the exception class name and a bounded,
sanitized first message line (``redact_sensitive_text`` + the canonical
emit-time redactor). Raw traceback content is never written to product
events.
"""

from __future__ import annotations

from typing import Any

from noam_coach.observability import taxonomy
from noam_coach.observability.emit import emit_event
from noam_coach.services.telegram_errors import redact_sensitive_text

_CAPTURED_MARK = "_noam_error_captured"

ENTITY_EXECUTION_BOUNDARY = "execution_boundary"


def already_captured(exc: BaseException) -> bool:
    """True when some boundary already recorded this exception object."""
    return bool(getattr(exc, _CAPTURED_MARK, False))


def mark_captured(exc: BaseException) -> None:
    try:
        setattr(exc, _CAPTURED_MARK, True)
    except Exception:  # noqa: BLE001 — exotic exception types without __dict__.
        pass


def _sanitized_message(exc: BaseException) -> str:
    first_line = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
    return redact_sensitive_text(first_line, max_length=300)


async def capture_unhandled(
    db: Any,
    user_id: int,
    exc: BaseException,
    *,
    boundary: str,
    source: str,
    surface: str | None = None,
    properties: dict[str, Any] | None = None,
) -> bool:
    """Record one ``error.captured`` event for an unhandled exception.

    Returns True when an event was written, False when the exception was
    already captured by a nested boundary (or emit was skipped). Never
    raises — error evidence must not replace one failure with another —
    and never swallows the caller's exception (callers re-raise).
    """
    if already_captured(exc):
        return False
    mark_captured(exc)
    merged: dict[str, Any] = {
        "boundary": boundary,
        "error_type": type(exc).__name__,
        "message": _sanitized_message(exc),
    }
    if properties:
        merged.update(properties)
    try:
        row_id = await emit_event(
            db,
            user_id,
            taxonomy.ERROR_CAPTURED,
            entity=ENTITY_EXECUTION_BOUNDARY,
            source=source,
            surface=surface,
            status="failed",
            outcome="unhandled_exception",
            properties=merged,
        )
        return row_id is not None
    except Exception:  # noqa: BLE001 — emit_event already contains failures; belt and braces.
        return False
