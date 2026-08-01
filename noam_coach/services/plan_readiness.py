"""Readiness gaps, classified by what they cost (A10).

`check_plan_readiness` returned a flat list of Hebrew display strings, and its
caller treated any non-empty list as a hard block. So a legacy user missing one
answer got silence instead of a plan — the defect A10 exists to fix.

The severity axis needed to do better **already existed** and was being
discarded: `questions.Question` carries `safety`, `plan_impact`, `urgency`,
`uncertainty` and `burden`, each 0-5. This module propagates those scores
instead of flattening them.

One thing genuinely did not exist, and measurement proved it. The claim that
`client_training_profile_from_facts` already behaves conservatively when
`training_limitations` is absent is **false**: for a user with no `active_pain`
and no `medical_avoidance` — exactly the population A10 serves — the fallback
produces `""`, which flows to `injuries=()`, `pain_areas=()`,
`movement_limitations=()` and `medical_flags=()`. `adapt_exercises` then receives
no constraints and loads every joint freely. A plan built under *unknown*
limitations was byte-identical in behaviour to one built under *known-none*.

`SAFETY_UNKNOWN` is that missing distinction, and it is the only new mechanism
here. Everything else extends what exists: `KIND_GAP` already models "asked and
not answered", `approvals` already stores a keyed pending decision, and A9's
`plan_mutations` already owns activation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import user_model
from config import LOGGER

# ---------------------------------------------------------------------------
# Classes. Ordered by how much they cost the user, worst first.
# ---------------------------------------------------------------------------
#: Cannot be activated at all: an input without which no plan can pass the
#: readiness gate, so building one would produce something certain to be
#: rejected.
CLASS_BLOCKING_INTEGRITY = "blocking_integrity"
#: A plan is possible, but it was built without safety adaptation. Requires
#: disclosure AND explicit confirmation before it may be activated.
CLASS_DEGRADED_SAFETY = "degraded_safety"
#: A plan is possible and safe; it is merely less tailored. Disclosed, then
#: activated normally.
CLASS_DEGRADED_PERSONALIZATION = "degraded_personalization"
#: Absent changes nothing about whether or how a plan is built.
CLASS_INFORMATIONAL = "informational"

#: Facts whose absence prevents ACTIVATION, not construction. Both are in
#: `READINESS_PROFILES["workout"].required`, so `_require_readiness` refuses
#: without them regardless of any builder default. Measured: a user with no
#: facts gets `ready=False` with both listed as missing.
_BLOCKING_INTEGRITY_FACTS = frozenset({"weekly_availability", "training_days_per_week"})

#: The one fact that changes what is SAFE rather than what is tailored.
#: `training_limitations` is the sole member of `SAFETY_QUESTIONS` and the only
#: workout fact scoring `safety=5`.
_DEGRADED_SAFETY_FACTS = frozenset({"training_limitations"})


def classify_gap(fact_key: str) -> str:
    """Which class a missing fact falls into.

    Driven by the scores `questions.Question` already carries wherever
    possible, with the two structural facts named explicitly because their cost
    is about the activation gate rather than about safety or tailoring.
    """
    if fact_key in _BLOCKING_INTEGRITY_FACTS:
        return CLASS_BLOCKING_INTEGRITY
    if fact_key in _DEGRADED_SAFETY_FACTS:
        return CLASS_DEGRADED_SAFETY

    spec = user_model.FACT_REGISTRY.get(fact_key)
    profile = user_model.READINESS_PROFILES.get("workout")
    if profile is not None and fact_key in profile.optional:
        return CLASS_INFORMATIONAL
    if spec is None:
        # Unknown key: treat as informational rather than inventing a block.
        # A fact nothing declares cannot be one the plan depends on.
        return CLASS_INFORMATIONAL
    return CLASS_DEGRADED_PERSONALIZATION


@dataclass(frozen=True)
class ReadinessAssessment:
    """What is missing, and what that means for building a plan.

    Replaces a flat `list[str]` whose only expressible answer was "block".
    """

    blocking: tuple[str, ...] = ()
    degraded_safety: tuple[str, ...] = ()
    degraded_personalization: tuple[str, ...] = ()
    informational: tuple[str, ...] = field(default_factory=tuple)

    @property
    def can_build(self) -> bool:
        """False only when an input is missing that activation would refuse."""
        return not self.blocking

    @property
    def safety_unknown(self) -> bool:
        """True when the plan must be built WITHOUT safety adaptation.

        The distinction this exists to carry: not "the user has no limitations"
        but "we do not know whether they do".
        """
        return bool(self.degraded_safety)

    @property
    def needs_confirmation(self) -> bool:
        """Safety-unknown plans are never activated without a deliberate tap."""
        return self.safety_unknown

    @property
    def is_degraded(self) -> bool:
        return bool(self.degraded_safety or self.degraded_personalization)

    def gap_count(self) -> int:
        """Bounded scalar for audit. A list would be dropped by the allowlist."""
        return len(self.degraded_safety) + len(self.degraded_personalization)


async def assess_plan_readiness(db: Any, user_id: int) -> ReadinessAssessment:
    """Classify every missing workout fact rather than flattening to a block.

    Reuses `compute_readiness`, which already knows the required set, the
    freshness rules and the `KIND_GAP` semantics. This adds only the
    classification the caller needs to decide between blocking, degrading and
    asking for confirmation.
    """
    blocking: list[str] = []
    degraded_safety: list[str] = []
    degraded_personalization: list[str] = []
    informational: list[str] = []

    try:
        readiness = await user_model.compute_readiness(db, user_id, "workout")
    except Exception:
        LOGGER.exception("plan_readiness_assessment_failed user_id=%s", user_id)
        # Fail closed on the safety axis: an unreadable readiness state is not
        # evidence that the user has no limitations.
        return ReadinessAssessment(degraded_safety=("training_limitations",))

    for fact_key in sorted(readiness.get("missing") or []):
        bucket = classify_gap(str(fact_key))
        if bucket == CLASS_BLOCKING_INTEGRITY:
            blocking.append(str(fact_key))
        elif bucket == CLASS_DEGRADED_SAFETY:
            degraded_safety.append(str(fact_key))
        elif bucket == CLASS_DEGRADED_PERSONALIZATION:
            degraded_personalization.append(str(fact_key))
        else:
            informational.append(str(fact_key))

    return ReadinessAssessment(
        blocking=tuple(blocking),
        degraded_safety=tuple(degraded_safety),
        degraded_personalization=tuple(degraded_personalization),
        informational=tuple(informational),
    )


# ---------------------------------------------------------------------------
# The conservative path that did not exist
# ---------------------------------------------------------------------------
#: Sentinel written into the training profile when limitations are UNKNOWN.
#:
#: Measured before adding this: with no `training_limitations`, no `active_pain`
#: and no `medical_avoidance`, `client_training_profile_from_facts` yields empty
#: tuples across every limitation field -- indistinguishable from a user who
#: answered "none". So absence silently meant "no limitations", and every joint
#: was loaded freely.
#:
#: This is the one NEW mechanism in A10. Its evidence is that measurement: no
#: existing path produces caution from absence.
SAFETY_UNKNOWN = "__safety_unknown__"


def is_safety_unknown(limitations: Any) -> bool:
    """True when the limitation value marks an unknown rather than a stated one."""
    if isinstance(limitations, str):
        return SAFETY_UNKNOWN in limitations
    if isinstance(limitations, dict):
        return SAFETY_UNKNOWN in str(limitations.get("location") or "")
    return False


def conservative_limitations_value() -> str:
    """The value to pass when limitations are unknown.

    Deliberately a sentinel rather than a guessed body region. Inventing
    "assume knee pain" would be a different wrong answer, not a safer one --
    it would suppress exercises the user may need and imply knowledge we do not
    have. The sentinel carries the *uncertainty* forward so consumers can act
    on it explicitly instead of inferring safety from an empty set.
    """
    return SAFETY_UNKNOWN


__all__ = [
    "CLASS_BLOCKING_INTEGRITY",
    "CLASS_DEGRADED_PERSONALIZATION",
    "CLASS_DEGRADED_SAFETY",
    "CLASS_INFORMATIONAL",
    "ReadinessAssessment",
    "SAFETY_UNKNOWN",
    "assess_plan_readiness",
    "classify_gap",
    "conservative_limitations_value",
    "is_safety_unknown",
]
