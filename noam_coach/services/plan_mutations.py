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
REASON_ACTIVE_SESSION = "active_session_in_progress"
REASON_INTERNAL = "internal_error"


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
    assumptions = list(assumptions) + [f"realigned:{reason}"]

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
    "REASON_ACTIVE_SESSION",
    "REASON_INTERNAL",
    "REASON_QUALITY",
    "REASON_READINESS",
    "REASON_UNEXPRESSIBLE",
    "activate_proposed_plan",
    "realign_saved_plan_to_weekdays",
]
