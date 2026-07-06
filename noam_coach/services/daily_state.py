"""Single source of truth for today's consumed and completed state."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from config import TZ


def local_day_bounds_utc(local_now: datetime | None = None) -> tuple[str, str]:
    """Return UTC bounds for the user's current local day."""
    current = (local_now or datetime.now(TZ)).astimezone(TZ)
    start_local = current.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(days=1)
    return (
        start_local.astimezone(timezone.utc).isoformat(),
        end_local.astimezone(timezone.utc).isoformat(),
    )


async def consumed_totals(
    db: Any,
    user_id: int,
    *,
    now: datetime | None = None,
) -> tuple[float, float]:
    start, end = local_day_bounds_utc(now)
    row = await db.fetch_one(
        """
        SELECT COALESCE(SUM(calories),0) AS calories,
               COALESCE(SUM(protein),0) AS protein
        FROM meals
        WHERE user_id=? AND eaten_at>=? AND eaten_at<?
        """,
        (user_id, start, end),
    )
    return float(row["calories"]), float(row["protein"])


async def consumed_meals(
    db: Any,
    user_id: int,
    *,
    now: datetime | None = None,
    descending: bool = False,
) -> list[dict[str, Any]]:
    start, end = local_day_bounds_utc(now)
    direction = "DESC" if descending else "ASC"
    return await db.fetch_all(
        f"""
        SELECT id, name, calories, protein, carbs, fat, confidence, eaten_at
        FROM meals
        WHERE user_id=? AND eaten_at>=? AND eaten_at<?
        ORDER BY eaten_at {direction}, id {direction}
        """,
        (user_id, start, end),
    )


async def consumed_meal_items(
    db: Any,
    user_id: int,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    start, end = local_day_bounds_utc(now)
    return await db.fetch_all(
        """
        SELECT mi.name, mi.grams, mi.calories, mi.protein
        FROM meal_items mi
        JOIN meals m ON m.id = mi.meal_id
        WHERE m.user_id=? AND m.eaten_at>=? AND m.eaten_at<?
        """,
        (user_id, start, end),
    )


async def latest_closed_session_today(
    db: Any,
    user_id: int,
    *,
    now: datetime | None = None,
    include_cancelled: bool = True,
) -> dict[str, Any] | None:
    start, end = local_day_bounds_utc(now)
    statuses = ("completed", "partial", "cancelled") if include_cancelled else ("completed", "partial")
    placeholders = ",".join("?" for _ in statuses)
    return await db.fetch_one(
        f"""
        SELECT *
        FROM sessions
        WHERE user_id=?
          AND ended_at>=?
          AND ended_at<?
          AND status IN ({placeholders})
        ORDER BY ended_at DESC, id DESC
        LIMIT 1
        """,
        (user_id, start, end, *statuses),
    )


async def workout_completed_today(
    db: Any,
    user_id: int,
    *,
    now: datetime | None = None,
) -> bool:
    start, end = local_day_bounds_utc(now)
    bot_done = await db.fetch_one(
        """
        SELECT 1
        FROM sessions
        WHERE user_id=?
          AND status IN ('completed','partial')
          AND ended_at>=?
          AND ended_at<?
        LIMIT 1
        """,
        (user_id, start, end),
    )
    if bot_done:
        return True
    imported = await db.fetch_one(
        """
        SELECT 1
        FROM health
        WHERE user_id=?
          AND sample_type='workout'
          AND start_time>=?
          AND start_time<?
        LIMIT 1
        """,
        (user_id, start, end),
    )
    return imported is not None
