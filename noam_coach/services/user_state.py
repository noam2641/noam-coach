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
started at 20:17. This module does not replace ``select_todays_workout_code``
(it answers a narrower, orthogonal question — which A/B/C code is today's
split — not phase) but it DOES become the new canonical place new code
resolves "workout state" from, and ``next_meal.py`` now sources its workout
phase from here instead of recomputing it inline. The resolver also now
reconciles HealthKit-imported workouts (``_healthkit_session_candidate``) as
an ACTUAL-rank source, closing the gap where ``workout_completed_today`` knew
about HealthKit workouts but this resolver did not.

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
from noam_coach.services.precedence import select_highest_precedence
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

# --- Temporal-validity policy (REC-ARCH-01 corrective pass 3) ---------------
#
# Rank alone ("ACTUAL always outranks EXPLICIT") is not sufficient: a
# candidate must also still be CURRENT to compete. These are product policy
# thresholds, not physiological or technical constants — they encode "how
# long can a same-day signal go unconfirmed before we stop trusting it as
# still-true" and are picked to comfortably cover a real workout's duration
# plus a reasonable margin, not derived from any measured distribution.
#
# An explicit self-report ("completed"/"during"/"cancelled") older than this
# no longer counts as fresh current-day input — it is dropped as a candidate
# entirely (not merely down-ranked) so a stale tap from hours ago cannot mask
# whatever PLAN/ROUTINE evidence is actually current now. Chosen as 6 hours:
# generous enough that a normal single-session clarification is never
# spuriously discarded mid-day, short enough that "completed" tapped in the
# morning cannot still be read as literally true state by evening.
STALE_EXPLICIT_CLARIFICATION_MAX_HOURS = 6

# A ``sessions`` row can be left ``status='active'`` forever if the user
# never taps "finish" (crash, app killed, forgot) — see
# ``_active_session_candidate``. No real workout plausibly runs this long, so
# past this bound an "active" row is evidence of an abandoned/never-closed
# session, not evidence that a workout is happening right now. Matches the
# existing POST_WORKOUT_LATER cutoff used elsewhere in this module for "still
# same-session-relevant" so the whole module uses one consistent notion of
# "how long is too long for same-session same-day evidence".
STALE_ACTIVE_SESSION_MAX_HOURS = 6


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
    # ``meals.fat`` is a real, always-populated (NOT NULL) column — carried
    # through here so downstream consumers (meal_timing's high-fat-share
    # check) read real data instead of an unnecessary "unknown".
    fat: float = 0.0
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
    # Today's planned session payload (exercises/sets/minutes), independent of
    # which source actually won ``workout`` — see ``get_planned_session_for_today``.
    # ``None`` when no active workout plan has a session for today.
    session_plan: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Workout state resolution — ACTUAL > EXPLICIT CURRENT-DAY INPUT > PLAN > ROUTINE
#
# This is the canonical resolver other call sites should migrate to over time.
# ``ui.py::select_todays_workout_code`` is left as its own narrow helper: it
# answers "which A/B/C code should today's split be" (a code-cycle selection
# question), not phase — see its own docstring for the one piece of overlap
# (excluding already-done-today codes) it still computes independently.
# ``daily_state.workout_completed_today`` now delegates to this resolver
# (see that function) instead of re-deriving its own sessions/health OR.
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


async def get_planned_session_for_today(db: Any, user_id: int, now: datetime) -> dict[str, Any] | None:
    """Public accessor for today's planned session payload (exercises, sets,
    minutes), independent of whether it currently outranks other sources.

    Used by ``WorkoutDecisionContext``/``meal_timing`` consumers that need the
    session's *content* (for workout-demand estimation) even when the
    resolved ``WorkoutState`` itself came from a different source (e.g. an
    active session already outranks the plan for phase purposes, but the
    plan's exercise list is still the right source for "how demanding is
    today's session").
    """
    workout_plan = await planning.get_active_plan(db, user_id, "workout")
    return _planned_session_for_today(workout_plan, now)


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


async def _explicit_clarification_candidate(
    explicit: str, *, flags: dict[str, Any], now: datetime
) -> WorkoutState | None:
    """The explicit "during"/"completed"/"cancelled" clarification, if any.

    Deliberately does NOT include "later" — "later" is not a claim about an
    actual event, it is a hint about how to read a *planned* session (see
    ``_planned_session_candidate``), so it stays folded into the PLAN
    candidate rather than becoming its own top-level EXPLICIT candidate.

    Temporal validity (REC-ARCH-01 pass 3): ``next_meal_workout_status`` is
    written alongside ``next_meal_workout_status_at`` (see
    ``next_meal.save_next_meal_workout_status``), but until this fix the
    timestamp was written and never read anywhere — an explicit clarification
    was treated as eternally valid at its fixed EXPLICIT rank no matter how
    old it was. A clarification older than
    ``STALE_EXPLICIT_CLARIFICATION_MAX_HOURS`` is no longer offered as a
    candidate at all (not merely down-ranked) — see that constant's docstring
    for the policy reasoning. When the timestamp is missing entirely (should
    not happen for anything written via the current save path, but the field
    predates this fix and old rows may lack it), the clarification is treated
    as valid rather than guessed-stale — we do not invent staleness from
    absent data, only from data that positively shows the input is old.
    """
    written_at = _parse_dt(flags.get("next_meal_workout_status_at"))
    if written_at is not None:
        age_hours = (now - written_at).total_seconds() / 3600
        if age_hours > STALE_EXPLICIT_CLARIFICATION_MAX_HOURS:
            return None
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
    return None


async def _active_session_candidate(db: Any, user_id: int, now: datetime) -> WorkoutState | None:
    """A ``sessions`` row still ``status='active'`` — i.e. genuinely in
    progress right now, structurally the strongest possible evidence.

    Temporal validity (REC-ARCH-01 pass 3): ``_active_session``'s query has
    no date bound at all, and nothing elsewhere in the app auto-closes an
    abandoned session (confirmed by code inspection — no TTL/cleanup job
    touches ``sessions.status``), so a session the user started and never
    explicitly finished (crash, forgot to tap "finish") can sit at
    ``status='active'`` indefinitely. Before this fix that row would win
    ``DURING_WORKOUT`` at ``ACTUAL_CURRENT_DAY_EVENT`` rank forever — able to
    permanently mask a real completed session or HealthKit import from
    today, and the tie-break (``active`` candidate is gathered before
    ``completed``/``healthkit`` candidates) always favored it on top of that.
    Past ``STALE_ACTIVE_SESSION_MAX_HOURS`` (no real workout runs this long)
    this candidate is dropped entirely rather than trusted as current truth,
    so a same-day completed/HealthKit session is free to win instead.
    """
    active = await _active_session(db, user_id)
    if not active:
        return None
    started = _parse_dt(active.get("started_at"))
    if started is not None:
        age_hours = (now - started).total_seconds() / 3600
        if age_hours > STALE_ACTIVE_SESSION_MAX_HOURS:
            return None
    return WorkoutState(
        phase=WorkoutPhase.DURING_WORKOUT,
        source="active_session",
        label="יש אימון פעיל כרגע.",
        actual_start=started,
    )


async def _closed_session_candidate(db: Any, user_id: int, now: datetime) -> WorkoutState | None:
    closed = await daily_state.latest_closed_session_today(db, user_id, now=now)
    if not closed:
        return None
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


async def _healthkit_workout_today(db: Any, user_id: int, now: datetime) -> dict[str, Any] | None:
    start, end = daily_state.local_day_bounds_utc(now)
    return await db.fetch_one(
        """
        SELECT *
        FROM health
        WHERE user_id=?
          AND sample_type='workout'
          AND start_time>=?
          AND start_time<?
        ORDER BY start_time DESC, id DESC
        LIMIT 1
        """,
        (user_id, start, end),
    )


async def _healthkit_session_candidate(db: Any, user_id: int, now: datetime) -> WorkoutState | None:
    """An imported HealthKit workout sample for today, as an ACTUAL event.

    Closes the gap where ``daily_state.workout_completed_today`` already
    OR'd in HealthKit workouts but this resolver only ever looked at bot
    ``sessions`` rows — a user who logged a workout purely via HealthKit
    import (no bot session row) previously read as "no workout evidence"
    here while ``workout_completed_today`` correctly said "yes, done".
    """
    sample = await _healthkit_workout_today(db, user_id, now)
    if not sample:
        return None
    started = _parse_dt(sample.get("start_time"))
    ended = _parse_dt(sample.get("end_time")) or started
    minutes_since = int((now - ended).total_seconds() // 60) if ended else None
    if minutes_since is not None and minutes_since < 0:
        # A HealthKit sample can be imported with a future/still-syncing end
        # time; never present that as a negative "minutes since".
        minutes_since = None
    if minutes_since is not None and minutes_since <= 90:
        phase = WorkoutPhase.POST_WORKOUT_IMMEDIATE
    elif minutes_since is not None and minutes_since <= 360:
        phase = WorkoutPhase.POST_WORKOUT_LATER
    else:
        phase = WorkoutPhase.WORKOUT_COMPLETED_EARLIER
    return WorkoutState(
        phase=phase,
        source="healthkit_session",
        label="אימון יובא מ-HealthKit היום.",
        actual_start=started,
        actual_end=ended,
        minutes_since=minutes_since,
    )


async def has_actual_workout_completion_evidence_today(db: Any, user_id: int, now: datetime) -> bool:
    """True only if a workout ACTUALLY completed today (bot session or
    HealthKit import) — never true from a plan, routine, or unverified
    explicit self-report alone.

    This is the strict-evidence subset of ``resolve_workout_state``'s ACTUAL
    tier: ``daily_state.workout_completed_today`` delegates to this (rather
    than to the full resolver) precisely because its own contract is
    "actually done today", deliberately excluding explicit-only claims — see
    that function's docstring. Sharing this helper still closes the
    duplicate-query gap (both used to run their own independent
    sessions/health queries for the same fact) without changing either
    function's documented behavior.
    """
    closed = await _closed_session_candidate(db, user_id, now)
    if closed is not None and closed.phase != WorkoutPhase.WORKOUT_CANCELLED:
        return True
    healthkit = await _healthkit_session_candidate(db, user_id, now)
    return healthkit is not None


async def _planned_session_candidate(
    db: Any, user_id: int, now: datetime, *, explicit: str, flags: dict[str, Any]
) -> WorkoutState | None:
    workout_plan = await planning.get_active_plan(db, user_id, "workout")
    planned = _planned_session_for_today(workout_plan, now)
    if not planned:
        return None
    start_time = _parse_hhmm(planned.get("time")) or time(18, 0)
    minutes = int(planned.get("minutes") or 60)
    start = datetime.combine(now.date(), start_time, tzinfo=TZ)
    end = start + timedelta(minutes=minutes)
    phase, minutes_until, minutes_since = _phase_from_times(now, start, end)
    # Temporal validity for "later" (REC-ARCH-01 pass 3): "later" is scoped to
    # today's flags row already (cross-day leakage is structurally impossible
    # — flags are looked up per local_day), but WITHIN today it can still go
    # stale: tapped at 09:00, unfollowed-up, and by 20:00 it should not keep
    # reinterpreting a long-passed plan window as "still near". Same policy
    # bound as the explicit-clarification staleness check, for one consistent
    # notion of "how old is too old" across this module. Missing timestamp
    # (rows written before this fix) is treated as valid, not guessed-stale.
    later_is_stale = False
    if explicit == "later":
        later_written_at = _parse_dt(flags.get("next_meal_workout_status_at"))
        if later_written_at is not None:
            later_age_hours = (now - later_written_at).total_seconds() / 3600
            later_is_stale = later_age_hours > STALE_EXPLICIT_CLARIFICATION_MAX_HOURS
    if explicit == "later" and not later_is_stale and phase in {
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


async def _routine_pattern_candidate(db: Any, user_id: int, now: datetime) -> WorkoutState | None:
    routine_pattern = await db.fetch_one("SELECT profile FROM routine_profile WHERE user_id=?", (user_id,))
    if not routine_pattern:
        return None
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
    return None


_NO_EVIDENCE_STATE = WorkoutState(
    phase=WorkoutPhase.REST_DAY,
    source="no_workout_evidence",
    label="לא נמצא אימון מתוכנן או פעיל היום.",
)


async def resolve_workout_state(
    db: Any,
    user_id: int,
    now: datetime,
    *,
    daily_flags: dict[str, Any] | None = None,
) -> WorkoutState:
    """Resolve today's workout state: ACTUAL > EXPLICIT INPUT > PLAN > ROUTINE.

    Gathers one candidate ``WorkoutState`` per source that has evidence, then
    hands the whole list to ``precedence.select_highest_precedence`` — the
    ranking is never re-derived here as a manual if/elif chain, so this
    resolver's effective behavior cannot silently drift from the policy
    declared in ``precedence.py`` (a source missing from
    ``WORKOUT_SOURCE_RANK`` fails loudly instead of landing at an
    unaudited position).

    Every source is queried unconditionally (not short-circuited) precisely
    so an ACTUAL event discovered later in source order (e.g. a session that
    is active right now) can still outrank an EXPLICIT clarification that
    happens to have been set earlier in the day, or a PLAN that has not
    started — that ordering bug (explicit input short-circuiting before
    actual current-day events were even checked) is what this rewrite fixes.

    Temporal validity (REC-ARCH-01 pass 3): rank alone is not sufficient — a
    candidate must also still be CURRENT. Two mechanisms enforce this before
    a candidate ever reaches ``select_highest_precedence``:
      * ``_explicit_clarification_candidate`` / the "later" branch of
        ``_planned_session_candidate`` drop an explicit self-report older
        than ``STALE_EXPLICIT_CLARIFICATION_MAX_HOURS`` — it is excluded as a
        candidate, not merely down-ranked, so it cannot mask newer PLAN or
        ROUTINE evidence either.
      * ``_active_session_candidate`` drops a ``status='active'`` session row
        older than ``STALE_ACTIVE_SESSION_MAX_HOURS`` — an abandoned,
        never-closed session must not permanently masquerade as "in progress
        right now".

    Tie-break note (``active_session`` vs ``completed_session`` /
    ``healthkit_session``, all ``ACTUAL_CURRENT_DAY_EVENT`` rank): once both
    candidates have passed their own validity check above, "active wins the
    tie" is not arbitrary table order — a session that is GENUINELY active
    right now (not stale) is, by definition, more current than any earlier
    same-day completion, regardless of the completed session's own
    timestamp. The gather order below (active, then closed, then healthkit)
    only matters for this already-sound tie; it is not standing in for real
    chronology.
    """
    local_day = now.date().isoformat()
    flags = daily_flags if daily_flags is not None else await _daily_flags(db, user_id, local_day)
    explicit = str(flags.get("next_meal_workout_status") or "").strip()

    candidates: list[tuple[str, WorkoutState]] = []

    active_candidate = await _active_session_candidate(db, user_id, now)
    if active_candidate is not None:
        candidates.append((active_candidate.source, active_candidate))

    closed_candidate = await _closed_session_candidate(db, user_id, now)
    healthkit_candidate = await _healthkit_session_candidate(db, user_id, now)
    # Both ``completed_session`` and ``healthkit_session`` are the same
    # ACTUAL_CURRENT_DAY_EVENT rank (a bot-tracked completion and an
    # imported HealthKit workout are equally "a real event today"), so
    # ``select_highest_precedence`` cannot itself pick between them — it only
    # knows about rank, not real chronology, and would silently favor
    # whichever is gathered first. When a user has BOTH today (e.g. a
    # bot-tracked session and a separate Apple Watch-tracked session), the
    # one that actually ended more recently is the more current fact; that
    # comparison happens here, explicitly, using real ``actual_end``
    # timestamps rather than table/gather order. A cancelled closed-session
    # candidate has no completion to compare chronologically and always
    # yields to a real HealthKit completion if one exists today.
    if closed_candidate is not None and healthkit_candidate is not None:
        if closed_candidate.phase == WorkoutPhase.WORKOUT_CANCELLED:
            candidates.append((healthkit_candidate.source, healthkit_candidate))
        elif closed_candidate.actual_end is not None and healthkit_candidate.actual_end is not None:
            more_recent = (
                closed_candidate if closed_candidate.actual_end >= healthkit_candidate.actual_end else healthkit_candidate
            )
            candidates.append((more_recent.source, more_recent))
        else:
            # One side's completion time is unknown — cannot compare
            # chronologically, so fall back to including both and letting
            # deterministic (first-seen) tie-break apply, same as before.
            candidates.append((closed_candidate.source, closed_candidate))
            candidates.append((healthkit_candidate.source, healthkit_candidate))
    else:
        if closed_candidate is not None:
            candidates.append((closed_candidate.source, closed_candidate))
        if healthkit_candidate is not None:
            candidates.append((healthkit_candidate.source, healthkit_candidate))

    explicit_candidate = await _explicit_clarification_candidate(explicit, flags=flags, now=now)
    if explicit_candidate is not None:
        candidates.append((explicit_candidate.source, explicit_candidate))

    planned_candidate = await _planned_session_candidate(db, user_id, now, explicit=explicit, flags=flags)
    if planned_candidate is not None:
        candidates.append((planned_candidate.source, planned_candidate))

    routine_candidate = await _routine_pattern_candidate(db, user_id, now)
    if routine_candidate is not None:
        candidates.append((routine_candidate.source, routine_candidate))

    candidates.append((_NO_EVIDENCE_STATE.source, _NO_EVIDENCE_STATE))

    return select_highest_precedence(candidates)


async def _consumed_meals_today(db: Any, user_id: int, now: datetime) -> tuple[ConsumedMeal, ...]:
    # daily_state.consumed_meals filters status='consumed' (COALESCE default)
    # only — never reads daily_flags' planned-meal keys, so a planned meal can
    # never be misread as consumed here.
    rows = await daily_state.consumed_meals(db, user_id, now=now, descending=True)
    meals: list[ConsumedMeal] = []
    for row in rows:
        meals.append(
            ConsumedMeal(
                name=str(row.get("name") or ""),
                calories=float(row.get("calories") or 0),
                protein=float(row.get("protein") or 0),
                fat=float(row.get("fat") or 0),
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
    session_plan = await get_planned_session_for_today(db, user_id, local_now)
    return SharedUserState(
        user_id=user_id,
        now=local_now,
        local_day=local_day,
        workout=workout,
        consumed_meals_today=consumed,
        planned_meal_titles=planned_titles,
        session_plan=session_plan,
    )
