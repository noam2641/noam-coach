"""Single source of truth for today's consumed and completed state.

DAY-SEMANTICS POLICY (B3 / ARCH-02 — resolved product decision):

- NUTRITION state (meals windows, daily flags day key, daily menu day,
  next-meal context/budget, nutrition summaries) uses the CANONICAL
  COACHING DAY (``noam_coach.services.coaching_day``): sleep/wake-anchored —
  a meal at 00:30 with a 23:00 bedtime still belongs to the evening's day.
  This module owns the accessors (:func:`coaching_day_key` /
  :func:`coaching_day_bounds_utc`); other nutrition modules must derive day
  identity through them, never independently.
- WORKOUT completion semantics and the weekly split selector remain LOCAL
  CALENDAR DAY by explicit product decision (a weekly training schedule is
  calendar-anchored). ``workout_completed_today`` /
  ``latest_closed_session_today`` below intentionally keep
  ``local_day_bounds_utc``.
- With no confirmed ``sleep_schedule`` fact the coaching day falls back to
  the calendar day, so users (and tests) without a bedtime see identical
  behavior on both models.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from config import TZ


def local_day_bounds_utc(local_now: datetime | None = None) -> tuple[str, str]:
    """UTC bounds for the LOCAL CALENDAR day (workout semantics; nutrition
    uses :func:`coaching_day_bounds_utc` instead — see module docstring)."""
    current = (local_now or datetime.now(TZ)).astimezone(TZ)
    start_local = current.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(days=1)
    return (
        start_local.astimezone(timezone.utc).isoformat(),
        end_local.astimezone(timezone.utc).isoformat(),
    )


async def coaching_day_for(db: Any, user_id: int, local_now: datetime | None = None):
    """The canonical coaching day (FIX 41 service) for one instant."""
    from noam_coach.services.coaching_day import resolve_coaching_day

    return await resolve_coaching_day(db, user_id, local_now=local_now)


async def coaching_day_key(
    db: Any, user_id: int, local_now: datetime | None = None
) -> str:
    """Canonical nutrition day key (YYYY-MM-DD of the coaching day)."""
    return (await coaching_day_for(db, user_id, local_now)).day_key


async def coaching_day_bounds_utc(
    db: Any, user_id: int, local_now: datetime | None = None
) -> tuple[str, str]:
    """Canonical UTC bounds of the nutrition (coaching) day."""
    day = await coaching_day_for(db, user_id, local_now)
    return day.start_utc, day.end_utc


async def consumed_totals(
    db: Any,
    user_id: int,
    *,
    now: datetime | None = None,
) -> tuple[float, float]:
    # B3/ARCH-02: nutrition day = canonical coaching day.
    start, end = await coaching_day_bounds_utc(db, user_id, now)
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
    # B3/ARCH-02: nutrition day = canonical coaching day.
    start, end = await coaching_day_bounds_utc(db, user_id, now)
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
    # B3/ARCH-02: nutrition day = canonical coaching day.
    start, end = await coaching_day_bounds_utc(db, user_id, now)
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
    # WORKOUT semantics: intentionally LOCAL CALENDAR DAY (product decision;
    # see module docstring) — do NOT migrate to the coaching day.
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
