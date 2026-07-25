from __future__ import annotations

from pathlib import Path

import pytest

import conversation
from db import Database
from helpers import utc_now


async def _db(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "conversation.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'Test',NULL,?)",
        (utc_now(),),
    )
    return db


@pytest.mark.asyncio
async def test_microflow_resumes_parent_after_restart_safe_storage(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    await conversation.set_active_flow(
        db,
        1,
        conversation.FlowName.workout_plan_selection,
        step="choose",
        payload={"candidate_ids": [1, 2, 3]},
    )
    parent = await conversation.get_active_flow(db, 1)
    await conversation.set_active_flow(
        db,
        1,
        conversation.FlowName.meal_correction,
        step="approval-1",
        payload={"approval_id": "approval-1"},
        suspend_current=True,
    )
    micro = await conversation.get_active_flow(db, 1)
    assert micro.suspended is not None
    assert micro.suspended["flow_id"] == parent.flow_id

    resumed = await conversation.resume_suspended(db, 1)
    assert resumed.name == conversation.FlowName.workout_plan_selection
    assert resumed.step == "choose"


@pytest.mark.asyncio
async def test_nested_two_deep_suspension_fully_unwinds_to_parent(tmp_path: Path) -> None:
    """LOG-015: parent question -> health_import -> wizard onboarding_question
    (2-deep) must fully restore the ORIGINAL parent, not stop one level short."""
    db = await _db(tmp_path)
    # 1) Original parent — a safety-critical onboarding question.
    await conversation.set_active_flow(
        db,
        1,
        conversation.FlowName.onboarding_question,
        step="training_limitations",
    )
    parent = await conversation.get_active_flow(db, 1)
    # 2) Health import microflow suspends the parent.
    await conversation.set_active_flow(
        db,
        1,
        conversation.FlowName.health_import,
        step="processing",
        suspend_current=True,
    )
    health = await conversation.get_active_flow(db, 1)
    # 3) The confirm wizard's own question suspends the health import.
    await conversation.set_active_flow(
        db,
        1,
        conversation.FlowName.onboarding_question,
        step="__health_edit_workout_frequency__",
        suspend_current=True,
    )
    wizard = await conversation.get_active_flow(db, 1)
    assert wizard.suspended is not None
    assert wizard.suspended["flow_id"] == health.flow_id

    # A single pop only reaches the intermediate health_import (regression guard).
    one = await conversation.resume_suspended(db, 1)
    assert one.name == conversation.FlowName.health_import
    assert one.suspended is not None  # the parent is still nested underneath

    # Re-suspend to rebuild the full 2-deep chain, then unwind it all at once.
    await conversation.set_active_flow(
        db,
        1,
        conversation.FlowName.onboarding_question,
        step="__health_edit_workout_frequency__",
        suspend_current=True,
    )
    restored = await conversation.resume_all_suspended(db, 1)
    assert restored is not None
    assert restored.name == conversation.FlowName.onboarding_question
    assert restored.step == "training_limitations"
    assert restored.flow_id == parent.flow_id
    assert restored.suspended is None


@pytest.mark.asyncio
async def test_stale_callback_is_rejected(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    await conversation.set_active_flow(
        db,
        1,
        conversation.FlowName.nutrition_plan_selection,
        step="choose",
    )
    flow = await conversation.get_active_flow(db, 1)
    callback = conversation.encode_callback(
        "planv2", "select", "10", flow_id=flow.flow_id, version=flow.version
    )
    assert conversation.check_version(
        flow,
        conversation.extract_version(callback),
        conversation.extract_flow_id(callback),
    ) is True

    await conversation.update_flow(db, 1, step="confirmed")
    current = await conversation.get_active_flow(db, 1)
    assert conversation.check_version(
        current,
        conversation.extract_version(callback),
        conversation.extract_flow_id(callback),
    ) is False


@pytest.mark.asyncio
async def test_router_gives_active_meal_flow_priority(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    await conversation.set_active_flow(
        db,
        1,
        conversation.FlowName.meal_correction,
        step="approval-1",
    )
    routed = await conversation.ConversationRouter.route(db, 1, "text")
    assert routed.handler == "meal_flow"


@pytest.mark.asyncio
async def test_document_interrupts_parent_as_health_microflow(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    await conversation.set_active_flow(
        db,
        1,
        conversation.FlowName.workout_plan_selection,
        step="choose",
    )
    routed = await conversation.ConversationRouter.route(db, 1, "document")
    assert routed.handler == "health_import"
    assert routed.action == "interrupt"

    await conversation.set_active_flow(
        db,
        1,
        conversation.FlowName.health_import,
        step="processing",
        suspend_current=True,
    )
    active = await conversation.get_active_flow(db, 1)
    assert active.name == conversation.FlowName.health_import
    assert active.suspended is not None


@pytest.mark.asyncio
async def test_second_document_is_consumed_by_existing_health_import(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    await conversation.set_active_flow(
        db,
        1,
        conversation.FlowName.health_import,
        step="processing",
    )
    routed = await conversation.ConversationRouter.route(db, 1, "document")
    assert routed.handler == "health_import"
    assert routed.action == "consume"
