"""G2.3B-1: durable idempotency operation core.

Every test here is deterministic: the clock is injected, concurrency uses
asyncio barriers rather than timing, and failures are injected through explicit
hooks. Nothing sleeps and nothing calls OpenAI.

The properties under test are the ones the design gate demanded:

* one winner under concurrency, decided inside the CAS -- not by a lock
* a reserved target identity that survives takeover and restart
* attempt_id fencing on EVERY persistent side effect, not just complete/fail
* the menu revision and its produced identity written by ONE mutation, so the
  "revision persisted but operation record not updated" window cannot exist
* unrelated daily_flags keys survive contention
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import coach_bot
from config import TZ
from noam_coach.services import daily_menu_operations as ops
from noam_coach.services.daily_menu_state import ACTIVE_DAILY_MENU_KEY

DAY = "2026-07-21"
T0 = datetime(2026, 7, 21, 9, 0, 0, tzinfo=TZ)


async def _make_db(tmp_path: Path) -> coach_bot.Database:
    db = coach_bot.Database(str(tmp_path / "coach.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (coach_bot.utc_now(),),
    )
    return db


def _delivery_identity(menu_id: str = "menu-1-2026-07-21-3", plan_id: object = 17):
    return ops.build_delivery_identity(
        coaching_day_key=DAY, menu_id=menu_id, plan_id=plan_id,
    )


def _refresh_identity(current_menu_id: str | None = "menu-1-2026-07-21-3"):
    return ops.build_refresh_identity(
        coaching_day_key=DAY, current_menu_id=current_menu_id, plan_id=17,
    )


async def _claim_refresh(db, *, attempt_id, now=T0, target="menu-1-2026-07-21-4",
                         revision=4, source_menu="menu-1-2026-07-21-3"):
    return await ops.claim_operation(
        db, 1, DAY,
        kind=ops.KIND_REFRESH_REQUEST,
        identity=_refresh_identity(source_menu),
        attempt_id=attempt_id,
        now=now,
        source_identity={"current_menu_id": source_menu, "current_revision": 3},
        target_menu_id=target,
        target_revision=revision,
        requested_by="refresh",
    )


async def _read_flags(db) -> dict:
    from noam_coach.services.daily_flags_cas import get_daily_flags_with_revision

    flags, _rev = await get_daily_flags_with_revision(db, 1, DAY)
    return flags


async def _set_active_menu(db, *, menu_id: str, revision: int) -> None:
    from noam_coach.services.daily_flags_cas import patch_daily_flags

    def _mutate(current):
        current[ACTIVE_DAILY_MENU_KEY] = {
            "text": "existing", "revision": revision, "menu_id": menu_id,
        }
        return current

    await patch_daily_flags(db, 1, DAY, _mutate, owner="test")


# --- 1-5: claim, suppression, takeover, sticky reservation ------------------


@pytest.mark.asyncio
async def test_new_claim_succeeds(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    result = await _claim_refresh(db, attempt_id="A")
    assert result.status == ops.ACQUIRED
    assert result.record["attempt_id"] == "A"
    assert result.record["attempt"] == 1


@pytest.mark.asyncio
async def test_equivalent_live_claim_is_suppressed(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    assert (await _claim_refresh(db, attempt_id="A")).status == ops.ACQUIRED

    # 60s later the 120s lease is still live.
    second = await _claim_refresh(db, attempt_id="B", now=T0 + timedelta(seconds=60))
    assert second.status == ops.IN_FLIGHT
    assert second.reason == "in_flight_lease_active"


@pytest.mark.asyncio
async def test_expired_claim_is_taken_over(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    await _claim_refresh(db, attempt_id="A")
    later = T0 + timedelta(seconds=ops.MENU_SEND_LEASE_SECONDS + 1)
    second = await _claim_refresh(db, attempt_id="B", now=later)
    assert second.status == ops.ACQUIRED
    assert second.record["attempt_id"] == "B"
    assert second.record["attempt"] == 2
    assert second.record["previous_attempt_id"] == "A"


@pytest.mark.asyncio
async def test_takeover_retains_same_target_menu_id(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    await _claim_refresh(db, attempt_id="A", target="menu-RESERVED", revision=4)
    later = T0 + timedelta(seconds=ops.MENU_SEND_LEASE_SECONDS + 1)
    # Deliberately offer a DIFFERENT target: the reservation must win.
    second = await _claim_refresh(db, attempt_id="B", now=later,
                                  target="menu-DIFFERENT", revision=99)
    assert second.record["target_menu_id"] == "menu-RESERVED"


@pytest.mark.asyncio
async def test_takeover_retains_same_target_revision(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    await _claim_refresh(db, attempt_id="A", target="menu-RESERVED", revision=4)
    later = T0 + timedelta(seconds=ops.MENU_SEND_LEASE_SECONDS + 1)
    second = await _claim_refresh(db, attempt_id="B", now=later,
                                  target="menu-DIFFERENT", revision=99)
    assert second.record["target_revision"] == 4


# --- 6-9: ownership fencing on renew / complete / fail ----------------------


@pytest.mark.asyncio
async def test_only_owning_attempt_id_can_renew(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    await _claim_refresh(db, attempt_id="A")
    renewed = await ops.renew_operation(
        db, 1, DAY, kind=ops.KIND_REFRESH_REQUEST,
        identity=_refresh_identity(), attempt_id="A",
        now=T0 + timedelta(seconds=40),
    )
    assert renewed.status == ops.ACQUIRED
    expiry = datetime.fromisoformat(renewed.record["lease_expires_at"])
    assert expiry > datetime.fromisoformat(
        (await _read_flags(db))[ops.DAILY_MENU_REFRESH_OP_KEY]["created_at"]
    )


@pytest.mark.asyncio
async def test_stale_owner_cannot_renew(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    await _claim_refresh(db, attempt_id="A")
    later = T0 + timedelta(seconds=ops.MENU_SEND_LEASE_SECONDS + 1)
    await _claim_refresh(db, attempt_id="B", now=later)          # takeover

    lost = await ops.renew_operation(
        db, 1, DAY, kind=ops.KIND_REFRESH_REQUEST,
        identity=_refresh_identity(), attempt_id="A", now=later,
    )
    assert lost.status == ops.OWNERSHIP_LOST
    assert (await _read_flags(db))[ops.DAILY_MENU_REFRESH_OP_KEY]["attempt_id"] == "B"


@pytest.mark.asyncio
async def test_stale_owner_cannot_complete(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    await _claim_refresh(db, attempt_id="A")
    later = T0 + timedelta(seconds=ops.MENU_SEND_LEASE_SECONDS + 1)
    await _claim_refresh(db, attempt_id="B", now=later)

    lost = await ops.complete_operation(
        db, 1, DAY, kind=ops.KIND_REFRESH_REQUEST,
        identity=_refresh_identity(), attempt_id="A", now=later,
    )
    assert lost.status == ops.OWNERSHIP_LOST
    record = (await _read_flags(db))[ops.DAILY_MENU_REFRESH_OP_KEY]
    assert record["status"] == ops.IN_FLIGHT      # B's record untouched
    assert record["attempt_id"] == "B"


@pytest.mark.asyncio
async def test_stale_owner_cannot_fail(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    await _claim_refresh(db, attempt_id="A")
    later = T0 + timedelta(seconds=ops.MENU_SEND_LEASE_SECONDS + 1)
    await _claim_refresh(db, attempt_id="B", now=later)

    lost = await ops.fail_operation(
        db, 1, DAY, kind=ops.KIND_REFRESH_REQUEST,
        identity=_refresh_identity(), attempt_id="A",
        category="telegram_network", now=later,
    )
    assert lost.status == ops.OWNERSHIP_LOST
    assert (await _read_flags(db))[ops.DAILY_MENU_REFRESH_OP_KEY]["status"] == ops.IN_FLIGHT


# --- 10-11: result persistence is fenced ------------------------------------


@pytest.mark.asyncio
async def test_stale_owner_cannot_persist_a_generated_result(tmp_path: Path) -> None:
    """The critical fence: a late AI return after takeover writes NOTHING."""
    db = await _make_db(tmp_path)
    await _set_active_menu(db, menu_id="menu-1-2026-07-21-3", revision=3)
    await _claim_refresh(db, attempt_id="A")
    later = T0 + timedelta(seconds=ops.MENU_SEND_LEASE_SECONDS + 1)
    await _claim_refresh(db, attempt_id="B", now=later)

    stale = await ops.persist_refresh_result(
        db, 1, DAY, identity=_refresh_identity(), attempt_id="A",
        text="stale generation", now=later,
    )
    assert stale.status == ops.OWNERSHIP_LOST

    flags = await _read_flags(db)
    assert flags[ACTIVE_DAILY_MENU_KEY]["menu_id"] == "menu-1-2026-07-21-3"
    assert flags[ACTIVE_DAILY_MENU_KEY]["revision"] == 3
    assert flags[ops.DAILY_MENU_REFRESH_OP_KEY]["produced_menu_id"] is None


@pytest.mark.asyncio
async def test_heartbeat_failure_fences_later_result_persistence(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    await _set_active_menu(db, menu_id="menu-1-2026-07-21-3", revision=3)
    await _claim_refresh(db, attempt_id="A")
    later = T0 + timedelta(seconds=ops.MENU_SEND_LEASE_SECONDS + 1)
    await _claim_refresh(db, attempt_id="B", now=later)

    # The heartbeat is what tells A it lost ownership...
    renewal = await ops.renew_operation(
        db, 1, DAY, kind=ops.KIND_REFRESH_REQUEST,
        identity=_refresh_identity(), attempt_id="A", now=later,
    )
    assert renewal.status == ops.OWNERSHIP_LOST

    # ...and the CAS check independently refuses the write even if A ignored it.
    assert (await ops.persist_refresh_result(
        db, 1, DAY, identity=_refresh_identity(), attempt_id="A",
        text="ignored", now=later,
    )).status == ops.OWNERSHIP_LOST


# --- 12-15: source identity and revision fencing ----------------------------


@pytest.mark.asyncio
async def test_source_menu_change_during_generation_is_detected(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    await _set_active_menu(db, menu_id="menu-1-2026-07-21-3", revision=3)
    await _claim_refresh(db, attempt_id="A")

    # Someone else advanced the menu while A was generating.
    await _set_active_menu(db, menu_id="menu-1-2026-07-21-9", revision=9)

    result = await ops.persist_refresh_result(
        db, 1, DAY, identity=_refresh_identity(), attempt_id="A",
        text="based on stale source", now=T0 + timedelta(seconds=30),
    )
    assert result.status == ops.STALE_SOURCE


@pytest.mark.asyncio
async def test_stale_source_result_cannot_overwrite_newer_active_menu(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    await _set_active_menu(db, menu_id="menu-1-2026-07-21-3", revision=3)
    await _claim_refresh(db, attempt_id="A")
    await _set_active_menu(db, menu_id="menu-1-2026-07-21-9", revision=9)

    await ops.persist_refresh_result(
        db, 1, DAY, identity=_refresh_identity(), attempt_id="A",
        text="stale", now=T0 + timedelta(seconds=30),
    )
    flags = await _read_flags(db)
    assert flags[ACTIVE_DAILY_MENU_KEY]["menu_id"] == "menu-1-2026-07-21-9"
    assert flags[ACTIVE_DAILY_MENU_KEY]["revision"] == 9
    assert flags[ACTIVE_DAILY_MENU_KEY]["text"] == "existing"


@pytest.mark.asyncio
async def test_active_revision_advanced_before_commit_is_rejected(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    # Same source menu_id, but the revision already reached our reservation.
    await _set_active_menu(db, menu_id="menu-1-2026-07-21-3", revision=3)
    await _claim_refresh(db, attempt_id="A", target="menu-1-2026-07-21-4", revision=4)

    from noam_coach.services.daily_flags_cas import patch_daily_flags

    def _bump(current):
        current[ACTIVE_DAILY_MENU_KEY] = {
            "text": "newer", "revision": 5, "menu_id": "menu-1-2026-07-21-3",
        }
        return current

    await patch_daily_flags(db, 1, DAY, _bump, owner="test")

    result = await ops.persist_refresh_result(
        db, 1, DAY, identity=_refresh_identity(), attempt_id="A",
        text="late", now=T0 + timedelta(seconds=30),
    )
    assert result.status == ops.CONFLICT
    assert result.reason == "revision_advanced"


@pytest.mark.asyncio
async def test_revision_conflict_leaves_neither_result_nor_produced_identity(
    tmp_path: Path,
) -> None:
    db = await _make_db(tmp_path)
    await _set_active_menu(db, menu_id="menu-1-2026-07-21-3", revision=3)
    await _claim_refresh(db, attempt_id="A")
    await _set_active_menu(db, menu_id="menu-1-2026-07-21-9", revision=9)

    await ops.persist_refresh_result(
        db, 1, DAY, identity=_refresh_identity(), attempt_id="A",
        text="rejected", now=T0 + timedelta(seconds=30),
    )
    flags = await _read_flags(db)
    assert flags[ACTIVE_DAILY_MENU_KEY]["text"] == "existing"          # no result
    assert flags[ops.DAILY_MENU_REFRESH_OP_KEY]["produced_menu_id"] is None  # no identity


# --- 16-19: atomicity and reuse ---------------------------------------------


@pytest.mark.asyncio
async def test_result_menu_and_produced_menu_id_are_written_atomically(
    tmp_path: Path,
) -> None:
    db = await _make_db(tmp_path)
    await _set_active_menu(db, menu_id="menu-1-2026-07-21-3", revision=3)
    await _claim_refresh(db, attempt_id="A", target="menu-1-2026-07-21-4", revision=4)

    result = await ops.persist_refresh_result(
        db, 1, DAY, identity=_refresh_identity(), attempt_id="A",
        text="fresh menu", produced_plan_id=17, now=T0 + timedelta(seconds=30),
    )
    assert result.status == ops.ACQUIRED

    flags = await _read_flags(db)
    # Both sides landed together.
    assert flags[ACTIVE_DAILY_MENU_KEY]["menu_id"] == "menu-1-2026-07-21-4"
    assert flags[ACTIVE_DAILY_MENU_KEY]["revision"] == 4
    assert flags[ACTIVE_DAILY_MENU_KEY]["text"] == "fresh menu"
    assert flags[ops.DAILY_MENU_REFRESH_OP_KEY]["produced_menu_id"] == "menu-1-2026-07-21-4"
    assert flags[ops.DAILY_MENU_REFRESH_OP_KEY]["produced_plan_id"] == "17"


@pytest.mark.asyncio
async def test_injected_cas_failure_leaves_neither_side_persisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inject a failure at the single write: neither key may change."""
    db = await _make_db(tmp_path)
    await _set_active_menu(db, menu_id="menu-1-2026-07-21-3", revision=3)
    await _claim_refresh(db, attempt_id="A")

    from noam_coach.services import daily_flags_cas

    async def _boom(*_a, **_k):
        raise RuntimeError("injected CAS failure")

    monkeypatch.setattr(daily_flags_cas, "patch_daily_flags", _boom)

    result = await ops.persist_refresh_result(
        db, 1, DAY, identity=_refresh_identity(), attempt_id="A",
        text="never stored", now=T0 + timedelta(seconds=30),
    )
    assert result.status == ops.CONFLICT

    monkeypatch.undo()
    flags = await _read_flags(db)
    assert flags[ACTIVE_DAILY_MENU_KEY]["text"] == "existing"
    assert flags[ops.DAILY_MENU_REFRESH_OP_KEY]["produced_menu_id"] is None


@pytest.mark.asyncio
async def test_takeover_after_atomic_persistence_reuses_produced_identity(
    tmp_path: Path,
) -> None:
    """Crash AFTER the atomic write: the takeover must reuse, not regenerate."""
    db = await _make_db(tmp_path)
    await _set_active_menu(db, menu_id="menu-1-2026-07-21-3", revision=3)
    await _claim_refresh(db, attempt_id="A", target="menu-1-2026-07-21-4", revision=4)
    await ops.persist_refresh_result(
        db, 1, DAY, identity=_refresh_identity(), attempt_id="A",
        text="generated once", now=T0 + timedelta(seconds=30),
    )

    later = T0 + timedelta(seconds=ops.MENU_SEND_LEASE_SECONDS + 1)
    takeover = await _claim_refresh(db, attempt_id="B", now=later,
                                    target="menu-IGNORED", revision=99)
    assert takeover.status == ops.ACQUIRED
    # The recovering worker sees the produced identity and can skip generation.
    assert takeover.record["produced_menu_id"] == "menu-1-2026-07-21-4"
    assert takeover.record["target_menu_id"] == "menu-1-2026-07-21-4"


@pytest.mark.asyncio
async def test_no_second_revision_after_successful_atomic_persistence(
    tmp_path: Path,
) -> None:
    db = await _make_db(tmp_path)
    await _set_active_menu(db, menu_id="menu-1-2026-07-21-3", revision=3)
    await _claim_refresh(db, attempt_id="A", target="menu-1-2026-07-21-4", revision=4)
    await ops.persist_refresh_result(
        db, 1, DAY, identity=_refresh_identity(), attempt_id="A",
        text="generated once", now=T0 + timedelta(seconds=30),
    )

    later = T0 + timedelta(seconds=ops.MENU_SEND_LEASE_SECONDS + 1)
    await _claim_refresh(db, attempt_id="B", now=later)
    replay = await ops.persist_refresh_result(
        db, 1, DAY, identity=_refresh_identity(), attempt_id="B",
        text="would be a second generation", now=later,
    )
    assert replay.status == ops.ACQUIRED
    assert replay.reason == "already_persisted"

    flags = await _read_flags(db)
    assert flags[ACTIVE_DAILY_MENU_KEY]["revision"] == 4          # still ONE revision
    assert flags[ACTIVE_DAILY_MENU_KEY]["text"] == "generated once"


# --- 20-25: concurrency, isolation, schema safety ---------------------------


@pytest.mark.asyncio
async def test_concurrent_claims_only_one_wins(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    barrier = asyncio.Event()

    async def _attempt(attempt_id: str):
        await barrier.wait()
        return await _claim_refresh(db, attempt_id=attempt_id)

    task_a = asyncio.create_task(_attempt("A"))
    task_b = asyncio.create_task(_attempt("B"))
    barrier.set()
    results = await asyncio.gather(task_a, task_b)

    acquired = [r for r in results if r.status == ops.ACQUIRED]
    assert len(acquired) == 1
    assert [r.status for r in results].count(ops.IN_FLIGHT) == 1


@pytest.mark.asyncio
async def test_unrelated_daily_flags_fields_survive_every_cas_retry(
    tmp_path: Path,
) -> None:
    db = await _make_db(tmp_path)
    from noam_coach.services.daily_flags_cas import patch_daily_flags

    def _other_writer(current):
        current["sleep_quality"] = "good"
        current["energy"] = "high"
        return current

    await patch_daily_flags(db, 1, DAY, _other_writer, owner="checkins")
    await _claim_refresh(db, attempt_id="A")
    await ops.renew_operation(
        db, 1, DAY, kind=ops.KIND_REFRESH_REQUEST,
        identity=_refresh_identity(), attempt_id="A",
        now=T0 + timedelta(seconds=40),
    )

    flags = await _read_flags(db)
    assert flags["sleep_quality"] == "good"
    assert flags["energy"] == "high"
    assert ops.DAILY_MENU_REFRESH_OP_KEY in flags


@pytest.mark.asyncio
async def test_delivery_and_refresh_identities_cannot_collide(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    delivery = _delivery_identity()
    refresh = _refresh_identity()

    assert ops.canonical_key(delivery) != ops.canonical_key(refresh)
    assert not ops.identities_match(delivery, refresh)

    assert (await ops.claim_operation(
        db, 1, DAY, kind=ops.KIND_DELIVERY, identity=delivery,
        attempt_id="D", now=T0,
    )).status == ops.ACQUIRED
    # A refresh claim is stored under a DIFFERENT key and is unaffected.
    assert (await _claim_refresh(db, attempt_id="R")).status == ops.ACQUIRED

    flags = await _read_flags(db)
    assert flags[ops.DAILY_MENU_SEND_OP_KEY]["attempt_id"] == "D"
    assert flags[ops.DAILY_MENU_REFRESH_OP_KEY]["attempt_id"] == "R"


@pytest.mark.asyncio
async def test_unsupported_schema_record_is_handled_safely(tmp_path: Path) -> None:
    """A legacy/malformed record must never suppress a new claim (fail-open)."""
    db = await _make_db(tmp_path)
    from noam_coach.services.daily_flags_cas import patch_daily_flags

    def _legacy(current):
        current[ops.DAILY_MENU_REFRESH_OP_KEY] = {"status": "in_flight", "junk": True}
        return current

    await patch_daily_flags(db, 1, DAY, _legacy, owner="test")

    result = await _claim_refresh(db, attempt_id="A")
    assert result.status == ops.ACQUIRED
    assert await ops.read_operation(db, 1, DAY, kind=ops.KIND_REFRESH_REQUEST) is not None


def test_semantic_identity_ignores_requested_by_labels() -> None:
    """The same menu delivered via different UI actions is ONE identity."""
    from_refresh = _delivery_identity()
    from_button = _delivery_identity()
    assert ops.identities_match(from_refresh, from_button)
    assert ops.canonical_key(from_refresh) == ops.canonical_key(from_button)
    assert "requested_by" not in from_refresh
    assert "refresh" not in ops.canonical_key(from_refresh)


def test_canonical_identity_serialization_cannot_be_ambiguous() -> None:
    with pytest.raises(ValueError):
        ops.build_delivery_identity(coaching_day_key="2026-07-21", menu_id="a|b")
    with pytest.raises(ValueError):
        ops.build_refresh_identity(
            coaching_day_key="2026-07-21", current_menu_id="x", plan_id="1|2",
        )
    # Absent components normalise rather than collapsing to an empty string.
    identity = ops.build_delivery_identity(coaching_day_key=DAY, menu_id=None, plan_id=None)
    assert identity["menu_id"] == ops.NONE_SENTINEL
    assert identity["plan_id"] == ops.NONE_SENTINEL
    # Field order never changes the canonical form.
    assert ops.canonical_key(dict(reversed(list(identity.items())))) == ops.canonical_key(identity)


@pytest.mark.asyncio
async def test_no_unbounded_operation_history_accumulates(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    for index in range(6):
        identity = ops.build_delivery_identity(
            coaching_day_key=DAY, menu_id=f"menu-{index}", plan_id=17,
        )
        attempt = f"A{index}"
        await ops.claim_operation(
            db, 1, DAY, kind=ops.KIND_DELIVERY, identity=identity,
            attempt_id=attempt, now=T0,
        )
        await ops.complete_operation(
            db, 1, DAY, kind=ops.KIND_DELIVERY, identity=identity,
            attempt_id=attempt, now=T0, message_id=index, chat_id=1,
        )

    flags = await _read_flags(db)
    op_keys = [k for k in flags if k.startswith("daily_menu_")]
    assert sorted(op_keys) == sorted(
        [ops.DAILY_MENU_SEND_OP_KEY, ops.DAILY_MENU_LAST_COMPLETED_KEY]
    )
    for key in op_keys:
        assert isinstance(flags[key], dict)      # one record, never a list


@pytest.mark.asyncio
async def test_completed_delivery_is_archived_on_identity_rotation(
    tmp_path: Path,
) -> None:
    """Suppression evidence for the PREVIOUS menu survives a revision bump."""
    db = await _make_db(tmp_path)
    first = _delivery_identity(menu_id="menu-A")
    await ops.claim_operation(db, 1, DAY, kind=ops.KIND_DELIVERY,
                              identity=first, attempt_id="A", now=T0)
    await ops.complete_operation(db, 1, DAY, kind=ops.KIND_DELIVERY,
                                 identity=first, attempt_id="A", now=T0,
                                 message_id=11, chat_id=1)

    second = _delivery_identity(menu_id="menu-B")
    await ops.claim_operation(db, 1, DAY, kind=ops.KIND_DELIVERY,
                              identity=second, attempt_id="B", now=T0)

    flags = await _read_flags(db)
    archived = flags[ops.DAILY_MENU_LAST_COMPLETED_KEY]
    assert archived["identity"]["menu_id"] == "menu-A"
    assert archived["message_id"] == 11


@pytest.mark.asyncio
async def test_completed_operation_is_terminal(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    identity = _delivery_identity()
    await ops.claim_operation(db, 1, DAY, kind=ops.KIND_DELIVERY,
                              identity=identity, attempt_id="A", now=T0)
    await ops.complete_operation(db, 1, DAY, kind=ops.KIND_DELIVERY,
                                 identity=identity, attempt_id="A", now=T0,
                                 message_id=5, chat_id=1)

    again = await ops.claim_operation(
        db, 1, DAY, kind=ops.KIND_DELIVERY, identity=identity,
        attempt_id="B", now=T0 + timedelta(seconds=ops.MENU_SEND_LEASE_SECONDS + 1),
    )
    assert again.status == ops.ALREADY_COMPLETED
    assert (await _read_flags(db))[ops.DAILY_MENU_SEND_OP_KEY]["status"] == "completed"


@pytest.mark.asyncio
async def test_failed_operation_allows_immediate_retry(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    await _claim_refresh(db, attempt_id="A")
    assert (await ops.fail_operation(
        db, 1, DAY, kind=ops.KIND_REFRESH_REQUEST, identity=_refresh_identity(),
        attempt_id="A", category="telegram_network", error_code="ETIMEDOUT", now=T0,
    )).status == ops.ACQUIRED

    # No lease wait required: a failed record is immediately re-claimable.
    retry = await _claim_refresh(db, attempt_id="B", now=T0 + timedelta(seconds=1))
    assert retry.status == ops.ACQUIRED
    assert retry.record["attempt_id"] == "B"


@pytest.mark.asyncio
async def test_suppression_survives_process_restart(tmp_path: Path) -> None:
    """State is in SQLite, so a fresh Database object still suppresses."""
    db = await _make_db(tmp_path)
    await _claim_refresh(db, attempt_id="A")

    reopened = coach_bot.Database(str(tmp_path / "coach.db"))
    await reopened.init()
    second = await ops.claim_operation(
        reopened, 1, DAY, kind=ops.KIND_REFRESH_REQUEST,
        identity=_refresh_identity(), attempt_id="B",
        now=T0 + timedelta(seconds=30),
    )
    assert second.status == ops.IN_FLIGHT


@pytest.mark.asyncio
async def test_reserved_target_identity_survives_restart(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    await _claim_refresh(db, attempt_id="A", target="menu-RESERVED", revision=4)

    reopened = coach_bot.Database(str(tmp_path / "coach.db"))
    await reopened.init()
    record = await ops.read_operation(reopened, 1, DAY, kind=ops.KIND_REFRESH_REQUEST)
    assert record["target_menu_id"] == "menu-RESERVED"
    assert record["target_revision"] == 4


def test_lease_constants_are_named_and_renewal_is_more_frequent() -> None:
    assert ops.MENU_SEND_LEASE_SECONDS == 120
    assert ops.MENU_LEASE_RENEW_EVERY_SECONDS == 40
    # Two consecutive missed heartbeats must still precede expiry.
    assert ops.MENU_LEASE_RENEW_EVERY_SECONDS * 2 < ops.MENU_SEND_LEASE_SECONDS


@pytest.mark.asyncio
async def test_semantic_suppression_is_independent_of_lease_duration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A COMPLETED identity is suppressed regardless of the lease value."""
    for lease in (1, 3600):
        sub = tmp_path / f"lease{lease}"
        sub.mkdir()
        db = await _make_db(sub)
        monkeypatch.setattr(ops, "MENU_SEND_LEASE_SECONDS", lease)
        identity = _delivery_identity()
        await ops.claim_operation(db, 1, DAY, kind=ops.KIND_DELIVERY,
                                  identity=identity, attempt_id="A", now=T0)
        await ops.complete_operation(db, 1, DAY, kind=ops.KIND_DELIVERY,
                                     identity=identity, attempt_id="A", now=T0,
                                     message_id=1, chat_id=1)
        repeat = await ops.claim_operation(
            db, 1, DAY, kind=ops.KIND_DELIVERY, identity=identity,
            attempt_id="B", now=T0 + timedelta(seconds=10_000),
        )
        assert repeat.status == ops.ALREADY_COMPLETED
    monkeypatch.undo()
