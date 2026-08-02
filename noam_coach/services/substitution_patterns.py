"""Promote a repeated substitution into a preference (A12).

A11b made a substitution *legible*: it records a bounded `reason`, a stable
`slot_id`, and routes saved-plan changes through A9. What it does not do is
notice that the user keeps making the same swap. This module notices, proposes,
and remembers the answer.

D-5 defines the shape and is authoritative here -- the threshold is not invented
in this module: **two consecutive occurrences**, and a decline suppresses the
same subject for **eight weeks**.

Three things had to be measured before any of this could be built, and each one
ruled out an obvious shortcut:

* **`job_state` cannot hold the cooldown.** Its key is
  `(user_id, day, key)` -- `day` is part of the identity, so a window longer
  than one calendar day is structurally inexpressible.
* **A `FactSpec` TTL cannot hold it either.** The TTL lives on the shared spec
  and is applied at read time. Measured: one stored row flipped from suppressed
  to expired purely because the spec constant changed. A promise to a user must
  not be rewritable by editing a config value, so `suppress_until` is absolute
  and stored on the row.
* **The current active plan cannot classify history.** A substitution recorded
  weeks ago happened under whatever plan was live *then*. Grouping by today's
  plan would silently merge evidence across a programme the user has since left.

So evidence is grouped by a **split signature** derived from the historical plan
version, and the two identities are kept apart throughout:

* `evidence_plan_id` -- the historical provenance a piece of evidence came
  from, used only to derive its split signature;
* `expected_active_plan_id` -- the plan that was active when the proposal was
  created, guarded when the user approves it.

Using an arbitrary historical evidence plan as the mutation expectation would
apply a change to a plan the user never saw.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import secrets
from typing import Any

from config import LOGGER

# ---------------------------------------------------------------------------
# Vocabulary. Bounded codes only -- these reach audit rows and callback data.
# ---------------------------------------------------------------------------
APPROVAL_KIND = "promote_substitution"

#: Approval lifecycle. `pending` and `processing` are both OPEN; the partial
#: unique index added by migration 17 permits only one open row per user.
STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_APPROVED = "approved"
STATUS_DECLINED = "declined"
STATUS_STALE = "stale"
STATUS_FAILED = "failed"
OPEN_STATUSES = (STATUS_PENDING, STATUS_PROCESSING)

#: Only reasons a verified production path mints, and only those that describe
#: a PREFERENCE. `pain` is never promotion evidence -- a safety adaptation is
#: not something the user chose, and promoting it would turn "this hurt" into
#: "I like this". `unspecified` marks a pre-A11b keyboard or an unrecognised
#: value; it is not a signal, so it is not eligible either.
PROMOTABLE_REASONS = frozenset({"equipment"})

#: D-5: "Pattern promotion asks after **two consecutive** occurrences ... with
#: an 8-week cooldown on decline." Sourced from the authoritative plan, not
#: chosen here.
PROMOTION_THRESHOLD = 2
DECLINE_COOLDOWN_DAYS = 56
APPROVE_COOLDOWN_DAYS = 365

#: The detector reads a bounded window and a bounded row count. Both exist so a
#: pathological history cannot turn one workout into an unbounded scan.
LOOKBACK_DAYS = 56
MAX_ROWS = 200

#: How long a `processing` claim is honoured before it is considered abandoned.
#:
#: `processing` is a LEASE, not a lock. A process that dies mid-mutation cannot
#: release it, so without an expiry the approval would be stranded forever and
#: the user's tap would never produce an answer. Twenty minutes matches the
#: existing `proactive_claim_ttl_minutes` default, so the codebase has one
#: notion of "a claim this old belongs to a process that is gone".
#:
#: The lease is carried by `approvals.decided_at`, which `claim_status` already
#: stamps on every transition -- no schema change. Checked in this module rather
#: than trusted from a read, because the reclaim must be a compare-and-set.
PROCESSING_LEASE_MINUTES = 20

_AUDIT_ACTION = "approve_substitution"


def utc_iso(moment: dt.datetime | None = None) -> str:
    """The one canonical timestamp format used by every column this module writes.

    Absolute, UTC, ISO-8601. `suppress_until` is compared with `MAX()` in SQL,
    which is only correct because every value shares this format -- a mixed
    local/naive value would sort wrongly and silently shorten a promise.
    """
    current = moment or dt.datetime.now(dt.timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=dt.timezone.utc)
    return current.astimezone(dt.timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------
def split_signature(session_keys: Any) -> str | None:
    """A canonical fingerprint of the SET of declared session keys in a split.

    Canonical JSON rather than a delimiter join: a joined string is ambiguous
    the moment a key could contain the delimiter, and two different key sets can
    collide into one signature. `json.dumps` with explicit separators and sorted
    input is stable across runs and unambiguous by construction.

    Sorting is what makes reordering a no-op: the same sessions in a different
    order are the same programme. Adding or removing a session changes the set,
    so it changes the signature -- which is exactly the boundary A12 must not
    join across.
    """
    if not isinstance(session_keys, (list, tuple, set)):
        return None
    keys = sorted(str(k) for k in session_keys if isinstance(k, str) and k.strip())
    if not keys:
        return None
    canonical = json.dumps(keys, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def subject_of(signature: str, slot_id: str, target: str) -> str:
    """The cooldown/proposal subject.

    `<split_signature>|<slot_id>|<target>` -- the signature leads so a cooldown
    earned under one structural split can never suppress a proposal under
    another. Two programmes that happen to share a slot id are different
    questions.
    """
    return f"{signature}|{slot_id}|{target}"


def signature_from_plan_payload(payload: Any) -> str | None:
    """Derive a split signature from a HISTORICAL plan-version payload.

    Reads the declared session keys the payload itself carries, never the
    current active plan and never a recomputed key.
    """
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            return None
    if not isinstance(payload, dict):
        return None
    keys = [
        session.get("session_occurrence")
        for session in (payload.get("sessions") or [])
        if isinstance(session, dict)
    ]
    return split_signature([k for k in keys if isinstance(k, str) and k])


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------
#: Why a candidate audit row was not counted. Bounded so the counts can be
#: logged without the rows themselves.
SKIP_INVALID_SLOT = "invalid_slot_id"
SKIP_NO_PROVENANCE = "no_plan_provenance"
SKIP_MALFORMED = "malformed_json"
SKIP_REASON = "reason_not_promotable"
SKIP_DUPLICATE_SESSION = "duplicate_session"


#: The detector's only query. Column order matches
#: `idx_audit_user_action_created(user_id, action, created_at, id)`, and the
#: ORDER BY matches the index too -- proven with EXPLAIN QUERY PLAN: without the
#: index the plan is `SCAN audit`; ordering by `id DESC` alone still leaves
#: `USE TEMP B-TREE FOR ORDER BY`, while `created_at DESC, id DESC` removes it.
_EVIDENCE_SQL = (
    "SELECT id, entity_id, details, created_at FROM audit "
    "WHERE user_id=? AND action=? AND created_at>=? "
    "ORDER BY created_at DESC, id DESC LIMIT ?"
)


async def collect_evidence(db: Any, user_id: int, *, now: dt.datetime | None = None) -> dict[str, Any]:
    """Group promotable substitutions by `(split_signature, slot_id, target)`.

    Counts DISTINCT session ids, never raw rows. A single workout can emit
    several `approve_substitution` rows for one slot -- the user changes their
    mind, or a replayed callback lands twice -- and counting rows would let one
    session manufacture a promotion on its own.

    Every rejection is counted by bounded code rather than dropped silently, so
    "no promotion" can be explained without re-reading the audit table.
    """
    current = now or dt.datetime.now(dt.timezone.utc)
    since = utc_iso(current - dt.timedelta(days=LOOKBACK_DAYS))

    try:
        rows = await db.fetch_all(
            _EVIDENCE_SQL, (user_id, _AUDIT_ACTION, since, MAX_ROWS)
        )
    except Exception:
        LOGGER.exception("substitution_evidence_read_failed user_id=%s", user_id)
        return {"groups": {}, "skipped": {}, "rows": 0}

    from noam_coach.services import workout_slots

    groups: dict[str, dict[str, Any]] = {}
    skipped: dict[str, int] = {}

    def _skip(code: str) -> None:
        skipped[code] = skipped.get(code, 0) + 1

    for row in rows or []:
        raw = row["details"] if "details" in row.keys() else None
        try:
            details = json.loads(raw or "{}")
        except (TypeError, ValueError):
            # A corrupt row must never break detection. Counted, not raised.
            _skip(SKIP_MALFORMED)
            continue
        if not isinstance(details, dict):
            _skip(SKIP_MALFORMED)
            continue

        if str(details.get("reason") or "") not in PROMOTABLE_REASONS:
            _skip(SKIP_REASON)
            continue

        slot_id = details.get("slot_id")
        # `is_valid_slot_id` is a round-trip check, so this rejects missing,
        # empty, malformed and non-round-tripping ids in one test. A11b stored
        # `slot_id=""` on legacy entries; those rows stay readable and never
        # count.
        if not workout_slots.is_valid_slot_id(slot_id):
            _skip(SKIP_INVALID_SLOT)
            continue

        signature = details.get("split_signature")
        plan_id = details.get("evidence_plan_id")
        if not isinstance(signature, str) or not signature or plan_id is None:
            _skip(SKIP_NO_PROVENANCE)
            continue

        target = str(details.get("target") or "")
        source = str(details.get("source") or "")
        if not target or not source:
            _skip(SKIP_NO_PROVENANCE)
            continue

        subject = subject_of(signature, str(slot_id), target)
        session_id = str(row["entity_id"] or "")
        if not session_id:
            _skip(SKIP_NO_PROVENANCE)
            continue

        group = groups.setdefault(
            subject,
            {
                "subject": subject,
                "split_signature": signature,
                "slot_id": str(slot_id),
                "target": target,
                "source": source,
                "sessions": set(),
                "evidence_plan_ids": set(),
            },
        )
        if session_id in group["sessions"]:
            _skip(SKIP_DUPLICATE_SESSION)
            continue
        group["sessions"].add(session_id)
        group["evidence_plan_ids"].add(plan_id)

    return {"groups": groups, "skipped": skipped, "rows": len(rows or [])}


async def find_promotable(db: Any, user_id: int, *, now: dt.datetime | None = None):
    """The single subject that has met the D-5 threshold, or None.

    Returns at most one candidate: only one proposal may be open per user, and
    that is a database rule (the partial unique index), not a convention.
    """
    evidence = await collect_evidence(db, user_id, now=now)
    ready = [
        group
        for group in evidence["groups"].values()
        if len(group["sessions"]) >= PROMOTION_THRESHOLD
    ]
    if not ready:
        if evidence["skipped"]:
            LOGGER.info(
                "substitution_evidence_skipped user_id=%s rows=%d %s",
                user_id,
                evidence["rows"],
                " ".join(f"{k}={v}" for k, v in sorted(evidence["skipped"].items())),
            )
        return None
    ready.sort(key=lambda g: (len(g["sessions"]), g["subject"]), reverse=True)
    winner = ready[0]
    return {
        "subject": winner["subject"],
        "split_signature": winner["split_signature"],
        "slot_id": winner["slot_id"],
        "target": winner["target"],
        "source": winner["source"],
        "occurrences": len(winner["sessions"]),
    }


# ---------------------------------------------------------------------------
# Cooldown
# ---------------------------------------------------------------------------
async def is_suppressed(db: Any, user_id: int, subject: str, *, now: dt.datetime | None = None) -> bool:
    """True while an unexpired promise covers this subject."""
    try:
        row = await db.fetch_one(
            "SELECT suppress_until FROM substitution_cooldowns "
            "WHERE user_id=? AND subject=?",
            (user_id, subject),
        )
    except Exception:
        LOGGER.exception("substitution_cooldown_read_failed user_id=%s", user_id)
        # Fail closed: an unreadable cooldown is not evidence that none exists.
        return True
    if not row:
        return False
    return utc_iso(now) < str(row["suppress_until"])


async def record_cooldown(
    db: Any,
    user_id: int,
    subject: str,
    *,
    decided_as: str,
    approval_id: str | None,
    days: int,
    now: dt.datetime | None = None,
) -> None:
    """Write the promise, never shortening one already made.

    The UPSERT keeps the LONGEST `suppress_until`, and -- the part that is easy
    to get wrong -- keeps the provenance of that winning promise with it. A
    shorter later decision must not overwrite `decided_as`/`approval_id`,
    because the row would then claim a 365-day approval was the reason for a
    56-day suppression, or vice versa. Every field moves together or none does.
    """
    current = now or dt.datetime.now(dt.timezone.utc)
    stamp = utc_iso(current)
    until = utc_iso(current + dt.timedelta(days=days))

    await db.execute(
        """
        INSERT INTO substitution_cooldowns(
            user_id, subject, suppress_until, decided_as, approval_id,
            decided_at, created_at, updated_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id, subject) DO UPDATE SET
            decided_as = CASE
                WHEN excluded.suppress_until > substitution_cooldowns.suppress_until
                THEN excluded.decided_as ELSE substitution_cooldowns.decided_as END,
            approval_id = CASE
                WHEN excluded.suppress_until > substitution_cooldowns.suppress_until
                THEN excluded.approval_id ELSE substitution_cooldowns.approval_id END,
            decided_at = CASE
                WHEN excluded.suppress_until > substitution_cooldowns.suppress_until
                THEN excluded.decided_at ELSE substitution_cooldowns.decided_at END,
            suppress_until = MAX(
                substitution_cooldowns.suppress_until, excluded.suppress_until
            ),
            updated_at = excluded.updated_at
        """,
        (user_id, subject, until, decided_as, approval_id, stamp, stamp, stamp),
    )


# ---------------------------------------------------------------------------
# Lifecycle
#
#   pending -> processing -> approved | declined | stale | failed
#
# `processing` exists so a crash has a recoverable state to be found in. Without
# it, a process that died between mutating the plan and finalizing the approval
# would leave a `pending` row indistinguishable from one nobody had tapped, and
# a retry would apply the substitution a second time.
#
# Only the caller that observes `rowcount == 1` on a state transition may act.
# That is the whole concurrency argument: SQLite serializes the UPDATE, so of
# two racing taps exactly one changes the row and exactly one side effect
# follows. `payload.subject` is a grouping value, NOT a database idempotency
# key -- nothing enforces uniqueness on it, and calling it one would be false.
# The database rules are the partial unique index (one open row per user) and
# these conditional UPDATEs.
# ---------------------------------------------------------------------------
async def claim_status(
    db: Any,
    approval_id: str,
    user_id: int,
    *,
    expect: str,
    become: str,
) -> bool:
    """Move one approval between states, returning whether THIS caller won.

    Conditional on both the id and the current status, so a replayed callback,
    a double tap and a concurrent worker all resolve to exactly one winner.
    """
    changed = await db.execute_rowcount(
        "UPDATE approvals SET status=?, decided_at=? "
        "WHERE id=? AND user_id=? AND status=?",
        (become, utc_iso(), approval_id, user_id, expect),
    )
    return int(changed) == 1


async def _finalize(
    db: Any,
    approval_id: str,
    user_id: int,
    *,
    become: str,
    subject: str,
    days: int,
    now: dt.datetime | None = None,
) -> bool:
    """Commit the final status AND its cooldown together, or neither.

    Two statements outside a transaction would leave a window in which the
    approval reads `approved` while no promise exists -- and the next detector
    pass would propose the same subject again, immediately, to a user who had
    just answered it. `db.transaction()` issues BEGIN IMMEDIATE, so the pair is
    atomic and the conditional UPDATE inside it still decides the single winner.

    Returns whether THIS caller performed the finalization.
    """
    stamp = utc_iso(now)
    until = utc_iso(
        (now or dt.datetime.now(dt.timezone.utc)) + dt.timedelta(days=days)
    )
    try:
        async with db.transaction() as conn:
            cursor = await conn.execute(
                "UPDATE approvals SET status=?, decided_at=? "
                "WHERE id=? AND user_id=? AND status=?",
                (become, stamp, approval_id, user_id, STATUS_PROCESSING),
            )
            if int(cursor.rowcount) != 1:
                return False
            if subject:
                await conn.execute(
                    """
                    INSERT INTO substitution_cooldowns(
                        user_id, subject, suppress_until, decided_as, approval_id,
                        decided_at, created_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(user_id, subject) DO UPDATE SET
                        decided_as = CASE
                            WHEN excluded.suppress_until > substitution_cooldowns.suppress_until
                            THEN excluded.decided_as ELSE substitution_cooldowns.decided_as END,
                        approval_id = CASE
                            WHEN excluded.suppress_until > substitution_cooldowns.suppress_until
                            THEN excluded.approval_id ELSE substitution_cooldowns.approval_id END,
                        decided_at = CASE
                            WHEN excluded.suppress_until > substitution_cooldowns.suppress_until
                            THEN excluded.decided_at ELSE substitution_cooldowns.decided_at END,
                        suppress_until = MAX(
                            substitution_cooldowns.suppress_until, excluded.suppress_until
                        ),
                        updated_at = excluded.updated_at
                    """,
                    (user_id, subject, until, become, approval_id, stamp, stamp, stamp),
                )
    except Exception:
        LOGGER.exception(
            "substitution_finalize_failed user_id=%s status=%s", user_id, become
        )
        return False
    return True


async def reclaim_abandoned(
    db: Any,
    approval_id: str,
    user_id: int,
    *,
    now: dt.datetime | None = None,
) -> bool:
    """Atomically take over a `processing` row whose lease has expired.

    This is what makes crash recovery real rather than asserted. After a crash
    between the mutation and finalization the row is `processing`, and the
    ordinary `pending -> processing` claim can never match it again -- so
    without this the approval is stranded and the user's tap has no outcome.

    The reclaim is a single conditional UPDATE, not a read-then-act: the
    `decided_at < ?` predicate is evaluated by SQLite inside the same statement
    that flips the row, so of two callers racing to recover the same approval
    exactly one sees `rowcount == 1`. A read followed by a write would let both
    read the same stale timestamp and both proceed.

    Re-stamping `decided_at` renews the lease, so a recovery that itself dies is
    recoverable in turn -- no row can be stranded permanently.
    """
    cutoff = utc_iso(
        (now or dt.datetime.now(dt.timezone.utc))
        - dt.timedelta(minutes=PROCESSING_LEASE_MINUTES)
    )
    changed = await db.execute_rowcount(
        "UPDATE approvals SET decided_at=? "
        "WHERE id=? AND user_id=? AND status=? AND decided_at IS NOT NULL "
        "AND decided_at < ?",
        (utc_iso(now), approval_id, user_id, STATUS_PROCESSING, cutoff),
    )
    return int(changed) == 1


async def propose(
    db: Any,
    user_id: int,
    candidate: dict[str, Any],
    *,
    active_plan_id: int | None,
    now: dt.datetime | None = None,
) -> str | None:
    """Create the pending proposal, or None when one must not be created.

    Returns None when the subject is under an unexpired promise, when there is
    no active plan to guard against, or when another open proposal already
    exists -- the last of which is enforced by the database, not by this check:
    the partial unique index raises on the INSERT if two callers race past the
    read.

    The whole create runs inside `db.transaction()`, which issues
    BEGIN IMMEDIATE, so the supersede-then-insert pair cannot interleave with
    another writer.
    """
    if active_plan_id is None:
        return None
    subject = str(candidate.get("subject") or "")
    if not subject:
        return None
    if await is_suppressed(db, user_id, subject, now=now):
        return None

    payload = {
        "subject": subject,
        "split_signature": candidate["split_signature"],
        "slot_id": candidate["slot_id"],
        "target": candidate["target"],
        "source": candidate["source"],
        "occurrences": int(candidate["occurrences"]),
        # The plan that was ACTIVE when the proposal was made -- guarded at
        # approval. Deliberately not an evidence plan id: evidence may span
        # several superseded versions, and applying a change to one of those
        # would touch a plan the user never saw this proposal for.
        "expected_active_plan_id": int(active_plan_id),
    }

    approval_id = secrets.token_urlsafe(8)
    try:
        async with db.transaction() as conn:
            await conn.execute(
                "UPDATE approvals SET status=? , decided_at=? "
                "WHERE user_id=? AND kind=? AND status IN (?, ?)",
                (STATUS_STALE, utc_iso(now), user_id, APPROVAL_KIND,
                 STATUS_PENDING, STATUS_PROCESSING),
            )
            await conn.execute(
                "INSERT INTO approvals(id, user_id, kind, payload, status, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (approval_id, user_id, APPROVAL_KIND,
                 json.dumps(payload, ensure_ascii=False), STATUS_PENDING,
                 utc_iso(now)),
            )
    except Exception:
        LOGGER.exception("substitution_proposal_failed user_id=%s", user_id)
        return None
    LOGGER.info(
        "substitution_proposed user_id=%s occurrences=%d", user_id,
        payload["occurrences"],
    )
    return approval_id


async def decline(db: Any, user_id: int, approval_id: str, *, now: dt.datetime | None = None) -> str:
    """Record a refusal and suppress the subject for the D-5 window."""
    row = await db.fetch_one(
        "SELECT payload, status FROM approvals WHERE id=? AND user_id=?",
        (approval_id, user_id),
    )
    if not row:
        return STATUS_STALE
    try:
        payload = json.loads(row["payload"] or "{}")
    except (TypeError, ValueError):
        payload = {}
    subject = str(payload.get("subject") or "")

    # A decline goes straight from `pending` to `declined` -- there is no
    # mutation to protect, so no lease is needed. The status and the promise
    # still commit together: a decline recorded without its cooldown would
    # re-ask the question the user just refused.
    if not await claim_status(
        db, approval_id, user_id, expect=STATUS_PENDING, become=STATUS_PROCESSING
    ):
        # Someone already decided this. The durable answer stands.
        return STATUS_DECLINED
    await _finalize(
        db, approval_id, user_id, become=STATUS_DECLINED,
        subject=subject, days=DECLINE_COOLDOWN_DAYS, now=now,
    )
    return STATUS_DECLINED


async def approve(db: Any, user_id: int, approval_id: str, *, now: dt.datetime | None = None) -> str:
    """Apply the promoted substitution, once, through the A9 boundary.

    The claim is `pending -> processing`, so a crash mid-mutation leaves a row
    that says so. Recovery is not a special path: re-entering calls the same
    boundary, which reports `no_change` when the target is already applied --
    finalizing without creating a second plan version.
    """
    row = await db.fetch_one(
        "SELECT payload, status FROM approvals WHERE id=? AND user_id=?",
        (approval_id, user_id),
    )
    if not row:
        return STATUS_STALE

    status = str(row["status"] or "")
    if status not in (STATUS_PENDING, STATUS_PROCESSING):
        return status

    if status == STATUS_PENDING:
        if not await claim_status(
            db, approval_id, user_id, expect=STATUS_PENDING, become=STATUS_PROCESSING
        ):
            # Lost the race. The winner owns the mutation.
            return STATUS_PROCESSING
    else:
        # Already `processing`. Either another caller is mid-mutation right now
        # -- in which case this tap must not act -- or a process died and left
        # the lease behind. Only an EXPIRED lease may be reclaimed, and only by
        # the single caller whose conditional UPDATE matches.
        if not await reclaim_abandoned(db, approval_id, user_id, now=now):
            return STATUS_PROCESSING
        LOGGER.info("substitution_promotion_recovered user_id=%s", user_id)

    try:
        payload = json.loads(row["payload"] or "{}")
    except (TypeError, ValueError):
        payload = {}

    slot_id = str(payload.get("slot_id") or "")
    target = str(payload.get("target") or "")
    source = str(payload.get("source") or "")
    expected_plan = payload.get("expected_active_plan_id")
    expected_signature = str(payload.get("split_signature") or "")
    subject = str(payload.get("subject") or "")

    if not slot_id or not target or expected_plan is None:
        await claim_status(
            db, approval_id, user_id, expect=STATUS_PROCESSING, become=STATUS_FAILED
        )
        return STATUS_FAILED

    from noam_coach.services import plan_mutations

    # ALREADY APPLIED takes precedence over every staleness guard, and the
    # ordering is load-bearing rather than cosmetic.
    #
    # Measured: the A9 boundary is copy-on-write, so a SUCCESSFUL mutation
    # always activates a NEW plan version -- id 1 becomes id 4. On recovery the
    # `expected_active_plan_id` guard therefore rejects the promotion's own
    # completed work as stale, and the cooldown is never written. Checking the
    # applied state first turns that into the finalize-only path it should be.
    if await _already_applied(db, user_id, slot_id, target):
        await _finalize(
            db, approval_id, user_id, become=STATUS_APPROVED,
            subject=subject, days=APPROVE_COOLDOWN_DAYS, now=now,
        )
        LOGGER.info("substitution_promotion_finalized user_id=%s applied=already", user_id)
        return STATUS_APPROVED

    # The split must still be the one the proposal was built against. Checked
    # here rather than inside A9's boundary because it is A12's contract, not
    # the mutation boundary's.
    active = await _active_plan_signature(db, user_id)
    if active is not None and expected_signature and active != expected_signature:
        await claim_status(
            db, approval_id, user_id, expect=STATUS_PROCESSING, become=STATUS_STALE
        )
        LOGGER.info("substitution_promotion_stale user_id=%s reason=split", user_id)
        return STATUS_STALE

    outcome = await plan_mutations.substitute_slot_in_saved_plan(
        db, user_id, slot_id, target,
        reason="promoted_preference",
        expected_active_plan_id=int(expected_plan),
        expected_source_exercise_id=source or None,
    )

    if outcome.outcome in (
        plan_mutations.OUTCOME_REALIGNED,
        # `no_change` is the crash-recovery case: the mutation already landed
        # and only finalization was missing. Finalize; do not mutate again.
        plan_mutations.OUTCOME_NO_CHANGE,
    ):
        await _finalize(
            db, approval_id, user_id, become=STATUS_APPROVED,
            subject=subject, days=APPROVE_COOLDOWN_DAYS, now=now,
        )
        LOGGER.info(
            "substitution_promoted user_id=%s outcome=%s", user_id, outcome.outcome
        )
        return STATUS_APPROVED

    if outcome.outcome == plan_mutations.OUTCOME_BLOCKED:
        # The plan or the slot moved on. Stale, not failed: nothing broke, the
        # question simply no longer applies.
        await claim_status(
            db, approval_id, user_id, expect=STATUS_PROCESSING, become=STATUS_STALE
        )
        LOGGER.info(
            "substitution_promotion_stale user_id=%s reason=%s", user_id, outcome.reason
        )
        return STATUS_STALE

    await claim_status(
        db, approval_id, user_id, expect=STATUS_PROCESSING, become=STATUS_FAILED
    )
    LOGGER.info(
        "substitution_promotion_failed user_id=%s reason=%s", user_id, outcome.reason
    )
    return STATUS_FAILED


async def _already_applied(db: Any, user_id: int, slot_id: str, target: str) -> bool:
    """True when the active plan's slot already holds the promoted exercise.

    The crash-recovery discriminator. It reads the CURRENT active plan on
    purpose -- unlike evidence classification, "is this change already in
    effect" is a question about now, not about history.
    """
    try:
        import planning
        from noam_coach.services import workout_slots

        active = await planning.get_active_plan(db, user_id, "workout")
    except Exception:
        LOGGER.exception("already_applied_check_failed user_id=%s", user_id)
        return False
    if not active:
        return False
    for session in (active.get("payload") or {}).get("sessions") or []:
        if not isinstance(session, dict):
            continue
        found = workout_slots.find_by_slot_id(session.get("exercises"), slot_id)
        if found is not None:
            return str(found[1].get("id") or "") == str(target)
    return False


async def _active_plan_signature(db: Any, user_id: int) -> str | None:
    """Split signature of the CURRENT active workout plan, or None."""
    try:
        import planning

        active = await planning.get_active_plan(db, user_id, "workout")
    except Exception:
        LOGGER.exception("active_plan_signature_failed user_id=%s", user_id)
        return None
    if not active:
        return None
    return signature_from_plan_payload(active.get("payload"))


__all__ = [
    "APPROVAL_KIND",
    "approve",
    "claim_status",
    "decline",
    "propose",
    "reclaim_abandoned",
    "collect_evidence",
    "find_promotable",
    "is_suppressed",
    "record_cooldown",
    "APPROVE_COOLDOWN_DAYS",
    "DECLINE_COOLDOWN_DAYS",
    "LOOKBACK_DAYS",
    "MAX_ROWS",
    "OPEN_STATUSES",
    "PROMOTABLE_REASONS",
    "PROCESSING_LEASE_MINUTES",
    "PROMOTION_THRESHOLD",
    "STATUS_APPROVED",
    "STATUS_DECLINED",
    "STATUS_FAILED",
    "STATUS_PENDING",
    "STATUS_PROCESSING",
    "STATUS_STALE",
    "signature_from_plan_payload",
    "split_signature",
    "subject_of",
    "utc_iso",
]
