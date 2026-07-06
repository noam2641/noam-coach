"""Small utility functions used across the application."""

from __future__ import annotations

import html
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from config import LOGGER, TZ


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def esc(value: Any) -> str:
    """HTML-escape any untrusted text (AI output, user input, Health values)
    before putting it inside a parse_mode=HTML / web HTML string."""
    return html.escape(str(value), quote=False)


def _safe_html_block(text: str) -> str:
    """Render a bot text block as safe HTML for the Mini App: escape everything,
    then re-allow only the bot's own formatting tags (<b>, <i>) and newlines.
    Any injected markup (e.g. <script>) stays neutralized."""
    out = html.escape(text, quote=False)
    out = re.sub(r'&lt;(/?[bi])&gt;', r'<\1>', out)
    return out.replace("\n", "<br>")


def friendly_error(exc: Exception, context_label: str) -> str:
    """Log the full exception and return a short, user-safe message.

    Users never see stack traces, DB errors, file paths or internal ids. The
    generated error id stays in logs only.
    """
    error_id = secrets.token_hex(3)
    LOGGER.error("[%s] %s failed: %r", error_id, context_label, exc)
    return "משהו השתבש כרגע. אפשר לנסות שוב, לחזור למסך הקודם או להמשיך מהמצב האחרון שנשמר."


def today_bounds_utc() -> tuple[str, str]:
    local_now = datetime.now(TZ)
    local_start = datetime(
        local_now.year,
        local_now.month,
        local_now.day,
        tzinfo=TZ,
    )
    local_end = local_start + timedelta(days=1)
    return (
        local_start.astimezone(timezone.utc).isoformat(),
        local_end.astimezone(timezone.utc).isoformat(),
    )
