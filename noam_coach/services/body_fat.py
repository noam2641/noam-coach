"""Centralized body-fat percentage normalizer.

REC-PROGRAM-04-08: Body-fat values arrive from multiple sources with
different unit conventions.  This module normalizes them to a canonical
percentage, tracks confidence, and flags ambiguous or implausible values
instead of silently guessing.

Supported source units
----------------------
fraction    0.20 means 20%
percent     20   means 20%
apple_health_pct   Apple Health stores as decimal fraction (0–1)
apple_health_percent   Some exports use whole-number percent

When the unit is unknown and the value is ambiguous, the result is
flagged as ``ambiguous`` rather than silently converted.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

# Plausible body-fat range for living humans.
MIN_PLAUSIBLE_PCT = 3.0
MAX_PLAUSIBLE_PCT = 65.0


@dataclass
class BodyFatResult:
    """Structured result from body-fat normalization."""

    normalized_pct: float | None
    source_unit: str  # "fraction", "percent", "unknown"
    confidence: float  # 0.0–1.0
    status: str  # "valid", "inferred_unit", "ambiguous", "implausible", "missing"
    warning: str  # empty when no warning


def normalize_body_fat(
    raw_value: Any,
    *,
    source_unit: str | None = None,
    source_type: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> BodyFatResult:
    """Normalize a raw body-fat value to a percentage.

    Parameters
    ----------
    raw_value:
        The raw value.  May be float, int, str, None, NaN, Inf, dict, etc.
    source_unit:
        Explicit unit hint: ``"fraction"``, ``"percent"``,
        ``"apple_health_pct"``, ``"apple_health_percent"``, or ``None``.
    source_type:
        Data source identifier (e.g. ``"apple_health"``, ``"user_report"``).
    metadata:
        Optional dict with extra context (unused for now, reserved).

    Returns
    -------
    BodyFatResult
        Structured result with normalized value, confidence, and status.
    """
    # --- Handle missing / non-numeric input ---
    if raw_value is None:
        return BodyFatResult(
            normalized_pct=None,
            source_unit=source_unit or "unknown",
            confidence=0.0,
            status="missing",
            warning="",
        )

    # Extract numeric value
    try:
        num = float(raw_value)
    except (TypeError, ValueError):
        return BodyFatResult(
            normalized_pct=None,
            source_unit=source_unit or "unknown",
            confidence=0.0,
            status="missing",
            warning="ערך לא מספרי",
        )

    # --- Handle NaN / Inf ---
    if not math.isfinite(num):
        return BodyFatResult(
            normalized_pct=None,
            source_unit=source_unit or "unknown",
            confidence=0.0,
            status="implausible",
            warning="ערך לא תקין (NaN/Infinity)",
        )

    # --- Handle negative ---
    if num < 0:
        return BodyFatResult(
            normalized_pct=None,
            source_unit=source_unit or "unknown",
            confidence=0.0,
            status="implausible",
            warning="ערך שלילי",
        )

    # --- Normalize based on source unit ---
    resolved_unit = _resolve_unit(source_unit, source_type)

    if resolved_unit == "fraction":
        # Explicit fraction: 0.20 → 20%
        pct = num * 100
        confidence = 0.95
        status = "valid"
    elif resolved_unit == "percent":
        # Explicit percent: 20 → 20%
        pct = num
        confidence = 0.95
        status = "valid"
    else:
        # Unknown unit — must infer
        pct, confidence, status = _infer_unit(num)

    # --- Plausibility check ---
    if pct is not None and (pct < MIN_PLAUSIBLE_PCT or pct > MAX_PLAUSIBLE_PCT):
        return BodyFatResult(
            normalized_pct=pct,
            source_unit=resolved_unit,
            confidence=min(confidence, 0.2),
            status="implausible",
            warning=f"ערך {pct:.1f}% מחוץ לטווח סביר ({MIN_PLAUSIBLE_PCT}-{MAX_PLAUSIBLE_PCT}%)",
        )

    return BodyFatResult(
        normalized_pct=round(pct, 1) if pct is not None else None,
        source_unit=resolved_unit,
        confidence=confidence,
        status=status,
        warning="",
    )


def _resolve_unit(source_unit: str | None, source_type: str | None) -> str:
    """Map explicit unit hints to canonical unit names."""
    if source_unit in ("fraction", "percent"):
        return source_unit
    if source_unit == "apple_health_pct":
        return "fraction"
    if source_unit == "apple_health_percent":
        return "percent"
    # Infer from source_type when unit is not specified
    if source_type == "apple_health":
        return "fraction"  # Apple Health BodyFatPercentage is 0–1
    return "unknown"


def _infer_unit(num: float) -> tuple[float | None, float, str]:
    """Infer the unit of a body-fat value when the source unit is unknown.

    Returns (normalized_pct, confidence, status).

    Decision rules:
    - 0 exactly: ambiguous (could be 0% or missing)
    - 0 < num < 1: likely fraction → multiply by 100 (inferred_unit)
    - 1.0 exactly: ambiguous (could be 1% or 100% fraction)
    - 1 < num <= MAX_PLAUSIBLE_PCT: likely percent → use as-is (inferred_unit)
    - num > MAX_PLAUSIBLE_PCT: implausible
    """
    if num == 0:
        return None, 0.0, "ambiguous"

    if num == 1.0:
        # Ambiguous: could be 1% body fat (extremely lean) or
        # 100% as a fraction (impossible).  Flag as ambiguous.
        return None, 0.0, "ambiguous"

    if 0 < num < 1.0:
        # Very likely a fraction
        pct = num * 100
        return pct, 0.8, "inferred_unit"

    if MIN_PLAUSIBLE_PCT <= num <= MAX_PLAUSIBLE_PCT:
        # Likely already a percentage
        return num, 0.8, "inferred_unit"

    if 1.0 < num < MIN_PLAUSIBLE_PCT:
        # Between 1 and 3: ambiguous — could be a very low percentage
        # or a fraction > 1 (invalid).  Flag as ambiguous.
        return None, 0.0, "ambiguous"

    # Above MAX_PLAUSIBLE_PCT
    return num, 0.2, "implausible"


def display_body_fat(result: BodyFatResult) -> str:
    """Return the user-facing Hebrew display string.

    Shows the normalized value when reliable, or ``דורש אימות`` when
    ambiguous or implausible.
    """
    if result.status in ("missing",):
        return "לא צוין"
    if result.status in ("ambiguous", "implausible"):
        return "דורש אימות"
    if result.normalized_pct is None:
        return "לא צוין"
    return f"{result.normalized_pct:.1f}%"
