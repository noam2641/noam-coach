"""Canonical coaching-day boundary resolver (FIX 41).

Product decision (recorded, not inferred): the coaching day is anchored to the
user's sleep/wake routine, not strict local-calendar midnight. A user who goes
to sleep at 01:00 is still living "today" at 00:30 -- meals, daily_flags,
workout state, and the active menu must not roll over to a new day until the
user's own bedtime (plus a short buffer) has passed, even though the wall
clock has crossed midnight.

Every other day-key computation in the repository (``daily_state.py::local_day_bounds_utc``,
``health_service.py::local_day_str``, ``daily_menu_state.py::_local_day``, and
the ad-hoc ``datetime.now(TZ).date().isoformat()`` calls scattered through
``next_meal.py``) must migrate to this resolver instead of deriving a day key
independently (FIX 41's required behavior: "All subsystems must use the same
decision.").

Fallback: if the user has no confirmed ``sleep_schedule`` fact (bedtime
unknown), the coaching day equals the strict local-calendar day -- there is no
routine to anchor to, so calendar midnight is the only available boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from datetime import time as dtime
from typing import Any

import user_model
from config import TZ

# Grace window past the user's stated bedtime during which activity still
# belongs to the *previous* coaching day. Chosen to cover "went to sleep a
# little late" without letting the coaching day run indefinitely.
POST_BEDTIME_GRACE = timedelta(hours=4)

# Default rollover hour used only when a user has no sleep_schedule fact yet.
DEFAULT_ROLLOVER_HOUR = 0


@dataclass(frozen=True)
class CoachingDay:
    """One resolved coaching-day boundary for a single instant."""

    day_key: str
    start_utc: str
    end_utc: str
    phase: str  # "awake_before_bed" | "post_midnight_pre_wake" | "daytime"
    rollover_reason: str  # "calendar_midnight" | "sleep_schedule_bedtime"
    anchor_hour: int  # local hour (0-23) at which the day actually rolls over


# A ``sleep_schedule`` fact is written in two shapes by two writers:
#   - Health import (health_service.py) writes ``bedtime`` / ``wake_time``.
#   - Onboarding text-edit (onboarding._parse_sleep_window_text) writes
#     ``typical_bedtime`` / ``typical_wake_time``.
# The resolver reads bedtime from EITHER (R-2b). Precedence: the canonical
# ``bedtime`` key wins when both are present (a fact mid-normalization), so the
# newer/normalized shape is authoritative.
_BEDTIME_KEYS = ("bedtime", "typical_bedtime")


def _parse_bedtime(value: Any) -> dtime | None:
    """Parse a ``sleep_schedule`` fact's bedtime string ("HH:MM") into a time.

    Accepts both the canonical ``bedtime`` key and the legacy ``typical_bedtime``
    key (R-2b) so onboarding-written and Health-imported sleep schedules produce
    identical coaching-day semantics. ``bedtime`` takes precedence if both exist.
    """
    if not isinstance(value, dict):
        return None
    raw: Any = None
    for key in _BEDTIME_KEYS:
        candidate = value.get(key)
        if isinstance(candidate, str) and ":" in candidate:
            raw = candidate
            break
    if not isinstance(raw, str):
        return None
    try:
        hour_str, minute_str = raw.split(":")[:2]
        return dtime(hour=int(hour_str) % 24, minute=int(minute_str) % 60)
    except (ValueError, TypeError):
        return None


async def get_rollover_hour(db: Any, user_id: int) -> tuple[int, str]:
    """Return the local hour at which the coaching day rolls over, and why.

    Only a *confirmed* sleep_schedule fact is decision-grade here (FIX 47
    policy applied locally: an unconfirmed/estimated bedtime must not silently
    become authoritative for day-boundary math). Falls back to calendar
    midnight otherwise.
    """
    fact = await user_model.get_fact(db, user_id, "sleep_schedule")
    if not fact or not fact.get("confirmed"):
        return DEFAULT_ROLLOVER_HOUR, "calendar_midnight"
    bedtime = _parse_bedtime(fact.get("value"))
    if bedtime is None:
        return DEFAULT_ROLLOVER_HOUR, "calendar_midnight"
    rollover = (
        datetime.combine(datetime.today(), bedtime) + POST_BEDTIME_GRACE
    ).time()
    # A rollover hour must stay in the "early morning" band -- if the grace
    # window pushes it past noon (an implausible bedtime fact), distrust it
    # and fall back rather than eating half the day.
    if rollover.hour >= 12:
        return DEFAULT_ROLLOVER_HOUR, "calendar_midnight"
    return rollover.hour, "sleep_schedule_bedtime"


def _day_key_for(local_now: datetime, rollover_hour: int) -> str:
    if rollover_hour == 0:
        return local_now.date().isoformat()
    anchor = local_now - timedelta(hours=rollover_hour)
    return anchor.date().isoformat()


def _phase_for(local_now: datetime, rollover_hour: int) -> str:
    if rollover_hour == 0:
        return "daytime"
    if local_now.hour < rollover_hour:
        return "post_midnight_pre_wake"
    if local_now.hour >= 22:
        return "awake_before_bed"
    return "daytime"


async def resolve_coaching_day(
    db: Any,
    user_id: int,
    *,
    local_now: datetime | None = None,
) -> CoachingDay:
    """Resolve the canonical coaching day for ``user_id`` at ``local_now``.

    This is the single function every day-key consumer should migrate to. It
    intentionally mirrors the return shape recommended by the addendum:
    day key, start/end instants, current phase, and rollover reason.
    """
    current = (local_now or datetime.now(TZ)).astimezone(TZ)
    rollover_hour, reason = await get_rollover_hour(db, user_id)
    day_key = _day_key_for(current, rollover_hour)
    phase = _phase_for(current, rollover_hour)

    start_local = datetime.combine(
        datetime.fromisoformat(day_key), dtime(hour=rollover_hour)
    ).replace(tzinfo=current.tzinfo)
    end_local = start_local + timedelta(days=1)
    return CoachingDay(
        day_key=day_key,
        start_utc=start_local.astimezone(timezone.utc).isoformat(),
        end_utc=end_local.astimezone(timezone.utc).isoformat(),
        phase=phase,
        rollover_reason=reason,
        anchor_hour=rollover_hour,
    )


def calendar_day_key(local_now: datetime | None = None) -> str:
    """Strict calendar-day key, used only as the documented fallback path."""
    current = (local_now or datetime.now(TZ)).astimezone(TZ)
    return current.date().isoformat()
