"""Cross-source workout-event reconciliation (FIX 52).

Bot ``sessions`` rows and HealthKit ``health`` (sample_type='workout') rows
are two independent evidence streams for "did a workout happen on day X".
Before this module, historical consumers picked one source each with no
shared identity or deduplication:

* ``routine.learn_workout_pattern`` reads HealthKit only -- a bot-only
  session (no Watch/HealthKit import) never influences the learned typical
  workout time/duration.
* ``planning.adherence_snapshot`` reads bot ``sessions`` only -- a
  HealthKit-only workout (imported, never started via the bot) does not
  count toward the weekly adherence number, even though the current-day
  resolver (``user_state.resolve_workout_state``) already recognizes it.

This module provides the minimal shared primitive both readers need: the
set of local calendar days within a window that have workout evidence from
EITHER source, deduplicated so a day with both a bot session and a
HealthKit import (the same physical workout recorded twice) counts once.
Full event-level identity/matching (matching a specific bot session to a
specific HealthKit sample by time overlap) is a larger project than either
consumer actually needs -- both only ever asked a day-level question
("was there a workout that day"), not an event-level one.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from config import TZ


async def reconciled_workout_days(
    db: Any,
    user_id: int,
    start_utc: str,
    end_utc: str,
) -> set[str]:
    """Return the set of local-day keys (YYYY-MM-DD) with workout evidence
    from EITHER a completed bot session or a HealthKit workout sample,
    within [start_utc, end_utc). A day present in both sources counts once.
    """
    bot_rows = await db.fetch_all(
        """
        SELECT started_at FROM sessions
        WHERE user_id=? AND status='completed' AND started_at>=? AND started_at<?
        """,
        (user_id, start_utc, end_utc),
    )
    health_rows = await db.fetch_all(
        """
        SELECT start_time FROM health
        WHERE user_id=? AND sample_type='workout' AND start_time>=? AND start_time<?
        """,
        (user_id, start_utc, end_utc),
    )

    days: set[str] = set()
    for row in bot_rows:
        day = _local_day_key(row.get("started_at"))
        if day:
            days.add(day)
    for row in health_rows:
        day = _local_day_key(row.get("start_time"))
        if day:
            days.add(day)
    return days


def _local_day_key(iso_value: Any) -> str | None:
    if not iso_value:
        return None
    try:
        parsed = datetime.fromisoformat(str(iso_value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(TZ).date().isoformat()
