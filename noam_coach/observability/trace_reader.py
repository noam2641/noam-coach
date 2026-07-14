"""Structured trace reader primitives (Observability O1).

Machine-readable grouping over the canonical ``product_events`` stream.
Causal order inside a trace is the append order (``id`` ascending): SQLite
assigns monotonically increasing ids under this single-writer design, so
grouping never depends on wall-clock string comparison.

The richer session/interaction trace model and human timeline renderer are
batch O9; these primitives are the query layer they build on.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any

import event_log
from event_log import ProductEvent


async def events_for_trace(
    db: Any,
    user_id: int,
    trace_id: str,
    *,
    limit: int = 2000,
) -> list[ProductEvent]:
    """All events of one trace, in causal (append) order."""
    return await event_log.list_events(db, user_id, trace_id=trace_id, limit=limit)


async def events_for_interaction(
    db: Any,
    user_id: int,
    interaction_id: str,
    *,
    limit: int = 2000,
) -> list[ProductEvent]:
    """All events of one user interaction, in causal (append) order."""
    return await event_log.list_events(db, user_id, interaction_id=interaction_id, limit=limit)


async def recent_events(db: Any, user_id: int, *, limit: int = 200) -> list[ProductEvent]:
    return await event_log.list_events(db, user_id, limit=limit)


def group_by_trace(events: list[ProductEvent]) -> "OrderedDict[str | None, list[ProductEvent]]":
    """Group events by trace_id, preserving causal order inside each group.

    Legacy/uncorrelated rows group under the explicit key ``None`` — they are
    never silently merged into a correlated trace.
    """
    groups: "OrderedDict[str | None, list[ProductEvent]]" = OrderedDict()
    for item in events:
        groups.setdefault(item.trace_id, []).append(item)
    return groups


def group_by_interaction(
    events: list[ProductEvent],
) -> "OrderedDict[str | None, list[ProductEvent]]":
    groups: "OrderedDict[str | None, list[ProductEvent]]" = OrderedDict()
    for item in events:
        groups.setdefault(item.interaction_id, []).append(item)
    return groups


def span_children(events: list[ProductEvent], span_id: str) -> list[ProductEvent]:
    """Events whose parent span is ``span_id`` (direct children only)."""
    return [item for item in events if item.parent_span_id == span_id]


def legacy_events(events: list[ProductEvent]) -> list[ProductEvent]:
    """The explicitly identifiable uncorrelated (pre-O1 style) rows."""
    return [item for item in events if item.is_legacy_uncorrelated]
