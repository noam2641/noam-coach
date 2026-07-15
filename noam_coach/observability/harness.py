"""Interaction trace test harness (Observability O9).

Journey-level regression API: run one user turn through the REAL ingress
envelope (observed_handler → interaction scope → handler) and get back the
machine-readable :class:`InteractionTrace` for assertions like

    trace = await run_user_turn(db, user_id, handler, text="מה לאכול עכשיו?")
    assert trace.routing.properties["handler"] == "free_text"
    assert trace.ai_calls[0].purpose == "intent_classification"
    assert trace.visible_outputs == ["..."]

The harness asserts nothing itself and never fakes events — every event in
the returned trace was written by the same production instrumentation the
live bot uses.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Awaitable, Callable

from noam_coach.observability.obs_context import current_interaction_id
from noam_coach.observability.session_trace import InteractionTrace, load_interaction
from noam_coach.observability.telegram_ingress import observed_handler


class HarnessMessage:
    """Telegram Message double: replies succeed and mint message ids."""

    def __init__(self, message_id: int = 100, chat_id: int = 1) -> None:
        self.message_id = message_id
        self.chat = SimpleNamespace(id=chat_id)
        self.chat_id = chat_id
        self.replies: list[tuple[str, Any]] = []
        self._next_id = message_id + 1000

    async def reply_text(self, text: str, reply_markup: Any = None, parse_mode: str | None = None) -> Any:
        self.replies.append((text, reply_markup))
        self._next_id += 1
        return SimpleNamespace(message_id=self._next_id)


class HarnessQuery:
    """CallbackQuery double for safe_edit-driven handlers."""

    def __init__(
        self,
        user_id: int,
        message_id: int = 100,
        *,
        edit_error: BaseException | None = None,
    ) -> None:
        self.from_user = SimpleNamespace(id=user_id)
        self.message = HarnessMessage(message_id, chat_id=user_id)
        self.edit_error = edit_error
        self.edits: list[tuple[str, Any]] = []
        self.data: str | None = None

    async def edit_message_text(self, text: str, reply_markup: Any = None, parse_mode: str | None = None) -> None:
        if self.edit_error is not None:
            raise self.edit_error
        self.edits.append((text, reply_markup))

    async def answer(self, *args: Any, **kwargs: Any) -> None:
        return None


def build_update(
    user_id: int,
    *,
    text: str | None = None,
    caption: str | None = None,
    callback_data: str | None = None,
    source_message_id: int | None = None,
    photo: list[Any] | None = None,
    document: Any | None = None,
    message_id: int = 100,
    query: HarnessQuery | None = None,
) -> SimpleNamespace:
    message = SimpleNamespace(
        message_id=message_id,
        text=text,
        caption=caption,
        photo=photo or [],
        document=document,
        chat=SimpleNamespace(id=user_id),
    )
    callback_query = None
    if callback_data is not None:
        callback_query = query or HarnessQuery(user_id, source_message_id or message_id)
        callback_query.data = callback_data
        if source_message_id is not None:
            callback_query.message.message_id = source_message_id
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id, first_name="Test", username=None),
        effective_chat=SimpleNamespace(id=user_id),
        effective_message=message,
        callback_query=callback_query,
    )


async def run_user_turn(
    db: Any,
    user_id: int,
    handler: Callable[[Any, Any], Awaitable[Any]],
    *,
    text: str | None = None,
    caption: str | None = None,
    callback_data: str | None = None,
    source_message_id: int | None = None,
    photo: list[Any] | None = None,
    document: Any | None = None,
    query: HarnessQuery | None = None,
    context: Any = None,
) -> InteractionTrace:
    """Run ONE user turn through the real ingress envelope; return its trace."""
    if callback_data is not None:
        kind = "callback"
    elif photo:
        kind = "photo"
    elif document is not None:
        kind = "document"
    else:
        kind = "text"
    update = build_update(
        user_id,
        text=text,
        caption=caption,
        callback_data=callback_data,
        source_message_id=source_message_id,
        photo=photo,
        document=document,
        query=query,
    )

    captured: dict[str, str | None] = {"interaction_id": None}

    async def capturing_handler(handler_update: Any, handler_context: Any) -> Any:
        captured["interaction_id"] = current_interaction_id()
        return await handler(handler_update, handler_context)

    await observed_handler(kind, capturing_handler)(update, context)
    interaction_id = captured["interaction_id"]
    if interaction_id is None:
        raise AssertionError(
            "run_user_turn: the handler never ran inside an interaction scope "
            "(is the user id allowed by SETTINGS.telegram_allowed_user_id?)"
        )
    trace = await load_interaction(db, user_id, interaction_id)
    if trace is None:
        raise AssertionError("run_user_turn: no events recorded for the interaction")
    return trace
