from __future__ import annotations

from typing import Any

import pytest
from telegram.error import BadRequest, TimedOut

import coach_bot
from config import RUNTIME_STATE
from noam_coach.app import runtime
from noam_coach.bot import callback_router
from noam_coach.bot.ui import safe_answer_callback, safe_edit
from noam_coach.services import telegram_errors


class FakeLogger:
    def __init__(self) -> None:
        self.debugs: list[str] = []
        self.warnings: list[str] = []
        self.errors: list[str] = []

    def debug(self, message: str, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        self.debugs.append(message)

    def warning(self, message: str, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        self.warnings.append(message)

    def error(self, message: str, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        self.errors.append(message)


class FakeContext:
    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.bot = object()


class FakeMessage:
    def __init__(self) -> None:
        self.replies: list[str] = []

    async def reply_text(self, text: str) -> None:
        self.replies.append(text)


@pytest.mark.asyncio
async def test_transient_telegram_error_is_not_admin_spam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logger = FakeLogger()
    admin_messages: list[str] = []

    async def notify_admin(_bot: object, text: str) -> None:
        admin_messages.append(text)

    monkeypatch.setattr(coach_bot, "LOGGER", logger)
    monkeypatch.setattr(coach_bot, "notify_admin", notify_admin)
    RUNTIME_STATE.shutting_down = False

    await callback_router.on_error(object(), FakeContext(TimedOut("temporary read timeout")))  # type: ignore[arg-type]

    assert logger.warnings
    assert not logger.errors
    assert admin_messages == []


@pytest.mark.asyncio
async def test_user_error_message_does_not_expose_error_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeUser:
        id = 1

    class FakeUpdate:
        def __init__(self) -> None:
            self.effective_message = FakeMessage()
            self.effective_user = FakeUser()
            self.callback_query = None

    update = FakeUpdate()

    async def notify_admin(_bot: object, _text: str) -> None:
        return None

    monkeypatch.setattr(coach_bot, "Update", FakeUpdate)
    monkeypatch.setattr(coach_bot, "LOGGER", FakeLogger())
    monkeypatch.setattr(coach_bot, "notify_admin", notify_admin)

    await callback_router.on_error(update, FakeContext(RuntimeError("boom")))  # type: ignore[arg-type]

    assert update.effective_message.replies
    reply = update.effective_message.replies[0]
    assert "קוד תקלה" not in reply
    assert "error" not in reply.lower()


def test_admin_notifications_are_deduplicated_by_fingerprint() -> None:
    telegram_errors._LAST_ADMIN_NOTICE_AT.clear()
    exc = RuntimeError("boom")

    first = telegram_errors.classify_telegram_error(exc)
    second = telegram_errors.classify_telegram_error(exc)

    assert first.notify_admin is True
    assert second.notify_admin is False


def test_sensitive_values_are_redacted_from_telegram_error_text() -> None:
    raw = (
        "failed Bearer abcdefghijklmnop token=plainsecret "
        "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ "
        "C:\\Users\\noam\\Downloads\\export.zip"
    )

    redacted = telegram_errors.redact_sensitive_text(raw)

    assert "abcdefghijklmnop" not in redacted
    assert "plainsecret" not in redacted
    assert "ABCDEFGHIJKLMNOPQRSTUVWXYZ" not in redacted
    assert "noam" not in redacted
    assert "<redacted" in redacted


@pytest.mark.asyncio
async def test_safe_answer_callback_ignores_stale_callback() -> None:
    class StaleQuery:
        async def answer(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs
            raise BadRequest("Query is too old and response timeout expired or query id is invalid")

    assert await safe_answer_callback(StaleQuery()) is False


@pytest.mark.asyncio
async def test_safe_answer_callback_reraises_non_stale_bad_request() -> None:
    class BrokenQuery:
        async def answer(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs
            raise BadRequest("chat not found")

    with pytest.raises(BadRequest):
        await safe_answer_callback(BrokenQuery())


@pytest.mark.asyncio
async def test_safe_edit_replies_when_original_message_cannot_be_edited() -> None:
    class ReplyMessage:
        def __init__(self) -> None:
            self.replies: list[dict[str, Any]] = []

        async def reply_text(self, text: str, **kwargs: Any) -> None:
            self.replies.append({"text": text, **kwargs})

    class StaleEditQuery:
        def __init__(self) -> None:
            self.message = ReplyMessage()

        async def edit_message_text(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs
            raise BadRequest("Message to edit not found")

    query = StaleEditQuery()

    await safe_edit(query, "fresh screen", None)

    assert query.message.replies
    assert query.message.replies[0]["text"] == "fresh screen"


@pytest.mark.asyncio
async def test_safe_edit_does_not_duplicate_on_not_modified() -> None:
    """A same-content edit (e.g. double-tap on the same button) must be
    silently ignored — falling back to reply_text here would send the user
    a duplicate message, which is exactly the bug users reported."""

    class ReplyMessage:
        def __init__(self) -> None:
            self.replies: list[dict[str, Any]] = []

        async def reply_text(self, text: str, **kwargs: Any) -> None:
            self.replies.append({"text": text, **kwargs})

    class NotModifiedQuery:
        def __init__(self) -> None:
            self.message = ReplyMessage()

        async def edit_message_text(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs
            raise BadRequest(
                "Message is not modified: specified new message content and "
                "reply markup are exactly the same"
            )

    query = NotModifiedQuery()

    await safe_edit(query, "same screen", None)

    assert query.message.replies == []  # no duplicate message


def test_stale_callback_error_is_transient_without_user_notification() -> None:
    class FakeUpdate:
        effective_message = object()

    decision = telegram_errors.classify_telegram_error(
        BadRequest("Query is too old and response timeout expired or query id is invalid"),
        update=FakeUpdate(),
    )

    assert decision.transient is True
    assert decision.notify_user is False
    assert decision.notify_admin is False


@pytest.mark.asyncio
async def test_unexpected_telegram_admin_alert_is_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    telegram_errors._LAST_ADMIN_NOTICE_AT.clear()
    admin_messages: list[str] = []

    async def notify_admin(_bot: object, text: str) -> None:
        admin_messages.append(text)

    monkeypatch.setattr(coach_bot, "LOGGER", FakeLogger())
    monkeypatch.setattr(coach_bot, "notify_admin", notify_admin)
    RUNTIME_STATE.shutting_down = False

    await callback_router.on_error(
        object(),
        FakeContext(
            RuntimeError(
                "token=plainsecret 123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ "
                "C:\\Users\\noam\\Downloads\\export.zip"
            )
        ),  # type: ignore[arg-type]
    )

    assert admin_messages
    message = admin_messages[0]
    assert "plainsecret" not in message
    assert "ABCDEFGHIJKLMNOPQRSTUVWXYZ" not in message
    assert "noam" not in message
    assert "<redacted" in message


@pytest.mark.asyncio
async def test_stop_telegram_application_stops_updater_before_application() -> None:
    calls: list[str] = []

    class FakeUpdater:
        running = True

        async def stop(self) -> None:
            calls.append("updater")
            self.running = False

    class FakeApplication:
        running = True
        updater = FakeUpdater()

        async def stop(self) -> None:
            calls.append("application")
            self.running = False

    await runtime._stop_telegram_application(FakeApplication())  # type: ignore[arg-type]

    assert calls == ["updater", "application"]


# ---------------------------------------------------------------------------
# M-NEW-3: end-to-end runtime.run lifecycle under failure injection
# ---------------------------------------------------------------------------

import asyncio  # noqa: E402


class _FakeUpdater:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls
        self.running = False
        self.polling_started = False

    async def start_polling(self, drop_pending_updates: bool = False) -> None:
        del drop_pending_updates
        self.running = True
        self.polling_started = True
        self._calls.append("updater.start_polling")

    async def stop(self) -> None:
        self.running = False
        self._calls.append("updater.stop")


class _FakeApplication:
    """Fake PTB Application supporting the async-context-manager lifecycle."""

    def __init__(self, calls: list[str]) -> None:
        self._calls = calls
        self.running = False
        self.updater = _FakeUpdater(calls)
        self.bot = object()
        self.scheduler_running = False

    async def __aenter__(self) -> "_FakeApplication":
        self._calls.append("app.initialize")
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self._calls.append("app.shutdown")

    async def start(self) -> None:
        self.running = True
        self.scheduler_running = True
        self._calls.append("app.start")

    async def stop(self) -> None:
        self.running = False
        self.scheduler_running = False
        self._calls.append("app.stop")


class _FakeServer:
    def __init__(self, *, serve_error: BaseException | None, calls: list[str]) -> None:
        self._serve_error = serve_error
        self._calls = calls
        self.should_exit = False
        self.started = False

    async def serve(self) -> None:
        self.started = True
        self._calls.append("server.serve")
        await asyncio.sleep(0)
        if self._serve_error is not None:
            raise self._serve_error


def _install_fake_runtime(
    monkeypatch: pytest.MonkeyPatch,
    *,
    serve_error: BaseException | None,
    calls: list[str],
    poll_error: BaseException | None = None,
) -> tuple[_FakeApplication, dict[str, bool]]:
    """Patch runtime boundaries on the coach_bot facade.

    runtime.run is wrapped by runtime_bound, which refreshes its globals from
    the coach_bot facade just before the call. So the patches MUST land on
    coach_bot, not on the runtime module, or they would be overwritten.
    """
    app = _FakeApplication(calls)
    if poll_error is not None:
        async def failing_poll(drop_pending_updates: bool = False) -> None:
            del drop_pending_updates
            calls.append("updater.start_polling")
            raise poll_error
        monkeypatch.setattr(app.updater, "start_polling", failing_poll)

    class _FakeUvicorn:
        Server = staticmethod(lambda config: _FakeServer(serve_error=serve_error, calls=calls))
        Config = staticmethod(lambda *a, **k: object())

    state: dict[str, Any] = {"cleaner_cancelled": False, "task": None}

    async def fake_cleanup_photos() -> None:
        # A genuine long-lived background task that must be cancelled cleanly.
        state["task"] = asyncio.current_task()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            calls.append("cleaner.cancelled")
            state["cleaner_cancelled"] = True
            raise

    async def noop(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(coach_bot, "uvicorn", _FakeUvicorn)
    monkeypatch.setattr(coach_bot, "build_telegram_app", lambda: app)
    monkeypatch.setattr(coach_bot, "verify_bot_identity", noop)
    # SETTINGS is a pydantic model; patch validate_runtime on the class.
    monkeypatch.setattr(type(coach_bot.SETTINGS), "validate_runtime", lambda self: None, raising=False)
    monkeypatch.setattr(coach_bot.DB, "init", noop, raising=False)
    monkeypatch.setattr(coach_bot, "ensure_user_record", noop)
    monkeypatch.setattr(coach_bot, "load_pending_state", noop)
    monkeypatch.setattr(coach_bot, "reconcile_onboarding_stage", noop)
    monkeypatch.setattr(coach_bot, "cleanup_photos", fake_cleanup_photos)
    return app, state


@pytest.mark.asyncio
async def test_run_shuts_down_cleanly_when_serve_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Injecting an error in server.serve() must still stop the updater, the
    application, and cancel background tasks — in order, with no orphan task and
    no 'Task exception was never retrieved'."""
    calls: list[str] = []
    app, state = _install_fake_runtime(
        monkeypatch, serve_error=RuntimeError("serve boom"), calls=calls
    )

    with pytest.raises(RuntimeError, match="serve boom"):
        await runtime.run()

    # Ordered teardown: polling stopped, app stopped, app shutdown, cleaner cancelled.
    assert "updater.start_polling" in calls
    assert calls.index("updater.stop") < calls.index("app.stop")
    assert calls.index("app.stop") < calls.index("app.shutdown")
    assert state["cleaner_cancelled"] is True
    leftover = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    assert leftover == []
    assert RUNTIME_STATE.telegram_ready is False
    assert RUNTIME_STATE.db_ready is False


@pytest.mark.asyncio
async def test_run_shuts_down_cleanly_when_polling_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An error while starting polling must not skip cleanup."""
    calls: list[str] = []
    app, state = _install_fake_runtime(
        monkeypatch,
        serve_error=None,
        calls=calls,
        poll_error=TimedOut("polling failed to start"),
    )

    with pytest.raises(TimedOut):
        await runtime.run()

    # serve() was never reached, but the app/updater stop + shutdown still ran.
    assert "server.serve" not in calls
    assert "app.stop" in calls
    assert "app.shutdown" in calls
    # No orphan background task survives the failed startup (run() awaited the
    # cleaner's cancellation, so nothing other than this test is still pending).
    leftover = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    assert leftover == []


def test_runtime_strings_have_no_mojibake() -> None:
    """L-NEW-1: source files must not contain known broken-encoding sequences."""
    import io
    from pathlib import Path

    # The classic gibberish prefix produced by a cp1255<->utf8 round-trip.
    bad_markers = ("׳", "Ã—", "Â", "â€")
    roots = [
        Path(runtime.__file__),
        Path(__file__).resolve().parents[1] / "config.py",
    ]
    for path in roots:
        with io.open(path, "r", encoding="utf-8") as handle:
            content = handle.read()
        for marker in bad_markers:
            assert marker not in content, f"mojibake {marker!r} found in {path.name}"


@pytest.mark.asyncio
async def test_stop_telegram_application_is_idempotent() -> None:
    """Calling the stop helper twice is safe (already-stopped app is a no-op)."""
    calls: list[str] = []

    class FakeUpdater:
        def __init__(self) -> None:
            self.running = True

        async def stop(self) -> None:
            calls.append("updater")
            self.running = False

    class FakeApplication:
        def __init__(self) -> None:
            self.running = True
            self.updater = FakeUpdater()

        async def stop(self) -> None:
            calls.append("application")
            self.running = False

    app = FakeApplication()
    await runtime._stop_telegram_application(app)  # type: ignore[arg-type]
    await runtime._stop_telegram_application(app)  # type: ignore[arg-type]

    # Second call must be a no-op because running flags are now False.
    assert calls == ["updater", "application"]


def test_sensitive_flow_callbacks_are_debounced() -> None:
    """Codex audit: single-shot flow actions must not run twice on a
    double-tap — e.g. two quick health:skip_item taps would silently skip
    TWO wizard items. Navigation stays debounce-free."""
    from noam_coach.bot import callback_router

    uid = 987_654  # unique user id so _LAST_CALLBACK state is fresh
    assert callback_router._is_duplicate_tap(uid, "health:skip_item") is False
    assert callback_router._is_duplicate_tap(uid, "health:skip_item") is True
    assert callback_router._is_duplicate_tap(uid, "health:confirm:weight_kg") is False
    assert callback_router._is_duplicate_tap(uid, "health:confirm:weight_kg") is True
    assert callback_router._is_duplicate_tap(uid, "plan:set:3") is False
    assert callback_router._is_duplicate_tap(uid, "plan:set:3") is True
    assert callback_router._is_duplicate_tap(uid, "qa:q_age:1") is False
    assert callback_router._is_duplicate_tap(uid, "qa:q_age:1") is True
    # Navigation is never debounced — repeats are legitimate.
    assert callback_router._is_duplicate_tap(uid, "menu:home") is False
    assert callback_router._is_duplicate_tap(uid, "menu:home") is False
