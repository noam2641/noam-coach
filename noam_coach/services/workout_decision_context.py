"""Phase 4 — the workout decision context, projected from ``SharedUserState``.

This is what a workout-side decision (today: the pre-workout meal-timing
recommendation in ``meal_timing.py``; tomorrow: fuller session-runtime
adaptation, out of scope for this pass) should read instead of re-resolving
"what's happening with today's workout" itself.

Kept deliberately small: it does not duplicate ``ClientTrainingProfile``
(stable profile facts — age/equipment/injuries) or ``RoutineProfile``
(learned patterns) — it is the CURRENT-DAY workout view only, sourced from the
one shared resolution in ``user_state.resolve_workout_state``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from noam_coach.services.user_state import SharedUserState, WorkoutPhase, WorkoutState


@dataclass(frozen=True)
class WorkoutDecisionContext:
    """The workout-side projection of the shared state.

    ``now`` is the SAME instant ``SharedUserState.now`` used to build any
    sibling nutrition projection in the same decision flow (see
    ``nutrition_context.build_nutrition_context``, which now sources its
    workout view from this same shared state rather than recomputing it).
    """

    user_id: int
    now: datetime
    workout: WorkoutState
    session_plan: dict[str, Any] | None = None

    @property
    def phase(self) -> WorkoutPhase:
        return self.workout.phase

    @property
    def is_upcoming(self) -> bool:
        return self.workout.is_future_plan

    @property
    def is_in_progress(self) -> bool:
        return self.workout.phase == WorkoutPhase.DURING_WORKOUT

    @property
    def is_completed(self) -> bool:
        return self.workout.phase in {
            WorkoutPhase.POST_WORKOUT_IMMEDIATE,
            WorkoutPhase.POST_WORKOUT_LATER,
            WorkoutPhase.WORKOUT_COMPLETED_EARLIER,
        }


def project_workout_decision_context(
    state: SharedUserState,
    *,
    session_plan: dict[str, Any] | None = None,
) -> WorkoutDecisionContext:
    """Project the workout-decision view from the shared snapshot.

    Pure/sync by design: it must never re-query the DB or re-derive workout
    state — that already happened once in ``build_shared_state``. Passing the
    same ``state`` into both this and
    ``nutrition_context`` projectors guarantees they agree on "is the workout
    planned, active, or done" because both read ``state.workout`` verbatim.
    """
    return WorkoutDecisionContext(
        user_id=state.user_id,
        now=state.now,
        workout=state.workout,
        session_plan=session_plan,
    )
