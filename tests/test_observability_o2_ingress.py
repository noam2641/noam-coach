"""Observability O2 — Telegram ingress + routing envelope acceptance tests.

Acceptance criteria covered:
- one interaction receives one stable interaction_id across all its events
- the route decision is reconstructable (handler/action/reason/flow)
- photo/document interactions reference provider media identities (no bytes)
- callback data is recorded safely + explicit unresolved render correlation
- the pre-routing flow snapshot is visible
- deterministic coverage for idle, active-flow interrupt, callback, photo
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import coach_bot
import conversation
import event_log
from db import Database
from helpers import utc_now
from noam_coach.observability import ObservabilityMode, interaction_scope, set_mode, trace_reader
from noam_coach.observability.emit import reset_observability_health
from noam_coach.observability.modes import reset_mode
from noam_coach.observability.telegram_ingress import (
    install_routing_observer,
    observed_handler,
    uninstall_routing_observer,
)

ALLOWED_USER_ID = 1


@pytest.fixture
async def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Database:
    database = Database(str(tmp_path / "obs_o2.db"))
    await database.init()
    await database.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(?, 'Test', NULL, ?)",
        (ALLOWED_USER_ID, utc_now()),
    )
    monkeypatch.setattr(coach_bot, "DB", database)
    from config import SETTINGS

    monkeypatch.setattr(SETTINGS, "telegram_allowed_user_id", ALLOWED_USER_ID, raising=False)
    return database


@pytest.fixture(autouse=True)
def _obs_isolation():
    set_mode(ObservabilityMode.CONTENT)
    reset_observability_health()
    yield
    uninstall_routing_observer()
    reset_mode()
    reset_observability_health()


def _update(
    *,
    user_id: int = ALLOWED_USER_ID,
    text: str | None = None,
    caption: str | None = None,
    photo: list[Any] | None = None,
    document: Any | None = None,
    callback_data: str | None = None,
    message_id: int = 111,
) -> SimpleNamespace:
    message = SimpleNamespace(
        message_id=message_id,
        text=text,
        caption=caption,
        photo=photo or [],
        document=document,
    )
    callback_query = None
    if callback_data is not None:
        callback_query = SimpleNamespace(data=callback_data, message=message)
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id, first_name="Test", username=None),
        effective_chat=SimpleNamespace(id=555),
        effective_message=message,
        callback_query=callback_query,
    )


@pytest.mark.asyncio
async def test_text_interaction_has_one_stable_interaction_id(db: Database) -> None:
    """The wrapped handler and everything it awaits share one interaction_id;
    interaction.received captures the text and the idle pre-routing flow."""
    seen: dict[str, Any] = {}

    async def handler(update: Any, context: Any) -> None:
        from noam_coach.observability import current_interaction_id, current_trace_id, emit_event

        seen["interaction_id"] = current_interaction_id()
        seen["trace_id"] = current_trace_id()
        await emit_event(db, ALLOWED_USER_ID, "context.built", entity="context")

    wrapped = observed_handler("text", handler)
    await wrapped(_update(text="מה לאכול עכשיו?"), None)

    events = await trace_reader.events_for_interaction(db, ALLOWED_USER_ID, seen["interaction_id"])
    assert [e.event for e in events] == ["interaction.received", "context.built"]
    received = events[0]
    assert received.trace_id == seen["trace_id"]
    assert received.surface == "telegram"
    assert received.properties["kind"] == "text"
    assert received.properties["chat_id"] == 555
    assert received.properties["flow_before"]["name"] == "idle"
    assert received.properties["content"]["text"] == "מה לאכול עכשיו?"


@pytest.mark.asyncio
async def test_routing_decided_is_reconstructable_for_idle_free_text(db: Database) -> None:
    install_routing_observer()

    async def handler(update: Any, context: Any) -> None:
        await conversation.ConversationRouter.route(db, ALLOWED_USER_ID, "text")

    await observed_handler("text", handler)(_update(text="hi"), None)

    events = await event_log.list_events(db, ALLOWED_USER_ID, event="routing.decided")
    assert len(events) == 1
    decided = events[0]
    assert decided.properties["input_kind"] == "text"
    assert decided.properties["handler"] == "free_text"
    assert decided.properties["action"] == "fallback"
    assert decided.properties["reason"] == "no active flow"
    assert decided.outcome == "free_text"
    assert decided.interaction_id is not None
    # Same interaction as the ingress event.
    received = await event_log.list_events(db, ALLOWED_USER_ID, event="interaction.received")
    assert received[0].interaction_id == decided.interaction_id
    assert decided.span_id is not None  # routing ran inside its own span


@pytest.mark.asyncio
async def test_active_flow_interrupt_shows_flow_before_and_decision(db: Database) -> None:
    """Photo during a non-meal active flow → meal microflow interrupts; the
    pre-routing snapshot must show the flow that was active BEFORE."""
    await conversation.set_active_flow(
        db, ALLOWED_USER_ID, conversation.FlowName.goal_review, step="review",
        payload={"goal_id": 7},
    )
    install_routing_observer()
    decisions: list[Any] = []

    async def handler(update: Any, context: Any) -> None:
        decisions.append(await conversation.ConversationRouter.route(db, ALLOWED_USER_ID, "photo"))

    photo = [SimpleNamespace(file_id="f1", file_unique_id="u1", width=90, height=90, file_size=100),
             SimpleNamespace(file_id="f2", file_unique_id="u2", width=800, height=600, file_size=52341)]
    await observed_handler("photo", handler)(_update(photo=photo, caption="צהריים"), None)

    assert decisions[0].handler == "meal_logging"
    assert decisions[0].action == "interrupt"

    received = (await event_log.list_events(db, ALLOWED_USER_ID, event="interaction.received"))[0]
    assert received.properties["flow_before"]["name"] == "goal_review"
    assert received.properties["flow_before"]["step"] == "review"
    media = received.properties["media"]
    assert media["media_kind"] == "photo"
    assert media["file_unique_id"] == "u2"  # largest variant identity
    assert media["width"] == 800
    assert "bytes" not in str(media)
    assert received.properties["content"]["caption"] == "צהריים"
    assert received.properties["content"]["flow_payload_before"] == {"goal_id": 7}

    decided = (await event_log.list_events(db, ALLOWED_USER_ID, event="routing.decided"))[0]
    assert decided.properties["action"] == "interrupt"
    assert decided.interaction_id == received.interaction_id


@pytest.mark.asyncio
async def test_callback_records_data_and_explicit_unresolved_render(db: Database) -> None:
    async def handler(update: Any, context: Any) -> None:
        return None

    await observed_handler("callback", handler)(_update(callback_data="menu:today", message_id=99), None)

    received = (await event_log.list_events(db, ALLOWED_USER_ID, event="interaction.received"))[0]
    assert received.properties["kind"] == "callback"
    assert received.properties["callback_data"] == "menu:today"
    assert received.properties["source_message_id"] == 99

    activated = (await event_log.list_events(db, ALLOWED_USER_ID, event="ui.control.activated"))[0]
    assert activated.properties["callback_data"] == "menu:today"
    assert activated.properties["source_render_id"] is None
    assert activated.properties["correlation"] == "unresolved"
    assert activated.interaction_id == received.interaction_id
    assert activated.trace_id == received.trace_id


@pytest.mark.asyncio
async def test_document_interaction_references_media_identity(db: Database) -> None:
    async def handler(update: Any, context: Any) -> None:
        return None

    document = SimpleNamespace(
        file_id="doc-f", file_unique_id="doc-u", mime_type="application/zip",
        file_size=123456, file_name="export.zip",
    )
    await observed_handler("document", handler)(_update(document=document), None)

    received = (await event_log.list_events(db, ALLOWED_USER_ID, event="interaction.received"))[0]
    media = received.properties["media"]
    assert media == {
        "media_kind": "document",
        "provider": "telegram",
        "file_id": "doc-f",
        "file_unique_id": "doc-u",
        "mime_type": "application/zip",
        "byte_size": 123456,
        "file_name": "export.zip",
    }


@pytest.mark.asyncio
async def test_unauthorized_user_is_not_traced_and_handler_still_runs(db: Database) -> None:
    calls: list[int] = []

    async def handler(update: Any, context: Any) -> None:
        calls.append(1)

    await observed_handler("text", handler)(_update(user_id=999, text="hi"), None)
    assert calls == [1]
    assert await event_log.list_events(db, ALLOWED_USER_ID) == []
    assert await db.fetch_all(
        "SELECT * FROM product_events WHERE user_id=999"
    ) == []


@pytest.mark.asyncio
async def test_two_interactions_get_distinct_traces_by_default(db: Database) -> None:
    async def handler(update: Any, context: Any) -> None:
        return None

    wrapped = observed_handler("text", handler)
    await wrapped(_update(text="one"), None)
    await wrapped(_update(text="two"), None)
    events = await event_log.list_events(db, ALLOWED_USER_ID, event="interaction.received")
    assert len(events) == 2
    assert events[0].trace_id != events[1].trace_id
    assert events[0].interaction_id != events[1].interaction_id


@pytest.mark.asyncio
async def test_observability_failure_does_not_break_the_handler(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ingress instrumentation with a broken store: coaching still runs."""

    class _BrokenDB:
        async def execute(self, *a: Any, **k: Any) -> int:
            raise RuntimeError("simulated store outage")

        async def fetch_one(self, *a: Any, **k: Any) -> None:
            raise RuntimeError("simulated store outage")

        async def fetch_all(self, *a: Any, **k: Any) -> list:
            raise RuntimeError("simulated store outage")

        def transaction(self):
            raise RuntimeError("simulated store outage")

    monkeypatch.setattr(coach_bot, "DB", _BrokenDB())
    calls: list[int] = []

    async def handler(update: Any, context: Any) -> None:
        calls.append(1)

    await observed_handler("text", handler)(_update(text="hi"), None)
    assert calls == [1]
    from noam_coach.observability import observability_health

    assert observability_health()["write_failures"] >= 1


@pytest.mark.asyncio
async def test_install_routing_observer_is_idempotent(db: Database) -> None:
    install_routing_observer()
    once = conversation.ConversationRouter.route
    install_routing_observer()  # second install must not double-wrap
    assert conversation.ConversationRouter.route is once
    with interaction_scope():
        await conversation.ConversationRouter.route(db, ALLOWED_USER_ID, "text")
    events = await event_log.list_events(db, ALLOWED_USER_ID, event="routing.decided")
    assert len(events) == 1  # exactly one event per route call


@pytest.mark.asyncio
async def test_uninstall_restores_pure_router(db: Database) -> None:
    pure = conversation.ConversationRouter.route
    install_routing_observer()
    assert conversation.ConversationRouter.route is not pure
    uninstall_routing_observer()
    decision = await conversation.ConversationRouter.route(db, ALLOWED_USER_ID, "text")
    assert decision.handler == "free_text"
    assert await event_log.list_events(db, ALLOWED_USER_ID, event="routing.decided") == []
