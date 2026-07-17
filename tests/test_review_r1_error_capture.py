"""Review batch R1 — unhandled exceptions become canonical trace evidence.

A crash at a real execution boundary must (a) emit exactly one
``error.captured`` event with preserved correlation, (b) re-raise so
production error handling is unchanged, (c) never misclassify cancellation
or expected transient conditions, and (d) never export sensitive text.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

import coach_bot
import mini_api
from db import Database
from helpers import utc_now
from noam_coach.observability import ObservabilityMode, set_mode
from noam_coach.observability.error_capture import (
    already_captured,
    capture_unhandled,
    mark_captured,
)
from noam_coach.observability.harness import run_failing_user_turn, run_user_turn
from noam_coach.observability.modes import reset_mode
from noam_coach.observability.taxonomy import ERROR_CAPTURED
from noam_coach.observability.telegram_ingress import observed_error_callback

USER_ID = 1


@pytest.fixture
async def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Database:
    database = Database(str(tmp_path / "r1.db"))
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
def _content_mode():
    set_mode(ObservabilityMode.CONTENT)
    yield
    reset_mode()


def _error_events(trace: Any) -> list[Any]:
    return [e for e in trace.events if e.event == ERROR_CAPTURED]


async def _all_error_events(db: Database) -> list[Any]:
    import event_log

    return await event_log.list_events(db, USER_ID, event=ERROR_CAPTURED)


# ---------------------------------------------------------------------------
# Telegram handler boundary
# ---------------------------------------------------------------------------


async def test_handler_crash_is_captured_correlated_and_reraised(db: Database) -> None:
    async def crashing(update: Any, context: Any) -> None:
        raise ValueError("boom during meal handling")

    exc, trace = await run_failing_user_turn(db, USER_ID, crashing, text="מה לאכול?")
    assert isinstance(exc, ValueError)  # original exception, re-raised

    events = _error_events(trace)
    assert len(events) == 1
    event = events[0]
    assert event.properties["boundary"] == "telegram_handler:text"
    assert event.properties["error_type"] == "ValueError"
    assert "boom during meal handling" in event.properties["message"]
    assert event.outcome == "unhandled_exception"
    # Correlation preserved: same interaction/trace as the ingress event.
    assert event.interaction_id == trace.interaction_id
    assert event.trace_id == trace.trace_id
    assert trace.received is not None
    assert event.trace_id == trace.received.trace_id


async def test_no_duplicate_when_dispatch_net_sees_same_exception(db: Database) -> None:
    """The exception object is marked at the inner boundary; the wrapped
    on_error safety net must not record it again."""

    async def crashing(update: Any, context: Any) -> None:
        raise RuntimeError("crash once")

    exc, _trace = await run_failing_user_turn(db, USER_ID, crashing, text="hi")

    calls: list[str] = []

    async def on_error(update: Any, context: Any) -> None:
        calls.append("ran")

    wrapped = observed_error_callback(on_error)
    await wrapped(None, SimpleNamespace(error=exc))
    assert calls == ["ran"]  # original error handler unchanged
    assert len(await _all_error_events(db)) == 1


async def test_successful_handler_records_no_error(db: Database) -> None:
    async def fine(update: Any, context: Any) -> None:
        return None

    trace = await run_user_turn(db, USER_ID, fine, text="שלום")
    assert _error_events(trace) == []


async def test_handler_that_recovers_internally_records_nothing(db: Database) -> None:
    """A boundary that intentionally owns recovery is not a crash."""

    async def recovering(update: Any, context: Any) -> None:
        try:
            raise KeyError("recoverable")
        except KeyError:
            return None

    trace = await run_user_turn(db, USER_ID, recovering, text="שלום")
    assert _error_events(trace) == []


async def test_cancellation_propagates_and_is_not_recorded(db: Database) -> None:
    async def cancelled(update: Any, context: Any) -> None:
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await run_user_turn(db, USER_ID, cancelled, text="hi")
    assert await _all_error_events(db) == []


async def test_sensitive_text_is_sanitized(db: Database) -> None:
    async def leaking(update: Any, context: Any) -> None:
        raise RuntimeError("call failed: Bearer abcdef1234567890 rejected")

    exc, trace = await run_failing_user_turn(db, USER_ID, leaking, text="hi")
    message = _error_events(trace)[0].properties["message"]
    assert "abcdef1234567890" not in message
    assert "Bearer <redacted>" in message


async def test_message_is_bounded_and_has_no_traceback(db: Database) -> None:
    async def long_crash(update: Any, context: Any) -> None:
        raise RuntimeError("x" * 5000)

    exc, trace = await run_failing_user_turn(db, USER_ID, long_crash, text="hi")
    props = _error_events(trace)[0].properties
    assert len(props["message"]) <= 300
    assert "Traceback" not in str(props)
    assert "File \"" not in str(props)


# ---------------------------------------------------------------------------
# Dispatch/error boundary (jobs and other PTB-routed failures)
# ---------------------------------------------------------------------------


async def test_dispatch_net_captures_job_style_failure(db: Database) -> None:
    """A JobQueue callback failure reaches PTB error handling with no
    update; the net records it against the allowed user, uncorrelated."""

    async def on_error(update: Any, context: Any) -> None:
        return None

    wrapped = observed_error_callback(on_error)
    await wrapped(None, SimpleNamespace(error=RuntimeError("job exploded")))

    events = await _all_error_events(db)
    assert len(events) == 1
    assert events[0].properties["boundary"] == "telegram_dispatch"
    assert events[0].properties["has_update"] is False
    assert events[0].is_legacy_uncorrelated  # explicitly outside any scope


async def test_dispatch_net_skips_transient_telegram_errors(db: Database) -> None:
    from telegram.error import NetworkError, TimedOut

    async def on_error(update: Any, context: Any) -> None:
        return None

    wrapped = observed_error_callback(on_error)
    await wrapped(None, SimpleNamespace(error=NetworkError("flaky link")))
    await wrapped(None, SimpleNamespace(error=TimedOut()))
    assert await _all_error_events(db) == []


async def test_dispatch_net_tolerates_missing_error(db: Database) -> None:
    async def on_error(update: Any, context: Any) -> None:
        return None

    wrapped = observed_error_callback(on_error)
    await wrapped(None, SimpleNamespace(error=None))
    assert await _all_error_events(db) == []


# ---------------------------------------------------------------------------
# Mini App / API boundary
# ---------------------------------------------------------------------------


async def _drive_mini_scope(exc: BaseException) -> BaseException | None:
    """Open mini_obs_scope, throw the endpoint exception back in at the
    yield point (exactly what FastAPI does), and return what re-raised."""
    agen = mini_api.mini_obs_scope(user_id=USER_ID, x_obs_client_interaction=None)
    assert await agen.__anext__() == USER_ID
    try:
        await agen.athrow(exc)
    except StopAsyncIteration:
        return None
    except BaseException as raised:  # noqa: BLE001 — asserting propagation.
        return raised
    return None


async def test_mini_api_crash_captured_and_reraised(db: Database) -> None:
    raised = await _drive_mini_scope(RuntimeError("endpoint exploded"))
    assert isinstance(raised, RuntimeError)
    events = await _all_error_events(db)
    assert len(events) == 1
    assert events[0].properties["boundary"] == "mini_api"
    assert events[0].surface == "mini_app"
    # Correlated: the scope was open when the capture ran.
    assert events[0].interaction_id is not None


async def test_mini_api_http_exception_is_not_a_crash(db: Database) -> None:
    raised = await _drive_mini_scope(HTTPException(status_code=422, detail="bad input"))
    assert isinstance(raised, HTTPException)
    assert await _all_error_events(db) == []


# ---------------------------------------------------------------------------
# Capture mechanics
# ---------------------------------------------------------------------------


async def test_capture_unhandled_marks_and_deduplicates(db: Database) -> None:
    exc = ValueError("once")
    assert not already_captured(exc)
    assert await capture_unhandled(db, USER_ID, exc, boundary="unit", source="test")
    assert already_captured(exc)
    assert not await capture_unhandled(db, USER_ID, exc, boundary="unit", source="test")
    assert len(await _all_error_events(db)) == 1


async def test_capture_never_raises_on_broken_db() -> None:
    exc = ValueError("db is down")

    class BrokenDB:
        async def execute(self, *args: Any, **kwargs: Any) -> None:
            raise OSError("cannot write")

    assert await capture_unhandled(BrokenDB(), USER_ID, exc, boundary="unit", source="test") is False


def test_mark_captured_tolerates_slotted_exceptions() -> None:
    class Slotted(Exception):
        __slots__ = ()

    exc = Slotted()
    mark_captured(exc)  # must not raise even if the mark cannot stick
