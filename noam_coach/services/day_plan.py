"""DayPlan — the single canonical nutrition/day model (Phase 1).

Every surface that shows "today" (Today's Menu, Today's Status, the Weekly
Plan's today row) must read its meal count, slots, timeline and remaining
budget from ONE resolved ``DayPlan`` instead of each re-deriving them. This
module owns the meal-count decision; it *reads* remaining/consumed budget from
the already-canonical ``nutrition_context`` / ``daily_state`` accessors rather
than reinventing them (ledger D-F), and reads workout timing read-only from the
existing workout-nutrition context (ledger P1.1 workout_window adapter).

DayPlan does NOT choose what to eat — it produces the timeline, slots,
constraints and budgets that a later recommendation layer consumes.

Meal-count semantics (ledger D-H, product-approved):
  * ``preferred_meal_count`` — the full-day soft band. Precedence:
    explicit confirmed user preference  >  learned meal-pattern inference  >
    default. The explicit preference is a soft planning band, never forced.
  * ``selected_planned_meals`` — the count actually planned for THIS day,
    chosen as a feasible value inside the preferred band given the day so far.
  * ``consumed_meals`` — meals already completed today.
  * ``remaining_meals`` — slots left. May legitimately fall below the preferred
    minimum late in the day or after meals were consumed; any such deviation is
    stated explicitly in ``assumptions`` — never a silent fallback to 3.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# The persisted fact key for an explicit, confirmed meal-count preference.
# Value shape: {"min": int, "max": int}. A single stated value is stored as
# {"min": n, "max": n}. See persist_preferred_meal_count().
PREFERRED_MEAL_COUNT_KEY = "preferred_meal_count"

# Default full-day band used only when there is neither an explicit preference
# nor a learned meal-hours pattern.
DEFAULT_PREFERRED_MIN = 3
DEFAULT_PREFERRED_MAX = 3

# Absolute sanity clamp for any resolved count (matches the schema bound on
# models.py::typical_meals_per_day, ge=1 le=10).
MIN_MEALS = 1
MAX_MEALS = 10

# Approximate spacing between meals. Used only for late-day feasibility: a full
# waking day comfortably fits the planned band, so this reduces the remaining
# count only when little time is left before sleep.
MEAL_SPACING_HOURS = 2.5


@dataclass(frozen=True)
class MealCountBand:
    """The preferred full-day meal-count band and where it came from."""

    minimum: int
    maximum: int
    source: str  # "explicit_preference" | "learned_pattern" | "default"

    @property
    def is_range(self) -> bool:
        return self.maximum > self.minimum


@dataclass(frozen=True)
class WorkoutWindow:
    """Read-only projection of today's workout timing (adapter over the existing
    workout-nutrition context — DayPlan performs NO workout writes/resolution)."""

    planned_start: str | None  # ISO/HH:MM as provided by the source context
    minutes_until: int | None
    minutes_since: int | None
    status: str  # workout_phase value, or "none"
    completed: bool

    @classmethod
    def empty(cls) -> "WorkoutWindow":
        return cls(planned_start=None, minutes_until=None, minutes_since=None,
                   status="none", completed=False)


@dataclass(frozen=True)
class MealSlot:
    """One planned meal slot for the remaining day."""

    index: int
    time_hint: str | None       # HH:MM local, or None if not time-anchored
    role: str | None            # breakfast/lunch/dinner/snack/pre_workout/... or None
    target_calories: int | None
    target_protein: int | None


@dataclass(frozen=True)
class TimelineEvent:
    """One chronological rest-of-day event (meal, workout, sleep)."""

    time_hint: str | None
    kind: str  # "meal" | "workout" | "sleep"
    label: str


@dataclass(frozen=True)
class DayPlan:
    """The canonical resolved plan for one coaching day at one instant."""

    # identity / anchoring
    user_id: int
    coaching_date: str          # canonical coaching-day key (YYYY-MM-DD)
    as_of: str                  # the instant this plan was resolved for (ISO)
    timezone: str
    wake_time: str | None       # HH:MM local, if known
    sleep_time: str | None      # HH:MM local (bedtime), if known
    workout_window: WorkoutWindow

    # meal-count — the four distinct quantities (ledger D-H)
    preferred_meal_count: MealCountBand
    selected_planned_meals: int
    consumed_meals: int
    remaining_meals: int

    # budgets (read from canonical nutrition context — not recomputed here)
    remaining_calories: float | None
    remaining_protein: float | None
    remaining_time_until_sleep: float | None  # hours

    # structure
    meal_slots: list[MealSlot]
    daily_timeline: list[TimelineEvent]

    # explainability / provenance
    assumptions: list[str] = field(default_factory=list)
    missing_information: list[str] = field(default_factory=list)
    requires_confirmation: bool = False
    confidence: float = 1.0


# ---------------------------------------------------------------------------
# Meal-count resolution — the single canonical owner (ledger D-H).
# ---------------------------------------------------------------------------

def _clamp(n: int, lo: int = MIN_MEALS, hi: int = MAX_MEALS) -> int:
    return max(lo, min(hi, n))


def parse_preferred_band(fact_value: Any) -> tuple[int, int] | None:
    """Parse a stored ``preferred_meal_count`` fact value into (min, max).

    Accepts {"min": n, "max": m}, a bare int, or a "5-6"/"5" string. Returns
    None when the value is absent/unparseable so the caller can fall through to
    the learned pattern.
    """
    if fact_value is None:
        return None
    if isinstance(fact_value, dict):
        lo, hi = fact_value.get("min"), fact_value.get("max")
        try:
            lo_i, hi_i = int(lo), int(hi)
        except (TypeError, ValueError):
            return None
        if lo_i > hi_i:
            lo_i, hi_i = hi_i, lo_i
        return _clamp(lo_i), _clamp(hi_i)
    if isinstance(fact_value, int):
        v = _clamp(fact_value)
        return v, v
    if isinstance(fact_value, str):
        import re

        m = re.search(r"(\d+)\s*[-–—]\s*(\d+)", fact_value)
        if m:
            lo_i, hi_i = _clamp(int(m.group(1))), _clamp(int(m.group(2)))
            return (lo_i, hi_i) if lo_i <= hi_i else (hi_i, lo_i)
        m = re.search(r"\d+", fact_value)
        if m:
            v = _clamp(int(m.group(0)))
            return v, v
    return None


def resolve_preferred_band(
    *,
    explicit_fact_value: Any,
    learned_meal_hours: list[str] | None,
) -> MealCountBand:
    """Resolve the full-day preferred band with explicit > learned > default.

    The explicit confirmed preference wins outright when present. Otherwise the
    learned ``typical_meal_hours`` length is used as a single-value band. Failing
    both, the documented default. Learned hours never override an explicit
    preference (they inform slot timing elsewhere).
    """
    explicit = parse_preferred_band(explicit_fact_value)
    if explicit is not None:
        lo, hi = explicit
        return MealCountBand(minimum=lo, maximum=hi, source="explicit_preference")

    hours = [h for h in (learned_meal_hours or []) if str(h).strip()]
    if hours:
        n = _clamp(len(hours))
        return MealCountBand(minimum=n, maximum=n, source="learned_pattern")

    return MealCountBand(
        minimum=DEFAULT_PREFERRED_MIN, maximum=DEFAULT_PREFERRED_MAX, source="default"
    )


@dataclass(frozen=True)
class MealCountResolution:
    """The four resolved meal-count quantities plus the explaining assumptions."""

    preferred: MealCountBand
    selected_planned: int
    consumed: int
    remaining: int
    assumptions: list[str]


def resolve_meal_counts(
    *,
    band: MealCountBand,
    consumed_meals: int,
    hours_until_sleep: float | None,
) -> MealCountResolution:
    """Derive selected-planned / consumed / remaining from the preferred band.

    Rules (ledger D-H):
      * selected_planned defaults to the TOP of the preferred band (plan for the
        user's stated intent), clamped to feasibility.
      * remaining = selected_planned − consumed, but is additionally capped by
        late-day feasibility (roughly one meal per ~4h until sleep, min 1 when
        any time remains). remaining never goes below 0.
      * Any time remaining falls below the preferred minimum because of the hour
        or already-consumed meals, that deviation is stated in assumptions —
        never silently coerced to a fixed 3.
    """
    assumptions: list[str] = []
    consumed = max(0, int(consumed_meals))

    selected_planned = band.maximum
    if band.source == "explicit_preference":
        assumptions.append(
            f"מספר הארוחות המועדף שלך: {_band_text(band)} (העדפה מפורשת)."
            if band.is_range
            else f"מספר הארוחות המועדף שלך: {band.maximum} (העדפה מפורשת)."
        )
    elif band.source == "learned_pattern":
        assumptions.append(
            f"מספר הארוחות נלמד מהרגלי האכילה שלך (~{band.maximum} ביום)."
        )
    else:
        assumptions.append("אין העדפת מספר ארוחות מפורשת — נעשה שימוש בברירת מחדל.")

    # Feasibility: how many more meals realistically fit before sleep. Meals
    # space at roughly one per MEAL_SPACING_HOURS; a full waking day easily fits
    # the planned band, so this only reduces the count late in the day.
    plan_remaining = max(0, selected_planned - consumed)
    if hours_until_sleep is None:
        feasible_remaining = plan_remaining
    else:
        # Round up: e.g. 3h → 2 slots feasible, 1h → 1, 0h → 0.
        import math

        by_time = (
            0 if hours_until_sleep <= 0
            else max(1, math.ceil(hours_until_sleep / MEAL_SPACING_HOURS))
        )
        feasible_remaining = min(plan_remaining, by_time)
        if by_time < plan_remaining:
            assumptions.append(
                f"נותרו כ-{hours_until_sleep:.0f} שעות עד השינה — לכן פחות ארוחות "
                f"מהמכסה המלאה מציאותיות היום."
            )

    remaining = max(0, feasible_remaining)

    if consumed >= selected_planned:
        assumptions.append(
            f"כבר אכלת {consumed} ארוחות מתוך {selected_planned} המתוכננות היום."
        )
    if band.source == "explicit_preference" and remaining < band.minimum:
        assumptions.append(
            f"מספר הארוחות שנותרו ({remaining}) נמוך מהמינימום המועדף "
            f"({band.minimum}) בגלל השעה/ארוחות שכבר נאכלו."
        )

    return MealCountResolution(
        preferred=band,
        selected_planned=selected_planned,
        consumed=consumed,
        remaining=remaining,
        assumptions=assumptions,
    )


def _band_text(band: MealCountBand) -> str:
    return f"{band.minimum}–{band.maximum}" if band.is_range else str(band.maximum)


# ---------------------------------------------------------------------------
# Preference persistence (ledger D-H): store an explicit stated preference as a
# confirmed user fact. Non-destructive; readable by the builder after restart.
# ---------------------------------------------------------------------------

async def persist_preferred_meal_count(
    db: Any, user_id: int, *, minimum: int, maximum: int | None = None
) -> bool:
    """Persist an explicitly stated meal-count preference as a confirmed fact.

    Stored as {"min", "max"} (a single stated value uses min==max). Marked
    confirmed + SOURCE_USER so it outranks any learned inference. Returns True
    if the stored value changed.
    """
    import user_model

    hi = maximum if maximum is not None else minimum
    lo, hi = _clamp(int(minimum)), _clamp(int(hi))
    if lo > hi:
        lo, hi = hi, lo
    return await user_model.set_fact(
        db,
        user_id,
        PREFERRED_MEAL_COUNT_KEY,
        {"min": lo, "max": hi},
        source=user_model.SOURCE_USER,
        confirmed=True,
    )


async def read_preferred_meal_count_fact(db: Any, user_id: int) -> Any:
    """Return the raw stored ``preferred_meal_count`` fact value, or None.

    Only a *confirmed* preference is decision-grade (mirrors the sleep-schedule
    policy in coaching_day) so an unconfirmed guess never becomes authoritative.
    """
    import user_model

    fact = await user_model.get_fact(db, user_id, PREFERRED_MEAL_COUNT_KEY)
    if not fact or not fact.get("confirmed"):
        return None
    return fact.get("value")


# ---------------------------------------------------------------------------
# The builder — composes ONE DayPlan from the already-canonical sources.
# Reads remaining/consumed from nutrition_context (ledger D-F: those are already
# consolidated), the coaching day from coaching_day (R-2b-safe), and workout
# timing read-only from the workout-nutrition context. It OWNS only the
# meal-count decision + slot/timeline assembly.
# ---------------------------------------------------------------------------

def _hhmm(value: Any) -> str | None:
    """Best-effort HH:MM from an ISO timestamp or an already-HH:MM string."""
    if not value or not isinstance(value, str):
        return None
    if "T" in value:
        try:
            from datetime import datetime as _dt

            return _dt.fromisoformat(value).strftime("%H:%M")
        except ValueError:
            return None
    return value[:5] if len(value) >= 5 and ":" in value else None


def _workout_window_from_context(workout_context: Any) -> WorkoutWindow:
    if workout_context is None:
        return WorkoutWindow.empty()
    phase = getattr(getattr(workout_context, "workout_phase", None), "value", None)
    planned = getattr(workout_context, "planned_workout_start", None)
    if planned is None and phase in (None, "rest_day"):
        return WorkoutWindow.empty()
    return WorkoutWindow(
        planned_start=planned,
        minutes_until=getattr(workout_context, "minutes_until_workout", None),
        minutes_since=getattr(workout_context, "minutes_since_workout", None),
        status=phase or "none",
        completed=bool(
            getattr(workout_context, "minutes_since_workout", None) is not None
        ),
    )


def _sleep_wake_from_profile(profile: dict[str, Any]) -> tuple[str | None, str | None]:
    """Read wake/bedtime for display, tolerating both fact shapes (R-2b)."""
    sleep = (profile or {}).get("sleep") or {}
    bedtime = sleep.get("bedtime") or sleep.get("typical_bedtime")
    wake = sleep.get("wake_time") or sleep.get("typical_wake_time")
    return _hhmm(wake), _hhmm(bedtime)


async def build_day_plan(
    db: Any,
    user_id: int,
    *,
    as_of: "Any" = None,
    nutrition_context: Any | None = None,
    workout_context: Any | None = None,
) -> DayPlan:
    """Resolve the canonical DayPlan for ``user_id`` at ``as_of``.

    ``as_of`` (a tz-aware datetime) pins the effective instant for deterministic
    tests and the Weekly-Plan today-projection. Callers that already built a
    ``NutritionContext`` / ``WorkoutNutritionContext`` for this request may pass
    them to avoid duplicate DB reads; otherwise the builder resolves them.
    """
    from noam_coach.services.coaching_day import resolve_coaching_day

    if nutrition_context is None:
        from noam_coach.services.nutrition_context import build_nutrition_context

        nutrition_context = await build_nutrition_context(
            db, user_id, "day_plan", now=as_of, local_now=as_of
        )
    if workout_context is None:
        from noam_coach.services.next_meal import build_workout_nutrition_context

        workout_context = await build_workout_nutrition_context(db, user_id, now=as_of)

    coaching = await resolve_coaching_day(db, user_id, local_now=as_of)

    # meal-count: explicit-confirmed preference > learned pattern > default
    explicit = await read_preferred_meal_count_fact(db, user_id)
    learned_hours = list(getattr(nutrition_context, "usual_meal_times", []) or [])
    band = resolve_preferred_band(
        explicit_fact_value=explicit, learned_meal_hours=learned_hours
    )
    # consumed count = today's reported meals already resolved on the context
    # (canonical: nutrition_context._reported_meals) — no extra DB read.
    consumed = len(getattr(nutrition_context, "reported_meals", []) or [])
    hours_until_sleep = getattr(nutrition_context, "hours_until_sleep", None)
    counts = resolve_meal_counts(
        band=band, consumed_meals=consumed, hours_until_sleep=hours_until_sleep
    )

    from noam_coach.services.nutrition_context import _routine_profile

    profile = await _routine_profile(db, user_id)
    wake_time, sleep_time = _sleep_wake_from_profile(profile)

    workout_window = _workout_window_from_context(workout_context)

    # slots + timeline from the remaining count, anchored on learned hours.
    slots = _build_slots(counts, nutrition_context, learned_hours)
    timeline = _build_timeline(slots, workout_window, sleep_time)

    assumptions = list(counts.assumptions)
    missing: list[str] = []
    if band.source == "default":
        missing.append("preferred_meal_count")
    if coaching.rollover_reason == "calendar_midnight":
        missing.append("sleep_schedule")

    return DayPlan(
        user_id=user_id,
        coaching_date=coaching.day_key,
        as_of=(as_of.isoformat() if as_of is not None else coaching.day_key),
        timezone=getattr(nutrition_context, "timezone", coaching.day_key),
        wake_time=wake_time,
        sleep_time=sleep_time,
        workout_window=workout_window,
        preferred_meal_count=band,
        selected_planned_meals=counts.selected_planned,
        consumed_meals=counts.consumed,
        remaining_meals=counts.remaining,
        remaining_calories=getattr(nutrition_context, "remaining_calories", None),
        remaining_protein=getattr(nutrition_context, "remaining_protein", None),
        remaining_time_until_sleep=hours_until_sleep,
        meal_slots=slots,
        daily_timeline=timeline,
        assumptions=assumptions,
        missing_information=missing,
        requires_confirmation=band.source == "default",
        confidence=_confidence_for(band, coaching.rollover_reason),
    )


def _confidence_for(band: MealCountBand, rollover_reason: str) -> float:
    c = 1.0
    if band.source == "learned_pattern":
        c -= 0.15
    elif band.source == "default":
        c -= 0.35
    if rollover_reason == "calendar_midnight":
        c -= 0.1
    return round(max(0.0, c), 2)


def _build_slots(
    counts: MealCountResolution,
    nutrition_context: Any,
    learned_hours: list[str],
) -> list[MealSlot]:
    """Build ``remaining`` slots, distributing remaining budget evenly and
    anchoring times on learned meal hours when available."""
    n = counts.remaining
    if n <= 0:
        return []
    rem_cal = getattr(nutrition_context, "remaining_calories", None)
    rem_prot = getattr(nutrition_context, "remaining_protein", None)
    per_cal = int(rem_cal / n) if isinstance(rem_cal, (int, float)) and rem_cal > 0 else None
    per_prot = int(rem_prot / n) if isinstance(rem_prot, (int, float)) and rem_prot > 0 else None

    # anchor the LAST n learned hours (the remaining part of the day)
    hint_hours = [_hhmm(h) for h in learned_hours][-n:] if learned_hours else []
    slots: list[MealSlot] = []
    for i in range(n):
        slots.append(
            MealSlot(
                index=i,
                time_hint=hint_hours[i] if i < len(hint_hours) else None,
                role=None,
                target_calories=per_cal,
                target_protein=per_prot,
            )
        )
    return slots


def _build_timeline(
    slots: list[MealSlot],
    workout_window: WorkoutWindow,
    sleep_time: str | None,
) -> list[TimelineEvent]:
    """Chronological rest-of-day events: meals, the upcoming workout, sleep."""
    events: list[TimelineEvent] = []
    for slot in slots:
        events.append(TimelineEvent(time_hint=slot.time_hint, kind="meal", label="ארוחה"))
    if workout_window.planned_start and not workout_window.completed:
        events.append(
            TimelineEvent(
                time_hint=_hhmm(workout_window.planned_start),
                kind="workout",
                label="אימון",
            )
        )
    if sleep_time:
        events.append(TimelineEvent(time_hint=sleep_time, kind="sleep", label="שינה"))

    def _key(ev: TimelineEvent) -> str:
        return ev.time_hint or "99:99"

    return sorted(events, key=_key)
