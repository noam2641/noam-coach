"""Unified Prompt Builder for every AI call (RE9-034/036/037).

One envelope shape, one safety contract, one place that pairs a request with its
context and a completeness assessment. Domain context assembly is delegated to
the services that already own it (nutrition_context, next_meal) so this module
adds structure without duplicating data gathering.

Envelope shape (stable across domains):
    {
      "domain": "nutrition" | "workout" | "user" | ...,
      "user_request": str,
      "context": {...},            # domain payload
      "context_quality": {...},    # completeness/quality metadata
      "safety": {...},             # uniform post-generation safety contract
    }
"""

from __future__ import annotations

from typing import Any

# The single safety contract every AI output must respect. Emitted uniformly so
# there is exactly one enforcement point (RE9-033 across all AI, not just meals).
SAFETY_CONTRACT: dict[str, bool] = {
    "do_not_treat_planned_meals_as_consumed": True,
    "validate_against_allergies_after_generation": True,
    "require_output_validation": True,
    "avoid_medical_diagnosis_or_dosage_advice": True,
    "prefer_natural_household_quantities": True,
}


def build_ai_request(
    domain: str,
    *,
    context_payload: dict[str, Any],
    context_quality: dict[str, Any],
    user_request: str,
) -> dict[str, Any]:
    """Assemble the uniform AI request envelope for any domain."""
    return {
        "domain": domain,
        "user_request": user_request,
        "context": context_payload,
        "context_quality": context_quality,
        "safety": dict(SAFETY_CONTRACT),
    }


def build_nutrition_request(context: Any, user_request: str) -> dict[str, Any]:
    """Nutrition envelope. Reuses the existing nutrition context + assessment."""
    from noam_coach.services.explainability import (
        assess_nutrition_context_completeness,
    )

    return build_ai_request(
        "nutrition",
        context_payload=context.to_ai_payload(),
        context_quality=assess_nutrition_context_completeness(context),
        user_request=user_request,
    )


def build_workout_request(context: Any, user_request: str) -> dict[str, Any]:
    """Workout envelope (RE9-036).

    ``context`` is a WorkoutNutritionContext (or any object exposing the same
    workout fields). Kept deterministic and dependency-light.
    """
    payload = _workout_payload(context)
    quality = _assess_workout_completeness(context)
    return build_ai_request(
        "workout",
        context_payload=payload,
        context_quality=quality,
        user_request=user_request,
    )


def _workout_payload(context: Any) -> dict[str, Any]:
    phase = getattr(context, "workout_phase", None)
    return {
        "workout_phase": getattr(phase, "value", phase),
        "workout_label": getattr(context, "workout_label", None),
        "planned_workout_start": getattr(context, "planned_workout_start", None),
        "planned_workout_end": getattr(context, "planned_workout_end", None),
        "minutes_until_workout": getattr(context, "minutes_until_workout", None),
        "minutes_since_workout": getattr(context, "minutes_since_workout", None),
        "hours_until_bedtime": getattr(context, "hours_until_bedtime", None),
        "restrictions": list(getattr(context, "restrictions", []) or []),
    }


def _assess_workout_completeness(context: Any) -> dict[str, Any]:
    missing: list[str] = []
    phase = getattr(context, "workout_phase", None)
    phase_value = getattr(phase, "value", phase)
    if phase_value in {None, "workout_status_unknown"}:
        missing.append("workout_status")
    if getattr(context, "hours_until_bedtime", None) is None:
        missing.append("sleep_time")
    score = max(0, min(100, 100 - len(missing) * 20))
    quality = "high" if score >= 85 else "medium" if score >= 65 else "low"
    return {
        "data_completeness": score,
        "recommendation_quality": quality,
        "missing_context": missing,
    }
