"""Meal intents: the nutritional/behavioral role of each eating opportunity
before the AI composes food for it (TASK-8).

A ``MealIntent`` is built by CODE, from the same remaining-balance allocator
``next_meal.py`` already uses for "what should I eat now"
(``build_remaining_slot_allocations``, itself built on
``build_workout_nutrition_context``'s ``NutritionTotals.calorie_balance`` /
``protein_balance``), so the daily menu and next-meal recommendations
allocate against the SAME already-consumed-aware remaining budget instead of
two independently-drifting engines. Concretely: if the user already ate
900/2200 kcal, meal intents for the rest of the day are built from the
~1300 kcal that remains, never from the original 2200 kcal target again.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from config import TZ
from noam_coach.services.learned_foods import LearnedFood, meal_slot_for_hour
from noam_coach.services.next_meal import (
    RemainingSlotAllocation,
    WorkoutNutritionContext,
    WorkoutPhase,
    build_remaining_slot_allocations,
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
    """One eating opportunity's role, before any food is chosen for it.

    ``intent_id`` is a stable identifier (e.g. "meal_0") so the AI generation
    contract and the post-generation validator/repair pipeline can refer to
    "the same" eating opportunity without re-deriving it from a generated
    meal's free-text name (TASK-8 architecture: CODE defines meal roles, AI
    only composes food content inside them).
    """

    intent_id: str
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

    def ai_payload(self) -> dict[str, Any]:
        """The generation-contract shape sent to the AI (TASK-8 Finding 2):
        CODE defines the meal opportunity and its nutritional intent; the AI
        composes food content to fit inside it, and never invents its own
        day allocation. Every field the validator later checks the candidate
        against is present here too, so there is no hidden second
        interpretation of meal roles based only on a generated meal's name."""
        return {
            "intent_id": self.intent_id,
            "slot": self.slot,
            "role_label": self.role_label,
            "approx_hour": self.approx_hour,
            "calorie_target": self.calorie_target,
            "calorie_min": self.calorie_min,
            "calorie_max": self.calorie_max,
            "protein_target": self.protein_target,
            "workout_relationship": self.workout_relationship,
            "digestion_requirement": self.digestion_requirement,
            "prep_constraint": self.prep_constraint,
            "familiar_food_names": [food.display_name for food in self.familiar_foods],
            "recently_consumed_foods": list(self.recently_consumed_foods),
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
    *,
    now: datetime,
) -> list[tuple[str, str, int]]:
    """Decide which slots exist today. Breakfast is dropped when the user's
    learned routine shows it is usually skipped (no breakfast-slot foods with
    at least weak evidence) — TASK-7/8: "optimize for real-life adherence".

    Finding 4: ``now`` must be the caller-supplied authoritative local time,
    not a fresh ``datetime.now(TZ)`` wall-clock read — otherwise slot planning
    is nondeterministic in tests and can drift from the ``now`` the rest of
    the pipeline (DailyContext, NutritionContext) already agreed on.
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
        workout_hour = (now.astimezone(TZ).hour + workout_context.minutes_until_workout // 60) % 24
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
    """Build the day's meal intents from the SAME remaining-balance allocator
    next_meal.py uses (Finding 1 fix).

    ``build_remaining_slot_allocations`` already:
      * subtracts what the user actually ate today (``NutritionTotals``
        computed from persisted consumed meals, never planned/AI-imagined
        food) from the daily target,
      * caps every slot's share to what actually remains,
      * collapses to a small protein-floor allocation when the day is
        at/over target instead of re-offering the full original budget.

    This is the same policy real-time "what should I eat now" already uses,
    so a daily menu generated after the user already logged food never
    re-proposes another full day's worth of calories on top of what they ate
    (e.g. 900/2200 kcal consumed -> intents are built from the ~1300 kcal
    that remains, not another 2200).
    """
    local_now = (now or datetime.now(TZ)).astimezone(TZ)
    workout_context = await build_workout_nutrition_context(db, user_id, now=local_now)
    slots = _slot_plan(workout_context, profile, now=local_now)
    n_slots = max(1, len(slots))

    allocations = build_remaining_slot_allocations(workout_context, slot_count=n_slots)
    # build_remaining_slot_allocations always returns >=1 allocation (it never
    # returns an empty list when count>=1); pad defensively so a mismatch in
    # count never causes an IndexError instead of a fallback share.
    remaining_cal = workout_context.nutrition.calorie_balance
    remaining_protein = max(0, workout_context.nutrition.protein_balance or 0)
    fallback_share_cal = max(0.0, float(remaining_cal or 0)) / n_slots
    fallback_share_protein = float(remaining_protein) / n_slots

    intents: list[MealIntent] = []
    for index, (slot, label, hour) in enumerate(slots):
        phase = (
            workout_context.workout_phase
            if _slot_is_workout_adjacent(slot, hour, workout_context, now=local_now)
            else WorkoutPhase.REST_DAY
        )
        allocation: RemainingSlotAllocation | None = allocations[index] if index < len(allocations) else None
        target_cal = float(allocation.calories) if allocation is not None else fallback_share_cal
        target_protein = float(allocation.protein) if allocation is not None else fallback_share_protein
        intents.append(
            MealIntent(
                intent_id=f"meal_{index}",
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


# A meal slot is only "workout-adjacent" when its own approximate hour falls
# within this many hours of the workout — not merely because SOME workout is
# within 180 minutes of the current moment. Finding 3: the previous version
# checked only `minutes_until_workout <= 180` for every non-pre_workout slot,
# so once a workout was within 3 hours of "now", breakfast/lunch/dinner were
# ALL marked workout-adjacent (wrong digestion_requirement/workout_relationship
# for meals nowhere near the workout).
_WORKOUT_ADJACENCY_WINDOW_HOURS = 2


def _slot_is_workout_adjacent(
    slot: str, hour: int, workout_context: WorkoutNutritionContext, *, now: datetime,
) -> bool:
    if slot == "pre_workout":
        return True
    if workout_context.minutes_until_workout is None:
        return False
    now_hour = now.astimezone(TZ).hour
    workout_hour = (now_hour + workout_context.minutes_until_workout // 60) % 24
    # Circular hour distance (handles wraparound across midnight).
    diff = abs(hour - workout_hour)
    distance = min(diff, 24 - diff)
    return distance <= _WORKOUT_ADJACENCY_WINDOW_HOURS


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
