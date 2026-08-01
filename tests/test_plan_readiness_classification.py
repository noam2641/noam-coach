"""Unknown safety must not behave like known-none (A10).

`check_plan_readiness` returned a flat list of Hebrew strings and its caller
treated any non-empty list as a hard block, so a legacy user missing one answer
got silence instead of a plan.

The severity axis needed to do better already existed and was being discarded —
`questions.Question` carries `safety`/`plan_impact`/`urgency`/`uncertainty`/
`burden`. What did *not* exist, and what this file pins hardest, is a
conservative path for an **unknown** limitation.

Measured on `develop` before A10: with no `training_limitations`, no
`active_pain` and no `medical_avoidance`, `client_training_profile_from_facts`
yields `injuries=()`, `pain_areas=()`, `movement_limitations=()`,
`medical_flags=()` — byte-identical to a user who answered "none". Absence
silently meant "no limitations", and every joint was loaded freely.

That is the defect these tests exist to make impossible to reintroduce.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

import training_intelligence
import user_model
from db import Database
from helpers import utc_now
from noam_coach.services import plan_readiness as pr


async def _db(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "readiness.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (utc_now(),),
    )
    return db


# ---------------------------------------------------------------------------
# The distinction the whole item rests on
# ---------------------------------------------------------------------------
def test_unknown_safety_is_distinguishable_from_known_none() -> None:
    """The measured defect: absence read as "no limitations".

    Without a sentinel there is nothing to distinguish the two states, because
    both produce empty tuples downstream. This asserts the sentinel is not
    silently equal to an empty or "none" value.
    """
    unknown = pr.conservative_limitations_value()

    assert pr.is_safety_unknown(unknown) is True
    assert pr.is_safety_unknown("") is False, (
        "empty is what the old fallback produced -- it must NOT read as unknown, "
        "or the sentinel adds nothing"
    )
    assert pr.is_safety_unknown("none") is False
    assert pr.is_safety_unknown(None) is False


def test_the_sentinel_does_not_invent_a_body_region() -> None:
    """Guessing "assume knee pain" would be a different wrong answer.

    It would suppress exercises the user may need and imply knowledge we do not
    have. The sentinel carries the uncertainty forward instead.
    """
    value = pr.conservative_limitations_value()
    for region_token in ("knee", "shoulder", "back", "elbow", "ברך", "כתף"):
        assert region_token not in value


def test_the_sentinel_is_not_mistaken_for_a_pain_region() -> None:
    """It must not accidentally match the region tokenizer.

    If it did, an unknown limitation would silently become a *specific* claimed
    injury -- worse than the defect it replaces.
    """
    regions = training_intelligence.pain_regions(
        pr.conservative_limitations_value(), None
    )
    assert regions == set() or not regions, (
        "the unknown sentinel must not resolve to a concrete pain region"
    )


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
def test_training_limitations_is_degraded_safety_not_blocking() -> None:
    """Owner decision: refusing outright is what produces silence."""
    assert pr.classify_gap("training_limitations") == pr.CLASS_DEGRADED_SAFETY


def test_the_two_structural_facts_block() -> None:
    """Not a safety matter -- an activation one.

    Both are in `READINESS_PROFILES["workout"].required`, so `_require_readiness`
    refuses without them regardless of any builder default. Building a plan that
    activation is certain to reject is a wasted outcome, not a degraded one.
    """
    assert pr.classify_gap("weekly_availability") == pr.CLASS_BLOCKING_INTEGRITY
    assert pr.classify_gap("training_days_per_week") == pr.CLASS_BLOCKING_INTEGRITY


def test_facts_with_conservative_builder_defaults_degrade() -> None:
    for key in ("session_minutes", "strength_experience", "equipment",
                "training_location", "primary_goal"):
        assert pr.classify_gap(key) == pr.CLASS_DEGRADED_PERSONALIZATION, key


def test_optional_facts_are_informational() -> None:
    for key in ("workout_window", "training_preferences", "performance_goal"):
        assert pr.classify_gap(key) == pr.CLASS_INFORMATIONAL, key


def test_an_unknown_fact_key_does_not_invent_a_block() -> None:
    """A fact nothing declares cannot be one the plan depends on."""
    assert pr.classify_gap("some_future_key") == pr.CLASS_INFORMATIONAL


def test_the_blocking_set_matches_the_readiness_profile() -> None:
    """Pins the premise. If either fact leaves `required`, this classification
    is wrong and must be revisited rather than silently drifting."""
    required = set(user_model.READINESS_PROFILES["workout"].required)
    assert {"weekly_availability", "training_days_per_week"} <= required
    assert "training_limitations" in required


# ---------------------------------------------------------------------------
# The assessment
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_user_with_nothing_is_blocked_and_safety_unknown(tmp_path) -> None:
    db = await _db(tmp_path)

    assessment = await pr.assess_plan_readiness(db, 1)

    assert assessment.can_build is False, "structural gaps must block"
    assert assessment.safety_unknown is True
    assert assessment.needs_confirmation is True
    assert "training_limitations" in assessment.degraded_safety


@pytest.mark.asyncio
async def test_a_readiness_failure_fails_closed_on_safety(tmp_path, caplog) -> None:
    """An unreadable readiness state is not evidence of no limitations.

    Failing open here would be the same class of defect as reporting a query
    failure as "no history" -- an infrastructure fault stated as a fact about
    the user.
    """

    class _Broken:
        async def fetch_all(self, *_a, **_k):
            raise RuntimeError("database is locked")

        async def fetch_one(self, *_a, **_k):
            raise RuntimeError("database is locked")

    with caplog.at_level(logging.ERROR):
        assessment = await pr.assess_plan_readiness(_Broken(), 1)

    assert assessment.safety_unknown is True, (
        "a failed readiness read must not be treated as 'no limitations'"
    )
    assert assessment.needs_confirmation is True
    assert any(
        "plan_readiness_assessment_failed" in r.message for r in caplog.records
    )


@pytest.mark.asyncio
async def test_the_three_outcomes_stay_distinct(tmp_path) -> None:
    """blocked / degraded / safety-unknown must never collapse into each other."""
    blocked = pr.ReadinessAssessment(blocking=("weekly_availability",))
    safety = pr.ReadinessAssessment(degraded_safety=("training_limitations",))
    tailoring = pr.ReadinessAssessment(degraded_personalization=("equipment",))

    assert (blocked.can_build, blocked.safety_unknown) == (False, False)
    assert (safety.can_build, safety.safety_unknown) == (True, True)
    assert (tailoring.can_build, tailoring.safety_unknown) == (True, False)

    # Only the safety case demands a deliberate tap.
    assert safety.needs_confirmation is True
    assert tailoring.needs_confirmation is False
    assert blocked.needs_confirmation is False


def test_gap_count_is_a_bounded_scalar_for_audit() -> None:
    """The audit allowlist drops lists silently, so the count is what survives.

    Asserted as a value rather than a type: a test that only checked "an audit
    row exists" would pass while the field vanished.
    """
    assessment = pr.ReadinessAssessment(
        degraded_safety=("training_limitations",),
        degraded_personalization=("equipment", "session_minutes"),
    )
    count = assessment.gap_count()
    assert count == 3
    assert isinstance(count, int), "a list would be dropped by _allowlist_audit_details"


def test_a_fully_ready_assessment_needs_no_confirmation() -> None:
    ready = pr.ReadinessAssessment()
    assert ready.can_build is True
    assert ready.is_degraded is False
    assert ready.needs_confirmation is False
    assert ready.gap_count() == 0


# ---------------------------------------------------------------------------
# Privacy
# ---------------------------------------------------------------------------
def test_class_names_carry_no_medical_detail() -> None:
    """The reason codes name a CLASS, never an answer.

    `SAFETY_QUESTIONS` has one member, so the class name does imply the topic --
    that residual is accepted and recorded. What must never appear is the user's
    answer or a body region.
    """
    for value in (pr.CLASS_BLOCKING_INTEGRITY, pr.CLASS_DEGRADED_SAFETY,
                  pr.CLASS_DEGRADED_PERSONALIZATION, pr.CLASS_INFORMATIONAL):
        lowered = value.lower()
        for leak in ("knee", "shoulder", "back", "elbow", "pain", "injur"):
            assert leak not in lowered, f"{value} leaks a medical detail"
