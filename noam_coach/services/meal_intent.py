"""Meal intents: the nutritional/behavioral role of each eating opportunity
before the AI composes food for it (TASK-8).

A ``MealIntent`` is built by CODE, from the same workout/remaining-budget
primitives ``next_meal.py`` already uses (``WorkoutPhase``,
``build_workout_nutrition_context``, ``allocate_next_meal_budget``), so the
daily menu and "what should I eat now" share one calorie-allocation engine
instead of two independently-drifting ones.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from config import TZ
from noam_coach.services.learned_foods import LearnedFood, meal_slot_for_hour
from noam_coach.services.next_meal import (
    MealBudget,
    WorkoutNutritionContext,
    WorkoutPhase,
    build_workout_nutrition_context,
)
from noam_coach.services.preference_profile import NutritionPreferenceProfile

# Default slot roles for a standard day. Breakfast is optional by product
# decision (see recommendations.morning_menu's system prompt) — the pipeline
# may drop it when the user's routine shows it is usually skipped.
_DEFAULT_SLOT_ROLES: tuple[tuple[str, str, int], ...] = (
    ("breakfast", "ארוחת בוקר", 8),
    ("lunch", "ארוחת צהריים", 13),
    ("dinner", "ארוחת ערב", 19),
)


@dataclass(frozen=True)
class MealIntent:
    """One eating opportunity's role, before any food is chosen for it."""

    slot: str
    role_label: str
    approx_hour: int
    calorie_target: float
    protein_target: float
    calorie_min: float
    calorie_max: float
    workout_relationship: str  # e.g. "pre_workout", "post_workout", "none"
    digestion_requirement: str  # "light" | "normal" | "hearty"
    familiar_foods: list[LearnedFood] = field(default_factory=list)
    recently_consumed_foods: list[str] = field(default_factory=list)
    prep_constraint: str | None = None

    def slot_affinity_context(self) -> dict[str, Any]:
        return {
            "slot": self.slot,
            "familiar_food_names": [food.display_name for food in self.familiar_foods],
        }


def _digestion_requirement(phase: WorkoutPhase) -> str:
    if phase in {WorkoutPhase.PRE_WORKOUT_IMMEDIATE, WorkoutPhase.DURING_WORKOUT}:
        return "light"
    if phase in {WorkoutPhase.POST_WORKOUT_IMMEDIATE, WorkoutPhase.POST_WORKOUT_LATER}:
        return "hearty"
    return "normal"


def _workout_relationship(phase: WorkoutPhase) -> str:
    if phase in {WorkoutPhase.PRE_WORKOUT_IMMEDIATE, WorkoutPhase.PRE_WORKOUT_NEAR, WorkoutPhase.PRE_WORKOUT_EARLY}:
        return "pre_workout"
    if phase in {WorkoutPhase.POST_WORKOUT_IMMEDIATE, WorkoutPhase.POST_WORKOUT_LATER}:
        return "post_workout"
    if phase == WorkoutPhase.DURING_WORKOUT:
        return "during_workout"
    return "none"


def _slot_plan(
    workout_context: WorkoutNutritionContext,
    profile: NutritionPreferenceProfile,
) -> list[tuple[str, str, int]]:
    """Decide which slots exist today. Breakfast is dropped when the user's
    learned routine shows it is usually skipped (no breakfast-slot foods with
    at least weak evidence) — TASK-7/8: "optimize for real-life adherence".
    """
    has_breakfast_history = any(
        food.slot_counts.get("breakfast", 0) > 0 for food in profile.learned_foods
    )
    no_history_at_all = not profile.learned_foods
    slots = list(_DEFAULT_SLOT_ROLES)
    if not has_breakfast_history and not no_history_at_all:
        slots = [s for s in slots if s[0] != "breakfast"]
    # Insert a workout-adjacent meal if a workout is meaningfully close and no
    # existing slot already sits in that window.
    if workout_context.minutes_until_workout is not None and workout_context.minutes_until_workout <= 180:
        workout_hour = (datetime.now(TZ).astimezone(TZ).hour + workout_context.minutes_until_workout // 60) % 24
        if not any(abs(hour - workout_hour) <= 1 for _slot, _label, hour in slots):
            slots.append(("pre_workout", "לפני האימון", workout_hour))
    slots.sort(key=lambda item: item[2])
    return slots


def _familiar_for_slot(profile: NutritionPreferenceProfile, slot: str) -> list[LearnedFood]:
    candidates = [
        food for food in profile.learned_foods
        if food.slot_counts.get(slot, 0) > 0
    ]
    candidates.sort(key=lambda food: food.slot_relevance(slot), reverse=True)
    return candidates[:4]


async def build_meal_intents(
    db: Any,
    user_id: int,
    profile: NutritionPreferenceProfile,
    *,
    calorie_target: float | None,
    protein_target: float | None,
    now: datetime | None = None,
) -> list[MealIntent]:
    """Build the day's meal intents from workout/budget primitives + profile.

    Reuses ``build_workout_nutrition_context``/``allocate_next_meal_budget``
    (Task 8 requirement) instead of a second calorie-allocation engine.
    """
    workout_context = await build_workout_nutrition_context(db, user_id, now=now)
    slots = _slot_plan(workout_context, profile)
    n_slots = max(1, len(slots))
    total_calories = float(calorie_target or 0)
    total_protein = float(protein_target or 0)

    intents: list[MealIntent] = []
    for slot, label, hour in slots:
        phase = workout_context.workout_phase if _slot_is_workout_adjacent(slot, hour, workout_context) else WorkoutPhase.REST_DAY
        share = 1.0 / n_slots
        target_cal = total_calories * share
        target_protein = total_protein * share
        intents.append(
            MealIntent(
                slot=slot,
                role_label=label,
                approx_hour=hour,
                calorie_target=round(target_cal, 0),
                protein_target=round(target_protein, 0),
                calorie_min=round(target_cal * 0.75, 0),
                calorie_max=round(target_cal * 1.25, 0),
                workout_relationship=_workout_relationship(phase),
                digestion_requirement=_digestion_requirement(phase),
                familiar_foods=_familiar_for_slot(profile, meal_slot_for_hour(hour)),
                recently_consumed_foods=list(profile.recently_rejected_meals),
                prep_constraint=_prep_constraint(profile),
            )
        )
    return intents


def _slot_is_workout_adjacent(slot: str, hour: int, workout_context: WorkoutNutritionContext) -> bool:
    if slot == "pre_workout":
        return True
    if workout_context.minutes_until_workout is None:
        return False
    return workout_context.minutes_until_workout <= 180


def _prep_constraint(profile: NutritionPreferenceProfile) -> str | None:
    if not profile.food_environment:
        return None
    from noam_coach.services.food_environment import personal_fit_signals

    signals = personal_fit_signals(profile.food_environment)
    if signals.get("quick_or_limited_access"):
        return "quick"
    if signals.get("low_cooking"):
        return "no_cook"
    return None


def budget_from_intent(intent: MealIntent) -> MealBudget:
    """Adapt a MealIntent's target window into a next_meal-style MealBudget,
    reusing ``allocate_next_meal_budget``'s bounds shape for callers that want
    a single-meal-shaped budget object rather than the raw intent fields."""
    return MealBudget(
        calories_min=int(intent.calorie_min),
        calories_max=int(intent.calorie_max),
        protein_min=int(intent.protein_target * 0.7),
        protein_max=int(intent.protein_target * 1.4),
        meal_size=intent.digestion_requirement,
        rationale=f"תקציב עבור {intent.role_label}",
        policy="normal",
        remaining_calories=None,
        allows_overage=False,
    )
