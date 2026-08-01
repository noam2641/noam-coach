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

import json
from contextlib import suppress
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


# ---------------------------------------------------------------------------
# The degraded-safety proposal flow
#
# LOG-015 made an unanswered safety question block plan building on EVERY path.
# That protection is real and must not simply be deleted -- but blocking is what
# produces silence for a legacy user, which is the defect A10 exists to fix.
#
# So the block is REPLACED, not removed, by three protections that together are
# strictly more informative:
#
#   1. conservative behaviour  -- the plan is built with SAFETY_UNKNOWN, so the
#      unknown is carried forward instead of read as "no limitations";
#   2. disclosure              -- the user is told which adaptation is missing,
#      specifically, not that "some info is missing";
#   3. confirmation            -- nothing is activated without a deliberate tap.
#
# Remove any one and the user is worse off than under LOG-015. A regression test
# asserts all three, so this is a trade with a guard, not an erosion.
#
# Everything here reuses existing machinery: `approvals` for the pending
# decision (with its `WHERE status='pending'` idempotency), the existing
# `create_approval`/`decide_approval` pair, and A9's `plan_mutations` for the
# activation itself. No new writer, no new table, no A4 allowlist change.
# ---------------------------------------------------------------------------

#: Approval kind for a plan built without safety information.
APPROVAL_KIND_DEGRADED_PLAN = "degraded_workout_plan"

#: What an enforcement point should do about an unanswered safety question.
GATE_ASK = "ask"          # never asked -- ask, and block this build
GATE_DEGRADE = "degrade"  # asked and deferred -- build conservatively, disclose
GATE_PROCEED = "proceed"  # answered -- nothing to do


async def safety_gate_decision(db: Any, user_id: int) -> str:
    """The single policy shared by all four enforcement points.

    LOG-015 blocked on *any* unanswered safety question, which is why a user who
    deferred once was never offered a plan again. The fix is not to stop asking;
    it is to stop asking **forever**. So the two states LOG-015 collapsed are
    separated here, using a distinction the fact store already draws:

      - `fact is None`      -- never asked. Ask. Blocking is correct and stays.
      - `kind == KIND_GAP`  -- asked and deferred. The user has already declined
                               once; re-blocking is the silence defect. Degrade.

    Read by `pending_safety_questions` today as a single "unanswered" bucket;
    A10 does not add state, it stops discarding a distinction `record_gap`
    already writes.

    Fails closed: if this cannot be determined, the answer is GATE_ASK -- the
    stricter of the two, matching pre-A10 behaviour.
    """
    import questions

    try:
        pending = await questions.pending_safety_questions(db, user_id)
    except Exception:
        LOGGER.exception("safety_gate_decision_failed user_id=%s", user_id)
        return GATE_ASK

    if not pending:
        return GATE_PROCEED

    for q in pending:
        try:
            fact = (
                await user_model.get_training_limitations_fact(db, user_id)
                if q.fact_key == "training_limitations"
                else await user_model.get_fact(db, user_id, q.fact_key)
            )
        except Exception:
            LOGGER.exception("safety_gate_fact_read_failed user_id=%s", user_id)
            return GATE_ASK
        # Never asked -> ask. One unasked question is enough to ask first.
        if fact is None:
            return GATE_ASK
    # Every pending safety question has been asked and deferred.
    return GATE_DEGRADE

#: Audit outcomes. Bounded codes -- never the user's answer, never a body region.
AUDIT_ACTION = "propose_degraded_plan"
AUDIT_ENTITY = "plan"
OUTCOME_PROPOSED = "proposed"
OUTCOME_CONFIRMED = "confirmed"
OUTCOME_BLOCKED = "blocked"
OUTCOME_ALREADY_DECIDED = "already_decided"


def disclosure_lines(assessment: "ReadinessAssessment") -> list[str]:
    """What the user is told was NOT applied.

    Named specifically rather than generically. "Some information is missing"
    does not let someone judge physical risk; "exercises were not adapted for
    pain or injury" does. That specificity is the whole point of requirement 3.
    """
    lines: list[str] = []
    if assessment.safety_unknown:
        lines.append(
            "⚠️ לא היה לי מידע על כאב, פציעה או מגבלה — "
            "התרגילים לא הותאמו לכך."
        )
    if assessment.degraded_personalization:
        lines.append(
            "חלק מהפרטים חסרים, אז השתמשתי בברירות מחדל זהירות."
        )
    return lines


async def propose_degraded_plan(
    db: Any,
    user_id: int,
    plan_id: int,
    assessment: "ReadinessAssessment",
) -> str | None:
    """Record a pending approval for a plan built without safety information.

    Returns the approval id, or None when no confirmation is required (the plan
    is not safety-degraded and may follow the normal path).

    Deliberately does NOT activate. The plan exists as a candidate; any existing
    active plan is untouched until the user taps.
    """
    if not assessment.needs_confirmation:
        return None

    from noam_coach.services.core import create_approval, write_audit

    approval_id = await create_approval(
        user_id,
        APPROVAL_KIND_DEGRADED_PLAN,
        {
            "plan_id": int(plan_id),
            # Bounded class names only. The user's answer never appears here,
            # and neither does a body region.
            "safety_unknown": True,
            "gap_count": assessment.gap_count(),
        },
    )
    with suppress(Exception):
        await write_audit(
            user_id, AUDIT_ACTION, AUDIT_ENTITY, plan_id,
            outcome=OUTCOME_PROPOSED,
            safety_unknown=True,
            gap_count=assessment.gap_count(),
            approval_id=approval_id,
        )
    LOGGER.info(
        "degraded_plan_proposed user_id=%s plan_id=%s gap_count=%d",
        user_id, plan_id, assessment.gap_count(),
    )
    return approval_id


async def confirm_degraded_plan(db: Any, user_id: int, approval_id: str) -> str:
    """Activate a previously proposed degraded plan, once.

    Activation goes through A9's boundary, so supersession, the fact mirror and
    the readiness gates all behave exactly as for any other plan -- A10 adds no
    writer of its own.

    Double-tap safe by reuse rather than by a new lock: `decide_approval` updates
    `WHERE status='pending'`, so the second call changes no row and this returns
    `already_decided` instead of activating twice.
    """
    from noam_coach.services.core import decide_approval, fetch_approval, write_audit

    # `fetch_approval` returns ONLY pending rows by design, so it doubles as the
    # "is this still claimable" check -- a row already decided, belonging to
    # another user, or never created reads as absent here.
    approval = await fetch_approval(user_id, approval_id)
    if not approval:
        return OUTCOME_ALREADY_DECIDED

    # The reader renames and decodes the column: `row["data"] = json.loads(
    # row.pop("payload"))`. Reading `payload` here would always yield None and
    # silently block every confirmation.
    payload = approval.get("data") or {}
    if isinstance(payload, str):  # defensive: an undecoded row must not block
        payload = json.loads(payload)
    plan_id = int(payload.get("plan_id") or 0)
    if not plan_id:
        return OUTCOME_BLOCKED

    # Claim it. `decide_approval` updates `WHERE status='pending'`, so a
    # concurrent tap that got here first has already flipped the row and this
    # call changes nothing -- which is exactly why the guard above (a
    # pending-only read) is sufficient and no second re-read is needed. Adding
    # one would create a divergent idempotency rule alongside the existing one.
    await decide_approval(approval_id, "approved")

    from noam_coach.services import plan_mutations

    outcome = await plan_mutations.activate_proposed_plan(
        db, user_id, plan_id, reason="degraded_safety_confirmed"
    )
    with suppress(Exception):
        await write_audit(
            user_id, AUDIT_ACTION, AUDIT_ENTITY, plan_id,
            outcome=OUTCOME_CONFIRMED,
            safety_unknown=True,
            approval_id=approval_id,
            mutation_outcome=outcome.outcome,
        )
    LOGGER.info(
        "degraded_plan_confirmed user_id=%s plan_id=%s mutation=%s",
        user_id, plan_id, outcome.outcome,
    )
    return OUTCOME_CONFIRMED if outcome.changed else OUTCOME_BLOCKED


__all__ = [
    "APPROVAL_KIND_DEGRADED_PLAN",
    "AUDIT_ACTION",
    "AUDIT_ENTITY",
    "CLASS_BLOCKING_INTEGRITY",
    "CLASS_DEGRADED_PERSONALIZATION",
    "CLASS_DEGRADED_SAFETY",
    "CLASS_INFORMATIONAL",
    "ReadinessAssessment",
    "SAFETY_UNKNOWN",
    "OUTCOME_ALREADY_DECIDED",
    "OUTCOME_BLOCKED",
    "OUTCOME_CONFIRMED",
    "OUTCOME_PROPOSED",
    "assess_plan_readiness",
    "confirm_degraded_plan",
    "disclosure_lines",
    "propose_degraded_plan",
    "classify_gap",
    "conservative_limitations_value",
    "is_safety_unknown",
]
