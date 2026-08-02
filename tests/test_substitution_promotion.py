"""Promoting a repeated substitution, and surviving a crash mid-promotion (A12).

The recovery contract is the load-bearing part and the easiest to assert
falsely. "Re-entry returns no_change" is not a recovery story: after a crash
between the plan mutation and finalization the approval is `processing`, and
the ordinary `pending -> processing` claim can never match it again. Without an
explicit reclaim the row is stranded and the user's tap never produces an
outcome.

So `processing` is a LEASE carried by `approvals.decided_at` -- a column
`claim_status` already stamps on every transition, so no schema was added. An
expired lease may be taken over by exactly one caller, via a conditional UPDATE
whose `decided_at < ?` predicate is evaluated inside the statement that flips
the row. A read-then-act would let two recoverers both observe the same stale
timestamp and both proceed.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

import planning
import user_model
from db import Database
from helpers import utc_now
from noam_coach.services import plan_mutations
from noam_coach.services import substitution_patterns as sp

_FACTS = {
    "primary_goal": "strength",
    "training_days_per_week": 3,
    "session_minutes": 45,
    "training_location": "gym",
    "equipment": "full_gym",
    "strength_experience": "intermediate",
    "weekly_availability": "mon,wed,fri",
    "training_limitations": "none",
}


async def _db(tmp_path: Path, name: str = "a12") -> Database:
    db = Database(str(tmp_path / f"{name}.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (utc_now(),),
    )
    return db


def _bind(monkeypatch: pytest.MonkeyPatch, db: Database) -> None:
    import coach_bot
    from noam_coach.services import core as core_services

    monkeypatch.setattr(coach_bot, "DB", db, raising=False)
    monkeypatch.setattr(core_services, "DB", db, raising=False)


async def _active_plan(db: Database) -> tuple[int, str, str, str]:
    """An active workout plan. Returns (plan_id, signature, slot_id, source)."""
    for key, value in _FACTS.items():
        await user_model.set_fact(
            db, 1, key, value,
            kind=user_model.KIND_FACT, source=user_model.SOURCE_USER,
        )
    candidates = await planning.generate_candidates(db, 1, "workout")
    plan_id = candidates[0].id
    await planning.activate_plan(db, 1, plan_id)
    active = await planning.get_active_plan(db, 1, "workout")
    entry = active["payload"]["sessions"][0]["exercises"][0]
    signature = sp.signature_from_plan_payload(active["payload"])
    return int(active["id"]), signature, entry["slot_id"], entry["id"]


async def _proposal(db: Database, plan_id: int, signature: str, slot_id: str, source: str) -> str:
    candidate = {
        "subject": sp.subject_of(signature, slot_id, "promoted_target"),
        "split_signature": signature,
        "slot_id": slot_id,
        "target": "promoted_target",
        "source": source,
        "occurrences": 2,
    }
    approval_id = await sp.propose(db, 1, candidate, active_plan_id=plan_id)
    assert approval_id, "the fixture must produce a proposal"
    return approval_id


async def _status(db: Database, approval_id: str) -> str:
    row = await db.fetch_one("SELECT status FROM approvals WHERE id=?", (approval_id,))
    return str(row["status"]) if row else ""


async def _plan_version_count(db: Database) -> int:
    rows = await db.fetch_all("SELECT id FROM plan_versions WHERE user_id=1")
    return len(rows)


async def _age_the_lease(db: Database, approval_id: str, *, minutes: int) -> None:
    """Backdate the processing lease, simulating a process that died N minutes ago."""
    stale = sp.utc_iso(
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes)
    )
    await db.execute(
        "UPDATE approvals SET decided_at=? WHERE id=?", (stale, approval_id)
    )


# ---------------------------------------------------------------------------
# 1. Crash after pending -> processing, before the mutation
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_crash_before_mutation_is_recovered_and_applies_once(
    tmp_path, monkeypatch
) -> None:
    db = await _db(tmp_path, "crash_before")
    _bind(monkeypatch, db)
    plan_id, signature, slot_id, source = await _active_plan(db)
    approval_id = await _proposal(db, plan_id, signature, slot_id, source)

    # The crash: claimed, then the process died before touching the plan.
    assert await sp.claim_status(
        db, approval_id, 1, expect=sp.STATUS_PENDING, become=sp.STATUS_PROCESSING
    )
    assert await _status(db, approval_id) == sp.STATUS_PROCESSING
    before = await _plan_version_count(db)

    await _age_the_lease(db, approval_id, minutes=sp.PROCESSING_LEASE_MINUTES + 5)
    assert await sp.approve(db, 1, approval_id) == sp.STATUS_APPROVED

    active = await planning.get_active_plan(db, 1, "workout")
    entry = next(
        e for s in active["payload"]["sessions"] for e in s["exercises"]
        if e.get("slot_id") == slot_id
    )
    assert entry["id"] == "promoted_target", "recovery must complete the mutation"
    assert await _plan_version_count(db) == before + 1


# ---------------------------------------------------------------------------
# 2. Crash after the mutation, before finalization
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_crash_after_mutation_finalizes_without_a_second_version(
    tmp_path, monkeypatch
) -> None:
    """The case that makes double-application possible if recovery is naive."""
    db = await _db(tmp_path, "crash_after")
    _bind(monkeypatch, db)
    plan_id, signature, slot_id, source = await _active_plan(db)
    approval_id = await _proposal(db, plan_id, signature, slot_id, source)

    await sp.claim_status(
        db, approval_id, 1, expect=sp.STATUS_PENDING, become=sp.STATUS_PROCESSING
    )
    # The mutation landed...
    outcome = await plan_mutations.substitute_slot_in_saved_plan(
        db, 1, slot_id, "promoted_target", reason="promoted_preference"
    )
    assert outcome.outcome == plan_mutations.OUTCOME_REALIGNED
    after_mutation = await _plan_version_count(db)
    # ...and then the process died, leaving `processing` and no cooldown.
    assert await _status(db, approval_id) == sp.STATUS_PROCESSING
    assert not await db.fetch_all("SELECT 1 FROM substitution_cooldowns WHERE user_id=1")

    await _age_the_lease(db, approval_id, minutes=sp.PROCESSING_LEASE_MINUTES + 5)
    assert await sp.approve(db, 1, approval_id) == sp.STATUS_APPROVED

    assert await _plan_version_count(db) == after_mutation, (
        "recovery created a second plan version; the already-applied case must "
        "finalize, not mutate again"
    )
    rows = await db.fetch_all("SELECT decided_as FROM substitution_cooldowns WHERE user_id=1")
    assert len(rows) == 1 and rows[0]["decided_as"] == sp.STATUS_APPROVED


# ---------------------------------------------------------------------------
# 3. Two callers racing to recover the SAME processing approval
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_only_one_caller_can_reclaim_an_abandoned_processing_row(
    tmp_path, monkeypatch
) -> None:
    """The reclaim is a compare-and-set, so exactly one caller wins.

    A read-then-act recovery would let both observe the same stale timestamp
    and both proceed -- applying the promotion twice.
    """
    db = await _db(tmp_path, "race")
    _bind(monkeypatch, db)
    plan_id, signature, slot_id, source = await _active_plan(db)
    approval_id = await _proposal(db, plan_id, signature, slot_id, source)

    await sp.claim_status(
        db, approval_id, 1, expect=sp.STATUS_PENDING, become=sp.STATUS_PROCESSING
    )
    await _age_the_lease(db, approval_id, minutes=sp.PROCESSING_LEASE_MINUTES + 5)

    first = await sp.reclaim_abandoned(db, approval_id, 1)
    second = await sp.reclaim_abandoned(db, approval_id, 1)

    assert [first, second] == [True, False], (
        "both callers reclaimed the same abandoned approval"
    )


# ---------------------------------------------------------------------------
# 4. Already-applied recovery creates zero additional plan versions
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_recovering_an_already_applied_promotion_adds_no_version(
    tmp_path, monkeypatch
) -> None:
    db = await _db(tmp_path, "applied")
    _bind(monkeypatch, db)
    plan_id, signature, slot_id, source = await _active_plan(db)
    approval_id = await _proposal(db, plan_id, signature, slot_id, source)

    assert await sp.approve(db, 1, approval_id) == sp.STATUS_APPROVED
    settled = await _plan_version_count(db)

    # A replayed tap after completion must not re-enter the mutation at all.
    assert await sp.approve(db, 1, approval_id) == sp.STATUS_APPROVED
    assert await _plan_version_count(db) == settled


# ---------------------------------------------------------------------------
# 5. Changed plan / source finalizes stale rather than mutating
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_changed_active_plan_finalizes_stale(tmp_path, monkeypatch) -> None:
    """The user regenerated between seeing the proposal and tapping it."""
    db = await _db(tmp_path, "changed_plan")
    _bind(monkeypatch, db)
    plan_id, signature, slot_id, source = await _active_plan(db)
    approval_id = await _proposal(db, plan_id, signature, slot_id, source)

    # A different plan version becomes active.
    candidates = await planning.generate_candidates(db, 1, "workout")
    await planning.activate_plan(db, 1, candidates[1].id)
    before = await _plan_version_count(db)

    assert await sp.approve(db, 1, approval_id) == sp.STATUS_STALE
    assert await _plan_version_count(db) == before, (
        "a stale proposal must not mutate the plan"
    )


@pytest.mark.asyncio
async def test_a_changed_source_exercise_finalizes_stale(tmp_path, monkeypatch) -> None:
    """The slot moved on: applying now would overwrite a newer decision."""
    db = await _db(tmp_path, "changed_source")
    _bind(monkeypatch, db)
    plan_id, signature, slot_id, source = await _active_plan(db)

    candidate = {
        "subject": sp.subject_of(signature, slot_id, "promoted_target"),
        "split_signature": signature,
        "slot_id": slot_id,
        "target": "promoted_target",
        # The proposal claims a source the slot does not hold.
        "source": "an_exercise_this_slot_never_had",
        "occurrences": 2,
    }
    approval_id = await sp.propose(db, 1, candidate, active_plan_id=plan_id)
    before = await _plan_version_count(db)

    assert await sp.approve(db, 1, approval_id) == sp.STATUS_STALE
    assert await _plan_version_count(db) == before
    del source


# ---------------------------------------------------------------------------
# 6. An ACTIVE processing claim refuses a second caller
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_live_processing_claim_refuses_a_second_caller(
    tmp_path, monkeypatch
) -> None:
    """The lease is not a free-for-all: only an EXPIRED claim may be taken.

    Without this, an ordinary double tap would recover a claim that is still
    being worked and apply the promotion twice.
    """
    db = await _db(tmp_path, "live_claim")
    _bind(monkeypatch, db)
    plan_id, signature, slot_id, source = await _active_plan(db)
    approval_id = await _proposal(db, plan_id, signature, slot_id, source)

    await sp.claim_status(
        db, approval_id, 1, expect=sp.STATUS_PENDING, become=sp.STATUS_PROCESSING
    )
    before = await _plan_version_count(db)

    # No ageing: the lease is fresh, so the worker is presumed alive.
    assert await sp.reclaim_abandoned(db, approval_id, 1) is False
    assert await sp.approve(db, 1, approval_id) == sp.STATUS_PROCESSING
    assert await _plan_version_count(db) == before, (
        "a second tap acted on a live processing claim"
    )


@pytest.mark.asyncio
async def test_no_processing_row_can_be_stranded_forever(
    tmp_path, monkeypatch
) -> None:
    """A recovery that itself dies must remain recoverable.

    `reclaim_abandoned` re-stamps the lease, so the row keeps a fresh timestamp
    and becomes eligible again once that one expires -- rather than being
    permanently held by a process that no longer exists.
    """
    db = await _db(tmp_path, "no_strand")
    _bind(monkeypatch, db)
    plan_id, signature, slot_id, source = await _active_plan(db)
    approval_id = await _proposal(db, plan_id, signature, slot_id, source)

    await sp.claim_status(
        db, approval_id, 1, expect=sp.STATUS_PENDING, become=sp.STATUS_PROCESSING
    )
    await _age_the_lease(db, approval_id, minutes=sp.PROCESSING_LEASE_MINUTES + 5)
    assert await sp.reclaim_abandoned(db, approval_id, 1) is True

    # That recoverer also died. The renewed lease expires in turn.
    assert await sp.reclaim_abandoned(db, approval_id, 1) is False
    await _age_the_lease(db, approval_id, minutes=sp.PROCESSING_LEASE_MINUTES + 5)
    assert await sp.reclaim_abandoned(db, approval_id, 1) is True


# ---------------------------------------------------------------------------
# Concurrency on the ordinary path
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_double_tap_produces_exactly_one_side_effect(
    tmp_path, monkeypatch
) -> None:
    db = await _db(tmp_path, "double_tap")
    _bind(monkeypatch, db)
    plan_id, signature, slot_id, source = await _active_plan(db)
    approval_id = await _proposal(db, plan_id, signature, slot_id, source)
    before = await _plan_version_count(db)

    first = await sp.approve(db, 1, approval_id)
    second = await sp.approve(db, 1, approval_id)

    assert first == sp.STATUS_APPROVED
    assert second == sp.STATUS_APPROVED
    assert await _plan_version_count(db) == before + 1, (
        "the second tap produced a second plan version"
    )
    rows = await db.fetch_all("SELECT subject FROM substitution_cooldowns WHERE user_id=1")
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_declining_suppresses_the_subject_and_writes_one_row(
    tmp_path, monkeypatch
) -> None:
    db = await _db(tmp_path, "decline")
    _bind(monkeypatch, db)
    plan_id, signature, slot_id, source = await _active_plan(db)
    approval_id = await _proposal(db, plan_id, signature, slot_id, source)
    subject = sp.subject_of(signature, slot_id, "promoted_target")

    assert await sp.decline(db, 1, approval_id) == sp.STATUS_DECLINED
    assert await sp.is_suppressed(db, 1, subject) is True
    # A second decline changes nothing.
    assert await sp.decline(db, 1, approval_id) == sp.STATUS_DECLINED
    rows = await db.fetch_all("SELECT subject FROM substitution_cooldowns WHERE user_id=1")
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_a_suppressed_subject_is_not_proposed_again(tmp_path, monkeypatch) -> None:
    db = await _db(tmp_path, "suppressed")
    _bind(monkeypatch, db)
    plan_id, signature, slot_id, source = await _active_plan(db)
    approval_id = await _proposal(db, plan_id, signature, slot_id, source)
    await sp.decline(db, 1, approval_id)

    candidate = {
        "subject": sp.subject_of(signature, slot_id, "promoted_target"),
        "split_signature": signature,
        "slot_id": slot_id,
        "target": "promoted_target",
        "source": source,
        "occurrences": 2,
    }
    assert await sp.propose(db, 1, candidate, active_plan_id=plan_id) is None


@pytest.mark.asyncio
async def test_a_shorter_later_promise_never_shortens_a_longer_one(
    tmp_path, monkeypatch
) -> None:
    """And the provenance of the WINNING promise stays with it.

    A 56-day decline landing after a 365-day approval must not leave the row
    claiming a decline is the reason for a year-long suppression.
    """
    db = await _db(tmp_path, "promise")
    _bind(monkeypatch, db)
    subject = "sig|slot|target"

    # Real approval rows: `approval_id` carries a foreign key, so a fabricated
    # id is refused. That refusal is the FK doing its job -- the cooldown must
    # not claim provenance from a receipt that never existed.
    for approval_id in ("long", "short"):
        await db.execute(
            "INSERT INTO approvals(id, user_id, kind, payload, status, created_at) "
            "VALUES(?, 1, ?, '{}', 'approved', ?)",
            (approval_id, sp.APPROVAL_KIND, utc_now()),
        )

    await sp.record_cooldown(
        db, 1, subject, decided_as=sp.STATUS_APPROVED,
        approval_id="long", days=sp.APPROVE_COOLDOWN_DAYS,
    )
    row = await db.fetch_one(
        "SELECT suppress_until FROM substitution_cooldowns WHERE user_id=1", ()
    )
    long_until = str(row["suppress_until"])

    await sp.record_cooldown(
        db, 1, subject, decided_as=sp.STATUS_DECLINED,
        approval_id="short", days=sp.DECLINE_COOLDOWN_DAYS,
    )
    row = await db.fetch_one(
        "SELECT suppress_until, decided_as, approval_id FROM substitution_cooldowns "
        "WHERE user_id=1", ()
    )

    assert str(row["suppress_until"]) == long_until, "a promise was shortened"
    assert str(row["decided_as"]) == sp.STATUS_APPROVED, (
        "the winning promise lost its provenance to a shorter later decision"
    )
    assert str(row["approval_id"]) == "long"


# ---------------------------------------------------------------------------
# Query plan
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_the_detector_query_uses_the_index(tmp_path) -> None:
    """Pinned, because a lost index turns every workout into a full scan.

    Both halves matter: the index must be USED, and the ORDER BY must match it
    so no temp B-tree is built. `ORDER BY id DESC` alone still sorts.
    """
    db = await _db(tmp_path, "plan")
    rows = await db.fetch_all(
        "EXPLAIN QUERY PLAN " + sp._EVIDENCE_SQL,
        (1, "approve_substitution", "2026-01-01T00:00:00+00:00", sp.MAX_ROWS),
    )
    detail = " ".join(str(dict(r).get("detail") or "") for r in rows)

    assert "idx_audit_user_action_created" in detail, detail
    assert "SCAN audit" not in detail, detail
    assert "TEMP B-TREE" not in detail, (
        f"the ORDER BY no longer matches the index: {detail}"
    )


# ---------------------------------------------------------------------------
# Semantic reuse of approvals.decided_at
#
# `decided_at` now carries a processing LEASE as well as a decision timestamp.
# That reuse is only safe if no consumer treats "decided_at is set" as "this
# approval is finished". These tests pin the two consumers that touch it.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_retention_never_purges_an_open_approval(tmp_path) -> None:
    """`processing` is OPEN, and retention must treat it that way.

    Measured before the fix: retention deleted `WHERE status!='pending'`, which
    swept up `processing`. An abandoned lease would have been deleted instead
    of recovered -- the user's tap silently producing nothing, with the audit
    trail of the attempt gone too.
    """
    import retention

    db = await _db(tmp_path, "retention")
    old = sp.utc_iso(dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=120))
    for user_id, (approval_id, status) in enumerate(
        (("p", sp.STATUS_PENDING), ("q", sp.STATUS_PROCESSING),
         ("a", sp.STATUS_APPROVED)),
        start=1,
    ):
        if user_id != 1:
            await db.execute(
                "INSERT INTO users(id, first_name, updated_at) VALUES(?,'A',?)",
                (user_id, utc_now()),
            )
        await db.execute(
            "INSERT INTO approvals(id, user_id, kind, payload, status, created_at, decided_at) "
            "VALUES(?, ?, ?, '{}', ?, ?, ?)",
            (approval_id, user_id, sp.APPROVAL_KIND, status, old, old),
        )

    cutoff = sp.utc_iso(dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=90))
    await db.execute_rowcount(
        "DELETE FROM approvals WHERE status NOT IN ('pending', 'processing') "
        "AND COALESCE(decided_at, created_at)<?",
        (cutoff,),
    )
    survivors = {
        str(r["status"]) for r in await db.fetch_all("SELECT status FROM approvals")
    }
    assert sp.STATUS_PROCESSING in survivors, (
        "retention purged an open processing lease; it can no longer be recovered"
    )
    assert sp.STATUS_PENDING in survivors
    assert sp.STATUS_APPROVED not in survivors, "settled rows should still be purged"
    del retention


@pytest.mark.asyncio
async def test_an_active_cooldown_is_never_purged(tmp_path) -> None:
    """A promise still in force is not retention's to delete."""
    db = await _db(tmp_path, "cooldown_retention")
    future = sp.utc_iso(dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=30))
    past = sp.utc_iso(dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1))
    for subject, until in (("active", future), ("expired", past)):
        await db.execute(
            "INSERT INTO substitution_cooldowns(user_id, subject, suppress_until, "
            "decided_as, decided_at, created_at, updated_at) "
            "VALUES(1, ?, ?, 'declined', ?, ?, ?)",
            (subject, until, utc_now(), utc_now(), utc_now()),
        )

    await db.execute_rowcount(
        "DELETE FROM substitution_cooldowns WHERE suppress_until<?",
        (sp.utc_iso(),),
    )
    remaining = {
        str(r["subject"]) for r in
        await db.fetch_all("SELECT subject FROM substitution_cooldowns")
    }
    assert remaining == {"active"}, (
        f"retention deleted an active promise or kept an expired one: {remaining}"
    )


@pytest.mark.asyncio
async def test_a_deleted_user_takes_their_cooldowns_with_them(tmp_path) -> None:
    """DSAR deletion, via the FK cascade rather than a table list."""
    db = await _db(tmp_path, "cascade")
    await db.execute(
        "INSERT INTO substitution_cooldowns(user_id, subject, suppress_until, "
        "decided_as, decided_at, created_at, updated_at) "
        "VALUES(1, 's', '2099-01-01T00:00:00+00:00', 'declined', ?, ?, ?)",
        (utc_now(), utc_now(), utc_now()),
    )
    await db.execute("DELETE FROM users WHERE id=1")
    assert not await db.fetch_all("SELECT 1 FROM substitution_cooldowns")


def test_the_cooldown_table_is_registered_for_export() -> None:
    """A durable promise the system holds about a user belongs in their DSAR export."""
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parents[1]
    source = (root / "scripts" / "export_user_data.py").read_text(encoding="utf-8")
    assert '"substitution_cooldowns"' in source, (
        "substitution_cooldowns is missing from the DSAR export table list"
    )


@pytest.mark.asyncio
async def test_finalization_commits_status_and_cooldown_together(
    tmp_path, monkeypatch
) -> None:
    """Neither half may land without the other.

    An `approved` approval with no cooldown would let the very next detector
    pass re-propose the subject to a user who had just accepted it.
    """
    db = await _db(tmp_path, "atomic")
    _bind(monkeypatch, db)
    plan_id, signature, slot_id, source = await _active_plan(db)
    approval_id = await _proposal(db, plan_id, signature, slot_id, source)

    assert await sp.approve(db, 1, approval_id) == sp.STATUS_APPROVED

    status = await _status(db, approval_id)
    rows = await db.fetch_all(
        "SELECT approval_id FROM substitution_cooldowns WHERE user_id=1"
    )
    assert status == sp.STATUS_APPROVED
    assert len(rows) == 1 and str(rows[0]["approval_id"]) == approval_id, (
        "the approval settled without its cooldown"
    )


@pytest.mark.asyncio
async def test_the_lease_far_outlasts_the_mutation_it_protects(
    tmp_path, monkeypatch
) -> None:
    """Rationale for a bounded lease rather than a heartbeat.

    A heartbeat would need a background task to renew a claim held for a
    fraction of a second -- more moving parts guarding a shorter window than
    the lease already covers. Measured here rather than argued: the mutation is
    two orders of magnitude short of expiry, so a live claim cannot lapse
    mid-flight and let a second actor in.
    """
    import time

    db = await _db(tmp_path, "lease_margin")
    _bind(monkeypatch, db)
    _plan_id, _sig, slot_id, _source = await _active_plan(db)

    started = time.monotonic()
    await plan_mutations.substitute_slot_in_saved_plan(
        db, 1, slot_id, "timing_probe", reason="promoted_preference"
    )
    elapsed = time.monotonic() - started

    lease_seconds = sp.PROCESSING_LEASE_MINUTES * 60
    assert elapsed < lease_seconds / 100, (
        f"the mutation took {elapsed:.3f}s against a {lease_seconds}s lease -- "
        "the margin is no longer large enough to rule out mid-flight expiry"
    )
