"""Shared explainability and decision-quality helpers.

The helpers here are intentionally deterministic and small: product surfaces can
show clear calculations, while logs/payloads can keep internal confidence and
context completeness without each handler inventing its own wording.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class CalculationLine:
    label: str
    target: int | None
    consumed: int
    planned: int
    remaining: int | None
    unit: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DecisionAudit:
    confidence: int
    data_completeness: int
    recommendation_quality: str
    missing_context: list[str] = field(default_factory=list)
    validation_events: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _clamp_percent(value: int) -> int:
    return max(0, min(100, int(value)))


def _quality_label(score: int) -> str:
    if score >= 85:
        return "high"
    if score >= 65:
        return "medium"
    return "low"


def nutrition_remaining_calculation(
    nutrition: Any,
    *,
    planned_calories: int = 0,
    planned_protein: int = 0,
) -> list[CalculationLine]:
    """Return the transparent daily-balance math.

    Planned meals are shown for trust, but consumed-only remaining stays the
    source of truth until the user confirms eating.
    """
    return [
        CalculationLine(
            label="קלוריות",
            target=nutrition.target_calories,
            consumed=int(round(float(nutrition.consumed_calories or 0))),
            planned=int(round(float(planned_calories or 0))),
            remaining=nutrition.calorie_balance,
            unit="קלוריות",
        ),
        CalculationLine(
            label="חלבון",
            target=nutrition.target_protein,
            consumed=int(round(float(nutrition.consumed_protein or 0))),
            planned=int(round(float(planned_protein or 0))),
            remaining=nutrition.protein_balance,
            unit="גרם",
        ),
    ]


def format_remaining_calculation(lines: list[CalculationLine]) -> list[str]:
    rendered: list[str] = []
    for line in lines:
        target = line.target if line.target is not None else "לא ידוע"
        remaining = line.remaining if line.remaining is not None else "לא ידוע"
        rendered.extend(
            [
                f"<b>{line.label}</b>",
                f"• יעד: {target} {line.unit}",
                f"• נאכל: {line.consumed} {line.unit}",
                f"• מתוכנן: {line.planned} {line.unit}",
                f"• נשאר: {remaining} {line.unit}",
            ]
        )
    return rendered


def build_next_meal_decision_audit(
    context: Any,
    *,
    option_count: int,
    validation_events: list[str] | None = None,
) -> DecisionAudit:
    missing: list[str] = []
    notes: list[str] = []
    events = list(validation_events or [])
    nutrition = context.nutrition

    if nutrition.goal_status in {"default", "missing", "unknown"}:
        missing.append("confirmed_goal")
    if context.hours_until_bedtime is None:
        missing.append("sleep_time")
    if context.workout_phase.value == "workout_status_unknown":
        missing.append("workout_status")
    if option_count <= 0:
        missing.append("valid_meal_option")
    if context.restrictions:
        notes.append("dietary_restrictions_validated")

    completeness = _clamp_percent(100 - (len(missing) * 12))
    confidence = _clamp_percent(completeness - (len(events) * 4) + (5 if option_count >= 2 else 0))
    return DecisionAudit(
        confidence=confidence,
        data_completeness=completeness,
        recommendation_quality=_quality_label(min(confidence, completeness)),
        missing_context=missing,
        validation_events=events,
        notes=notes,
    )


def assess_nutrition_context_completeness(context: Any) -> dict[str, Any]:
    """Compact metadata for AI payloads and logs; not shown by default."""
    missing: list[str] = []
    warnings = list(getattr(context, "data_warnings", []) or [])

    if getattr(context, "calorie_target", None) is None or getattr(context, "protein_target", None) is None:
        missing.append("confirmed_goal")
    if getattr(context, "hours_until_sleep", None) is None:
        missing.append("sleep_time")
    if getattr(context, "workout_status", None) in {None, "unknown"}:
        missing.append("workout_context")
    if not getattr(context, "allergies", []):
        warnings.append("allergies_not_reported")

    uncaptured = list(getattr(context, "uncaptured_fields", ()) or ())
    score = _clamp_percent(100 - len(missing) * 15 - len(uncaptured) * 3)
    return {
        "data_completeness": score,
        "recommendation_quality": _quality_label(score),
        "missing_context": missing,
        "uncaptured_fields": uncaptured,
        "warnings": warnings,
    }
