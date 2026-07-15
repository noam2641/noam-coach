"""Observability O8 — Mini App semantic view observability tests.

Acceptance criteria covered:
- server API success is NOT treated as view-render success (only the
  client's explicit report creates ui.view.rendered)
- an action references its source view/render; the resulting view links
  back to the action via the deterministic client trace
- focus refresh is a distinguishable trigger
- the reporting endpoint rejects arbitrary event types / views / oversized
  payloads and hard-bounds batch size
- reporting failure never breaks the Mini App (client: swallow-all
  guarantees asserted at source level; server: tolerant per-event loop)
- no full-DOM/screenshot recording exists
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import coach_bot
import event_log
import mini_api
import miniapp
from db import Database
from helpers import utc_now
from noam_coach.observability import ObservabilityMode, set_mode
from noam_coach.observability.emit import reset_observability_health
from noam_coach.observability.modes import reset_mode
from noam_coach.observability.obs_context import (
    current_interaction_id,
    current_trace_id,
    current_user_id,
)

USER_ID = 1


@pytest.fixture
async def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Database:
    database = Database(str(tmp_path / "obs_o8.db"))
    await database.init()
    await database.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(?, 'Test', NULL, ?)",
        (USER_ID, utc_now()),
    )
    monkeypatch.setattr(coach_bot, "DB", database)
    monkeypatch.setattr(mini_api, "DB", database)
    from config import SETTINGS

    monkeypatch.setattr(SETTINGS, "telegram_allowed_user_id", USER_ID, raising=False)
    return database


@pytest.fixture(autouse=True)
def _obs_isolation():
    set_mode(ObservabilityMode.CONTENT)
    reset_observability_health()
    yield
    reset_mode()
    reset_observability_health()


CI = "ci_abc123def45600aa"


@pytest.mark.asyncio
async def test_view_render_and_action_events_are_recorded_and_correlated(db: Database) -> None:
    render_id = "rn_11aa22bb33cc44dd"
    response = await mini_api.mini_obs_events(
        payload={
            "events": [
                {
                    "event": "ui.action.activated",
                    "action": "next_meal_workout_status",
                    "source_view": "next_meal",
                    "source_render_id": render_id,
                    "client_interaction_id": CI,
                    "payload": {"status": "completed"},
                    "client_ts": 1,
                },
                {
                    "event": "ui.view.rendered",
                    "view": "next_meal",
                    "render_id": "rn_55ee66ff77aa88bb",
                    "trigger": "post_mutation_refresh",
                    "caused_by_client_interaction_id": CI,
                    "state": {"text_chars": 240, "action_count": 3},
                    "client_ts": 2,
                },
            ]
        },
        user_id=USER_ID,
    )
    body = json.loads(response.body)
    assert body == {"accepted": 2, "rejected": 0}

    events = await event_log.list_events(db, USER_ID)
    action = [e for e in events if e.event == "ui.action.activated"][0]
    rendered = [e for e in events if e.event == "ui.view.rendered"][0]

    assert action.surface == "mini_app"
    assert action.properties["source_view"] == "next_meal"
    assert action.properties["source_render_id"] == render_id
    assert action.interaction_id == CI

    assert rendered.properties["view"] == "next_meal"
    assert rendered.properties["trigger"] == "post_mutation_refresh"
    assert rendered.properties["caused_by_client_interaction_id"] == CI
    assert rendered.properties["content"]["state"]["action_count"] == 3

    # The resulting view links to the action: one deterministic client trace.
    assert action.trace_id == rendered.trace_id == "tr_mini_" + CI[3:]


@pytest.mark.asyncio
async def test_api_processing_joins_the_client_action_trace(db: Database) -> None:
    """The correlation dependency puts the whole API request inside the
    client action's trace — no clock-time matching anywhere."""
    agen = mini_api.mini_obs_scope(user_id=USER_ID, x_obs_client_interaction=CI)
    user_id = await agen.__anext__()
    assert user_id == USER_ID
    assert current_trace_id() == "tr_mini_" + CI[3:]
    assert current_interaction_id() == CI
    assert current_user_id() == USER_ID
    # Server-side events emitted during the request join the same trace.
    await event_log.append_event(db, USER_ID, "workout_status_saved", entity="next_meal")
    with pytest.raises(StopAsyncIteration):
        await agen.__anext__()

    event = (await event_log.list_events(db, USER_ID))[0]
    assert event.trace_id == "tr_mini_" + CI[3:]
    assert event.interaction_id == CI


@pytest.mark.asyncio
async def test_invalid_header_falls_back_to_fresh_scope(db: Database) -> None:
    agen = mini_api.mini_obs_scope(
        user_id=USER_ID,
        x_obs_client_interaction="ci_' OR 1=1 --",  # not a valid client id
    )
    await agen.__anext__()
    assert current_interaction_id() != "ci_' OR 1=1 --"
    assert current_trace_id() is not None  # fresh server-side scope
    with pytest.raises(StopAsyncIteration):
        await agen.__anext__()


@pytest.mark.asyncio
async def test_endpoint_rejects_arbitrary_events_views_and_oversized_payloads(db: Database) -> None:
    response = await mini_api.mini_obs_events(
        payload={
            "events": [
                {"event": "observability.write_failed"},          # not allowlisted
                {"event": "ui.view.rendered", "view": "admin"},  # unknown view
                {"event": "ui.action.activated", "action": "drop_tables", "source_view": "profile"},
                {"event": "ui.view.rendered", "view": "profile", "state": {"x": "y" * 5000}},
                "not-a-dict",
            ]
        },
        user_id=USER_ID,
    )
    body = json.loads(response.body)
    assert body == {"accepted": 0, "rejected": 5}
    assert await event_log.list_events(db, USER_ID) == []

    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        await mini_api.mini_obs_events(
            payload={"events": [{"event": "ui.view.rendered"}] * 21},
            user_id=USER_ID,
        )
    with pytest.raises(HTTPException):
        await mini_api.mini_obs_events(payload={"events": "nope"}, user_id=USER_ID)


@pytest.mark.asyncio
async def test_server_api_success_is_not_view_render_success(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    """Calling a read API inside the correlation scope emits NO
    ui.view.rendered — only the client's explicit report does."""
    del monkeypatch  # the real read endpoint runs against the test DB
    agen = mini_api.mini_obs_scope(user_id=USER_ID, x_obs_client_interaction=CI)
    await agen.__anext__()
    response = await mini_api.mini_today_meals(user_id=USER_ID)
    assert response.status_code == 200
    with pytest.raises(StopAsyncIteration):
        await agen.__anext__()
    rendered = [
        e for e in await event_log.list_events(db, USER_ID)
        if e.event == "ui.view.rendered"
    ]
    assert rendered == []


def test_client_source_reports_semantic_views_and_never_records_dom() -> None:
    js = (miniapp.STATIC_DIR / "app.js").read_text(encoding="utf-8")
    # Semantic view reporting exists for every important region.
    for view in ("dashboard", "operations", "next_meal", "today_meals",
                 "plan_candidates", "profile", "health_upload"):
        assert f"obsViewRendered('{view}'" in js, view
    # Action reporting exists for the important behaviors.
    for action in ("next_meal_workout_status", "refresh_next_meal",
                   "refresh_today_meals", "generate_plans", "activate_plan",
                   "build_unified_plan", "save_profile",
                   "health_file_selected", "health_import_submitted"):
        assert f"obsAction('{action}'" in js, action
    # Correlation header + bounded batch transport.
    assert "X-Obs-Client-Interaction" in js
    assert "/mini/api/obs/events" in js
    assert "obsFlush" in js and "keepalive" in js
    # Failure containment: telemetry fetch errors are swallowed.
    assert ".catch(() => {})" in js
    # Focus refresh stays distinguishable from user actions.
    assert "loadDashboard('focus_refresh')" in js
    assert "loadNextMeal('user_action')" in js
    # No full-DOM recording / screenshots / session video.
    assert "outerHTML" not in js
    assert "document.body.innerHTML" not in js
    assert "getDisplayMedia" not in js
    assert "MutationObserver" not in js


@pytest.mark.asyncio
async def test_reporting_failure_does_not_break_the_endpoint(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    """A broken store: the endpoint still answers, health counters count."""
    class _Broken:
        async def execute(self, *a: Any, **k: Any) -> int:
            raise RuntimeError("simulated outage")

    monkeypatch.setattr(mini_api, "DB", _Broken())
    response = await mini_api.mini_obs_events(
        payload={"events": [{"event": "ui.view.rendered", "view": "dashboard",
                             "render_id": "rn_0011aabbccddeeff", "trigger": "initial_load"}]},
        user_id=USER_ID,
    )
    body = json.loads(response.body)
    # emit_event contains the failure; the event is counted as accepted
    # (the client did its part) and the degradation is visible in health.
    assert body["accepted"] == 1
    from noam_coach.observability import observability_health

    assert observability_health()["write_failures"] >= 1
