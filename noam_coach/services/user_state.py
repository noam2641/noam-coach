"""Shared user-state layer: ONE factual snapshot, MANY domain projections.

REC-ARCH-01 — this module exists because an audit of the nutrition/training
subsystems (see the accompanying report) found several places independently
reconstructing overlapping pieces of "the user's current reality":

- ``next_meal.py`` resolved today's workout phase (planned vs. active vs.
  completed) for meal timing.
- ``noam_coach/bot/ui.py::select_todays_workout_code`` independently resolved
  "which workout is today" from a *different* source (the ``active_workout_plan``
  user_fact mirror + a code cycle) for split selection.
- ``noam_coach/services/daily_state.py::workout_completed_today`` independently
  OR'd bot sessions with HealthKit workouts into a third, boolean-only view.

None of the three shared logic, so it was possible (in principle) for one
surface to say "workout is still ahead at 20:00" while another already knew it
started at 20:17. This module does not replace any of those call sites in one
shot (they are out of scope / lower-value to migrate this pass — see the
report) but it DOES become the new canonical place new code resolves
"workout state" from, and ``next_meal.py`` now sources its workout phase from
here instead of recomputing it inline.

Design constraints (from the architecture brief this implements):

1. Four layers that are never collapsed into each other:
   - KNOWLEDGE: stable/semi-stable facts (``user_model``/``user_facts``).
   - ROUTINE: what usually happens (``routine.py`` — behavioral evidence only).
   - PLAN: what is scheduled/intended (``planning`` active plans).
   - ACTUAL: what happened today (``sessions``, ``meals`` — DB rows remain the
     source of truth; this module never persists a second copy of them).
2. ONE explicit ``now`` per snapshot — no independently-called
   ``datetime.now()`` scattered through a single decision flow.
3. A centralized precedence policy (see ``precedence.py``) instead of each
   caller re-deriving "which layer wins" ad hoc.
4. Context PROJECTORS, not a god-object: ``build_shared_state`` builds the
   facts once; ``project_workout_decision_context`` / callers derive only the
   slice a given decision needs. Two projectors reading the same shared state
   must never independently recompute a conflicting answer to the same
   question.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from enum import Enum
from typing import Any

import planning
from config import TZ
from noam_coach.services import daily_state
from noam_coach.services.weekdays import local_weekday


class WorkoutPhase(str, Enum):
    """Where today's workout stands right now, from ACTUAL > PLAN > ROUTINE."""

    REST_DAY = "rest_day"
    PRE_WORKOUT_EARLY = "pre_workout_early"
    PRE_WORKOUT_NEAR = "pre_workout_near"
    PRE_WORKOUT_IMMEDIATE = "pre_workout_immediate"
    DURING_WORKOUT = "during_workout"
    POST_WORKOUT_IMMEDIATE = "post_workout_immediate"
    POST_WORKOUT_LATER = "post_workout_later"
    WORKOUT_COMPLETED_EARLIER = "workout_completed_earlier"
    WORKOUT_CANCELLED = "workout_cancelled"
    WORKOUT_PLANNED_TIME_PASSED = "workout_planned_time_passed"
    WORKOUT_STATUS_UNKNOWN = "workout_status_unknown"


# A workout many hours away must not read as "pre-workout" for near-term
# decisions (meal timing, etc.) — beyond this window it is just later-today.
PRE_WORKOUT_WINDOW_MIN = 300  # 5 hours


@dataclass(frozen=True)
class WorkoutState:
    """The single resolved view of "today's workout" — ACTUAL > PLAN > ROUTINE.

    Every field here has explicit provenance (``source``) so a consumer can
    tell a confirmed active session from a routine-pattern guess. Consumers
    must not treat ``planned_start`` as if it were ``actual_start`` — the two
    are kept as separate fields on purpose (plan != actual, never confused).
    """

    phase: WorkoutPhase
    source: str  # "user_clarification" | "active_session" | "completed_session" |
    #                "active_workout_plan" | "routine_pattern" | "no_workout_evidence"
    label: str
    planned_start: datetime | None = None
    planned_end: datetime | None = None
    actual_start: datetime | None = None
    actual_end: datetime | None = None
    minutes_until: int | None = None
    minutes_since: int | None = None

    @property
    def is_actual(self) -> bool:
        """True when this state reflects a real event today, not a plan/guess."""
        return self.source in {"user_clarification", "active_session", "completed_session"}

    @property
    def is_future_plan(self) -> bool:
        """True only while the planned workout has neither started nor been
        overridden by an actual/explicit signal — i.e. still safe to call
        "upcoming"."""
        return self.source == "active_workout_plan" and self.phase in {
            WorkoutPhase.PRE_WORKOUT_EARLY,
            WorkoutPhase.PRE_WORKOUT_NEAR,
            WorkoutPhase.PRE_WORKOUT_IMMEDIATE,
        }


@dataclass(frozen=True)
class ConsumedMeal:
    """One meal ACTUALLY eaten today, projected from the ``meals`` table.

    Never includes planned/recommended meals — those live in daily_flags and
    are surfaced separately (see ``planned_meal_titles``) so a plan is never
    mistaken for consumption.
    """

    name: str
    calories: float
    protein: float
    eaten_at: datetime | None
    # Eating-time confidence policy (documented previously in
    # next_meal.save_chosen_meal): eaten_at is the confirmation/log moment,
    # not a guaranteed true eating instant. "logged" = photo/manual log at the
    # time of eating (best available signal); "confirmed_recommendation" = a
    # next-meal option the user tapped "confirm eaten" for, timestamped at the
    # tap, not any earlier planned slot.
    time_confidence: str = "logged"


@dataclass(frozen=True)
class SharedUserState:
    """Built ONCE per decision flow. Project domain views from this, don't
    rebuild it. ``now`` is the single snapshot instant every projector must
    use — no projector may call ``datetime.now()`` independently."""

    user_id: int
    now: datetime
    local_day: str
    workout: WorkoutState
    consumed_meals_today: tuple[ConsumedMeal, ...] = field(default_factory=tuple)
    planned_meal_titles: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Workout state resolution — ACTUAL > EXPLICIT CURRENT-DAY INPUT > PLAN > ROUTINE
#
# This is the canonical resolver other call sites should migrate to over time
# (see the architecture report for why ui.py's split-selection resolver and
# daily_state.workout_completed_today were left as-is this pass: they answer
# narrower/different questions — "which A/B/C code" and "did *something*
# happen" respectively — and migrating them is lower-value, higher-risk than
# unifying the phase resolution nutrition already depended on).
# ---------------------------------------------------------------------------


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(TZ)


def _parse_hhmm(value: Any) -> time | None:
    if not value:
        return None
    text = str(value).strip()[:5]
    try:
        hour, minute = text.split(":", 1)
        return time(int(hour), int(minute))
    except (TypeError, ValueError):
        return None


async def _daily_flags(db: Any, user_id: int, local_day: str) -> dict[str, Any]:
    row = await db.fetch_one(
        "SELECT flags FROM daily_flags WHERE user_id=? AND day=?",
        (user_id, local_day),
    )
    if not row:
        return {}
    try:
        data = json.loads(row["flags"] or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


async def _active_session(db: Any, user_id: int) -> dict[str, Any] | None:
    return await db.fetch_one(
        """
        SELECT *
        FROM sessions
        WHERE user_id=? AND status='active'
        ORDER BY started_at DESC, id DESC
        LIMIT 1
        """,
        (user_id,),
    )


def _planned_session_for_today(workout_plan: dict[str, Any] | None, now: datetime) -> dict[str, Any] | None:
    if not workout_plan:
        return None
    sessions = (workout_plan.get("payload") or {}).get("sessions") or []
    today = local_weekday(now)
    candidates = [session for session in sessions if int(session.get("weekday", -1)) == today]
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: str(item.get("time") or "23:59"))[0]


def _phase_from_times(now: datetime, start: datetime, end: datetime) -> tuple[WorkoutPhase, int | None, int | None]:
    if now < start:
        minutes_until = int((start - now).total_seconds() // 60)
        if minutes_until <= 30:
            return WorkoutPhase.PRE_WORKOUT_IMMEDIATE, minutes_until, None
        if minutes_until <= 120:
            return WorkoutPhase.PRE_WORKOUT_NEAR, minutes_until, None
        if minutes_until <= PRE_WORKOUT_WINDOW_MIN:
            return WorkoutPhase.PRE_WORKOUT_EARLY, minutes_until, None
        # Workout is later today but outside the pre-workout meal window: treat
        # the current context as a normal day (not pre-workout).
        return WorkoutPhase.REST_DAY, minutes_until, None
    if start <= now <= end:
        return WorkoutPhase.WORKOUT_STATUS_UNKNOWN, None, None
    return WorkoutPhase.WORKOUT_PLANNED_TIME_PASSED, None, int((now - end).total_seconds() // 60)


async def resolve_workout_state(
    db: Any,
    user_id: int,
    now: datetime,
    *,
    daily_flags: dict[str, Any] | None = None,
) -> WorkoutState:
    """Resolve today's workout state: ACTUAL > EXPLICIT INPUT > PLAN > ROUTINE.

    This is a straight extraction of the resolution chain that previously
    lived only in ``next_meal._workout_state`` (the richest existing workout
    state resolver per the architecture audit) — moved here so it is no
    longer nutrition-private, without changing its behavior.
    """
    local_day = now.date().isoformat()
    flags = daily_flags if daily_flags is not None else await _daily_flags(db, user_id, local_day)
    explicit = str(flags.get("next_meal_workout_status") or "").strip()

    if explicit == "during":
        return WorkoutState(
            phase=WorkoutPhase.DURING_WORKOUT,
            source="user_clarification",
            label="האימון מתבצע עכשיו לפי הדיווח שלך.",
        )
    if explicit == "completed":
        return WorkoutState(
            phase=WorkoutPhase.POST_WORKOUT_IMMEDIATE,
            source="user_clarification",
            label="דיווחת שהאימון הושלם.",
            minutes_since=0,
        )
    if explicit == "cancelled":
        return WorkoutState(
            phase=WorkoutPhase.WORKOUT_CANCELLED,
            source="user_clarification",
            label="דיווחת שהאימון בוטל היום.",
        )

    active = await _active_session(db, user_id)
    if active:
        return WorkoutState(
            phase=WorkoutPhase.DURING_WORKOUT,
            source="active_session",
            label="יש אימון פעיל כרגע.",
            actual_start=_parse_dt(active.get("started_at")),
        )

    closed = await daily_state.latest_closed_session_today(db, user_id, now=now)
    if closed:
        ended = _parse_dt(closed.get("ended_at"))
        if str(closed.get("status")) == "cancelled":
            return WorkoutState(
                phase=WorkoutPhase.WORKOUT_CANCELLED,
                source="session_status",
                label="האימון סומן כמבוטל היום.",
                actual_end=ended,
            )
        minutes_since = int((now - ended).total_seconds() // 60) if ended else None
        if minutes_since is not None and minutes_since <= 90:
            phase = WorkoutPhase.POST_WORKOUT_IMMEDIATE
        elif minutes_since is not None and minutes_since <= 360:
            phase = WorkoutPhase.POST_WORKOUT_LATER
        else:
            phase = WorkoutPhase.WORKOUT_COMPLETED_EARLIER
        return WorkoutState(
            phase=phase,
            source="completed_session",
            label="האימון היום כבר הושלם.",
            actual_start=_parse_dt(closed.get("started_at")),
            actual_end=ended,
            minutes_since=minutes_since,
        )

    workout_plan = await planning.get_active_plan(db, user_id, "workout")
    planned = _planned_session_for_today(workout_plan, now)
    if planned:
        start_time = _parse_hhmm(planned.get("time")) or time(18, 0)
        minutes = int(planned.get("minutes") or 60)
        start = datetime.combine(now.date(), start_time, tzinfo=TZ)
        end = start + timedelta(minutes=minutes)
        phase, minutes_until, minutes_since = _phase_from_times(now, start, end)
        if explicit == "later" and phase in {
            WorkoutPhase.WORKOUT_STATUS_UNKNOWN,
            WorkoutPhase.WORKOUT_PLANNED_TIME_PASSED,
        }:
            phase = WorkoutPhase.PRE_WORKOUT_NEAR
            minutes_until = None
        return WorkoutState(
            phase=phase,
            source="active_workout_plan",
            label="יש אימון מתוכנן היום לפי התוכנית.",
            planned_start=start,
            planned_end=end,
            minutes_until=minutes_until,
            minutes_since=minutes_since,
        )

    routine_pattern = await db.fetch_one("SELECT profile FROM routine_profile WHERE user_id=?", (user_id,))
    if routine_pattern:
        try:
            profile = json.loads(routine_pattern["profile"] or "{}")
            weekdays = (profile.get("workout") or {}).get("common_weekdays") or []
            if now.weekday() in [int(day) for day in weekdays]:
                return WorkoutState(
                    phase=WorkoutPhase.WORKOUT_STATUS_UNKNOWN,
                    source="routine_pattern",
                    label="יש דפוס אימונים היסטורי היום, אבל אין ראיה שאימון נקבע או בוצע.",
                )
        except (TypeError, ValueError, json.JSONDecodeError):
            pass

    return WorkoutState(
        phase=WorkoutPhase.REST_DAY,
        source="no_workout_evidence",
        label="לא נמצא אימון מתוכנן או פעיל היום.",
    )


async def _consumed_meals_today(db: Any, user_id: int, now: datetime) -> tuple[ConsumedMeal, ...]:
    rows = await daily_state.consumed_meals(db, user_id, now=now, descending=True)
    meals: list[ConsumedMeal] = []
    for row in rows:
        meals.append(
            ConsumedMeal(
                name=str(row.get("name") or ""),
                calories=float(row.get("calories") or 0),
                protein=float(row.get("protein") or 0),
                eaten_at=_parse_dt(row.get("eaten_at")),
                time_confidence="logged",
            )
        )
    return tuple(meals)


def _planned_meal_titles(flags: dict[str, Any]) -> tuple[str, ...]:
    planned = flags.get("next_meal_planned") or []
    titles: list[str] = []
    for meal in planned:
        if isinstance(meal, dict) and meal.get("name"):
            titles.append(str(meal["name"]))
    return tuple(titles)


async def build_shared_state(
    db: Any,
    user_id: int,
    *,
    now: datetime | None = None,
) -> SharedUserState:
    """Build the shared factual snapshot ONCE. Project domain views from it.

    ``now`` is resolved exactly once here; every projector downstream must
    receive and reuse this same instant (never call ``datetime.now()`` again
    within the same decision flow) so nutrition and workout decisions can
    never observe two different "current times" mid-flow.
    """
    local_now = (now or datetime.now(TZ)).astimezone(TZ)
    local_day = local_now.date().isoformat()
    flags = await _daily_flags(db, user_id, local_day)
    workout = await resolve_workout_state(db, user_id, local_now, daily_flags=flags)
    consumed = await _consumed_meals_today(db, user_id, local_now)
    planned_titles = _planned_meal_titles(flags)
    return SharedUserState(
        user_id=user_id,
        now=local_now,
        local_day=local_day,
        workout=workout,
        consumed_meals_today=consumed,
        planned_meal_titles=planned_titles,
    )
