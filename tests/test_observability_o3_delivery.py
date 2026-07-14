"""Observability O3 — Telegram render and delivery acceptance tests.

Acceptance criteria covered:
- rendered controls are reconstructable (exact labels + callback data)
- callback correlates to its source render when evidence exists, including
  trace continuation; unresolved correlation stays explicit
- edit success trace is correct
- stale-edit → reply fallback chain is traced exactly
- a reply-fallback failure is visible even though safe_edit suppresses it
- Telegram "message is not modified" is recorded as outcome=not_modified,
  never as a content-changing delivery
- the exact final visible text is recoverable in content mode
- proactive job delivery has attempted/succeeded/failed semantics
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest

import coach_bot
import event_log
from db import Database
from helpers import utc_now
from noam_coach.observability import ObservabilityMode, set_mode
from noam_coach.observability.emit import reset_observability_health
from noam_coach.observability.modes import reset_mode
from noam_coach.observability.obs_context import interaction_scope
from noam_coach.observability.render_registry import find_render_for_message, resolve_control
from noam_coach.observability.telegram_egress import (
    install_telegram_egress,
    uninstall_telegram_egress,
    unobserved_delivery,
)
from noam_coach.observability.telegram_ingress import observed_handler

USER_ID = 1


@pytest.fixture
async def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Database:
    database = Database(str(tmp_path / "obs_o3.db"))
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
    install_telegram_egress()
    yield
    uninstall_telegram_egress()
    reset_mode()
    reset_observability_health()


class FakeMessage:
    def __init__(self, message_id: int = 500, *, fail_reply: bool = False) -> None:
        self.message_id = message_id
        self.chat = SimpleNamespace(id=USER_ID)
        self.fail_reply = fail_reply
        self.replies: list[str] = []
        self._next_id = message_id + 100

    async def reply_text(self, text: str, reply_markup: Any = None, parse_mode: str | None = None) -> Any:
        if self.fail_reply:
            raise RuntimeError("simulated network failure on reply")
        self.replies.append(text)
        self._next_id += 1
        return SimpleNamespace(message_id=self._next_id)


class FakeQuery:
    """CallbackQuery double with configurable edit behavior."""

    def __init__(
        self,
        message_id: int = 500,
        *,
        edit_error: BaseException | None = None,
        fail_reply: bool = False,
    ) -> None:
        self.from_user = SimpleNamespace(id=USER_ID)
        self.message = FakeMessage(message_id, fail_reply=fail_reply)
        self.edit_error = edit_error
        self.edits: list[str] = []

    async def edit_message_text(self, text: str, reply_markup: Any = None, parse_mode: str | None = None) -> None:
        if self.edit_error is not None:
            raise self.edit_error
        self.edits.append(text)

    async def answer(self, *a: Any, **k: Any) -> None:
        return None


_KEYBOARD = InlineKeyboardMarkup(
    [
        [InlineKeyboardButton("אפשרות 1", callback_data="pick:1"),
         InlineKeyboardButton("אפשרות 2", callback_data="pick:2")],
        [InlineKeyboardButton("⬅️ חזרה", callback_data="menu:home")],
    ]
)


def _events_by_name(events: list, name: str) -> list:
    return [e for e in events if e.event == name]


@pytest.mark.asyncio
async def test_edit_success_trace_with_reconstructable_controls(db: Database) -> None:
    query = FakeQuery(message_id=700)
    with interaction_scope(user_id=USER_ID):
        await coach_bot.safe_edit(query, "בחר ארוחה:", _KEYBOARD)

    events = await event_log.list_events(db, USER_ID)
    prepared = _events_by_name(events, "ui.render.prepared")[0]
    attempted = _events_by_name(events, "delivery.attempted")[0]
    succeeded = _events_by_name(events, "delivery.succeeded")[0]

    # Exact final visible text is recoverable in content mode.
    assert prepared.properties["content"]["text"] == "בחר ארוחה:"
    # Exact presented control model.
    controls = prepared.properties["controls"]
    assert [c["label"] for c in controls] == ["אפשרות 1", "אפשרות 2", "⬅️ חזרה"]
    assert [c["callback_data"] for c in controls] == ["pick:1", "pick:2", "menu:home"]
    assert controls[2]["row"] == 1

    render_id = prepared.properties["render_id"]
    assert attempted.properties == {
        "render_id": render_id, "operation": "edit", "channel": "telegram",
        "chat_id": USER_ID, "target_message_id": 700,
    }
    assert succeeded.outcome == "delivered"
    assert succeeded.properties["operation"] == "edit"
    assert succeeded.properties["message_id"] == 700
    assert query.edits == ["בחר ארוחה:"]


@pytest.mark.asyncio
async def test_stale_edit_falls_back_to_reply_with_exact_chain(db: Database) -> None:
    query = FakeQuery(message_id=700, edit_error=BadRequest("Message to edit not found"))
    with interaction_scope(user_id=USER_ID):
        await coach_bot.safe_edit(query, "מסך חדש", _KEYBOARD)

    events = await event_log.list_events(db, USER_ID)
    names = [(e.event, e.properties.get("operation")) for e in events]
    assert names == [
        ("ui.render.prepared", None),
        ("delivery.attempted", "edit"),
        ("delivery.failed", "edit"),
        ("delivery.attempted", "reply"),
        ("delivery.succeeded", "reply"),
    ]
    failed = events[2]
    assert failed.properties["reason"] == "stale_edit"
    assert failed.properties["will_fallback"] is True
    succeeded = events[4]
    assert succeeded.properties["message_id"] == 801  # the NEW reply message
    assert query.message.replies == ["מסך חדש"]
    # One render, one chain: all delivery events share the render_id.
    render_id = events[0].properties["render_id"]
    assert all(e.properties.get("render_id") == render_id for e in events[1:])


@pytest.mark.asyncio
async def test_reply_fallback_failure_is_visible_despite_suppression(db: Database) -> None:
    query = FakeQuery(
        message_id=700,
        edit_error=BadRequest("Message to edit not found"),
        fail_reply=True,
    )
    with interaction_scope(user_id=USER_ID):
        # safe_edit suppresses the reply failure — the user flow survives...
        await coach_bot.safe_edit(query, "מסך חדש", None)

    events = await event_log.list_events(db, USER_ID)
    names = [(e.event, e.properties.get("operation")) for e in events]
    # ...but the trace shows the delivery truly failed end-to-end.
    assert names == [
        ("ui.render.prepared", None),
        ("delivery.attempted", "edit"),
        ("delivery.failed", "edit"),
        ("delivery.attempted", "reply"),
        ("delivery.failed", "reply"),
    ]
    assert events[4].properties["error_type"] == "RuntimeError"


@pytest.mark.asyncio
async def test_not_modified_is_not_reported_as_content_change(db: Database) -> None:
    query = FakeQuery(message_id=700, edit_error=BadRequest("Message is not modified"))
    with interaction_scope(user_id=USER_ID):
        await coach_bot.safe_edit(query, "אותו תוכן", None)

    events = await event_log.list_events(db, USER_ID)
    names = [e.event for e in events]
    assert names == ["ui.render.prepared", "delivery.attempted", "delivery.succeeded"]
    outcome_event = events[2]
    assert outcome_event.outcome == "not_modified"
    # No reply fallback was attempted — the screen already shows the content.
    assert query.message.replies == []


@pytest.mark.asyncio
async def test_callback_resolves_source_render_and_continues_trace(db: Database, monkeypatch) -> None:
    # 1) Deliver a render (a fresh reply → message 601 carries the keyboard).
    query = FakeQuery(message_id=500, edit_error=BadRequest("Message to edit not found"))
    with interaction_scope(user_id=USER_ID) as first:
        await coach_bot.safe_edit(query, "בחר:", _KEYBOARD)
    delivered = _events_by_name(await event_log.list_events(db, USER_ID), "delivery.succeeded")[0]
    delivered_message_id = delivered.properties["message_id"]

    render = await find_render_for_message(db, USER_ID, delivered_message_id)
    assert render is not None
    assert render.trace_id == first.trace_id

    resolved = await resolve_control(db, USER_ID, delivered_message_id, "pick:2")
    assert resolved is not None
    assert resolved.label == "אפשרות 2"
    assert resolved.render_id == render.render_id

    # 2) The user presses a button on that delivered render.
    async def handler(update: Any, context: Any) -> None:
        return None

    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=USER_ID, first_name="T", username=None),
        effective_chat=SimpleNamespace(id=USER_ID),
        effective_message=SimpleNamespace(
            message_id=delivered_message_id, text=None, caption=None, photo=[], document=None,
        ),
        callback_query=SimpleNamespace(
            data="pick:2",
            message=SimpleNamespace(message_id=delivered_message_id),
        ),
    )
    await observed_handler("callback", handler)(update, None)

    activated = _events_by_name(await event_log.list_events(db, USER_ID), "ui.control.activated")[0]
    assert activated.properties["correlation"] == "resolved"
    assert activated.properties["source_render_id"] == render.render_id
    assert activated.properties["label"] == "אפשרות 2"
    # Trace continuation: the callback interaction joined the render's trace.
    assert activated.trace_id == first.trace_id
    received = _events_by_name(await event_log.list_events(db, USER_ID), "interaction.received")[0]
    assert received.trace_id == first.trace_id
    assert received.interaction_id != None  # noqa: E711
    assert received.interaction_id != delivered.interaction_id


@pytest.mark.asyncio
async def test_callback_with_no_delivered_render_stays_unresolved(db: Database) -> None:
    async def handler(update: Any, context: Any) -> None:
        return None

    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=USER_ID, first_name="T", username=None),
        effective_chat=SimpleNamespace(id=USER_ID),
        effective_message=SimpleNamespace(message_id=42, text=None, caption=None, photo=[], document=None),
        callback_query=SimpleNamespace(data="menu:today", message=SimpleNamespace(message_id=42)),
    )
    await observed_handler("callback", handler)(update, None)

    activated = _events_by_name(await event_log.list_events(db, USER_ID), "ui.control.activated")[0]
    assert activated.properties["correlation"] == "unresolved"
    assert activated.properties["source_render_id"] is None


@pytest.mark.asyncio
async def test_metadata_mode_keeps_digest_but_not_visible_text(db: Database) -> None:
    set_mode(ObservabilityMode.METADATA)
    query = FakeQuery(message_id=700)
    with interaction_scope(user_id=USER_ID):
        await coach_bot.safe_edit(query, "טקסט סודי למדי", None)
    prepared = _events_by_name(await event_log.list_events(db, USER_ID), "ui.render.prepared")[0]
    assert "content" not in prepared.properties
    assert prepared.properties["content_digest"]["text"]["chars"] == len("טקסט סודי למדי")


@pytest.mark.asyncio
async def test_free_text_context_send_is_observed(db: Database) -> None:
    from noam_coach.bot import assistant as bot_assistant

    message = FakeMessage(message_id=300)
    ctx = bot_assistant.FreeTextContext(
        update=None, user_id=USER_ID, message=message, text="מה המצב",
        intent=None, action="status", slots={},
        follow=_KEYBOARD,
    )
    with interaction_scope(user_id=USER_ID):
        await ctx.send("הנה הסטטוס שלך")

    events = await event_log.list_events(db, USER_ID)
    prepared = _events_by_name(events, "ui.render.prepared")[0]
    assert prepared.properties["content"]["text"] == "הנה הסטטוס שלך"
    assert prepared.properties["intended_operation"] == "reply"
    assert [c["callback_data"] for c in prepared.properties["controls"]] == [
        "pick:1", "pick:2", "menu:home",
    ]
    succeeded = _events_by_name(events, "delivery.succeeded")[0]
    assert succeeded.properties["operation"] == "reply"
    assert message.replies == ["הנה הסטטוס שלך"]


@pytest.mark.asyncio
async def test_proactive_job_delivery_semantics(db: Database, monkeypatch) -> None:
    from noam_coach.jobs import proactive as proactive_module

    monkeypatch.setattr(proactive_module, "DB", db, raising=False)

    class _Claim:
        attempt_count = 1

    async def ok_claim(*a: Any, **k: Any) -> Any:
        return _Claim()

    async def noop(*a: Any, **k: Any) -> Any:
        return None

    async def allowed(*a: Any, **k: Any) -> tuple[bool, str]:
        return True, ""

    # deliver_proactive_message is @runtime_bound: its globals re-sync from
    # the coach_bot facade on every call, so the facade is the patch point.
    monkeypatch.setattr(coach_bot, "claim_job_delivery", ok_claim, raising=False)
    monkeypatch.setattr(coach_bot, "complete_job_delivery", noop, raising=False)
    monkeypatch.setattr(coach_bot.data_quality, "can_send_proactive", allowed, raising=False)

    sends: list[str] = []

    async def sender() -> None:
        sends.append("sent")

    context = SimpleNamespace(bot=None)
    result = await proactive_module.deliver_proactive_message(
        context, key="workout_prompt", sender=sender, priority=1,
    )
    assert result is True
    assert sends == ["sent"]
    events = await event_log.list_events(db, USER_ID)
    ops = [(e.event, e.properties.get("operation"), e.properties.get("key")) for e in events]
    assert ("delivery.attempted", "proactive_job", "workout_prompt") in ops
    assert ("delivery.succeeded", "proactive_job", "workout_prompt") in ops
    # Job-level events share one trace (message-level events would join it).
    assert len({e.trace_id for e in events}) == 1


@pytest.mark.asyncio
async def test_unobserved_delivery_scope_suppresses_transport_probe(db: Database) -> None:
    """The rest-timer tick exemption: nothing is emitted inside the scope."""
    from noam_coach.observability.telegram_egress import _transport_probe_suppressed

    with unobserved_delivery():
        assert _transport_probe_suppressed.get() is True
    assert _transport_probe_suppressed.get() is False


@pytest.mark.asyncio
async def test_uninstall_restores_safe_edit(db: Database) -> None:
    uninstall_telegram_egress()
    query = FakeQuery(message_id=700)
    with interaction_scope(user_id=USER_ID):
        await coach_bot.safe_edit(query, "ללא תצפית", None)
    assert query.edits == ["ללא תצפית"]
    assert await event_log.list_events(db, USER_ID) == []
    install_telegram_egress()  # leave installed for the autouse teardown
