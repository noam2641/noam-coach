"""A degraded plan is proposed, disclosed and confirmed — never auto-activated.

LOG-015 made an unanswered safety question block plan building on **every**
path. That protection was real, and A10 removes the block. It must therefore be
replaced by something at least as protective, not merely deleted.

The replacement is three protections that together are strictly more
informative than a refusal:

1. **conservative behaviour** — the plan is built under `SAFETY_UNKNOWN`, so
   the unknown is carried forward instead of read as "no limitations";
2. **disclosure** — the user is told *which* adaptation is missing;
3. **confirmation** — nothing is activated without a deliberate tap.

`test_the_log015_protection_is_replaced_not_removed` asserts all three at once
and fails if any single one is dropped. That test is the reason removing the
block is a trade rather than an erosion, and it should be the last thing anyone
deletes.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

import planning
import user_model
from db import Database
from helpers import utc_now
from noam_coach.services import plan_mutations
from noam_coach.services import plan_readiness as pr


async def _db(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "degraded.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (utc_now(),),
    )
    return db


def _bind(monkeypatch: pytest.MonkeyPatch, db: Database) -> None:
    """`write_audit`/`create_approval` resolve DB from the facade via runtime_bound."""
    import coach_bot
    from noam_coach.services import core as core_services

    monkeypatch.setattr(coach_bot, "DB", db, raising=False)
    monkeypatch.setattr(core_services, "DB", db, raising=False)


def _payload() -> dict:
    return {
        "frequency": 1,
        "sessions": [
            {
                "index": 0, "weekday": 0, "time": "18:00", "minutes": 45,
                "code": "A", "name": "A",
                "exercises": [
                    {"id": eid, "name": eid.title(), "sets": 3, "rmin": 8,
                     "rmax": 12, "rest": 90, "inc": 2.5, "weight": 40.0,
                     "muscle": "chest", "cues": [], "alts": []}
                    for eid in ("bench", "incline_db", "fly", "pushdown")
                ],
            }
        ],
    }


async def _candidate(db: Database) -> int:
    plan_id = await db.execute(
        "INSERT INTO plan_versions("
        "  user_id, plan_type, title, strategy, fit_score, status, payload, created_at"
        ") VALUES(1, 'workout', 'A', 'balanced', 0.8, 'candidate', ?, ?)",
        (json.dumps(_payload(), ensure_ascii=False), utc_now()),
    )
    return int(plan_id)


_SAFETY_UNKNOWN = pr.ReadinessAssessment(degraded_safety=("training_limitations",))


# ---------------------------------------------------------------------------
# The LOG-015 replacement — the load-bearing test
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_the_log015_protection_is_replaced_not_removed(
    tmp_path, monkeypatch
) -> None:
    """All three protections, asserted together.

    LOG-015's block is gone. If any ONE of conservative behaviour, disclosure or
    confirmation is also gone, the user is worse off than under the block — so
    this fails if any single one is removed. Do not split it into three passing
    tests: the point is that they are only adequate *together*.
    """
    db = await _db(tmp_path)
    _bind(monkeypatch, db)
    plan_id = await _candidate(db)

    # 1. Conservative behaviour: the unknown is carried, not read as none.
    assert _SAFETY_UNKNOWN.safety_unknown is True
    assert pr.is_safety_unknown(pr.conservative_limitations_value()) is True
    assert pr.is_safety_unknown("") is False

    # 2. Disclosure: specific about WHICH adaptation is missing.
    lines = pr.disclosure_lines(_SAFETY_UNKNOWN)
    assert lines, "a degraded-safety plan must disclose something"
    disclosure = " ".join(lines)
    assert "כאב" in disclosure or "פציעה" in disclosure, (
        "the disclosure must name the missing adaptation, not say "
        "'some information is missing'"
    )

    # 3. Confirmation: proposed, and NOT active.
    approval_id = await pr.propose_degraded_plan(db, 1, plan_id, _SAFETY_UNKNOWN)
    assert approval_id, "a safety-degraded plan must require confirmation"

    row = await db.fetch_one("SELECT status FROM plan_versions WHERE id=?", (plan_id,))
    assert row["status"] == "candidate", (
        "proposing must NOT activate -- that is the whole protection"
    )
    active = await db.fetch_one(
        "SELECT plan_id FROM active_plans WHERE user_id=1 AND plan_type='workout'"
    )
    assert active is None, "no active plan may be created without confirmation"


# ---------------------------------------------------------------------------
# Proposal
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_plan_without_safety_gaps_needs_no_confirmation(
    tmp_path, monkeypatch
) -> None:
    """Confirmation is for the safety-unknown case only.

    Demanding a tap for a merely-less-tailored plan would train the user to tap
    through warnings, which is how a real warning stops being read.
    """
    db = await _db(tmp_path)
    _bind(monkeypatch, db)
    plan_id = await _candidate(db)

    tailoring_only = pr.ReadinessAssessment(degraded_personalization=("equipment",))
    approval_id = await pr.propose_degraded_plan(db, 1, plan_id, tailoring_only)

    assert approval_id is None


@pytest.mark.asyncio
async def test_the_proposal_audit_is_structured_and_carries_no_medical_text(
    tmp_path, monkeypatch
) -> None:
    """Bounded scalars only. A limitation string names a body part."""
    db = await _db(tmp_path)
    _bind(monkeypatch, db)
    plan_id = await _candidate(db)

    await pr.propose_degraded_plan(db, 1, plan_id, _SAFETY_UNKNOWN)

    row = await db.fetch_one(
        "SELECT details FROM audit WHERE user_id=1 AND action=?", (pr.AUDIT_ACTION,)
    )
    assert row is not None, "a proposal must leave an audit trail"
    details = json.loads(row["details"])

    assert details["outcome"] == pr.OUTCOME_PROPOSED
    assert details["safety_unknown"] is True or details["safety_unknown"] == 1
    assert details["gap_count"] == 1

    serialized = json.dumps(details, ensure_ascii=False)
    for leak in ("כאב", "פציעה", "knee", "shoulder", "elbow", "bench"):
        assert leak not in serialized, f"{leak!r} must not reach an audit row"


@pytest.mark.asyncio
async def test_the_proposal_log_carries_counts_not_medical_text(
    tmp_path, monkeypatch, caplog
) -> None:
    db = await _db(tmp_path)
    _bind(monkeypatch, db)
    plan_id = await _candidate(db)

    with caplog.at_level(logging.INFO):
        await pr.propose_degraded_plan(db, 1, plan_id, _SAFETY_UNKNOWN)

    emitted = "\n".join(r.message for r in caplog.records)
    assert "degraded_plan_proposed" in emitted
    assert "user_id=1" in emitted
    for leak in ("כאב", "פציעה", "training_limitations"):
        assert leak not in emitted


# ---------------------------------------------------------------------------
# Confirmation
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_confirmation_activates_through_the_a9_boundary(
    tmp_path, monkeypatch
) -> None:
    db = await _db(tmp_path)
    _bind(monkeypatch, db)
    plan_id = await _candidate(db)

    async def _ok(*_a, **_k):
        return {}

    monkeypatch.setattr(planning, "_require_readiness", _ok)

    approval_id = await pr.propose_degraded_plan(db, 1, plan_id, _SAFETY_UNKNOWN)
    outcome = await pr.confirm_degraded_plan(db, 1, approval_id)

    assert outcome == pr.OUTCOME_CONFIRMED
    row = await db.fetch_one("SELECT status FROM plan_versions WHERE id=?", (plan_id,))
    assert row["status"] == "active"


@pytest.mark.asyncio
async def test_a_double_tap_cannot_activate_twice(tmp_path, monkeypatch) -> None:
    """Idempotency by reuse, not by a new lock.

    `decide_approval` updates `WHERE status='pending'`, so the second claim
    changes no row. Reusing that is why A10 needed no lock of its own.
    """
    db = await _db(tmp_path)
    _bind(monkeypatch, db)
    plan_id = await _candidate(db)

    async def _ok(*_a, **_k):
        return {}

    monkeypatch.setattr(planning, "_require_readiness", _ok)

    approval_id = await pr.propose_degraded_plan(db, 1, plan_id, _SAFETY_UNKNOWN)
    first = await pr.confirm_degraded_plan(db, 1, approval_id)
    second = await pr.confirm_degraded_plan(db, 1, approval_id)

    assert first == pr.OUTCOME_CONFIRMED
    assert second == pr.OUTCOME_ALREADY_DECIDED, (
        "a stale or double tap must not activate a second time"
    )

    rows = await db.fetch_all(
        "SELECT id FROM plan_versions WHERE user_id=1 AND status='active'"
    )
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_a_stale_approval_id_is_refused(tmp_path, monkeypatch) -> None:
    db = await _db(tmp_path)
    _bind(monkeypatch, db)

    outcome = await pr.confirm_degraded_plan(db, 1, "does-not-exist")

    assert outcome == pr.OUTCOME_ALREADY_DECIDED


@pytest.mark.asyncio
async def test_another_users_approval_cannot_be_confirmed(
    tmp_path, monkeypatch
) -> None:
    """`fetch_approval` scopes on user_id -- reused, not re-implemented."""
    db = await _db(tmp_path)
    _bind(monkeypatch, db)
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(2,'B',NULL,?)",
        (utc_now(),),
    )
    plan_id = await _candidate(db)
    approval_id = await pr.propose_degraded_plan(db, 1, plan_id, _SAFETY_UNKNOWN)

    outcome = await pr.confirm_degraded_plan(db, 2, approval_id)

    assert outcome == pr.OUTCOME_ALREADY_DECIDED
    row = await db.fetch_one("SELECT status FROM plan_versions WHERE id=?", (plan_id,))
    assert row["status"] == "candidate"


@pytest.mark.asyncio
async def test_a_blocked_activation_is_reported_not_silently_swallowed(
    tmp_path, monkeypatch
) -> None:
    """Safety-critical gaps still block at activation.

    Confirming a degraded plan does not buy a pass on the readiness gate -- it
    only covers the *safety-unknown* case the user explicitly accepted.
    """
    db = await _db(tmp_path)
    _bind(monkeypatch, db)
    plan_id = await _candidate(db)

    async def _blocked(_db, _user_id, _profile):
        raise planning.PlanningBlockedError("missing", missing=["weekly_availability"])

    monkeypatch.setattr(planning, "_require_readiness", _blocked)

    approval_id = await pr.propose_degraded_plan(db, 1, plan_id, _SAFETY_UNKNOWN)
    outcome = await pr.confirm_degraded_plan(db, 1, approval_id)

    assert outcome == pr.OUTCOME_BLOCKED
    row = await db.fetch_one("SELECT status FROM plan_versions WHERE id=?", (plan_id,))
    assert row["status"] == "candidate"


# ---------------------------------------------------------------------------
# The boundary extension
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_activation_reuses_the_a9_boundary_rather_than_writing_directly(
    tmp_path,
) -> None:
    """A10 must not become a second plan writer.

    Asserted against source with docstrings stripped: the module explains the
    rule it must not break, so a raw text scan would flag its own prose.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(pr))
    code = "\n".join(
        ast.unparse(node)
        for node in tree.body
        if not (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        )
    )

    assert "plan_mutations" in code, "activation must route through the A9 boundary"
    for forbidden in ("INSERT INTO plan_versions", "UPDATE plan_versions",
                      "INSERT INTO active_plans", "set_fact",
                      "authorize_governed_fact_write"):
        assert forbidden not in code, (
            f"{forbidden!r} in A10; it must not write plans or the governed fact"
        )


@pytest.mark.asyncio
async def test_an_already_active_plan_confirms_as_no_change(
    tmp_path, monkeypatch
) -> None:
    """Already live is a success, not a failure."""
    db = await _db(tmp_path)
    _bind(monkeypatch, db)
    plan_id = await _candidate(db)
    await db.execute(
        "UPDATE plan_versions SET status='active' WHERE id=?", (plan_id,)
    )

    outcome = await plan_mutations.activate_proposed_plan(
        db, 1, plan_id, reason="test"
    )

    assert outcome.outcome == plan_mutations.OUTCOME_NO_CHANGE
    assert outcome.is_failure is False


# ---------------------------------------------------------------------------
# The four enforcement points
#
# LOG-015 was enforced in four places, and A10 changes the answer in all four.
# A fix applied to three of them would leave a path that still goes silent, so
# each is pinned separately rather than trusting the shared helper.
# ---------------------------------------------------------------------------
async def _defer_safety(db: Database) -> None:
    """Record the safety question as ASKED AND DEFERRED, via the real writer."""
    await user_model.record_gap(
        db, 1, "training_limitations",
        why_matters="A10 test: asked and deferred",
    )


#: Everything the workout profile requires EXCEPT the safety fact.
#:
#: The population A10 serves is a user who answered everything else and deferred
#: only the limitation question. A fixture missing the other facts would be
#: blocked by `blocking_integrity` gaps and would prove nothing about the safety
#: axis -- which is exactly the confusion A10's classification exists to prevent.
_OTHER_WORKOUT_FACTS = {
    "primary_goal": "strength",
    "training_days_per_week": 3,
    "session_minutes": 45,
    "training_location": "gym",
    "equipment": "full_gym",
    "strength_experience": "intermediate",
    "weekly_availability": "mon,wed,fri",
}


async def _answer_everything_but_safety(db: Database) -> None:
    for key, value in _OTHER_WORKOUT_FACTS.items():
        await user_model.set_fact(
            db, 1, key, value,
            kind=user_model.KIND_FACT, source=user_model.SOURCE_USER,
        )


@pytest.mark.asyncio
async def test_never_asked_still_blocks_but_deferred_degrades(
    tmp_path, monkeypatch
) -> None:
    """The distinction the whole change rests on.

    LOG-015 collapsed these two into one "unanswered" bucket and blocked both.
    Blocking the first is right; blocking the second is the silence defect.
    """
    db = await _db(tmp_path)
    _bind(monkeypatch, db)

    assert await pr.safety_gate_decision(db, 1) == pr.GATE_ASK, (
        "a question never asked must still block -- that protection is kept"
    )

    await _defer_safety(db)
    assert await pr.safety_gate_decision(db, 1) == pr.GATE_DEGRADE, (
        "a question already asked and deferred must degrade, not block again"
    )


@pytest.mark.asyncio
async def test_an_answered_safety_question_proceeds_normally(
    tmp_path, monkeypatch
) -> None:
    db = await _db(tmp_path)
    _bind(monkeypatch, db)
    await user_model.set_fact(
        db, 1, "training_limitations", "none",
        kind=user_model.KIND_FACT, source=user_model.SOURCE_USER,
    )
    assert await pr.safety_gate_decision(db, 1) == pr.GATE_PROCEED


@pytest.mark.asyncio
async def test_the_gate_fails_closed_when_it_cannot_read(monkeypatch) -> None:
    """An unreadable state is not evidence that the user has no limitations.

    Fail-open here would be the same class of defect as reporting a query
    failure as "no history": an infrastructure fault stated as a fact.
    """

    class _Broken:
        async def fetch_all(self, *_a, **_k):
            raise RuntimeError("database is locked")

        async def fetch_one(self, *_a, **_k):
            raise RuntimeError("database is locked")

    assert await pr.safety_gate_decision(_Broken(), 1) == pr.GATE_ASK, (
        "a failed read must take the STRICTER branch, not the permissive one"
    )


@pytest.mark.asyncio
async def test_gate_1_check_plan_readiness_stops_reporting_a_deferred_gap(
    tmp_path, monkeypatch
) -> None:
    """Enforcement point 1: `onboarding.check_plan_readiness`."""
    from noam_coach.bot import onboarding

    db = await _db(tmp_path)
    _bind(monkeypatch, db)
    monkeypatch.setattr(onboarding, "DB", db, raising=False)
    await user_model.set_fact(
        db, 1, "primary_goal", "strength",
        kind=user_model.KIND_FACT, source=user_model.SOURCE_USER,
    )

    assert "שאלות בטיחות לא נענו" in await onboarding.check_plan_readiness(1)

    await _defer_safety(db)
    assert "שאלות בטיחות לא נענו" not in await onboarding.check_plan_readiness(1), (
        "a deferred safety question must no longer read as a hard readiness gap"
    )


@pytest.mark.asyncio
async def test_gate_2_the_onboarding_build_block_releases_once_deferred(
    tmp_path, monkeypatch
) -> None:
    """Enforcement points 2 and 3: both `_block_plan_for_pending_safety` sites.

    One helper serves both call sites, so pinning the helper pins both -- but
    the helper is asserted to still ASK when the question was never put.
    """
    from noam_coach.bot import onboarding

    db = await _db(tmp_path)
    _bind(monkeypatch, db)
    monkeypatch.setattr(onboarding, "DB", db, raising=False)

    asked: list[str] = []

    class _Target:
        async def reply_text(self, text, **_kw):
            asked.append(text)

    monkeypatch.setattr(onboarding, "set_pending", _noop_pending, raising=False)

    blocked = await onboarding._block_plan_for_pending_safety(_Target(), 1)
    assert blocked is True, "never asked -> must still block and ask"
    assert asked, "the blocking path must actually surface the question"

    await _defer_safety(db)
    asked.clear()
    blocked = await onboarding._block_plan_for_pending_safety(_Target(), 1)
    assert blocked is False, "deferred -> the build proceeds (conservatively)"
    assert not asked, "a deferred question must not be re-asked on every build"


async def _noop_pending(*_a, **_kw) -> None:
    return None


@pytest.mark.asyncio
async def test_gate_4_activation_refuses_a_degraded_plan_without_confirmation(
    tmp_path, monkeypatch
) -> None:
    """Enforcement point 4: `_validate_plan_for_activation`.

    This is the one gate that must NOT become permissive. Building under an
    unknown is allowed; activating under one without an explicit tap is not.
    """
    import planning

    db = await _db(tmp_path)
    _bind(monkeypatch, db)
    await _answer_everything_but_safety(db)
    await _defer_safety(db)
    plan_id = await _candidate(db)

    outcome = await plan_mutations.activate_proposed_plan(
        db, 1, plan_id, reason="test_no_confirmation"
    )
    assert outcome.outcome == plan_mutations.OUTCOME_BLOCKED, (
        "an unknown limitation must still block ACTIVATION without confirmation"
    )
    row = await db.fetch_one("SELECT status FROM plan_versions WHERE id=?", (plan_id,))
    assert row["status"] == "candidate", "the plan must not have gone live"

    # The same plan, once explicitly approved, is allowed through.
    assert await planning._degraded_plan_confirmed(db, 1, plan_id) is False
    approval_id = await pr.propose_degraded_plan(db, 1, plan_id, _SAFETY_UNKNOWN)
    assert await pr.confirm_degraded_plan(db, 1, approval_id) == pr.OUTCOME_CONFIRMED
    row = await db.fetch_one("SELECT status FROM plan_versions WHERE id=?", (plan_id,))
    assert row["status"] == "active"


@pytest.mark.asyncio
async def test_approving_one_plan_does_not_authorise_a_different_one(
    tmp_path, monkeypatch
) -> None:
    """Confirmation is scoped to the plan the user actually saw."""
    db = await _db(tmp_path)
    _bind(monkeypatch, db)
    await _answer_everything_but_safety(db)
    await _defer_safety(db)
    approved_id = await _candidate(db)
    other_id = await _candidate(db)

    approval_id = await pr.propose_degraded_plan(db, 1, approved_id, _SAFETY_UNKNOWN)
    await pr.confirm_degraded_plan(db, 1, approval_id)

    assert await planning._degraded_plan_confirmed(db, 1, approved_id) is True
    assert await planning._degraded_plan_confirmed(db, 1, other_id) is False, (
        "approving one plan must not silently authorise another"
    )


@pytest.mark.asyncio
async def test_the_ignore_set_matches_the_safety_profile_exactly() -> None:
    """Pins the premise behind `_require_readiness(..., ignore=...)`.

    Skipping a fact in the general gate is only safe because the safety gate
    enforces that exact fact immediately afterwards. If the safety profile ever
    gains or loses a member, the skip stops being covered and silently becomes a
    hole -- so the two sets must stay identical rather than merely overlapping.
    """
    assert planning._SAFETY_FACTS == frozenset(
        user_model.READINESS_PROFILES["safety"].required
    ), (
        "a fact skipped by the general readiness gate must be one the safety "
        "gate still enforces"
    )


@pytest.mark.asyncio
async def test_the_audit_allowlist_drops_an_unlisted_medical_field(
    tmp_path, monkeypatch
) -> None:
    """Registration must CONSTRAIN, not merely describe.

    Every field A10 audits is a bounded scalar, so the unregistered
    `_scalar_only` fallback would have preserved them anyway — and a test that
    only checked "the expected fields are present" would pass identically with
    no registration at all. What registration buys is the opposite guarantee:
    a field nobody vetted is dropped. A limitation string is exactly the kind of
    scalar the fallback would have waved through.
    """
    from noam_coach.services import core as core_services

    db = await _db(tmp_path)
    _bind(monkeypatch, db)
    plan_id = await _candidate(db)

    await core_services.write_audit(
        1, pr.AUDIT_ACTION, pr.AUDIT_ENTITY, plan_id,
        outcome=pr.OUTCOME_PROPOSED,
        gap_count=1,
        limitation_detail="כאב בברך ימין",  # must never be stored
    )

    row = await db.fetch_one(
        "SELECT details FROM audit WHERE user_id=1 AND action=? "
        "ORDER BY id DESC LIMIT 1",
        (pr.AUDIT_ACTION,),
    )
    details = json.loads(row["details"])

    assert details["gap_count"] == 1, "vetted fields must survive"
    assert "limitation_detail" not in details, (
        "an unlisted field must be dropped by the allowlist, not stored because "
        "it happened to be a scalar"
    )
    assert "ברך" not in json.dumps(details, ensure_ascii=False)


# ---------------------------------------------------------------------------
# The disclosure must reach the USER, not just exist as a function
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_the_disclosure_is_actually_rendered_with_the_plan(
    tmp_path, monkeypatch
) -> None:
    """Requirement 2, asserted where the user would see it.

    Softening the gates made these build paths reachable with limitations
    unknown. If the plan then renders unchanged, the degraded plan is
    indistinguishable from a fully adapted one and the LOG-015 block was
    removed for nothing. Asserting `disclosure_lines()` returns text does NOT
    cover this — the helper can be correct while no caller uses it.
    """
    from noam_coach.bot import onboarding

    db = await _db(tmp_path)
    _bind(monkeypatch, db)
    monkeypatch.setattr(onboarding, "DB", db, raising=False)
    await _answer_everything_but_safety(db)
    await _defer_safety(db)

    rendered = await onboarding._with_safety_disclosure(1, "PLAN BODY")

    assert "PLAN BODY" in rendered, "the plan itself must still be shown"
    assert rendered != "PLAN BODY", (
        "a plan built without safety adaptation must not render identically to "
        "an adapted one"
    )
    assert "כאב" in rendered or "פציעה" in rendered, (
        "the disclosure must name the missing adaptation specifically"
    )


@pytest.mark.asyncio
async def test_a_fully_answered_user_sees_no_warning(tmp_path, monkeypatch) -> None:
    """The disclosure must not become background noise.

    A warning shown to everyone is one nobody reads, which would quietly undo
    the protection for the users who actually need it.
    """
    from noam_coach.bot import onboarding

    db = await _db(tmp_path)
    _bind(monkeypatch, db)
    monkeypatch.setattr(onboarding, "DB", db, raising=False)
    await _answer_everything_but_safety(db)
    await user_model.set_fact(
        db, 1, "training_limitations", "none",
        kind=user_model.KIND_FACT, source=user_model.SOURCE_USER,
    )

    assert await onboarding._with_safety_disclosure(1, "PLAN BODY") == "PLAN BODY"


def test_every_plan_render_path_routes_through_the_disclosure() -> None:
    """Structural guard: a fourth build path must not silently skip disclosure.

    The behavioural test above pins the helper. This pins the *call sites*,
    because the failure mode here is additive: someone adds a new build path,
    renders `format_weekly_plan` directly, and every existing test still passes
    while that one path delivers an undisclosed degraded plan.

    Parsed with `ast` rather than grepped so this file's own prose about
    `format_weekly_plan` cannot satisfy or trip the check.
    """
    import ast
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parents[1]
    offenders: list[str] = []

    for rel in ("noam_coach/bot/onboarding.py", "noam_coach/bot/assistant.py"):
        source = (root / rel).read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue
            if node.func.id != "format_weekly_plan":
                continue
            # The rendered text must be consumed by the disclosure wrapper, or
            # be assigned to a name that a nearby disclosure block prefixes.
            segment = ast.get_source_segment(source, node) or ""
            line = node.lineno
            window = "\n".join(source.splitlines()[max(0, line - 12): line + 14])
            if "_with_safety_disclosure" in window or "disclosure_lines" in window:
                continue
            offenders.append(f"{rel}:{line} {segment[:60]}")

    assert not offenders, (
        "these plan renders do not pass through the A10 safety disclosure, so a "
        "degraded plan would render as if fully adapted:\n  "
        + "\n  ".join(offenders)
    )


# ---------------------------------------------------------------------------
# The invariant, on the path that previously could not honour it
#
#   degraded proposal -> conservative behaviour -> disclosure
#   -> explicit confirmation -> activation through A9 only
#
# `build_weekly_plan` used to write the governed fact directly with no
# `plan_versions` row. There was no candidate to hold and no activation call to
# gate, so an unconfirmed activation was structurally unavoidable. These tests
# assert it is now structurally impossible.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_building_a_weekly_plan_proposes_and_does_not_activate(
    tmp_path, monkeypatch
) -> None:
    """The heart of the contract: build must not activate."""
    from noam_coach.bot import onboarding

    db = await _db(tmp_path)
    _bind(monkeypatch, db)
    monkeypatch.setattr(onboarding, "DB", db, raising=False)
    await _answer_everything_but_safety(db)
    await _defer_safety(db)

    plan = await onboarding.build_weekly_plan(1, 3)

    assert plan.get("plan_id"), "the built plan must be backed by a plan_versions row"
    row = await db.fetch_one(
        "SELECT status FROM plan_versions WHERE id=?", (plan["plan_id"],)
    )
    assert row["status"] == "candidate", "building must produce a CANDIDATE"

    assert await user_model.get_value(db, 1, "active_workout_plan") is None, (
        "no governed fact may be written before confirmation -- this is the "
        "direct write A10 retired"
    )
    active = await db.fetch_one(
        "SELECT plan_id FROM active_plans WHERE user_id=1 AND plan_type='workout'"
    )
    assert active is None, "no plan may become active before the user confirms"


@pytest.mark.asyncio
async def test_the_built_plan_carries_real_exercises(tmp_path, monkeypatch) -> None:
    """Routing through the canonical pipeline is what makes the gate possible.

    The old shape carried only `weekday/time/code/name`, which
    `_validate_plan_for_activation` rejects for having no exercises and no
    minutes -- so it could never have been activated through the gate even in
    principle. Real exercises are a precondition for the contract, not a bonus.
    """
    from noam_coach.bot import onboarding

    db = await _db(tmp_path)
    _bind(monkeypatch, db)
    monkeypatch.setattr(onboarding, "DB", db, raising=False)
    await _answer_everything_but_safety(db)
    await _defer_safety(db)

    session = (await onboarding.build_weekly_plan(1, 3))["sessions"][0]

    assert session.get("exercises"), "sessions must carry exercises"
    assert session.get("minutes"), "sessions must carry a duration"
    assert session.get("time"), (
        "sessions must carry a time -- repair_workout_payload supplies it, and "
        "without it activation refuses with missing=['weekly_availability']"
    )


@pytest.mark.asyncio
async def test_ignoring_the_proposal_never_activates(tmp_path, monkeypatch) -> None:
    """A user who is shown a plan and never taps keeps whatever they had."""
    from noam_coach.bot import onboarding

    db = await _db(tmp_path)
    _bind(monkeypatch, db)
    monkeypatch.setattr(onboarding, "DB", db, raising=False)
    await _answer_everything_but_safety(db)
    await _defer_safety(db)

    await onboarding.build_weekly_plan(1, 3)
    await onboarding.build_weekly_plan(1, 3)  # shown again, still no tap

    active = await db.fetch_one(
        "SELECT plan_id FROM active_plans WHERE user_id=1 AND plan_type='workout'"
    )
    assert active is None
    assert await user_model.get_value(db, 1, "active_workout_plan") is None


@pytest.mark.asyncio
async def test_an_existing_active_plan_survives_an_unconfirmed_proposal(
    tmp_path, monkeypatch
) -> None:
    """Proposing must not disturb what the user is currently training from.

    The old direct write replaced the active plan outright, so a user who asked
    to see options lost the plan they were mid-week on.
    """
    from noam_coach.bot import onboarding

    db = await _db(tmp_path)
    _bind(monkeypatch, db)
    monkeypatch.setattr(onboarding, "DB", db, raising=False)
    await _answer_everything_but_safety(db)
    await _defer_safety(db)

    # An existing active plan, activated the legitimate way.
    first = await onboarding.build_weekly_plan(1, 3)
    approval_id = await pr.propose_degraded_plan(db, 1, first["plan_id"], _SAFETY_UNKNOWN)
    assert await pr.confirm_degraded_plan(db, 1, approval_id) == pr.OUTCOME_CONFIRMED
    before = await db.fetch_one(
        "SELECT plan_id FROM active_plans WHERE user_id=1 AND plan_type='workout'"
    )
    assert before["plan_id"] == first["plan_id"]

    # A new proposal, not confirmed.
    await onboarding.build_weekly_plan(1, 3)

    after = await db.fetch_one(
        "SELECT plan_id FROM active_plans WHERE user_id=1 AND plan_type='workout'"
    )
    assert after["plan_id"] == before["plan_id"], (
        "an unconfirmed proposal must leave the active plan exactly as it was"
    )


@pytest.mark.asyncio
async def test_confirmation_activates_exactly_once_through_a9(
    tmp_path, monkeypatch
) -> None:
    """End to end on the previously-ungated path, including the double tap."""
    from noam_coach.bot import onboarding

    db = await _db(tmp_path)
    _bind(monkeypatch, db)
    monkeypatch.setattr(onboarding, "DB", db, raising=False)
    await _answer_everything_but_safety(db)
    await _defer_safety(db)

    plan = await onboarding.build_weekly_plan(1, 3)
    approval_id = await pr.propose_degraded_plan(db, 1, plan["plan_id"], _SAFETY_UNKNOWN)

    assert await pr.confirm_degraded_plan(db, 1, approval_id) == pr.OUTCOME_CONFIRMED
    assert await pr.confirm_degraded_plan(db, 1, approval_id) == pr.OUTCOME_ALREADY_DECIDED
    assert await pr.confirm_degraded_plan(db, 1, approval_id) == pr.OUTCOME_ALREADY_DECIDED

    row = await db.fetch_one(
        "SELECT status FROM plan_versions WHERE id=?", (plan["plan_id"],)
    )
    assert row["status"] == "active"

    # Exactly one active row, and the governed fact now mirrors Tier-1.
    actives = await db.fetch_all(
        "SELECT plan_id FROM active_plans WHERE user_id=1 AND plan_type='workout'"
    )
    assert len(actives) == 1
    fact = await user_model.get_value(db, 1, "active_workout_plan")
    assert fact and fact.get("plan_id") == plan["plan_id"], (
        "the fact must be the derived mirror of the activated row, not an "
        "independently authored copy"
    )


@pytest.mark.asyncio
async def test_a_stale_approval_for_a_superseded_candidate_cannot_activate(
    tmp_path, monkeypatch
) -> None:
    """An old keyboard must not activate a plan the user has moved past.

    `save_candidates` marks previous candidates `superseded`. A user who was
    shown plan A, asked for a rebuild, and then tapped the OLD message's button
    must not get plan A: they are looking at plan B. `activate_plan` refuses a
    non-candidate/active row, so the A9 boundary reports this rather than
    activating a stale proposal.
    """
    from noam_coach.bot import onboarding

    db = await _db(tmp_path)
    _bind(monkeypatch, db)
    monkeypatch.setattr(onboarding, "DB", db, raising=False)
    await _answer_everything_but_safety(db)
    await _defer_safety(db)

    first = await onboarding.build_weekly_plan(1, 3)
    stale_approval = await pr.propose_degraded_plan(
        db, 1, first["plan_id"], _SAFETY_UNKNOWN
    )

    # A newer proposal supersedes the first candidate.
    await onboarding.build_weekly_plan(1, 3)
    row = await db.fetch_one(
        "SELECT status FROM plan_versions WHERE id=?", (first["plan_id"],)
    )
    assert row["status"] == "superseded", "the rebuild must supersede the old candidate"

    outcome = await pr.confirm_degraded_plan(db, 1, stale_approval)

    assert outcome != pr.OUTCOME_CONFIRMED, (
        "a stale tap must not activate a superseded candidate"
    )
    active = await db.fetch_one(
        "SELECT plan_id FROM active_plans WHERE user_id=1 AND plan_type='workout'"
    )
    assert active is None, "no plan may become active from a stale approval"


@pytest.mark.asyncio
async def test_the_fallback_shape_renders_but_can_never_activate(
    tmp_path, monkeypatch
) -> None:
    """When the canonical pipeline cannot build, the local shape still renders.

    This is the one branch where `build_weekly_plan` returns something that is
    NOT backed by a `plan_versions` row. It must therefore be inert: no
    `plan_id` to tap, no governed fact, nothing active. The user sees a plan
    and is asked to complete what is missing -- the pre-A10 behaviour minus the
    silent activation that used to accompany it.

    Without this test the fallback is the obvious place for the retired writer
    to creep back in, because it is the path where "we could not propose"
    historically meant "so write it directly".
    """
    from noam_coach.bot import onboarding

    db = await _db(tmp_path)
    _bind(monkeypatch, db)
    monkeypatch.setattr(onboarding, "DB", db, raising=False)
    # No workout facts at all: the pipeline refuses on blocking-integrity gaps.
    plan = await onboarding.build_weekly_plan(1, 3)

    assert plan.get("sessions"), "the user must still be shown something"
    assert not plan.get("plan_id"), (
        "the fallback must not claim a plan_versions row it does not have"
    )
    assert await user_model.get_value(db, 1, "active_workout_plan") is None, (
        "the fallback must not write the governed fact -- this is exactly where "
        "the retired direct writer would creep back in"
    )
    active = await db.fetch_one(
        "SELECT plan_id FROM active_plans WHERE user_id=1 AND plan_type='workout'"
    )
    assert active is None


# ---------------------------------------------------------------------------
# The rest of the A10 contract: what free text actually delivers
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_free_text_frequency_days_time_and_duration_reach_the_candidate(
    tmp_path, monkeypatch
) -> None:
    """D-1: free text is not an independent plan-creation path.

    The intent a user expresses in free text -- how many days, which days, what
    time, how long -- must survive into the stored candidate. If it did not,
    routing through the pipeline would have quietly discarded what the user
    said, which is worse than the fact-only path it replaced.
    """
    from noam_coach.bot import onboarding

    db = await _db(tmp_path)
    _bind(monkeypatch, db)
    monkeypatch.setattr(onboarding, "DB", db, raising=False)
    await _answer_everything_but_safety(db)
    await user_model.set_fact(
        db, 1, "session_minutes", 45,
        kind=user_model.KIND_FACT, source=user_model.SOURCE_USER,
    )
    await _defer_safety(db)

    plan = await onboarding.build_weekly_plan(1, 3)

    stored = await db.fetch_one(
        "SELECT payload FROM plan_versions WHERE id=?", (plan["plan_id"],)
    )
    payload = json.loads(stored["payload"])
    sessions = payload["sessions"]

    assert payload["frequency"] == 3, "the requested frequency must reach the candidate"
    assert len(sessions) == 3
    # Weekdays: the user's confirmed availability (mon,wed,fri -> 0,2,4).
    assert sorted(s["weekday"] for s in sessions) == [0, 2, 4], (
        "the user's chosen training days must reach the candidate"
    )
    for session in sessions:
        assert session.get("time"), "each session must carry a time"
        assert session.get("minutes") == 45, (
            "the user's session duration must reach the candidate"
        )


@pytest.mark.asyncio
async def test_the_candidate_carries_concrete_exercises_not_a_template_code(
    tmp_path, monkeypatch
) -> None:
    """The stored candidate must BE the plan, not a pointer to a template.

    The retired fact-only shape carried `code` and left the renderer to expand
    it from the global `PLANS` template -- which is why substitutions were
    invisible and why A11b's renderer rewrite was blocked. The stored payload
    must carry the exercises themselves, already adapted, so the plan a user
    confirms is the plan that was stored.
    """
    from noam_coach.bot import onboarding

    db = await _db(tmp_path)
    _bind(monkeypatch, db)
    monkeypatch.setattr(onboarding, "DB", db, raising=False)
    await _answer_everything_but_safety(db)
    await _defer_safety(db)

    plan = await onboarding.build_weekly_plan(1, 3)
    stored = await db.fetch_one(
        "SELECT payload FROM plan_versions WHERE id=?", (plan["plan_id"],)
    )
    sessions = json.loads(stored["payload"])["sessions"]

    for session in sessions:
        exercises = session.get("exercises") or []
        assert exercises, (
            "a stored session with no exercises is a template pointer, not a "
            "plan -- and activation refuses it outright"
        )
        for exercise in exercises:
            assert exercise.get("id"), "each exercise must be concrete"
            assert exercise.get("sets"), "each exercise must carry a prescription"


@pytest.mark.asyncio
async def test_the_three_gap_classes_stay_distinct_end_to_end(
    tmp_path, monkeypatch
) -> None:
    """blocking_integrity / degraded_personalization / degraded_safety must not
    collapse into each other on the real path.

    Collapsing them is the original defect: every gap became a hard block, so a
    user missing one optional answer got the same silence as one missing a
    structural input.
    """
    from noam_coach.bot import onboarding

    db = await _db(tmp_path)
    _bind(monkeypatch, db)
    monkeypatch.setattr(onboarding, "DB", db, raising=False)

    # 1. blocking_integrity: nothing answered -> no candidate is produced.
    blocked = await onboarding.build_weekly_plan(1, 3)
    assert not blocked.get("plan_id"), (
        "a structural gap must not produce an activatable candidate"
    )

    # 2. degraded_safety: everything but the safety fact -> a candidate, and
    #    confirmation is required.
    await _answer_everything_but_safety(db)
    await _defer_safety(db)
    degraded = await onboarding.build_weekly_plan(1, 3)
    assert degraded.get("plan_id"), "a safety gap must still produce a plan"
    assessment = await pr.assess_plan_readiness(db, 1)
    assert assessment.safety_unknown is True
    assert assessment.needs_confirmation is True
    assert pr.disclosure_lines(assessment), "safety-degraded must disclose"

    # 3. degraded_personalization: answered safety -> no confirmation demanded.
    await user_model.set_fact(
        db, 1, "training_limitations", "none",
        kind=user_model.KIND_FACT, source=user_model.SOURCE_USER,
    )
    tailored = await pr.assess_plan_readiness(db, 1)
    assert tailored.safety_unknown is False
    assert tailored.needs_confirmation is False, (
        "a merely-less-tailored plan must not demand a tap, or the tap stops "
        "meaning anything where it matters"
    )


@pytest.mark.asyncio
async def test_the_choice_screen_discloses_before_the_confirmation_tap(
    tmp_path, monkeypatch
) -> None:
    """Disclosure must precede the decision, not follow it.

    `render_candidate_list` is the screen the user chooses from, and its
    buttons ARE the confirmation tap (`planv2:select:<id>`). Disclosing only
    after the tap would inform someone about a decision they had already made.
    """
    from noam_coach.bot import onboarding

    db = await _db(tmp_path)
    _bind(monkeypatch, db)
    monkeypatch.setattr(onboarding, "DB", db, raising=False)
    await _answer_everything_but_safety(db)
    await _defer_safety(db)
    await onboarding.build_weekly_plan(1, 3)

    sent: list[str] = []
    buttons: list[str] = []

    async def _capture(target, text, markup=None, **_kw):
        sent.append(text)
        for row in getattr(markup, "inline_keyboard", []) or []:
            for btn in row:
                buttons.append(getattr(btn, "callback_data", "") or "")

    # `safe_edit` is runtime_bound and re-syncs from the facade on every call,
    # so patching the onboarding attribute alone is overwritten. Patch the
    # owning module too -- the same trap that made A9's audit tests pass
    # locally and fail in CI.
    import coach_bot
    from noam_coach.bot import ui as _ui

    monkeypatch.setattr(_ui, "safe_edit", _capture, raising=False)
    monkeypatch.setattr(coach_bot, "safe_edit", _capture, raising=False)
    monkeypatch.setattr(onboarding, "safe_edit", _capture, raising=False)

    await onboarding.render_candidate_list(object(), 1, "workout")

    body = "\n".join(sent)
    assert body, "the choice screen must render"
    assert "כאב" in body or "פציעה" in body, (
        "the choice screen must disclose that limitations could not be applied, "
        "before the user taps"
    )
    assert any(b.startswith("planv2:select:") for b in buttons), (
        "the confirmation tap must be reachable from this screen"
    )
