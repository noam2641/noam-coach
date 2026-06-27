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
