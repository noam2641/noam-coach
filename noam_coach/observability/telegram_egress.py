"""Telegram render + delivery observability (Observability O3).

Instrumented egress boundaries:

1. ``safe_edit`` — the canonical screen-update path (~180 call sites).
   Wrapped at the ``coach_bot`` facade: ``@runtime_bound`` re-syncs the
   ``safe_edit`` global from the facade before every call, so one install
   covers every module without touching the (protected) ``ui.py`` source.
   The wrapper does NOT reimplement safe_edit's logic — it hands the
   original function a probed ``query`` whose ``edit_message_text`` /
   ``message.reply_text`` / ``send_new`` emit delivery events around the
   real Telegram operations. The exact edit → stale-failure → reply
   fallback chain is therefore traced as it actually happens, including a
   reply-fallback failure that safe_edit itself deliberately suppresses.

2. ``FreeTextContext.send`` — free-text replies from the assistant flow.

3. ``send_to_user`` — the proactive/scheduled text boundary (jobs).

4. ``ExtBot.send_message`` / ``ExtBot.edit_message_text`` — the transport
   catch-all. Every remaining direct ``message.reply_text`` /
   ``progress.edit_text`` production path (meal progress/approval cards,
   onboarding wizard replies, the morning check-in send, …) ultimately
   funnels through these two python-telegram-bot methods, so wrapping them
   at the class level covers the long tail WITHOUT editing any protected
   handler file. Boundaries 1–3 set a suppression flag around their inner
   calls so nothing is double-emitted.

Semantics (never a false ``message_sent``):

- ``ui.render.prepared``   — exact final text + control model, BEFORE any
                             network attempt. Content-mode governed.
- ``delivery.attempted``   — operation edit|reply|send, per real attempt.
- ``delivery.succeeded``   — outcome "delivered", with the resulting
                             Telegram message identity when available; a
                             Telegram "message is not modified" answer is
                             recorded with outcome "not_modified" — the
                             screen already showed this content; it is
                             deliberately NOT a content-changing edit.
- ``delivery.failed``      — classified reason (stale_edit, network, …).

Known exemptions (documented, deliberate):
- workout rest-timer countdown ticks (one edit every few seconds during a
  rest) run inside :func:`unobserved_delivery` — high frequency, zero
  decision value; the rest-timer lifecycle itself is O7 scope.
"""

from __future__ import annotations

import functools
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

from noam_coach.observability import taxonomy
from noam_coach.observability.emit import emit_event
from noam_coach.observability.ids import new_render_id
from noam_coach.observability.obs_context import current_user_id

SURFACE_TELEGRAM = "telegram"

# When a higher-level boundary (safe_edit probe, send_to_user, FreeText
# send) already emitted the render/delivery pair, the transport catch-all
# must stay silent for the inner python-telegram-bot call.
_transport_probe_suppressed: ContextVar[bool] = ContextVar(
    "obs_transport_probe_suppressed", default=False
)


@contextmanager
def unobserved_delivery() -> Iterator[None]:
    """Suppress transport-level delivery events inside this scope.

    Reserved for explicitly justified high-frequency, zero-decision-value
    output (rest-timer countdown ticks). Every use is an exemption that the
    final report must list.
    """
    token = _transport_probe_suppressed.set(True)
    try:
        yield
    finally:
        _transport_probe_suppressed.reset(token)


def _facade() -> Any:
    import coach_bot

    return coach_bot


def control_model(keyboard: Any) -> list[dict[str, Any]]:
    """Normalized reader for an InlineKeyboardMarkup's presented controls."""
    rows = getattr(keyboard, "inline_keyboard", None) or []
    controls: list[dict[str, Any]] = []
    for row_index, row in enumerate(rows):
        for col_index, control in enumerate(row):
            controls.append(
                {
                    "label": getattr(control, "text", None),
                    "callback_data": getattr(control, "callback_data", None),
                    "row": row_index,
                    "col": col_index,
                }
            )
    return controls


def classify_delivery_error(exc: BaseException) -> str:
    try:
        from noam_coach.services.telegram_errors import is_stale_edit_error

        if is_stale_edit_error(exc):
            return "stale_edit"
    except Exception:  # noqa: BLE001
        pass
    if "not modified" in str(exc).lower():
        return "not_modified"
    return type(exc).__name__


class _EgressTrace:
    """Shared state for one render's delivery chain."""

    def __init__(self, db: Any, user_id: int, render_id: str, chat_id: Any = None) -> None:
        self.db = db
        self.user_id = user_id
        self.render_id = render_id
        self.chat_id = chat_id

    async def attempted(self, operation: str, *, target_message_id: Any = None) -> None:
        await emit_event(
            self.db,
            self.user_id,
            taxonomy.DELIVERY_ATTEMPTED,
            entity="delivery",
            entity_id=self.render_id,
            source="telegram_egress",
            surface=SURFACE_TELEGRAM,
            status="attempted",
            properties={
                "render_id": self.render_id,
                "operation": operation,
                "channel": "telegram",
                "chat_id": self.chat_id,
                "target_message_id": target_message_id,
            },
        )

    async def succeeded(
        self,
        operation: str,
        *,
        message_id: Any = None,
        outcome: str = "delivered",
    ) -> None:
        await emit_event(
            self.db,
            self.user_id,
            taxonomy.DELIVERY_SUCCEEDED,
            entity="delivery",
            entity_id=self.render_id,
            source="telegram_egress",
            surface=SURFACE_TELEGRAM,
            status="succeeded",
            outcome=outcome,
            properties={
                "render_id": self.render_id,
                "operation": operation,
                "message_id": message_id,
                "chat_id": self.chat_id,
            },
        )

    async def failed(self, operation: str, exc: BaseException, *, will_fallback: bool) -> None:
        await emit_event(
            self.db,
            self.user_id,
            taxonomy.DELIVERY_FAILED,
            entity="delivery",
            entity_id=self.render_id,
            source="telegram_egress",
            surface=SURFACE_TELEGRAM,
            status="failed",
            outcome=classify_delivery_error(exc),
            properties={
                "render_id": self.render_id,
                "operation": operation,
                "reason": classify_delivery_error(exc),
                "error_type": type(exc).__name__,
                "will_fallback": will_fallback,
                "chat_id": self.chat_id,
            },
        )


async def _emit_render_prepared(
    egress: _EgressTrace,
    text: str,
    keyboard: Any,
    intended_operation: str,
) -> None:
    controls = control_model(keyboard)
    await emit_event(
        egress.db,
        egress.user_id,
        taxonomy.UI_RENDER_PREPARED,
        entity="render",
        entity_id=egress.render_id,
        source="telegram_egress",
        surface=SURFACE_TELEGRAM,
        status="prepared",
        properties={
            "render_id": egress.render_id,
            "parse_mode": "HTML",
            "intended_operation": intended_operation,
            "controls": controls,
            "control_count": len(controls),
        },
        content={"text": text},
    )


class _MessageProbe:
    """Wraps a Telegram ``Message`` so reply_text emits delivery events."""

    def __init__(self, inner: Any, egress: _EgressTrace) -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_egress", egress)

    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "_inner"), name)

    async def reply_text(self, text: str, **kwargs: Any) -> Any:
        inner = object.__getattribute__(self, "_inner")
        egress: _EgressTrace = object.__getattribute__(self, "_egress")
        await egress.attempted("reply", target_message_id=getattr(inner, "message_id", None))
        token = _transport_probe_suppressed.set(True)
        try:
            result = await inner.reply_text(text, **kwargs)
        except BaseException as exc:
            # safe_edit suppresses a failed reply fallback; the trace must
            # still show it (acceptance: reply fallback failure is visible).
            await egress.failed("reply", exc, will_fallback=False)
            raise
        finally:
            _transport_probe_suppressed.reset(token)
        await egress.succeeded("reply", message_id=getattr(result, "message_id", None))
        return result


class _QueryProbe:
    """Wraps safe_edit's ``query`` so real Telegram calls emit delivery events."""

    def __init__(self, inner: Any, egress: _EgressTrace) -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_egress", egress)

    def __getattr__(self, name: str) -> Any:
        inner = object.__getattribute__(self, "_inner")
        egress: _EgressTrace = object.__getattribute__(self, "_egress")
        if name == "message":
            message = getattr(inner, "message", None)
            if message is None:
                return None
            return _MessageProbe(message, egress)
        if name == "send_new":
            send_new = getattr(inner, "send_new", None)
            if not callable(send_new):
                return send_new

            async def observed_send_new(text: str, **kwargs: Any) -> Any:
                await egress.attempted("send")
                token = _transport_probe_suppressed.set(True)
                try:
                    result = await send_new(text, **kwargs)
                except BaseException as exc:
                    await egress.failed("send", exc, will_fallback=False)
                    raise
                finally:
                    _transport_probe_suppressed.reset(token)
                await egress.succeeded("send", message_id=getattr(result, "message_id", None))
                return result

            return observed_send_new
        return getattr(inner, name)

    async def edit_message_text(self, text: str, **kwargs: Any) -> Any:
        inner = object.__getattribute__(self, "_inner")
        egress: _EgressTrace = object.__getattribute__(self, "_egress")
        message = getattr(inner, "message", None)
        await egress.attempted(
            "edit", target_message_id=getattr(message, "message_id", None)
        )
        token = _transport_probe_suppressed.set(True)
        try:
            result = await inner.edit_message_text(text, **kwargs)
        except BaseException as exc:
            reason = classify_delivery_error(exc)
            if reason == "not_modified":
                # The screen already shows exactly this content. Telegram
                # rejected the edit as a no-op; deliberately NOT reported as
                # a content-changing success and NOT as a delivery failure.
                await egress.succeeded("edit", message_id=getattr(message, "message_id", None), outcome="not_modified")
            else:
                await egress.failed("edit", exc, will_fallback=reason == "stale_edit")
            raise
        finally:
            _transport_probe_suppressed.reset(token)
        await egress.succeeded("edit", message_id=getattr(message, "message_id", None))
        return result


def _resolve_user_id(query: Any) -> int | None:
    ambient = current_user_id()
    if ambient is not None:
        return ambient
    from_user = getattr(query, "from_user", None)
    if from_user is not None and getattr(from_user, "id", None) is not None:
        return int(from_user.id)
    return None


_original_safe_edit: Any = None
_original_free_text_send: Any = None
_original_send_to_user: Any = None
_original_bot_send_message: Any = None
_original_bot_edit_message_text: Any = None


def _transport_user_id(chat_id: Any) -> int | None:
    ambient = current_user_id()
    if ambient is not None:
        return ambient
    try:
        from config import SETTINGS

        allowed = getattr(SETTINGS, "telegram_allowed_user_id", None)
        if allowed is not None and chat_id is not None and int(chat_id) == int(allowed):
            # Single-user bot: the private chat id IS the user id.
            return int(allowed)
    except Exception:  # noqa: BLE001
        return None
    return None


def _extract_call(args: tuple, kwargs: dict, first: str, second: str) -> tuple[Any, Any]:
    first_value = kwargs.get(first, args[0] if len(args) >= 1 else None)
    second_value = kwargs.get(second, args[1] if len(args) >= 2 else None)
    return first_value, second_value


def install_telegram_egress() -> None:
    """Wrap the canonical egress boundaries (idempotent)."""
    global _original_safe_edit, _original_free_text_send, _original_send_to_user
    global _original_bot_send_message, _original_bot_edit_message_text
    coach_bot = _facade()

    if _original_bot_send_message is None:
        try:
            from telegram.ext import ExtBot
        except Exception:  # noqa: BLE001 — no PTB in some tooling contexts.
            ExtBot = None
        if ExtBot is not None:
            _original_bot_send_message = ExtBot.send_message
            _original_bot_edit_message_text = ExtBot.edit_message_text

            @functools.wraps(_original_bot_send_message)
            async def observed_bot_send_message(self: Any, *args: Any, **kwargs: Any) -> Any:
                if _transport_probe_suppressed.get():
                    return await _original_bot_send_message(self, *args, **kwargs)
                chat_id, text = _extract_call(args, kwargs, "chat_id", "text")
                user_id = _transport_user_id(chat_id)
                if user_id is None:
                    return await _original_bot_send_message(self, *args, **kwargs)
                egress = _EgressTrace(_facade().DB, user_id, new_render_id(), chat_id=chat_id)
                await _emit_render_prepared(egress, text, kwargs.get("reply_markup"), "send")
                await egress.attempted("send")
                try:
                    result = await _original_bot_send_message(self, *args, **kwargs)
                except BaseException as exc:
                    await egress.failed("send", exc, will_fallback=False)
                    raise
                await egress.succeeded("send", message_id=getattr(result, "message_id", None))
                return result

            @functools.wraps(_original_bot_edit_message_text)
            async def observed_bot_edit_message_text(self: Any, *args: Any, **kwargs: Any) -> Any:
                if _transport_probe_suppressed.get():
                    return await _original_bot_edit_message_text(self, *args, **kwargs)
                text, chat_id = _extract_call(args, kwargs, "text", "chat_id")
                message_id = kwargs.get("message_id", args[2] if len(args) >= 3 else None)
                user_id = _transport_user_id(chat_id)
                if user_id is None:
                    return await _original_bot_edit_message_text(self, *args, **kwargs)
                egress = _EgressTrace(_facade().DB, user_id, new_render_id(), chat_id=chat_id)
                await _emit_render_prepared(egress, text, kwargs.get("reply_markup"), "edit")
                await egress.attempted("edit", target_message_id=message_id)
                try:
                    result = await _original_bot_edit_message_text(self, *args, **kwargs)
                except BaseException as exc:
                    reason = classify_delivery_error(exc)
                    if reason == "not_modified":
                        await egress.succeeded("edit", message_id=message_id, outcome="not_modified")
                    else:
                        await egress.failed("edit", exc, will_fallback=reason == "stale_edit")
                    raise
                await egress.succeeded("edit", message_id=message_id)
                return result

            ExtBot.send_message = observed_bot_send_message
            ExtBot.edit_message_text = observed_bot_edit_message_text

    if _original_safe_edit is None:
        _original_safe_edit = coach_bot.safe_edit

        @functools.wraps(_original_safe_edit)
        async def observed_safe_edit(query: Any, text: str, keyboard: Any = None) -> None:
            user_id = _resolve_user_id(query)
            if user_id is None:
                return await _original_safe_edit(query, text, keyboard)
            db = _facade().DB
            message = getattr(query, "message", None)
            chat = getattr(message, "chat", None) if message is not None else None
            egress = _EgressTrace(db, user_id, new_render_id(), chat_id=getattr(chat, "id", None))
            await _emit_render_prepared(egress, text, keyboard, "edit")
            return await _original_safe_edit(_QueryProbe(query, egress), text, keyboard)

        coach_bot.safe_edit = observed_safe_edit

    if _original_free_text_send is None:
        from noam_coach.bot import assistant as bot_assistant

        _original_free_text_send = bot_assistant.FreeTextContext.send
        sentinel = bot_assistant._DEFAULT_REPLY_KEYBOARD

        @functools.wraps(_original_free_text_send)
        async def observed_send(self: Any, body: str, kb: Any = sentinel) -> None:
            user_id = current_user_id() or getattr(self, "user_id", None)
            if user_id is None:
                return await _original_free_text_send(self, body, kb)
            db = _facade().DB
            keyboard = self.follow if kb is sentinel else kb
            egress = _EgressTrace(db, int(user_id), new_render_id())
            await _emit_render_prepared(egress, body, keyboard, "reply")
            import dataclasses

            probed = dataclasses.replace(self, message=_MessageProbe(self.message, egress))
            return await _original_free_text_send(probed, body, kb)

        bot_assistant.FreeTextContext.send = observed_send

    if _original_send_to_user is None:
        _original_send_to_user = coach_bot.send_to_user

        @functools.wraps(_original_send_to_user)
        async def observed_send_to_user(context: Any, text: str, *, reply_markup: Any = None) -> Any:
            user_id = current_user_id()
            if user_id is None:
                from config import SETTINGS

                user_id = getattr(SETTINGS, "telegram_allowed_user_id", None)
            if user_id is None:
                return await _original_send_to_user(context, text, reply_markup=reply_markup)
            db = _facade().DB
            egress = _EgressTrace(db, int(user_id), new_render_id())
            await _emit_render_prepared(egress, text, reply_markup, "send")
            await egress.attempted("send")
            token = _transport_probe_suppressed.set(True)
            try:
                result = await _original_send_to_user(context, text, reply_markup=reply_markup)
            except BaseException as exc:
                await egress.failed("send", exc, will_fallback=False)
                raise
            finally:
                _transport_probe_suppressed.reset(token)
            await egress.succeeded("send", message_id=getattr(result, "message_id", None))
            return result

        coach_bot.send_to_user = observed_send_to_user


def uninstall_telegram_egress() -> None:
    """Restore the original egress functions (test isolation)."""
    global _original_safe_edit, _original_free_text_send, _original_send_to_user
    global _original_bot_send_message, _original_bot_edit_message_text
    coach_bot = _facade()
    if _original_safe_edit is not None:
        coach_bot.safe_edit = _original_safe_edit
        _original_safe_edit = None
    if _original_free_text_send is not None:
        from noam_coach.bot import assistant as bot_assistant

        bot_assistant.FreeTextContext.send = _original_free_text_send
        _original_free_text_send = None
    if _original_send_to_user is not None:
        coach_bot.send_to_user = _original_send_to_user
        _original_send_to_user = None
    if _original_bot_send_message is not None:
        from telegram.ext import ExtBot

        ExtBot.send_message = _original_bot_send_message
        ExtBot.edit_message_text = _original_bot_edit_message_text
        _original_bot_send_message = None
        _original_bot_edit_message_text = None


async def observed_reply(
    message: Any,
    text: str,
    *,
    user_id: int,
    keyboard: Any = None,
    parse_mode: str | None = None,
) -> Any:
    """Instrumented direct reply for high-value call sites that reply to the
    incoming message instead of going through safe_edit. Returns the sent
    Telegram message (same contract as ``message.reply_text``)."""
    db = _facade().DB
    egress = _EgressTrace(db, user_id, new_render_id())
    await _emit_render_prepared(egress, text, keyboard, "reply")
    probe = _MessageProbe(message, egress)
    return await probe.reply_text(text, reply_markup=keyboard, parse_mode=parse_mode)
