"""Observability O7 — meaningful state transition tests.

Acceptance criteria covered:
- a flow transition is reconstructable before/after (start/update/
  complete/expire/suspend)
- meal save/undo reconstruction relies on the pre-existing domain events
  joining traces (covered in O5) — here we prove the flow/state families
- workout set/session transitions are reconstructable
- recommendation invalidation is visible
- daily-menu presentation/invalidation is visible with reasons
- goal/plan activation is visible
- health-import reconciliation has start/result/failure semantics
- fact mutations carry before/after and skip no-op writes (bounded volume)
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import coach_bot
import conversation
import event_log
import planning
import user_model
from db import Database
from helpers import utc_now
from noam_coach.observability import ObservabilityMode, interaction_scope, set_mode
from noam_coach.observability.emit import reset_observability_health
from noam_coach.observability.modes import reset_mode
from noam_coach.observability.state_trace import install_state_trace, uninstall_state_trace

USER_ID = 1


@pytest.fixture
async def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Database:
    database = Database(str(tmp_path / "obs_o7.db"))
    await database.init()
    await database.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(?, 'Test', NULL, ?)",
        (USER_ID, utc_now()),
    )
    monkeypatch.setattr(coach_bot, "DB", database)
    from config import SETTINGS

    monkeypatch.setattr(SETTINGS, "telegram_allowed_user_id", USER_ID, raising=False)
    return database


@pytest.fixture(autouse=True)
def _obs_isolation():
    set_mode(ObservabilityMode.CONTENT)
    reset_observability_health()
    install_state_trace()
    yield
    uninstall_state_trace()
    reset_mode()
    reset_observability_health()


def _by_event(events: list, name: str) -> list:
    return [e for e in events if e.event == name]


def _mutations(events: list, domain: str) -> list:
    return [
        e for e in events
        if e.event == "state.mutated" and e.properties.get("domain") == domain
    ]


@pytest.mark.asyncio
async def test_flow_lifecycle_started_updated_completed(db: Database) -> None:
    with interaction_scope(user_id=USER_ID):
        await conversation.set_active_flow(
            db, USER_ID, conversation.FlowName.goal_review, step="ask", payload={"goal": 1},
        )
        await conversation.set_active_flow(
            db, USER_ID, conversation.FlowName.goal_review, step="confirm", payload={"goal": 1},
        )
        await conversation.clear_active_flow(db, USER_ID)

    events = await event_log.list_events(db, USER_ID)
    started = _by_event(events, "flow.started")[0]
    assert started.properties["flow_before"]["name"] == "idle"
    assert started.properties["flow_after"]["name"] == "goal_review"
    assert started.properties["flow_after"]["step"] == "ask"
    assert started.after["payload"] == {"goal": 1}

    updated = _by_event(events, "flow.updated")
    completed = _by_event(events, "flow.completed")[0]
    assert completed.properties["flow_before"]["name"] == "goal_review"
    assert completed.properties["flow_after"]["name"] == "idle"
    # started → (updated…) → completed, reconstructable in order.
    assert events.index(started) < events.index(completed)
    if updated:
        assert updated[0].properties["flow_before"]["step"] == "ask"
        assert updated[0].properties["flow_after"]["step"] == "confirm"


@pytest.mark.asyncio
async def test_flow_expiry_is_named_expired(db: Database) -> None:
    await conversation.set_active_flow(
        db, USER_ID, conversation.FlowName.goal_review, step="ask",
        expiry_minutes=1,
    )
    # Force the stored expiry into the past.
    past = (datetime.now().astimezone() - timedelta(minutes=5)).isoformat()
    await db.execute(
        "UPDATE active_flow SET expires_at=? WHERE user_id=?", (past, USER_ID),
    )
    with interaction_scope(user_id=USER_ID):
        flow = await conversation.expire_if_needed(db, USER_ID)
    assert flow.is_idle
    events = await event_log.list_events(db, USER_ID)
    expired = _by_event(events, "flow.expired")
    assert expired, [e.event for e in events]
    assert expired[0].properties["flow_before"]["name"] == "goal_review"


@pytest.mark.asyncio
async def test_fact_mutations_have_before_after_and_skip_noops(db: Database) -> None:
    with interaction_scope(user_id=USER_ID):
        await user_model.set_fact(db, USER_ID, "weight_kg", 100.0, source=user_model.SOURCE_USER)
        await user_model.set_fact(db, USER_ID, "weight_kg", 98.5, source=user_model.SOURCE_USER)
        await user_model.set_fact(db, USER_ID, "weight_kg", 98.5, source=user_model.SOURCE_USER)  # no-op
        await user_model.confirm_fact(db, USER_ID, "weight_kg")
        await user_model.invalidate_fact(db, USER_ID, "weight_kg")

    events = await event_log.list_events(db, USER_ID)
    fact_events = _mutations(events, "user_fact")
    actions = [e.properties["action"] for e in fact_events]
    assert actions == ["created", "changed", "confirmed", "invalidated"]
    changed = fact_events[1]
    assert changed.before == {"value": 100.0}
    assert changed.after == {"value": 98.5}


@pytest.mark.asyncio
async def test_goal_and_plan_activation_visible(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    from noam_coach.services import goals as goals_module

    monkeypatch.setattr(goals_module, "DB", db, raising=False)
    version_id = await db.execute(
        """
        INSERT INTO goal_versions(user_id, calories, protein, steps, phase, status, source, created_at)
        VALUES(?, 2100, 150, 8000, 'fat_loss_muscle_retention', 'proposed', 'test', ?)
        """,
        (USER_ID, utc_now()),
    )
    with interaction_scope(user_id=USER_ID):
        await goals_module.activate_goal_version(USER_ID, version_id)

    import json as _json

    plan_id = await db.execute(
        """
        INSERT INTO plan_versions(user_id, plan_type, title, strategy, fit_score, status, payload,
                                  rationale, tradeoffs, assumptions, based_on, created_at)
        VALUES(?, 'workout', 'Plan A', 'balanced', 1, 'candidate', ?, '[]', '[]', '[]', '{}', ?)
        """,
        (USER_ID, _json.dumps({"sessions": []}), utc_now()),
    )
    async def readiness_ok(*a: Any, **k: Any) -> None:
        return None

    # Plan-readiness validation is planning's own concern; this test proves
    # the observability wrapper around a successful activation.
    monkeypatch.setattr(planning, "_validate_plan_for_activation", readiness_ok)
    with interaction_scope(user_id=USER_ID):
        activated = await planning.activate_plan(db, USER_ID, plan_id)
    assert activated is not None

    events = await event_log.list_events(db, USER_ID)
    goal_events = _mutations(events, "goal")
    assert goal_events and goal_events[0].outcome == "activated"
    assert goal_events[0].properties["goal_version_id"] == version_id
    plan_events = _mutations(events, "plan")
    assert plan_events and plan_events[0].outcome == "activated"
    assert plan_events[0].properties["plan_type"] == "workout"


@pytest.mark.asyncio
async def test_recommendation_lifecycle_presented_selected_invalidated(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    from noam_coach.services import core as core_services
    from noam_coach.services import next_meal

    monkeypatch.setattr(core_services, "DB", db, raising=False)
    monkeypatch.setattr(coach_bot, "DB", db)

    class _Rec:
        options = [type("O", (), {"title": "אופציה"})()]
        context = SimpleNamespace(nutrition=SimpleNamespace(calorie_balance=420))
        budget = SimpleNamespace(policy="default")

    async def fake_set_flow_state(*a: Any, **k: Any) -> None:
        return None

    async def fake_get_flow_state(*a: Any, **k: Any) -> tuple:
        return ("active", {"options": []})

    async def fake_clear_flow_state(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr(core_services, "set_flow_state", fake_set_flow_state, raising=False)
    monkeypatch.setattr(core_services, "clear_flow_state", fake_clear_flow_state, raising=False)
    monkeypatch.setattr(coach_bot, "set_flow_state", fake_set_flow_state, raising=False)
    monkeypatch.setattr(coach_bot, "clear_flow_state", fake_clear_flow_state, raising=False)
    # The wrapper is under test, not the option serializers — keep the fake
    # recommendation duck-typed by stubbing the fingerprint/payload helpers.
    monkeypatch.setattr(next_meal, "option_fingerprint", lambda o: {"title": o.title}, raising=False)
    monkeypatch.setattr(next_meal, "_option_to_payload", lambda o: {"title": o.title}, raising=False)

    with interaction_scope(user_id=USER_ID):
        rec = _Rec()
        rec.options[0].title = "שייק חלבון"

        # NextMealRecommendation duck-type is enough for the wrapper.
        await next_meal.remember_active_recommendation(db, USER_ID, rec, message_id=42)
        await next_meal.clear_active_recommendation(db, USER_ID)

    events = await event_log.list_events(db, USER_ID)
    rec_events = _mutations(events, "recommendation")
    assert [e.outcome for e in rec_events] == ["presented", "invalidated"]
    assert rec_events[0].properties["option_titles"] == ["שייק חלבון"]
    assert rec_events[0].properties["message_id"] == 42


@pytest.mark.asyncio
async def test_daily_menu_presented_and_invalidated_with_reason(db: Database) -> None:
    from noam_coach.services import daily_menu_state

    with interaction_scope(user_id=USER_ID):
        await daily_menu_state.remember_active_daily_menu(
            db, USER_ID, text="תפריט היום", strategy="balanced", meals=[],
        )
        marked = await daily_menu_state.mark_daily_menu_stale(
            db, USER_ID, reason="goal_changed",
        )
    assert marked is True
    events = await event_log.list_events(db, USER_ID)
    menu_events = _mutations(events, "daily_menu")
    assert [e.outcome for e in menu_events] == ["presented", "invalidated"]
    assert menu_events[1].properties["reason"] == "goal_changed"


@pytest.mark.asyncio
async def test_workout_set_and_session_completion_visible(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    from noam_coach.bot import workout as workout_module

    calls: list[tuple] = []

    async def fake_save_set(session: dict, weight: float, reps: int, rir: int, source: str, client_event_id: Any = None) -> tuple:
        calls.append((weight, reps))
        return (True, 3)  # this save completed the session

    # Wrap OUR fake (uninstall first so the wrap applies to the fake).
    uninstall_state_trace()
    monkeypatch.setattr(workout_module, "save_set", fake_save_set)
    install_state_trace()

    session = {"id": 7, "user_id": USER_ID, "code": "A", "exercise_index": 2, "set_number": 3}
    with interaction_scope(user_id=USER_ID):
        result = await workout_module.save_set(session, 60.0, 10, 2, "telegram")
    assert result == (True, 3)
    assert calls == [(60.0, 10)]

    events = await event_log.list_events(db, USER_ID)
    set_events = _mutations(events, "workout_set")
    assert set_events[0].properties == {
        "domain": "workout_set", "action": "saved", "session_id": 7,
        "session_code": "A", "exercise_index": 2, "set_number": 3,
        "weight": 60.0, "reps": 10, "rir": 2, "input_source": "telegram",
    }
    session_events = _mutations(events, "workout_session")
    assert session_events[0].outcome == "completed"


@pytest.mark.asyncio
async def test_rest_timer_lifecycle_events(db: Database) -> None:
    from noam_coach.bot import workout_runtime

    with interaction_scope(user_id=USER_ID):
        await workout_runtime._emit_rest_event(USER_ID, 7, "started", total_seconds=90)
        await workout_runtime._emit_rest_event(USER_ID, 7, "restored", remaining_seconds=30, total_seconds=90)
        await workout_runtime._emit_rest_event(USER_ID, 7, "finished", total_seconds=90)

    events = await event_log.list_events(db, USER_ID)
    rest_events = _mutations(events, "rest_timer")
    assert [e.outcome for e in rest_events] == ["started", "restored", "finished"]
    assert rest_events[1].properties["remaining_seconds"] == 30


@pytest.mark.asyncio
async def test_health_reconciliation_start_result_failure(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    from noam_coach.services import health_jobs

    async def ok_reconciliation(message: Any, user_id: int) -> str:
        return "ok"

    async def bad_reconciliation(message: Any, user_id: int) -> str:
        raise RuntimeError("simulated reconciliation failure")

    uninstall_state_trace()
    monkeypatch.setattr(health_jobs, "run_post_import_reconciliation", ok_reconciliation)
    install_state_trace()
    with interaction_scope(user_id=USER_ID):
        assert await health_jobs.run_post_import_reconciliation(None, USER_ID) == "ok"

    uninstall_state_trace()
    monkeypatch.setattr(health_jobs, "run_post_import_reconciliation", bad_reconciliation)
    install_state_trace()
    with interaction_scope(user_id=USER_ID):
        with pytest.raises(RuntimeError):
            await health_jobs.run_post_import_reconciliation(None, USER_ID)

    events = await event_log.list_events(db, USER_ID)
    health_events = _mutations(events, "health_import")
    outcomes = [e.outcome for e in health_events]
    assert outcomes == [
        "reconciliation_started", "reconciliation_completed",
        "reconciliation_started", "reconciliation_failed",
    ]
    failed = health_events[-1]
    assert failed.properties["error_type"] == "RuntimeError"
    assert failed.status == "failed"
