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
from typing import TypeVar


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
    # An imported HealthKit workout sample is just as much an actual
    # current-day event as a bot-tracked session — see
    # ``user_state._healthkit_session_candidate``, which closes the gap
    # where ``daily_state.workout_completed_today`` knew about HealthKit
    # workouts but the shared resolver did not.
    "healthkit_session": PrecedenceRank.ACTUAL_CURRENT_DAY_EVENT,
    "user_clarification": PrecedenceRank.EXPLICIT_CURRENT_DAY_INPUT,
    "active_workout_plan": PrecedenceRank.CURRENT_PLAN,
    "routine_pattern": PrecedenceRank.HIGH_CONFIDENCE_ROUTINE,
    "no_workout_evidence": PrecedenceRank.DEFAULT,
}


class UnknownPrecedenceSourceError(ValueError):
    """Raised when a ``WorkoutState.source`` has no registered rank.

    A silent fallback rank here would let a newly-added source slot into the
    ordering at an arbitrary, unaudited position — exactly the kind of
    unnoticed policy drift this module exists to prevent. Every source must
    be registered in ``WORKOUT_SOURCE_RANK`` explicitly before it can be
    compared, so adding a new resolver branch that forgets to register its
    source string fails loudly (a test/runtime error) instead of silently
    landing at the wrong precedence.
    """


def workout_source_rank(source: str) -> PrecedenceRank:
    """Rank of a ``WorkoutState.source`` string in the shared precedence policy.

    Raises ``UnknownPrecedenceSourceError`` for any source not explicitly
    registered in ``WORKOUT_SOURCE_RANK`` — see that error's docstring for why
    a defaulted rank is not safe here.
    """
    try:
        return WORKOUT_SOURCE_RANK[source]
    except KeyError as exc:
        raise UnknownPrecedenceSourceError(
            f"Unregistered workout state source {source!r}: add it to "
            "WORKOUT_SOURCE_RANK with an explicit PrecedenceRank before it "
            "can participate in workout-state precedence decisions."
        ) from exc


def higher_precedence_source(a: str, b: str) -> str:
    """Return whichever of two ``WorkoutState.source`` values outranks the other.

    Ties keep ``a`` (stable — callers pass the currently-held value as ``a``
    when comparing against a freshly-resolved candidate ``b``, so "no change"
    is the tie-break default rather than silently flipping on equal rank).
    """
    return a if workout_source_rank(a) >= workout_source_rank(b) else b


T = TypeVar("T")


def select_highest_precedence(candidates: list[tuple[str, T]]) -> T:
    """Pick the value whose source ranks highest among a list of candidates.

    ``candidates`` is a list of ``(source, value)`` pairs in the order a
    resolver discovered them (e.g. active session, completed session,
    explicit clarification, plan, routine). This is the single place that
    decides "which layer wins" for a resolver built around this module —
    resolvers should gather every source they can find evidence for and let
    this function pick the winner, rather than hand-rolling their own
    if/elif precedence chain that can drift from the declared policy.

    Earlier entries win ties (mirrors ``higher_precedence_source``'s
    stability rule). Raises ``ValueError`` on an empty list and
    ``UnknownPrecedenceSourceError`` (via ``workout_source_rank``) if any
    candidate's source is unregistered.
    """
    if not candidates:
        raise ValueError("select_highest_precedence requires at least one candidate")
    best_source, best_value = candidates[0]
    best_rank = workout_source_rank(best_source)
    for source, value in candidates[1:]:
        rank = workout_source_rank(source)
        if rank > best_rank:
            best_source, best_value, best_rank = source, value, rank
    return best_value
