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


__all__ = [
    "APPROVAL_KIND",
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
