"""G2.3B-3: durable refresh generation, fencing and recovery.

The AI pipeline is replaced by a counting fake, so "one generation" is an
assertion about a real number rather than a hope. Time is injected, concurrency
uses barriers, and failures are injected at explicit points. Nothing sleeps,
nothing reaches Telegram or OpenAI, and no DB is created outside tmp_path.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import coach_bot
from config import TZ
from noam_coach.services import daily_menu_operations as ops
from noam_coach.services import daily_menu_refresh as refresh
from noam_coach.services.daily_menu_state import ACTIVE_DAILY_MENU_KEY

T0 = datetime(2026, 7, 21, 9, 0, 0, tzinfo=TZ)


class _FakeGenerator:
    """Counts AI calls; optionally raises or blocks on a barrier."""

    def __init__(self, *, text: str = "fresh menu", raises: Exception | None = None,
                 gate: asyncio.Event | None = None) -> None:
        self.calls = 0
        self.text = text
        self.raises = raises
        self.gate = gate

    async def __call__(self) -> dict:
        self.calls += 1
        if self.gate is not None:
            await self.gate.wait()
        if self.raises is not None:
            raise self.raises
        return {"text": self.text, "meals": [], "strategy": "balanced"}


class _FakeDelivery:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, menu_id: str, text: str) -> None:
        self.calls.append((menu_id, text))


async def _make_db(tmp_path: Path) -> coach_bot.Database:
    db = coach_bot.Database(str(tmp_path / "coach.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (coach_bot.utc_now(),),
    )
    return db


async def _seed_menu(db, *, menu_id="menu-1-2026-07-21-1", revision=1, text="old menu"):
    from noam_coach.services.daily_flags_cas import patch_daily_flags
    from noam_coach.services.daily_menu_state import _nutrition_day

    day = await _nutrition_day(db, 1, T0)

    def _mutate(current):
        current[ACTIVE_DAILY_MENU_KEY] = {
            "text": text, "revision": revision, "menu_id": menu_id,
        }
        return current

    await patch_daily_flags(db, 1, day, _mutate, owner="test")
    return day


async def _read_flags(db, day):
    from noam_coach.services.daily_flags_cas import get_daily_flags_with_revision

    flags, _rev = await get_daily_flags_with_revision(db, 1, day)
    return flags


async def _refresh(db, gen, *, deliver=None, now=T0, requested_by="menu:refresh_daily_menu",
                   renew_every=0):
    return await refresh.refresh_daily_menu(
        db, 1, generate=gen, deliver=deliver, requested_by=requested_by,
        now=now, renew_every=renew_every,
    )


# --- 1-5: claim before generation; concurrency ------------------------------


@pytest.mark.asyncio
async def test_claim_occurs_before_generation(tmp_path: Path) -> None:
    """A losing claim must mean the generator was never invoked."""
    db = await _make_db(tmp_path)
    day = await _seed_menu(db)
    _d, identity, _m, _r = await refresh.resolve_refresh_identity(db, 1, now=T0)
    # Someone else already holds the claim.
    await ops.claim_operation(db, 1, day, kind=ops.KIND_REFRESH_REQUEST,
                              identity=identity, attempt_id="OTHER", now=T0)

    gen = _FakeGenerator()
    result = await _refresh(db, gen, now=T0 + timedelta(seconds=10))

    assert result.status == refresh.SUPPRESSED_IN_FLIGHT
    assert gen.calls == 0, "a losing caller must never reach the AI pipeline"


@pytest.mark.asyncio
async def test_two_concurrent_refreshes_produce_one_ai_call(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    await _seed_menu(db)
    gen = _FakeGenerator()
    gate = asyncio.Event()

    async def _attempt():
        await gate.wait()
        return await _refresh(db, gen)

    tasks = [asyncio.create_task(_attempt()) for _ in range(2)]
    gate.set()
    results = await asyncio.gather(*tasks)

    assert gen.calls == 1
    assert len([r for r in results if r.status == refresh.REFRESHED]) == 1
    assert len([r for r in results if r.suppressed]) == 1


@pytest.mark.asyncio
async def test_losing_caller_performs_no_generation(tmp_path: Path) -> None:
    """A caller that loses the claim must never reach the AI pipeline.

    Note this is about *losing a claim*, not about a later refresh. Per
    DECISION-R a second deliberate refresh IS allowed to regenerate: it acts
    on a new current menu, so it computes a new identity and legitimately
    wins its own claim. The losing case is a concurrent/duplicate tap against
    a live claim, exercised here by holding one open.
    """
    db = await _make_db(tmp_path)
    day = await _seed_menu(db)
    _d, identity, _m, _r = await refresh.resolve_refresh_identity(db, 1, now=T0)
    await ops.claim_operation(db, 1, day, kind=ops.KIND_REFRESH_REQUEST,
                              identity=identity, attempt_id="HOLDER", now=T0)

    gen = _FakeGenerator()
    second = await _refresh(db, gen, now=T0 + timedelta(seconds=1))

    assert second.suppressed
    assert gen.calls == 0


@pytest.mark.asyncio
async def test_second_deliberate_refresh_regenerates_per_decision_r(
    tmp_path: Path,
) -> None:
    """DECISION-R: an explicit refresh always mints a new revision and sends,
    because it acts on the menu the first refresh produced."""
    db = await _make_db(tmp_path)
    day = await _seed_menu(db, revision=1)
    gen = _FakeGenerator()

    first = await _refresh(db, gen)
    second = await _refresh(db, gen, now=T0 + timedelta(seconds=30))

    assert first.status == refresh.REFRESHED
    assert second.status == refresh.REFRESHED
    assert second.menu_id != first.menu_id
    assert gen.calls == 2
    assert (await _read_flags(db, day))[ACTIVE_DAILY_MENU_KEY]["revision"] == 3


@pytest.mark.asyncio
async def test_concurrent_refresh_creates_one_revision(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    day = await _seed_menu(db, revision=1)
    gen = _FakeGenerator()
    gate = asyncio.Event()

    async def _attempt():
        await gate.wait()
        return await _refresh(db, gen)

    tasks = [asyncio.create_task(_attempt()) for _ in range(2)]
    gate.set()
    await asyncio.gather(*tasks)

    flags = await _read_flags(db, day)
    assert flags[ACTIVE_DAILY_MENU_KEY]["revision"] == 2   # 1 -> 2, exactly once


@pytest.mark.asyncio
async def test_concurrent_refresh_creates_one_delivery_attempt(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    await _seed_menu(db)
    gen = _FakeGenerator()
    deliver = _FakeDelivery()
    gate = asyncio.Event()

    async def _attempt():
        await gate.wait()
        return await _refresh(db, gen, deliver=deliver)

    tasks = [asyncio.create_task(_attempt()) for _ in range(2)]
    gate.set()
    await asyncio.gather(*tasks)

    assert len(deliver.calls) == 1


# --- 6-10: identity and reservation -----------------------------------------


@pytest.mark.asyncio
async def test_requested_by_does_not_change_refresh_identity(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    _d, a, _m, _r = await refresh.resolve_refresh_identity(db, 1, now=T0)
    _d2, b, _m2, _r2 = await refresh.resolve_refresh_identity(db, 1, now=T0)
    assert ops.identities_match(a, b)
    assert "requested_by" not in a


@pytest.mark.asyncio
async def test_target_identity_is_reserved_before_generation(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    day = await _seed_menu(db, revision=3)
    gate = asyncio.Event()
    gen = _FakeGenerator(gate=gate)

    task = asyncio.create_task(_refresh(db, gen))
    await asyncio.sleep(0)          # let the claim land, generation is blocked
    while gen.calls == 0:
        await asyncio.sleep(0)

    record = (await _read_flags(db, day))[ops.DAILY_MENU_REFRESH_OP_KEY]
    assert record["target_menu_id"] == "menu-1-2026-07-21-4"
    assert record["target_revision"] == 4
    assert record["produced_menu_id"] is None   # not yet generated

    gate.set()
    await task


@pytest.mark.asyncio
async def test_takeover_retains_target_menu_id_and_revision(tmp_path: Path) -> None:
    db = await _make_db(tmp_path, )
    day = await _seed_menu(db, revision=1)
    _d, identity, _m, _r = await refresh.resolve_refresh_identity(db, 1, now=T0)
    await ops.claim_operation(
        db, 1, day, kind=ops.KIND_REFRESH_REQUEST, identity=identity,
        attempt_id="OLD", now=T0, target_menu_id="menu-RESERVED",
        target_revision=2,
        source_identity={"current_menu_id": "menu-1-2026-07-21-1", "current_revision": 1},
    )

    later = T0 + timedelta(seconds=ops.MENU_SEND_LEASE_SECONDS + 1)
    gen = _FakeGenerator()
    await _refresh(db, gen, now=later)

    record = (await _read_flags(db, day))[ops.DAILY_MENU_REFRESH_OP_KEY]
    assert record["target_menu_id"] == "menu-RESERVED"
    assert record["target_revision"] == 2


# --- 11-14: heartbeat and fencing -------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_renews_only_for_owning_attempt(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    day = await _seed_menu(db)
    _d, identity, _m, _r = await refresh.resolve_refresh_identity(db, 1, now=T0)
    await ops.claim_operation(db, 1, day, kind=ops.KIND_REFRESH_REQUEST,
                              identity=identity, attempt_id="OWNER", now=T0)

    ok = await ops.renew_operation(db, 1, day, kind=ops.KIND_REFRESH_REQUEST,
                                   identity=identity, attempt_id="OWNER",
                                   now=T0 + timedelta(seconds=40))
    bad = await ops.renew_operation(db, 1, day, kind=ops.KIND_REFRESH_REQUEST,
                                    identity=identity, attempt_id="INTRUDER",
                                    now=T0 + timedelta(seconds=41))
    assert ok.status == ops.ACQUIRED
    assert bad.status == ops.OWNERSHIP_LOST


@pytest.mark.asyncio
async def test_lease_loss_during_generation_fences_persistence(tmp_path: Path) -> None:
    """A stale AI result must not persist, deliver or change the menu."""
    db = await _make_db(tmp_path)
    day = await _seed_menu(db, menu_id="menu-1-2026-07-21-1", revision=1)
    gate = asyncio.Event()
    gen = _FakeGenerator(gate=gate)
    deliver = _FakeDelivery()

    task = asyncio.create_task(
        _refresh(db, gen, deliver=deliver, renew_every=0.001)
    )
    while gen.calls == 0:
        await asyncio.sleep(0)

    # A newer owner takes the claim while generation is in flight.
    _d, identity, _m, _r = await refresh.resolve_refresh_identity(db, 1, now=T0)
    later = T0 + timedelta(seconds=ops.MENU_SEND_LEASE_SECONDS + 1)
    await ops.claim_operation(db, 1, day, kind=ops.KIND_REFRESH_REQUEST,
                              identity=identity, attempt_id="NEWER", now=later)
    await asyncio.sleep(0.02)      # let the heartbeat observe the loss
    gate.set()
    result = await task

    assert result.suppressed
    assert result.generated is True                 # the AI call did happen
    flags = await _read_flags(db, day)
    assert flags[ACTIVE_DAILY_MENU_KEY]["menu_id"] == "menu-1-2026-07-21-1"
    assert flags[ACTIVE_DAILY_MENU_KEY]["revision"] == 1
    assert flags[ops.DAILY_MENU_REFRESH_OP_KEY]["attempt_id"] == "NEWER"
    assert deliver.calls == []


@pytest.mark.asyncio
async def test_stale_ai_result_cannot_persist_via_primitive(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    day = await _seed_menu(db, revision=1)
    _d, identity, _m, _r = await refresh.resolve_refresh_identity(db, 1, now=T0)
    await ops.claim_operation(
        db, 1, day, kind=ops.KIND_REFRESH_REQUEST, identity=identity,
        attempt_id="OLD", now=T0, target_menu_id="menu-1-2026-07-21-2",
        target_revision=2,
        source_identity={"current_menu_id": "menu-1-2026-07-21-1", "current_revision": 1},
    )
    later = T0 + timedelta(seconds=ops.MENU_SEND_LEASE_SECONDS + 1)
    await ops.claim_operation(db, 1, day, kind=ops.KIND_REFRESH_REQUEST,
                              identity=identity, attempt_id="NEW", now=later)

    stale = await ops.persist_refresh_result(
        db, 1, day, identity=identity, attempt_id="OLD",
        text="stale result", now=later,
    )
    assert stale.status == ops.OWNERSHIP_LOST
    flags = await _read_flags(db, day)
    assert flags[ACTIVE_DAILY_MENU_KEY]["revision"] == 1
    assert flags[ops.DAILY_MENU_REFRESH_OP_KEY]["produced_menu_id"] is None


# --- 15-18: source fencing and crash-before-persistence ---------------------


@pytest.mark.asyncio
async def test_source_menu_change_during_generation_refuses(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    day = await _seed_menu(db, menu_id="menu-1-2026-07-21-1", revision=1)
    gate = asyncio.Event()
    gen = _FakeGenerator(gate=gate)

    task = asyncio.create_task(_refresh(db, gen))
    while gen.calls == 0:
        await asyncio.sleep(0)

    await _seed_menu(db, menu_id="menu-OTHER", revision=9, text="newer")
    gate.set()
    result = await task

    assert result.status == refresh.REFUSED_STALE_SOURCE
    flags = await _read_flags(db, day)
    assert flags[ACTIVE_DAILY_MENU_KEY]["menu_id"] == "menu-OTHER"
    assert flags[ACTIVE_DAILY_MENU_KEY]["text"] == "newer"


@pytest.mark.asyncio
async def test_crash_before_persistence_leaves_no_orphan_revision(
    tmp_path: Path,
) -> None:
    db = await _make_db(tmp_path)
    day = await _seed_menu(db, revision=1)
    gen = _FakeGenerator(raises=RuntimeError("pipeline exploded"))

    result = await _refresh(db, gen)

    assert result.status == refresh.GENERATION_FAILED
    flags = await _read_flags(db, day)
    assert flags[ACTIVE_DAILY_MENU_KEY]["revision"] == 1      # unchanged
    assert flags[ops.DAILY_MENU_REFRESH_OP_KEY]["status"] == "failed"
    assert flags[ops.DAILY_MENU_REFRESH_OP_KEY]["produced_menu_id"] is None


@pytest.mark.asyncio
async def test_retry_after_generation_failure_reuses_same_target_identity(
    tmp_path: Path,
) -> None:
    """A second AI call is permitted here, but the identity must not change."""
    db = await _make_db(tmp_path)
    day = await _seed_menu(db, revision=1)
    failing = _FakeGenerator(raises=RuntimeError("boom"))
    await _refresh(db, failing)
    first_target = (await _read_flags(db, day))[ops.DAILY_MENU_REFRESH_OP_KEY]["target_menu_id"]

    healthy = _FakeGenerator()
    result = await _refresh(db, healthy, now=T0 + timedelta(seconds=1))

    assert result.status == refresh.REFRESHED
    assert healthy.calls == 1
    assert result.menu_id == first_target


# --- 19-23: atomic persistence and resume -----------------------------------


@pytest.mark.asyncio
async def test_successful_persistence_records_menu_and_identity_together(
    tmp_path: Path,
) -> None:
    db = await _make_db(tmp_path)
    day = await _seed_menu(db, revision=1)
    gen = _FakeGenerator(text="brand new menu")

    result = await _refresh(db, gen)

    flags = await _read_flags(db, day)
    assert result.status == refresh.REFRESHED
    assert flags[ACTIVE_DAILY_MENU_KEY]["menu_id"] == result.menu_id
    assert flags[ACTIVE_DAILY_MENU_KEY]["revision"] == 2
    assert flags[ACTIVE_DAILY_MENU_KEY]["text"] == "brand new menu"
    assert flags[ops.DAILY_MENU_REFRESH_OP_KEY]["produced_menu_id"] == result.menu_id


@pytest.mark.asyncio
async def test_takeover_after_persistence_resumes_without_second_ai_call(
    tmp_path: Path,
) -> None:
    db = await _make_db(tmp_path)
    day = await _seed_menu(db, revision=1)
    gen = _FakeGenerator()
    first = await _refresh(db, gen)
    assert gen.calls == 1

    # Reopen the claim as if the process had died before completing.
    from noam_coach.services.daily_flags_cas import patch_daily_flags

    def _reopen(current):
        rec = dict(current[ops.DAILY_MENU_REFRESH_OP_KEY])
        rec["status"] = ops.IN_FLIGHT
        current[ops.DAILY_MENU_REFRESH_OP_KEY] = rec
        return current

    await patch_daily_flags(db, 1, day, _reopen, owner="test")

    later = T0 + timedelta(seconds=ops.MENU_SEND_LEASE_SECONDS + 1)
    deliver = _FakeDelivery()
    resumed = await _refresh(db, gen, deliver=deliver, now=later)

    assert resumed.status == refresh.RESUMED
    assert resumed.menu_id == first.menu_id
    assert gen.calls == 1, "resume must not regenerate"
    assert deliver.calls and deliver.calls[0][0] == first.menu_id


@pytest.mark.asyncio
async def test_resume_creates_no_second_revision(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    day = await _seed_menu(db, revision=1)
    gen = _FakeGenerator()
    await _refresh(db, gen)

    from noam_coach.services.daily_flags_cas import patch_daily_flags

    def _reopen(current):
        rec = dict(current[ops.DAILY_MENU_REFRESH_OP_KEY])
        rec["status"] = ops.IN_FLIGHT
        current[ops.DAILY_MENU_REFRESH_OP_KEY] = rec
        return current

    await patch_daily_flags(db, 1, day, _reopen, owner="test")
    later = T0 + timedelta(seconds=ops.MENU_SEND_LEASE_SECONDS + 1)
    await _refresh(db, gen, now=later)

    flags = await _read_flags(db, day)
    assert flags[ACTIVE_DAILY_MENU_KEY]["revision"] == 2      # still one bump
    assert gen.calls == 1


@pytest.mark.asyncio
async def test_produced_identity_survives_service_reconstruction(
    tmp_path: Path,
) -> None:
    db = await _make_db(tmp_path)
    await _seed_menu(db, revision=1)
    gen = _FakeGenerator()
    first = await _refresh(db, gen)

    reopened = coach_bot.Database(str(tmp_path / "coach.db"))
    await reopened.init()
    record = await ops.read_operation(reopened, 1,
                                      (await refresh.resolve_refresh_identity(
                                          reopened, 1, now=T0))[0],
                                      kind=ops.KIND_REFRESH_REQUEST)
    assert record["produced_menu_id"] == first.menu_id


@pytest.mark.asyncio
async def test_active_target_menu_recovery_does_not_regenerate(
    tmp_path: Path,
) -> None:
    """Defensive D6 state: the reserved menu is already active but the record
    shows no produced_menu_id. Recovery must reuse it, never regenerate."""
    db = await _make_db(tmp_path)
    day = await _seed_menu(db, menu_id="menu-1-2026-07-21-2", revision=2)
    _d, identity, _m, _r = await refresh.resolve_refresh_identity(db, 1, now=T0)
    await ops.claim_operation(
        db, 1, day, kind=ops.KIND_REFRESH_REQUEST, identity=identity,
        attempt_id="OLD", now=T0,
        target_menu_id="menu-1-2026-07-21-2", target_revision=2,
        source_identity={"current_menu_id": "menu-1-2026-07-21-2", "current_revision": 2},
    )

    later = T0 + timedelta(seconds=ops.MENU_SEND_LEASE_SECONDS + 1)
    gen = _FakeGenerator()
    result = await _refresh(db, gen, now=later)

    assert result.status == refresh.RESUMED
    assert gen.calls == 0, "the reserved menu is already persisted"


# --- 24-27: persistence failure and conditional failure ---------------------


@pytest.mark.asyncio
async def test_injected_persistence_failure_leaves_neither_side(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = await _make_db(tmp_path)
    day = await _seed_menu(db, revision=1)

    async def _refuse(*_a, **_k):
        return ops.OperationOutcome(status=ops.CONFLICT, reason="injected")

    monkeypatch.setattr(ops, "persist_refresh_result", _refuse)
    gen = _FakeGenerator()
    result = await _refresh(db, gen)
    monkeypatch.undo()

    assert result.status == refresh.REFUSED_CONFLICT
    flags = await _read_flags(db, day)
    assert flags[ACTIVE_DAILY_MENU_KEY]["revision"] == 1
    assert flags[ops.DAILY_MENU_REFRESH_OP_KEY]["produced_menu_id"] is None


@pytest.mark.asyncio
async def test_generation_exception_records_conditional_failure(
    tmp_path: Path,
) -> None:
    db = await _make_db(tmp_path)
    day = await _seed_menu(db)
    gen = _FakeGenerator(raises=ValueError("bad pipeline"))
    result = await _refresh(db, gen)

    assert result.status == refresh.GENERATION_FAILED
    record = (await _read_flags(db, day))[ops.DAILY_MENU_REFRESH_OP_KEY]
    assert record["status"] == "failed"
    assert record["failure"]["category"] == refresh.GENERATION_ERROR
    assert record["failure"]["error_code"] == "ValueError"


@pytest.mark.asyncio
async def test_stale_owner_cannot_fail_the_newer_attempt(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    day = await _seed_menu(db)
    _d, identity, _m, _r = await refresh.resolve_refresh_identity(db, 1, now=T0)
    await ops.claim_operation(db, 1, day, kind=ops.KIND_REFRESH_REQUEST,
                              identity=identity, attempt_id="OLD", now=T0)
    later = T0 + timedelta(seconds=ops.MENU_SEND_LEASE_SECONDS + 1)
    await ops.claim_operation(db, 1, day, kind=ops.KIND_REFRESH_REQUEST,
                              identity=identity, attempt_id="NEW", now=later)

    refused = await ops.fail_operation(
        db, 1, day, kind=ops.KIND_REFRESH_REQUEST, identity=identity,
        attempt_id="OLD", category="generation_failed", now=later,
    )
    assert refused.status == ops.OWNERSHIP_LOST
    assert (await _read_flags(db, day))[ops.DAILY_MENU_REFRESH_OP_KEY]["status"] == ops.IN_FLIGHT


# --- 28-31: delivery integration and isolation ------------------------------


@pytest.mark.asyncio
async def test_refresh_result_routes_through_delivery_helper(
    tmp_path: Path,
) -> None:
    db = await _make_db(tmp_path)
    await _seed_menu(db)
    gen = _FakeGenerator(text="delivered menu")
    deliver = _FakeDelivery()

    result = await _refresh(db, gen, deliver=deliver)

    assert result.delivered is True
    assert deliver.calls == [(result.menu_id, "delivered menu")]


@pytest.mark.asyncio
async def test_refresh_then_display_sends_once_total(tmp_path: Path) -> None:
    """The refresh persists a new menu; a following ordinary display of that
    same menu must not produce a second standalone delivery."""
    from noam_coach.services import daily_menu_delivery as delivery

    class _Sent:
        def __init__(self, mid: int) -> None:
            self.message_id = mid
            self.chat = type("C", (), {"id": 1})()

    sends = {"n": 0}

    async def _send():
        sends["n"] += 1
        return _Sent(100 + sends["n"])

    db = await _make_db(tmp_path)
    await _seed_menu(db)
    gen = _FakeGenerator()

    async def _deliver(menu_id: str, text: str) -> None:
        await delivery.deliver_standalone_menu(
            db, 1, send=_send, requested_by="menu:refresh_daily_menu",
            source="refresh", now=T0,
        )

    await _refresh(db, gen, deliver=_deliver)
    # Ordinary display of the SAME persisted menu.
    await delivery.deliver_standalone_menu(
        db, 1, send=_send, requested_by="menu:daily_menu",
        source="menu_callback", now=T0 + timedelta(seconds=5),
    )

    assert sends["n"] == 1, "refresh + display of one menu is one delivery"


@pytest.mark.asyncio
async def test_unrelated_daily_flags_fields_survive_refresh(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    day = await _seed_menu(db)
    from noam_coach.services.daily_flags_cas import patch_daily_flags

    def _other(current):
        current["sleep_quality"] = "good"
        current["energy"] = "high"
        return current

    await patch_daily_flags(db, 1, day, _other, owner="checkins")
    await _refresh(db, _FakeGenerator(), deliver=_FakeDelivery())

    flags = await _read_flags(db, day)
    assert flags["sleep_quality"] == "good"
    assert flags["energy"] == "high"


@pytest.mark.asyncio
async def test_no_unbounded_operation_history(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    day = await _seed_menu(db, revision=1)
    for index in range(4):
        gen = _FakeGenerator()
        await _refresh(db, gen, now=T0 + timedelta(seconds=index * 10))

    flags = await _read_flags(db, day)
    op_keys = [k for k in flags if k.startswith("daily_menu_")]
    assert len(op_keys) <= 3
    for key in op_keys:
        assert isinstance(flags[key], dict)


# --- 32-35: hygiene and untouched behaviour ---------------------------------


@pytest.mark.asyncio
async def test_display_without_refresh_performs_no_ai_call(tmp_path: Path) -> None:
    """An ordinary display reuses the persisted menu; no generation happens."""
    from noam_coach.services.daily_menu_state import get_active_daily_menu

    db = await _make_db(tmp_path)
    await _seed_menu(db, text="already here")
    gen = _FakeGenerator()

    active = await get_active_daily_menu(db, 1, now=T0)
    assert active["text"] == "already here"
    assert gen.calls == 0


def test_meal_text_direct_edit_remains_outside_standalone_delivery() -> None:
    """meal_text's menu-edit reply must NOT be wrapped in delivery idempotency."""
    import inspect

    from noam_coach.bot import meal_text

    source = inspect.getsource(meal_text)
    assert "deliver_standalone_menu" not in source
    assert "remember_daily_menu_message" in source   # still records the edit target


def test_refresh_uses_no_wall_clock_or_process_local_cache() -> None:
    import inspect

    source = inspect.getsource(refresh)
    for banned in ("time.monotonic", "time.time(", "_LAST_", "DEBOUNCE", "saved_at"):
        assert banned not in source
    module_dicts = [
        name for name, value in vars(refresh).items()
        if isinstance(value, (dict, set)) and not name.startswith("__")
        and name not in {"REFRESH_TOAST"}
    ]
    assert module_dicts == []


def test_no_database_file_is_created_in_the_repository_root() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    strays = [
        p.name for p in repo_root.iterdir()
        if p.suffix in {".db", ".sqlite", ".sqlite3"}
        or p.name.endswith("-wal") or p.name.endswith("-shm")
    ]
    assert strays == [], f"stray database artifacts: {strays}"
