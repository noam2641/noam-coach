"""Deterministic next-meal recommendation service.

REC-NEXT-MEAL-05 centralises "what should I eat now?" so Telegram, proactive
jobs, and the Mini App all read the same workout-aware nutrition context.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, time, timedelta, timezone
from enum import Enum
from typing import Any

import planning
import user_model
from config import SETTINGS, TZ
from helpers import esc, utc_now
from noam_coach.services import daily_state
from noam_coach.services.decision_engine import evaluate_next_meal_decision
from noam_coach.services.dietary_restrictions import (
    DietaryRestriction,
    load_restrictions_from_facts,
    validate_meal_restrictions,
)
from noam_coach.services.explainability import (
    format_remaining_calculation,
    nutrition_remaining_calculation,
)
from noam_coach.services.food_preferences import (
    merge_with_preference_restrictions,
    preference_restrictions_from_facts,
    record_food_preference_from_slots,
)
from noam_coach.services.weekdays import local_weekday


class WorkoutPhase(str, Enum):
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


@dataclass
class NutritionTotals:
    target_calories: int | None
    target_protein: int | None
    consumed_calories: int
    consumed_protein: int
    calorie_balance: int | None
    protein_balance: int | None
    calorie_overage: int
    protein_overage: int
    goal_status: str
    goal_source: str


@dataclass
class WorkoutNutritionContext:
    user_id: int
    local_now: str
    local_day: str
    nutrition: NutritionTotals
    workout_phase: WorkoutPhase
    workout_source: str
    workout_label: str
    planned_workout_start: str | None = None
    planned_workout_end: str | None = None
    actual_workout_start: str | None = None
    actual_workout_end: str | None = None
    minutes_until_workout: int | None = None
    minutes_since_workout: int | None = None
    hours_until_bedtime: float | None = None
    meals_remaining_estimate: int = 1
    recent_meal_minutes_ago: int | None = None
    recent_meal_name: str | None = None
    fasting: bool = False
    medications_today: list[str] = field(default_factory=list)
    restrictions: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)


@dataclass
class MealBudget:
    calories_min: int
    calories_max: int
    protein_min: int
    protein_max: int
    meal_size: str
    rationale: str
    # re7 P0-2: the remaining daily calories the budget MUST respect, the policy
    # mode used to build it, and whether the budget deliberately exceeds the
    # remaining balance (only with an explicit, surfaced justification).
    policy: str = "normal"
    remaining_calories: int | None = None
    allows_overage: bool = False
    overage_reason: str | None = None


@dataclass
class MealIngredient:
    food_id: str
    display_name: str
    quantity: float
    unit: str
    calories: float
    protein_g: float
    carbs_g: float | None = None
    fat_g: float | None = None
    source: str = "noam_coach_canonical_v1"
    confidence: float | None = 0.75

    def display_text(self) -> str:
        quantity = int(self.quantity) if float(self.quantity).is_integer() else round(self.quantity, 1)
        return f"{self.display_name} {quantity} {self.unit}".strip()


@dataclass
class MealOption:
    title: str
    ingredients: list[str]
    calories: int
    protein: int
    rationale: str
    substitutions: list[str] = field(default_factory=list)
    restriction_validated: bool = True
    ingredient_details: list[MealIngredient] = field(default_factory=list)
    # RE9-052/053/013: deterministic match score + whether this is the single
    # "⭐ מומלץ עבורך" option, with a one-line reason.
    score: float = 0.0
    recommended: bool = False
    recommended_reason: str = ""

    def __post_init__(self) -> None:
        if self.ingredient_details:
            self.ingredients = [ingredient.display_text() for ingredient in self.ingredient_details]
            self.calories = _rounded_sum(ingredient.calories for ingredient in self.ingredient_details)
            self.protein = _rounded_sum(ingredient.protein_g for ingredient in self.ingredient_details)


@dataclass
class NextMealRecommendation:
    context: WorkoutNutritionContext
    budget: MealBudget
    options: list[MealOption]
    needs_workout_clarification: bool = False
    notices: list[str] = field(default_factory=list)
    validation_events: list[str] = field(default_factory=list)
    decision_audit: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["context"]["workout_phase"] = self.context.workout_phase.value
        return out


def _safe_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _rounded_sum(values: Any) -> int:
    return int(round(sum(float(value or 0) for value in values)))


def _clamp_int(value: float, low: int, high: int) -> int:
    return int(max(low, min(high, round(value))))


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


_NUTRITION_FACTS: dict[str, dict[str, float | str]] = {
    "cottage_5": {"name": "קוטג' 5%", "unit": "גרם", "kcal_per_unit": 0.98, "protein_per_unit": 0.11, "carbs_per_unit": 0.03, "fat_per_unit": 0.05},
    "cucumber": {"name": "מלפפון", "unit": "גרם", "kcal_per_unit": 0.15, "protein_per_unit": 0.007, "carbs_per_unit": 0.036, "fat_per_unit": 0.001},
    "tomato": {"name": "עגבנייה", "unit": "גרם", "kcal_per_unit": 0.18, "protein_per_unit": 0.009, "carbs_per_unit": 0.039, "fat_per_unit": 0.002},
    "protein_yogurt_0": {"name": "יוגורט חלבון 0%", "unit": "גרם", "kcal_per_unit": 0.72, "protein_per_unit": 0.10, "carbs_per_unit": 0.055, "fat_per_unit": 0.001},
    "apple": {"name": "תפוח", "unit": "גרם", "kcal_per_unit": 0.52, "protein_per_unit": 0.003, "carbs_per_unit": 0.14, "fat_per_unit": 0.002},
    "egg_white": {"name": "חלבון ביצה", "unit": "יחידה", "kcal_per_unit": 17.0, "protein_per_unit": 3.6, "carbs_per_unit": 0.2, "fat_per_unit": 0.0},
    "vegetables": {"name": "ירקות", "unit": "גרם", "kcal_per_unit": 0.25, "protein_per_unit": 0.012, "carbs_per_unit": 0.05, "fat_per_unit": 0.002},
    "olive_oil": {"name": "שמן זית", "unit": "כפית", "kcal_per_unit": 40.0, "protein_per_unit": 0.0, "carbs_per_unit": 0.0, "fat_per_unit": 4.5},
    "banana": {"name": "בננה", "unit": "גרם", "kcal_per_unit": 0.89, "protein_per_unit": 0.011, "carbs_per_unit": 0.23, "fat_per_unit": 0.003},
    "protein_powder": {"name": "אבקת חלבון", "unit": "גרם", "kcal_per_unit": 4.0, "protein_per_unit": 0.78, "carbs_per_unit": 0.08, "fat_per_unit": 0.05},
    "rice_cake": {"name": "פריכית אורז", "unit": "יחידה", "kcal_per_unit": 35.0, "protein_per_unit": 0.7, "carbs_per_unit": 7.3, "fat_per_unit": 0.2},
    "turkey_breast": {"name": "חזה הודו", "unit": "גרם", "kcal_per_unit": 1.05, "protein_per_unit": 0.22, "carbs_per_unit": 0.0, "fat_per_unit": 0.02},
    "rice_cooked": {"name": "אורז מבושל", "unit": "גרם", "kcal_per_unit": 1.30, "protein_per_unit": 0.027, "carbs_per_unit": 0.28, "fat_per_unit": 0.003},
    "chicken_breast": {"name": "חזה עוף", "unit": "גרם", "kcal_per_unit": 1.65, "protein_per_unit": 0.31, "carbs_per_unit": 0.0, "fat_per_unit": 0.036},
    "lentils_cooked": {"name": "עדשים מבושלות", "unit": "גרם", "kcal_per_unit": 1.16, "protein_per_unit": 0.09, "carbs_per_unit": 0.20, "fat_per_unit": 0.004},
    "quinoa_cooked": {"name": "קינואה מבושלת", "unit": "גרם", "kcal_per_unit": 1.20, "protein_per_unit": 0.044, "carbs_per_unit": 0.21, "fat_per_unit": 0.019},
    "tahini": {"name": "טחינה", "unit": "גרם", "kcal_per_unit": 5.95, "protein_per_unit": 0.17, "carbs_per_unit": 0.21, "fat_per_unit": 0.53},
    "potato": {"name": "תפוח אדמה", "unit": "גרם", "kcal_per_unit": 0.87, "protein_per_unit": 0.019, "carbs_per_unit": 0.20, "fat_per_unit": 0.001},
    "salad": {"name": "סלט ירקות", "unit": "גרם", "kcal_per_unit": 0.22, "protein_per_unit": 0.01, "carbs_per_unit": 0.045, "fat_per_unit": 0.002},
    "wheat_tortilla": {"name": "טורטיית חיטה", "unit": "יחידה", "kcal_per_unit": 160.0, "protein_per_unit": 5.0, "carbs_per_unit": 28.0, "fat_per_unit": 4.0},
}


def _ingredient(food_id: str, quantity: float, *, display_name: str | None = None, unit: str | None = None) -> MealIngredient:
    fact = _NUTRITION_FACTS[food_id]
    qty = float(quantity)
    return MealIngredient(
        food_id=food_id,
        display_name=str(display_name or fact["name"]),
        quantity=qty,
        unit=str(unit or fact["unit"]),
        calories=round(qty * float(fact["kcal_per_unit"]), 1),
        protein_g=round(qty * float(fact["protein_per_unit"]), 1),
        carbs_g=round(qty * float(fact.get("carbs_per_unit") or 0), 1),
        fat_g=round(qty * float(fact.get("fat_per_unit") or 0), 1),
    )


def _meal_option(
    title: str,
    ingredient_details: list[MealIngredient],
    rationale: str,
    substitutions: list[str] | None = None,
) -> MealOption:
    return MealOption(
        title=title,
        ingredients=[],
        calories=0,
        protein=0,
        rationale=rationale,
        substitutions=substitutions or [],
        ingredient_details=ingredient_details,
    )


def _option_to_payload(option: MealOption) -> dict[str, Any]:
    return asdict(option)


def _option_from_payload(payload: dict[str, Any]) -> MealOption:
    ingredient_details = [
        MealIngredient(**ingredient)
        for ingredient in (payload.get("ingredient_details") or [])
        if isinstance(ingredient, dict)
    ]
    return MealOption(
        title=str(payload.get("title") or ""),
        ingredients=[str(item) for item in (payload.get("ingredients") or [])],
        calories=int(payload.get("calories") or 0),
        protein=int(payload.get("protein") or 0),
        rationale=str(payload.get("rationale") or ""),
        substitutions=[str(item) for item in (payload.get("substitutions") or [])],
        restriction_validated=bool(payload.get("restriction_validated", True)),
        ingredient_details=ingredient_details,
    )


def _local_weekday_index(local_dt: datetime) -> int:
    return local_weekday(local_dt)


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


async def save_next_meal_workout_status(
    db: Any,
    user_id: int,
    status: str,
    *,
    now: datetime | None = None,
) -> None:
    """Persist today's explicit workout clarification from a Telegram button."""
    local_now = (now or datetime.now(TZ)).astimezone(TZ)
    local_day = local_now.date().isoformat()
    flags = await _daily_flags(db, user_id, local_day)
    flags["next_meal_workout_status"] = status
    flags["next_meal_workout_status_at"] = utc_now()
    await _save_daily_flags(db, user_id, local_day, flags)


async def _save_daily_flags(db: Any, user_id: int, local_day: str, flags: dict[str, Any]) -> None:
    await db.execute(
        """
        INSERT INTO daily_flags(user_id, day, flags, created_at)
        VALUES(?, ?, ?, ?)
        ON CONFLICT(user_id, day) DO UPDATE SET flags=excluded.flags
        """,
        (user_id, local_day, json.dumps(flags, ensure_ascii=False), utc_now()),
    )


async def _today_meals(db: Any, user_id: int, now: datetime | None = None) -> list[dict[str, Any]]:
    rows = await daily_state.consumed_meals(db, user_id, now=now, descending=True)
    return [
        {
            "name": row.get("name"),
            "calories": row.get("calories"),
            "protein": row.get("protein"),
            "eaten_at": row.get("eaten_at"),
        }
        for row in rows
    ]


async def _nutrition_totals(
    db: Any,
    user_id: int,
    *,
    now: datetime | None = None,
) -> tuple[NutritionTotals, dict[str, Any] | None]:
    goal = await planning.active_goal(db, user_id)
    target_calories = None
    target_protein = None
    goal_status = "unavailable"
    goal_source = "none"
    if goal:
        cal = _safe_float(goal.get("calories"))
        protein = _safe_float(goal.get("protein"))
        if cal and cal > 0:
            target_calories = int(round(cal))
        if protein and protein > 0:
            target_protein = int(round(protein))
        goal_status = str(goal.get("status") or "active")
        goal_source = str(goal.get("source") or "goal_versions")
    else:
        target_calories = int(getattr(SETTINGS, "default_calories", 2000) or 2000)
        target_protein = int(getattr(SETTINGS, "default_protein", 140) or 140)
        goal_status = "default"
        goal_source = "settings_default"

    consumed_calories = 0
    consumed_protein = 0
    for row in await _today_meals(db, user_id, now):
        calories = _safe_float(row.get("calories"))
        protein = _safe_float(row.get("protein"))
        if calories is not None and calories > 0:
            consumed_calories += int(round(calories))
        if protein is not None and protein > 0:
            consumed_protein += int(round(protein))

    calorie_balance = None if target_calories is None else target_calories - consumed_calories
    protein_balance = None if target_protein is None else target_protein - consumed_protein
    return (
        NutritionTotals(
            target_calories=target_calories,
            target_protein=target_protein,
            consumed_calories=consumed_calories,
            consumed_protein=consumed_protein,
            calorie_balance=calorie_balance,
            protein_balance=protein_balance,
            calorie_overage=max(0, -int(calorie_balance or 0)),
            protein_overage=max(0, -int(protein_balance or 0)),
            goal_status=goal_status,
            goal_source=goal_source,
        ),
        goal,
    )


async def _recent_meal(db: Any, user_id: int, now: datetime) -> tuple[str | None, int | None]:
    rows = await _today_meals(db, user_id, now)
    if not rows:
        return None, None
    row = rows[0]
    eaten_at = _parse_dt(row.get("eaten_at"))
    if not eaten_at:
        return str(row.get("name") or ""), None
    minutes = max(0, int((now - eaten_at).total_seconds() // 60))
    return str(row.get("name") or ""), minutes


async def _restrictions(db: Any, user_id: int) -> list[DietaryRestriction]:
    diet = await user_model.get_value(db, user_id, "diet_restrictions")
    allergies = await user_model.get_value(db, user_id, "allergies")
    base = load_restrictions_from_facts(
        str(diet) if diet not in (None, "", "none") else None,
        str(allergies) if allergies not in (None, "", "none") else None,
    )
    preferences = await preference_restrictions_from_facts(db, user_id)
    return merge_with_preference_restrictions(base, preferences)


async def _bedtime_hours(db: Any, user_id: int, now: datetime) -> float | None:
    sleep_fact = await user_model.get_value(db, user_id, "sleep_schedule")
    bedtime = None
    if isinstance(sleep_fact, dict):
        bedtime = sleep_fact.get("bedtime") or sleep_fact.get("typical_bedtime")
    if not bedtime:
        row = await db.fetch_one("SELECT profile FROM routine_profile WHERE user_id=?", (user_id,))
        if row:
            try:
                profile = json.loads(row["profile"] or "{}")
                bedtime = (profile.get("sleep") or {}).get("typical_bedtime")
            except (TypeError, json.JSONDecodeError):
                bedtime = None
    parsed = _parse_hhmm(bedtime) or time(23, 0)
    bed_dt = datetime.combine(now.date(), parsed, tzinfo=TZ)
    if bed_dt <= now:
        bed_dt += timedelta(days=1)
    return round((bed_dt - now).total_seconds() / 3600, 1)


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


async def _latest_closed_session_today(db: Any, user_id: int, now: datetime) -> dict[str, Any] | None:
    return await daily_state.latest_closed_session_today(db, user_id, now=now)


def _planned_session_for_today(workout_plan: dict[str, Any] | None, now: datetime) -> dict[str, Any] | None:
    if not workout_plan:
        return None
    sessions = (workout_plan.get("payload") or {}).get("sessions") or []
    today = _local_weekday_index(now)
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
        return WorkoutPhase.PRE_WORKOUT_EARLY, minutes_until, None
    if start <= now <= end:
        return WorkoutPhase.WORKOUT_STATUS_UNKNOWN, None, None
    return WorkoutPhase.WORKOUT_PLANNED_TIME_PASSED, None, int((now - end).total_seconds() // 60)


async def _workout_state(
    db: Any,
    user_id: int,
    now: datetime,
    flags: dict[str, Any],
) -> dict[str, Any]:
    explicit = str(flags.get("next_meal_workout_status") or "").strip()
    if explicit == "during":
        return {
            "phase": WorkoutPhase.DURING_WORKOUT,
            "source": "user_clarification",
            "label": "האימון מתבצע עכשיו לפי הדיווח שלך.",
        }
    if explicit == "completed":
        return {
            "phase": WorkoutPhase.POST_WORKOUT_IMMEDIATE,
            "source": "user_clarification",
            "label": "דיווחת שהאימון הושלם.",
            "minutes_since": 0,
        }
    if explicit == "cancelled":
        return {
            "phase": WorkoutPhase.WORKOUT_CANCELLED,
            "source": "user_clarification",
            "label": "דיווחת שהאימון בוטל היום.",
        }

    active = await _active_session(db, user_id)
    if active:
        return {
            "phase": WorkoutPhase.DURING_WORKOUT,
            "source": "active_session",
            "label": "יש אימון פעיל כרגע.",
            "actual_start": _parse_dt(active.get("started_at")),
        }

    closed = await _latest_closed_session_today(db, user_id, now)
    if closed:
        ended = _parse_dt(closed.get("ended_at"))
        if str(closed.get("status")) == "cancelled":
            return {
                "phase": WorkoutPhase.WORKOUT_CANCELLED,
                "source": "session_status",
                "label": "האימון סומן כמבוטל היום.",
                "actual_end": ended,
            }
        minutes_since = int((now - ended).total_seconds() // 60) if ended else None
        if minutes_since is not None and minutes_since <= 90:
            phase = WorkoutPhase.POST_WORKOUT_IMMEDIATE
        elif minutes_since is not None and minutes_since <= 360:
            phase = WorkoutPhase.POST_WORKOUT_LATER
        else:
            phase = WorkoutPhase.WORKOUT_COMPLETED_EARLIER
        return {
            "phase": phase,
            "source": "completed_session",
            "label": "האימון היום כבר הושלם.",
            "actual_start": _parse_dt(closed.get("started_at")),
            "actual_end": ended,
            "minutes_since": minutes_since,
        }

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
        return {
            "phase": phase,
            "source": "active_workout_plan",
            "label": "יש אימון מתוכנן היום לפי התוכנית.",
            "planned_start": start,
            "planned_end": end,
            "minutes_until": minutes_until,
            "minutes_since": minutes_since,
        }

    routine_pattern = await db.fetch_one("SELECT profile FROM routine_profile WHERE user_id=?", (user_id,))
    if routine_pattern:
        try:
            profile = json.loads(routine_pattern["profile"] or "{}")
            weekdays = ((profile.get("workout") or {}).get("common_weekdays") or [])
            if now.weekday() in [int(day) for day in weekdays]:
                return {
                    "phase": WorkoutPhase.WORKOUT_STATUS_UNKNOWN,
                    "source": "routine_pattern",
                    "label": "יש דפוס אימונים היסטורי היום, אבל אין ראיה שאימון נקבע או בוצע.",
                }
        except (TypeError, ValueError, json.JSONDecodeError):
            pass

    return {
        "phase": WorkoutPhase.REST_DAY,
        "source": "no_workout_evidence",
        "label": "לא נמצא אימון מתוכנן או פעיל היום.",
    }


def _meals_remaining(hours_until_bedtime: float | None, recent_minutes: int | None, flags: dict[str, Any]) -> int:
    assumption = str(flags.get("next_meal_count_assumption") or "").strip()
    if assumption in {"1", "last", "one"}:
        return 1
    if assumption in {"2", "two"}:
        return 2
    if assumption in {"3", "three"}:
        return 3
    if hours_until_bedtime is None:
        estimate = 2
    elif hours_until_bedtime <= 3:
        estimate = 1
    elif hours_until_bedtime <= 7:
        estimate = 2
    else:
        estimate = 3
    if recent_minutes is not None and recent_minutes < 75:
        estimate = min(estimate, 1)
    return max(1, min(3, estimate))


async def build_workout_nutrition_context(
    db: Any,
    user_id: int,
    *,
    now: datetime | None = None,
) -> WorkoutNutritionContext:
    local_now = (now or datetime.now(TZ)).astimezone(TZ)
    local_day = local_now.date().isoformat()
    flags = await _daily_flags(db, user_id, local_day)
    nutrition, _goal = await _nutrition_totals(db, user_id, now=local_now)
    workout = await _workout_state(db, user_id, local_now, flags)
    recent_name, recent_minutes = await _recent_meal(db, user_id, local_now)
    hours_until_bedtime = await _bedtime_hours(db, user_id, local_now)
    restrictions = await _restrictions(db, user_id)
    assumptions: list[str] = []
    if nutrition.goal_status in {"default", "unavailable"}:
        assumptions.append("אין יעד פעיל מאושר, לכן נעשה שימוש בערכי ברירת מחדל זהירים.")
    elif nutrition.goal_status == "active_provisional":
        assumptions.append("היעד זמני, לכן ההמלצה נשארת שמרנית.")
    if workout["source"] == "routine_pattern":
        assumptions.append("דפוס אימונים היסטורי אינו הוכחה שאימון מתקיים היום.")

    return WorkoutNutritionContext(
        user_id=user_id,
        local_now=local_now.isoformat(),
        local_day=local_day,
        nutrition=nutrition,
        workout_phase=workout["phase"],
        workout_source=str(workout["source"]),
        workout_label=str(workout["label"]),
        planned_workout_start=workout.get("planned_start").isoformat() if workout.get("planned_start") else None,
        planned_workout_end=workout.get("planned_end").isoformat() if workout.get("planned_end") else None,
        actual_workout_start=workout.get("actual_start").isoformat() if workout.get("actual_start") else None,
        actual_workout_end=workout.get("actual_end").isoformat() if workout.get("actual_end") else None,
        minutes_until_workout=workout.get("minutes_until"),
        minutes_since_workout=workout.get("minutes_since"),
        hours_until_bedtime=hours_until_bedtime,
        meals_remaining_estimate=_meals_remaining(hours_until_bedtime, recent_minutes, flags),
        recent_meal_minutes_ago=recent_minutes,
        recent_meal_name=recent_name,
        fasting=bool(flags.get("fasting")),
        medications_today=[
            str(item)
            for item in (flags.get("medications") or [])
            if str(item).strip()
        ],
        restrictions=[restriction.canonical_id for restriction in restrictions],
        assumptions=assumptions,
    )


# re7 P0-2: a near-workout meal may justifiably exceed the plain remaining
# balance, but only by a bounded amount and always with a surfaced reason.
_WORKOUT_OVERAGE_PHASES = {
    WorkoutPhase.PRE_WORKOUT_IMMEDIATE,
    WorkoutPhase.PRE_WORKOUT_NEAR,
    WorkoutPhase.POST_WORKOUT_IMMEDIATE,
    WorkoutPhase.POST_WORKOUT_LATER,
    WorkoutPhase.DURING_WORKOUT,
}
# Below this many remaining calories we treat the day as "low remaining" and
# build a protein-dense, balance-fitting budget instead of a normal meal.
_LOW_REMAINING_THRESHOLD = 350
# A budget cap is never allowed to fall below this floor unless there is a real
# positive remaining balance smaller than it (then the balance is the cap).
_MIN_MEAL_FLOOR = 120


def allocate_next_meal_budget(
    context: WorkoutNutritionContext,
    *,
    allow_overage: bool = False,
) -> MealBudget:
    """Build a meal budget that treats remaining calories as a binding cap.

    The maximum calories never exceed the remaining daily balance, except:
      * a bounded, explicitly-explained overage near a workout, or
      * an overage the user explicitly asked for (``allow_overage``).
    When the day is at/over target, no "full meal that meets the goal" is built;
    instead a light-recovery or explicit-overage budget is returned.
    """
    nutrition = context.nutrition
    balance = nutrition.calorie_balance
    remaining_cal = int(balance) if balance is not None else None
    remaining_protein = max(0, int(nutrition.protein_balance or 0))
    meals_left = max(1, context.meals_remaining_estimate)
    base_protein = remaining_protein / meals_left if remaining_protein else 22
    phase = context.workout_phase

    def _cap(value: int, *, allow: bool, reason: str | None) -> int:
        """Clamp a desired calorie ceiling to the remaining balance."""
        if remaining_cal is None:
            return value  # unknown balance: no numeric cap to enforce
        if allow:
            # Bounded overage: at most +35% of remaining (min +250) over balance.
            ceiling = max(remaining_cal, 0) + max(250, int(max(remaining_cal, 0) * 0.35))
            return min(value, ceiling)
        return min(value, max(remaining_cal, 0))

    # ---- At / over target, or no usable positive balance -------------------
    if remaining_cal is not None and remaining_cal <= 0:
        if allow_overage:
            target = max(250, int(max(0, -remaining_cal) and 300 or 350))
            return MealBudget(
                calories_min=150,
                calories_max=target,
                protein_min=_clamp_int(base_protein, 18, 35),
                protein_max=_clamp_int(base_protein + 12, 25, 45),
                meal_size="explicit_overage",
                rationale="אישרת חריגה מבוקרת — זו תוספת קטנה מעבר ליעד, עם דגש על חלבון.",
                policy="explicit_overage",
                remaining_calories=remaining_cal,
                allows_overage=True,
                overage_reason="המשתמש ביקש חריגה מבוקרת",
            )
        return MealBudget(
            calories_min=0,
            calories_max=max(0, min(150, remaining_cal if remaining_cal > 0 else 150)) if remaining_cal > 0 else 120,
            protein_min=_clamp_int(base_protein, 12, 30),
            protein_max=_clamp_int(base_protein + 10, 20, 40),
            meal_size="at_or_over_target",
            rationale="הגעת ליעד הקלוריות היומי. עדיף נשנוש קל מאוד או משקה דל קלוריות.",
            policy="at_or_over_target",
            remaining_calories=remaining_cal,
            allows_overage=False,
        )

    # ---- Workout-justified overage ----------------------------------------
    workout_overage = phase in _WORKOUT_OVERAGE_PHASES and remaining_cal is not None and remaining_cal < 350

    # ---- Low remaining balance: fit tightly, favour protein ----------------
    if remaining_cal is not None and 0 < remaining_cal <= _LOW_REMAINING_THRESHOLD and not workout_overage:
        return MealBudget(
            calories_min=max(0, min(120, remaining_cal - 40)),
            calories_max=remaining_cal,
            protein_min=_clamp_int(max(base_protein, 18), 15, 40),
            protein_max=_clamp_int(max(base_protein, 18) + 12, 22, 55),
            meal_size="low_remaining_protein_dense",
            rationale="נשארו מעט קלוריות להיום, אז האפשרויות נבנו כדי להיכנס ליתרה עם דגש על חלבון.",
            policy="low_remaining",
            remaining_calories=remaining_cal,
            allows_overage=False,
        )

    # ---- Normal / workout phases ------------------------------------------
    base_cal = (remaining_cal / meals_left) if (remaining_cal and remaining_cal > 0) else 350

    if phase in {WorkoutPhase.PRE_WORKOUT_IMMEDIATE, WorkoutPhase.DURING_WORKOUT}:
        desired_max = _clamp_int(min(base_cal, 320), 180, 340)
        return MealBudget(
            calories_min=min(120, desired_max),
            calories_max=_cap(desired_max, allow=workout_overage or allow_overage, reason="סמוך לאימון"),
            protein_min=8,
            protein_max=25,
            meal_size="quick_pre_workout",
            rationale="קרוב לאימון עדיף משהו קל לעיכול, בלי ארוחה כבדה ושומנית.",
            policy="workout" if workout_overage else "normal",
            remaining_calories=remaining_cal,
            allows_overage=workout_overage or allow_overage,
            overage_reason="ארוחה סמוכה לאימון" if workout_overage else None,
        )
    if phase == WorkoutPhase.PRE_WORKOUT_NEAR:
        desired_max = _clamp_int(base_cal + 120, 360, 650)
        return MealBudget(
            calories_min=min(_clamp_int(base_cal - 120, 200, 450), desired_max),
            calories_max=_cap(desired_max, allow=workout_overage or allow_overage, reason="לפני אימון"),
            protein_min=_clamp_int(base_protein, 20, 35),
            protein_max=_clamp_int(base_protein + 18, 30, 55),
            meal_size="pre_workout_meal",
            rationale="יש זמן לארוחה אמיתית לפני האימון, עם פחמימה נוחה וחלבון מתון.",
            policy="workout" if workout_overage else "normal",
            remaining_calories=remaining_cal,
            allows_overage=workout_overage or allow_overage,
            overage_reason="ארוחה לפני אימון" if workout_overage else None,
        )
    if phase in {WorkoutPhase.POST_WORKOUT_IMMEDIATE, WorkoutPhase.POST_WORKOUT_LATER}:
        desired_max = _clamp_int(base_cal + 160, 480, 780)
        return MealBudget(
            calories_min=min(_clamp_int(base_cal - 80, 250, 520), desired_max),
            calories_max=_cap(desired_max, allow=workout_overage or allow_overage, reason="אחרי אימון"),
            protein_min=_clamp_int(base_protein + 8, 30, 45),
            protein_max=_clamp_int(base_protein + 25, 40, 65),
            meal_size="post_workout_meal",
            rationale="אחרי אימון כדאי לתת עדיפות לחלבון ולארוחה משביעה.",
            policy="workout" if workout_overage else "normal",
            remaining_calories=remaining_cal,
            allows_overage=workout_overage or allow_overage,
            overage_reason="ארוחה אחרי אימון" if workout_overage else None,
        )

    if context.fasting:
        desired_max = 480
        return MealBudget(
            calories_min=min(220, _cap(desired_max, allow=allow_overage, reason=None)),
            calories_max=_cap(desired_max, allow=allow_overage, reason=None),
            protein_min=_clamp_int(base_protein, 18, 35),
            protein_max=_clamp_int(base_protein + 15, 28, 50),
            meal_size="fast_break",
            rationale="אתה מסומן בצום היום, לכן ההמלצה מניחה ארוחה עדינה לשבירת צום.",
            policy="fasting",
            remaining_calories=remaining_cal,
            allows_overage=allow_overage,
        )

    desired_max = _clamp_int(base_cal + 120, 380, 750)
    return MealBudget(
        calories_min=min(_clamp_int(base_cal - 100, 200, 520), desired_max),
        calories_max=_cap(desired_max, allow=allow_overage, reason=None),
        protein_min=_clamp_int(base_protein, 20, 40),
        protein_max=_clamp_int(base_protein + 18, 30, 60),
        meal_size="balanced_meal",
        rationale="חלוקה מאוזנת של מה שנשאר להיום לפי מספר הארוחות המשוער.",
        policy="normal",
        remaining_calories=remaining_cal,
        allows_overage=allow_overage,
    )


def _low_remaining_templates(budget: MealBudget) -> list[MealOption]:
    """Protein-dense, low-calorie options for when little budget remains."""
    del budget
    return [
        _meal_option(
            "קוטג׳ 5% עם ירקות",
            [_ingredient("cottage_5", 150), _ingredient("cucumber", 100), _ingredient("tomato", 120)],
            "צפיפות חלבון טובה בקלוריות נמוכות, נכנס ביתרה.",
            ["אם יש מגבלת חלב: 150 גרם טונה במים במקום."],
        ),
        _meal_option(
            "יוגורט חלבון 0% עם פרי קטן",
            [_ingredient("protein_yogurt_0", 200), _ingredient("apple", 120, display_name="תפוח קטן")],
            "חלבון טוב, מתוק וקל, ונשאר בתוך היתרה.",
            ["בלי חלב: פודינג חלבון על בסיס סויה."],
        ),
        _meal_option(
            "חביתת חלבונים עם ירק",
            [_ingredient("egg_white", 3), _ingredient("vegetables", 160, display_name="ירקות מוקפצים"), _ingredient("olive_oil", 1, unit="כפית")],
            "ארוחה חמה דלת קלוריות עם חלבון מדוד.",
            ["בלי ביצים: 120 גרם גבינה לבנה 5%."],
        ),
    ]


def _candidate_templates(phase: WorkoutPhase, budget: MealBudget) -> list[MealOption]:
    if budget.policy in {"low_remaining", "at_or_over_target"}:
        return _low_remaining_templates(budget)
    if phase in {WorkoutPhase.PRE_WORKOUT_IMMEDIATE, WorkoutPhase.DURING_WORKOUT}:
        return [
            _meal_option(
                "בננה ושייק חלבון קל",
                [_ingredient("banana", 120), _ingredient("protein_powder", 25)],
                "קל לעיכול ונותן אנרגיה זמינה.",
                ["אם אין שייק: יוגורט חלבון מתאים רק אם אין מגבלת חלב."],
            ),
            _meal_option(
                "פריכיות אורז עם חזה הודו",
                [_ingredient("rice_cake", 3), _ingredient("turkey_breast", 90), _ingredient("cucumber", 100)],
                "קטן, מלוח, ולא כבד לפני תנועה.",
                ["אפשר להחליף לעוף קר או טונה אם מתאים למגבלות."],
            ),
        ]
    if phase in {WorkoutPhase.POST_WORKOUT_IMMEDIATE, WorkoutPhase.POST_WORKOUT_LATER}:
        return [
            _meal_option(
                "קערת אורז ועוף",
                [_ingredient("rice_cooked", 150), _ingredient("chicken_breast", 120), _ingredient("vegetables", 150), _ingredient("olive_oil", 1)],
                "ארוחה פשוטה עם חלבון גבוה ופחמימה נוחה אחרי אימון.",
                ["אפשר להחליף אורז לתפוח אדמה או קינואה."],
            ),
            _meal_option(
                "קערת עדשים וקינואה",
                [_ingredient("lentils_cooked", 170), _ingredient("quinoa_cooked", 120), _ingredient("vegetables", 150), _ingredient("tahini", 15)],
                "אפשרות צמחית ומשביעה בלי להישען על מוצרי חלב.",
                ["אם יש מגבלת שומשום, להחליף טחינה באבוקדו קטן."],
            ),
        ]
    return [
        _meal_option(
            "צלחת עוף, תפוח אדמה וסלט",
            [_ingredient("chicken_breast", 120), _ingredient("potato", 180), _ingredient("salad", 180), _ingredient("olive_oil", 1)],
            "מאוזן, משביע, ומכסה חלבון בלי להעמיס.",
            ["אפשר להחליף עוף בטופו רק אם אין מגבלת סויה."],
        ),
        _meal_option(
            "טורטייה חלבון",
            [_ingredient("wheat_tortilla", 1), _ingredient("turkey_breast", 100), _ingredient("vegetables", 120), _ingredient("tahini", 12)],
            "מתאים כשצריך משהו מהיר ולא ארוחה כבדה.",
            ["ללא גלוטן: להחליף לטורטייה תירס או קערת אורז."],
        ),
        _meal_option(
            "קערת עדשים ואורז",
            [_ingredient("lentils_cooked", 170), _ingredient("rice_cooked", 140), _ingredient("vegetables", 150), _ingredient("olive_oil", 1)],
            "אפשרות פשוטה בלי חלב, ביצים, דגים או אגוזים.",
            ["אפשר להוסיף עוף אם אין העדפה צמחית."],
        ),
    ]

def _filter_options(
    options: list[MealOption],
    restrictions: list[DietaryRestriction],
    recent_titles: list[str] | None = None,
    budget: MealBudget | None = None,
) -> list[MealOption]:
    if not restrictions:
        return _prioritize_fresh_options(options, recent_titles)
    safe: list[MealOption] = []
    for option in options:
        items = [{"item_name": ingredient} for ingredient in option.ingredients]
        violations = validate_meal_restrictions(items, restrictions)
        preference_hit = _matches_free_text_preference([option.title, *option.ingredients], restrictions)
        if not violations and not preference_hit:
            safe.append(option)
    if len(safe) >= 2:
        return _prioritize_fresh_options(safe, recent_titles)
    # Budget-aware allergen-light fallback (never a hardcoded calorie count that
    # could exceed the remaining balance — it is fitted later by the validator).
    del budget
    fallback = _meal_option(
        "קערת אורז ועדשים פשוטה",
        [_ingredient("rice_cooked", 120), _ingredient("lentils_cooked", 150), _ingredient("vegetables", 150), _ingredient("olive_oil", 1)],
        "ברירת מחדל שממעטת באלרגנים נפוצים ומספקת בסיס מאוזן.",
        ["אם אחת מהרכיבים לא מתאימה לך, עדכן מגבלה בפרופיל לפני בחירה."],
    )
    if not validate_meal_restrictions([{"item_name": item} for item in fallback.ingredients], restrictions):
        safe.append(fallback)
    return _prioritize_fresh_options(safe, recent_titles) or options[:1]


def _prioritize_fresh_options(
    options: list[MealOption],
    recent_titles: list[str] | None,
) -> list[MealOption]:
    if not recent_titles:
        return options
    recent_keys = {_free_text_preference_key(title) for title in recent_titles if str(title).strip()}
    fresh = [
        option
        for option in options
        if _free_text_preference_key(option.title) not in recent_keys
    ]
    stale = [
        option
        for option in options
        if _free_text_preference_key(option.title) in recent_keys
    ]
    return [*fresh, *stale] if fresh else options


def _matches_free_text_preference(
    ingredients: list[str],
    restrictions: list[DietaryRestriction],
) -> bool:
    disliked = [
        _free_text_preference_key(restriction.canonical_id or restriction.user_label)
        for restriction in restrictions
        if restriction.restriction_type in {"preference", "unavailable"}
    ]
    if not disliked:
        return False
    ingredient_keys = [*(_free_text_preference_key(ingredient) for ingredient in ingredients)]
    for dislike in disliked:
        if not dislike:
            continue
        if any(dislike in ingredient or ingredient in dislike for ingredient in ingredient_keys):
            return True
    return False


def _free_text_preference_key(value: str) -> str:
    return " ".join(
        "".join(
            char if char.isalnum() or char.isspace() else " "
            for char in str(value).lower()
        ).split()
    )


def option_fingerprint(option: MealOption) -> str:
    """Canonical identity of a meal option for rejection / anti-repetition.

    Two options with the same normalised title or the same core ingredient set
    share a fingerprint so a "different" alternative is genuinely different
    (re7 P1-8), not the same dish under a slightly different name.
    """
    title_key = _free_text_preference_key(option.title)
    core = sorted(
        {
            _free_text_preference_key(_strip_quantity(ingredient))
            for ingredient in option.ingredients
            if str(ingredient).strip()
        }
    )
    return f"{title_key}|{'+'.join(core)}"


def _strip_quantity(text: str) -> str:
    """Drop quantities/units so '150 גרם קוטג׳' and 'קוטג׳' match."""
    cleaned = re.sub(r"\d+(?:[.,]\d+)?\s*(?:גרם|ג|מ\"ל|מל|כף|כפית|יחידה|יח׳)?", " ", str(text))
    return " ".join(cleaned.split())


def _ingredient_totals(option: MealOption) -> tuple[int, int]:
    return (
        _rounded_sum(ingredient.calories for ingredient in option.ingredient_details),
        _rounded_sum(ingredient.protein_g for ingredient in option.ingredient_details),
    )


def _round_quantity_by_unit(quantity: float, unit: str) -> float:
    unit_text = str(unit)
    if any(marker in unit_text for marker in ("יחידה", "פריכית")):
        return float(max(1, round(quantity)))
    if "כפית" in unit_text:
        return float(max(1, round(quantity)))
    if "גרם" in unit_text or unit_text in {"ג", "g"}:
        if quantity >= 100:
            step = 10
        elif quantity >= 30:
            step = 5
        else:
            step = 1
        return float(max(step, round(quantity / step) * step))
    return round(max(0.1, quantity), 1)


def _with_quantity(ingredient: MealIngredient, quantity: float) -> MealIngredient:
    rounded_quantity = _round_quantity_by_unit(quantity, ingredient.unit)
    base_quantity = max(float(ingredient.quantity or 1), 0.001)
    scale = rounded_quantity / base_quantity
    return MealIngredient(
        food_id=ingredient.food_id,
        display_name=ingredient.display_name,
        quantity=rounded_quantity,
        unit=ingredient.unit,
        calories=round(max(0, ingredient.calories * scale), 1),
        protein_g=round(max(0, ingredient.protein_g * scale), 1),
        carbs_g=None if ingredient.carbs_g is None else round(max(0, ingredient.carbs_g * scale), 1),
        fat_g=None if ingredient.fat_g is None else round(max(0, ingredient.fat_g * scale), 1),
        source=ingredient.source,
        confidence=ingredient.confidence,
    )


def _scale_option_ingredients(option: MealOption, scale: float) -> MealOption:
    bounded = max(0.25, min(2.0, scale))
    scaled = [
        _with_quantity(ingredient, max(0.1, ingredient.quantity * bounded))
        for ingredient in option.ingredient_details
    ]
    return _meal_option(option.title, scaled, option.rationale, list(option.substitutions))


def validate_meal_option(
    option: MealOption,
    budget: MealBudget,
    restrictions: list[DietaryRestriction],
) -> list[str]:
    """Deterministic post-generation validation. Returns a list of problems."""
    problems: list[str] = []
    if option.calories < 0 or option.protein < 0:
        problems.append("negative_values")
    if not option.ingredients:
        problems.append("missing_ingredients")
    if not option.ingredient_details:
        problems.append("missing_typed_ingredients")
    else:
        ingredient_calories, ingredient_protein = _ingredient_totals(option)
        if abs(option.calories - ingredient_calories) > 1 or abs(option.protein - ingredient_protein) > 1:
            problems.append("ingredient_total_mismatch")
        for ingredient in option.ingredient_details:
            if ingredient.quantity <= 0 or ingredient.calories < 0 or ingredient.protein_g < 0:
                problems.append("invalid_ingredient_quantity")
                break
            if ingredient.protein_g * 4 > ingredient.calories + 8:
                problems.append("implausible_ingredient_protein")
                break
        if option.protein * 4 > option.calories + 15:
            problems.append("implausible_total_protein")
        if option.calories >= 250 and option.protein * 4 > option.calories * 0.9:
            problems.append("missing_macro_room")
    # Budget cap: never exceed calories_max unless the budget explicitly allows
    # an acknowledged overage. A small rounding tolerance is permitted.
    tolerance = max(15, int(budget.calories_max * 0.05))
    if not budget.allows_overage and option.calories > budget.calories_max + tolerance:
        problems.append("over_budget")
    # Restrictions / allergies / dislikes.
    items = [{"item_name": ingredient} for ingredient in option.ingredients]
    if validate_meal_restrictions(items, restrictions):
        problems.append("restriction_violation")
    if _matches_free_text_preference([option.title, *option.ingredients], restrictions):
        problems.append("disliked_food")
    return problems


def _repair_option_to_budget(option: MealOption, budget: MealBudget) -> MealOption:
    """Scale an option into the meal budget when that is nutritionally sane."""
    if option.calories <= 0:
        return option
    if option.calories > budget.calories_max and budget.allows_overage:
        return option
    if option.calories > budget.calories_max:
        target = max(budget.calories_min or 0, min(option.calories, budget.calories_max))
    elif budget.calories_min and option.calories < budget.calories_min and budget.policy not in {"low_remaining", "at_or_over_target"}:
        target = min(budget.calories_max, max(budget.calories_min, int((budget.calories_min + budget.calories_max) / 2)))
    else:
        return option
    if target <= 0 or target == option.calories:
        return option
    scale = target / option.calories
    return _scale_option_ingredients(option, scale)


def _score_option(
    option: MealOption,
    budget: MealBudget,
    context: WorkoutNutritionContext,
    recent_keys: set[str],
) -> tuple[float, str]:
    """Deterministic match score in [0,1] plus the single strongest reason.

    RE9-052/053/013. Combines protein-fit, calorie-fit, timing, freshness and
    simplicity so ranking is explainable and stable (no AI, no randomness).
    """
    reasons: list[tuple[float, str]] = []

    # Protein fit: reward hitting the protein window; weight higher when the day
    # still needs a lot of protein.
    protein_mid = (budget.protein_min + budget.protein_max) / 2 or 1
    protein_gap = abs(option.protein - protein_mid) / protein_mid
    protein_fit = max(0.0, 1.0 - protein_gap)
    protein_short = context.nutrition.protein_balance
    protein_weight = 0.35 if (protein_short is not None and protein_short >= 40) else 0.25
    if protein_short is not None and protein_short >= 40 and option.protein >= protein_mid:
        reasons.append((protein_fit * protein_weight + 0.2, "הכי קרוב ליעד החלבון שנותר"))
    else:
        reasons.append((protein_fit * protein_weight, "מאזן חלבון טוב"))

    # Calorie fit within the budget window.
    cal_mid = (budget.calories_min + budget.calories_max) / 2 or 1
    cal_gap = abs(option.calories - cal_mid) / cal_mid
    cal_fit = max(0.0, 1.0 - cal_gap)
    reasons.append((cal_fit * 0.3, "הכי מתאים לתקציב הקלורי"))

    # Timing: pre/post-workout phases favour the templates built for them; the
    # candidate pool is already phase-specific, so this is a small steady bonus.
    timing = 0.15 if context.workout_phase not in {
        WorkoutPhase.WORKOUT_STATUS_UNKNOWN,
        WorkoutPhase.WORKOUT_PLANNED_TIME_PASSED,
    } else 0.05
    if context.workout_phase in {WorkoutPhase.PRE_WORKOUT_IMMEDIATE, WorkoutPhase.DURING_WORKOUT}:
        reasons.append((timing, "מתאים לזמן שלפני האימון"))
    elif context.workout_phase in {WorkoutPhase.POST_WORKOUT_IMMEDIATE, WorkoutPhase.POST_WORKOUT_LATER}:
        reasons.append((timing, "טוב להתאוששות אחרי אימון"))
    else:
        reasons.append((timing, ""))

    # Simplicity: fewer ingredients is a mild tie-breaker (RE9-144).
    simplicity = max(0.0, 0.1 - 0.02 * max(0, len(option.ingredient_details or option.ingredients) - 3))
    reasons.append((simplicity, "פשוט ומהיר להכנה"))

    base = min(1.0, sum(weight for weight, _ in reasons))
    best_reason = max((r for r in reasons if r[1]), key=lambda r: r[0], default=(0.0, ""))[1]

    # Freshness dominates ordering (RE9-041): a recently-served title must never
    # outrank a fresh one, so apply a large penalty rather than a small bonus.
    if _free_text_preference_key(option.title) in recent_keys:
        return base - 1.0, best_reason
    if base >= 0.5 and "לאחרונה" not in best_reason:
        best_reason = best_reason or "לא הצעתי לך את זה לאחרונה"
    return base, best_reason


def _rank_and_recommend(
    options: list[MealOption],
    budget: MealBudget,
    context: WorkoutNutritionContext,
    recent_keys: set[str],
) -> list[MealOption]:
    """Score, sort (desc) and mark exactly one option as recommended."""
    for option in options:
        option.score, option.recommended_reason = _score_option(option, budget, context, recent_keys)
        option.recommended = False
    ranked = sorted(options, key=lambda o: o.score, reverse=True)
    if ranked:
        ranked[0].recommended = True
    return ranked


def _fit_and_validate_options(
    options: list[MealOption],
    budget: MealBudget,
    restrictions: list[DietaryRestriction],
    *,
    excluded_fingerprints: set[str] | None = None,
    honor_user_quantity_scales: bool = False,
) -> tuple[list[MealOption], list[str]]:
    """Repair-or-drop options so a broken / over-budget option is never shown.

    Returns (valid_options, validation_events) where events feed observability.
    """
    excluded = excluded_fingerprints or set()
    valid: list[MealOption] = []
    events: list[str] = []
    for option in options:
        if option_fingerprint(option) in excluded:
            continue
        problems = validate_meal_option(option, budget, restrictions)
        if ("over_budget" in problems and not budget.allows_overage) or (
            budget.calories_min
            and option.calories < budget.calories_min
            and budget.policy not in {"low_remaining", "at_or_over_target"}
            and not honor_user_quantity_scales
        ):
            events.append("next_meal_validation_failed")
            option = _repair_option_to_budget(option, budget)
            problems = validate_meal_option(option, budget, restrictions)
            if not problems:
                events.append("next_meal_regenerated")
        if problems:
            continue  # unrepairable -> never displayed
        valid.append(option)
    return valid, events


async def generate_next_meal_recommendation(
    db: Any,
    user_id: int,
    *,
    now: datetime | None = None,
    excluded_fingerprints: set[str] | None = None,
    allow_overage: bool = False,
) -> NextMealRecommendation:
    context = await build_workout_nutrition_context(db, user_id, now=now)
    budget = allocate_next_meal_budget(context, allow_overage=allow_overage)
    restrictions = await _restrictions(db, user_id)
    flags = await _daily_flags(db, user_id, context.local_day)
    restrictions = [*restrictions, *_temporary_avoid_restrictions(flags)]
    recent_titles = [
        str(title)
        for title in (flags.get("next_meal_recent_titles") or [])
        if str(title).strip()
    ]
    # Persisted temporary rejections (re7 P1-7): exclude rejected fingerprints
    # for this context without recording a permanent dislike.
    stored_rejections = _active_rejections(flags, now=now)
    excluded = set(excluded_fingerprints or set()) | stored_rejections

    has_user_quantity_scales = bool(_quantity_scales(flags))
    candidates = _apply_quantity_scales(_candidate_templates(context.workout_phase, budget), flags)
    filtered = _filter_options(candidates, restrictions, recent_titles, budget)
    options, validation_events = _fit_and_validate_options(
        filtered,
        budget,
        restrictions,
        excluded_fingerprints=excluded,
        honor_user_quantity_scales=has_user_quantity_scales,
    )
    if len(options) < 2:
        # Top up from the full candidate pool (still validated & de-duplicated).
        extra, extra_events = _fit_and_validate_options(
            _apply_quantity_scales(_candidate_templates(context.workout_phase, budget), flags),
            budget,
            restrictions,
            excluded_fingerprints=excluded | {option_fingerprint(o) for o in options},
            honor_user_quantity_scales=has_user_quantity_scales,
        )
        validation_events += extra_events
        for option in extra:
            if option_fingerprint(option) not in {option_fingerprint(o) for o in options}:
                options.append(option)
            if len(options) >= 4:
                break
    # RE9-052/053/013: rank the validated pool by deterministic score, then keep
    # the top options and mark exactly one as "⭐ מומלץ עבורך".
    recent_keys = {_free_text_preference_key(title) for title in recent_titles if str(title).strip()}
    options = _rank_and_recommend(options, budget, context, recent_keys)[:2]

    notices: list[str] = []
    if budget.policy == "low_remaining":
        notices.append("האפשרויות נבנו כדי להיכנס ליתרת הקלוריות שנותרה להיום.")
    if budget.policy == "at_or_over_target":
        notices.append(
            "הגעת ליעד הקלוריות היומי. אם אתה עדיין רעב, אפשר לבקש חריגה מבוקרת."
        )
    if (
        budget.policy == "normal"
        and context.nutrition.calorie_balance is not None
        and context.nutrition.calorie_balance >= 1200
        and budget.calories_max >= 650
    ):
        notices.append(
            "נשארה לך יתרה גדולה, לכן בניתי ארוחה עיקרית גדולה יחסית. לא צריך להשלים את כל היתרה בבת אחת; אפשר להשאיר מקום לעוד ארוחה קטנה בהמשך."
        )
    if budget.allows_overage and budget.overage_reason:
        notices.append(
            f"ההמלצה חורגת מעט מהיתרה מסיבה ברורה: {budget.overage_reason}. "
            "שים לב להשפעה על סוף היום."
        )
    if context.recent_meal_minutes_ago is not None and context.recent_meal_minutes_ago < 75:
        notices.append(f"אכלת לאחרונה את {context.recent_meal_name}; אם אין רעב, אפשר לבחור גרסה קטנה.")
    if context.fasting:
        notices.append(
            "דיווחת שאתה בצום היום. אם כבר אינך בצום, כתוב ״אני לא בצום״."
        )
    needs_clarification = context.workout_phase in {
        WorkoutPhase.WORKOUT_STATUS_UNKNOWN,
        WorkoutPhase.WORKOUT_PLANNED_TIME_PASSED,
    }
    decision_audit = evaluate_next_meal_decision(
        context,
        option_count=len(options),
        validation_events=validation_events,
    )
    return NextMealRecommendation(
        context=context,
        budget=budget,
        options=options,
        needs_workout_clarification=needs_clarification,
        notices=notices,
        validation_events=validation_events,
        decision_audit=decision_audit.to_dict(),
    )


def _active_rejections(flags: dict[str, Any], *, now: datetime | None = None) -> set[str]:
    """Return non-expired temporary option rejections from daily flags."""
    raw = flags.get("next_meal_rejections") or []
    if not isinstance(raw, list):
        return set()
    current = (now or datetime.now(TZ)).astimezone(TZ)
    active: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        fingerprint = str(entry.get("fingerprint") or "")
        if not fingerprint:
            continue
        expiry = _parse_dt(entry.get("expiry"))
        if expiry is not None and expiry < current:
            continue
        active.add(fingerprint)
    return active


def _temporary_avoid_restrictions(flags: dict[str, Any]) -> list[DietaryRestriction]:
    items = flags.get("next_meal_temp_avoid_items") or []
    if not isinstance(items, list):
        return []
    restrictions: list[DietaryRestriction] = []
    for item in items:
        label = str(item).strip()
        if not label:
            continue
        restrictions.append(
            DietaryRestriction(
                canonical_id=_free_text_preference_key(label),
                user_label=label,
                original_input=label,
                restriction_type="unavailable",
                severity="medium",
                confirmed=True,
                source="next_meal_active_flow",
            )
        )
    return restrictions


async def _record_temporary_avoid_item(
    db: Any,
    user_id: int,
    item: str,
    recommendation: NextMealRecommendation,
) -> None:
    label = item.strip()
    if not label:
        return
    flags = await _daily_flags(db, user_id, recommendation.context.local_day)
    items = [str(value).strip() for value in (flags.get("next_meal_temp_avoid_items") or []) if str(value).strip()]
    existing = {_free_text_preference_key(value) for value in items}
    if _free_text_preference_key(label) not in existing:
        items.append(label)
    flags["next_meal_temp_avoid_items"] = items[-20:]
    await _save_daily_flags(db, user_id, recommendation.context.local_day, flags)


def _quantity_scales(flags: dict[str, Any]) -> dict[str, float]:
    raw = flags.get("next_meal_quantity_scales") or {}
    if not isinstance(raw, dict):
        return {}
    scales: dict[str, float] = {}
    for key, value in raw.items():
        number = _safe_float(value)
        if number is not None:
            scales[str(key)] = max(0.25, min(2.0, number))
    return scales


def _apply_quantity_scales(options: list[MealOption], flags: dict[str, Any]) -> list[MealOption]:
    scales = _quantity_scales(flags)
    if not scales:
        return options
    adjusted: list[MealOption] = []
    for option in options:
        scale = scales.get(option_fingerprint(option))
        adjusted.append(_scale_option_ingredients(option, scale) if scale else option)
    return adjusted


async def record_next_meal_served(
    db: Any,
    user_id: int,
    recommendation: NextMealRecommendation,
) -> None:
    local_day = recommendation.context.local_day
    flags = await _daily_flags(db, user_id, local_day)
    recent = [
        str(title)
        for title in (flags.get("next_meal_recent_titles") or [])
        if str(title).strip()
    ]
    for option in recommendation.options:
        recent = [title for title in recent if _free_text_preference_key(title) != _free_text_preference_key(option.title)]
        recent.append(option.title)
    flags["next_meal_recent_titles"] = recent[-6:]
    flags["next_meal_recent_titles_at"] = utc_now()
    await _save_daily_flags(db, user_id, local_day, flags)


_REJECTION_TTL_HOURS = 12


async def save_next_meal_option_feedback(
    db: Any,
    user_id: int,
    option_number: int,
    *,
    now: datetime | None = None,
) -> tuple[str, NextMealRecommendation]:
    """"לא מתאים לי N": a TEMPORARY rejection of this option in this context.

    re7 P1-7: this is NOT a permanent dislike. We record the option's
    fingerprint with an expiry, exclude it (and near-identical options) from the
    next generation, and return a genuinely different alternative. The user's
    standing preferences are untouched.
    """
    recommendation = await generate_next_meal_recommendation(db, user_id, now=now)
    if option_number < 1 or option_number > len(recommendation.options):
        raise ValueError("Unknown next-meal option")
    option = recommendation.options[option_number - 1]
    fingerprint = option_fingerprint(option)

    await _record_temporary_rejection(
        db, user_id, recommendation, fingerprint, now=now, reason="not_suitable_now"
    )
    # Regenerate excluding the rejected fingerprint -> a truly different option.
    refreshed = await generate_next_meal_recommendation(
        db, user_id, now=now, excluded_fingerprints={fingerprint}
    )
    return option.title, refreshed


async def _record_temporary_rejection(
    db: Any,
    user_id: int,
    recommendation: NextMealRecommendation,
    fingerprint: str,
    *,
    now: datetime | None = None,
    reason: str = "not_suitable_now",
) -> None:
    local_day = recommendation.context.local_day
    current = (now or datetime.now(TZ)).astimezone(TZ)
    expiry = (current + timedelta(hours=_REJECTION_TTL_HOURS)).isoformat()
    flags = await _daily_flags(db, user_id, local_day)
    rejections = [r for r in (flags.get("next_meal_rejections") or []) if isinstance(r, dict)]
    rejections = [r for r in rejections if str(r.get("fingerprint")) != fingerprint]
    rejections.append(
        {
            "fingerprint": fingerprint,
            "rejected_at": current.isoformat(),
            "reason": reason,
            "expiry": expiry,
        }
    )
    flags["next_meal_rejections"] = rejections[-20:]
    await _save_daily_flags(db, user_id, local_day, flags)


async def save_next_meal_unavailable_item(
    db: Any,
    user_id: int,
    option_number: int,
    *,
    now: datetime | None = None,
) -> tuple[str, NextMealRecommendation]:
    """"אין לי בבית": temporary stock shortage, not a standing dislike.

    Treated like a temporary rejection of the option so a different one is shown.
    """
    return await save_next_meal_option_feedback(db, user_id, option_number, now=now)


async def regenerate_with_size(
    db: Any,
    user_id: int,
    option_number: int,
    *,
    smaller: bool,
    now: datetime | None = None,
) -> NextMealRecommendation:
    """"קטן יותר"/"גדול יותר": rebuild with a size hint, respecting the budget cap."""
    flags = await _daily_flags(db, user_id, (now or datetime.now(TZ)).astimezone(TZ).date().isoformat())
    # A "bigger" request may justify an explicit overage; "smaller" never does.
    allow_overage = not smaller and bool(flags.get("next_meal_size_pref") == "bigger")
    flags["next_meal_size_pref"] = "smaller" if smaller else "bigger"
    await _save_daily_flags(db, user_id, (now or datetime.now(TZ)).astimezone(TZ).date().isoformat(), flags)
    return await generate_next_meal_recommendation(db, user_id, now=now, allow_overage=allow_overage)


async def adjust_next_meal_quantity(
    db: Any,
    user_id: int,
    option_number: int,
    scale: float,
    *,
    now: datetime | None = None,
) -> NextMealRecommendation:
    """Persist a quantity scale for one recommendation option and recalculate.

    The scale is keyed by option fingerprint, so choose/save callbacks that
    regenerate the recommendation still use the adjusted ingredient quantities.
    """
    recommendation = await generate_next_meal_recommendation(db, user_id, now=now)
    if option_number < 1 or option_number > len(recommendation.options):
        raise ValueError("Unknown next-meal option")
    option = recommendation.options[option_number - 1]
    fingerprint = option_fingerprint(option)
    local_day = recommendation.context.local_day
    flags = await _daily_flags(db, user_id, local_day)
    scales = _quantity_scales(flags)
    current = scales.get(fingerprint, 1.0)
    scales[fingerprint] = max(0.25, min(2.0, current * scale))
    flags["next_meal_quantity_scales"] = scales
    await _save_daily_flags(db, user_id, local_day, flags)
    return await generate_next_meal_recommendation(db, user_id, now=now)


def workout_clarification_actions(recommendation: NextMealRecommendation) -> list[list[tuple[str, str]]]:
    if not recommendation.needs_workout_clarification:
        return []
    return [
        [("אדחה את האימון", "nextmeal:wkt:later"), ("אני באימון", "nextmeal:wkt:during")],
        [("סיימתי אימון", "nextmeal:wkt:done"), ("ביטלתי היום", "nextmeal:wkt:cancel")],
    ]


def option_feedback_actions(recommendation: NextMealRecommendation) -> list[list[tuple[str, str]]]:
    return [
        [(f"לא מתאים לי {index}", f"nextmeal:dislike:{index}")]
        for index, _option in enumerate(recommendation.options, 1)
    ]


def next_meal_action_rows(recommendation: NextMealRecommendation) -> list[list[tuple[str, str]]]:
    """Primary next-meal actions shown on the recommendation screen.

    RE9 recording follow-up: choosing must not be ambiguous. The first screen
    separates "I ate this" from "plan this for later" so consumed and planned
    stay distinct without forcing an extra tap through a generic choice screen.
    """
    rows: list[list[tuple[str, str]]] = []
    rows.extend(workout_clarification_actions(recommendation))
    for index, _option in enumerate(recommendation.options, 1):
        rows.append([
            (f"🍽 אכלתי אפשרות {index}", f"nextmeal:save:{index}"),
            (f"📅 תכנן אפשרות {index}", f"nextmeal:plan:{index}"),
        ])
    rows.append([("איך חושב?", "nextmeal:why")])
    rows.append([("🔄 הצעות אחרות", "nextmeal:refresh")])
    return rows


def _signed_balance_line(label: str, target: int | None, consumed: int, balance: int | None, unit: str) -> list[str]:
    lines = [f"• יעד {label}: {target if target is not None else 'לא ידוע'} {unit}"]
    lines.append(f"• נאכל עד עכשיו: {consumed} {unit}")
    if balance is None:
        lines.append("• נותר להיום: לא ידוע")
    elif balance >= 0:
        lines.append(f"• נותר להיום: {balance} {unit}")
    else:
        lines.append(f"• חריגה מהיעד: {abs(balance)} {unit}")
    return lines


def _remaining_headline(nutrition: NutritionTotals) -> str:
    cal = nutrition.calorie_balance
    prot = nutrition.protein_balance
    if cal is None:
        return "<b>מה לאכול עכשיו</b>"
    cal_part = (
        f"נשארו לך היום <b>{cal}</b> קלוריות"
        if cal >= 0
        else f"היום כבר יש חריגה של <b>{abs(cal)}</b> קלוריות"
    )
    if prot is not None and prot > 0:
        return f"{cal_part} ו-<b>{prot}</b> גרם חלבון."
    return f"{cal_part}."


def after_meal_balance(nutrition: NutritionTotals, option: MealOption) -> tuple[int | None, int | None]:
    """RE9-020: consumed-based remaining if this option were eaten now.

    Planned meals are deliberately excluded — this shows the honest effect of
    eating this specific option, matching the consumed-only remaining balance.
    """
    after_cal = None if nutrition.calorie_balance is None else nutrition.calorie_balance - option.calories
    after_prot = None if nutrition.protein_balance is None else nutrition.protein_balance - option.protein
    return after_cal, after_prot


def _after_meal_line(nutrition: NutritionTotals, option: MealOption) -> str:
    after_cal, after_prot = after_meal_balance(nutrition, option)
    if after_cal is None:
        return ""
    if after_cal >= 0:
        cal_txt = f"יישארו כ-{after_cal} קל׳"
    else:
        cal_txt = f"חריגה של כ-{abs(after_cal)} קל׳"
    if after_prot is not None and after_prot > 0:
        return f"<i>אחרי הארוחה: {cal_txt} ו-{after_prot} ג׳ חלבון להיום</i>"
    return f"<i>אחרי הארוחה: {cal_txt} להיום</i>"


def _fit_score_label(option: MealOption) -> str:
    score = int(round(max(0.0, min(1.0, option.score)) * 100))
    if score <= 0:
        return ""
    return f" | התאמה {score}%"


def format_next_meal_recommendation(recommendation: NextMealRecommendation) -> str:
    """Answer-first message (re7 P1-10): remaining + options first, short note,
    and the long explanation only via the 'why it fits' detail view."""
    context = recommendation.context
    nutrition = context.nutrition
    goal_note = ""
    if nutrition.goal_status == "active_provisional":
        goal_note = " (יעד זמני)"
    elif nutrition.goal_status == "default":
        goal_note = " (ברירת מחדל עד לאישור יעד)"

    lines = [_remaining_headline(nutrition) + goal_note, ""]
    for index, option in enumerate(recommendation.options, 1):
        star = " ⭐ מומלץ עבורך" if option.recommended else ""
        after = _after_meal_line(nutrition, option)
        lines += [
            f"<b>אפשרות {index}: {esc(option.title)}</b>{star}",
            f"{esc(', '.join(option.ingredients))}",
            f"כ-{option.calories} קל׳ | כ-{option.protein} גרם חלבון{_fit_score_label(option)}",
        ]
        if after:
            lines.append(after)
        if option.recommended and option.recommended_reason:
            lines.append(f"<i>{esc(option.recommended_reason)}</i>")
        lines.append("")
    if recommendation.options:
        if nutrition.calorie_balance is not None and nutrition.calorie_balance >= 0 and not recommendation.budget.allows_overage:
            lines.append("שתיהן מתאימות ליתרה שלך כרגע.")
        elif recommendation.budget.allows_overage and recommendation.budget.overage_reason:
            lines.append(f"שים לב: ההצעה חורגת מעט מהיתרה ({esc(recommendation.budget.overage_reason)}).")
    if recommendation.needs_workout_clarification:
        lines.append("לא אניח שהאימון קרה בלי דיווח — אפשר לעדכן בכפתורים.")
    return "\n".join(lines).strip()


def format_next_meal_explanation(recommendation: NextMealRecommendation) -> str:
    """The 'why it fits' detail screen (re7 P1-10): full calculation & context."""
    context = recommendation.context
    nutrition = context.nutrition
    budget = recommendation.budget
    lines = [
        "<b>למה זה מתאים</b>",
        "",
        "<b>איך חושב?</b>",
        *format_remaining_calculation(nutrition_remaining_calculation(nutrition)),
        "",
        "<b>מצב היום</b>",
        *_signed_balance_line("קלוריות", nutrition.target_calories, nutrition.consumed_calories, nutrition.calorie_balance, "קל׳"),
        *_signed_balance_line("חלבון", nutrition.target_protein, nutrition.consumed_protein, nutrition.protein_balance, "גרם"),
        "",
        "<b>אימון</b>",
        f"• {esc(context.workout_label)}",
    ]
    if context.minutes_until_workout is not None:
        lines.append(f"• זמן עד אימון: כ-{context.minutes_until_workout} דקות")
    if context.minutes_since_workout is not None:
        lines.append(f"• זמן מאז אימון: כ-{context.minutes_since_workout} דקות")
    lines += [
        "",
        "<b>תקציב הארוחה</b>",
        f"• כ-{budget.calories_min}-{budget.calories_max} קלוריות",
        f"• כ-{budget.protein_min}-{budget.protein_max} גרם חלבון",
        f"• {esc(budget.rationale)}",
    ]
    if context.meals_remaining_estimate:
        lines.append(f"• הערכת ארוחות שנותרו היום: {context.meals_remaining_estimate}")
    timeline = build_day_timeline(context)
    if timeline:
        lines += ["", "<b>המשך היום</b>", *timeline]
    for notice in recommendation.notices:
        lines.append(f"• {esc(notice)}")
    if context.assumptions:
        lines += ["", "<i>" + esc(" ".join(context.assumptions)) + "</i>"]
    return "\n".join(lines)


def build_day_timeline(context: WorkoutNutritionContext) -> list[str]:
    """RE9-002: a lightweight plan of the rest of the day.

    Deterministic, derived from the workout phase, minutes-to/since workout and
    hours-until-bedtime already on the context. Kept in the detail view so the
    first screen stays answer-first (RE9-012).
    """
    steps: list[str] = ["עכשיו — הארוחה הבאה"]
    phase = context.workout_phase
    if phase in {WorkoutPhase.PRE_WORKOUT_IMMEDIATE, WorkoutPhase.DURING_WORKOUT} or (
        context.minutes_until_workout is not None
    ):
        if context.minutes_until_workout:
            steps.append(f"בעוד ~{context.minutes_until_workout} דקות — אימון")
        else:
            steps.append("בהמשך — אימון")
        steps.append("אחרי האימון — ארוחת התאוששות עם חלבון")
    hours = context.hours_until_bedtime
    if hours is not None:
        if hours <= 1.5:
            steps.append("סמוך לשינה — לא מומלץ עוד ארוחה כבדה")
        elif hours <= 3.5:
            steps.append("לפני השינה — אפשר ארוחה קלה אחת אם צריך")
        else:
            steps.append("לפני השינה — נשאר מקום לעוד 1-2 ארוחות")
        steps.append(f"עוד ~{hours:.0f} שעות עד השינה")
    steps.append("סוף היום — סיכום יומי")
    return [f"• {esc(step)}" for step in steps]


@dataclass
class RemainingSlotAllocation:
    """One remaining meal slot for the rest of today (RE10-13).

    ``label`` is a human phase description ("לפני אימון" / "אחרי אימון" /
    "ארוחת לילה" / "ארוחה"), not a fixed plan-slot name — it is derived at
    render time from the workout phase and position in the day, not stored.
    """

    label: str
    time_hint: str | None
    calories: int
    protein: int
    is_night_meal: bool = False


# D9/E2: the last remaining slot before bedtime is always small and
# protein-dominant (a slow protein source helps preserve muscle overnight in
# a deficit) rather than an arbitrary share of whatever calories are left.
_NIGHT_MEAL_CALORIES = 150
_NIGHT_MEAL_MIN_PROTEIN = 12


def build_remaining_slot_allocations(
    context: WorkoutNutritionContext,
    *,
    slot_count: int | None = None,
) -> list[RemainingSlotAllocation]:
    """Split the remaining calorie/protein balance across the rest of today.

    Reuses the SAME hard-cap principle as ``allocate_next_meal_budget``
    (D9): the sum of every returned slot's calories never exceeds the
    remaining daily balance. When the balance is at/under zero, all slots
    collapse to the night-meal-sized floor (never a "budget" that implies
    room that doesn't exist).

    ``slot_count`` defaults to ``context.meals_remaining_estimate`` (the same
    estimator next-meal recommendations already use), so "מצב היום" and "מה
    לאכול עכשיו" never disagree about how many meals are left today.
    """
    nutrition = context.nutrition
    remaining_cal = nutrition.calorie_balance
    remaining_protein = max(0, nutrition.protein_balance or 0)
    count = max(1, slot_count if slot_count is not None else context.meals_remaining_estimate)

    if remaining_cal is None or remaining_cal <= 0:
        # Nothing left to allocate — still show the night-meal floor if a
        # real night slot exists, so the user sees a safe closing option
        # rather than an empty section that reads as "nothing to plan".
        floor = min(_NIGHT_MEAL_CALORIES, max(0, remaining_cal or 0))
        return [
            RemainingSlotAllocation(
                label="ארוחת לילה", time_hint=None, calories=int(floor),
                protein=_NIGHT_MEAL_MIN_PROTEIN if floor > 0 else 0, is_night_meal=True,
            )
        ] if count >= 1 else []

    has_night_slot = count >= 2
    night_calories = min(_NIGHT_MEAL_CALORIES, remaining_cal) if has_night_slot else 0
    night_protein = _NIGHT_MEAL_MIN_PROTEIN if has_night_slot else 0
    day_slot_count = count - 1 if has_night_slot else count
    day_calories_pool = max(0, remaining_cal - night_calories)
    day_protein_pool = max(0, remaining_protein - night_protein)

    allocations: list[RemainingSlotAllocation] = []
    per_slot_calories = day_calories_pool // day_slot_count if day_slot_count else 0
    per_slot_protein = day_protein_pool // day_slot_count if day_slot_count else 0
    allocated_calories = 0
    allocated_protein = 0
    for index in range(day_slot_count):
        is_last_day_slot = index == day_slot_count - 1
        cal = int(day_calories_pool - allocated_calories) if is_last_day_slot else int(per_slot_calories)
        prot = int(day_protein_pool - allocated_protein) if is_last_day_slot else int(per_slot_protein)
        allocated_calories += cal
        allocated_protein += prot
        allocations.append(
            RemainingSlotAllocation(label="ארוחה", time_hint=None, calories=cal, protein=prot)
        )

    if has_night_slot:
        allocations.append(
            RemainingSlotAllocation(
                label="ארוחת לילה", time_hint=None,
                calories=int(night_calories), protein=int(night_protein), is_night_meal=True,
            )
        )

    # Label the first one or two day slots by workout phase when relevant —
    # this is what lets the render layer say "ארוחה לפני אימון" / "אחרי אימון"
    # instead of a generic "ארוחה 1" (D10: derived at render time, the
    # underlying nutrition-plan slot names are never mutated).
    phase = context.workout_phase
    if allocations and phase in {
        WorkoutPhase.PRE_WORKOUT_EARLY, WorkoutPhase.PRE_WORKOUT_NEAR, WorkoutPhase.PRE_WORKOUT_IMMEDIATE,
    }:
        allocations[0].label = "ארוחה לפני אימון"
        if len(allocations) > 1 and not allocations[1].is_night_meal:
            allocations[1].label = "ארוחה אחרי אימון"
    elif allocations and phase in {WorkoutPhase.DURING_WORKOUT, WorkoutPhase.POST_WORKOUT_IMMEDIATE, WorkoutPhase.POST_WORKOUT_LATER}:
        allocations[0].label = "ארוחה אחרי אימון"

    return allocations


ACTIVE_RECOMMENDATION_FLOW = "next_meal_recommendation"
_RECOMMENDATION_TTL_HOURS = 6


async def remember_active_recommendation(
    db: Any,
    user_id: int,
    recommendation: NextMealRecommendation,
    *,
    message_id: int | None = None,
    now: datetime | None = None,
) -> None:
    """Persist the active next-meal recommendation so a later free-text
    correction ("זה גדול מדי", "אבל נשאר לי 269") can be understood against it,
    and survive a restart for a reasonable window (re7 P1-15)."""
    from noam_coach.services import core as core_services

    current = (now or datetime.now(TZ)).astimezone(TZ)
    payload = {
        "options": [option_fingerprint(o) for o in recommendation.options],
        "option_payloads": [_option_to_payload(o) for o in recommendation.options],
        "option_titles": [o.title for o in recommendation.options],
        "remaining_calories": recommendation.context.nutrition.calorie_balance,
        "budget_policy": recommendation.budget.policy,
        "message_id": message_id,
        "created_at": current.isoformat(),
        "expiry": (current + timedelta(hours=_RECOMMENDATION_TTL_HOURS)).isoformat(),
    }
    await core_services.set_flow_state(user_id, ACTIVE_RECOMMENDATION_FLOW, "active", payload)


async def get_active_recommendation_state(
    db: Any,
    user_id: int,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    from noam_coach.services import core as core_services

    state = await core_services.get_flow_state(user_id, ACTIVE_RECOMMENDATION_FLOW)
    if not state:
        return None
    payload = state.get("payload") or {}
    expiry = _parse_dt(payload.get("expiry"))
    current = (now or datetime.now(TZ)).astimezone(TZ)
    if expiry is not None and expiry < current:
        await core_services.clear_flow_state(user_id, ACTIVE_RECOMMENDATION_FLOW)
        return None
    return payload


async def get_active_recommendation_options(
    db: Any,
    user_id: int,
    *,
    now: datetime | None = None,
) -> list[MealOption]:
    state = await get_active_recommendation_state(db, user_id, now=now)
    if not state:
        return []
    options = []
    for payload in state.get("option_payloads") or []:
        if isinstance(payload, dict):
            options.append(_option_from_payload(payload))
    return options


async def clear_active_recommendation(db: Any, user_id: int) -> None:
    from noam_coach.services import core as core_services

    await core_services.clear_flow_state(user_id, ACTIVE_RECOMMENDATION_FLOW)


async def mark_active_recommendation_selection(
    db: Any,
    user_id: int,
    option_number: int,
    *,
    now: datetime | None = None,
) -> None:
    """Remember which visible option the user is reviewing before save."""
    from noam_coach.services import core as core_services

    state = await get_active_recommendation_state(db, user_id, now=now)
    if not state:
        return
    state["selected_option"] = option_number
    state["review_started_at"] = (now or datetime.now(TZ)).astimezone(TZ).isoformat()
    await core_services.set_flow_state(user_id, ACTIVE_RECOMMENDATION_FLOW, "active", state)


def _selected_option_number(state: dict[str, Any] | None) -> int:
    if not state:
        return 1
    try:
        selected = int(state.get("selected_option") or 1)
    except (TypeError, ValueError):
        return 1
    return max(1, selected)


def classify_recommendation_correction(text: str) -> dict[str, Any]:
    """Interpret a free-text message relative to an active recommendation.

    Returns {"kind": ...} where kind is one of:
      budget_correction (with 'calories'), smaller, bigger, pre_workout,
      short_time (with 'minutes'), unavailable_item (with 'item'),
      dislike_item (with 'item'), or none.
    """
    t = f" {text.strip()} "

    # A stated remaining-calories number ("אבל נשאר לי 269 קלוריות").
    if any(word in t for word in ("נשאר", "נשארו", "נותר", "נותרו")):
        match = re.search(r"(?<!\d)(\d{2,4})(?!\d)", t)
        if match:
            value = int(match.group(1))
            if 0 <= value <= 6000:
                return {"kind": "budget_correction", "calories": value}

    if any(word in t for word in ("גדול מדי", "יותר מדי", "כבד מדי", "משהו קטן", "קטן יותר", "פחות")):
        return {"kind": "smaller"}
    if any(word in t for word in ("ממש רעב", "רעב מאוד", "גדול יותר", "יותר אוכל", "משהו גדול")):
        return {"kind": "bigger"}
    if any(word in t for word in ("לפני אימון", "לפני האימון", "הולך להתאמן", "מתאמן עוד")):
        return {"kind": "pre_workout"}

    minutes_match = re.search(r"(?:רק\s*)?(\d{1,3})\s*דקות", t)
    if minutes_match and ("יש לי" in t or "רק" in t or "זמן" in t):
        return {"kind": "short_time", "minutes": int(minutes_match.group(1))}

    if any(word in t for word in ("אין לי", "נגמר", "נגמרו", "אזל")):
        return {"kind": "unavailable_item", "item": text.strip()}
    qty_match = re.search(r"([\w\u0590-\u05ff׳'\" -]{2,40}?)\s*(\d{1,4})\s*(?:גרם|ג׳|ג'|גר|g)\b", t)
    if qty_match:
        item = qty_match.group(1).strip(" ,.-")
        grams = int(qty_match.group(2))
        if 5 <= grams <= 1000:
            return {"kind": "quantity_override", "item": item, "grams": grams}

    if any(word in t for word in ("בלי", "ללא", "אל תשים", "תוריד")):
        item = _extract_item_after_marker(text, ("בלי", "ללא", "אל תשים", "תוריד"))
        return {"kind": "avoid_now", "item": item or text.strip()}

    if any(word in t for word in ("לא אוהב", "לא אוהבת", "שונא", "לא מתחבר")):
        return {"kind": "dislike_item", "item": text.strip()}

    return {"kind": "none"}


def _extract_item_after_marker(text: str, markers: tuple[str, ...]) -> str:
    for marker in markers:
        index = text.find(marker)
        if index >= 0:
            tail = text[index + len(marker):].strip(" :,-.")
            return re.split(r"\s+(?:ו|וגם|אבל|עם)\s+", tail, maxsplit=1)[0].strip()
    return ""


def _text_key(value: str) -> str:
    return _free_text_preference_key(value).replace("׳", "'")


def _option_mentions_item(option: MealOption, item: str) -> bool:
    item_key = _text_key(item)
    if not item_key:
        return False
    haystack = [_text_key(option.title), *(_text_key(part) for part in option.ingredients)]
    haystack.extend(_text_key(ingredient.display_name) for ingredient in option.ingredient_details)
    haystack.extend(_text_key(ingredient.food_id) for ingredient in option.ingredient_details)
    return any(item_key in part or part in item_key for part in haystack if part)


def _override_option_ingredient_quantity(option: MealOption, item: str, grams: int) -> MealOption:
    item_key = _text_key(item)
    updated: list[MealIngredient] = []
    changed = False
    for ingredient in option.ingredient_details:
        names = (
            _text_key(ingredient.display_name),
            _text_key(ingredient.food_id),
        )
        if not changed and any(item_key in name or name in item_key for name in names if name):
            updated.append(_with_quantity(ingredient, float(grams)))
            changed = True
        else:
            updated.append(ingredient)
    return MealOption(
        title=option.title,
        ingredients=[],
        calories=0,
        protein=0,
        rationale=option.rationale,
        substitutions=option.substitutions,
        restriction_validated=option.restriction_validated,
        ingredient_details=updated,
    ) if changed else option


def _replace_selected_option(
    recommendation: NextMealRecommendation,
    option_number: int,
    option: MealOption,
) -> NextMealRecommendation:
    options = list(recommendation.options)
    if 1 <= option_number <= len(options):
        options[option_number - 1] = option
    validation_events = [*recommendation.validation_events, "next_meal_selected_option_updated"]
    decision_audit = evaluate_next_meal_decision(
        recommendation.context,
        option_count=len(options),
        validation_events=validation_events,
    )
    return NextMealRecommendation(
        context=recommendation.context,
        budget=recommendation.budget,
        options=options,
        needs_workout_clarification=recommendation.needs_workout_clarification,
        notices=recommendation.notices,
        validation_events=validation_events,
        decision_audit=decision_audit.to_dict(),
    )


async def handle_recommendation_correction(
    db: Any,
    user_id: int,
    text: str,
    *,
    now: datetime | None = None,
) -> tuple[str, NextMealRecommendation] | None:
    """Apply a free-text correction to the active recommendation (re7 P1-13/14).

    Returns (prefix_message, refreshed_recommendation) when the text was a
    recommendation correction, else None so the caller falls through to generic
    routing. Records observability events. Does NOT count anything as eaten.
    """
    from noam_coach.services import core as core_services

    state = await get_active_recommendation_state(db, user_id, now=now)
    if not state:
        return None
    correction = classify_recommendation_correction(text)
    kind = correction["kind"]
    if kind == "none":
        return None
    del core_services  # imported for symmetry; state already loaded above

    prefix = ""
    allow_overage = False
    selected_option = _selected_option_number(state)

    if kind == "budget_correction":
        snapshot = await build_workout_nutrition_context(db, user_id, now=now)
        actual_remaining = snapshot.nutrition.calorie_balance
        stated = int(correction["calories"])
        await _log_event(db, user_id, "next_meal_user_budget_correction",
                         {"stated": stated, "actual": actual_remaining})
        if actual_remaining is not None and abs(actual_remaining - stated) > 30:
            await _log_event(db, user_id, "nutrition_context_mismatch",
                             {"stated": stated, "actual": actual_remaining})
            prefix = (
                f"לפי מה שרשום אצלי נשארו לך {actual_remaining} קלוריות (ולא {stated}). "
                "הנה אפשרויות שמתאימות לחישוב הזה:"
            )
        else:
            prefix = (
                f"צודק — נשארו לך כ-{actual_remaining if actual_remaining is not None else stated} "
                "קלוריות. חישבתי מחדש, והנה אפשרויות שמתאימות ליתרה:"
            )
    elif kind == "smaller":
        prefix = "הקטנתי את ההצעה כך שתתאים טוב יותר ליתרה ולרעב שלך."
    elif kind == "bigger":
        allow_overage = True
        prefix = "הגדלתי מעט את ההצעה. שים לב להשפעה על סוף היום."
    elif kind == "pre_workout":
        await save_next_meal_workout_status(db, user_id, "later", now=now)
        prefix = "התאמתי את ההצעה לארוחה לפני אימון."
    elif kind == "short_time":
        prefix = "התאמתי לאפשרויות מהירות להכנה."
    elif kind == "unavailable_item":
        prefix = "סימנתי שחסר לך מרכיב כרגע (זמני) והחלפתי את ההצעה."
    elif kind == "avoid_now":
        item = str(correction.get("item") or "").strip()
        temp_context = await build_workout_nutrition_context(db, user_id, now=now)
        temp_budget = allocate_next_meal_budget(temp_context)
        temp_recommendation = NextMealRecommendation(
            context=temp_context,
            budget=temp_budget,
            options=[],
        )
        await _record_temporary_avoid_item(db, user_id, item, temp_recommendation)
        active_options = await get_active_recommendation_options(db, user_id, now=now)
        if 1 <= selected_option <= len(active_options):
            selected = active_options[selected_option - 1]
            await _record_temporary_rejection(
                db,
                user_id,
                _replace_selected_option(temp_recommendation, 1, selected),
                option_fingerprint(selected),
                now=now,
                reason="avoid_now",
            )
        prefix = f"הסרתי את {esc(item)} מההצעה הנוכחית והכנתי חלופה בלי המרכיב הזה."
    elif kind == "dislike_item":
        # A standing dislike -> persist via the canonical preferences service.
        await record_food_preference_from_slots(
            db, user_id,
            {"kind": "preference", "polarity": "avoid", "item": text.strip(), "note": text.strip()},
            text.strip(),
        )
        prefix = "שמרתי את ההעדפה הקבועה והחלפתי את ההצעה."
    elif kind == "quantity_override":
        active_options = await get_active_recommendation_options(db, user_id, now=now)
        item = str(correction.get("item") or "").strip()
        grams = int(correction.get("grams") or 0)
        if 1 <= selected_option <= len(active_options) and grams > 0:
            option = active_options[selected_option - 1]
            updated = _override_option_ingredient_quantity(option, item, grams)
            if updated is not option:
                refreshed_context = await build_workout_nutrition_context(db, user_id, now=now)
                refreshed_budget = allocate_next_meal_budget(refreshed_context)
                refreshed = _replace_selected_option(
                    NextMealRecommendation(
                        context=refreshed_context,
                        budget=refreshed_budget,
                        options=active_options,
                    ),
                    selected_option,
                    updated,
                )
                await remember_active_recommendation(db, user_id, refreshed, now=now)
                await mark_active_recommendation_selection(db, user_id, selected_option, now=now)
                await _log_event(db, user_id, "next_meal_quantity_text_updated", {"item": item, "grams": grams})
                return f"עדכנתי את {esc(item)} ל-{grams} גרם וחישבתי מחדש.", refreshed
        prefix = "לא מצאתי את המרכיב הזה באפשרות הנבחרת. אפשר לכתוב למשל: קוטג׳ 150 גרם."

    refreshed = await generate_next_meal_recommendation(
        db, user_id, now=now, allow_overage=allow_overage
    )
    await remember_active_recommendation(db, user_id, refreshed, now=now)
    await _log_event(db, user_id, "next_meal_option_replaced", {"correction": kind})
    return prefix, refreshed


async def _log_event(db: Any, user_id: int, name: str, properties: dict[str, Any]) -> None:
    import event_log

    try:
        await event_log.append_event(
            db, user_id, name, entity="next_meal", source="user_text", properties=properties
        )
    except Exception:  # observability must never break the flow  # noqa: BLE001
        pass


async def save_chosen_meal(
    db: Any,
    user_id: int,
    option: MealOption,
    *,
    now: datetime | None = None,
) -> bool:
    """Persist a chosen next-meal option as an eaten meal (re7 task 11/5).

    Only this function counts a recommendation as consumed — choosing/viewing
    never does. A short-lived per-fingerprint guard makes a fast double-tap
    idempotent. Returns False when the same option was just saved.
    """
    current = (now or datetime.now(TZ)).astimezone(TZ)
    fingerprint = option_fingerprint(option)
    local_day = current.date().isoformat()
    flags = await _daily_flags(db, user_id, local_day)
    saved = flags.get("next_meal_saved") or {}
    last_at = _parse_dt(saved.get(fingerprint)) if isinstance(saved, dict) else None
    if last_at is not None and (current - last_at).total_seconds() < 120:
        return False  # double-tap within 2 minutes -> no duplicate row

    iso_now = current.astimezone(timezone.utc).isoformat()
    await db.execute(
        """
        INSERT INTO meals(user_id, name, calories, protein, carbs, fat,
                          confidence, eaten_at, created_at)
        VALUES(?, ?, ?, ?, 0, 0, ?, ?, ?)
        """,
        (user_id, option.title, int(option.calories), int(option.protein), 0.6, iso_now, iso_now),
    )
    if not isinstance(saved, dict):
        saved = {}
    saved[fingerprint] = current.isoformat()
    flags["next_meal_saved"] = saved
    await _save_daily_flags(db, user_id, local_day, flags)
    await _log_event(db, user_id, "next_meal_saved_as_meal", {"title": option.title, "calories": option.calories})
    return True


async def plan_chosen_meal(
    db: Any,
    user_id: int,
    option: MealOption,
    *,
    now: datetime | None = None,
) -> bool:
    """RE9-019: record a chosen option as *planned* for later, not consumed.

    Planned meals live in daily_flags['next_meal_planned'] and are surfaced by
    the nutrition context as planned — they never reduce the consumed balance
    until the user explicitly saves them as eaten. Idempotent per fingerprint.
    """
    current = (now or datetime.now(TZ)).astimezone(TZ)
    fingerprint = option_fingerprint(option)
    local_day = current.date().isoformat()
    flags = await _daily_flags(db, user_id, local_day)
    planned = flags.get("next_meal_planned")
    if not isinstance(planned, list):
        planned = []
    if any(isinstance(m, dict) and m.get("fingerprint") == fingerprint for m in planned):
        return False  # already planned -> no duplicate
    planned.append({
        "fingerprint": fingerprint,
        "name": option.title,
        "calories": int(option.calories),
        "protein": int(option.protein),
        "planned_at": current.isoformat(),
    })
    flags["next_meal_planned"] = planned
    await _save_daily_flags(db, user_id, local_day, flags)
    await _log_event(db, user_id, "next_meal_planned_for_later", {"title": option.title, "calories": option.calories})
    return True


async def invalidate_daily_nutrition_cache(
    db: Any,
    user_id: int,
    *,
    now: datetime | None = None,
) -> None:
    """Drop per-day next-meal cache state after a goal/snapshot change (re7 P0-5).

    The budget is always recomputed live from the active goal, but stale recent
    titles / size preference could bias the next recommendation, so clear them.
    """
    local_day = (now or datetime.now(TZ)).astimezone(TZ).date().isoformat()
    flags = await _daily_flags(db, user_id, local_day)
    for key in ("next_meal_recent_titles", "next_meal_recent_titles_at", "next_meal_size_pref"):
        flags.pop(key, None)
    await _save_daily_flags(db, user_id, local_day, flags)


async def build_next_meal_response_text(db: Any, user_id: int) -> str:
    recommendation = await generate_next_meal_recommendation(db, user_id)
    await record_next_meal_served(db, user_id, recommendation)
    return format_next_meal_recommendation(recommendation)
