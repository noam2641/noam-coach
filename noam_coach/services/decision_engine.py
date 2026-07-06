"""Central Decision Engine (RE9-X1, RE9-X2, Coach Reasoning Layer).

A thin, deterministic orchestration layer over the explainability audit Codex
already built for next-meal. It gives every recommendation flow one place to:

- produce a `DecisionAudit` (confidence / data_completeness / quality / missing
  context) — internal by default (RE9-X1), surfaced only via "איך חושב?";
- run a context-completeness gate *before* an AI request, so the system knows
  when a recommendation is based on partial information (RE9-X2) and either asks
  for the missing piece or tags the output honestly.

No AI dependency — rules over context, matching the existing explainability
design. Domains delegate to the specialist that owns their data.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from noam_coach.services.explainability import (
    DecisionAudit,
    build_next_meal_decision_audit,
)

# Critical context each domain needs before trusting an AI recommendation.
CRITICAL_CONTEXT: dict[str, tuple[str, ...]] = {
    "nutrition": ("confirmed_goal", "sleep_time", "workout_status", "allergies_known"),
    "workout": ("workout_status", "availability", "readiness"),
}


@dataclass(frozen=True)
class CompletenessGate:
    """Result of checking whether we have enough context to recommend."""

    complete: bool
    missing: list[str]
    data_completeness: int
    based_on_partial_info: bool

    def tag(self) -> str:
        """Short, honest tag for the recommendation (empty when complete)."""
        if self.complete:
            return ""
        return "ההמלצה מבוססת על מידע חלקי — כדאי להשלים פרטים כדי לדייק."


def evaluate_next_meal_decision(
    context: Any,
    *,
    option_count: int,
    validation_events: list[str] | None = None,
) -> DecisionAudit:
    """Nutrition/next-meal audit — wraps the existing builder unchanged."""
    return build_next_meal_decision_audit(
        context,
        option_count=option_count,
        validation_events=validation_events,
    )


def evaluate_workout_decision(context_quality: dict[str, Any]) -> DecisionAudit:
    """Workout audit derived from the unified workout context-quality metadata."""
    completeness = int(context_quality.get("data_completeness", 0))
    missing = list(context_quality.get("missing_context", []))
    quality = str(context_quality.get("recommendation_quality", "low"))
    return DecisionAudit(
        confidence=max(0, completeness - len(missing) * 5),
        data_completeness=completeness,
        recommendation_quality=quality,
        missing_context=missing,
    )


def context_completeness_gate(
    domain: str,
    context_quality: dict[str, Any],
) -> CompletenessGate:
    """RE9-X2: decide if we have enough context before an AI request.

    ``context_quality`` is the metadata block produced by the Prompt Builder /
    explainability assessment. We treat any missing *critical* field as partial
    information rather than silently guessing.
    """
    critical = set(CRITICAL_CONTEXT.get(domain, ()))
    reported_missing = set(context_quality.get("missing_context", []))
    completeness = int(context_quality.get("data_completeness", 0))
    missing_critical = sorted(reported_missing & critical) if critical else sorted(reported_missing)
    complete = not missing_critical and completeness >= 60
    return CompletenessGate(
        complete=complete,
        missing=missing_critical,
        data_completeness=completeness,
        based_on_partial_info=not complete,
    )
