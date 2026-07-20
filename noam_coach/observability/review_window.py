"""Reproducible review-window selection (Review batch R2).

The continuous-improvement workflow reviews "everything since my last
review" — or any explicit historical window — without the operator
reconstructing trace ids by hand. This module owns:

- **Requested selection**: the operator's intent (cursor / time window /
  recent duration / explicit event-id range / explicit traces or
  interactions), recorded verbatim so any package can be rebuilt later.
- **Resolved selection**: the immutable canonical form — a closed event-id
  range plus optional trace/interaction filters. Timestamps only define
  the REQUESTED window; they are resolved once to an id range and all
  extraction pages by id, preserving the repository's append-order
  causality contract (monotonic row ids, never wall-clock comparison).
- **The last-review cursor**: a repo-local convenience pointer, NOT the
  source of truth. It lives outside the product database (works against a
  copied snapshot), is written atomically, advances only when a caller
  explicitly says the review completed, and survives interruption
  unchanged. A malformed state file raises a clear, recoverable error —
  explicit selections keep working regardless.

Boundary semantics: UTC everywhere; naive timestamps are interpreted as
UTC; start is inclusive, end is exclusive ([start, end)). Events written
at the exact boundary instant follow the stored-string comparison of the
canonical ``created_at`` format; sub-second precision at the boundary
second is not guaranteed and reviews needing exactness select by event id.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator

import event_log
from event_log import ProductEvent

REVIEW_STATE_VERSION = 1
DEFAULT_STATE_FILENAME = "state.json"
DEFAULT_PAGE_SIZE = 500
FIRST_REVIEW_DEFAULT_HOURS = 48


class ReviewWindowError(ValueError):
    """A selection could not be resolved as requested."""


class MalformedCursorError(ReviewWindowError):
    """reviews/state.json exists but cannot be trusted."""


class NoCursorError(ReviewWindowError):
    """since-last-review was requested but no completed review exists."""


def _to_utc_iso(value: str) -> str:
    """Normalize an operator timestamp to the stored UTC ISO format."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ReviewWindowError(f"invalid timestamp {value!r}: {exc}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


@dataclass(frozen=True)
class RequestedSelection:
    """The operator's selection intent, recorded verbatim in the manifest."""

    mode: str  # since_last_review | time_window | last_hours | event_id_range | traces | interactions
    start_time: str | None = None
    end_time: str | None = None
    last_hours: float | None = None
    after_event_id: int | None = None
    until_event_id: int | None = None
    trace_ids: tuple[str, ...] = field(default=())
    interaction_ids: tuple[str, ...] = field(default=())

    _MODES = (
        "since_last_review",
        "time_window",
        "last_hours",
        "event_id_range",
        "traces",
        "interactions",
    )

    def __post_init__(self) -> None:
        if self.mode not in self._MODES:
            raise ReviewWindowError(f"unknown selection mode {self.mode!r}")
        if self.mode == "time_window" and self.start_time is None and self.end_time is None:
            raise ReviewWindowError("time_window selection needs a start and/or end timestamp")
        if self.mode == "last_hours" and (self.last_hours is None or self.last_hours <= 0):
            raise ReviewWindowError("last_hours selection needs a positive duration")
        if self.mode == "traces" and not self.trace_ids:
            raise ReviewWindowError("traces selection needs at least one trace id")
        if self.mode == "interactions" and not self.interaction_ids:
            raise ReviewWindowError("interactions selection needs at least one interaction id")

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "last_hours": self.last_hours,
            "after_event_id": self.after_event_id,
            "until_event_id": self.until_event_id,
            "trace_ids": list(self.trace_ids),
            "interaction_ids": list(self.interaction_ids),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RequestedSelection":
        return cls(
            mode=data["mode"],
            start_time=data.get("start_time"),
            end_time=data.get("end_time"),
            last_hours=data.get("last_hours"),
            after_event_id=data.get("after_event_id"),
            until_event_id=data.get("until_event_id"),
            trace_ids=tuple(data.get("trace_ids") or ()),
            interaction_ids=tuple(data.get("interaction_ids") or ()),
        )


@dataclass(frozen=True)
class ResolvedSelection:
    """The immutable canonical selection a package is built from.

    ``after_event_id`` is EXCLUSIVE and ``until_event_id`` INCLUSIVE —
    exactly the cursor semantics ("everything after what I last reviewed,
    up to and including the newest event that existed when I resolved").
    An empty window is represented explicitly (``is_empty``), never as an
    error.
    """

    user_id: int
    after_event_id: int
    until_event_id: int | None
    trace_ids: tuple[str, ...] = field(default=())
    interaction_ids: tuple[str, ...] = field(default=())
    resolved_start_time: str | None = None
    resolved_end_time: str | None = None

    @property
    def is_empty(self) -> bool:
        return self.until_event_id is None or self.until_event_id <= self.after_event_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "after_event_id": self.after_event_id,
            "until_event_id": self.until_event_id,
            "trace_ids": list(self.trace_ids),
            "interaction_ids": list(self.interaction_ids),
            "resolved_start_time": self.resolved_start_time,
            "resolved_end_time": self.resolved_end_time,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ResolvedSelection":
        return cls(
            user_id=int(data["user_id"]),
            after_event_id=int(data["after_event_id"]),
            until_event_id=(
                int(data["until_event_id"]) if data.get("until_event_id") is not None else None
            ),
            trace_ids=tuple(data.get("trace_ids") or ()),
            interaction_ids=tuple(data.get("interaction_ids") or ()),
            resolved_start_time=data.get("resolved_start_time"),
            resolved_end_time=data.get("resolved_end_time"),
        )


# ---------------------------------------------------------------------------
# Selection resolution
# ---------------------------------------------------------------------------


async def resolve_selection(
    db: Any,
    user_id: int,
    requested: RequestedSelection,
    *,
    reviews_dir: Path | str = "reviews",
) -> ResolvedSelection:
    """Resolve the operator's intent to the immutable canonical selection.

    Resolution reads the CURRENT max event id exactly once, so a review
    never silently grows while it is being built. ``since_last_review``
    with no cursor raises :class:`NoCursorError` — the CLI decides the
    first-run default (and says so out loud) rather than this module
    silently reviewing all history.
    """
    latest = await event_log.max_event_id(db, user_id)
    if requested.mode == "since_last_review":
        cursor = load_cursor(reviews_dir)
        if cursor is None:
            raise NoCursorError(
                "no completed review recorded yet — pass an explicit selection "
                f"(e.g. --last-hours {FIRST_REVIEW_DEFAULT_HOURS}) for the first review"
            )
        return ResolvedSelection(
            user_id=user_id,
            after_event_id=cursor["last_reviewed_event_id"],
            until_event_id=latest,
        )
    if requested.mode == "event_id_range":
        return ResolvedSelection(
            user_id=user_id,
            after_event_id=int(requested.after_event_id or 0),
            until_event_id=(
                int(requested.until_event_id) if requested.until_event_id is not None else latest
            ),
        )
    if requested.mode in ("time_window", "last_hours"):
        if requested.mode == "last_hours":
            start_dt = datetime.now(timezone.utc) - timedelta(hours=float(requested.last_hours or 0))
            start = start_dt.isoformat()
            end = None
        else:
            start = _to_utc_iso(requested.start_time) if requested.start_time else None
            end = _to_utc_iso(requested.end_time) if requested.end_time else None
            if start and end and start >= end:
                raise ReviewWindowError("window start must be before window end")
        bounds = await event_log.event_id_range_for_window(db, user_id, start=start, end=end)
        if bounds is None:
            return ResolvedSelection(
                user_id=user_id,
                after_event_id=latest or 0,
                until_event_id=None,
                resolved_start_time=start,
                resolved_end_time=end,
            )
        first_id, last_id = bounds
        return ResolvedSelection(
            user_id=user_id,
            after_event_id=first_id - 1,
            until_event_id=last_id,
            resolved_start_time=start,
            resolved_end_time=end,
        )
    # traces / interactions: filters over the full id range.
    return ResolvedSelection(
        user_id=user_id,
        after_event_id=0,
        until_event_id=latest,
        trace_ids=requested.trace_ids,
        interaction_ids=requested.interaction_ids,
    )


async def iter_window_events(
    db: Any,
    selection: ResolvedSelection,
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> AsyncIterator[ProductEvent]:
    """Stream the selection in canonical append order, page by page.

    Deterministic and bounded: each page is ``id > last_seen`` ascending,
    so a multi-day window never depends on the 2000-row ``list_events``
    ceiling and never loads unbounded history into memory.
    """
    if selection.is_empty:
        return
    after = selection.after_event_id
    while True:
        page = await event_log.list_events_after(
            db,
            selection.user_id,
            after_id=after,
            until_id=selection.until_event_id,
            trace_ids=list(selection.trace_ids) or None,
            interaction_ids=list(selection.interaction_ids) or None,
            limit=page_size,
        )
        if not page:
            return
        for item in page:
            yield item
        after = page[-1].id
        if len(page) < page_size:
            return


async def collect_window_events(
    db: Any,
    selection: ResolvedSelection,
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> list[ProductEvent]:
    return [event async for event in iter_window_events(db, selection, page_size=page_size)]


# ---------------------------------------------------------------------------
# The last-review cursor (convenience pointer, never the source of truth)
# ---------------------------------------------------------------------------


def _state_path(reviews_dir: Path | str) -> Path:
    return Path(reviews_dir) / DEFAULT_STATE_FILENAME


def load_cursor(reviews_dir: Path | str) -> dict[str, Any] | None:
    """The recorded last-completed-review pointer, or None on first use.

    Raises :class:`MalformedCursorError` — with the offending path and a
    recovery hint — when the file exists but cannot be trusted. Explicit
    selections never depend on this file.
    """
    path = _state_path(reviews_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("state root is not an object")
        last_id = data["last_reviewed_event_id"]
        if not isinstance(last_id, int) or last_id < 0:
            raise ValueError(f"invalid last_reviewed_event_id: {last_id!r}")
        return data
    except (ValueError, KeyError, OSError) as exc:
        raise MalformedCursorError(
            f"review cursor {path} is malformed ({exc}); fix or delete it, or run "
            "with an explicit selection (--start/--end/--last-hours/--after-id)"
        ) from exc


def advance_cursor(
    reviews_dir: Path | str,
    *,
    last_reviewed_event_id: int,
    review_id: str,
) -> dict[str, Any]:
    """Atomically record a COMPLETED review (temp file + os.replace).

    Callers advance only after package creation, review, findings
    validation and report generation all succeeded — an interrupted run
    leaves the previous state byte-identical. Advancing never destroys
    history: rebuilding an old review or reviewing an overlapping window
    uses explicit selections, which ignore this file entirely.
    """
    path = _state_path(reviews_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "state_version": REVIEW_STATE_VERSION,
        "last_reviewed_event_id": int(last_reviewed_event_id),
        "review_id": review_id,
        "advanced_at": datetime.now(timezone.utc).isoformat(),
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return state
