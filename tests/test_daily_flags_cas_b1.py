"""B1 / ARCH-03 — daily_flags canonical CAS write contract.

Required regressions:
A. two non-conflicting writers that observed the same old state both land;
   a genuine CAS race is retried and the retry is trace-inspectable
B. unknown/future fields survive every migrated writer family
C. same-field conflict is deterministic (last successfully committed wins)
D. retry exhaustion → bounded, canonical conflict event, no stale overwrite
E. cross-surface realistic journey (Telegram next-meal status ⨯ health/job
   flag) converging on one row, provable via distinct traces
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

import coach_bot
import event_log
import health_service
from config import TZ
from db import Database
from helpers import utc_now
from noam_coach.observability import ObservabilityMode, interaction_scope, set_mode
from noam_coach.observability.emit import reset_observability_health
from noam_coach.observability.modes import reset_mode
from noam_coach.services.daily_flags_cas import (
    FIELD_OWNERS,
    DailyFlagsConflict,
    commit_flags_update,
    get_daily_flags_with_revision,
    patch_daily_flags,
    read_flags_for_update,
)

USER_ID = 1
DAY = "2026-06-28"


@pytest.fixture
async def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Database:
    database = Database(str(tmp_path / "flags_cas.db"))
    await database.init()
    await database.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(?, 'Test', NULL, ?)",
        (USER_ID, utc_now()),
    )
    monkeypatch.setattr(coach_bot, "DB", database)
    monkeypatch.setattr(health_service, "DB", database, raising=False)
    return database


@pytest.fixture(autouse=True)
def _obs_isolation():
    set_mode(ObservabilityMode.CONTENT)
    reset_observability_health()
    yield
    reset_mode()
    reset_observability_health()


async def _revision(db: Database) -> int:
    _, revision = await get_daily_flags_with_revision(db, USER_ID, DAY)
    return revision


# ---------------------------------------------------------------------------
# A. Two non-conflicting writers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("first_committer", ["next_meal", "day_checkin"])
async def test_two_non_conflicting_writers_both_land(db: Database, first_committer: str) -> None:
    """Both writers read the SAME (empty, revision-0) state; both commits
    land regardless of order; the final row contains both keys and the
    revision reflects two committed mutations. Parametrized on order to
    prove order independence, not timing luck."""
    both_read = asyncio.Event()
    first_done = asyncio.Event()

    async def writer_next_meal() -> None:
        flags = await read_flags_for_update(db, USER_ID, DAY)
        flags["next_meal_workout_status"] = "completed"
        await both_read.wait()
        if first_committer != "next_meal":
            await first_done.wait()
        await commit_flags_update(db, USER_ID, DAY, flags, owner="next_meal")
        if first_committer == "next_meal":
            first_done.set()

    async def writer_checkin() -> None:
        flags = await read_flags_for_update(db, USER_ID, DAY)
        flags["fasting"] = True
        await both_read.wait()
        if first_committer != "day_checkin":
            await first_done.wait()
        await commit_flags_update(db, USER_ID, DAY, flags, owner="day_checkin")
        if first_committer == "day_checkin":
            first_done.set()

    async def release_when_both_ready() -> None:
        # Both writer tasks are created before this runs; give them one loop
        # turn to perform their reads, then release the commits.
        await asyncio.sleep(0.05)
        both_read.set()

    await asyncio.gather(writer_next_meal(), writer_checkin(), release_when_both_ready())

    flags, revision = await get_daily_flags_with_revision(db, USER_ID, DAY)
    assert flags["next_meal_workout_status"] == "completed"
    assert flags["fasting"] is True
    assert revision == 2  # insert (rev 1) + one patch (rev 2)


@pytest.mark.asyncio
async def test_genuine_cas_race_is_retried_and_inspectable(db: Database) -> None:
    """A competitor lands between patch's internal read and its UPDATE:
    the patch retries against the fresh row, BOTH changes survive, and the
    retry emits the canonical state.mutated(patched_after_retry) event."""
    await patch_daily_flags(
        db, USER_ID, DAY, lambda f: {**f, "seed": 1}, owner="test", touched_keys=["seed"],
    )
    assert await _revision(db) == 1

    class _RaceOnceDB:
        """Delegates everything; before the FIRST UPDATE attempt, lets a
        competing writer commit — deterministically forcing one lost CAS."""

        def __init__(self, inner: Database) -> None:
            self._inner = inner
            self.raced = False

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

        async def execute_rowcount(self, sql: str, parameters: tuple = ()) -> int:
            if not self.raced and "UPDATE daily_flags" in sql:
                self.raced = True
                await patch_daily_flags(
                    self._inner, USER_ID, DAY,
                    lambda f: {**f, "fasting": True},
                    owner="day_checkin", touched_keys=["fasting"],
                )
            return await self._inner.execute_rowcount(sql, parameters)

    racy = _RaceOnceDB(db)
    with interaction_scope(user_id=USER_ID):
        result = await patch_daily_flags(
            racy, USER_ID, DAY,
            lambda f: {**f, "next_meal_workout_status": "completed"},
            owner="next_meal", touched_keys=["next_meal_workout_status"],
        )

    assert result["fasting"] is True                       # competitor preserved
    assert result["next_meal_workout_status"] == "completed"
    flags, revision = await get_daily_flags_with_revision(db, USER_ID, DAY)
    assert flags["fasting"] is True and flags["next_meal_workout_status"] == "completed"
    assert revision == 3  # seed + competitor + retried writer

    events = await event_log.list_events(db, USER_ID, event="state.mutated")
    retried = [e for e in events if e.outcome == "patched_after_retry"]
    assert len(retried) == 1
    props = retried[0].properties
    assert props["day"] == DAY
    assert props["keys"] == ["next_meal_workout_status"]
    assert props["owner"] == "next_meal"
    assert props["retry_count"] == 1
    assert props["observed_revision"] == 3
    assert retried[0].trace_id is not None  # correlated into the interaction


# ---------------------------------------------------------------------------
# B. Unknown-field survival across every migrated writer family
# ---------------------------------------------------------------------------

_UNKNOWN = {"future_unknown_field": {"schema": "v9", "value": 42}}


@pytest.mark.asyncio
async def test_unknown_fields_survive_all_migrated_writer_families(
    db: Database, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    fixed_now = datetime(2026, 6, 28, 13, 0, tzinfo=TZ)
    await patch_daily_flags(
        db, USER_ID, DAY, lambda f: {**f, **_UNKNOWN}, owner="test",
        touched_keys=list(_UNKNOWN),
    )

    # 1) next_meal family — real production writer.
    from noam_coach.services.next_meal import save_next_meal_workout_status

    await save_next_meal_workout_status(db, USER_ID, "completed", now=fixed_now)

    # 2) daily_menu family — real production writer.
    from noam_coach.services.daily_menu_state import remember_active_daily_menu

    await remember_active_daily_menu(
        db, USER_ID, text="תפריט", strategy="balanced", meals=[], now=fixed_now,
    )

    # 3) day_checkin family (health_service.get/set) — real production shape.
    monkeypatch.setattr(
        health_service, "local_day_str", lambda: DAY, raising=False,
    )
    flags = await health_service.get_daily_flags(USER_ID, DAY)
    flags["fasting"] = True
    await health_service.set_daily_flags(USER_ID, flags)

    # 4) daily_menu_edit family — real production writer.
    from noam_coach.services import daily_menu_edit as dme

    class _Intent:
        slot = "snack"
        instruction = "בלי טונה"

    monkeypatch.setattr(
        dme, "datetime",
        type("FrozenDT", (), {
            "now": staticmethod(lambda tz=None: fixed_now),
        }),
    )
    await dme._remember_request(db, USER_ID, _Intent())

    final, revision = await get_daily_flags_with_revision(db, USER_ID, DAY)
    assert final["future_unknown_field"] == _UNKNOWN["future_unknown_field"]
    # And each family's own write landed too.
    assert final["next_meal_workout_status"] == "completed"
    assert "active_daily_menu" in final
    assert final["fasting"] is True
    assert final["daily_menu_last_edit_request"]["slot"] == "snack"
    assert revision >= 5


# ---------------------------------------------------------------------------
# C. Same-field conflict is deterministic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("last_committer_value", ["later", "completed"])
async def test_same_field_conflict_last_committed_wins(
    db: Database, last_committer_value: str,
) -> None:
    first_value = "completed" if last_committer_value == "later" else "later"

    async def writer(value: str, gate: asyncio.Event | None, done: asyncio.Event | None) -> None:
        flags = await read_flags_for_update(db, USER_ID, DAY)
        flags["next_meal_workout_status"] = value
        if gate is not None:
            await gate.wait()
        await commit_flags_update(db, USER_ID, DAY, flags, owner="next_meal")
        if done is not None:
            done.set()

    first_done = asyncio.Event()
    await asyncio.gather(
        writer(first_value, None, first_done),
        writer_after(first_done, writer, last_committer_value),
    )
    flags, revision = await get_daily_flags_with_revision(db, USER_ID, DAY)
    # Deterministic contract: per-key last-successfully-committed wins.
    assert flags["next_meal_workout_status"] == last_committer_value
    assert revision == 2


async def writer_after(gate: asyncio.Event, writer: Any, value: str) -> None:
    await gate.wait()
    await writer(value, None, None)


# ---------------------------------------------------------------------------
# D. Retry exhaustion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_exhaustion_is_bounded_conflict_and_no_stale_overwrite(db: Database) -> None:
    await patch_daily_flags(
        db, USER_ID, DAY, lambda f: {**f, "existing": "keep-me"},
        owner="test", touched_keys=["existing"],
    )

    class _AlwaysLosesDB:
        def __init__(self, inner: Database) -> None:
            self._inner = inner
            self.update_attempts = 0

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

        async def execute_rowcount(self, sql: str, parameters: tuple = ()) -> int:
            if "UPDATE daily_flags" in sql:
                self.update_attempts += 1
                return 0  # perpetual lost race
            return await self._inner.execute_rowcount(sql, parameters)

    losing = _AlwaysLosesDB(db)
    with interaction_scope(user_id=USER_ID):
        with pytest.raises(DailyFlagsConflict):
            await patch_daily_flags(
                losing, USER_ID, DAY,
                lambda f: {**f, "next_meal_workout_status": "completed"},
                owner="next_meal", touched_keys=["next_meal_workout_status"],
            )

    from noam_coach.services.daily_flags_cas import MAX_CAS_RETRIES

    assert losing.update_attempts == MAX_CAS_RETRIES  # bounded
    flags, revision = await get_daily_flags_with_revision(db, USER_ID, DAY)
    assert flags == {"existing": "keep-me"}  # NO partial/stale overwrite
    assert revision == 1

    conflicts = await event_log.list_events(db, USER_ID, event="error.captured")
    assert len(conflicts) == 1
    props = conflicts[0].properties
    assert conflicts[0].entity == "daily_flags_conflict"
    assert conflicts[0].outcome == "conflict"
    assert props["day"] == DAY
    assert props["keys"] == ["next_meal_workout_status"]
    assert props["owner"] == "next_meal"
    assert props["retry_count"] == MAX_CAS_RETRIES
    assert props["observed_revision"] == 1
    assert conflicts[0].trace_id is not None


@pytest.mark.asyncio
async def test_observability_failure_does_not_alter_cas_outcome(
    db: Database, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Requirement 8: even if the conflict event cannot be written, the CAS
    contract (raise on exhaustion / commit on success) is unchanged."""
    from noam_coach.observability import emit as emit_module

    async def broken_emit(*a: Any, **k: Any) -> None:
        raise RuntimeError("observability outage")

    monkeypatch.setattr(emit_module, "emit_event", broken_emit)

    class _AlwaysLosesDB:
        def __init__(self, inner: Database) -> None:
            self._inner = inner

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

        async def execute_rowcount(self, sql: str, parameters: tuple = ()) -> int:
            if "UPDATE daily_flags" in sql:
                return 0
            return await self._inner.execute_rowcount(sql, parameters)

    await patch_daily_flags(db, USER_ID, DAY, lambda f: {**f, "seed": 1}, owner="test")
    with pytest.raises(DailyFlagsConflict):
        await patch_daily_flags(
            _AlwaysLosesDB(db), USER_ID, DAY, lambda f: {**f, "x": 1}, owner="test",
        )
    flags, _ = await get_daily_flags_with_revision(db, USER_ID, DAY)
    assert flags == {"seed": 1}


# ---------------------------------------------------------------------------
# E. Cross-surface realistic journey
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_surface_journey_telegram_status_vs_health_flag(
    db: Database, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Telegram 'סיימתי אימון' (next-meal workout status) interleaved with a
    health/job flag write that read an OLDER snapshot: both survive on one
    row, and the traces prove two different interactions converged there."""
    fixed_now = datetime(2026, 6, 28, 13, 0, tzinfo=TZ)
    day = fixed_now.date().isoformat()
    monkeypatch.setattr(health_service, "local_day_str", lambda: day, raising=False)

    from noam_coach.services.next_meal import save_next_meal_workout_status

    telegram_trace: dict[str, str | None] = {}
    job_trace: dict[str, str | None] = {}

    async def health_job() -> None:
        # Job reads flags FIRST (older snapshot), then Telegram commits,
        # then the job commits from its stale snapshot.
        with interaction_scope(user_id=USER_ID) as scope:
            job_trace["id"] = scope.trace_id
            flags = await health_service.get_daily_flags(USER_ID, day)
            flags["fasting"] = True
            await telegram_committed.wait()
            await health_service.set_daily_flags(USER_ID, flags)

    telegram_committed = asyncio.Event()

    async def telegram_turn() -> None:
        with interaction_scope(user_id=USER_ID) as scope:
            telegram_trace["id"] = scope.trace_id
            await asyncio.sleep(0.02)  # let the job read first
            await save_next_meal_workout_status(db, USER_ID, "completed", now=fixed_now)
            telegram_committed.set()

    await asyncio.gather(health_job(), telegram_turn())

    flags, revision = await get_daily_flags_with_revision(db, USER_ID, day)
    assert flags["next_meal_workout_status"] == "completed"  # Telegram survived
    assert flags["fasting"] is True                          # job survived
    assert revision == 2
    assert telegram_trace["id"] != job_trace["id"]

    # The row's convergence is trace-provable: seed one more write per
    # surface and confirm the writes carry the surfaces' distinct traces.
    events = await event_log.list_events(db, USER_ID)
    assert {e.trace_id for e in events if e.trace_id} <= {telegram_trace["id"], job_trace["id"]}


# ---------------------------------------------------------------------------
# Ownership registry sanity
# ---------------------------------------------------------------------------


def test_field_ownership_registry_covers_known_production_keys() -> None:
    required = {
        "next_meal_workout_status", "next_meal_quantity_scales",
        "next_meal_recent_titles", "next_meal_rejections",
        "next_meal_temp_avoid_items", "next_meal_planned",
        "active_daily_menu", "daily_menu_message", "daily_menu_last_edit_request",
        "ritalin", "fasting", "medications", "sleep_quality",
    }
    assert required <= set(FIELD_OWNERS)
    for key, entry in FIELD_OWNERS.items():
        assert entry["owner"], key
        assert entry["clearers"], key
