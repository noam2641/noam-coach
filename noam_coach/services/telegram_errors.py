from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

TRANSIENT_CLASS_NAMES = {
    "BrokenResourceError",
    "ClosedResourceError",
    "ConnectError",
    "ConnectTimeout",
    "NetworkError",
    "PoolTimeout",
    "ReadError",
    "ReadTimeout",
    "TimedOut",
    "TimeoutException",
    "WriteError",
    "WriteTimeout",
}

STALE_CALLBACK_MESSAGE_FRAGMENTS = (
    "query is too old",
    "query id is invalid",
    "response timeout expired",
)

# NOTE: "message is not modified" is deliberately NOT listed — it means the
# screen already shows this exact content (e.g. a double-tap), and falling
# back to reply_text would send the user a duplicate message. safe_edit
# silently ignores that case instead.
STALE_EDIT_MESSAGE_FRAGMENTS = (
    "message to edit not found",
    "message can't be edited",
    "message identifier is not specified",
)


@dataclass(frozen=True)
class TelegramErrorDecision:
    fingerprint: str
    transient: bool
    log_level: str
    notify_admin: bool
    notify_user: bool


_LAST_ADMIN_NOTICE_AT: dict[str, float] = {}


_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b"), "<redacted-token>"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer <redacted>"),
    (re.compile(r"(?i)(token=)[^&\s]+"), r"\1<redacted>"),
    (
        re.compile(r"(?i)\b[A-Z]:(?:\\+|/)+Users(?:\\+|/)+[^\\/\s]+(?:\\+|/)+[^\s'\"]+"),
        "<redacted-user-path>",
    ),
)


def redact_sensitive_text(value: Any, *, max_length: int = 500) -> str:
    text = str(value)
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    if len(text) > max_length:
        text = text[: max_length - 1] + "…"
    return text


def _walk_error_chain(exc: BaseException | None) -> list[BaseException]:
    chain: list[BaseException] = []
    seen: set[int] = set()
    current = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def _class_names(exc: BaseException | None) -> set[str]:
    return {type(item).__name__ for item in _walk_error_chain(exc)}


def telegram_error_fingerprint(exc: BaseException | None) -> str:
    if exc is None:
        return "unknown"
    names = sorted(_class_names(exc))
    message = redact_sensitive_text(str(exc).strip().splitlines()[0], max_length=80)
    return f"{'|'.join(names)}:{message}"


def is_transient_telegram_error(exc: BaseException | None) -> bool:
    names = _class_names(exc)
    if names & TRANSIENT_CLASS_NAMES:
        return True
    text = repr(exc)
    return any(name in text for name in TRANSIENT_CLASS_NAMES)


def is_stale_callback_error(exc: BaseException | None) -> bool:
    """Return True for Telegram's expired callback-query ACK errors."""
    text = repr(exc).lower()
    return any(fragment in text for fragment in STALE_CALLBACK_MESSAGE_FRAGMENTS)


def is_stale_edit_error(exc: BaseException | None) -> bool:
    """Return True when a Telegram message can no longer be edited."""
    text = repr(exc).lower()
    return any(fragment in text for fragment in STALE_EDIT_MESSAGE_FRAGMENTS)


def should_notify_admin(
    fingerprint: str,
    *,
    window_seconds: int = 600,
    now: float | None = None,
) -> bool:
    current = time.monotonic() if now is None else now
    previous = _LAST_ADMIN_NOTICE_AT.get(fingerprint)
    if previous is not None and current - previous < window_seconds:
        return False
    _LAST_ADMIN_NOTICE_AT[fingerprint] = current
    return True


def classify_telegram_error(
    exc: BaseException | None,
    *,
    shutting_down: bool = False,
    update: Any = None,
) -> TelegramErrorDecision:
    transient = is_transient_telegram_error(exc)
    stale_callback = is_stale_callback_error(exc)
    fingerprint = telegram_error_fingerprint(exc)
    has_user_update = getattr(update, "effective_message", None) is not None
    if transient or stale_callback:
        return TelegramErrorDecision(
            fingerprint=fingerprint,
            transient=True,
            log_level="debug" if shutting_down else "warning",
            notify_admin=False,
            notify_user=False if (shutting_down or stale_callback) else has_user_update,
        )
    return TelegramErrorDecision(
        fingerprint=fingerprint,
        transient=False,
        log_level="error",
        notify_admin=should_notify_admin(fingerprint),
        notify_user=has_user_update,
    )
