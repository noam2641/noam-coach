"""Compare-and-swap access to the shared ``daily_flags`` JSON document (FIX 57/22).

Every existing writer of ``daily_flags`` (``health_service.py``, ``next_meal.py``,
``daily_menu_state.py``, ``daily_menu_edit.py``) follows the same unsafe shape:
read the full JSON blob, mutate one key in Python, write the full blob back.
Only the final ``UPDATE``/``UPSERT`` statement is atomic -- the read-modify-write
sequence spanning it is not, so two concurrent writers touching *different*
keys can still silently drop one another's update (last writer wins on the
whole document, not per key).

``patch_daily_flags`` closes that gap without requiring a schema rewrite: it
reads the current ``(flags, revision)`` pair, applies a caller-supplied pure
mutator to a copy of the dict, and writes back only if the revision has not
changed since the read, retrying on conflict. This is additive -- it does not
replace ``get_daily_flags``, so existing simple read call sites are unaffected;
only writers that need racy-safe key patches should switch to this helper.
"""

from __future__ import annotations

import json
from typing import Any, Callable

MAX_CAS_RETRIES = 5


class DailyFlagsConflict(RuntimeError):
    """Raised when a compare-and-swap patch could not land after retries."""


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


async def patch_daily_flags(
    db: Any,
    user_id: int,
    day: str,
    mutator: Callable[[dict[str, Any]], dict[str, Any]],
    *,
    created_at: str,
) -> dict[str, Any]:
    """Atomically apply ``mutator`` to the day's flags dict and persist it.

    ``mutator`` receives a shallow copy of the current flags dict and must
    return the dict to persist (it may be the same object, mutated in place,
    or a new one). Retries on a lost compare-and-swap race up to
    ``MAX_CAS_RETRIES`` times before raising ``DailyFlagsConflict``.
    """
    for _ in range(MAX_CAS_RETRIES):
        current, revision = await _read_flags_and_revision(db, user_id, day)
        next_flags = mutator(dict(current))
        payload = json.dumps(next_flags, ensure_ascii=False)

        if revision == 0:
            # No row yet for this user/day -- insert; a concurrent first
            # writer racing this INSERT will violate the PRIMARY KEY and we
            # simply retry, which will then see revision=1 and CAS normally.
            try:
                await db.execute(
                    """
                    INSERT INTO daily_flags(user_id, day, flags, created_at, revision)
                    VALUES(?, ?, ?, ?, 1)
                    """,
                    (user_id, day, payload, created_at),
                )
                return next_flags
            except Exception:
                continue

        updated = await db.execute_rowcount(
            """
            UPDATE daily_flags
            SET flags=?, revision=revision+1
            WHERE user_id=? AND day=? AND revision=?
            """,
            (payload, user_id, day, revision),
        )
        if updated == 1:
            return next_flags
        # Lost the race: another writer changed the row between our read and
        # write. Re-read and re-apply the mutator against the fresh state.

    raise DailyFlagsConflict(
        f"could not patch daily_flags for user={user_id} day={day} after "
        f"{MAX_CAS_RETRIES} retries"
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
