"""The safe canonical event-write boundary.

Rules this module enforces (Observability O1):

1. NEVER break coaching: any failure while persisting an observability event
   is contained here — the caller's user-facing flow proceeds.
2. NEVER fail silently with no health signal: a write failure increments an
   in-process health counter, logs a structured error, and (best-effort,
   non-recursive) records one ``observability.write_failed`` event.
3. Every payload passes the canonical redaction boundary
   (:mod:`noam_coach.observability.redaction`) — including in DEBUG mode.
4. Mode policy (:mod:`noam_coach.observability.modes`):
   - OFF: no write at all.
   - METADATA: ``properties`` (metadata) are kept; ``content`` fields are
     reduced to sha256+length digests under ``content_digest``.
   - CONTENT / DEBUG: ``content`` fields are kept (redacted) under
     ``content``; digests are still included so queries written against
     METADATA-mode rows keep working.
5. Correlation identity defaults from the ambient
   :mod:`noam_coach.observability.obs_context` scope, so instrumented code
   deep inside a handler does not need explicit correlation plumbing.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

import event_log
from noam_coach.observability import obs_context
from noam_coach.observability.modes import ObservabilityMode, get_mode
from noam_coach.observability.redaction import redact
from noam_coach.observability.taxonomy import (
    DELIVERY_ATTEMPTED,
    DELIVERY_SUCCEEDED,
    OBSERVABILITY_WRITE_FAILED,
    UI_RENDER_PREPARED,
    UI_VIEW_RENDERED,
    event_version,
)

LOGGER = logging.getLogger("noam_coach.observability")

# Events that prove the interaction produced something for the user.
#
# ``delivery.attempted`` counts deliberately: an interaction that TRIED to
# answer and failed at the transport is a delivery defect, not a routing
# black hole, and the two must not collapse into the same outcome. The
# accompanying ``delivery.failed`` is what distinguishes them.
RESPONSE_EVENTS: frozenset[str] = frozenset(
    {
        UI_RENDER_PREPARED,
        UI_VIEW_RENDERED,
        DELIVERY_ATTEMPTED,
        DELIVERY_SUCCEEDED,
    }
)

# In-process health state: observability degradation must stay detectable
# even when event persistence itself is what failed.
_health: dict[str, Any] = {
    "write_failures": 0,
    "write_failed_events_written": 0,
    "write_failed_event_failures": 0,
    "last_error": None,
    "last_failed_event": None,
}


def observability_health() -> dict[str, Any]:
    """Snapshot of the in-process observability health counters."""
    return dict(_health)


def reset_observability_health() -> None:
    _health.update(
        write_failures=0,
        write_failed_events_written=0,
        write_failed_event_failures=0,
        last_error=None,
        last_failed_event=None,
    )


def _digest(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        raw = value
    else:
        try:
            raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=repr)
        except (TypeError, ValueError):
            raw = repr(value)
    return {
        "sha256": hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest(),
        "chars": len(raw),
    }


async def emit_event(
    db: Any,
    user_id: int,
    event: str,
    *,
    entity: str = "system",
    entity_id: str | int | None = None,
    flow_id: str | None = None,
    flow_version: int | None = None,
    source: str = "bot",
    surface: str | None = None,
    status: str | None = None,
    outcome: str | None = None,
    properties: dict[str, Any] | None = None,
    content: dict[str, Any] | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    trace_id: str | None = None,
    interaction_id: str | None = None,
    span_id: str | None = None,
    parent_span_id: str | None = None,
) -> int | None:
    """Safely append one canonical event; returns the row id or ``None``.

    ``properties`` is correlation/metadata (kept in every non-OFF mode).
    ``content`` is conversational/payload material governed by the mode.
    """
    if event in RESPONSE_EVENTS:
        # Response evidence is scope state, not a persisted row: record it
        # before the mode gate so an interaction's outcome does not silently
        # change meaning when capture is dialled down.
        try:
            obs_context.mark_response()
        except Exception:  # noqa: BLE001 — rule 1: never break coaching.
            pass

    mode = get_mode()
    if mode is ObservabilityMode.OFF:
        return None
    try:
        correlation = obs_context.correlation_kwargs()
        if trace_id is not None:
            correlation["trace_id"] = trace_id
        if interaction_id is not None:
            correlation["interaction_id"] = interaction_id
        if span_id is not None:
            correlation["span_id"] = span_id
        if parent_span_id is not None:
            correlation["parent_span_id"] = parent_span_id

        merged: dict[str, Any] = dict(redact(properties) if properties else {})
        if content:
            merged["content_digest"] = {key: _digest(value) for key, value in content.items()}
            if mode in (ObservabilityMode.CONTENT, ObservabilityMode.DEBUG):
                merged["content"] = redact(content)

        return await event_log.append_event(
            db,
            user_id,
            event,
            entity=entity,
            entity_id=entity_id,
            flow_id=flow_id,
            flow_version=flow_version,
            source=source,
            surface=surface,
            status=status,
            outcome=outcome,
            properties=merged,
            before=redact(before) if before is not None else None,
            after=redact(after) if after is not None else None,
            event_version=event_version(event),
            **correlation,
        )
    except Exception as exc:  # noqa: BLE001 — rule 1: never break coaching.
        _health["write_failures"] += 1
        _health["last_error"] = f"{type(exc).__name__}: {exc}"
        _health["last_failed_event"] = event
        LOGGER.error(
            "observability write failed for event=%s user=%s: %s",
            event,
            user_id,
            exc,
            exc_info=True,
        )
        if event != OBSERVABILITY_WRITE_FAILED:
            try:
                await event_log.append_event(
                    db,
                    user_id,
                    OBSERVABILITY_WRITE_FAILED,
                    entity="observability",
                    source=source,
                    status="failed",
                    outcome="write_failed",
                    properties={
                        "failed_event": event,
                        "error_type": type(exc).__name__,
                    },
                )
                _health["write_failed_events_written"] += 1
            except Exception:  # noqa: BLE001 — no recursive failure loops.
                _health["write_failed_event_failures"] += 1
        return None
