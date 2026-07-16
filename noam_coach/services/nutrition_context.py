"""Central nutrition context for menu and nutrition AI requests.

This service builds one structured snapshot for nutrition decisions so
handlers do not recalculate consumed/remaining macros or daily state in
slightly different ways.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

import data_quality
import planning
import user_model
from config import SETTINGS, TZ
from helpers import utc_now
from noam_coach.services.daily_state import coaching_day_bounds_utc, coaching_day_key
from noam_coach.services.dietary_restrictions import (
    load_restrictions_from_facts,
)
from noam_coach.services.food_environment import normalize_food_environment_context
from noam_coach.services.learned_foods import learned_foods_from_meals
from noam_coach.services.next_meal import (
    WorkoutPhase,
    build_workout_nutrition_context,
)
from noam_coach.services.user_state import SharedUserState
from noam_coach.services.weekdays import local_weekday

# Sentinel sent to the AI for a field that has no real source yet. It is
# deliberately NOT an empty list/None so the model does not infer "the user has
# none of this" (e.g. an empty available_ingredients must not read as "no food").
NOT_CAPTURED = "not_captured"

# Inputs that are read from daily_flags but currently have NO production writer.
# Each maps to the flags key that a future capture flow would populate. When the
# key is absent at build time the field is marked uncaptured (honest), instead of
# defaulting to an empty value that looks like real data.
_FLAG_BACKED_OPTIONAL_FIELDS: dict[str, str] = {
    "available_prep_minutes": "available_prep_minutes",
    "eating_location": "eating_location",
    "available_equipment": "available_equipment",
    "available_ingredients": "available_ingredients",
    "recently_rejected_meals": "recently_rejected_meals",
}

# Internal provenance trace for every NutritionContext field: where it comes
# from, who writes it, who reads it, freshness, fallback, and whether it is sent
# to the AI. Kept in code (not user-facing) so the data model stays honest.
FIELD_PROVENANCE: dict[str, dict[str, Any]] = {
    "calorie_target": {"source": "goal_versions", "writer": "planning.activate_goal", "reader": "menu/next_meal", "freshness": "on goal change", "fallback": "SETTINGS.default_calories", "sent_to_ai": True},
    "protein_target": {"source": "goal_versions", "writer": "planning.activate_goal", "reader": "menu/next_meal", "freshness": "on goal change", "fallback": "SETTINGS.default_protein", "sent_to_ai": True},
    "consumed_calories": {"source": "meals", "writer": "meal logging", "reader": "context builder", "freshness": "per meal", "fallback": "0", "sent_to_ai": True},
    "reported_meals": {"source": "meals", "writer": "meal logging", "reader": "context builder", "freshness": "per meal", "fallback": "[]", "sent_to_ai": True},
    "disliked_foods": {"source": "user_facts:disliked_foods", "writer": "food_preferences.record_food_preference_from_slots", "reader": "next_meal/_restrictions", "freshness": "on user statement", "fallback": "[]", "sent_to_ai": True},
    "preferred_foods": {"source": "user_facts:preferred_foods", "writer": "food_preferences.record_food_preference_from_slots", "reader": "menu", "freshness": "on user statement", "fallback": "[]", "sent_to_ai": True},
    "learned_foods": {"source": "approved meal_items history", "writer": "meal approval / persist_meal", "reader": "meal analysis, menu, next_meal", "freshness": "last 180 days", "fallback": "[]", "sent_to_ai": True},
    "allergies": {"source": "user_facts:allergies", "writer": "food_preferences", "reader": "restriction validator", "freshness": "on user statement", "fallback": "[]", "sent_to_ai": True},
    "food_environment_context": {"source": "user_facts:food_environment_context", "writer": "questions.record_answer", "reader": "planner/menu/next_meal", "freshness": "180 days", "fallback": "None", "sent_to_ai": True},
    "fasting_status": {"source": "daily_flags.fasting", "writer": "flags menu / morning_flag", "reader": "budget", "freshness": "per day", "fallback": "False", "sent_to_ai": True},
    "hunger_level": {"source": "daily_flags.hunger", "writer": "flags menu", "reader": "context", "freshness": "per day", "fallback": "None", "sent_to_ai": True},
    "energy_level": {"source": "daily_flags.energy", "writer": "flags menu", "reader": "context", "freshness": "per day", "fallback": "None", "sent_to_ai": True},
    "sleep_quality": {"source": "daily_flags.sleep_quality", "writer": "flags menu", "reader": "context", "freshness": "per day", "fallback": "None", "sent_to_ai": True},
    "available_prep_minutes": {"source": "daily_flags.available_prep_minutes", "writer": "NONE (not captured)", "reader": "context", "freshness": "n/a", "fallback": "not_captured", "sent_to_ai": "as not_captured"},
    "eating_location": {"source": "daily_flags.eating_location", "writer": "NONE (not captured)", "reader": "context", "freshness": "n/a", "fallback": "not_captured", "sent_to_ai": "as not_captured"},
    "available_equipment": {"source": "daily_flags.available_equipment", "writer": "NONE (not captured)", "reader": "context", "freshness": "n/a", "fallback": "not_captured", "sent_to_ai": "as not_captured"},
    "available_ingredients": {"source": "daily_flags.available_ingredients", "writer": "NONE (not captured)", "reader": "context", "freshness": "n/a", "fallback": "not_captured", "sent_to_ai": "as not_captured"},
    "recently_rejected_meals": {"source": "daily_flags.recently_rejected_meals", "writer": "NONE (not captured; see next_meal recent-titles which is separate)", "reader": "context", "freshness": "n/a", "fallback": "not_captured", "sent_to_ai": "as not_captured"},
    "health_data_freshness": {"source": "health", "writer": "health_jobs import", "reader": "context", "freshness": "per import", "fallback": "{}", "sent_to_ai": True},
}


@dataclass(frozen=True)
class MealSnapshot:
    id: int
    name: str
    calories: float
    protein: float
    carbs: float
    fat: float
    confidence: float
    eaten_at: str


@dataclass(frozen=True)
class NutritionContext:
    user_id: int
    request_type: str
    generated_at: str
    timezone: str
    current_local_time: str
    # TASK-20: "weekday" | "friday" | "saturday" so menu generation can adapt
    # naturally to the user's week (which begins on Sunday) — Friday/Saturday
    # are generally treated differently from workdays.
    day_type: str
    calorie_target: int | None
    protein_target: int | None
    carbs_target: int | None
    fat_target: int | None
    consumed_calories: float
    consumed_protein: float
    consumed_carbs: float
    consumed_fat: float
    remaining_calories: float | None
    remaining_protein: float | None
    remaining_carbs: float | None
    remaining_fat: float | None
    tracking_completeness: float
    tracking_quality_score: float
    reported_meals: list[MealSnapshot]
    planned_meals: list[dict[str, Any]]
    expected_remaining_meals: int
    hours_until_sleep: float | None
    usual_meal_times: list[str]
    workout_status: str
    workout_source: str
    workout_type: str | None
    workout_scheduled_time: str | None
    time_until_workout_minutes: int | None
    time_since_workout_minutes: int | None
    workout_completed: bool
    workout_partial: bool
    workout_cancelled: bool
    allergies: list[str]
    intolerances: list[str]
    dietary_rules: list[str]
    medical_food_constraints: list[dict[str, Any]]
    disliked_foods: list[str]
    preferred_foods: list[str]
    learned_foods: list[dict[str, Any]]
    recently_rejected_meals: list[str]
    appetite: str | None
    hunger_level: str | None
    energy_level: str | None
    sleep_quality: str | None
    fasting_status: bool
    medication_status_reported_today: list[str]
    relevant_daily_symptoms: list[str]
    food_environment_context: dict[str, Any] | None
    available_prep_minutes: int | None
    eating_location: str | None
    available_equipment: list[str]
    available_ingredients: list[str]
    active_weekly_plan: dict[str, Any] | None
    today_plan: dict[str, Any] | None
    health_data_freshness: dict[str, Any]
    data_warnings: list[str] = field(default_factory=list)
    # Fields whose value at build time could not be captured from any real
    # source (no writer exists yet). They are listed here so to_ai_payload can
    # send an honest "not_captured" sentinel instead of an empty list / None
    # that the model would read as "the user has none of this".
    uncaptured_fields: tuple[str, ...] = field(default_factory=tuple)

    def to_ai_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("uncaptured_fields", None)
        for name in self.uncaptured_fields:
            payload[name] = NOT_CAPTURED
        return payload


def _safe_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _remaining(target: int | None, consumed: float) -> float | None:
    return None if target is None else round(float(target) - consumed, 1)


def _list_fact(value: Any) -> list[str]:
    if value in (None, "", "none"):
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, dict):
        return [str(key).strip() for key, enabled in value.items() if enabled and str(key).strip()]
    return [part.strip() for part in str(value).replace("/", ",").split(",") if part.strip()]


async def _daily_flags(db: Any, user_id: int, local_day: str) -> dict[str, Any]:
    row = await db.fetch_one(
        "SELECT flags FROM daily_flags WHERE user_id=? AND day=?",
        (user_id, local_day),
    )
    if not row:
        return {}
    try:
        parsed = json.loads(row["flags"] or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


async def _routine_profile(db: Any, user_id: int) -> dict[str, Any]:
    row = await db.fetch_one("SELECT profile FROM routine_profile WHERE user_id=?", (user_id,))
    if not row:
        return {}
    try:
        parsed = json.loads(row["profile"] or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


async def _reported_meals(db: Any, user_id: int, local_now: datetime | None = None) -> list[MealSnapshot]:
    start_utc, end_utc = await coaching_day_bounds_utc(db, user_id, local_now)
    rows = await db.fetch_all(
        """
        SELECT id, name, calories, protein, carbs, fat, confidence, eaten_at, COALESCE(status, 'consumed') AS status
        FROM meals
        WHERE user_id=? AND eaten_at>=? AND eaten_at<?
          AND COALESCE(status, 'consumed')='consumed'
        ORDER BY eaten_at ASC, id ASC
        """,
        (user_id, start_utc, end_utc),
    )
    return [
        MealSnapshot(
            id=int(row["id"]),
            name=str(row["name"] or ""),
            calories=_safe_float(row.get("calories")),
            protein=_safe_float(row.get("protein")),
            carbs=_safe_float(row.get("carbs")),
            fat=_safe_float(row.get("fat")),
            confidence=_safe_float(row.get("confidence")),
            eaten_at=str(row.get("eaten_at") or ""),
        )
        for row in rows
    ]


def _day_type(local_now: datetime) -> str:
    """TASK-20: classify today so the menu can adapt to the Israeli week.

    Python weekday(): Mon=0 … Fri=4, Sat=5, Sun=6.  Friday and Saturday are the
    Israeli weekend and are generally treated differently from workdays.
    """
    weekday = local_weekday(local_now)
    if weekday == 4:
        return "friday"
    if weekday == 5:
        return "saturday"
    return "weekday"


def day_type(local_now: datetime) -> str:
    """Public alias for ``_day_type`` (TASK-19 audit correction).

    Callers outside this module (e.g. health_jobs.build_morning_briefing_text)
    used to independently re-derive weekday info via local_weekday(...)
    directly instead of reusing this canonical classification — harmless
    today since both read the same underlying weekday() call, but it meant
    "weekday"/"friday"/"saturday" was defined in exactly one place only by
    convention, not in fact, so a future change to the classification (e.g.
    treating a holiday as weekend-like) would need to be duplicated by hand
    everywhere instead of being picked up automatically.
    """
    return _day_type(local_now)


def _today_plan(active_plan: dict[str, Any] | None, local_now: datetime) -> dict[str, Any] | None:
    if not active_plan:
        return None
    days = (active_plan.get("payload") or {}).get("days") or []
    today = local_weekday(local_now)
    for day in days:
        if int(day.get("weekday", -1)) == today:
            return dict(day)
    return None


def _planned_meals(today_plan: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not today_plan:
        return []
    meals = today_plan.get("meals") or []
    return [dict(meal) for meal in meals if isinstance(meal, dict)]


def _planned_next_meals(flags: dict[str, Any]) -> list[dict[str, Any]]:
    """RE9-019: next-meal options the user chose to *plan* (not eat) for later.

    Stored in daily_flags by next_meal.plan_chosen_meal. Surfaced as planned —
    never folded into consumed, preserving the planned/consumed separation.
    """
    planned = flags.get("next_meal_planned") or []
    result: list[dict[str, Any]] = []
    for meal in planned:
        if isinstance(meal, dict) and meal.get("name"):
            result.append({
                "fingerprint": meal.get("fingerprint"),
                "name": str(meal.get("name")),
                "calories": int(meal.get("calories") or 0),
                "protein": int(meal.get("protein") or 0),
                "planned_at": meal.get("planned_at"),
                "source": "next_meal_plan",
            })
    return result


def _target_from_goal(goal: dict[str, Any] | None, key: str, default_name: str) -> int | None:
    value = (goal or {}).get(key)
    if value in (None, ""):
        value = getattr(SETTINGS, default_name, None)
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _restriction_groups(restrictions: list[Any]) -> tuple[list[str], list[str], list[str]]:
    allergies: list[str] = []
    intolerances: list[str] = []
    dietary_rules: list[str] = []
    for restriction in restrictions:
        label = str(getattr(restriction, "canonical_id", "") or "").strip()
        if not label:
            continue
        kind = str(getattr(restriction, "restriction_type", "") or "")
        if kind == "allergy":
            allergies.append(label)
        elif kind in {"intolerance", "sensitivity"}:
            intolerances.append(label)
        else:
            dietary_rules.append(label)
    return sorted(set(allergies)), sorted(set(intolerances)), sorted(set(dietary_rules))


def _warnings(goal: dict[str, Any] | None, meals: list[MealSnapshot], quality: Any, flags: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    if not goal or str(goal.get("status") or "") in {"", "default", "active_provisional"}:
        warnings.append("goal_not_fully_confirmed")
    if not meals:
        warnings.append("no_meals_reported_today")
    for issue in getattr(quality, "issues", []) or []:
        warnings.append(str(issue.code))
    if not flags:
        warnings.append("no_daily_checkin_flags")
    return list(dict.fromkeys(warnings))


async def build_nutrition_context(
    db: Any,
    user_id: int,
    request_type: str,
    *,
    daily_ctx: Any | None = None,
    now: datetime | None = None,
    local_now: datetime | None = None,
    shared_state: SharedUserState | None = None,
) -> NutritionContext:
    """Build the nutrition-side decision snapshot.

    ``shared_state``: pass an already-built ``SharedUserState`` (REC-ARCH-01)
    when the caller already resolved one this request (e.g. a handler that
    also builds a next-meal recommendation or a workout view for the same
    request) so workout state is resolved from the DB exactly once across
    the whole request instead of once per independent caller. When ``now``
    is also given, it must agree with ``shared_state.now`` — ``shared_state``
    is the single source of truth for "now" whenever both are supplied.
    """
    # `local_now` is kept as a compatibility alias for existing regression tests
    # and callers. Prefer `now` in new code. If both are provided, `now` wins.
    resolved_now = (
        (shared_state.now if shared_state is not None else None)
        or now
        or local_now
        or getattr(daily_ctx, "now", None)
        or datetime.now(TZ)
    )
    local_now = resolved_now.astimezone(TZ)
    local_day = await coaching_day_key(db, user_id, local_now)
    flags = dict(getattr(daily_ctx, "flags", None) or await _daily_flags(db, user_id, local_day))
    profile = dict(getattr(daily_ctx, "profile", None) or await _routine_profile(db, user_id))
    meals = await _reported_meals(db, user_id, local_now)
    consumed_calories = round(sum(meal.calories for meal in meals), 1)
    consumed_protein = round(sum(meal.protein for meal in meals), 1)
    consumed_carbs = round(sum(meal.carbs for meal in meals), 1)
    consumed_fat = round(sum(meal.fat for meal in meals), 1)

    goal = await planning.active_goal(db, user_id)
    nutrition_plan = await planning.get_active_plan(db, user_id, "nutrition")
    today_plan = _today_plan(nutrition_plan, local_now)
    planned_meals = [*_planned_meals(today_plan), *_planned_next_meals(flags)]
    expected_meals = max(1, len(planned_meals) or len(((profile.get("eating") or {}).get("typical_meal_hours") or [])) or 3)
    start_utc, end_utc = await coaching_day_bounds_utc(db, user_id, local_now)
    quality = await data_quality.assess_day(db, user_id, start_utc, end_utc, expected_meals=expected_meals)
    workout_context = await build_workout_nutrition_context(
        db,
        user_id,
        now=local_now,
        workout_state=shared_state.workout if shared_state is not None else None,
    )

    # B11/ARCH-15: deliberate RAW reads — an unconfirmed restriction failing
    # CLOSED (over-restricting) is the safe direction for diet/allergy data.
    diet_value = await user_model.get_value(db, user_id, "diet_restrictions")
    allergy_value = await user_model.get_value(db, user_id, "allergies")
    restrictions = load_restrictions_from_facts(
        str(diet_value) if diet_value not in (None, "", "none") else None,
        str(allergy_value) if allergy_value not in (None, "", "none") else None,
    )
    allergies, intolerances, dietary_rules = _restriction_groups(restrictions)
    # B11/ARCH-15: menu-style decisions are confirmed-only — an unconfirmed
    # derived environment profile must not silently steer menu generation.
    food_environment_value = await user_model.get_decision_value(
        db, user_id, "food_environment_context"
    )
    food_environment_context = (
        normalize_food_environment_context(food_environment_value)
        if food_environment_value not in (None, "", "none")
        else None
    )

    # Mark optional flag-backed inputs that have no real value yet so they are
    # sent to the AI as "not_captured" instead of an empty/None default that
    # would read as real (e.g. empty ingredients != "user has no food").
    uncaptured_fields = tuple(
        name
        for name, flag_key in _FLAG_BACKED_OPTIONAL_FIELDS.items()
        if flags.get(flag_key) in (None, "", [], ())
    )

    calorie_target = _target_from_goal(goal, "calories", "default_calories")
    protein_target = _target_from_goal(goal, "protein", "default_protein")
    active_plan_summary = None
    if nutrition_plan:
        active_plan_summary = {
            "id": nutrition_plan.get("id"),
            "title": nutrition_plan.get("title"),
            "strategy": nutrition_plan.get("strategy"),
            "status": nutrition_plan.get("status"),
        }

    return NutritionContext(
        user_id=user_id,
        request_type=request_type,
        generated_at=utc_now(),
        timezone=str(getattr(TZ, "key", "Asia/Jerusalem")),
        current_local_time=local_now.isoformat(),
        day_type=_day_type(local_now),
        calorie_target=calorie_target,
        protein_target=protein_target,
        carbs_target=_target_from_goal(goal, "carbs", "default_carbs"),
        fat_target=_target_from_goal(goal, "fat", "default_fat"),
        consumed_calories=consumed_calories,
        consumed_protein=consumed_protein,
        consumed_carbs=consumed_carbs,
        consumed_fat=consumed_fat,
        remaining_calories=_remaining(calorie_target, consumed_calories),
        remaining_protein=_remaining(protein_target, consumed_protein),
        remaining_carbs=_remaining(_target_from_goal(goal, "carbs", "default_carbs"), consumed_carbs),
        remaining_fat=_remaining(_target_from_goal(goal, "fat", "default_fat"), consumed_fat),
        tracking_completeness=float((quality.metrics or {}).get("reporting_completeness", 0.0)),
        tracking_quality_score=float(quality.score),
        reported_meals=meals,
        planned_meals=planned_meals,
        expected_remaining_meals=workout_context.meals_remaining_estimate,
        hours_until_sleep=workout_context.hours_until_bedtime,
        usual_meal_times=[
            str(value)
            for value in ((profile.get("eating") or {}).get("typical_meal_hours") or [])
            if str(value).strip()
        ],
        workout_status=workout_context.workout_phase.value,
        workout_source=workout_context.workout_source,
        workout_type=(today_plan or {}).get("workout_type"),
        workout_scheduled_time=workout_context.planned_workout_start,
        time_until_workout_minutes=workout_context.minutes_until_workout,
        time_since_workout_minutes=workout_context.minutes_since_workout,
        workout_completed=workout_context.workout_phase
        in {
            WorkoutPhase.POST_WORKOUT_IMMEDIATE,
            WorkoutPhase.POST_WORKOUT_LATER,
            WorkoutPhase.WORKOUT_COMPLETED_EARLIER,
        },
        workout_partial=workout_context.workout_source == "completed_session"
        and workout_context.workout_phase == WorkoutPhase.POST_WORKOUT_LATER,
        workout_cancelled=workout_context.workout_phase == WorkoutPhase.WORKOUT_CANCELLED,
        allergies=allergies,
        intolerances=intolerances,
        dietary_rules=dietary_rules,
        medical_food_constraints=list(getattr(daily_ctx, "active_constraints", []) or []),
        # RAW by policy: avoiding a maybe-disliked food is failing safe.
        disliked_foods=_list_fact(await user_model.get_value(db, user_id, "disliked_foods")),
        preferred_foods=_list_fact(await user_model.get_value(db, user_id, "preferred_foods")),
        # TASK-4: daily-menu/next-meal personalization must use the module's
        # own documented default (min_count=2) so a single one-off meal does
        # not masquerade as a reliable personalization signal. min_count=1 is
        # reserved for calibration/recognition prompts (see
        # learned_foods_prompt_block callers in profile.py), not this context.
        learned_foods=[food.ai_payload() for food in await learned_foods_from_meals(db, user_id, limit=8, min_count=2)],
        recently_rejected_meals=_list_fact(flags.get("recently_rejected_meals")),
        appetite=flags.get("appetite"),
        hunger_level=flags.get("hunger"),
        energy_level=flags.get("energy"),
        sleep_quality=flags.get("sleep_quality"),
        fasting_status=bool(flags.get("fasting")),
        medication_status_reported_today=[
            str(item)
            for item in (flags.get("medications") or [])
            if str(item).strip()
        ],
        relevant_daily_symptoms=_list_fact(flags.get("pain") or flags.get("symptoms")),
        food_environment_context=food_environment_context,
        available_prep_minutes=flags.get("available_prep_minutes"),
        eating_location=flags.get("eating_location"),
        available_equipment=_list_fact(flags.get("available_equipment")),
        available_ingredients=_list_fact(flags.get("available_ingredients")),
        active_weekly_plan=active_plan_summary,
        today_plan=today_plan,
        health_data_freshness={"latest_health_date": getattr(daily_ctx, "latest_health_date", None)},
        data_warnings=_warnings(goal, meals, quality, flags),
        uncaptured_fields=uncaptured_fields,
    )


def build_nutrition_ai_request(
    context: NutritionContext,
    user_request: str,
) -> dict[str, Any]:
    """Build the structured payload every nutrition AI call shares.

    Delegates to the unified Prompt Builder (RE9-034) so the envelope shape and
    safety contract are identical across all AI domains.
    """
    from noam_coach.services.prompt_builder import build_nutrition_request

    return build_nutrition_request(context, user_request)
