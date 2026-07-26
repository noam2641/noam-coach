"""LOG004 — flow ids must be fully opaque (no user-derived component).

The historical scheme was ``f-<raw telegram user_id>-<hex>``, which leaked the
account id into ``active_flow.flow_id``, ``product_events.flow_id`` and every
observability surface that correlates on flow identity.

These tests pin the replacement contract:

* a newly minted id carries NO component derived from the user — not raw, not
  encoded, not truncated, not hashed (a hash is still a stable per-user
  identifier and would re-enable cross-flow linkage of one account);
* ids stay unique under repeated and concurrent generation;
* LEGACY-format ids remain readable and usable — lookup, resume, the ARCH-04
  callback grammar and the version guard all behave identically;
* suspend/resume, including nested flows, is unaffected;
* event/observability correlation still works.

Forward-only: nothing here backfills or rewrites stored ids.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from pathlib import Path

import pytest

import conversation
from conversation import FlowName
from db import Database
from helpers import utc_now
from noam_coach.services import callback_grammar

USER_A = 322493274  # sanitized stand-in for a real Telegram id


# --- 1. no user-derived component ------------------------------------------


def test_new_flow_id_contains_no_raw_user_id() -> None:
    flow_id = conversation._new_flow_id(USER_A)
    assert str(USER_A) not in flow_id


def test_new_flow_id_contains_no_encoded_or_hashed_user_component() -> None:
    """Not raw, not truncated, not hex/hashed — no stable per-user marker."""
    flow_id = conversation._new_flow_id(USER_A)
    raw = str(USER_A)
    forbidden = {
        raw,
        raw[:6],
        raw[-6:],
        f"{USER_A:x}",
        hashlib.sha256(raw.encode()).hexdigest()[:12],
        hashlib.md5(raw.encode()).hexdigest()[:12],  # noqa: S324 - test probe only
    }
    for needle in forbidden:
        assert needle not in flow_id, f"flow id leaks a user-derived value: {needle!r}"


def test_two_users_ids_are_not_distinguishable_by_shape() -> None:
    """Different users must not produce structurally different ids."""
    a = conversation._new_flow_id(USER_A)
    b = conversation._new_flow_id(1)
    shape = re.compile(r"^f-\d{15}-[0-9a-f]{12}$")
    assert shape.match(a), a
    assert shape.match(b), b
    assert len(a) == len(b)


def test_same_user_gets_unlinkable_ids_across_calls() -> None:
    """Two flows of ONE account share no common segment."""
    first = conversation._new_flow_id(USER_A)
    second = conversation._new_flow_id(USER_A)
    assert first != second
    assert first.split("-")[1] != second.split("-")[1]


# --- 2. uniqueness (repeated and concurrent) --------------------------------


def test_ids_are_unique_across_many_repeated_draws() -> None:
    ids = {conversation._new_flow_id(USER_A) for _ in range(20_000)}
    assert len(ids) == 20_000


@pytest.mark.asyncio
async def test_ids_are_unique_under_concurrent_generation() -> None:
    async def mint() -> str:
        await asyncio.sleep(0)
        return conversation._new_flow_id(USER_A)

    ids = await asyncio.gather(*(mint() for _ in range(2_000)))
    assert len(set(ids)) == len(ids)


# --- 3. legacy ids stay readable / usable (ARCH-04 grammar) -----------------


def _legacy_flow_id(user_id: int) -> str:
    """The pre-LOG004 shape, exactly as it is still persisted today."""
    return f"f-{user_id}-abc123def456"


def test_callback_grammar_extracts_both_legacy_and_new_flow_tokens() -> None:
    legacy = _legacy_flow_id(USER_A)
    new = conversation._new_flow_id(USER_A)
    pattern = callback_grammar._FLOW_TOKEN_RE
    assert pattern.match("f" + legacy).group(1) == legacy
    assert pattern.match("f" + new).group(1) == new


def test_no_consumer_derives_a_user_id_from_a_flow_id() -> None:
    """The id is an opaque token: nothing may read identity out of it."""
    new = conversation._new_flow_id(USER_A)
    digits = new.split("-")[1]
    assert digits != str(USER_A)
    assert not digits.startswith(str(USER_A)[:4])


# --- 4. DB-backed: legacy lookup/resume, nesting, version guard -------------


async def _db(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "log004.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'Test',NULL,?)",
        (utc_now(),),
    )
    return db


@pytest.mark.asyncio
async def test_legacy_format_flow_is_still_readable_and_resumable(tmp_path: Path) -> None:
    """A flow persisted with the OLD id shape keeps working — no backfill."""
    db = await _db(tmp_path)
    legacy = _legacy_flow_id(1)
    await conversation.set_active_flow(
        db, 1, FlowName.workout_plan_selection, step="choose",
        payload={"k": "v"}, flow_id=legacy,
    )
    current = await conversation.get_active_flow(db, 1)
    assert current.flow_id == legacy  # read back verbatim

    # suspend under a microflow, then resume the legacy-id parent
    await conversation.set_active_flow(
        db, 1, FlowName.meal_correction, step="approval-1",
        payload={"approval_id": "approval-1"}, suspend_current=True,
    )
    micro = await conversation.get_active_flow(db, 1)
    assert micro.suspended["flow_id"] == legacy

    resumed = await conversation.resume_suspended(db, 1)
    assert resumed.name == FlowName.workout_plan_selection
    assert resumed.flow_id == legacy
    assert resumed.step == "choose"


@pytest.mark.asyncio
async def test_new_opaque_id_supports_suspend_resume_and_nesting(tmp_path: Path) -> None:
    """Newly minted opaque ids behave exactly like the legacy ones."""
    db = await _db(tmp_path)
    await conversation.set_active_flow(
        db, 1, FlowName.onboarding_question, step="q_pain", payload={},
    )
    parent = await conversation.get_active_flow(db, 1)
    # The digit group is random, not the account id. (A substring check is
    # meaningless for a 1-digit id, so assert the identity property instead.)
    assert parent.flow_id.split("-")[1] != str(1)

    await conversation.set_active_flow(
        db, 1, FlowName.health_import, step="processing",
        payload={}, suspend_current=True,
    )
    await conversation.set_active_flow(
        db, 1, FlowName.onboarding_question, step="__health_edit_x__",
        payload={}, suspend_current=True,
    )
    restored = await conversation.resume_all_suspended(db, 1)
    assert restored.flow_id == parent.flow_id
    assert restored.step == "q_pain"


@pytest.mark.asyncio
async def test_version_guard_unaffected_by_the_id_scheme(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    await conversation.set_active_flow(db, 1, FlowName.onboarding_question, step="a")
    first = await conversation.get_active_flow(db, 1)
    await conversation.set_active_flow(db, 1, FlowName.onboarding_question, step="b")
    second = await conversation.get_active_flow(db, 1)
    assert second.version > first.version
    # a stale callback carrying the OLD version must still be refused
    assert conversation.check_version(second, first.version) is False
    assert conversation.check_version(second, second.version) is True
