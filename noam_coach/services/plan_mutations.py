"""The supported boundary for changing a saved workout plan (A9).

Until now there was no way to change a stored plan except to regenerate it from
scratch. That is why an availability correction updated five availability facts
and left the saved plan still scheduling the removed day — the code that applied
the correction had nowhere to write the consequence, and said so:

    "Consequence, recorded rather than hidden: after a correction the
    availability stores say Friday while a stored plan may still schedule
    Saturday, until the plan is rebuilt."

This module is the write path that comment asks for, built inside three
constraints the codebase already enforces.

**Copy-on-write.** No production code ever updates `plan_versions.payload`; the
only mutations are status transitions, and a guard test enforces that. A
corrected plan is therefore a NEW version, never an edit. The old row survives
as `superseded`, so a completed session can still be explained against the plan
that was live when it happened.

**One authorized writer.** `activate_plan` is the sole owner permitted to write
the `active_workout_plan` fact (A4). This module does not wrap that
authorization around itself — A4's own error text says to route through the
existing owner instead. So a realignment ends by calling `activate_plan`, which
supersedes, activates and mirrors exactly as it does for any other plan.

**The gates stay.** `_validate_plan_for_activation` still runs. A realigned plan
that fails readiness or quality is refused, not forced through. That is
deliberate: a corrected weekday is not a reason to lower the bar on safety.

The outcome type distinguishes four situations that a boolean would flatten, and
flattening them is how "nothing happened" becomes indistinguishable from "it
broke".
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Any

import planning
from config import LOGGER
from noam_coach.services.core import write_audit

# ---------------------------------------------------------------------------
# Outcomes. Bounded reason codes, safe to log and count.
# ---------------------------------------------------------------------------
#: The saved plan was changed: a new version exists and is active.
OUTCOME_REALIGNED = "realigned"
#: The plan already matched. Nothing to do, and that is a success.
OUTCOME_NO_CHANGE = "no_change"
#: There was no saved plan to realign. Not a failure -- a legacy or new user.
OUTCOME_NO_PLAN = "no_plan"
#: A gate refused the change: readiness, safety or plan quality.
OUTCOME_BLOCKED = "blocked"
#: The mutation was attempted and something broke.
OUTCOME_FAILED = "failed"

#: Why a realignment was blocked or failed. Bounded so they can be counted.
REASON_READINESS = "readiness_missing"
REASON_QUALITY = "plan_quality"
REASON_UNEXPRESSIBLE = "not_expressible_as_remap"
REASON_INTERNAL = "internal_error"
# `REASON_ACTIVE_SESSION` was declared here and never returned by any path --
# a protection that existed only as a name. Removed by A11b rather than
# implemented, because the protection it described is real and already lives
# elsewhere: `sessions.plan` snapshots the plan at session start, so a live
# workout continues on the payload it began with no matter how the saved plan
# changes underneath it. A blocking condition here would have refused a
# legitimate correction to defend against a corruption that cannot occur.


@dataclass(frozen=True)
class MutationOutcome:
    """What happened to the saved plan, and why.

    `outcome` is always set. `reason` is set only for BLOCKED and FAILED, and
    the pair is what lets a caller tell "your plan already matched" from "we
    could not change your plan" -- two sentences a user must never see
    interchanged.
    """

    outcome: str
    reason: str | None = None
    plan_id: int | None = None
    new_plan_id: int | None = None
    #: Weekday sets, for audit. Short sorted strings, never lists -- the audit
    #: allowlist drops list-valued details silently.
    before_days: str = ""
    after_days: str = ""
    missing: tuple[str, ...] = field(default_factory=tuple)

    @property
    def changed(self) -> bool:
        return self.outcome == OUTCOME_REALIGNED

    @property
    def is_failure(self) -> bool:
        """True only for genuine failure -- NOT for no_change or no_plan.

        The distinction this property exists to protect: a plan that already
        matched is a success. Reporting it as a failure would tell a user
        something is wrong when nothing is.
        """
        return self.outcome in (OUTCOME_BLOCKED, OUTCOME_FAILED)


def _weekday_key(days: Any) -> str:
    """Weekday set as a short sorted scalar, e.g. "0,2,4".

    The audit allowlist keeps only scalars and drops lists silently, so a
    weekday list passed as a list would vanish while the test still passed.
    Bounded by construction: at most 7 single digits.
    """
    if not isinstance(days, (list, tuple, set)):
        return ""
    try:
        return ",".join(str(int(d)) for d in sorted(set(days)))
    except (TypeError, ValueError):
        return ""


#: Tokens `workout_quality_issues` produces. Readiness reports FACT KEYS
#: ("training_limitations", "weekly_availability"); quality reports structural
#: findings about the plan itself. Matching on shape rather than an exact list
#: so a new quality check does not silently start being reported as readiness.
_QUALITY_TOKEN_MARKERS = ("session_", "workout_plan_", "weekly_", "_exercise_")


def _looks_like_quality(missing: tuple[str, ...]) -> bool:
    """True when the blocked reason describes the PLAN, not the user's answers."""
    return any(
        any(marker in token for marker in _QUALITY_TOKEN_MARKERS) for token in missing
    )


def _session_weekdays(payload: dict[str, Any]) -> list[int]:
    days: list[int] = []
    for session in payload.get("sessions") or []:
        if not isinstance(session, dict):
            continue
        weekday = session.get("weekday")
        if isinstance(weekday, int):
            days.append(weekday)
    return days


def _remap_sessions(payload: dict[str, Any], target_days: list[int]) -> dict[str, Any] | None:
    """A copy of `payload` with session weekdays moved onto `target_days`.

    Deliberately narrow. It moves sessions to different days and changes
    nothing else -- same sessions, same exercises, same order, same count. A
    user who said "Friday, not Saturday" asked for a day to move, not for their
    programme to be redesigned, and regenerating from scratch could hand back a
    different split with different exercises.

    Returns None when the correction cannot be expressed as a pure remap --
    when the day count differs, the session count is unchanged but the target
    set cannot host it. Refusing is correct there: the honest answer is to
    offer a rebuild, not to invent a structure the user did not ask for.
    """
    sessions = payload.get("sessions") or []
    if not sessions or not target_days:
        return None
    if len(sessions) != len(target_days):
        # A different number of training days is a structural change, not a
        # remap. Out of scope by design.
        return None

    remapped = copy.deepcopy(payload)
    ordered = sorted(target_days)
    for session, weekday in zip(remapped.get("sessions") or [], ordered):
        if not isinstance(session, dict):
            return None
        session["weekday"] = weekday
    return remapped


async def realign_saved_plan_to_weekdays(
    db: Any,
    user_id: int,
    target_days: list[int],
    *,
    reason: str,
) -> MutationOutcome:
    """Move the saved workout plan onto `target_days`. Closes W1-44.

    The plan is not edited. A corrected copy is inserted as a new candidate and
    activated through `activate_plan`, so supersession, the fact mirror, the
    readiness gates and the existing activation telemetry all behave exactly as
    they do for any other plan.

    Never guesses. If the correction cannot be expressed as a remap, or a gate
    refuses it, the outcome says so with a bounded reason rather than
    approximating something the user did not ask for.
    """
    try:
        active = await planning.get_active_plan(db, user_id, "workout")
    except Exception:
        LOGGER.exception(
            "plan_realign_failed user_id=%s stage=read reason=%s",
            user_id, REASON_INTERNAL,
        )
        return MutationOutcome(OUTCOME_FAILED, reason=REASON_INTERNAL)

    if not active:
        # A legacy or new user with no Tier-1 plan. Not a failure: there is
        # simply nothing to realign, and saying "we could not update your plan"
        # would be false.
        return MutationOutcome(OUTCOME_NO_PLAN)

    plan_id = int(active["id"])
    payload = active.get("payload") or {}
    if isinstance(payload, str):
        payload = json.loads(payload)

    before = _weekday_key(_session_weekdays(payload))
    after = _weekday_key(target_days)

    if before == after:
        return MutationOutcome(
            OUTCOME_NO_CHANGE, plan_id=plan_id, before_days=before, after_days=after
        )

    remapped = _remap_sessions(payload, target_days)
    if remapped is None:
        await _audit(
            user_id, OUTCOME_BLOCKED, REASON_UNEXPRESSIBLE, plan_id, None, before, after
        )
        return MutationOutcome(
            OUTCOME_BLOCKED,
            reason=REASON_UNEXPRESSIBLE,
            plan_id=plan_id,
            before_days=before,
            after_days=after,
        )

    try:
        new_plan_id = await _insert_corrected_version(db, user_id, active, remapped, reason)
        # Route through the existing owner. A4 permits activate_plan to write
        # the governed fact; wrapping that authorization around this module
        # instead is exactly what A4's error text forbids.
        await planning.activate_plan(db, user_id, new_plan_id)
    except planning.PlanningBlockedError as exc:
        missing = tuple(str(m) for m in (getattr(exc, "missing", None) or ()))
        # `_validate_plan_for_activation` raises the SAME exception type for a
        # missing readiness fact and for a plan-quality defect, so the type
        # cannot separate them -- but the operator response differs completely.
        # Readiness means "the user still owes us an answer"; quality means "we
        # built something unusable". Reporting the second as the first would
        # send someone to re-ask a question that was never the problem.
        reason = REASON_QUALITY if _looks_like_quality(missing) else REASON_READINESS
        await _audit(user_id, OUTCOME_BLOCKED, reason, plan_id, None, before, after)
        LOGGER.info(
            "plan_realign_blocked user_id=%s plan_id=%s reason=%s missing=%d",
            user_id, plan_id, reason, len(missing),
        )
        return MutationOutcome(
            OUTCOME_BLOCKED,
            reason=reason,
            plan_id=plan_id,
            before_days=before,
            after_days=after,
            missing=missing,
        )
    except Exception:
        LOGGER.exception(
            "plan_realign_failed user_id=%s plan_id=%s stage=activate reason=%s",
            user_id, plan_id, REASON_INTERNAL,
        )
        await _audit(
            user_id, OUTCOME_FAILED, REASON_INTERNAL, plan_id, None, before, after
        )
        return MutationOutcome(
            OUTCOME_FAILED,
            reason=REASON_INTERNAL,
            plan_id=plan_id,
            before_days=before,
            after_days=after,
        )

    await _audit(
        user_id, OUTCOME_REALIGNED, None, plan_id, new_plan_id, before, after
    )
    return MutationOutcome(
        OUTCOME_REALIGNED,
        plan_id=plan_id,
        new_plan_id=new_plan_id,
        before_days=before,
        after_days=after,
    )


async def _insert_corrected_version(
    db: Any,
    user_id: int,
    active: dict[str, Any],
    payload: dict[str, Any],
    reason: str,
    *,
    assumption_prefix: str = "realigned",
) -> int:
    """Insert the corrected plan as a candidate and return its id.

    A direct INSERT rather than `save_candidates`, which supersedes every
    existing candidate for the type as a side effect -- correct when generating
    a fresh set of three, wrong when adding one corrected version beside them.
    """
    from helpers import utc_now

    assumptions = active.get("assumptions") or []
    if isinstance(assumptions, str):
        assumptions = json.loads(assumptions or "[]")
    # A11b generalised the prefix: the note records WHICH operation produced
    # this version, and "realigned:slot_substituted" would have been a lie.
    assumptions = list(assumptions) + [f"{assumption_prefix}:{reason}"]

    plan_id = await db.execute(
        """
        INSERT INTO plan_versions(
            user_id, plan_type, title, strategy, fit_score, status,
            payload, rationale, tradeoffs, assumptions, based_on, created_at
        ) VALUES(?, 'workout', ?, ?, ?, 'candidate', ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            str(active.get("title") or "workout"),
            str(active.get("strategy") or "balanced"),
            float(active.get("fit_score") or 0.0),
            json.dumps(payload, ensure_ascii=False),
            json.dumps(active.get("rationale") or [], ensure_ascii=False),
            json.dumps(active.get("tradeoffs") or [], ensure_ascii=False),
            json.dumps(assumptions, ensure_ascii=False),
            json.dumps(active.get("based_on") or {}, ensure_ascii=False),
            utc_now(),
        ),
    )
    return int(plan_id)


async def _audit(
    user_id: int,
    outcome: str,
    reason: str | None,
    plan_id: int | None,
    new_plan_id: int | None,
    before_days: str,
    after_days: str,
) -> None:
    """Record the mutation attempt through the existing audit mechanism.

    Best-effort by contract: a failure to record must not break a plan change
    the user asked for. Scalars only -- the allowlist drops anything else, and
    a plan payload must never reach an audit row.
    """
    try:
        await write_audit(
            user_id,
            "realign_weekdays",
            "plan",
            plan_id,
            outcome=outcome,
            reason=reason,
            new_plan_id=new_plan_id,
            before_days=before_days,
            after_days=after_days,
        )
    except Exception:
        LOGGER.exception(
            "plan_realign_audit_failed user_id=%s plan_id=%s outcome=%s",
            user_id, plan_id, outcome,
        )



#: A11b: the slot the caller named is not in the saved plan. Distinct from
#: `no_plan` (there is no plan at all) and from `no_change` (the slot is
#: already implemented by that exercise) -- collapsing them would report a
#: stale callback as a successful no-op.
REASON_UNKNOWN_SLOT = "slot_not_in_plan"


async def substitute_slot_in_saved_plan(
    db: Any,
    user_id: int,
    slot_id: str,
    replacement_exercise_id: str,
    *,
    reason: str,
) -> MutationOutcome:
    """Persist a substitution against the SAVED plan, by slot identity.

    Added beside the existing operations, per A9's contract: later items extend
    this module and never introduce a writer elsewhere. This is the third.

    Copy-on-write, like every operation here: the active version is never
    edited, a new candidate carries the change, and activation runs through
    `activate_plan` so the governed-fact mirror stays A4-authorized. An
    in-progress workout is unaffected -- `sessions.plan` snapshotted the payload
    at session start, so the change lands from the NEXT session onward.

    Resolution is by slot identity, never by list position, so a plan whose
    exercises were re-ranked between the offer and the tap either finds the same
    professional slot or finds nothing.
    """
    from noam_coach.services import workout_slots

    if not workout_slots.is_valid_slot_id(slot_id):
        return MutationOutcome(
            OUTCOME_BLOCKED, reason=REASON_UNKNOWN_SLOT, missing=(str(slot_id),)
        )

    try:
        active = await planning.get_active_plan(db, user_id, "workout")
    except Exception:
        LOGGER.exception(
            "slot_substitution_failed user_id=%s stage=read reason=%s",
            user_id, REASON_INTERNAL,
        )
        return MutationOutcome(OUTCOME_FAILED, reason=REASON_INTERNAL)

    if not active:
        return MutationOutcome(OUTCOME_NO_PLAN)

    # Defensive, not load-bearing, and measured as such: `get_active_plan`
    # decodes the payload from JSON on every call, so this dict is already a
    # fresh object and mutating it cannot reach the stored row. The copy stays
    # because that is a property of the reader, not a contract -- a future
    # cached reader would make in-place edits reach the database silently.
    # Deliberate breakage confirmed removing it changes no observable
    # behaviour today, so no test asserts it; the comment is the record.
    payload = copy.deepcopy(active.get("payload") or {})
    target = None
    for session in payload.get("sessions") or []:
        if not isinstance(session, dict):
            continue
        found = workout_slots.find_by_slot_id(session.get("exercises"), slot_id)
        if found is not None:
            target = found[1]
            break

    if target is None:
        # A stale offer, or a slot from a superseded plan. Refusing is the whole
        # point of identity-based resolution: the alternative is applying the
        # change to whatever now occupies that position.
        return MutationOutcome(
            OUTCOME_BLOCKED, reason=REASON_UNKNOWN_SLOT, plan_id=int(active["id"])
        )

    if str(target.get("id") or "") == str(replacement_exercise_id):
        return MutationOutcome(OUTCOME_NO_CHANGE, plan_id=int(active["id"]))

    # The slot keeps its identity; only the implementation changes. That is the
    # distinction the whole slot model exists to express.
    target["original_id"] = target.get("original_id") or target.get("id")
    target["id"] = str(replacement_exercise_id)

    try:
        new_plan_id = await _insert_corrected_version(
            db, user_id, active, payload, reason,
            assumption_prefix="slot_substituted",
        )
        await planning.activate_plan(db, user_id, new_plan_id)
    except planning.PlanningBlockedError as exc:
        missing = tuple(str(m) for m in (getattr(exc, "missing", None) or ()))
        blocked_reason = REASON_QUALITY if _looks_like_quality(missing) else REASON_READINESS
        LOGGER.info(
            "slot_substitution_blocked user_id=%s slot=%s reason=%s",
            user_id, slot_id, blocked_reason,
        )
        return MutationOutcome(OUTCOME_BLOCKED, reason=blocked_reason, missing=missing)
    except Exception:
        LOGGER.exception(
            "slot_substitution_failed user_id=%s stage=write reason=%s",
            user_id, REASON_INTERNAL,
        )
        return MutationOutcome(OUTCOME_FAILED, reason=REASON_INTERNAL)

    LOGGER.info(
        "slot_substituted user_id=%s slot=%s new_plan_id=%s reason=%s",
        user_id, slot_id, new_plan_id, reason,
    )
    return MutationOutcome(OUTCOME_REALIGNED, plan_id=new_plan_id)


async def activate_proposed_plan(
    db: Any,
    user_id: int,
    plan_id: int,
    *,
    reason: str,
) -> MutationOutcome:
    """Activate a plan the user has explicitly approved.

    Added BESIDE the realignment operation rather than as a second boundary --
    A9's published contract requires later items to extend this module, never to
    introduce a writer elsewhere. A10 is the first item to take it up.

    The activation itself is `planning.activate_plan`, so supersession, the
    governed fact mirror (which A4 authorizes only there), the readiness gates
    and the existing telemetry all behave exactly as for any other plan. This
    function adds the outcome vocabulary and the audit, nothing else.

    Deliberately does not re-check approval state: the caller owns the approval
    lifecycle and its `WHERE status='pending'` claim. Duplicating that check
    here would create a second, divergent idempotency rule.
    """
    try:
        row = await db.fetch_one(
            "SELECT id, status FROM plan_versions WHERE id=? AND user_id=?",
            (plan_id, user_id),
        )
    except Exception:
        LOGGER.exception(
            "plan_activate_failed user_id=%s plan_id=%s stage=read reason=%s",
            user_id, plan_id, REASON_INTERNAL,
        )
        return MutationOutcome(OUTCOME_FAILED, reason=REASON_INTERNAL, plan_id=plan_id)

    if not row:
        return MutationOutcome(OUTCOME_NO_PLAN, plan_id=plan_id)
    if str(row.get("status")) == "active":
        # Already live -- a success, not a failure. Distinguishing this is what
        # stops a double tap from reading as an error.
        return MutationOutcome(OUTCOME_NO_CHANGE, plan_id=plan_id)

    try:
        await planning.activate_plan(db, user_id, plan_id)
    except planning.PlanningBlockedError as exc:
        missing = tuple(str(m) for m in (getattr(exc, "missing", None) or ()))
        blocked_reason = REASON_QUALITY if _looks_like_quality(missing) else REASON_READINESS
        await _audit(user_id, OUTCOME_BLOCKED, blocked_reason, plan_id, None, "", "")
        LOGGER.info(
            "plan_activate_blocked user_id=%s plan_id=%s reason=%s missing=%d",
            user_id, plan_id, blocked_reason, len(missing),
        )
        return MutationOutcome(
            OUTCOME_BLOCKED, reason=blocked_reason, plan_id=plan_id, missing=missing
        )
    except Exception:
        LOGGER.exception(
            "plan_activate_failed user_id=%s plan_id=%s stage=activate reason=%s",
            user_id, plan_id, REASON_INTERNAL,
        )
        await _audit(user_id, OUTCOME_FAILED, REASON_INTERNAL, plan_id, None, "", "")
        return MutationOutcome(OUTCOME_FAILED, reason=REASON_INTERNAL, plan_id=plan_id)

    await _audit(user_id, OUTCOME_REALIGNED, None, plan_id, plan_id, "", "")
    LOGGER.info(
        "plan_activated user_id=%s plan_id=%s reason=%s", user_id, plan_id, reason
    )
    return MutationOutcome(OUTCOME_REALIGNED, plan_id=plan_id, new_plan_id=plan_id)


__all__ = [
    "MutationOutcome",
    "OUTCOME_BLOCKED",
    "OUTCOME_FAILED",
    "OUTCOME_NO_CHANGE",
    "OUTCOME_NO_PLAN",
    "OUTCOME_REALIGNED",
    "REASON_INTERNAL",
    "REASON_QUALITY",
    "REASON_READINESS",
    "REASON_UNEXPRESSIBLE",
    "REASON_UNKNOWN_SLOT",
    "activate_proposed_plan",
    "realign_saved_plan_to_weekdays",
    "substitute_slot_in_saved_plan",
]
