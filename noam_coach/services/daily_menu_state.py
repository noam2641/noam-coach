"""Daily menu message lifecycle helpers.

The product contract for TASK-05 is that ``תפריט להיום`` is a standalone,
pin-friendly message.  These helpers keep the Telegram message id in the same
per-day state store used by the rest of the daily flows, so refresh/replace
flows can later edit the same menu instead of creating ambiguous duplicate
screens.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from config import TZ
from helpers import utc_now

DAILY_MENU_MESSAGE_KEY = "daily_menu_message"
ACTIVE_DAILY_MENU_KEY = "active_daily_menu"

# TASK-9: schema version tag for the structured ``meals`` payload. Bump this
# if the meal-record shape changes so future readers can branch on it; old
# text-only rows (no "schema_version" key at all) are handled separately by
# ``is_structured``/``structured_meals`` below and never crash new readers.
DAILY_MENU_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class MenuMealRecord:
    """One meal inside a structured active daily menu (TASK-9)."""

    meal_id: str
    slot: str
    time: str
    role: str
    calories: float
    protein: float
    ingredients: list[dict[str, Any]] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def menu_meal_record_from_menu_meal(meal: Any, *, slot: str, index: int) -> MenuMealRecord:
    """Build a ``MenuMealRecord`` from a ``recommendations.MenuMeal``.

    Finding 8: real structured ingredients (when the AI populated them) are
    persisted, not discarded — a targeted edit ("בלי ביצים") and the
    validator both need to know a meal actually CONTAINS eggs, not guess it
    from whether the word appears in the rendered ``note``.
    """
    raw_ingredients = getattr(meal, "ingredients", None) or []
    ingredients = [
        {
            "name": str(getattr(item, "name", "") or ""),
            "grams": getattr(item, "grams", None),
            "calories": getattr(item, "calories", None),
            "protein": getattr(item, "protein", None),
            "carbs": getattr(item, "carbs", None),
            "fat": getattr(item, "fat", None),
        }
        for item in raw_ingredients
        if str(getattr(item, "name", "") or "").strip()
    ]
    return MenuMealRecord(
        meal_id=f"{slot}-{index}",
        slot=slot,
        time=str(getattr(meal, "time_hint", "") or ""),
        role=str(getattr(meal, "name", "") or ""),
        calories=float(getattr(meal, "calories", 0) or 0),
        protein=float(getattr(meal, "protein", 0) or 0),
        ingredients=ingredients,
        note=str(getattr(meal, "note", "") or ""),
    )


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
    meals: list[MenuMealRecord] | list[dict[str, Any]] | None = None,
    menu_id: str | None = None,
    context_version: str | None = None,
) -> dict[str, Any]:
    """Persist the latest full standalone menu for revision flows.

    TASK-9: when ``meals`` is provided the canonical structured shape is
    stored alongside ``text`` (``text`` is always kept too, so every existing
    text-only reader keeps working unmodified). ``meals`` is a list of
    ``MenuMealRecord`` or already-plain dicts with the same keys.
    """
    local_day = _local_day(now)
    flags = await _daily_flags(db, user_id, local_day)
    previous = flags.get(ACTIVE_DAILY_MENU_KEY)
    previous_revision = (
        int(previous.get("revision") or 0)
        if isinstance(previous, dict)
        else 0
    )
    menu_state: dict[str, Any] = {
        "text": text,
        "strategy": strategy,
        "revision": previous_revision + 1 if revision is None else int(revision),
        "source": source,
        "updated_at": datetime.now(TZ).isoformat(),
    }
    if meals is not None:
        menu_state["schema_version"] = DAILY_MENU_SCHEMA_VERSION
        menu_state["menu_id"] = menu_id or f"menu-{user_id}-{local_day}-{menu_state['revision']}"
        menu_state["generated_at"] = menu_state["updated_at"]
        menu_state["context_version"] = context_version
        menu_state["meals"] = [
            item.to_dict() if isinstance(item, MenuMealRecord) else dict(item)
            for item in meals
        ]
    flags[ACTIVE_DAILY_MENU_KEY] = menu_state
    await _save_daily_flags(db, user_id, local_day, flags)
    return menu_state


async def get_active_daily_menu(
    db: Any,
    user_id: int,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Return today's latest full standalone menu, if one exists.

    Backward compatible with the pre-TASK-9 text-only shape: a legacy row has
    no ``schema_version``/``meals`` keys and is returned exactly as before, so
    old callers and old persisted rows never crash new readers.
    """
    flags = await _daily_flags(db, user_id, _local_day(now))
    value = flags.get(ACTIVE_DAILY_MENU_KEY)
    return value if isinstance(value, dict) else None


async def mark_daily_menu_stale(
    db: Any,
    user_id: int,
    *,
    reason: str,
    now: datetime | None = None,
) -> bool:
    """Mark today's active daily menu stale after a day-state change (FIX 43).

    Does not delete the menu (old messages/history stay intact); instead sets
    a ``stale`` flag and ``stale_reason`` so readers (save/edit handlers) can
    reject or refresh actions against it instead of silently applying them to
    a menu built from pre-change state. Returns False when there was no
    active menu to invalidate.
    """
    local_day = _local_day(now)
    flags = await _daily_flags(db, user_id, local_day)
    menu_state = flags.get(ACTIVE_DAILY_MENU_KEY)
    if not isinstance(menu_state, dict):
        return False
    menu_state["stale"] = True
    menu_state["stale_reason"] = reason
    menu_state["stale_at"] = datetime.now(TZ).isoformat()
    flags[ACTIVE_DAILY_MENU_KEY] = menu_state
    await _save_daily_flags(db, user_id, local_day, flags)
    return True


def is_structured_menu(menu_state: dict[str, Any] | None) -> bool:
    """True when ``menu_state`` carries the TASK-9 structured meal list."""
    if not isinstance(menu_state, dict):
        return False
    return isinstance(menu_state.get("meals"), list) and bool(menu_state.get("schema_version"))


def structured_meals(menu_state: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return the structured meal list, or [] for a legacy text-only menu."""
    if not is_structured_menu(menu_state):
        return []
    return [dict(item) for item in (menu_state or {}).get("meals") or [] if isinstance(item, dict)]
