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
