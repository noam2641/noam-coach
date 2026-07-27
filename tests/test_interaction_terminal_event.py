"""W1-12 — an interaction must terminate.

Before this batch every interaction emitted ``interaction.received`` and
NOTHING closed it. The consequence was not academic: a ``menu:goals`` button
emitted by the bot's own top-priority CTA was handled by nothing, and in the
event stream that was indistinguishable from a working button, because
``routing.decided`` is written BEFORE dispatch and no event recorded the
outcome. These tests pin the three facts that make the difference:

1. a normal interaction terminates with a ``handled`` outcome and a duration
2. an interaction nothing handled terminates with a DIFFERENT outcome
3. an exception inside the scope still terminates the interaction, and the
   exception is not swallowed
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import coach_bot
import event_log
from db import Database
from helpers import utc_now
from noam_coach.observability import ObservabilityMode, set_mode, trace_reader
from noam_coach.observability.emit import emit_event, reset_observability_health
from noam_coach.observability.interaction_lifecycle import (
    OUTCOME_CANCELLED,
    OUTCOME_ERROR,
    OUTCOME_HANDLED,
    OUTCOME_NO_HANDLER,
    classify_outcome,
    observed_interaction,
)
from noam_coach.observability.modes import reset_mode
from noam_coach.observability.taxonomy import INTERACTION_COMPLETED, INTERACTION_RECEIVED
from noam_coach.observability.telegram_ingress import observed_handler

ALLOWED_USER_ID = 1


@pytest.fixture
async def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Database:
    """A throwaway DB under tmp_path — never the default ./noam_coach.db."""
    database = Database(str(tmp_path / "obs_w1_12.db"))
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
    reset_mode()
    reset_observability_health()


def _update(*, text: str | None = None, callback_data: str | None = None) -> SimpleNamespace:
    message = SimpleNamespace(message_id=111, text=text, caption=None, photo=[], document=None)
    callback_query = (
        SimpleNamespace(data=callback_data, message=message) if callback_data is not None else None
    )
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=ALLOWED_USER_ID, first_name="Test", username=None),
        effective_chat=SimpleNamespace(id=555),
        effective_message=message,
        callback_query=callback_query,
    )


async def _terminal_events(db: Database) -> list[Any]:
    events = await event_log.list_events(db, ALLOWED_USER_ID, limit=200)
    return [e for e in events if e.event == INTERACTION_COMPLETED]


async def _render_something(db: Database) -> None:
    """Emit the response evidence a real handler produces when it answers."""
    await emit_event(
        db,
        ALLOWED_USER_ID,
        "ui.render.prepared",
        entity="render",
        properties={"render_id": "r1"},
    )


# ---------------------------------------------------------------------------
# 1. a normal interaction terminates with a handled outcome
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handled_interaction_emits_terminal_event(db: Database) -> None:
    async def handler(update: Any, context: Any) -> None:
        await _render_something(db)

    await observed_handler("text", handler)(_update(text="hi"), None)

    terminals = await _terminal_events(db)
    assert len(terminals) == 1, "exactly one terminal event per interaction"
    terminal = terminals[0]
    assert terminal.outcome == OUTCOME_HANDLED
    assert terminal.properties["outcome"] == OUTCOME_HANDLED
    assert terminal.properties["responses"] >= 1


@pytest.mark.asyncio
async def test_terminal_event_carries_duration_in_properties(db: Database) -> None:
    """Duration lives in properties (metadata), so it survives METADATA mode
    instead of being reduced to a content digest."""

    async def handler(update: Any, context: Any) -> None:
        await _render_something(db)

    await observed_handler("text", handler)(_update(text="hi"), None)

    terminal = (await _terminal_events(db))[0]
    assert "duration_ms" in terminal.properties
    assert isinstance(terminal.properties["duration_ms"], int)
    assert terminal.properties["duration_ms"] >= 0
    # Not a digest: METADATA-mode readers must still see latency.
    assert "duration_ms" not in (terminal.properties.get("content_digest") or {})


@pytest.mark.asyncio
async def test_terminal_event_shares_the_interaction_correlation(db: Database) -> None:
    """The terminal event closes the SAME interaction it terminates — no
    second correlation mechanism was introduced."""
    seen: dict[str, Any] = {}

    async def handler(update: Any, context: Any) -> None:
        from noam_coach.observability import current_interaction_id

        seen["interaction_id"] = current_interaction_id()
        await _render_something(db)

    await observed_handler("text", handler)(_update(text="hi"), None)

    events = await trace_reader.events_for_interaction(db, ALLOWED_USER_ID, seen["interaction_id"])
    names = [e.event for e in events]
    assert names[0] == INTERACTION_RECEIVED
    assert names[-1] == INTERACTION_COMPLETED, "the terminal event closes the interaction"


# ---------------------------------------------------------------------------
# 2. an interaction nothing handled is DISTINGUISHABLE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unhandled_interaction_is_distinguishable(db: Database) -> None:
    """The menu:goals defect: routing chose a handler, dispatch produced
    nothing, and the old stream looked identical to success."""

    async def handler(update: Any, context: Any) -> None:
        return None  # nothing rendered, nothing sent — a silent drop

    await observed_handler("callback", handler)(_update(callback_data="menu:goals"), None)

    terminal = (await _terminal_events(db))[0]
    assert terminal.outcome == OUTCOME_NO_HANDLER
    assert terminal.properties["responses"] == 0
    assert terminal.outcome != OUTCOME_HANDLED


@pytest.mark.asyncio
async def test_handled_and_unhandled_differ_in_the_stream(db: Database) -> None:
    """The two cases must not collapse to the same recorded outcome."""

    async def answering(update: Any, context: Any) -> None:
        await _render_something(db)

    async def silent(update: Any, context: Any) -> None:
        return None

    await observed_handler("callback", answering)(_update(callback_data="menu:ok"), None)
    await observed_handler("callback", silent)(_update(callback_data="menu:goals"), None)

    outcomes = [e.outcome for e in await _terminal_events(db)]
    assert len(outcomes) == 2
    assert set(outcomes) == {OUTCOME_HANDLED, OUTCOME_NO_HANDLER}


# ---------------------------------------------------------------------------
# 3. an exception still terminates the interaction, and still propagates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exception_terminates_interaction_and_propagates(db: Database) -> None:
    class Boom(RuntimeError):
        pass

    async def handler(update: Any, context: Any) -> None:
        raise Boom("handler exploded")

    with pytest.raises(Boom, match="handler exploded"):
        await observed_handler("text", handler)(_update(text="hi"), None)

    terminals = await _terminal_events(db)
    assert len(terminals) == 1, "a crashing interaction is still terminated"
    terminal = terminals[0]
    assert terminal.outcome == OUTCOME_ERROR
    assert terminal.properties["error_type"] == "Boom"
    assert terminal.status == "failed"


@pytest.mark.asyncio
async def test_terminal_emission_failure_never_breaks_the_interaction(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Containment: if writing the terminal event fails, the user's flow and
    the original exception are unaffected."""
    import noam_coach.observability.interaction_lifecycle as lifecycle

    async def exploding_emit(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("event store down")

    monkeypatch.setattr(lifecycle, "emit_event", exploding_emit)

    async with observed_interaction(db, ALLOWED_USER_ID):
        pass  # must not raise

    class Boom(RuntimeError):
        pass

    with pytest.raises(Boom):
        async with observed_interaction(db, ALLOWED_USER_ID):
            raise Boom("original defect must survive")


@pytest.mark.asyncio
async def test_cancellation_is_not_reported_as_an_error(db: Database) -> None:
    """Shutdown noise must not look like a crash."""
    import asyncio

    with pytest.raises(asyncio.CancelledError):
        async with observed_interaction(db, ALLOWED_USER_ID):
            raise asyncio.CancelledError()

    terminal = (await _terminal_events(db))[0]
    assert terminal.outcome == OUTCOME_CANCELLED


# ---------------------------------------------------------------------------
# outcome classification (pure)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "responses", "expected"),
    [
        (None, 1, OUTCOME_HANDLED),
        (None, 0, OUTCOME_NO_HANDLER),
        (RuntimeError("x"), 1, OUTCOME_ERROR),
        (RuntimeError("x"), 0, OUTCOME_ERROR),
        (KeyboardInterrupt(), 0, OUTCOME_CANCELLED),
    ],
)
def test_classify_outcome(exc: BaseException | None, responses: int, expected: str) -> None:
    assert classify_outcome(exc, responses) == expected
