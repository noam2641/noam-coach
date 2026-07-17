"""Structured product event log and deterministic conversation replay.

The Telegram transcript alone is not enough to understand why a flow broke.
This module records every important state transition and domain action with the
algorithm/prompt version that produced it.  It is intentionally independent of
Telegram so it can be reused by the bot, Mini App and tests.

Observability O1: ``product_events`` is the CANONICAL interaction trace
stream.  The envelope carries correlation identity (``trace_id`` /
``interaction_id`` / ``span_id`` / ``parent_span_id``), the surface the user
experienced, and status/outcome semantics, in addition to the original
entity/flow fields.  All correlation fields are optional so the 40+
pre-existing ``append_event`` call sites keep working unchanged; historical
rows read back with ``trace_id is None`` and are thereby explicitly
identifiable as legacy/uncorrelated — they are never silently reinterpreted
as if they carried correlation.

Higher-level policy (redaction, observability modes, safe-write behavior)
lives in ``noam_coach.observability``; this module stays the thin canonical
store boundary.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from helpers import utc_now


@dataclass(frozen=True)
class ProductEvent:
    id: int
    user_id: int
    event: str
    entity: str
    entity_id: str | None
    flow_id: str | None
    flow_version: int | None
    source: str
    properties: dict[str, Any]
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    created_at: str
    # Observability O1 envelope (None on legacy/uncorrelated rows).
    event_version: int = 1
    trace_id: str | None = None
    interaction_id: str | None = None
    span_id: str | None = None
    parent_span_id: str | None = None
    surface: str | None = None
    status: str | None = None
    outcome: str | None = None

    @property
    def is_legacy_uncorrelated(self) -> bool:
        """True when the row predates (or bypassed) trace correlation."""
        return self.trace_id is None and self.interaction_id is None


async def append_event(
    db: Any,
    user_id: int,
    event: str,
    *,
    entity: str = "system",
    entity_id: str | int | None = None,
    flow_id: str | None = None,
    flow_version: int | None = None,
    source: str = "bot",
    properties: dict[str, Any] | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    event_version: int = 1,
    trace_id: str | None = None,
    interaction_id: str | None = None,
    span_id: str | None = None,
    parent_span_id: str | None = None,
    surface: str | None = None,
    status: str | None = None,
    outcome: str | None = None,
) -> int:
    """Append one immutable event and return its id.

    Correlation defaulting (Observability O5): when the caller passes no
    correlation at all — every pre-observability domain call site — the
    ambient interaction scope (if one is open) supplies trace/interaction/
    span identity, so legacy domain events like ``meal_saved`` join the
    trace of the interaction that caused them. Rows written outside any
    scope keep NULL correlation and stay explicitly legacy/uncorrelated.
    """
    if trace_id is None and interaction_id is None and span_id is None:
        try:
            from noam_coach.observability import obs_context as _obs_context

            trace_id = _obs_context.current_trace_id()
            interaction_id = _obs_context.current_interaction_id()
            span_id = _obs_context.current_span_id()
            parent_span_id = parent_span_id or _obs_context.current_parent_span_id()
        except Exception:  # noqa: BLE001 — correlation is best-effort here.
            pass
    return await db.execute(
        """
        INSERT INTO product_events(
            user_id, event, entity, entity_id, flow_id, flow_version,
            source, properties, before_state, after_state, created_at,
            event_version, trace_id, interaction_id, span_id,
            parent_span_id, surface, status, outcome
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            event,
            entity,
            str(entity_id) if entity_id is not None else None,
            flow_id,
            flow_version,
            source,
            json.dumps(properties or {}, ensure_ascii=False),
            json.dumps(before, ensure_ascii=False) if before is not None else None,
            json.dumps(after, ensure_ascii=False) if after is not None else None,
            utc_now(),
            int(event_version),
            trace_id,
            interaction_id,
            span_id,
            parent_span_id,
            surface,
            status,
            outcome,
        ),
    )


def _decode(row: dict[str, Any]) -> ProductEvent:
    return ProductEvent(
        id=int(row["id"]),
        user_id=int(row["user_id"]),
        event=row["event"],
        entity=row["entity"],
        entity_id=row.get("entity_id"),
        flow_id=row.get("flow_id"),
        flow_version=(int(row["flow_version"]) if row.get("flow_version") is not None else None),
        source=row.get("source") or "bot",
        properties=json.loads(row.get("properties") or "{}"),
        before=json.loads(row["before_state"]) if row.get("before_state") else None,
        after=json.loads(row["after_state"]) if row.get("after_state") else None,
        created_at=row["created_at"],
        event_version=int(row.get("event_version") or 1),
        trace_id=row.get("trace_id"),
        interaction_id=row.get("interaction_id"),
        span_id=row.get("span_id"),
        parent_span_id=row.get("parent_span_id"),
        surface=row.get("surface"),
        status=row.get("status"),
        outcome=row.get("outcome"),
    )


async def list_events(
    db: Any,
    user_id: int,
    *,
    limit: int = 200,
    event: str | None = None,
    flow_id: str | None = None,
    trace_id: str | None = None,
    interaction_id: str | None = None,
) -> list[ProductEvent]:
    clauses = ["user_id=?"]
    params: list[Any] = [user_id]
    if event:
        clauses.append("event=?")
        params.append(event)
    if flow_id:
        clauses.append("flow_id=?")
        params.append(flow_id)
    if trace_id:
        clauses.append("trace_id=?")
        params.append(trace_id)
    if interaction_id:
        clauses.append("interaction_id=?")
        params.append(interaction_id)
    params.append(max(1, min(limit, 2000)))
    rows = await db.fetch_all(
        f"""
        SELECT * FROM product_events
        WHERE {' AND '.join(clauses)}
        ORDER BY id DESC
        LIMIT ?
        """,
        tuple(params),
    )
    return [_decode(row) for row in reversed(rows)]


async def max_event_id(db: Any, user_id: int) -> int | None:
    """Highest canonical event id for one user (None when no events exist)."""
    rows = await db.fetch_all(
        "SELECT MAX(id) AS max_id FROM product_events WHERE user_id=?",
        (user_id,),
    )
    value = rows[0]["max_id"] if rows else None
    return int(value) if value is not None else None


async def event_id_range_for_window(
    db: Any,
    user_id: int,
    *,
    start: str | None = None,
    end: str | None = None,
) -> tuple[int, int] | None:
    """MIN/MAX event id whose ``created_at`` falls in ``[start, end)``.

    Review batch R2: timestamps define the REQUESTED window only — they are
    resolved to a canonical id range once, and all further selection pages
    by id, so ordering authority stays the append order (monotonic row
    ids), never wall-clock comparison. Bounds are UTC ISO strings in the
    same format ``utc_now`` writes; start is inclusive, end is exclusive.
    Returns None for an empty window.
    """
    clauses = ["user_id=?"]
    params: list[Any] = [user_id]
    if start is not None:
        clauses.append("created_at >= ?")
        params.append(start)
    if end is not None:
        clauses.append("created_at < ?")
        params.append(end)
    rows = await db.fetch_all(
        f"""
        SELECT MIN(id) AS min_id, MAX(id) AS max_id FROM product_events
        WHERE {' AND '.join(clauses)}
        """,
        tuple(params),
    )
    if not rows or rows[0]["min_id"] is None:
        return None
    return int(rows[0]["min_id"]), int(rows[0]["max_id"])


async def list_events_after(
    db: Any,
    user_id: int,
    *,
    after_id: int,
    until_id: int | None = None,
    trace_ids: list[str] | None = None,
    interaction_ids: list[str] | None = None,
    limit: int = 500,
) -> list[ProductEvent]:
    """One ascending page of events with ``id > after_id`` (R2 pagination).

    The additive window-query primitive: unlike :func:`list_events` (newest
    N, bounded at 2000) this pages FORWARD in canonical append order, so a
    multi-day window is read in deterministic chunks of ``limit`` without
    ever loading unbounded history. ``until_id`` is inclusive. Optional
    trace/interaction filters select exactly those groups' events (legacy
    uncorrelated rows never match a filter — callers include them by
    omitting filters).
    """
    clauses = ["user_id=?", "id > ?"]
    params: list[Any] = [user_id, int(after_id)]
    if until_id is not None:
        clauses.append("id <= ?")
        params.append(int(until_id))
    if trace_ids:
        clauses.append(f"trace_id IN ({','.join('?' * len(trace_ids))})")
        params.extend(trace_ids)
    if interaction_ids:
        clauses.append(f"interaction_id IN ({','.join('?' * len(interaction_ids))})")
        params.extend(interaction_ids)
    params.append(max(1, min(limit, 2000)))
    rows = await db.fetch_all(
        f"""
        SELECT * FROM product_events
        WHERE {' AND '.join(clauses)}
        ORDER BY id ASC
        LIMIT ?
        """,
        tuple(params),
    )
    return [_decode(row) for row in rows]


async def replay_summary(db: Any, user_id: int, *, limit: int = 100) -> str:
    """Return a compact human-readable timeline for debugging.

    Kept for backward compatibility; the structured replacement is
    ``noam_coach.observability.trace_reader`` (machine-readable grouping)
    and the batch-O9 timeline renderer.
    """
    events = await list_events(db, user_id, limit=limit)
    if not events:
        return "לא נמצאו אירועים."
    lines: list[str] = []
    for item in events:
        time_text = item.created_at[11:19] if len(item.created_at) >= 19 else item.created_at
        flow = f" flow={item.flow_id}@{item.flow_version}" if item.flow_id else ""
        details = ""
        if item.properties:
            safe = {k: v for k, v in item.properties.items() if k not in {"token", "secret", "image_bytes"}}
            details = f" {json.dumps(safe, ensure_ascii=False, separators=(',', ':'))[:180]}"
        lines.append(f"{time_text} · {item.event} · {item.entity}{flow}{details}")
    return "\n".join(lines)
