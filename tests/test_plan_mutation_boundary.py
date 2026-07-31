"""The supported way to change a saved plan, and W1-44 closed (A9).

Until this landed there was no write path for a stored plan at all. That is why
an availability correction updated five availability facts and left the saved
plan still scheduling the removed day — and the code that applied the correction
said so in a comment rather than fixing it, because closing it "needs a write
path in workout_catalog (and, for Tier-1, in planning.py) -- a separate task".

The user-visible half was worse than the data half: the reply said
"ימי אימון עודכנו ✅" while the plan the user actually trains from had not moved.

Three constraints shape what A9 is allowed to be, and each has a test here
because each is the kind of rule that erodes quietly:

* plans are copy-on-write — a correction creates a NEW version, never an edit;
* `activate_plan` is the sole A4-authorized writer of the governed fact, so the
  boundary routes through it rather than wrapping authorization around itself;
* the activation gates still run — a corrected weekday is not a reason to lower
  the bar on safety.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

import planning
from db import Database
from helpers import utc_now
from noam_coach.services import plan_mutations


async def _db(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "a9.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (utc_now(),),
    )
    return db


def _payload(weekdays: list[int]) -> dict[str, Any]:
    return {
        "frequency": len(weekdays),
        "sessions": [
            {
                "index": i,
                "weekday": wd,
                "time": "18:00",
                "minutes": 45,
                "code": "A",
                "name": "A",
                # Enough exercises, each fully specified, so the plan passes
                # `workout_quality_issues`. A thinner fixture is rejected at
                # activation and every realignment test would report `blocked`
                # for a reason unrelated to what it is testing.
                "exercises": [
                    {"id": eid, "name": eid.title(), "sets": 3, "rmin": 8,
                     "rmax": 12, "rest": 90, "inc": 2.5, "weight": 40.0,
                     "muscle": "chest", "cues": [], "alts": []}
                    for eid in ("bench", "incline_db", "fly", "pushdown")
                ],
            }
            for i, wd in enumerate(weekdays)
        ],
    }


def _bind_audit(monkeypatch: pytest.MonkeyPatch, db: Database) -> None:
    """Point the audit writer at the test database.

    `write_audit` resolves `DB` from its own module globals via runtime_bound,
    so patching the caller is not enough -- the write lands on the real
    configured path and fails with "no such table: audit".
    """
    from noam_coach.services import core as core_services

    monkeypatch.setattr(core_services, "DB", db, raising=False)


async def _active_plan(db: Database, weekdays: list[int]) -> int:
    plan_id = await db.execute(
        "INSERT INTO plan_versions("
        "  user_id, plan_type, title, strategy, fit_score, status, payload, created_at"
        ") VALUES(1, 'workout', 'A', 'balanced', 0.8, 'active', ?, ?)",
        (json.dumps(_payload(weekdays), ensure_ascii=False), utc_now()),
    )
    await db.execute(
        "INSERT INTO active_plans(user_id, plan_type, plan_id, updated_at) "
        "VALUES(1, 'workout', ?, ?)",
        (plan_id, utc_now()),
    )
    return int(plan_id)


# ---------------------------------------------------------------------------
# The four outcomes are genuinely distinct
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_no_plan_is_not_a_failure(tmp_path) -> None:
    """A legacy or new user has nothing to realign, and that is fine.

    Reporting this as a failure would tell the user their plan could not be
    updated when there was never a plan to update — and would put a warning in
    front of exactly the users least equipped to interpret it.
    """
    db = await _db(tmp_path)

    outcome = await plan_mutations.realign_saved_plan_to_weekdays(
        db, 1, [4], reason="test"
    )

    assert outcome.outcome == plan_mutations.OUTCOME_NO_PLAN
    assert outcome.is_failure is False
    assert outcome.changed is False


@pytest.mark.asyncio
async def test_a_plan_that_already_matches_reports_no_change(tmp_path) -> None:
    """"Already correct" is a success, not a no-op to be reported as failure."""
    db = await _db(tmp_path)
    plan_id = await _active_plan(db, [4])

    outcome = await plan_mutations.realign_saved_plan_to_weekdays(
        db, 1, [4], reason="test"
    )

    assert outcome.outcome == plan_mutations.OUTCOME_NO_CHANGE
    assert outcome.is_failure is False
    assert outcome.plan_id == plan_id
    assert outcome.before_days == outcome.after_days == "4"

    # And nothing was written: no second version, no supersession.
    rows = await db.fetch_all("SELECT status FROM plan_versions WHERE user_id=1")
    assert len(rows) == 1
    assert rows[0]["status"] == "active"


@pytest.mark.asyncio
async def test_a_correction_that_is_not_a_remap_is_blocked_not_guessed(
    tmp_path,
) -> None:
    """Changing the NUMBER of training days is a structural change.

    A user who says "Friday, not Saturday" asked for a day to move. Turning a
    2-day plan into a 3-day plan is a different programme, and inventing one
    would be guessing at something they never asked for. Refusing with a
    bounded reason is the honest answer.
    """
    db = await _db(tmp_path)
    await _active_plan(db, [1, 4])

    outcome = await plan_mutations.realign_saved_plan_to_weekdays(
        db, 1, [1, 3, 5], reason="test"
    )

    assert outcome.outcome == plan_mutations.OUTCOME_BLOCKED
    assert outcome.reason == plan_mutations.REASON_UNEXPRESSIBLE
    assert outcome.is_failure is True

    rows = await db.fetch_all("SELECT status FROM plan_versions WHERE user_id=1")
    assert len(rows) == 1, "a blocked realignment must not leave a stray version"


@pytest.mark.asyncio
async def test_a_read_failure_is_reported_as_failure_not_as_no_plan(
    tmp_path, caplog
) -> None:
    """The distinction that matters most operationally.

    "You have no plan" and "we could not read your plan" are different
    sentences, and collapsing them turns an infrastructure fault into a
    statement about the user's data — the same class of defect A5 fixed in the
    history reader.
    """

    class _Broken:
        async def fetch_one(self, *_a, **_k):
            raise RuntimeError("database is locked")

        async def fetch_all(self, *_a, **_k):
            raise RuntimeError("database is locked")

    with caplog.at_level(logging.ERROR):
        outcome = await plan_mutations.realign_saved_plan_to_weekdays(
            _Broken(), 1, [4], reason="test"
        )

    assert outcome.outcome == plan_mutations.OUTCOME_FAILED
    assert outcome.reason == plan_mutations.REASON_INTERNAL
    assert outcome.outcome != plan_mutations.OUTCOME_NO_PLAN
    assert any("plan_realign_failed" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Copy-on-write and the authorized writer
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_realignment_creates_a_new_version_and_supersedes_the_old(
    tmp_path, monkeypatch
) -> None:
    """The load-bearing structural assertion.

    Editing the payload in place would break the (plan_id, session_index)
    identity every completed session is explained against — a finished workout
    would silently start describing a plan that never ran.
    """
    db = await _db(tmp_path)
    old_id = await _active_plan(db, [5])

    # Bypass the readiness gate: this test is about copy-on-write, not gating,
    # and seeding full readiness here would test compute_readiness instead.
    async def _ok(*_a, **_k):
        return {}

    monkeypatch.setattr(planning, "_require_readiness", _ok)

    outcome = await plan_mutations.realign_saved_plan_to_weekdays(
        db, 1, [4], reason="schedule_correction"
    )

    assert outcome.outcome == plan_mutations.OUTCOME_REALIGNED
    assert outcome.new_plan_id != old_id
    assert outcome.before_days == "5"
    assert outcome.after_days == "4"

    old = await db.fetch_one("SELECT status, payload FROM plan_versions WHERE id=?", (old_id,))
    new = await db.fetch_one(
        "SELECT status, payload FROM plan_versions WHERE id=?", (outcome.new_plan_id,)
    )
    assert old["status"] == "superseded", "the old version must survive, not be edited"
    assert new["status"] == "active"

    # The old payload is untouched -- copy-on-write, not mutation.
    assert json.loads(old["payload"])["sessions"][0]["weekday"] == 5
    assert json.loads(new["payload"])["sessions"][0]["weekday"] == 4


@pytest.mark.asyncio
async def test_the_realigned_plan_keeps_its_exercises(tmp_path, monkeypatch) -> None:
    """A day moved. Nothing else.

    Regenerating instead of remapping could hand back a different split with
    different exercises — a much larger change than the user requested.
    """
    db = await _db(tmp_path)
    await _active_plan(db, [5])

    async def _ok(*_a, **_k):
        return {}

    monkeypatch.setattr(planning, "_require_readiness", _ok)

    outcome = await plan_mutations.realign_saved_plan_to_weekdays(
        db, 1, [4], reason="test"
    )

    new = await db.fetch_one(
        "SELECT payload FROM plan_versions WHERE id=?", (outcome.new_plan_id,)
    )
    sessions = json.loads(new["payload"])["sessions"]
    assert len(sessions) == 1
    assert sessions[0]["exercises"][0]["id"] == "bench"
    assert sessions[0]["time"] == "18:00"
    assert sessions[0]["minutes"] == 45


@pytest.mark.asyncio
async def test_the_governed_fact_is_written_by_activate_plan_not_by_this_module(
    tmp_path,
) -> None:
    """A9 must not become a second writer of the governed fact.

    A4 permits exactly one owner and its error text says to route through that
    owner rather than wrapping the authorization around a new call site. This
    asserts the source, not the behaviour: a module that opened its own
    authorization would still pass a functional test while defeating A4.
    """
    import ast
    import inspect

    # Strip docstrings and comments before scanning: this module EXPLAINS why it
    # must not write the governed fact, so a raw text scan would flag the very
    # prose documenting the rule. Same trap as the A4 identity guard.
    tree = ast.parse(inspect.getsource(plan_mutations))
    code = "\n".join(
        ast.unparse(node)
        for node in tree.body
        if not (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        )
    )

    assert "authorize_governed_fact_write" not in code, (
        "the mutation boundary opened its own governed-fact authorization; "
        "route through activate_plan, which A4 already authorizes"
    )
    assert "set_fact" not in code, (
        "the mutation boundary writes a fact directly; activate_plan is the "
        "sole authorized writer"
    )
    assert "activate_plan" in code, (
        "the boundary must route activation through the existing owner"
    )


@pytest.mark.asyncio
async def test_a_blocked_readiness_gate_is_reported_not_bypassed(
    tmp_path, monkeypatch
) -> None:
    """Safety-critical gaps still block. A corrected weekday does not buy a pass.

    `_validate_plan_for_activation` requires the safety profile, which requires
    `training_limitations`. A realignment for a user who has not answered that
    must be refused with a bounded reason, not forced through.
    """
    db = await _db(tmp_path)
    await _active_plan(db, [5])

    async def _blocked(_db, _user_id, profile):
        raise planning.PlanningBlockedError("missing", missing=["training_limitations"])

    monkeypatch.setattr(planning, "_require_readiness", _blocked)

    outcome = await plan_mutations.realign_saved_plan_to_weekdays(
        db, 1, [4], reason="test"
    )

    assert outcome.outcome == plan_mutations.OUTCOME_BLOCKED
    assert outcome.reason == plan_mutations.REASON_READINESS
    assert "training_limitations" in outcome.missing

    active = await db.fetch_one(
        "SELECT plan_id FROM active_plans WHERE user_id=1 AND plan_type='workout'"
    )
    old = await db.fetch_one("SELECT payload FROM plan_versions WHERE id=?", (active["plan_id"],))
    assert json.loads(old["payload"])["sessions"][0]["weekday"] == 5, (
        "a blocked realignment must leave the active plan exactly as it was"
    )


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_the_realignment_is_audited_with_scalars_only(
    tmp_path, monkeypatch
) -> None:
    """Recoverable, and free of the user's programme.

    The audit allowlist keeps scalars and drops lists silently, so weekday sets
    are encoded as short strings. A list would vanish while this test still
    passed if it only asserted the row existed — hence the value assertions.
    """
    db = await _db(tmp_path)
    _bind_audit(monkeypatch, db)

    async def _ok(*_a, **_k):
        return {}

    monkeypatch.setattr(planning, "_require_readiness", _ok)
    await _active_plan(db, [5])

    outcome = await plan_mutations.realign_saved_plan_to_weekdays(
        db, 1, [4], reason="schedule_correction"
    )
    assert outcome.outcome == plan_mutations.OUTCOME_REALIGNED

    row = await db.fetch_one(
        "SELECT details FROM audit WHERE user_id=1 AND action='realign_weekdays'"
    )
    assert row is not None, "a plan change must leave an audit trail"
    details = json.loads(row["details"])

    assert details["outcome"] == plan_mutations.OUTCOME_REALIGNED
    assert details["before_days"] == "5"
    assert details["after_days"] == "4"
    assert details["new_plan_id"] == outcome.new_plan_id

    # The programme itself must never reach an audit row.
    serialized = json.dumps(details, ensure_ascii=False)
    assert "bench" not in serialized
    assert "exercises" not in serialized
    assert "sessions" not in serialized


@pytest.mark.asyncio
async def test_a_blocked_attempt_is_also_audited(tmp_path, monkeypatch) -> None:
    """Refusals are the rows an operator most needs and systems most often skip."""
    db = await _db(tmp_path)
    _bind_audit(monkeypatch, db)
    await _active_plan(db, [1, 4])

    await plan_mutations.realign_saved_plan_to_weekdays(db, 1, [1, 3, 5], reason="test")

    row = await db.fetch_one(
        "SELECT details FROM audit WHERE user_id=1 AND action='realign_weekdays'"
    )
    assert row is not None
    details = json.loads(row["details"])
    assert details["outcome"] == plan_mutations.OUTCOME_BLOCKED
    assert details["reason"] == plan_mutations.REASON_UNEXPRESSIBLE


@pytest.mark.asyncio
async def test_an_audit_failure_does_not_break_the_plan_change(
    tmp_path, monkeypatch, caplog
) -> None:
    """Recording is best-effort; the plan change the user asked for is not.

    The inverse — letting a logging fault roll back a successful mutation —
    would make observability actively harmful.
    """
    db = await _db(tmp_path)
    _bind_audit(monkeypatch, db)

    async def _ok(*_a, **_k):
        return {}

    monkeypatch.setattr(planning, "_require_readiness", _ok)

    async def _broken_audit(*_a, **_k):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(plan_mutations, "write_audit", _broken_audit)
    await _active_plan(db, [5])

    with caplog.at_level(logging.ERROR):
        outcome = await plan_mutations.realign_saved_plan_to_weekdays(
            db, 1, [4], reason="test"
        )

    assert outcome.outcome == plan_mutations.OUTCOME_REALIGNED, (
        "an audit failure must not roll back a plan change"
    )
    assert any("plan_realign_audit_failed" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Privacy
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_failure_logs_carry_ids_not_plan_contents(tmp_path, caplog) -> None:
    class _Broken:
        async def fetch_one(self, *_a, **_k):
            raise RuntimeError("bench 3x5 at 60kg -- payload that must not leak")

        async def fetch_all(self, *_a, **_k):
            raise RuntimeError("bench 3x5 at 60kg -- payload that must not leak")

    with caplog.at_level(logging.ERROR):
        await plan_mutations.realign_saved_plan_to_weekdays(_Broken(), 1, [4], reason="t")

    emitted = "\n".join(r.message for r in caplog.records)
    assert "user_id=1" in emitted
    assert "60kg" not in emitted, (
        "the log formats ids and bounded reason codes only; exception text must "
        "not be interpolated into it"
    )
