"""Centralized precedence policy for current-day decisions.

REC-ARCH-01. Every domain that reasons about "what's true right now" was at
risk of re-deriving its own version of this ordering ad hoc (and next_meal.py
essentially had one baked into its phase resolution already). This module
gives that ordering ONE place to live so it is stated once, not reimplemented
per-service.

    ACTUAL CURRENT-DAY EVENT
    > EXPLICIT CURRENT-DAY USER INPUT
    > CURRENT PLAN
    > CONFIRMED USER KNOWLEDGE
    > HIGH-CONFIDENCE ROUTINE
    > WEAK INFERENCE
    > DEFAULT

Concretely for workout state (see ``noam_coach/services/user_state.py``):
a routine says "usually 19:00"; today's plan says "20:00" -> the plan wins
for today's decisions (rank PLAN > ROUTINE). But once the workout actually
starts (say at 20:17), the actual event outranks the plan -> the system must
stop treating "20:00, not yet started" as current truth.

This module does not re-run the resolution itself (that stays in
``user_state.resolve_workout_state``, which already applies this order) — it
exposes the ranking as data so callers/tests can reason about *why* one
signal should win over another without hardcoding magic string comparisons,
and so any FUTURE resolver is written against the same ranks instead of
inventing its own.
"""

from __future__ import annotations

from enum import IntEnum


class PrecedenceRank(IntEnum):
    """Higher value = wins. Mirrors the policy docstring above."""

    DEFAULT = 0
    WEAK_INFERENCE = 1
    HIGH_CONFIDENCE_ROUTINE = 2
    CONFIRMED_USER_KNOWLEDGE = 3
    CURRENT_PLAN = 4
    EXPLICIT_CURRENT_DAY_INPUT = 5
    ACTUAL_CURRENT_DAY_EVENT = 6


# Maps every ``WorkoutState.source`` value (see user_state.py) to its rank in
# the shared precedence policy. Kept as an explicit table (not inferred from
# code order) so the policy is auditable independent of resolver internals.
WORKOUT_SOURCE_RANK: dict[str, PrecedenceRank] = {
    "active_session": PrecedenceRank.ACTUAL_CURRENT_DAY_EVENT,
    "completed_session": PrecedenceRank.ACTUAL_CURRENT_DAY_EVENT,
    "session_status": PrecedenceRank.ACTUAL_CURRENT_DAY_EVENT,
    "user_clarification": PrecedenceRank.EXPLICIT_CURRENT_DAY_INPUT,
    "active_workout_plan": PrecedenceRank.CURRENT_PLAN,
    "routine_pattern": PrecedenceRank.HIGH_CONFIDENCE_ROUTINE,
    "no_workout_evidence": PrecedenceRank.DEFAULT,
}


def workout_source_rank(source: str) -> PrecedenceRank:
    """Rank of a ``WorkoutState.source`` string in the shared precedence policy.

    Unknown sources rank as WEAK_INFERENCE (better than DEFAULT, worse than
    anything named) so a typo'd/new source never silently outranks a real
    plan or actual event, but also never disappears below "no evidence".
    """
    return WORKOUT_SOURCE_RANK.get(source, PrecedenceRank.WEAK_INFERENCE)


def higher_precedence_source(a: str, b: str) -> str:
    """Return whichever of two ``WorkoutState.source`` values outranks the other.

    Ties keep ``a`` (stable — callers pass the currently-held value as ``a``
    when comparing against a freshly-resolved candidate ``b``, so "no change"
    is the tie-break default rather than silently flipping on equal rank).
    """
    return a if workout_source_rank(a) >= workout_source_rank(b) else b
