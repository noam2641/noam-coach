"""Canonical weekday helpers.

Internal convention for Noam Coach RE9:
0=Monday, 1=Tuesday, 2=Wednesday, 3=Thursday, 4=Friday, 5=Saturday, 6=Sunday.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

WEEKDAY_SCHEMA_VERSION = "monday_first_v1"
LEGACY_SUNDAY_FIRST_SCHEMA = "sunday_first_v1"

WEEKDAY_NAMES_HE: list[str] = [
    "שני",
    "שלישי",
    "רביעי",
    "חמישי",
    "שישי",
    "שבת",
    "ראשון",
]

WEEKDAY_NAME_BY_INDEX: dict[int, str] = {
    index: name for index, name in enumerate(WEEKDAY_NAMES_HE)
}


@dataclass(frozen=True)
class NormalizedWeekday:
    weekday: int | None
    schema: str
    needs_confirmation: bool = False
    reason: str | None = None


def weekday_he(index: int) -> str:
    return WEEKDAY_NAMES_HE[int(index) % 7]


def sunday_first_key(index: int) -> int:
    """Sort/iteration key that orders Monday-first indices (0=Mon..6=Sun) so
    Sunday comes first, matching the Israeli week (ראשון..שבת).

    Internal storage stays Monday-first everywhere (date.weekday()); this key
    is only for building user-facing day sequences/lists.
    """
    return (int(index) + 1) % 7


def sunday_first_order(indices: "list[int] | set[int] | tuple[int, ...]") -> list[int]:
    """Return the given Monday-first weekday indices sorted for Israeli display."""
    return sorted(indices, key=sunday_first_key)


def local_weekday(dt: datetime) -> int:
    return dt.weekday()


def sunday_first_to_monday_first(index: int) -> int:
    return (int(index) + 6) % 7


def monday_first_to_sunday_first(index: int) -> int:
    return (int(index) + 1) % 7


def normalize_weekday(value: Any, schema: Any = WEEKDAY_SCHEMA_VERSION) -> NormalizedWeekday:
    try:
        raw = int(value)
    except (TypeError, ValueError):
        return NormalizedWeekday(None, str(schema or ""), True, "invalid_weekday")
    if raw < 0 or raw > 6:
        return NormalizedWeekday(None, str(schema or ""), True, "invalid_weekday")

    schema_text = str(schema or "").strip()
    if schema_text == WEEKDAY_SCHEMA_VERSION:
        return NormalizedWeekday(raw, WEEKDAY_SCHEMA_VERSION)
    if schema_text == LEGACY_SUNDAY_FIRST_SCHEMA:
        return NormalizedWeekday(sunday_first_to_monday_first(raw), WEEKDAY_SCHEMA_VERSION)
    return NormalizedWeekday(raw, schema_text or "unknown", True, "missing_or_unknown_weekday_schema")


def with_weekday_schema(payload: dict[str, Any]) -> dict[str, Any]:
    out = dict(payload)
    out["weekday_schema"] = WEEKDAY_SCHEMA_VERSION
    return out
