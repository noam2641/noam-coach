"""The canonical concurrency-safe write boundary for ``daily_flags`` (ARCH-03).

HISTORY (FIX 57/22 → B1): every writer of ``daily_flags`` used to follow the
same unsafe shape — read the full JSON blob, mutate one or several keys in
Python, write the full blob back. Only the final UPSERT was atomic; the
read-modify-write sequence spanning it was not, so two concurrent writers
touching *different* keys silently dropped one another's update (last writer
wins on the whole document). ``patch_daily_flags`` (the CAS primitive over
migration 12's ``revision`` column) existed but had zero production callers.

B1 makes this module the SINGLE write boundary. The canonical contract:

1. Production writers never persist a full stale JSON snapshot. They either
   call :func:`patch_daily_flags` with a key-scoped mutator, or use the
   read-ledger pair :func:`read_flags_for_update` →
   :func:`commit_flags_update`, which converts the traditional
   "read dict / mutate / save dict" call shape into a per-key patch by
   diffing the final dict against the snapshot that THIS task actually read.
2. A patch touches only the keys the writer changed/removed; unknown and
   unrelated keys always survive.
3. ``revision`` advances by exactly 1 per committed mutation (CAS
   ``WHERE revision=?``; first row insert lands at revision 1).
4. Retries are bounded (``MAX_CAS_RETRIES``); a retry re-reads the CURRENT
   row and re-applies only the writer's own key diff, so both writers'
   non-conflicting changes survive a lost race.
5. Same-key conflicts are deterministic: the LAST SUCCESSFULLY COMMITTED
   writer wins that key (per-key last-write-wins at top-level-key
   granularity — nested sub-structures are owned wholesale by their
   top-level key's owner, see FIELD_OWNERS).
6. Concurrency is OBSERVABLE through the canonical product_events stream:
   a write that needed retries emits ``state.mutated``
   (entity=daily_flags, outcome=patched_after_retry); retry exhaustion
   emits ``error.captured`` (entity=daily_flags_conflict) and raises
   :class:`DailyFlagsConflict`. Events carry bounded metadata only (day
   key, attempted keys, revisions, retry count, owner) — never the payload.
   Observability failure never alters the CAS outcome (safe emit boundary
   plus a local guard).

Day-key policy (B3 / ARCH-02): callers derive the day key through
``daily_state.coaching_day_key`` (canonical coaching day for nutrition/day
state; calendar fallback when no confirmed bedtime exists). This module
stays day-key agnostic — it patches whatever row key the caller addresses.
"""

from __future__ import annotations

import json
from contextvars import ContextVar
from typing import Any, Callable

from helpers import utc_now

MAX_CAS_RETRIES = 5

# Bounded metadata for conflict events: never more key names than this.
_MAX_EVENT_KEYS = 16


class DailyFlagsConflict(RuntimeError):
    """Raised when a compare-and-swap patch could not land after retries."""


# ---------------------------------------------------------------------------
# Field ownership registry
# ---------------------------------------------------------------------------
# Answers, in code, "which subsystem writes each known field", which fields
# legitimately have multiple writers, and who may clear a field. This is a
# policy record (consulted by reviewers and asserted by tests), not a runtime
# enforcement framework — enforcement would add a failure mode to coaching
# writes for no correctness gain, since per-key patching already prevents
# cross-owner clobbering.
#
# merge semantics: "patch" = the owner replaces the whole top-level key value;
# "multi" = several owners patch the SAME key deliberately — per-key
# last-committed-wins applies between them and is safe because each write is
# a self-contained user statement (e.g. tapping צום after ריטלין), never a
# partial merge of another owner's sub-fields.
FIELD_OWNERS: dict[str, dict[str, Any]] = {
    # -- next-meal recommendation engine (noam_coach/services/next_meal.py).
    #    next_meal_workout_status carries the workout self-report INCLUDING
    #    cancellation ("cancelled") and is written from Telegram callbacks,
    #    free text AND the Mini App workout-status endpoint — all through
    #    save_next_meal_workout_status, so one owner, multiple surfaces.
    "next_meal_workout_status": {"owner": "next_meal", "clearers": ["next_meal"]},
    "next_meal_workout_status_at": {"owner": "next_meal", "clearers": ["next_meal"]},
    # B12/ARCH-14: the CONCRETE rescheduled time collected by the reschedule
    # flow (save_workout_reschedule_time) — never a vague "later".
    "next_meal_workout_expected_at": {"owner": "next_meal", "clearers": ["next_meal"]},
    "next_meal_quantity_scales": {"owner": "next_meal", "clearers": ["next_meal"]},
    "next_meal_recent_titles": {"owner": "next_meal", "clearers": ["next_meal"]},
    "next_meal_recent_titles_at": {"owner": "next_meal", "clearers": ["next_meal"]},
    "next_meal_size_pref": {"owner": "next_meal", "clearers": ["next_meal"]},
    "next_meal_rejections": {"owner": "next_meal", "clearers": ["next_meal"]},
    "next_meal_temp_avoid_items": {"owner": "next_meal", "clearers": ["next_meal"]},
    "next_meal_planned": {"owner": "next_meal", "clearers": ["next_meal"]},
    "next_meal_saved": {"owner": "next_meal", "clearers": ["next_meal"]},
    # -- daily menu state/lifecycle (daily_menu_state.py / daily_menu_edit.py)
    "active_daily_menu": {"owner": "daily_menu", "clearers": ["daily_menu"]},
    "daily_menu_message": {"owner": "daily_menu", "clearers": ["daily_menu"]},
    "daily_menu_last_edit_request": {"owner": "daily_menu_edit", "clearers": ["daily_menu_edit"]},
    # -- day check-ins / health flags (health_service.set_daily_flags callers:
    #    checkins, callback_menu flag:*, assistant morning_flag intent,
    #    health_jobs /flags text, record_medication). Deliberately MULTIPLE
    #    writers: each flag is one self-contained user statement; per-key
    #    last-committed-wins between them is the intended semantics.
    "ritalin": {"owner": "multi:day_checkin", "clearers": ["day_checkin"]},
    "fasting": {"owner": "multi:day_checkin", "clearers": ["day_checkin"]},
    "sleep_quality": {"owner": "multi:day_checkin", "clearers": ["day_checkin"]},
    "energy": {"owner": "multi:day_checkin", "clearers": ["day_checkin"]},
    "state": {"owner": "multi:day_checkin", "clearers": ["day_checkin"]},
    "medications": {"owner": "multi:day_checkin", "clearers": ["day_checkin"]},
    "notes": {"owner": "multi:day_checkin", "clearers": ["day_checkin"]},
    "morning_update": {"owner": "multi:day_checkin", "clearers": ["day_checkin"]},
}


def owner_for_key(key: str) -> str:
    entry = FIELD_OWNERS.get(key)
    return str(entry["owner"]) if entry else "unregistered"


# ---------------------------------------------------------------------------
# CAS primitive
# ---------------------------------------------------------------------------


async def _read_flags_and_revision(
    db: Any, user_id: int, day: str
) -> tuple[dict[str, Any], int]:
    row = await db.fetch_one(
        "SELECT flags, revision FROM daily_flags WHERE user_id=? AND day=?",
        (user_id, day),
    )
    if row is None:
        return {}, 0
    flags = json.loads(row["flags"]) if row["flags"] else {}
    return flags, int(row["revision"] or 1)


async def _emit_concurrency_event(
    db: Any,
    user_id: int,
    event: str,
    *,
    day: str,
    keys: list[str],
    owner: str,
    retry_count: int,
    outcome: str,
    expected_revision: int | None,
    observed_revision: int | None,
    entity: str,
    status: str,
) -> None:
    """Best-effort canonical observability; never alters CAS correctness."""
    try:
        from noam_coach.observability import taxonomy
        from noam_coach.observability.emit import emit_event

        canonical = {
            "state.mutated": taxonomy.STATE_MUTATED,
            "error.captured": taxonomy.ERROR_CAPTURED,
        }[event]
        bounded_keys = sorted(keys)[:_MAX_EVENT_KEYS]
        await emit_event(
            db,
            user_id,
            canonical,
            entity=entity,
            entity_id=day,
            source="daily_flags",
            status=status,
            outcome=outcome,
            properties={
                "domain": "daily_flags",
                "day": day,
                "keys": bounded_keys,
                "key_count": len(keys),
                "owner": owner,
                "retry_count": retry_count,
                "expected_revision": expected_revision,
                "observed_revision": observed_revision,
            },
        )
    except Exception:  # noqa: BLE001 — observability must not affect CAS.
        return


async def patch_daily_flags(
    db: Any,
    user_id: int,
    day: str,
    mutator: Callable[[dict[str, Any]], dict[str, Any]],
    *,
    created_at: str | None = None,
    owner: str = "unregistered",
    touched_keys: list[str] | None = None,
) -> dict[str, Any]:
    """Atomically apply ``mutator`` to the day's flags dict and persist it.

    ``mutator`` receives a shallow copy of the current flags dict and must
    return the dict to persist. On a lost compare-and-swap race the CURRENT
    row is re-read and the mutator re-applied to it, so a retry preserves
    the other writer's non-conflicting keys. Bounded by ``MAX_CAS_RETRIES``;
    exhaustion emits the canonical conflict event and raises
    :class:`DailyFlagsConflict` — no stale full-JSON overwrite ever occurs.

    ``touched_keys`` (optional) names the keys this patch intends to change —
    used only for bounded conflict/retry observability metadata.
    """
    created_at = created_at or utc_now()
    last_expected: int | None = None
    for attempt in range(MAX_CAS_RETRIES):
        current, revision = await _read_flags_and_revision(db, user_id, day)
        last_expected = revision
        next_flags = mutator(dict(current))
        payload = json.dumps(next_flags, ensure_ascii=False)
        if touched_keys is not None:
            event_keys = list(touched_keys)
        else:
            diff_changed, diff_removed = _diff_against_snapshot(current, next_flags)
            event_keys = [*diff_changed.keys(), *diff_removed]

        if revision == 0:
            # No row yet for this user/day -- insert; a concurrent first
            # writer racing this INSERT violates the PRIMARY KEY and we
            # simply retry, which then sees revision=1 and CASes normally.
            try:
                await db.execute(
                    """
                    INSERT INTO daily_flags(user_id, day, flags, created_at, revision)
                    VALUES(?, ?, ?, ?, 1)
                    """,
                    (user_id, day, payload, created_at),
                )
            except Exception:
                continue
            if attempt > 0:
                await _emit_concurrency_event(
                    db, user_id, "state.mutated",
                    day=day, keys=event_keys, owner=owner,
                    retry_count=attempt, outcome="patched_after_retry",
                    expected_revision=0, observed_revision=1,
                    entity="daily_flags", status="mutated",
                )
            return next_flags

        updated = await db.execute_rowcount(
            """
            UPDATE daily_flags
            SET flags=?, revision=revision+1
            WHERE user_id=? AND day=? AND revision=?
            """,
            (payload, user_id, day, revision),
        )
        if updated == 1:
            if attempt > 0:
                await _emit_concurrency_event(
                    db, user_id, "state.mutated",
                    day=day, keys=event_keys, owner=owner,
                    retry_count=attempt, outcome="patched_after_retry",
                    expected_revision=revision, observed_revision=revision + 1,
                    entity="daily_flags", status="mutated",
                )
            return next_flags
        # Lost the race: another writer changed the row between our read and
        # write. Loop: re-read fresh state and re-apply the mutator to it.

    _, observed = await _read_flags_and_revision(db, user_id, day)
    await _emit_concurrency_event(
        db, user_id, "error.captured",
        day=day, keys=list(touched_keys or []), owner=owner,
        retry_count=MAX_CAS_RETRIES, outcome="conflict",
        expected_revision=last_expected, observed_revision=observed,
        entity="daily_flags_conflict", status="failed",
    )
    raise DailyFlagsConflict(
        f"could not patch daily_flags for user={user_id} day={day} after "
        f"{MAX_CAS_RETRIES} retries"
    )


# ---------------------------------------------------------------------------
# Read-ledger adapter: keeps the traditional "read dict / mutate / save dict"
# writer shape safe without rewriting every call site.
# ---------------------------------------------------------------------------

# Task-local: (user_id, day) -> JSON-serialized snapshot of the flags this
# asyncio task read for a subsequent update. contextvars keep concurrent
# handlers from pairing with each other's snapshots.
_read_ledger: ContextVar[dict[tuple[int, str], str] | None] = ContextVar(
    "daily_flags_read_ledger", default=None
)


def _ledger() -> dict[tuple[int, str], str]:
    current = _read_ledger.get()
    if current is None:
        current = {}
        _read_ledger.set(current)
    return current


async def read_flags_for_update(db: Any, user_id: int, day: str) -> dict[str, Any]:
    """Read the day's flags AND remember, task-locally, exactly what was read.

    A later :func:`commit_flags_update` for the same (user, day) diffs the
    final dict against this snapshot so only the keys THIS writer actually
    changed are patched — concurrent writers' keys survive.
    """
    flags, _revision = await _read_flags_and_revision(db, user_id, day)
    _ledger()[(user_id, day)] = json.dumps(flags, ensure_ascii=False, sort_keys=True)
    return flags


def _diff_against_snapshot(
    snapshot: dict[str, Any], final: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    changed: dict[str, Any] = {}
    for key, value in final.items():
        if key not in snapshot or json.dumps(value, ensure_ascii=False, sort_keys=True) != json.dumps(
            snapshot[key], ensure_ascii=False, sort_keys=True
        ):
            changed[key] = value
    removed = [key for key in snapshot if key not in final]
    return changed, removed


async def commit_flags_update(
    db: Any,
    user_id: int,
    day: str,
    flags: dict[str, Any],
    *,
    owner: str = "unregistered",
) -> dict[str, Any]:
    """Persist a writer's changes as a per-key CAS patch.

    ``flags`` is the writer's FINAL desired dict (the traditional call
    shape). The patch applied is the per-key diff against the snapshot this
    task read via :func:`read_flags_for_update`; keys the writer did not
    touch are left to whatever the current row holds. When no snapshot was
    recorded (a writer that never read first), every provided key is treated
    as an intentional merge-set and nothing is removed — the conservative
    interpretation that can never clear another writer's key.
    """
    snapshot_json = _ledger().pop((user_id, day), None)
    if snapshot_json is None:
        changed = dict(flags)
        removed: list[str] = []
    else:
        changed, removed = _diff_against_snapshot(json.loads(snapshot_json), flags)
    if not changed and not removed:
        return flags

    def _apply(current: dict[str, Any]) -> dict[str, Any]:
        for key, value in changed.items():
            current[key] = value
        for key in removed:
            current.pop(key, None)
        return current

    return await patch_daily_flags(
        db, user_id, day, _apply,
        owner=owner,
        touched_keys=[*changed.keys(), *removed],
    )


async def get_daily_flags_with_revision(
    db: Any, user_id: int, day: str
) -> tuple[dict[str, Any], int]:
    """Read-only accessor exposing the current revision alongside the flags,
    for callers (menu/recommendation projections) that need to record which
    day-state revision they were built from (FIX 43's dependency metadata
    requirement).
    """
    return await _read_flags_and_revision(db, user_id, day)
