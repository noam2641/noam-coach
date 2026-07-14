"""Correlation identifier generators.

Prefixed, collision-resistant, log-friendly IDs. The prefix makes an ID's
kind self-describing anywhere it appears (event rows, timelines, headers)
so a trace_id can never be silently confused with an interaction_id.
"""

from __future__ import annotations

from uuid import uuid4


def _new(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:16]}"


def new_trace_id() -> str:
    """A logically related user journey / operation chain."""
    return _new("tr")


def new_interaction_id() -> str:
    """One user-originated interaction (message, photo, callback, Mini App action)."""
    return _new("in")


def new_span_id() -> str:
    """One meaningful internal operation inside an interaction."""
    return _new("sp")


def new_render_id() -> str:
    """One prepared user-visible render (text + controls)."""
    return _new("rn")


def new_ai_call_id() -> str:
    """One observable AI invocation."""
    return _new("ai")


def new_media_id() -> str:
    """One media identity (reference/fingerprint record, never bytes)."""
    return _new("md")
