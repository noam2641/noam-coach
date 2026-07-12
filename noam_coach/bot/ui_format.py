"""TASK-16 — shared user-facing formatting helpers for the coach UI.

These keep the nutrition/summary views consistent: one macro format
("1,316 קל׳" / "98 ג׳ חלבון"), one heading style, and HH:MM local times (never
a raw ISO/RFC3339 timestamp). They are intentionally tiny — a shared vocabulary,
not a UI framework.
"""
from __future__ import annotations

from datetime import datetime

# Canonical Hebrew units. Do NOT introduce "קק\"ל" / "קלוריות" / "קל'" /
# "גרם חלבון" variants in user-facing text — use these.
CAL_UNIT = "קל׳"
PROTEIN_UNIT = "ג׳ חלבון"


def cal(value: float) -> str:
    """Format a calorie amount with a thousands separator and the canonical unit."""
    return f"{value:,.0f} {CAL_UNIT}"


def protein(value: float) -> str:
    return f"{value:,.0f} {PROTEIN_UNIT}"


def macros(calories: float, protein_g: float, *, approx: bool = False) -> str:
    """One canonical "X קל׳ | Y ג׳ חלבון" line, optionally prefixed with כ- ."""
    prefix = "כ-" if approx else ""
    return f"{prefix}{cal(calories)} | {prefix}{protein(protein_g)}"


def heading(emoji: str, title: str, *, suffix: str = "") -> str:
    """A consistent bold section heading, e.g. "<b>📊 מצב היום</b> — 00:49"."""
    tail = f" — {suffix}" if suffix else ""
    return f"<b>{emoji} {title}</b>{tail}"


def local_hhmm(value: str | datetime | None) -> str:
    """Return HH:MM from an ISO string / datetime, never a raw ISO timestamp."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%H:%M")
    text = str(value)
    # Fast path for an ISO datetime like "2026-07-12T19:09:00+03:00".
    if "T" in text and len(text) >= 16:
        return text[11:16]
    try:
        return datetime.fromisoformat(text).strftime("%H:%M")
    except (TypeError, ValueError):
        return ""
