"""RE10-13 / D9 / D10 / E2 — remaining-slot budget allocator.

Verifies the same hard-cap principle used by allocate_next_meal_budget:
the sum of every slot returned must never exceed the remaining daily
calorie balance, the trailing night slot is always small and protein-dense,
and workout-phase labels are applied without mutating the underlying plan.
"""

from __future__ import annotations

from noam_coach.services.next_meal import (
    NutritionTotals,
    WorkoutNutritionContext,
    WorkoutPhase,
    build_remaining_slot_allocations,
)


def _context(
    *,
    calorie_balance: int | None,
    protein_balance: int | None = 100,
    meals_remaining_estimate: int = 3,
    workout_phase: WorkoutPhase = WorkoutPhase.REST_DAY,
) -> WorkoutNutritionContext:
    nutrition = NutritionTotals(
        target_calories=2100,
        target_protein=190,
        consumed_calories=2100 - (calorie_balance or 0),
        consumed_protein=190 - (protein_balance or 0),
        calorie_balance=calorie_balance,
        protein_balance=protein_balance,
        calorie_overage=0,
        protein_overage=0,
        goal_status="active",
        goal_source="approved",
    )
    return WorkoutNutritionContext(
        user_id=1,
        local_now="2026-01-01T18:00:00+02:00",
        local_day="2026-01-01",
        nutrition=nutrition,
        workout_phase=workout_phase,
        workout_source="user_reported",
        workout_label="אימון מתוכנן",
        meals_remaining_estimate=meals_remaining_estimate,
    )


def test_allocations_never_exceed_remaining_balance() -> None:
    context = _context(calorie_balance=2100, meals_remaining_estimate=3)
    allocations = build_remaining_slot_allocations(context)
    assert sum(a.calories for a in allocations) <= 2100


def test_exact_scenario_from_user_screenshot() -> None:
    """2,100 kcal / 195g protein remaining, 3 slots -> matches the format the
    user specified: two ~635 kcal meals plus a light night meal."""
    context = _context(calorie_balance=2100, protein_balance=195, meals_remaining_estimate=3)
    allocations = build_remaining_slot_allocations(context)
    assert len(allocations) == 3
    assert allocations[-1].is_night_meal is True
    assert allocations[-1].calories <= 150
    assert sum(a.calories for a in allocations) <= 2100


def test_night_meal_is_always_protein_dense() -> None:
    context = _context(calorie_balance=1000, meals_remaining_estimate=2)
    allocations = build_remaining_slot_allocations(context)
    night = next(a for a in allocations if a.is_night_meal)
    assert night.protein >= 12
    assert night.calories <= 150


def test_single_remaining_meal_has_no_separate_night_slot() -> None:
    context = _context(calorie_balance=400, meals_remaining_estimate=1)
    allocations = build_remaining_slot_allocations(context)
    assert len(allocations) == 1
    assert sum(a.calories for a in allocations) <= 400


def test_zero_or_negative_balance_collapses_to_safe_floor() -> None:
    context = _context(calorie_balance=-50, meals_remaining_estimate=2)
    allocations = build_remaining_slot_allocations(context)
    assert sum(a.calories for a in allocations) <= 0
    assert all(a.calories == 0 for a in allocations) or len(allocations) == 1


def test_none_balance_is_treated_as_no_budget() -> None:
    context = _context(calorie_balance=None, meals_remaining_estimate=2)
    allocations = build_remaining_slot_allocations(context)
    assert sum(a.calories for a in allocations) <= 0


def test_pre_workout_phase_labels_first_slots() -> None:
    context = _context(
        calorie_balance=1500, meals_remaining_estimate=3,
        workout_phase=WorkoutPhase.PRE_WORKOUT_NEAR,
    )
    allocations = build_remaining_slot_allocations(context)
    assert allocations[0].label == "ארוחה לפני אימון"
    assert allocations[1].label == "ארוחה אחרי אימון"
    assert allocations[-1].is_night_meal is True


def test_post_workout_phase_labels_first_slot() -> None:
    context = _context(
        calorie_balance=900, meals_remaining_estimate=2,
        workout_phase=WorkoutPhase.POST_WORKOUT_IMMEDIATE,
    )
    allocations = build_remaining_slot_allocations(context)
    assert allocations[0].label == "ארוחה אחרי אימון"


def test_explicit_slot_count_overrides_context_estimate() -> None:
    context = _context(calorie_balance=1200, meals_remaining_estimate=3)
    allocations = build_remaining_slot_allocations(context, slot_count=1)
    assert len(allocations) == 1
    assert sum(a.calories for a in allocations) <= 1200
