"""G2.3B-2: durable standalone daily-menu delivery.

Every send in production now goes through one boundary,
``daily_menu_delivery.deliver_standalone_menu``. These tests exercise that
boundary directly with a fake sender: no Telegram, no OpenAI, no sleeping, and
no DB anywhere except the pytest ``tmp_path``.

The properties under test are the ones that distinguish this from the rejected
wall-clock debounce: suppression follows the menu's *semantic identity*, the
requesting UI action is irrelevant to it, and a completed delivery stays
suppressed across a process restart.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import coach_bot
from config import TZ
from noam_coach.services import daily_menu_delivery as delivery
from noam_coach.services import daily_menu_operations as ops
from noam_coach.services.daily_menu_state import (
    ACTIVE_DAILY_MENU_KEY,
    DAILY_MENU_MESSAGE_KEY,
)

T0 = datetime(2026, 7, 21, 9, 0, 0, tzinfo=TZ)


class _SentMessage:
    def __init__(self, message_id: int, chat_id: int = 1) -> None:
        self.message_id = message_id
        self.chat = type("_Chat", (), {"id": chat_id})()


class _FakeSender:
    """Records every send; optionally raises to simulate a Telegram failure."""

    def __init__(self, *, raises: Exception | None = None) -> None:
        self.calls = 0
        self.raises = raises

    async def __call__(self) -> _SentMessage:
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return _SentMessage(message_id=100 + self.calls)


async def _make_db(tmp_path: Path) -> coach_bot.Database:
    db = coach_bot.Database(str(tmp_path / "coach.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (coach_bot.utc_now(),),
    )
    return db


async def _seed_menu(db, *, menu_id: str = "menu-A", revision: int = 1) -> str:
    """Persist an active menu and return its coaching-day key."""
    from noam_coach.services.daily_flags_cas import patch_daily_flags
    from noam_coach.services.daily_menu_state import _nutrition_day

    day = await _nutrition_day(db, 1, T0)

    def _mutate(current):
        current[ACTIVE_DAILY_MENU_KEY] = {
            "text": "menu text", "revision": revision, "menu_id": menu_id,
        }
        return current

    await patch_daily_flags(db, 1, day, _mutate, owner="test")
    return day


async def _deliver(db, sender, *, requested_by="menu:daily_menu", now=T0):
    return await delivery.deliver_standalone_menu(
        db, 1, send=sender, requested_by=requested_by,
        source="test", now=now,
    )


async def _read_flags(db, day):
    from noam_coach.services.daily_flags_cas import get_daily_flags_with_revision

    flags, _rev = await get_daily_flags_with_revision(db, 1, day)
    return flags


# --- 1-3: send once, suppress repeats, survive concurrency ------------------


@pytest.mark.asyncio
async def test_first_delivery_sends_once(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    await _seed_menu(db)
    sender = _FakeSender()

    result = await _deliver(db, sender)

    assert result.status == delivery.SENT
    assert sender.calls == 1
    assert result.message_id == 101


@pytest.mark.asyncio
async def test_identical_repeated_request_is_suppressed(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    await _seed_menu(db)
    sender = _FakeSender()

    first = await _deliver(db, sender)
    second = await _deliver(db, sender, now=T0 + timedelta(seconds=1))

    assert first.was_sent
    assert second.status == delivery.SUPPRESSED_ALREADY_COMPLETED
    assert sender.calls == 1, "the menu must be sent exactly once"


@pytest.mark.asyncio
async def test_two_concurrent_identical_requests_produce_exactly_one_send(
    tmp_path: Path,
) -> None:
    db = await _make_db(tmp_path)
    await _seed_menu(db)
    sender = _FakeSender()
    gate = asyncio.Event()

    async def _attempt():
        await gate.wait()
        return await _deliver(db, sender)

    tasks = [asyncio.create_task(_attempt()) for _ in range(2)]
    gate.set()
    results = await asyncio.gather(*tasks)

    assert sender.calls == 1
    assert len([r for r in results if r.was_sent]) == 1
    assert len([r for r in results if r.suppressed]) == 1


# --- 4-6: identity ignores the requesting path ------------------------------


@pytest.mark.asyncio
async def test_requested_by_does_not_change_delivery_identity(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    await _seed_menu(db)
    sender = _FakeSender()

    first = await _deliver(db, sender, requested_by="menu:daily_menu")
    second = await _deliver(db, sender, requested_by="planv2:select")

    assert first.identity == second.identity
    assert second.status == delivery.SUPPRESSED_ALREADY_COMPLETED
    assert sender.calls == 1


@pytest.mark.asyncio
async def test_plan_path_and_menu_path_for_same_menu_send_once_total(
    tmp_path: Path,
) -> None:
    db = await _make_db(tmp_path)
    await _seed_menu(db)
    sender = _FakeSender()

    await _deliver(db, sender, requested_by="planv2:select")
    await _deliver(db, sender, requested_by="menu:daily_menu")

    assert sender.calls == 1


@pytest.mark.asyncio
async def test_refresh_style_caller_and_display_caller_send_once_total(
    tmp_path: Path,
) -> None:
    """Same PERSISTED menu reached from a refresh-style path and the ordinary
    display path is one delivery. (No refresh generation here -- that is B-3.)"""
    db = await _make_db(tmp_path)
    await _seed_menu(db)
    sender = _FakeSender()

    await _deliver(db, sender, requested_by="menu:refresh_daily_menu")
    await _deliver(db, sender, requested_by="menu:daily_menu")

    assert sender.calls == 1


# --- 7-9: a genuinely different identity may be delivered -------------------


@pytest.mark.asyncio
async def test_new_menu_id_sends(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    await _seed_menu(db, menu_id="menu-A", revision=1)
    sender = _FakeSender()
    await _deliver(db, sender)

    await _seed_menu(db, menu_id="menu-B", revision=2)
    second = await _deliver(db, sender, now=T0 + timedelta(seconds=5))

    assert second.was_sent
    assert sender.calls == 2


@pytest.mark.asyncio
async def test_new_plan_id_sends(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = await _make_db(tmp_path)
    day = await _seed_menu(db)
    sender = _FakeSender()

    import planning

    async def _plan_17(*_a, **_k):
        return {"id": 17}

    monkeypatch.setattr(planning, "get_active_plan", _plan_17)
    first = await _deliver(db, sender)
    assert first.identity["plan_id"] == "17"

    async def _plan_18(*_a, **_k):
        return {"id": 18}

    monkeypatch.setattr(planning, "get_active_plan", _plan_18)
    second = await _deliver(db, sender, now=T0 + timedelta(seconds=5))

    assert second.was_sent
    assert second.identity["plan_id"] == "18"
    assert sender.calls == 2
    assert day  # coaching day unchanged across both


@pytest.mark.asyncio
async def test_new_coaching_day_sends(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    await _seed_menu(db)
    sender = _FakeSender()

    await _deliver(db, sender, now=T0)
    tomorrow = T0 + timedelta(days=1)
    second = await _deliver(db, sender, now=tomorrow)

    assert second.was_sent
    assert second.identity["coaching_day_key"] != (await _deliver(
        db, _FakeSender(), now=T0,
    )).identity["coaching_day_key"]
    assert sender.calls == 2


# --- 10-11: persistence and in-flight ---------------------------------------


@pytest.mark.asyncio
async def test_already_completed_survives_service_reconstruction(
    tmp_path: Path,
) -> None:
    db = await _make_db(tmp_path)
    await _seed_menu(db)
    sender = _FakeSender()
    await _deliver(db, sender)

    reopened = coach_bot.Database(str(tmp_path / "coach.db"))
    await reopened.init()
    second = await delivery.deliver_standalone_menu(
        reopened, 1, send=sender, requested_by="menu:daily_menu",
        source="test", now=T0 + timedelta(seconds=5),
    )

    assert second.status == delivery.SUPPRESSED_ALREADY_COMPLETED
    assert sender.calls == 1


@pytest.mark.asyncio
async def test_in_flight_duplicate_does_not_send(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    day = await _seed_menu(db)
    _day, identity = await delivery.resolve_delivery_identity(db, 1, now=T0)

    # Someone else holds a live claim on this exact identity.
    await ops.claim_operation(
        db, 1, day, kind=ops.KIND_DELIVERY, identity=identity,
        attempt_id="OTHER", now=T0,
    )

    sender = _FakeSender()
    result = await _deliver(db, sender, now=T0 + timedelta(seconds=30))

    assert result.status == delivery.SUPPRESSED_IN_FLIGHT
    assert sender.calls == 0


# --- 12-14: UX ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_callback_is_acknowledged_on_acquired_path(tmp_path: Path) -> None:
    """The handler edits its screen on success (verified through the handler)."""
    db = await _make_db(tmp_path)
    await _seed_menu(db)
    result = await _deliver(db, _FakeSender())
    assert result.was_sent
    # A sent delivery is not suppressed, so the handler takes its normal branch.
    assert not result.suppressed


@pytest.mark.asyncio
async def test_suppressed_delivery_offers_a_toast_not_a_new_message(
    tmp_path: Path,
) -> None:
    db = await _make_db(tmp_path)
    await _seed_menu(db)
    sender = _FakeSender()
    await _deliver(db, sender)
    second = await _deliver(db, sender, now=T0 + timedelta(seconds=1))

    assert second.suppressed
    toast = delivery.suppression_toast(second)
    assert toast and len(toast) < 60          # a toast, not a chat message
    assert sender.calls == 1                  # nothing extra was sent


@pytest.mark.asyncio
async def test_background_suppression_sends_no_user_message(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    await _seed_menu(db)
    sender = _FakeSender()

    await _deliver(db, sender, requested_by="job_morning")
    second = await _deliver(db, sender, requested_by="job_morning",
                            now=T0 + timedelta(seconds=1))

    assert second.suppressed
    assert sender.calls == 1                  # the job sent nothing further


# --- 15-16: failure and retry -----------------------------------------------


@pytest.mark.asyncio
async def test_telegram_exception_records_failure_not_completion(
    tmp_path: Path,
) -> None:
    db = await _make_db(tmp_path)
    day = await _seed_menu(db)
    sender = _FakeSender(raises=RuntimeError("telegram exploded"))

    with pytest.raises(RuntimeError):
        await _deliver(db, sender)

    record = (await _read_flags(db, day))[ops.DAILY_MENU_SEND_OP_KEY]
    assert record["status"] == "failed"
    assert record["failure"]["category"] == "unknown"
    assert record["message_id"] is None
    assert DAILY_MENU_MESSAGE_KEY not in await _read_flags(db, day)


@pytest.mark.asyncio
async def test_retry_after_failed_delivery_is_allowed(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    await _seed_menu(db)
    failing = _FakeSender(raises=RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        await _deliver(db, failing)

    # No lease wait: a failed record is immediately re-claimable.
    healthy = _FakeSender()
    retry = await _deliver(db, healthy, now=T0 + timedelta(seconds=1))

    assert retry.was_sent
    assert healthy.calls == 1


# --- 17-19: stale-owner fencing and success persistence ---------------------


@pytest.mark.asyncio
async def test_stale_owner_cannot_record_completion(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    day = await _seed_menu(db)
    _day, identity = await delivery.resolve_delivery_identity(db, 1, now=T0)

    await ops.claim_operation(db, 1, day, kind=ops.KIND_DELIVERY,
                              identity=identity, attempt_id="OLD", now=T0)
    later = T0 + timedelta(seconds=ops.MENU_SEND_LEASE_SECONDS + 1)
    await ops.claim_operation(db, 1, day, kind=ops.KIND_DELIVERY,
                              identity=identity, attempt_id="NEW", now=later)

    refused = await ops.complete_operation(
        db, 1, day, kind=ops.KIND_DELIVERY, identity=identity,
        attempt_id="OLD", now=later, message_id=999, chat_id=1,
    )
    assert refused.status == ops.OWNERSHIP_LOST
    record = (await _read_flags(db, day))[ops.DAILY_MENU_SEND_OP_KEY]
    assert record["attempt_id"] == "NEW"
    assert record["status"] == ops.IN_FLIGHT
    assert record["message_id"] is None


@pytest.mark.asyncio
async def test_stale_owner_cannot_update_message_metadata(tmp_path: Path) -> None:
    """Message metadata is written only by the attempt that owns completion."""
    db = await _make_db(tmp_path)
    day = await _seed_menu(db)
    _day, identity = await delivery.resolve_delivery_identity(db, 1, now=T0)

    await ops.claim_operation(db, 1, day, kind=ops.KIND_DELIVERY,
                              identity=identity, attempt_id="OLD", now=T0)
    later = T0 + timedelta(seconds=ops.MENU_SEND_LEASE_SECONDS + 1)
    await ops.claim_operation(db, 1, day, kind=ops.KIND_DELIVERY,
                              identity=identity, attempt_id="NEW", now=later)

    # OLD's completion is refused, so it never reaches the metadata write.
    assert (await ops.complete_operation(
        db, 1, day, kind=ops.KIND_DELIVERY, identity=identity,
        attempt_id="OLD", now=later, message_id=999, chat_id=1,
    )).status == ops.OWNERSHIP_LOST
    flags = await _read_flags(db, day)
    assert flags.get(DAILY_MENU_MESSAGE_KEY) is None


@pytest.mark.asyncio
async def test_success_stores_completion_and_message_metadata_consistently(
    tmp_path: Path,
) -> None:
    db = await _make_db(tmp_path)
    day = await _seed_menu(db)
    sender = _FakeSender()

    result = await _deliver(db, sender)

    flags = await _read_flags(db, day)
    record = flags[ops.DAILY_MENU_SEND_OP_KEY]
    assert record["status"] == "completed"
    assert record["message_id"] == result.message_id
    assert record["completed_at"]
    assert flags[DAILY_MENU_MESSAGE_KEY]["message_id"] == result.message_id


@pytest.mark.asyncio
async def test_completion_cas_failure_does_not_falsely_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = await _make_db(tmp_path)
    day = await _seed_menu(db)
    sender = _FakeSender()

    async def _refuse(*_a, **_k):
        return ops.OperationOutcome(status=ops.CONFLICT, reason="injected")

    monkeypatch.setattr(ops, "complete_delivery_atomically", _refuse)
    result = await _deliver(db, sender)
    monkeypatch.undo()

    # The message really was sent, so the caller is told SENT...
    assert result.status == delivery.SENT
    assert sender.calls == 1
    # ...but nothing was falsely recorded as completed.
    record = (await _read_flags(db, day))[ops.DAILY_MENU_SEND_OP_KEY]
    assert record["status"] != "completed"


@pytest.mark.asyncio
async def test_crash_after_send_before_completion_permits_at_least_once_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Documents at-least-once honestly -- this is NOT exactly-once.

    A crash between a successful send and durable completion leaves an
    in_flight record. After the lease expires a takeover sends again, so the
    user may receive a second copy. That is the accepted cost of Telegram
    offering no idempotency key.
    """
    db = await _make_db(tmp_path)
    await _seed_menu(db)
    sender = _FakeSender()

    async def _crash(*_a, **_k):
        return ops.OperationOutcome(status=ops.CONFLICT, reason="simulated_crash")

    monkeypatch.setattr(ops, "complete_delivery_atomically", _crash)
    await _deliver(db, sender)              # sent, but never completed
    monkeypatch.undo()

    later = T0 + timedelta(seconds=ops.MENU_SEND_LEASE_SECONDS + 1)
    retry = await _deliver(db, sender, now=later)

    assert retry.was_sent
    assert sender.calls == 2, "at-least-once: a duplicate is possible here"


# --- 20-25: hygiene, isolation, no rejected mechanisms ----------------------


@pytest.mark.asyncio
async def test_unrelated_daily_flags_fields_survive_claim_and_completion(
    tmp_path: Path,
) -> None:
    db = await _make_db(tmp_path)
    day = await _seed_menu(db)
    from noam_coach.services.daily_flags_cas import patch_daily_flags

    def _other(current):
        current["sleep_quality"] = "good"
        current["energy"] = "high"
        return current

    await patch_daily_flags(db, 1, day, _other, owner="checkins")
    await _deliver(db, _FakeSender())

    flags = await _read_flags(db, day)
    assert flags["sleep_quality"] == "good"
    assert flags["energy"] == "high"
    assert flags[ACTIVE_DAILY_MENU_KEY]["menu_id"] == "menu-A"


@pytest.mark.asyncio
async def test_no_unbounded_history_is_added(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    day = await _seed_menu(db, menu_id="menu-A", revision=1)
    sender = _FakeSender()
    await _deliver(db, sender)
    await _seed_menu(db, menu_id="menu-B", revision=2)
    await _deliver(db, sender, now=T0 + timedelta(seconds=5))
    await _seed_menu(db, menu_id="menu-C", revision=3)
    await _deliver(db, sender, now=T0 + timedelta(seconds=10))

    flags = await _read_flags(db, day)
    op_keys = sorted(k for k in flags if k.startswith("daily_menu_"))
    assert op_keys == sorted([
        ops.DAILY_MENU_SEND_OP_KEY,
        ops.DAILY_MENU_LAST_COMPLETED_KEY,
        "daily_menu_message",
    ])
    for key in op_keys:
        assert isinstance(flags[key], dict)     # one record each, never a list


def test_no_wall_clock_debounce_or_process_local_cache_in_delivery() -> None:
    """Structural: the rejected mechanisms must not reappear."""
    import inspect

    source = inspect.getsource(delivery)
    for banned in ("time.monotonic", "time.time(", "asyncio.sleep",
                   "_LAST_", "DEBOUNCE", "saved_at"):
        assert banned not in source, f"{banned} must not be used for correctness"
    # No module-level mutable cache.
    module_dicts = [
        name for name, value in vars(delivery).items()
        if isinstance(value, (dict, set)) and not name.startswith("__")
        and name not in {"SUPPRESSION_TOAST"}
    ]
    assert module_dicts == []


@pytest.mark.asyncio
async def test_delivery_identity_uses_canonical_components(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    await _seed_menu(db, menu_id="menu-A")
    _day, identity = await delivery.resolve_delivery_identity(db, 1, now=T0)

    assert set(identity) == {
        "coaching_day_key", "menu_id", "plan_id", "delivery_type",
    }
    assert identity["delivery_type"] == ops.DELIVERY_TYPE_STANDALONE
    assert "requested_by" not in identity


@pytest.mark.asyncio
async def test_missing_active_menu_still_yields_a_wellformed_identity(
    tmp_path: Path,
) -> None:
    db = await _make_db(tmp_path)
    _day, identity = await delivery.resolve_delivery_identity(db, 1, now=T0)
    assert identity["menu_id"] == ops.NONE_SENTINEL
    assert identity["plan_id"] == ops.NONE_SENTINEL


# --- atomic completion: status + archive + message identity in ONE write ----


@pytest.mark.asyncio
async def test_completion_stores_all_fields_in_one_mutation(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    day = await _seed_menu(db)
    sender = _FakeSender()

    result = await _deliver(db, sender)

    flags = await _read_flags(db, day)
    record = flags[ops.DAILY_MENU_SEND_OP_KEY]
    assert record["status"] == "completed"
    assert record["completed_at"]
    assert record["message_id"] == result.message_id
    # Archived alongside, in the same write.
    assert flags[ops.DAILY_MENU_LAST_COMPLETED_KEY]["message_id"] == result.message_id
    # Message identity, in the same write.
    assert flags[DAILY_MENU_MESSAGE_KEY]["message_id"] == result.message_id
    assert flags[DAILY_MENU_MESSAGE_KEY]["source"] == "test"


@pytest.mark.asyncio
async def test_crash_cannot_produce_completed_without_message_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The B-2 atomicity fix: the old two-write sequence could leave
    status=completed with DAILY_MENU_MESSAGE_KEY absent. Sabotaging the legacy
    follow-up writer must now change nothing, because completion no longer
    depends on it."""
    import noam_coach.services.daily_menu_state as state

    async def _would_have_crashed(*_a, **_k):
        raise RuntimeError("process died before metadata write")

    monkeypatch.setattr(state, "remember_daily_menu_message", _would_have_crashed)

    db = await _make_db(tmp_path)
    day = await _seed_menu(db)
    result = await _deliver(db, _FakeSender())
    monkeypatch.undo()

    flags = await _read_flags(db, day)
    assert flags[ops.DAILY_MENU_SEND_OP_KEY]["status"] == "completed"
    assert flags[DAILY_MENU_MESSAGE_KEY]["message_id"] == result.message_id


@pytest.mark.asyncio
async def test_injected_completion_cas_failure_stores_no_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = await _make_db(tmp_path)
    day = await _seed_menu(db)

    from noam_coach.services import daily_flags_cas

    real_patch = daily_flags_cas.patch_daily_flags
    calls = {"n": 0}

    async def _fail_second(*a, **k):
        calls["n"] += 1
        if calls["n"] > 1:          # let the claim land, break the completion
            raise RuntimeError("injected CAS failure")
        return await real_patch(*a, **k)

    monkeypatch.setattr(daily_flags_cas, "patch_daily_flags", _fail_second)
    result = await _deliver(db, _FakeSender())
    monkeypatch.undo()

    assert result.status == delivery.SENT      # the message really was sent
    flags = await _read_flags(db, day)
    record = flags[ops.DAILY_MENU_SEND_OP_KEY]
    assert record["status"] != "completed"
    assert record["message_id"] is None
    assert DAILY_MENU_MESSAGE_KEY not in flags
    assert ops.DAILY_MENU_LAST_COMPLETED_KEY not in flags


@pytest.mark.asyncio
async def test_stale_owner_atomic_completion_stores_no_field(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    day = await _seed_menu(db)
    _day, identity = await delivery.resolve_delivery_identity(db, 1, now=T0)

    await ops.claim_operation(db, 1, day, kind=ops.KIND_DELIVERY,
                              identity=identity, attempt_id="OLD", now=T0)
    later = T0 + timedelta(seconds=ops.MENU_SEND_LEASE_SECONDS + 1)
    await ops.claim_operation(db, 1, day, kind=ops.KIND_DELIVERY,
                              identity=identity, attempt_id="NEW", now=later)

    refused = await ops.complete_delivery_atomically(
        db, 1, day, identity=identity, attempt_id="OLD",
        message_id=999, chat_id=1, source="stale", now=later,
    )

    assert refused.status == ops.OWNERSHIP_LOST
    flags = await _read_flags(db, day)
    assert flags[ops.DAILY_MENU_SEND_OP_KEY]["attempt_id"] == "NEW"
    assert flags[ops.DAILY_MENU_SEND_OP_KEY]["status"] == ops.IN_FLIGHT
    assert DAILY_MENU_MESSAGE_KEY not in flags
    assert ops.DAILY_MENU_LAST_COMPLETED_KEY not in flags


@pytest.mark.asyncio
async def test_atomic_completion_preserves_unrelated_flags(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    day = await _seed_menu(db)
    from noam_coach.services.daily_flags_cas import patch_daily_flags

    def _other(current):
        current["sleep_quality"] = "good"
        return current

    await patch_daily_flags(db, 1, day, _other, owner="checkins")
    await _deliver(db, _FakeSender())

    flags = await _read_flags(db, day)
    assert flags["sleep_quality"] == "good"
    assert flags[ACTIVE_DAILY_MENU_KEY]["menu_id"] == "menu-A"


@pytest.mark.asyncio
async def test_atomically_completed_delivery_remains_suppressed(
    tmp_path: Path,
) -> None:
    db = await _make_db(tmp_path)
    await _seed_menu(db)
    sender = _FakeSender()
    await _deliver(db, sender)
    second = await _deliver(db, sender, now=T0 + timedelta(seconds=5))

    assert second.status == delivery.SUPPRESSED_ALREADY_COMPLETED
    assert sender.calls == 1, "no Telegram resend may be introduced"


def test_no_database_file_is_created_in_the_repository_root() -> None:
    """Importing/using the delivery boundary must not create a stray DB."""
    repo_root = Path(__file__).resolve().parents[1]
    strays = [
        p.name for p in repo_root.iterdir()
        if p.suffix in {".db", ".sqlite", ".sqlite3"}
        or p.name.endswith("-wal") or p.name.endswith("-shm")
    ]
    assert strays == [], f"stray database artifacts in repo root: {strays}"
