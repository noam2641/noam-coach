"""Source-render lookup for callback correlation (Observability O3).

When a callback arrives, the only provider-side link to the keyboard the
user actually pressed is the Telegram message id the control lived on.
This module resolves that message id back to the render that delivered it,
by scanning recent ``delivery.succeeded`` events (newest first — a message
that was edited several times resolves to its LATEST delivered render,
which is exactly what the user saw when pressing).

No second store: the canonical product_events stream is the registry.
Resolution is evidence-based — when no delivered render matches, the
result is ``None`` and callers must represent the correlation as
unresolved, never guessed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import event_log
from noam_coach.observability import taxonomy


@dataclass(frozen=True)
class RenderRef:
    render_id: str
    trace_id: str | None
    interaction_id: str | None
    message_id: Any


@dataclass(frozen=True)
class ResolvedControl:
    render_id: str
    trace_id: str | None
    label: str | None
    callback_data: str


async def find_render_for_message(
    db: Any,
    user_id: int,
    message_id: Any,
    *,
    scan_limit: int = 400,
) -> RenderRef | None:
    """The most recent successfully delivered render on ``message_id``."""
    if message_id is None:
        return None
    events = await event_log.list_events(
        db, user_id, event=taxonomy.DELIVERY_SUCCEEDED, limit=scan_limit
    )
    for item in reversed(events):  # newest first
        if item.properties.get("message_id") == message_id and item.properties.get("render_id"):
            return RenderRef(
                render_id=item.properties["render_id"],
                trace_id=item.trace_id,
                interaction_id=item.interaction_id,
                message_id=message_id,
            )
    return None


async def resolve_control(
    db: Any,
    user_id: int,
    message_id: Any,
    callback_data: str,
    *,
    scan_limit: int = 400,
) -> ResolvedControl | None:
    """Resolve which displayed control produced ``callback_data``.

    Returns ``None`` when the source render cannot be proven. When the
    render is proven but the specific control label is not found in its
    recorded control model, the resolution still returns the render with
    ``label=None`` — a proven source with an unproven label is more honest
    than dropping the render evidence.
    """
    render = await find_render_for_message(
        db, user_id, message_id, scan_limit=scan_limit
    )
    if render is None:
        return None
    label: str | None = None
    prepared = await event_log.list_events(
        db, user_id, event=taxonomy.UI_RENDER_PREPARED, limit=scan_limit
    )
    for item in reversed(prepared):
        if item.properties.get("render_id") != render.render_id:
            continue
        for control in item.properties.get("controls") or []:
            if control.get("callback_data") == callback_data:
                label = control.get("label")
                break
        break
    return ResolvedControl(
        render_id=render.render_id,
        trace_id=render.trace_id,
        label=label,
        callback_data=callback_data,
    )
