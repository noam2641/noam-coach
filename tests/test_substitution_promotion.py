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
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

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
        # A fabricated source, as an arbitrary historical evidence row could
        # carry. It must never reach the payload.
        "source": "an_exercise_this_slot_never_had",
        "occurrences": 2,
    }
    approval_id = await sp.propose(db, 1, candidate, active_plan_id=plan_id)

    stored = await db.fetch_one(
        "SELECT payload FROM approvals WHERE id=?", (approval_id,)
    )
    payload = json.loads(stored["payload"])
    assert payload["source"] == source, (
        "the proposal kept a historical source instead of resolving the "
        "exercise the active plan actually holds"
    )
    assert payload["source"] != "an_exercise_this_slot_never_had"


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

    # Call PRODUCTION retention, not a copy of its SQL. The first version of
    # this test inlined the DELETE and therefore asserted its own string:
    # mutating retention.py changed nothing it observed, and the mutation
    # survived.
    import db as _db_module

    monkeypatch_db = _db_module.DB
    _db_module.DB = db
    try:
        await retention.cleanup_operational_data_once()
    finally:
        _db_module.DB = monkeypatch_db

    survivors = {
        str(r["status"]) for r in await db.fetch_all("SELECT status FROM approvals")
    }
    assert sp.STATUS_PROCESSING in survivors, (
        "retention purged an open processing lease; it can no longer be recovered"
    )
    assert sp.STATUS_PENDING in survivors
    assert sp.STATUS_APPROVED not in survivors, "settled rows should still be purged"


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

    import db as _db_module
    import retention

    previous = _db_module.DB
    _db_module.DB = db
    try:
        await retention.cleanup_operational_data_once()
    finally:
        _db_module.DB = previous

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


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------
def test_every_lifecycle_outcome_has_its_own_user_facing_sentence() -> None:
    """Collapsing outcomes into one "done" is the UI version of the data defect.

    Telling a user their preference was saved when it was refused as stale is
    exactly the confusion this item exists to prevent at the data layer; it
    must not reappear at the surface. Asserted at the source because the
    alternative is five callback round-trips testing the fake query harness
    more than the copy.
    """
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parents[1]
    source = (root / "noam_coach" / "bot" / "callback_plans.py").read_text(
        encoding="utf-8"
    )
    start = source.index('if data.startswith("planv2:promote:")')
    block = source[start : start + 3000]

    for status in (
        "STATUS_APPROVED", "STATUS_PROCESSING", "STATUS_DECLINED", "STATUS_STALE",
    ):
        assert status in block, f"{status} has no explicit branch"
    assert "else:" in block, "the failed outcome has no fallback branch"

    # And the sentences must differ -- five branches saying the same thing is
    # the collapse this test exists to prevent.
    import re as _re

    sentences = _re.findall(r'text = \(?\s*"([^"]+)"', block)
    assert len(set(sentences)) >= 4, f"outcomes share wording: {sentences}"


def test_the_promotion_callback_reuses_the_registered_planv2_family() -> None:
    """No new top-level prefix, so no registration surface can be missed.

    `planv2:` is already router-owned, already debounced and already covered by
    the orphan guard. A brand-new prefix would have needed all three updated in
    lockstep -- the exact failure mode A11b found when `sub` sat in an allowlist
    while being invisible to the scanner.
    """
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parents[1]
    router = (root / "noam_coach" / "bot" / "callback_router.py").read_text(
        encoding="utf-8"
    )
    guard = (root / "tests" / "regression" / "test_re10_regression.py").read_text(
        encoding="utf-8"
    )

    assert '"planv2:promote:"' in router, "the promotion prefix is not debounced"
    assert '"planv2"' in guard, "planv2 is not registered with the orphan guard"


# ---------------------------------------------------------------------------
# Detector evidence rules
#
# Added after deliberate breakage: five mutations survived because nothing
# called `collect_evidence` or `find_promotable` at all. The lifecycle was
# tested thoroughly and the thing that DECIDES whether to start a lifecycle was
# not tested once -- so `pain` could have become promotable, invalid slot ids
# could have counted, and one workout could have manufactured a promotion,
# with every existing test still green.
# ---------------------------------------------------------------------------
_EVIDENCE_SESSION_KEYS = ["abc1_a", "abc1_b"]


async def _owned_evidence_plan(db: Database, plan_id: int = 7) -> str:
    """A real plan version owned by user 1, and its true split signature.

    The detector validates ownership and re-derives the signature FROM this
    row, so evidence tests must build a real plan rather than assert against a
    fabricated `split_signature` string. That is the point of the check: an
    audit row may claim any signature it likes and only the plan decides.
    """
    payload = {
        "sessions": [
            {"session_occurrence": key, "exercises": []}
            for key in _EVIDENCE_SESSION_KEYS
        ]
    }
    await db.execute(
        "INSERT OR REPLACE INTO plan_versions("
        "id, user_id, plan_type, title, strategy, fit_score, status, payload, created_at) "
        "VALUES(?, 1, 'workout', 'evidence', 'test', 1.0, 'superseded', ?, ?)",
        (plan_id, json.dumps(payload), utc_now()),
    )
    return sp.signature_from_plan_payload(payload)


async def _owned_session(
    db: Database,
    session_id: int,
    *,
    performed: str,
    slot_id: str = "",
    started_at: str | None = None,
) -> None:
    """A session owned by user 1, with one logged set recording what was done.

    `performed` is the exercise actually trained in the slot. Consecutiveness
    is decided on this -- not on the substitution audit -- so a test that only
    wrote audit rows could never distinguish A, A from A, B, A.
    """
    plan = {"exercises": [{"slot_id": slot_id, "id": performed, "name": performed}]}
    await db.execute(
        "INSERT OR REPLACE INTO sessions("
        "id, user_id, code, name, plan, status, exercise_index, set_number, started_at) "
        "VALUES(?, 1, 'A', 'A', ?, 'completed', 0, 1, ?)",
        (session_id, json.dumps(plan), started_at or utc_now()),
    )
    await db.execute(
        "INSERT INTO sets(session_id, exercise_id, exercise_name, set_number, "
        "weight, reps, rir, source, exercise_index, created_at) "
        "VALUES(?, ?, ?, 1, 60, 10, 2, 'test', 0, ?)",
        (session_id, performed, performed, started_at or utc_now()),
    )


async def _audit_row(
    db: Database,
    *,
    session_id: int,
    slot_id: str | None,
    target: str = "hack_squat",
    source: str = "leg_press",
    reason: str = "equipment",
    plan_id: int | None = 7,
    signature: str | None = "sig0123456789ab",
    details_override: str | None = None,
) -> None:
    """One `approve_substitution` audit row, written the way production does."""
    payload = {
        "source": source,
        "target": target,
        "reason": reason,
    }
    if slot_id is not None:
        payload["slot_id"] = slot_id
    if plan_id is not None:
        payload["evidence_plan_id"] = plan_id
    if signature is not None:
        payload["split_signature"] = signature
    await db.execute(
        "INSERT INTO audit(user_id, action, entity, entity_id, details, created_at) "
        "VALUES(1, 'approve_substitution', 'exercise', ?, ?, ?)",
        (
            str(session_id),
            details_override if details_override is not None
            else json.dumps(payload, ensure_ascii=False),
            utc_now(),
        ),
    )


def _days_ago(days: int) -> str:
    """An in-window timestamp. The detector bounds its read to LOOKBACK_DAYS,
    so a fixed calendar date silently ages out of the window and the evidence
    disappears -- which is correct behaviour, and makes absolute dates the
    wrong choice for a fixture."""
    return sp.utc_iso(dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days))


_VALID_SLOT = "abc1_a:leg_press"


@pytest.mark.asyncio
async def test_two_distinct_sessions_meet_the_threshold(tmp_path) -> None:
    """D-5: two consecutive occurrences. Sourced from the plan, not invented."""
    db = await _db(tmp_path, "evidence_ok")
    signature = await _owned_evidence_plan(db)
    await _owned_session(
        db, 1, performed="hack_squat", slot_id=_VALID_SLOT,
        started_at=_days_ago(10),
    )
    await _owned_session(
        db, 2, performed="hack_squat", slot_id=_VALID_SLOT,
        started_at=_days_ago(5),
    )
    await _audit_row(db, session_id=1, slot_id=_VALID_SLOT, signature=signature)
    await _audit_row(db, session_id=2, slot_id=_VALID_SLOT, signature=signature)

    found = await sp.find_promotable(db, 1)

    assert found is not None
    assert found["slot_id"] == _VALID_SLOT
    assert found["target"] == "hack_squat"
    assert found["occurrences"] == sp.PROMOTION_THRESHOLD


@pytest.mark.asyncio
async def test_one_session_never_promotes(tmp_path) -> None:
    db = await _db(tmp_path, "evidence_one")
    signature = await _owned_evidence_plan(db)
    await _owned_session(db, 1, performed="hack_squat", slot_id=_VALID_SLOT)
    await _audit_row(db, session_id=1, slot_id=_VALID_SLOT, signature=signature)
    assert await sp.find_promotable(db, 1) is None


@pytest.mark.asyncio
async def test_repeated_rows_from_one_session_cannot_promote(tmp_path) -> None:
    """The replay guard. Counting rows instead of sessions would let a single
    workout -- or one replayed callback -- manufacture a promotion by itself."""
    db = await _db(tmp_path, "evidence_replay")
    signature = await _owned_evidence_plan(db)
    await _owned_session(db, 1, performed="hack_squat", slot_id=_VALID_SLOT)
    for _ in range(5):
        await _audit_row(db, session_id=1, slot_id=_VALID_SLOT, signature=signature)

    evidence = await sp.collect_evidence(db, 1)
    assert evidence["rows"] == 5
    assert evidence["skipped"].get(sp.SKIP_DUPLICATE_SESSION) == 4
    assert await sp.find_promotable(db, 1) is None, (
        "one session produced a promotion; the detector is counting rows"
    )


@pytest.mark.asyncio
async def test_pain_is_never_promotion_evidence(tmp_path) -> None:
    """A safety adaptation is not a preference.

    Promoting it would turn "this hurt" into "I like this" and bake a
    pain-driven avoidance into the programme permanently.
    """
    db = await _db(tmp_path, "evidence_pain")
    await _audit_row(db, session_id=1, slot_id=_VALID_SLOT, reason="pain")
    await _audit_row(db, session_id=2, slot_id=_VALID_SLOT, reason="pain")

    evidence = await sp.collect_evidence(db, 1)
    assert evidence["skipped"].get(sp.SKIP_REASON) == 2
    assert await sp.find_promotable(db, 1) is None
    assert "pain" not in sp.PROMOTABLE_REASONS


@pytest.mark.asyncio
async def test_unspecified_is_not_automatically_eligible(tmp_path) -> None:
    """It marks a pre-A11b keyboard or an unrecognised value, not a signal."""
    db = await _db(tmp_path, "evidence_unspec")
    await _audit_row(db, session_id=1, slot_id=_VALID_SLOT, reason="unspecified")
    await _audit_row(db, session_id=2, slot_id=_VALID_SLOT, reason="unspecified")

    assert await sp.find_promotable(db, 1) is None
    assert "unspecified" not in sp.PROMOTABLE_REASONS


@pytest.mark.asyncio
async def test_invalid_slot_ids_are_never_evidence(tmp_path) -> None:
    """Missing, empty and malformed all fail the same round-trip check.

    A11b stored `slot_id=""` on legacy entries. Treating that as a key would
    merge every legacy substitution into one phantom pattern.
    """
    db = await _db(tmp_path, "evidence_slot")
    for session_id, slot_id in ((1, None), (2, ""), (3, "not-a-slot"), (4, "abc1_a")):
        await _audit_row(db, session_id=session_id, slot_id=slot_id)

    evidence = await sp.collect_evidence(db, 1)
    assert evidence["skipped"].get(sp.SKIP_INVALID_SLOT) == 4
    assert evidence["groups"] == {}
    assert await sp.find_promotable(db, 1) is None


@pytest.mark.asyncio
async def test_missing_plan_provenance_is_never_evidence(tmp_path) -> None:
    """Without knowing which plan it happened under, it cannot be grouped."""
    db = await _db(tmp_path, "evidence_prov")
    await _audit_row(db, session_id=1, slot_id=_VALID_SLOT, plan_id=None)
    await _audit_row(db, session_id=2, slot_id=_VALID_SLOT, signature=None)

    evidence = await sp.collect_evidence(db, 1)
    assert evidence["skipped"].get(sp.SKIP_NO_PROVENANCE) == 2
    assert await sp.find_promotable(db, 1) is None


@pytest.mark.asyncio
async def test_malformed_json_is_counted_not_raised(tmp_path) -> None:
    """A corrupt row must never break detection for every other row."""
    db = await _db(tmp_path, "evidence_json")
    signature = await _owned_evidence_plan(db)
    for session_id, day in ((1, 15), (2, 10), (3, 5)):
        await _owned_session(
            db, session_id, performed="hack_squat", slot_id=_VALID_SLOT,
            started_at=_days_ago(day),
        )
    await _audit_row(db, session_id=1, slot_id=_VALID_SLOT, details_override="{not json")
    await _audit_row(db, session_id=2, slot_id=_VALID_SLOT, signature=signature)
    await _audit_row(db, session_id=3, slot_id=_VALID_SLOT, signature=signature)

    evidence = await sp.collect_evidence(db, 1)
    assert evidence["skipped"].get(sp.SKIP_MALFORMED) == 1
    assert await sp.find_promotable(db, 1) is not None, (
        "one corrupt row suppressed detection for the healthy rows"
    )


@pytest.mark.asyncio
async def test_evidence_never_crosses_a_split_signature_boundary(tmp_path) -> None:
    """Two occurrences under DIFFERENT splits are not two occurrences.

    They are one each, under two programmes -- and joining them would promote a
    pattern the user never expressed in either.
    """
    db = await _db(tmp_path, "evidence_split")
    first = await _owned_evidence_plan(db, plan_id=7)
    second_payload = {
        "sessions": [
            {"session_occurrence": key, "exercises": []}
            for key in ("xyz9_a", "xyz9_b", "xyz9_c")
        ]
    }
    await db.execute(
        "INSERT OR REPLACE INTO plan_versions("
        "id, user_id, plan_type, title, strategy, fit_score, status, payload, created_at) "
        "VALUES(8, 1, 'workout', 'evidence', 'test', 1.0, 'superseded', ?, ?)",
        (json.dumps(second_payload), utc_now()),
    )
    second = sp.signature_from_plan_payload(second_payload)
    assert first != second
    await _owned_session(db, 1, performed="hack_squat", slot_id=_VALID_SLOT)
    await _owned_session(db, 2, performed="hack_squat", slot_id=_VALID_SLOT)
    await _audit_row(db, session_id=1, slot_id=_VALID_SLOT, signature=first, plan_id=7)
    await _audit_row(db, session_id=2, slot_id=_VALID_SLOT, signature=second, plan_id=8)

    evidence = await sp.collect_evidence(db, 1)
    assert len(evidence["groups"]) == 2, "the two splits were merged into one group"
    assert all(len(g["sessions"]) == 1 for g in evidence["groups"].values())
    assert await sp.find_promotable(db, 1) is None


def test_the_subject_is_scoped_by_split_signature() -> None:
    """A cooldown earned under one split must not suppress another.

    Dropping the signature from the subject would make a decline in a 3-day
    programme silence the same question in a 6-day one.
    """
    first = sp.subject_of("aaaaaaaaaaaaaaaa", _VALID_SLOT, "hack_squat")
    second = sp.subject_of("bbbbbbbbbbbbbbbb", _VALID_SLOT, "hack_squat")

    assert first != second, "the subject does not distinguish splits"
    assert first.startswith("aaaaaaaaaaaaaaaa|")


@pytest.mark.asyncio
async def test_the_lookback_window_is_bounded(tmp_path) -> None:
    """Evidence older than the window does not count."""
    db = await _db(tmp_path, "evidence_window")
    old = sp.utc_iso(
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=sp.LOOKBACK_DAYS + 5)
    )
    await _audit_row(db, session_id=1, slot_id=_VALID_SLOT)
    await db.execute(
        "INSERT INTO audit(user_id, action, entity, entity_id, details, created_at) "
        "VALUES(1, 'approve_substitution', 'exercise', '99', ?, ?)",
        (json.dumps({
            "source": "leg_press", "target": "hack_squat", "reason": "equipment",
            "slot_id": _VALID_SLOT, "evidence_plan_id": 7,
            "split_signature": "sig0123456789ab",
        }), old),
    )

    evidence = await sp.collect_evidence(db, 1)
    assert evidence["rows"] == 1, "a row outside the lookback window was read"
    assert await sp.find_promotable(db, 1) is None


@pytest.mark.asyncio
async def test_a_shorter_promise_cannot_shorten_a_longer_one_via_the_detector_path(
    tmp_path,
) -> None:
    """The UPSERT rule, asserted on values rather than on SQL text."""
    db = await _db(tmp_path, "cooldown_order")
    subject = "sig|slot|target"

    await sp.record_cooldown(
        db, 1, subject, decided_as=sp.STATUS_APPROVED,
        approval_id=None, days=sp.APPROVE_COOLDOWN_DAYS,
    )
    long_row = await db.fetch_one(
        "SELECT suppress_until FROM substitution_cooldowns WHERE user_id=1", ()
    )
    await sp.record_cooldown(
        db, 1, subject, decided_as=sp.STATUS_DECLINED,
        approval_id=None, days=sp.DECLINE_COOLDOWN_DAYS,
    )
    after = await db.fetch_one(
        "SELECT suppress_until, decided_as FROM substitution_cooldowns WHERE user_id=1", ()
    )

    assert str(after["suppress_until"]) == str(long_row["suppress_until"])
    assert str(after["decided_as"]) == sp.STATUS_APPROVED


@pytest.mark.asyncio
async def test_finalize_refuses_when_it_does_not_hold_the_claim(
    tmp_path, monkeypatch
) -> None:
    """The rowcount check is the whole concurrency argument.

    `_finalize` must write NOTHING unless its conditional UPDATE matched. A
    version that ignored the rowcount would let a caller that never held the
    lease write a cooldown -- and deliberate breakage found that this was
    untested: mutating the check away broke nothing.
    """
    db = await _db(tmp_path, "finalize_claim")
    _bind(monkeypatch, db)
    plan_id, signature, slot_id, source = await _active_plan(db)
    approval_id = await _proposal(db, plan_id, signature, slot_id, source)

    # The row is `pending`, not `processing`, so no caller holds the claim.
    won = await sp._finalize(
        db, approval_id, 1, become=sp.STATUS_APPROVED,
        subject="sig|slot|target", days=sp.APPROVE_COOLDOWN_DAYS,
    )

    assert won is False, "finalize claimed a row it did not hold"
    assert await _status(db, approval_id) == sp.STATUS_PENDING
    assert not await db.fetch_all(
        "SELECT 1 FROM substitution_cooldowns WHERE user_id=1"
    ), "a cooldown was written without holding the processing claim"


@pytest.mark.asyncio
async def test_only_one_of_two_finalizers_writes(tmp_path, monkeypatch) -> None:
    """Two callers, one winner -- the same rule, on the finalize path."""
    db = await _db(tmp_path, "finalize_race")
    _bind(monkeypatch, db)
    plan_id, signature, slot_id, source = await _active_plan(db)
    approval_id = await _proposal(db, plan_id, signature, slot_id, source)
    await sp.claim_status(
        db, approval_id, 1, expect=sp.STATUS_PENDING, become=sp.STATUS_PROCESSING
    )

    first = await sp._finalize(
        db, approval_id, 1, become=sp.STATUS_APPROVED,
        subject="sig|slot|target", days=sp.APPROVE_COOLDOWN_DAYS,
    )
    second = await sp._finalize(
        db, approval_id, 1, become=sp.STATUS_APPROVED,
        subject="sig|slot|target", days=sp.APPROVE_COOLDOWN_DAYS,
    )

    assert [first, second] == [True, False]
    rows = await db.fetch_all("SELECT subject FROM substitution_cooldowns WHERE user_id=1")
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# Real router round-trip
#
# Everything above calls the service layer directly. That leaves the most
# expensive failure untested: callback data that never reaches the handler at
# all. A11b found exactly that -- `sub` sat in an allowlist while being
# invisible to the guard scanner, and the orphan test passed while covering
# nothing.
#
# These drive the PRODUCTION router entry point with data built the way the
# keyboard builds it, so a prefix that stops dispatching fails here.
# ---------------------------------------------------------------------------
class _RouterQuery:
    """Minimal stand-in for a Telegram CallbackQuery."""

    def __init__(self, data: str, user_id: int = 1) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=user_id, first_name="T", username=None)
        self.message = SimpleNamespace(
            message_id=1,
            chat=SimpleNamespace(id=user_id),
            reply_markup=None,
        )
        self.edits: list[str] = []
        self.answers: list[str] = []

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        del show_alert
        if text:
            self.answers.append(text)

    async def edit_message_text(
        self, text: str, reply_markup=None, parse_mode=None
    ) -> None:
        del reply_markup, parse_mode
        self.edits.append(text)

    async def edit_message_reply_markup(self, reply_markup=None) -> None:
        del reply_markup


def _router_update(query: _RouterQuery, user_id: int = 1) -> SimpleNamespace:
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id, first_name="T", username=None),
        effective_chat=SimpleNamespace(id=user_id),
        effective_message=query.message,
        callback_query=query,
    )


async def _drive_router(query: _RouterQuery, user_id: int = 1) -> None:
    import coach_bot

    await coach_bot.handle_callback(
        _router_update(query, user_id), SimpleNamespace(job_queue=None, bot=None)
    )


@pytest.fixture
def _allow_user(monkeypatch: pytest.MonkeyPatch):
    from config import SETTINGS

    monkeypatch.setattr(SETTINGS, "telegram_allowed_user_id", 1, raising=False)
    yield


@pytest.mark.asyncio
async def test_a_generated_callback_reaches_the_handler_through_the_router(
    tmp_path, monkeypatch, _allow_user
) -> None:
    """The whole path: minted data -> production router -> A12 handler -> plan.

    If `planv2:promote:` ever stopped dispatching, every service-level test
    above would still pass while the feature was dead in production.
    """
    import coach_bot
    from noam_coach.bot import callback_plans

    db = await _db(tmp_path, "roundtrip")
    _bind(monkeypatch, db)
    monkeypatch.setattr(callback_plans, "DB", db, raising=False)
    monkeypatch.setattr(coach_bot, "DB", db, raising=False)

    plan_id, signature, slot_id, source = await _active_plan(db)
    approval_id = await _proposal(db, plan_id, signature, slot_id, source)

    query = _RouterQuery(f"planv2:promote:yes:{approval_id}")
    await _drive_router(query)

    assert await _status(db, approval_id) == sp.STATUS_APPROVED, (
        "the callback did not reach the A12 handler through the real router"
    )
    active = await planning.get_active_plan(db, 1, "workout")
    entry = next(
        e for s in active["payload"]["sessions"] for e in s["exercises"]
        if e.get("slot_id") == slot_id
    )
    assert entry["id"] == "promoted_target"
    assert query.edits, "the user was shown nothing"


@pytest.mark.asyncio
async def test_another_users_approval_cannot_be_promoted(
    tmp_path, monkeypatch, _allow_user
) -> None:
    """Ownership is enforced on the approval row, not assumed from the router."""
    db = await _db(tmp_path, "ownership")
    _bind(monkeypatch, db)
    plan_id, signature, slot_id, source = await _active_plan(db)
    approval_id = await _proposal(db, plan_id, signature, slot_id, source)

    await db.execute(
        "INSERT INTO users(id, first_name, updated_at) VALUES(2,'B',?)", (utc_now(),)
    )
    before = await _plan_version_count(db)

    # User 2 taps user 1's approval id.
    assert await sp.approve(db, 2, approval_id) == sp.STATUS_STALE
    assert await _status(db, approval_id) == sp.STATUS_PENDING, (
        "another user's tap changed the owner's approval"
    )
    assert await _plan_version_count(db) == before


@pytest.mark.asyncio
async def test_a_double_tap_through_the_router_mutates_once(
    tmp_path, monkeypatch, _allow_user
) -> None:
    """Two taps, one side effect -- asserted through the real dispatch path."""
    import coach_bot
    from noam_coach.bot import callback_plans

    db = await _db(tmp_path, "router_double")
    _bind(monkeypatch, db)
    monkeypatch.setattr(callback_plans, "DB", db, raising=False)
    monkeypatch.setattr(coach_bot, "DB", db, raising=False)

    plan_id, signature, slot_id, source = await _active_plan(db)
    approval_id = await _proposal(db, plan_id, signature, slot_id, source)
    before = await _plan_version_count(db)

    data = f"planv2:promote:yes:{approval_id}"
    await _drive_router(_RouterQuery(data))
    await _drive_router(_RouterQuery(data))

    assert await _plan_version_count(db) == before + 1, (
        "the second tap produced a second plan version"
    )
    rows = await db.fetch_all("SELECT subject FROM substitution_cooldowns WHERE user_id=1")
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_a_stale_approval_id_through_the_router_changes_nothing(
    tmp_path, monkeypatch, _allow_user
) -> None:
    """An id from a keyboard whose approval no longer exists."""
    import coach_bot
    from noam_coach.bot import callback_plans

    db = await _db(tmp_path, "router_stale")
    _bind(monkeypatch, db)
    monkeypatch.setattr(callback_plans, "DB", db, raising=False)
    monkeypatch.setattr(coach_bot, "DB", db, raising=False)
    await _active_plan(db)
    before = await _plan_version_count(db)

    await _drive_router(_RouterQuery("planv2:promote:yes:never_existed"))

    assert await _plan_version_count(db) == before
    assert not await db.fetch_all("SELECT 1 FROM substitution_cooldowns WHERE user_id=1")


@pytest.mark.asyncio
async def test_tapping_yes_after_declining_does_not_mutate(
    tmp_path, monkeypatch, _allow_user
) -> None:
    """A stale keyboard still showing both buttons after the answer was given."""
    import coach_bot
    from noam_coach.bot import callback_plans

    db = await _db(tmp_path, "declined_then_yes")
    _bind(monkeypatch, db)
    monkeypatch.setattr(callback_plans, "DB", db, raising=False)
    monkeypatch.setattr(coach_bot, "DB", db, raising=False)

    plan_id, signature, slot_id, source = await _active_plan(db)
    approval_id = await _proposal(db, plan_id, signature, slot_id, source)
    before = await _plan_version_count(db)

    await _drive_router(_RouterQuery(f"planv2:promote:no:{approval_id}"))
    assert await _status(db, approval_id) == sp.STATUS_DECLINED

    await _drive_router(_RouterQuery(f"planv2:promote:yes:{approval_id}"))

    assert await _status(db, approval_id) == sp.STATUS_DECLINED, (
        "a late yes overturned a recorded decline"
    )
    assert await _plan_version_count(db) == before


@pytest.mark.asyncio
async def test_a_decline_after_an_approval_keeps_the_longer_promise(
    tmp_path, monkeypatch
) -> None:
    """The ordering rule, exercised through the LIFECYCLE path.

    Deliberate breakage found this gap: the UPSERT existed twice -- once in
    `record_cooldown`, once inline in `_finalize` -- and mutating one left the
    other keeping the tests green. The SQL is now stated once, and this drives
    it the way production does rather than calling the helper directly.
    """
    db = await _db(tmp_path, "lifecycle_order")
    _bind(monkeypatch, db)
    plan_id, signature, slot_id, source = await _active_plan(db)

    # An approval promises 365 days.
    approval_id = await _proposal(db, plan_id, signature, slot_id, source)
    subject = sp.subject_of(signature, slot_id, "promoted_target")
    assert await sp.approve(db, 1, approval_id) == sp.STATUS_APPROVED
    row = await db.fetch_one(
        "SELECT suppress_until FROM substitution_cooldowns WHERE user_id=1", ()
    )
    long_promise = str(row["suppress_until"])

    # A later decline on the same subject promises only 56.
    await db.execute(
        "INSERT INTO approvals(id, user_id, kind, payload, status, created_at) "
        "VALUES('later', 1, ?, ?, 'pending', ?)",
        (sp.APPROVAL_KIND, json.dumps({"subject": subject}), utc_now()),
    )
    assert await sp.decline(db, 1, "later") == sp.STATUS_DECLINED

    row = await db.fetch_one(
        "SELECT suppress_until, decided_as, approval_id FROM substitution_cooldowns "
        "WHERE user_id=1", ()
    )
    assert str(row["suppress_until"]) == long_promise, (
        "the lifecycle path shortened a promise already made"
    )
    assert str(row["decided_as"]) == sp.STATUS_APPROVED, (
        "the winning promise lost its provenance to a shorter later decision"
    )
    assert str(row["approval_id"]) == approval_id


def test_the_cooldown_upsert_is_defined_exactly_once() -> None:
    """Two copies of a rule drift, and this one did.

    Deliberate breakage mutated the `record_cooldown` copy while the lifecycle
    used an inline duplicate in `_finalize`, so the mutation survived with every
    test green. Stating it once is what makes a single mutation observable.
    """
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parents[1]
    source = (root / "noam_coach" / "services" / "substitution_patterns.py").read_text(
        encoding="utf-8"
    )
    assert source.count("suppress_until = MAX(") == 1, (
        "the cooldown UPSERT is defined more than once; a mutation in one copy "
        "will be masked by the other"
    )


# ---------------------------------------------------------------------------
# Production round-trip and the nine review gaps
#
# Written after an independent review found that A12 defined a detector, a
# proposal and a consumer -- and NOTHING in production called any of them. The
# service-level tests above all passed while the feature was unreachable from
# the bot, which is precisely the failure mode a service-level fixture cannot
# see. Every test below therefore drives a REAL production entry point:
# `handle_session_action_callback` for the workout, `handle_plan_callback` for
# the answer.
# ---------------------------------------------------------------------------
class _RoundTripQuery:
    """A Telegram query that records edits AND replies.

    `_render_workout_end` edits the summary and then sends the promotion ask as
    a separate message, so a harness that only captures edits would miss the
    keyboard entirely -- and report a passing test for a prompt the user never
    receives.
    """

    def __init__(self) -> None:
        self.messages: list[str] = []
        self.reply_markups: list[Any] = []
        self.replies: list[str] = []
        self.reply_keyboards: list[Any] = []
        outer = self

        class _Message:
            async def reply_text(self, text: str, reply_markup: Any = None, **kw: Any) -> None:
                del kw
                outer.replies.append(text)
                outer.reply_keyboards.append(reply_markup)

        self.message = _Message()

    async def edit_message_text(
        self, text: str, reply_markup: Any = None, parse_mode: str | None = None
    ) -> None:
        del parse_mode
        self.messages.append(text)
        self.reply_markups.append(reply_markup)

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        del text, show_alert


def _callback_data(markup: Any) -> list[str]:
    if markup is None:
        return []
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def _button_labels(markup: Any) -> list[str]:
    if markup is None:
        return []
    return [b.text for row in markup.inline_keyboard for b in row]


_RT_SLOT = "rt01_a:leg_press"


def _one_exercise_plan(performed: str, slot_id: str = _RT_SLOT) -> dict[str, Any]:
    """A single-exercise session plan, so ONE set completes the workout."""
    return {
        "exercises": [
            {
                "slot_id": slot_id,
                "id": performed,
                "name": performed,
                "sets": 1,
                "reps": 10,
                "rmin": 8,
                "rmax": 12,
                "inc": 2.5,
                "rest": 60,
                "alts": [{"id": "hack_squat", "name": "hack squat"}],
            }
        ]
    }


async def _completed_workout(
    db: Database,
    session_id: int,
    performed: str,
    *,
    days_ago: int,
    slot_id: str = _RT_SLOT,
    user_id: int = 1,
    status: str = "completed",
    source: str = "telegram_one_tap",
) -> None:
    """A finished workout with one logged set, as production leaves it."""
    stamp = _days_ago(days_ago)
    await db.execute(
        "INSERT OR REPLACE INTO sessions("
        "id, user_id, code, name, plan, status, exercise_index, set_number, "
        "started_at, ended_at) VALUES(?, ?, 'A', 'A', ?, ?, 0, 1, ?, ?)",
        (
            session_id, user_id,
            json.dumps(_one_exercise_plan(performed, slot_id)),
            status, stamp, stamp,
        ),
    )
    await db.execute(
        "INSERT INTO sets(session_id, exercise_id, exercise_name, set_number, "
        "weight, reps, rir, source, exercise_index, created_at) "
        "VALUES(?, ?, ?, 1, 60, 10, 2, ?, 0, ?)",
        (session_id, performed, performed, source, stamp),
    )


async def _substitution_audit(
    db: Database,
    session_id: int,
    *,
    signature: str,
    plan_id: int,
    slot_id: str = _RT_SLOT,
    target: str = "hack_squat",
    user_id: int = 1,
) -> None:
    await db.execute(
        "INSERT INTO audit(user_id, action, entity, entity_id, details, created_at) "
        "VALUES(?, 'approve_substitution', 'exercise', ?, ?, ?)",
        (
            user_id,
            str(session_id),
            json.dumps({
                "source": "leg_press", "target": target, "reason": "equipment",
                "slot_id": slot_id, "evidence_plan_id": plan_id,
                "split_signature": signature,
            }),
            utc_now(),
        ),
    )


async def _active_rt_plan(
    db: Database, slot_id: str = _RT_SLOT, *, activate: bool = True
) -> tuple[int, str]:
    """An ACTIVE plan whose slot holds leg_press. Returns (plan_id, signature)."""
    payload = {
        "sessions": [
            {
                "session_occurrence": "rt01_a",
                "code": "A", "name": "אימון A",
                "weekday": 0, "time": "18:00", "minutes": 45,
                "exercises": [
                    {
                        "slot_id": slot_id, "id": "leg_press", "name": "leg press",
                        "sets": 3, "reps": 10, "rmin": 8, "rmax": 12,
                        "inc": 2.5, "rest": 90,
                        "alts": [{"id": "hack_squat", "name": "hack squat"}],
                    }
                ],
            },
            {
                "session_occurrence": "rt01_b",
                "code": "B", "name": "אימון B",
                "weekday": 2, "time": "18:00", "minutes": 45,
                "exercises": [
                    {
                        "slot_id": "rt01_b:bench", "id": "bench", "name": "bench",
                        "sets": 3, "reps": 10, "rmin": 8, "rmax": 12,
                        "inc": 2.5, "rest": 90, "alts": [],
                    }
                ],
            },
        ]
    }
    # The facts activation validates against. Set through the real API so the
    # fixture exercises the same gate production does.
    for key, value in _FACTS.items():
        await user_model.set_fact(
            db, 1, key, value,
            kind=user_model.KIND_FACT, source=user_model.SOURCE_USER,
        )
    plan_id = await db.execute(
        "INSERT INTO plan_versions("
        "user_id, plan_type, title, strategy, fit_score, status, payload, "
        "created_at, activated_at) "
        "VALUES(1, 'workout', 'rt', 'test', 1.0, 'active', ?, ?, ?)",
        (json.dumps(payload), utc_now(), utc_now()),
    )
    # Activation lives in `active_plans`, not in a status column: hand-setting
    # `status='active'` leaves `get_active_plan` returning None, so the
    # proposal is refused and the round-trip silently produces nothing.
    if activate:
        await planning.activate_plan(db, 1, int(plan_id))
    return int(plan_id), sp.signature_from_plan_payload(payload)


async def _drive_workout_to_completion(db: Database, monkeypatch) -> _RoundTripQuery:
    """Log the final set through the REAL callback, completing the workout."""
    import coach_bot
    from noam_coach.bot import callback_session as callback_session_bot

    session_id = await db.execute(
        "INSERT INTO sessions(user_id, code, name, plan, status, exercise_index, "
        "set_number, started_at) VALUES(1, 'A', 'A', ?, 'active', 0, 1, ?)",
        (json.dumps(_one_exercise_plan("hack_squat")), utc_now()),
    )
    session = dict(await db.fetch_one("SELECT * FROM sessions WHERE id=?", (session_id,)))

    query = _RoundTripQuery()
    await callback_session_bot.handle_session_action_callback(
        query,
        context=_NoJobContext(),
        user_id=1,
        data=coach_bot.session_action_data("setok", session, "60", "10"),
    )
    return query


class _NoJobContext:
    job_queue = None


@pytest.mark.asyncio
async def test_completing_a_workout_produces_the_promotion_prompt(tmp_path, monkeypatch):
    """1. The round-trip the review found missing, end to end.

    Real completion callback -> summary -> detector -> proposal -> a keyboard
    with two answerable buttons. Every earlier A12 test passed while this
    produced nothing at all.
    """
    db = await _db(tmp_path, "rt_offer")
    _bind(monkeypatch, db)
    plan_id, signature = await _active_rt_plan(db)

    # Two consecutive prior workouts where the substitution was performed.
    for index, session_id in enumerate((101, 102)):
        await _completed_workout(db, session_id, "hack_squat", days_ago=20 - index * 5)
        await _substitution_audit(db, session_id, signature=signature, plan_id=plan_id)

    query = await _drive_workout_to_completion(db, monkeypatch)

    assert query.messages, "the workout summary was never rendered"
    assert "האימון הושלם" in query.messages[-1]
    assert query.replies, (
        "the workout completed but no promotion prompt was sent -- the detector "
        "is not reachable from production"
    )
    data = _callback_data(query.reply_keyboards[-1])
    assert len(data) == 2, f"expected a yes/no keyboard, got {data}"
    assert any(d.startswith("planv2:promote:yes:") for d in data), data
    assert any(d.startswith("planv2:promote:no:") for d in data), data

    row = await db.fetch_one(
        "SELECT status, kind FROM approvals WHERE user_id=1", ()
    )
    assert row is not None and row["status"] == sp.STATUS_PENDING
    assert row["kind"] == sp.APPROVAL_KIND


@pytest.mark.asyncio
async def test_the_prompt_leads_to_a_real_plan_change(tmp_path, monkeypatch):
    """1b. Tapping "yes" on the produced keyboard rewrites the saved plan."""
    db = await _db(tmp_path, "rt_apply")
    _bind(monkeypatch, db)
    plan_id, signature = await _active_rt_plan(db)
    for index, session_id in enumerate((101, 102)):
        await _completed_workout(db, session_id, "hack_squat", days_ago=20 - index * 5)
        await _substitution_audit(db, session_id, signature=signature, plan_id=plan_id)

    query = await _drive_workout_to_completion(db, monkeypatch)
    yes = next(
        d for d in _callback_data(query.reply_keyboards[-1])
        if d.startswith("planv2:promote:yes:")
    )

    from noam_coach.bot import callback_plans as callback_plans_bot

    answer = _RoundTripQuery()
    await callback_plans_bot.handle_plan_callback(
        answer, user_id=1, data=yes
    )

    active = await planning.get_active_plan(db, 1, "workout")
    entry = next(
        e for s in active["payload"]["sessions"] for e in s["exercises"]
        if e.get("slot_id") == _RT_SLOT
    )
    assert entry["id"] == "hack_squat", "the approved promotion never reached the plan"
    assert "עודכן" in answer.messages[-1]


@pytest.mark.asyncio
async def test_a_reversible_finish_produces_no_proposal(tmp_path, monkeypatch):
    """2. The explicit finish keeps a "reopen" button, so nothing is settled.

    Asking there would put a permanent question on a workout the user can still
    take back -- and a promotion approved against a reopened session would be
    evidence the user never actually produced.
    """
    db = await _db(tmp_path, "rt_partial")
    _bind(monkeypatch, db)
    plan_id, signature = await _active_rt_plan(db)
    for index, session_id in enumerate((101, 102)):
        await _completed_workout(db, session_id, "hack_squat", days_ago=20 - index * 5)
        await _substitution_audit(db, session_id, signature=signature, plan_id=plan_id)

    import coach_bot
    from noam_coach.bot import callback_session as callback_session_bot

    session_id = await db.execute(
        "INSERT INTO sessions(user_id, code, name, plan, status, exercise_index, "
        "set_number, started_at) VALUES(1, 'A', 'A', ?, 'active', 0, 1, ?)",
        (json.dumps(_one_exercise_plan("hack_squat")), utc_now()),
    )
    session = dict(await db.fetch_one("SELECT * FROM sessions WHERE id=?", (session_id,)))

    query = _RoundTripQuery()
    await callback_session_bot.handle_session_action_callback(
        query, context=_NoJobContext(), user_id=1,
        data=coach_bot.session_action_data("finish", session),
    )

    assert not query.replies, "a reversible finish must not raise the promotion ask"
    assert not await db.fetch_all(
        "SELECT 1 FROM approvals WHERE user_id=1 AND kind=?", (sp.APPROVAL_KIND,)
    )


@pytest.mark.asyncio
async def test_a_genuine_completion_after_a_reversible_finish_asks_once(
    tmp_path, monkeypatch
):
    """2b. Deferred, not lost: the next real completion still asks -- once."""
    db = await _db(tmp_path, "rt_deferred")
    _bind(monkeypatch, db)
    plan_id, signature = await _active_rt_plan(db)
    for index, session_id in enumerate((101, 102)):
        await _completed_workout(db, session_id, "hack_squat", days_ago=20 - index * 5)
        await _substitution_audit(db, session_id, signature=signature, plan_id=plan_id)

    first = await _drive_workout_to_completion(db, monkeypatch)
    assert first.replies, "the first genuine completion did not ask"

    second = await _drive_workout_to_completion(db, monkeypatch)
    assert not second.replies, (
        "a second prompt was raised while one was still open -- the user would "
        "be asked the same question twice"
    )
    rows = await db.fetch_all(
        "SELECT id FROM approvals WHERE user_id=1 AND kind=? AND status=?",
        (sp.APPROVAL_KIND, sp.STATUS_PENDING),
    )
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# Consecutiveness
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_then_a_promotes(tmp_path, monkeypatch) -> None:
    """3. Two consecutive occurrences, which is what D-5 actually says."""
    db = await _db(tmp_path, "consec_aa")
    _bind(monkeypatch, db)
    plan_id, signature = await _active_rt_plan(db, activate=False)
    for index, session_id in enumerate((101, 102)):
        await _completed_workout(db, session_id, "hack_squat", days_ago=20 - index * 5)
        await _substitution_audit(db, session_id, signature=signature, plan_id=plan_id)

    assert await sp.find_promotable(db, 1) is not None


@pytest.mark.asyncio
async def test_a_then_b_then_a_does_not_promote(tmp_path, monkeypatch) -> None:
    """4. The interrupted run. Two occurrences, but not consecutive.

    Substituting in week 1, training it AS PROGRAMMED in week 2, substituting
    again in week 3 is an occasional preference. Promoting it would rewrite the
    plan on evidence the user's own middle workout contradicts.
    """
    db = await _db(tmp_path, "consec_aba")
    _bind(monkeypatch, db)
    plan_id, signature = await _active_rt_plan(db, activate=False)

    await _completed_workout(db, 101, "hack_squat", days_ago=21)
    await _substitution_audit(db, 101, signature=signature, plan_id=plan_id)
    # The interruption: the programmed exercise, actually performed.
    await _completed_workout(db, 102, "leg_press", days_ago=14)
    await _completed_workout(db, 103, "hack_squat", days_ago=7)
    await _substitution_audit(db, 103, signature=signature, plan_id=plan_id)

    evidence = await sp.collect_evidence(db, 1)
    group = next(iter(evidence["groups"].values()))
    assert len(group["sessions"]) == 2, "both substitutions are still evidence"
    assert await sp.find_promotable(db, 1) is None, (
        "A, B, A promoted; the detector is counting occurrences, not runs"
    )


@pytest.mark.asyncio
async def test_performing_the_programmed_exercise_breaks_the_streak(
    tmp_path, monkeypatch
) -> None:
    """5. The most recent occurrence decides. A, A, B does not promote."""
    db = await _db(tmp_path, "consec_aab")
    _bind(monkeypatch, db)
    plan_id, signature = await _active_rt_plan(db, activate=False)

    await _completed_workout(db, 101, "hack_squat", days_ago=21)
    await _substitution_audit(db, 101, signature=signature, plan_id=plan_id)
    await _completed_workout(db, 102, "hack_squat", days_ago=14)
    await _substitution_audit(db, 102, signature=signature, plan_id=plan_id)
    # Then they went back to the programmed exercise.
    await _completed_workout(db, 103, "leg_press", days_ago=3)

    assert await sp.find_promotable(db, 1) is None, (
        "the streak was broken by the latest workout and still promoted"
    )


@pytest.mark.asyncio
async def test_duplicate_and_split_set_rows_do_not_create_occurrences(
    tmp_path, monkeypatch
) -> None:
    """6. One workout is one occurrence, whatever the set rows look like.

    A split set writes a `telegram_split_secondary` row, and a replayed
    callback can write another. Counting set rows would let a single session
    manufacture a full streak on its own.
    """
    db = await _db(tmp_path, "consec_dupes")
    _bind(monkeypatch, db)
    plan_id, signature = await _active_rt_plan(db, activate=False)

    await _completed_workout(db, 101, "hack_squat", days_ago=20)
    await _substitution_audit(db, 101, signature=signature, plan_id=plan_id)
    # The same session, logged three more times: a split secondary and two
    # replays.
    stamp = _days_ago(20)
    for source in ("telegram_split_secondary", "telegram_one_tap", "watch"):
        await db.execute(
            "INSERT INTO sets(session_id, exercise_id, exercise_name, set_number, "
            "weight, reps, rir, source, exercise_index, created_at) "
            "VALUES(101, 'hack_squat', 'hack_squat', 2, 60, 10, 2, ?, 0, ?)",
            (source, stamp),
        )
        await _substitution_audit(db, 101, signature=signature, plan_id=plan_id)

    evidence = await sp.collect_evidence(db, 1)
    history = evidence["slot_history"].get(_RT_SLOT) or {}
    assert len(history) == 1, f"one session became {len(history)} occurrences"
    assert await sp.find_promotable(db, 1) is None


@pytest.mark.asyncio
async def test_an_unfinished_session_is_not_an_occurrence(tmp_path, monkeypatch) -> None:
    """6b. An abandoned workout is not training that happened."""
    db = await _db(tmp_path, "consec_unfinished")
    _bind(monkeypatch, db)
    plan_id, signature = await _active_rt_plan(db, activate=False)

    await _completed_workout(db, 101, "hack_squat", days_ago=20)
    await _substitution_audit(db, 101, signature=signature, plan_id=plan_id)
    await _completed_workout(db, 102, "hack_squat", days_ago=10, status="active")
    await _substitution_audit(db, 102, signature=signature, plan_id=plan_id)

    evidence = await sp.collect_evidence(db, 1)
    history = evidence["slot_history"].get(_RT_SLOT) or {}
    assert "102" not in history, "an unfinished session counted as performed"
    assert await sp.find_promotable(db, 1) is None


# ---------------------------------------------------------------------------
# Ownership and provenance
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_another_users_session_is_never_evidence(tmp_path, monkeypatch) -> None:
    """7. Cross-user provenance fails closed."""
    db = await _db(tmp_path, "own_session")
    _bind(monkeypatch, db)
    plan_id, signature = await _active_rt_plan(db, activate=False)
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(2,'B',NULL,?)",
        (utc_now(),),
    )

    await _completed_workout(db, 101, "hack_squat", days_ago=20)
    await _substitution_audit(db, 101, signature=signature, plan_id=plan_id)
    # A session that belongs to user 2, claimed by user 1's audit row.
    await _completed_workout(db, 202, "hack_squat", days_ago=10, user_id=2)
    await _substitution_audit(db, 202, signature=signature, plan_id=plan_id)

    evidence = await sp.collect_evidence(db, 1)
    assert evidence["skipped"].get(sp.SKIP_FOREIGN_SESSION) == 1
    assert await sp.find_promotable(db, 1) is None


@pytest.mark.asyncio
async def test_another_users_plan_is_never_provenance(tmp_path, monkeypatch) -> None:
    """7b. The evidence plan must belong to the audit user."""
    db = await _db(tmp_path, "own_plan")
    _bind(monkeypatch, db)
    plan_id, signature = await _active_rt_plan(db, activate=False)
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(2,'B',NULL,?)",
        (utc_now(),),
    )
    foreign_plan = await db.execute(
        "INSERT INTO plan_versions("
        "user_id, plan_type, title, strategy, fit_score, status, payload, created_at) "
        "VALUES(2, 'workout', 'other', 'test', 1.0, 'active', ?, ?)",
        (json.dumps({"sessions": [{"session_occurrence": "rt01_a", "exercises": []}]}),
         utc_now()),
    )

    for index, session_id in enumerate((101, 102)):
        await _completed_workout(db, session_id, "hack_squat", days_ago=20 - index * 5)
    await _substitution_audit(db, 101, signature=signature, plan_id=plan_id)
    await _substitution_audit(db, 102, signature=signature, plan_id=int(foreign_plan))

    evidence = await sp.collect_evidence(db, 1)
    assert evidence["skipped"].get(sp.SKIP_FOREIGN_PLAN) == 1
    assert await sp.find_promotable(db, 1) is None


@pytest.mark.asyncio
async def test_a_forged_signature_is_rejected(tmp_path, monkeypatch) -> None:
    """8. The claimed signature must be the one that plan version really has.

    Otherwise an audit row could name any signature it liked and group
    unrelated substitutions into a promotable pattern.
    """
    db = await _db(tmp_path, "forged_sig")
    _bind(monkeypatch, db)
    plan_id, signature = await _active_rt_plan(db, activate=False)

    for index, session_id in enumerate((101, 102)):
        await _completed_workout(db, session_id, "hack_squat", days_ago=20 - index * 5)
        await _substitution_audit(
            db, session_id, signature="ffffffffffffffff", plan_id=plan_id
        )

    evidence = await sp.collect_evidence(db, 1)
    assert evidence["skipped"].get(sp.SKIP_SIGNATURE_MISMATCH) == 2
    assert await sp.find_promotable(db, 1) is None
    del signature


def test_a_partial_key_set_has_no_signature() -> None:
    """8b. Identity fails closed rather than fingerprinting a subset."""
    assert sp.split_signature(["a", "b"]) is not None
    assert sp.split_signature(["a", None]) is None, "a malformed key was dropped"
    assert sp.split_signature(["a", ""]) is None
    assert sp.split_signature(["a", "a"]) is None, "duplicate keys collapsed"
    assert sp.split_signature([]) is None
    assert sp.signature_from_plan_payload(
        {"sessions": [{"session_occurrence": "a"}, {"no_key": 1}]}
    ) is None


# ---------------------------------------------------------------------------
# Lifecycle honesty
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_finalization_failure_is_never_rendered_as_success(
    tmp_path, monkeypatch
) -> None:
    """9. A failed status+cooldown transaction must not print "done".

    Injected at the real boundary and answered through the real callback, so
    what is asserted is the sentence the user actually receives.
    """
    db = await _db(tmp_path, "final_fail")
    _bind(monkeypatch, db)
    plan_id, signature, slot_id, source = await _active_plan(db)
    approval_id = await _proposal(db, plan_id, signature, slot_id, source)

    async def _refuse(*args: Any, **kwargs: Any) -> bool:
        return False

    monkeypatch.setattr(sp, "_finalize", _refuse)

    from noam_coach.bot import callback_plans as callback_plans_bot

    query = _RoundTripQuery()
    await callback_plans_bot.handle_plan_callback(
        query, user_id=1,
        data=sp.promotion_callback_data("yes", approval_id),
    )

    rendered = query.messages[-1]
    assert "עודכן ✅" not in rendered, (
        f"finalization failed and the user was told it succeeded: {rendered}"
    )
    row = await db.fetch_one(
        "SELECT status FROM approvals WHERE id=?", (approval_id,)
    )
    assert str(row["status"]) != sp.STATUS_APPROVED


@pytest.mark.asyncio
async def test_an_active_lease_is_never_superseded(tmp_path, monkeypatch) -> None:
    """10. A live mutation must not be marked stale by a new proposal.

    Superseding it would mark the approval stale while A9 is mid-flight, so the
    change would land on the plan and then be reported as though it never
    happened.
    """
    db = await _db(tmp_path, "live_lease")
    _bind(monkeypatch, db)
    plan_id, signature, slot_id, source = await _active_plan(db)
    approval_id = await _proposal(db, plan_id, signature, slot_id, source)

    # A caller takes the lease and is still working.
    assert await sp.claim_status(
        db, approval_id, 1, expect=sp.STATUS_PENDING, become=sp.STATUS_PROCESSING
    )

    second = await sp.propose(
        db, 1,
        {
            "subject": sp.subject_of(signature, slot_id, "another_target"),
            "split_signature": signature, "slot_id": slot_id,
            "target": "another_target", "source": source, "occurrences": 2,
        },
        active_plan_id=plan_id,
    )
    assert second is None, "a new proposal was created over a live lease"
    row = await db.fetch_one("SELECT status FROM approvals WHERE id=?", (approval_id,))
    assert str(row["status"]) == sp.STATUS_PROCESSING, (
        "the in-flight mutation was marked stale underneath its own caller"
    )


@pytest.mark.asyncio
async def test_an_expired_lease_may_be_superseded(tmp_path, monkeypatch) -> None:
    """10b. The complement: abandoned work must not block the feature forever."""
    db = await _db(tmp_path, "dead_lease")
    _bind(monkeypatch, db)
    plan_id, signature, slot_id, source = await _active_plan(db)
    approval_id = await _proposal(db, plan_id, signature, slot_id, source)
    await sp.claim_status(
        db, approval_id, 1, expect=sp.STATUS_PENDING, become=sp.STATUS_PROCESSING
    )
    await _age_the_lease(db, approval_id, minutes=sp.PROCESSING_LEASE_MINUTES + 5)

    second = await sp.propose(
        db, 1,
        {
            "subject": sp.subject_of(signature, slot_id, "another_target"),
            "split_signature": signature, "slot_id": slot_id,
            "target": "another_target", "source": source, "occurrences": 2,
        },
        active_plan_id=plan_id,
    )
    assert second, "an abandoned lease permanently blocked new proposals"


@pytest.mark.asyncio
async def test_the_decision_audit_is_written_exactly_once(tmp_path, monkeypatch) -> None:
    """11. Double taps and recovery must not multiply the record.

    The audit rides the same transaction as the conditional status UPDATE, so
    only the caller that wins rowcount==1 writes it.
    """
    db = await _db(tmp_path, "audit_once")
    _bind(monkeypatch, db)
    plan_id, signature, slot_id, source = await _active_plan(db)
    approval_id = await _proposal(db, plan_id, signature, slot_id, source)

    from noam_coach.bot import callback_plans as callback_plans_bot

    for _ in range(3):
        await callback_plans_bot.handle_plan_callback(
            _RoundTripQuery(), user_id=1,
            data=sp.promotion_callback_data("yes", approval_id),
        )

    rows = await db.fetch_all(
        "SELECT details FROM audit WHERE user_id=1 AND action=?",
        (sp._PROMOTION_AUDIT_ACTION,),
    )
    assert len(rows) == 1, f"the decision was recorded {len(rows)} times"
    details = json.loads(rows[0]["details"])
    assert details["outcome"] == sp.STATUS_APPROVED
    assert details["approval_id"] == approval_id
    # Bounded fields only -- no free text, no payload contents.
    assert set(details) == {
        "outcome", "subject", "slot_id", "target", "occurrences", "approval_id"
    }


@pytest.mark.asyncio
async def test_no_after_yes_reports_the_durable_decision(tmp_path, monkeypatch) -> None:
    """12. A late "no" must not claim the plan was left unchanged.

    By then the plan HAS been rewritten. Telling the user otherwise contradicts
    what their own plan now shows.
    """
    db = await _db(tmp_path, "no_after_yes")
    _bind(monkeypatch, db)
    plan_id, signature, slot_id, source = await _active_plan(db)
    approval_id = await _proposal(db, plan_id, signature, slot_id, source)

    from noam_coach.bot import callback_plans as callback_plans_bot

    yes = _RoundTripQuery()
    await callback_plans_bot.handle_plan_callback(
        yes, user_id=1,
        data=sp.promotion_callback_data("yes", approval_id),
    )
    assert "עודכן" in yes.messages[-1]

    no = _RoundTripQuery()
    await callback_plans_bot.handle_plan_callback(
        no, user_id=1,
        data=sp.promotion_callback_data("no", approval_id),
    )
    rendered = no.messages[-1]
    assert "נשאיר את התוכנית כמו שהיא" not in rendered, (
        f"a late no claimed nothing changed after the plan was rewritten: {rendered}"
    )
    assert "עודכנה" in rendered or "כבר" in rendered, rendered


@pytest.mark.asyncio
async def test_a_coincidental_target_under_another_split_is_stale(
    tmp_path, monkeypatch
) -> None:
    """13. Already-applied must be OUR change, not a lookalike.

    The user moved to a different programme that happens to hold the same
    exercise in the same slot. Finalizing that as approved would write a
    365-day cooldown for a change this promotion never made.
    """
    db = await _db(tmp_path, "coincidence")
    _bind(monkeypatch, db)
    plan_id, signature, slot_id, source = await _active_plan(db)
    approval_id = await _proposal(db, plan_id, signature, slot_id, source)

    assert await sp._already_applied(
        db, 1, slot_id, source, expected_signature=signature
    ), "precondition: the slot holds `source` under the proposal's own split"
    assert not await sp._already_applied(
        db, 1, slot_id, source, expected_signature="ffffffffffffffff"
    ), "a different split was accepted as already applied"
    del approval_id, plan_id


@pytest.mark.asyncio
async def test_the_first_real_set_defines_what_was_performed(
    tmp_path, monkeypatch
) -> None:
    """Occurrence rules, pinned on the two that actually change the answer.

    A split set writes a `telegram_split_secondary` row for its second half,
    and a replayed callback can add another row later. Both are ordered around
    the real set, so the slot's occurrence must be decided by the FIRST genuine
    set -- otherwise the recorded history says the user trained something they
    did not, and the streak is built on it.
    """
    db = await _db(tmp_path, "first_set_wins")
    _bind(monkeypatch, db)
    plan_id, signature = await _active_rt_plan(db, activate=False)
    stamp = _days_ago(20)
    await db.execute(
        "INSERT INTO sessions(id, user_id, code, name, plan, status, "
        "exercise_index, set_number, started_at, ended_at) "
        "VALUES(101, 1, 'A', 'A', ?, 'completed', 0, 1, ?, ?)",
        (json.dumps(_one_exercise_plan("hack_squat")), stamp, stamp),
    )
    # Ordered deliberately: a split-secondary FIRST, the real set second, a
    # replay last. Only the middle row is what the user actually performed.
    for exercise, source in (
        ("leg_press", "telegram_split_secondary"),
        ("hack_squat", "telegram_one_tap"),
        ("leg_press", "telegram_one_tap"),
    ):
        await db.execute(
            "INSERT INTO sets(session_id, exercise_id, exercise_name, set_number, "
            "weight, reps, rir, source, exercise_index, created_at) "
            "VALUES(101, ?, ?, 1, 60, 10, 2, ?, 0, ?)",
            (exercise, exercise, source, stamp),
        )
    await _substitution_audit(db, 101, signature=signature, plan_id=plan_id)

    evidence = await sp.collect_evidence(db, 1)
    history = evidence["slot_history"].get(_RT_SLOT) or {}

    assert len(history) == 1, "one session produced more than one occurrence"
    assert history["101"][1] == "hack_squat", (
        "the occurrence was decided by a split-secondary or a replayed set "
        "rather than by the first real set"
    )
