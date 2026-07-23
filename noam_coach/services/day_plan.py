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
