"""Contextvar propagation of the active trace / interaction / span.

The ingress boundary (batch O2) opens an :func:`interaction_scope`; every
``emit_event`` deeper in the same asyncio task then inherits the correlation
identity automatically — no need to thread trace_id through dozens of
signatures. Spans nest: :func:`span_scope` records its parent so
parent/child operation relationships stay reconstructable.

contextvars are asyncio-task-local, so concurrent user updates cannot bleed
correlation identity into each other.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Iterator

from noam_coach.observability.ids import new_interaction_id, new_span_id, new_trace_id

_trace_id: ContextVar[str | None] = ContextVar("obs_trace_id", default=None)
_interaction_id: ContextVar[str | None] = ContextVar("obs_interaction_id", default=None)
_span_id: ContextVar[str | None] = ContextVar("obs_span_id", default=None)
_parent_span_id: ContextVar[str | None] = ContextVar("obs_parent_span_id", default=None)
_user_id: ContextVar[int | None] = ContextVar("obs_user_id", default=None)


def current_user_id() -> int | None:
    """The user the active interaction belongs to (for boundaries whose
    signatures don't carry a user id, e.g. safe_edit)."""
    return _user_id.get()


def current_trace_id() -> str | None:
    return _trace_id.get()


def current_interaction_id() -> str | None:
    return _interaction_id.get()


def current_span_id() -> str | None:
    return _span_id.get()


def current_parent_span_id() -> str | None:
    return _parent_span_id.get()


def correlation_kwargs() -> dict[str, Any]:
    """Correlation fields for ``append_event``/``emit_event`` from the ambient context."""
    return {
        "trace_id": _trace_id.get(),
        "interaction_id": _interaction_id.get(),
        "span_id": _span_id.get(),
        "parent_span_id": _parent_span_id.get(),
    }


@dataclass(frozen=True)
class InteractionScope:
    trace_id: str
    interaction_id: str


@contextmanager
def interaction_scope(
    *,
    trace_id: str | None = None,
    interaction_id: str | None = None,
    user_id: int | None = None,
) -> Iterator[InteractionScope]:
    """Open the correlation scope for one user-originated interaction.

    ``trace_id=None`` continues the ambient trace when one exists (e.g. a
    callback that belongs to an ongoing journey passes the stored trace id
    explicitly; a fresh message starts a fresh trace).
    """
    resolved_trace = trace_id or _trace_id.get() or new_trace_id()
    resolved_interaction = interaction_id or new_interaction_id()
    t1 = _trace_id.set(resolved_trace)
    t2 = _interaction_id.set(resolved_interaction)
    t3 = _span_id.set(None)
    t4 = _parent_span_id.set(None)
    t5 = _user_id.set(user_id if user_id is not None else _user_id.get())
    try:
        yield InteractionScope(trace_id=resolved_trace, interaction_id=resolved_interaction)
    finally:
        _trace_id.reset(t1)
        _interaction_id.reset(t2)
        _span_id.reset(t3)
        _parent_span_id.reset(t4)
        _user_id.reset(t5)


@dataclass(frozen=True)
class SpanScope:
    span_id: str
    parent_span_id: str | None


@contextmanager
def span_scope(span_id: str | None = None) -> Iterator[SpanScope]:
    """Open one meaningful internal operation; nests under the current span."""
    parent = _span_id.get()
    resolved = span_id or new_span_id()
    t1 = _span_id.set(resolved)
    t2 = _parent_span_id.set(parent)
    try:
        yield SpanScope(span_id=resolved, parent_span_id=parent)
    finally:
        _span_id.reset(t1)
        _parent_span_id.reset(t2)
