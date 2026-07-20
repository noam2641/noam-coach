"""One chronological remaining-day timeline (TASK-59).

Incident: the post-meal continuation rendered a hard-to-scan block —
sleep first (an after-midnight bedtime sorts lexicographically before the
evening workout), an UNTIMED meal carrying the entire remaining budget, and
redundant prose under the timeline ("יש אימון מתוכנן היום, עדיין לפניו")
repeating what the timeline already shows.

This module is the single builder every remaining-day surface reuses (the
post-meal continuation in workout.py and the next-meal detail view) — no
renderer independently reconstructs "the rest of today" anymore:

- events: remaining meal slots (the canonical allocator
  ``build_remaining_slot_allocations`` — budget SPLIT across slots, never
  the full remainder per meal), the future workout at its scheduled time
  (including a B12 concrete reschedule), B12 planned meals that are still
  effectively planned (consumed/expired ones drop out via their durable
  lifecycle), and a confirmed-only sleep event;
- every meal slot gets a CONCRETE time (the same synthesis the next-meal
  view uses: pre-workout −90m, post-workout +15m, bedtime−1h for the night
  meal, 2.5h spacing otherwise);
- chronological order is minutes-from-NOW (mod 24h), so a 00:36 bedtime
  sorts after the 19:09 workout, not before it;
- recalculated on every render from the CURRENT context — meal approvals,
  corrections, workout completion/cancellation/reschedule and target
  changes all shift the next render because they shift the context.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from config import TZ
from helpers import esc

_UNTIMED = 10**6


@dataclass(frozen=True)
class TimelineEvent:
    sort_minutes: int
    icon: str
    time_hhmm: str | None
    text: str

    def line(self) -> str:
        time_part = f"{esc(self.time_hhmm)} " if self.time_hhmm else ""
        return f"{self.icon} {time_part}{self.text}"


def _minutes_from_now(now: datetime, hhmm: str | None) -> int:
    if not hhmm:
        return _UNTIMED
    try:
        hour, minute = (int(part) for part in hhmm.split(":"))
    except (TypeError, ValueError):
        return _UNTIMED
    event_minutes = hour * 60 + minute
    now_minutes = now.hour * 60 + now.minute
    return (event_minutes - now_minutes) % (24 * 60)


def build_remaining_day_events(
    context: Any,
    *,
    planned_meals: tuple[dict[str, Any], ...] = (),
    now: datetime | None = None,
) -> list[TimelineEvent]:
    """Build the chronological remaining-day events from the workout
    nutrition context (+ optionally the B12 planned meals)."""
    from noam_coach.services.next_meal import (
        _hhmm_from_iso,
        _slot_time_hint,
        build_remaining_slot_allocations,
    )

    local_now = now
    if local_now is None:
        parsed = None
        try:
            parsed = datetime.fromisoformat(str(context.local_now))
        except (TypeError, ValueError):
            parsed = None
        local_now = (parsed or datetime.now(TZ)).astimezone(TZ)

    events: list[TimelineEvent] = []

    phase = str(getattr(context.workout_phase, "value", context.workout_phase))
    future_workout = phase.startswith("pre_workout") or (
        phase == "rest_day" and context.minutes_until_workout not in (None, 0)
    )
    if future_workout:
        workout_hhmm = _hhmm_from_iso(context.planned_workout_start)
        if workout_hhmm:
            events.append(
                TimelineEvent(
                    sort_minutes=_minutes_from_now(local_now, workout_hhmm),
                    icon="🏋️", time_hhmm=workout_hhmm, text="אימון",
                )
            )

    for index, allocation in enumerate(build_remaining_slot_allocations(context)):
        time_hint = _slot_time_hint(context, allocation, index)
        hhmm = time_hint if _looks_like_time(time_hint) else None
        sort_key = (
            0 if time_hint == "עכשיו" else _minutes_from_now(local_now, hhmm)
        )
        display_time = time_hint if (hhmm or time_hint == "עכשיו") else None
        events.append(
            TimelineEvent(
                sort_minutes=sort_key,
                icon="🍽️",
                time_hhmm=display_time,
                text=(
                    f"{allocation.label}: "
                    f"כ-{allocation.calories} קל׳ | כ-{allocation.protein} ג׳ חלבון"
                ),
            )
        )

    for meal in planned_meals:
        planned_hhmm = None
        try:
            planned_at = datetime.fromisoformat(str(meal.get("planned_at")))
            planned_hhmm = planned_at.astimezone(TZ).strftime("%H:%M")
        except (TypeError, ValueError):
            planned_hhmm = None
        events.append(
            TimelineEvent(
                sort_minutes=_minutes_from_now(local_now, planned_hhmm),
                icon="📌",
                time_hhmm=planned_hhmm,
                text=(
                    f"מתוכנן: {meal.get('name')} "
                    f"(כ-{int(meal.get('calories') or 0)} קל׳)"
                ),
            )
        )

    if (
        context.sleep_reference in {"confirmed_fact", "routine_profile"}
        and context.hours_until_bedtime is not None
        and context.hours_until_bedtime > 0
    ):
        from datetime import timedelta

        bedtime = (local_now + timedelta(hours=context.hours_until_bedtime)).strftime("%H:%M")
        events.append(
            TimelineEvent(
                sort_minutes=_minutes_from_now(local_now, bedtime),
                icon="😴", time_hhmm=bedtime, text="שינה",
            )
        )

    events.sort(key=lambda event: event.sort_minutes)
    return events


def _looks_like_time(value: str | None) -> bool:
    return bool(value) and len(value) == 5 and value[2] == ":" and value[:2].isdigit()


def format_remaining_day_lines(events: list[TimelineEvent]) -> list[str]:
    """The user-facing timeline block. No prose below it — the timeline IS
    the state (TASK-59 requirement 7)."""
    if not events:
        return []
    return ["המשך היום:", *[event.line() for event in events]]


async def active_planned_meals(db: Any, user_id: int, now: datetime | None = None) -> tuple[dict[str, Any], ...]:
    """B12 planned meals that are still effectively planned (their durable
    lifecycle filters consumed/expired entries out of the timeline)."""
    from noam_coach.services import daily_state
    from noam_coach.services.daily_flags_cas import get_daily_flags_with_revision
    from noam_coach.services.next_meal import planned_meal_view_status

    local_now = (now or datetime.now(TZ)).astimezone(TZ)
    day = await daily_state.coaching_day_key(db, user_id, local_now)
    flags, _revision = await get_daily_flags_with_revision(db, user_id, day)
    planned = flags.get("next_meal_planned") or []
    return tuple(
        meal for meal in planned
        if isinstance(meal, dict)
        and meal.get("name")
        and planned_meal_view_status(meal, local_now) == "planned"
    )
