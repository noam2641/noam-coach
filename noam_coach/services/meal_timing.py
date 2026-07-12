"""Phase 6 (minimal slice) — ONE concrete cross-domain decision.

"A recent large meal changes the workout timing recommendation, and nutrition
sees that recommendation instead of independently suggesting another full
meal." This module is deliberately narrow: it does not attempt fatigue
tracing, progression, or session-runtime adaptation (those are documented as
remaining architectural debt in the audit report).

Ordering (documented, not implicit): SHARED STATE -> WORKOUT DECISION
(this module) -> explicit derived decision (``MealTimingRecommendation``) ->
NUTRITION DECISION reads it. Nutrition never calls back into this module's
inputs, and this module never reads anything nutrition computed — it reads
only the shared ``ConsumedMeal``/``WorkoutState`` facts. That one-way flow is
what keeps this a straight line instead of a nutrition<->workout call cycle.

Data-quality discipline: meal time here is a logged/confirmed timestamp, not
a lab-measured ingestion time, and workout demand is derived only from
data we actually have (planned duration + planned sets + which muscle groups
the plan targets) — never a fabricated "digestion score" or invented
fiber/GI data. Where the inputs are too thin to support a decision, the
function returns ``None`` rather than guessing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from noam_coach.services.user_state import ConsumedMeal, WorkoutState

# A meal below this size is not "large" regardless of fat content — too small
# to plausibly slow a workout down.
_LARGE_MEAL_CALORIE_FLOOR = 700
# High-fat share (of calories) that meaningfully slows gastric emptying.
_HIGH_FAT_SHARE_FLOOR = 0.35
# Below this gap, a large recent meal is still "in digestion" for a demanding
# session. Above it, the body has had enough time regardless of meal size.
_SHORT_GAP_MAX_MINUTES = 150
# Session demand floor (in planned working sets) to call a session "high load".
_HIGH_LOAD_SET_FLOOR = 12
# Session duration floor (minutes) contributing to "high demand".
_HIGH_LOAD_DURATION_FLOOR = 55

# Hebrew primary-muscle labels (see exercise_plans.EXERCISE_MUSCLES /
# planning.PLANS) that count as lower-body — real data the plan already
# carries, not an invented classifier.
_LOWER_BODY_MUSCLES = {"רגליים", "שרשרת אחורית"}


@dataclass(frozen=True)
class RecentMealState:
    """What we actually know about the most recent meal, with its confidence.

    ``calories``/``protein`` come straight off the ``meals`` row. ``fat_g`` is
    optional: the schema does not guarantee fat is populated for every logged
    meal, so a missing value must stay ``None`` (never assumed 0 or "normal")
    per the data-quality discipline this brief requires.
    """

    calories: float
    fat_g: float | None
    minutes_since_eaten: int | None
    time_confidence: str  # mirrors ConsumedMeal.time_confidence

    @property
    def is_large(self) -> bool:
        return self.calories >= _LARGE_MEAL_CALORIE_FLOOR

    @property
    def fat_share(self) -> float | None:
        if self.fat_g is None or self.calories <= 0:
            return None
        fat_calories = self.fat_g * 9
        return min(1.0, fat_calories / self.calories)

    @property
    def is_high_fat(self) -> bool | None:
        share = self.fat_share
        if share is None:
            return None  # unknown, not "no" — never silently assume low-fat
        return share >= _HIGH_FAT_SHARE_FLOOR


@dataclass(frozen=True)
class WorkoutDemand:
    """Session load proxy built only from data the plan actually carries.

    Not a physiological load estimate — a simple, honest proxy from planned
    duration/sets/muscle focus. Intentionally has no "intensity score" field:
    nothing in the plan payload supports one without fabricating precision.
    """

    planned_minutes: int | None
    planned_sets: int | None
    is_lower_body_focus: bool

    @property
    def is_high_demand(self) -> bool:
        duration_high = (self.planned_minutes or 0) >= _HIGH_LOAD_DURATION_FLOOR
        sets_high = (self.planned_sets or 0) >= _HIGH_LOAD_SET_FLOOR
        # Lower-body sessions digest-compete with a full stomach more than
        # upper-body-only sessions of similar duration; count it as one signal
        # toward "high demand" alongside duration/sets, not on its own.
        signals = sum([duration_high, sets_high, self.is_lower_body_focus])
        return signals >= 2


@dataclass(frozen=True)
class UserToleranceEvidence:
    """What we know about how this user has tolerated eating-before-training.

    No production data source exists yet for this (no logged
    "did you feel OK training after eating" signal) — it is included as an
    explicit, honestly-empty extension point so a future capture flow has
    somewhere principled to attach, WITHOUT the current decision function
    silently assuming "tolerates fine" in its absence. ``sample_count == 0``
    means "no evidence", and the decision function must treat that as neutral,
    not permissive.
    """

    sample_count: int = 0
    tolerates_pre_workout_meals: bool | None = None


@dataclass(frozen=True)
class MealTimingRecommendation:
    """The one thing nutrition is allowed to read from this module.

    ``delay_minutes`` is a bounded RANGE label, never a single fabricated
    number, when confidence is anything less than high — see
    ``confidence``.
    """

    should_delay: bool
    delay_minutes_min: int | None
    delay_minutes_max: int | None
    confidence: str  # "low" | "medium" | "high"
    reason: str


def recent_meal_state_from_consumed(
    meal: ConsumedMeal | None,
    *,
    now: datetime | None = None,
) -> RecentMealState | None:
    """Build a ``RecentMealState`` from the shared state's most recent meal.

    ``now`` should be the SAME instant as the enclosing ``SharedUserState.now``
    (never a fresh ``datetime.now()`` call) so this stays consistent with
    every other projection built from that snapshot. ``minutes_since_eaten``
    is computed from ``now - meal.eaten_at`` when both are known; if
    ``meal.eaten_at`` is ``None`` (eating time was never actually captured),
    it stays honestly ``None`` — never defaulted to 0 (which would read as
    "just ate") or otherwise fabricated.

    ``fat_g`` is sourced from ``ConsumedMeal.fat`` — the ``meals`` table
    always populates this column (NOT NULL), so it is real data, not a
    schema-limitation placeholder.

    Returns ``None`` when there is no recent meal to reason about at all.
    """
    if meal is None:
        return None
    minutes_since_eaten: int | None = None
    if now is not None and meal.eaten_at is not None:
        minutes_since_eaten = max(0, int((now - meal.eaten_at).total_seconds() // 60))
    return RecentMealState(
        calories=meal.calories,
        fat_g=meal.fat,
        minutes_since_eaten=minutes_since_eaten,
        time_confidence=meal.time_confidence,
    )


def workout_demand_from_session(session: dict[str, Any] | None) -> WorkoutDemand:
    if not session:
        return WorkoutDemand(planned_minutes=None, planned_sets=None, is_lower_body_focus=False)
    minutes = session.get("minutes")
    exercises = session.get("exercises") or []
    total_sets = 0
    lower_body_sets = 0
    for exercise in exercises:
        try:
            sets = int(exercise.get("sets") or 0)
        except (TypeError, ValueError):
            sets = 0
        total_sets += sets
        if str(exercise.get("muscle") or "") in _LOWER_BODY_MUSCLES:
            lower_body_sets += sets
    is_lower_body_focus = total_sets > 0 and lower_body_sets / total_sets >= 0.4
    return WorkoutDemand(
        planned_minutes=int(minutes) if isinstance(minutes, (int, float)) else None,
        planned_sets=total_sets or None,
        is_lower_body_focus=is_lower_body_focus,
    )


def evaluate_pre_workout_meal_timing(
    *,
    workout: WorkoutState,
    recent_meal: RecentMealState | None,
    demand: WorkoutDemand,
    tolerance: UserToleranceEvidence | None = None,
    minutes_until_workout: int | None = None,
) -> MealTimingRecommendation | None:
    """Decide whether a large recent meal should push out the workout.

    Structured inputs only — never a bare "if calories > 900" shortcut. Only
    fires ahead of an upcoming (not yet started) workout: an in-progress or
    completed workout cannot be delayed, so this must be called with a
    forward-looking ``WorkoutState`` (``is_future_plan`` truthy) by the
    caller; this function still guards it defensively.

    Returns ``None`` (no recommendation, not "no delay") whenever the inputs
    are too thin to support a decision — e.g. no recent meal, or the workout
    is not a genuinely upcoming plan.
    """
    tolerance = tolerance or UserToleranceEvidence()
    if not workout.is_future_plan:
        return None
    if recent_meal is None:
        return None
    gap = minutes_until_workout if minutes_until_workout is not None else workout.minutes_until
    if gap is None:
        return None

    if not recent_meal.is_large:
        return None
    if gap > _SHORT_GAP_MAX_MINUTES:
        # Plenty of time regardless of meal size — no delay needed.
        return None
    if not demand.is_high_demand:
        # Large recent meal, but a low-demand session doesn't warrant pushing
        # the schedule — the same meal is fine ahead of a light session.
        return None

    high_fat = recent_meal.is_high_fat
    time_known = recent_meal.time_confidence == "logged"

    # Confidence reflects how much of the input chain is solid, not just the
    # meal-time confidence alone.
    if not time_known:
        confidence = "low"
    elif high_fat is None:
        confidence = "medium"
    else:
        confidence = "high"

    # Base delay window scales with how compressed the gap already is; kept
    # as a bounded range (never a fabricated single minute count) whenever
    # confidence is not high.
    if gap <= 60:
        base_min, base_max = 45, 60
    else:
        base_min, base_max = 30, 45

    if high_fat:
        base_min += 10
        base_max += 15

    reason_parts = [
        f"ארוחה גדולה יחסית (כ-{int(recent_meal.calories)} קלוריות) לפני אימון תובעני",
    ]
    if high_fat:
        reason_parts.append("עם רכיב שומן גבוה שמאט את הספיגה")
    if not time_known:
        reason_parts.append("שעת הארוחה המדויקת אינה ודאית, לכן ההמלצה שמרנית")

    if confidence == "low":
        # Not enough certainty for a numeric window — surface the concern
        # without inventing exact minutes.
        return MealTimingRecommendation(
            should_delay=True,
            delay_minutes_min=None,
            delay_minutes_max=None,
            confidence="low",
            reason="; ".join(reason_parts) + " — מומלץ להשאיר מרווח נוסף לפני האימון, בלי מספר מדויק.",
        )

    return MealTimingRecommendation(
        should_delay=True,
        delay_minutes_min=base_min,
        delay_minutes_max=base_max,
        confidence=confidence,
        reason="; ".join(reason_parts) + f" — מומלץ לדחות את האימון בכ-{base_min}-{base_max} דקות.",
    )
