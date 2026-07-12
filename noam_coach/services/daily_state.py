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
    # Only consumed meals count toward daily totals (TASK-04): a planned or
    # recommended row must never reduce the calorie/protein balance.
    row = await db.fetch_one(
        """
        SELECT COALESCE(SUM(calories),0) AS calories,
               COALESCE(SUM(protein),0) AS protein
        FROM meals
        WHERE user_id=? AND eaten_at>=? AND eaten_at<?
          AND COALESCE(status, 'consumed')='consumed'
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
          AND COALESCE(status, 'consumed')='consumed'
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
          AND COALESCE(m.status, 'consumed')='consumed'
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
    """True only if a workout ACTUALLY completed today (bot session or
    HealthKit import) — never true from a plan, routine, or explicit
    self-report alone.

    REC-ARCH-01: delegates to ``user_state.has_actual_workout_completion_evidence_today``
    (a local import — ``user_state`` imports this module, so importing it back
    at module scope would create a cycle) so the sessions/HealthKit evidence
    query lives in exactly one place instead of being duplicated here and in
    ``user_state.resolve_workout_state``'s ACTUAL-tier candidates. Behavior is
    unchanged: still strictly "did a real event happen", not "did the user
    say so".
    """
    from noam_coach.services.user_state import has_actual_workout_completion_evidence_today

    local_now = (now or datetime.now(TZ)).astimezone(TZ)
    return await has_actual_workout_completion_evidence_today(db, user_id, local_now)
