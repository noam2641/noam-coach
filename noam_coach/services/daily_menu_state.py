"""Daily menu message lifecycle helpers.

The product contract for TASK-05 is that ``תפריט להיום`` is a standalone,
pin-friendly message.  These helpers keep the Telegram message id in the same
per-day state store used by the rest of the daily flows, so refresh/replace
flows can later edit the same menu instead of creating ambiguous duplicate
screens.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from config import TZ
from helpers import utc_now

DAILY_MENU_MESSAGE_KEY = "daily_menu_message"
ACTIVE_DAILY_MENU_KEY = "active_daily_menu"


async def _daily_flags(db: Any, user_id: int, local_day: str) -> dict[str, Any]:
    row = await db.fetch_one(
        "SELECT flags FROM daily_flags WHERE user_id=? AND day=?",
        (user_id, local_day),
    )
    if not row:
        return {}
    try:
        parsed = json.loads(row["flags"] or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


async def _save_daily_flags(db: Any, user_id: int, local_day: str, flags: dict[str, Any]) -> None:
    now = utc_now()
    await db.execute(
        """
        INSERT INTO daily_flags(user_id, day, flags, created_at)
        VALUES(?, ?, ?, ?)
        ON CONFLICT(user_id, day) DO UPDATE SET flags=excluded.flags
        """,
        (user_id, local_day, json.dumps(flags, ensure_ascii=False), now),
    )


def _local_day(now: datetime | None = None) -> str:
    return (now or datetime.now(TZ)).astimezone(TZ).date().isoformat()


async def remember_daily_menu_message(
    db: Any,
    user_id: int,
    *,
    chat_id: int | str | None,
    message_id: int | None,
    now: datetime | None = None,
    source: str = "daily_menu",
) -> None:
    """Store the standalone menu message id for today's menu.

    Missing ids are ignored so test doubles / failed sends do not corrupt state.
    """
    if message_id is None:
        return
    local_day = _local_day(now)
    flags = await _daily_flags(db, user_id, local_day)
    flags[DAILY_MENU_MESSAGE_KEY] = {
        "chat_id": chat_id,
        "message_id": int(message_id),
        "source": source,
        "saved_at": datetime.now(TZ).isoformat(),
    }
    await _save_daily_flags(db, user_id, local_day, flags)


async def get_daily_menu_message(
    db: Any,
    user_id: int,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    flags = await _daily_flags(db, user_id, _local_day(now))
    value = flags.get(DAILY_MENU_MESSAGE_KEY)
    return value if isinstance(value, dict) else None


async def remember_active_daily_menu(
    db: Any,
    user_id: int,
    *,
    text: str,
    strategy: str | None = None,
    revision: int | None = None,
    now: datetime | None = None,
    source: str = "daily_menu",
) -> dict[str, Any]:
    """Persist the latest full standalone menu text for revision flows."""
    local_day = _local_day(now)
    flags = await _daily_flags(db, user_id, local_day)
    previous = flags.get(ACTIVE_DAILY_MENU_KEY)
    previous_revision = (
        int(previous.get("revision") or 0)
        if isinstance(previous, dict)
        else 0
    )
    menu_state = {
        "text": text,
        "strategy": strategy,
        "revision": previous_revision + 1 if revision is None else int(revision),
        "source": source,
        "updated_at": datetime.now(TZ).isoformat(),
    }
    flags[ACTIVE_DAILY_MENU_KEY] = menu_state
    await _save_daily_flags(db, user_id, local_day, flags)
    return menu_state


async def get_active_daily_menu(
    db: Any,
    user_id: int,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Return today's latest full standalone menu, if one exists."""
    flags = await _daily_flags(db, user_id, _local_day(now))
    value = flags.get(ACTIVE_DAILY_MENU_KEY)
    return value if isinstance(value, dict) else None
