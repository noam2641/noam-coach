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


def weekday_labels_he(
    indices: "list[int] | set[int] | tuple[int, ...]",
    schema: Any = WEEKDAY_SCHEMA_VERSION,
) -> list[str]:
    """Hebrew day names for weekday indices, normalized and in Israeli order.

    The single owner of "indices → ראשון, שלישי, חמישי" so every screen
    handles legacy sunday-first schemas the same way.
    """
    normalized = {
        result.weekday
        for raw in indices
        if (result := normalize_weekday(raw, schema)).weekday is not None
    }
    return [weekday_he(day) for day in sunday_first_order(normalized)]


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


# Balanced weekly templates (Monday-first indices) used to spread N training
# days across the week when the user's data doesn't already pin them down.
# Chosen to avoid back-to-back days where possible so recovery is even.
_BALANCED_TEMPLATES: dict[int, list[int]] = {
    1: [1],                    # שני
    2: [1, 4],                 # שני, שישי
    3: [6, 2, 4],              # ראשון, שלישי, חמישי
    4: [6, 1, 3, 5],           # ראשון, שני, רביעי, שישי
    5: [6, 1, 3, 4, 5],        # ראשון, שני, רביעי, חמישי, שישי
    6: [6, 0, 1, 3, 4, 5],     # ראשון..שישי
}


def propose_training_days(
    detected: "list[int] | tuple[int, ...]",
    count: int,
    *,
    schema: Any = WEEKDAY_SCHEMA_VERSION,
) -> tuple[list[int], list[int]]:
    """Propose exactly ``count`` training days (Monday-first indices).

    The user's DETECTED days (ordered strongest-first) are kept, then the list
    is topped up from a balanced weekly template so the proposal always matches
    the requested frequency — never fewer days than the user asked for. When
    there are more detected days than requested, the strongest ``count`` win.

    Returns ``(proposed_days, added_days)`` where ``proposed_days`` is in
    Israeli display order and ``added_days`` are the template days appended to
    reach ``count`` (so the UI can explain "I added X to reach N").
    """
    count = max(0, min(7, int(count)))
    if count == 0:
        return [], []

    kept: list[int] = []
    for raw in detected:
        result = normalize_weekday(raw, schema)
        if result.weekday is not None and result.weekday not in kept:
            kept.append(result.weekday)
    kept = kept[:count]

    added: list[int] = []
    if len(kept) < count:
        template = _BALANCED_TEMPLATES.get(count, _BALANCED_TEMPLATES[6])
        for day in template:
            if len(kept) + len(added) >= count:
                break
            if day not in kept and day not in added:
                added.append(day)
        # Extremely rare: template + kept still short (kept had odd extras).
        for day in range(7):
            if len(kept) + len(added) >= count:
                break
            if day not in kept and day not in added:
                added.append(day)

    proposed = sunday_first_order(kept + added)
    return proposed, added
