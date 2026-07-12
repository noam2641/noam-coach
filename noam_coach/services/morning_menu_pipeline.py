"""Generate -> validate -> repair-or-fallback pipeline for the daily menu.

TASK-6/7. Implements the target architecture end to end for the standalone
daily menu:

    CODE CONSTRAINS -> AI COMPOSES -> CODE VALIDATES -> CODE REPAIRS OR REJECTS
    -> SAFE PERSONALIZED OUTPUT

``build_personalized_morning_menu`` is the single entry point
``health_jobs.build_morning_menu_text`` calls. It:

  1. builds the personalization profile (Task 1/2) and nutrition context,
  2. derives meal intents (Task 8) from the same workout/budget primitives
     next_meal uses,
  3. calls ``recommendations.morning_menu`` (still AI-composed),
  4. deterministically validates the result (Task 5), operationalizing every
     key in ``prompt_builder.SAFETY_CONTRACT`` (Task 6),
  5. on a repairable failure, issues exactly ONE targeted repair request that
     only touches the flagged meals, then validates again,
  6. on continued failure, falls back to ``recommendations.morning_menu``'s
     own deterministic (no-AI) fallback, which is already known-safe against
     the same validator constraints via a defensive final scrub,
  7. logs observability events (Task 12) at each decision point.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import recommendations
from config import TZ
from noam_coach.services.daily_menu_state import (
    MenuMealRecord,
    menu_meal_record_from_menu_meal,
)
from noam_coach.services.learned_foods import meal_slot_for_hour
from noam_coach.services.meal_intent import MealIntent, build_meal_intents
from noam_coach.services.menu_validation import (
    MenuValidationResult,
    build_repair_request,
    validate_menu,
)
from noam_coach.services.nutrition_context import (
    build_nutrition_ai_request,
    build_nutrition_context,
)
from noam_coach.services.preference_profile import build_preference_profile

LOGGER = logging.getLogger(__name__)


async def _log_event(db: Any, user_id: int, name: str, properties: dict[str, Any]) -> None:
    """TASK-12: reuse the shared product-event log; observability never breaks the flow."""
    import event_log

    try:
        await event_log.append_event(
            db, user_id, name, entity="daily_menu", source="system", properties=properties
        )
    except Exception:  # noqa: BLE001
        pass


@dataclass(frozen=True)
class MorningMenuPipelineResult:
    menu: recommendations.MorningMenu
    validation: MenuValidationResult
    used_fallback: bool
    repaired: bool
    meal_records: list[MenuMealRecord]


def _consumed_meal_keys(reported_meals: list[Any]) -> set[str]:
    from noam_coach.services.next_meal import _free_text_preference_key

    return {
        _free_text_preference_key(str(getattr(meal, "name", "") or ""))
        for meal in reported_meals
        if str(getattr(meal, "name", "") or "").strip()
    }


def _meal_slots_for(menu: recommendations.MorningMenu, intents: list[MealIntent]) -> list[str | None]:
    """Best-effort slot label per generated meal, for slot-affinity checks."""
    slots: list[str | None] = []
    intent_by_role = {intent.role_label: intent.slot for intent in intents}
    for meal in menu.meals:
        name = str(getattr(meal, "name", "") or "")
        if name in intent_by_role:
            slots.append(intent_by_role[name])
            continue
        time_hint = str(getattr(meal, "time_hint", "") or "")
        hour = None
        if ":" in time_hint:
            head = time_hint.split()[0] if time_hint.split() else time_hint
            try:
                hour = int(head.split(":", 1)[0])
            except ValueError:
                hour = None
        slots.append(meal_slot_for_hour(hour) if hour is not None else None)
    return slots


async def _repair_menu(
    *,
    menu: recommendations.MorningMenu,
    result: MenuValidationResult,
    profile: Any,
    calorie_target: float | None,
    protein_target: float | None,
    restrictions: list[Any],
    goal: dict[str, Any],
    today_has_workout: bool,
    daily_flags: dict[str, Any],
    nutrition_context_payload: dict[str, Any],
) -> recommendations.MorningMenu:
    """ONE bounded targeted repair pass (never unbounded retry).

    Only the meals flagged by the validator are eligible to change; a
    menu-level-only failure (e.g. calorie total drift, non-chronological
    order) regenerates the whole menu once since no single meal is at fault.
    Unaffected meals are copied through untouched.
    """
    repair_request = build_repair_request(
        menu, result, calorie_target=calorie_target, protein_target=protein_target, restrictions=restrictions
    )
    affected = repair_request.affected_meal_indices

    if not affected:
        # Menu-level problem only: one full regeneration attempt.
        return await recommendations.morning_menu(
            None,  # deterministic path: menu-level repairs must not loop through AI again
            "repair",
            {},
            goal,
            today_has_workout,
            daily_flags,
            nutrition_context_payload,
        )

    # Meal-level problem: keep unaffected meals, replace only flagged ones
    # with the deterministic fallback's corresponding slot when available.
    fallback = await recommendations.morning_menu(
        None,
        "repair",
        {},
        goal,
        today_has_workout,
        daily_flags,
        nutrition_context_payload,
    )
    repaired_meals = list(menu.meals)
    for index in sorted(affected):
        if index >= len(repaired_meals):
            continue
        replacement = fallback.meals[index] if index < len(fallback.meals) else None
        if replacement is not None:
            repaired_meals[index] = replacement
    return recommendations.MorningMenu(
        headline=menu.headline,
        meals=repaired_meals,
        training_advice=menu.training_advice,
        closing=menu.closing,
    )


async def build_personalized_morning_menu(
    db: Any,
    user_id: int,
    *,
    profile: dict[str, Any],
    goal: dict[str, Any],
    today_has_workout: bool,
    daily_flags: dict[str, Any],
    openai_client: Any,
    openai_model: str,
    now: datetime | None = None,
) -> MorningMenuPipelineResult:
    local_now = (now or datetime.now(TZ)).astimezone(TZ)
    pref_profile = await build_preference_profile(db, user_id, now=local_now, flags=daily_flags)
    nutrition_context = await build_nutrition_context(db, user_id, "morning_menu", now=local_now)
    nutrition_request = build_nutrition_ai_request(nutrition_context, "Build today's standalone nutrition menu")

    calorie_target = nutrition_context.calorie_target
    protein_target = nutrition_context.protein_target
    intents = await build_meal_intents(
        db, user_id, pref_profile,
        calorie_target=calorie_target, protein_target=protein_target, now=local_now,
    )

    menu = await recommendations.morning_menu(
        openai_client,
        openai_model,
        profile,
        goal,
        today_has_workout,
        daily_flags,
        nutrition_request["context"],
    )

    restrictions = pref_profile.restrictions
    meal_slots = _meal_slots_for(menu, intents)
    consumed_keys = _consumed_meal_keys(list(nutrition_context.reported_meals))
    rejected_keys = {
        item.strip().lower() for item in pref_profile.recently_rejected_meals if item.strip()
    }

    def _validate(candidate: recommendations.MorningMenu) -> MenuValidationResult:
        return validate_menu(
            candidate,
            restrictions=restrictions,
            profile=pref_profile,
            calorie_target=calorie_target,
            protein_target=protein_target,
            meal_slots=meal_slots,
            recently_rejected_keys=rejected_keys,
            consumed_meal_keys=consumed_keys,
        )

    result = _validate(menu)
    used_fallback = False
    repaired = False

    if not result.ok:
        for code in sorted(result.hard_fail_codes):
            await _log_event(db, user_id, "menu_validation_failed", {"code": code})
        if any(v.code == "disliked_food" for v in result.violations):
            await _log_event(db, user_id, "disliked_food_rejected", {"count": len(result.violations)})
        if any(v.code == "restriction_violation" for v in result.violations):
            await _log_event(db, user_id, "restriction_violation_rejected", {"count": len(result.violations)})
        if any(v.code == "slot_affinity_violation" for v in result.violations):
            await _log_event(db, user_id, "slot_affinity_violation", {"count": len(result.violations)})

        await _log_event(db, user_id, "menu_repair_requested", {"affected_meals": len(result.affected_meal_indices)})
        try:
            repaired_menu = await _repair_menu(
                menu=menu,
                result=result,
                profile=pref_profile,
                calorie_target=calorie_target,
                protein_target=protein_target,
                restrictions=restrictions,
                goal=goal,
                today_has_workout=today_has_workout,
                daily_flags=daily_flags,
                nutrition_context_payload=nutrition_request["context"],
            )
        except Exception:  # noqa: BLE001
            LOGGER.exception("menu repair raised; falling back to deterministic menu")
            repaired_menu = None

        repaired_result = _validate(repaired_menu) if repaired_menu is not None else None
        if repaired_menu is not None and repaired_result is not None and repaired_result.ok:
            await _log_event(db, user_id, "menu_repair_succeeded", {})
            menu = repaired_menu
            result = repaired_result
            repaired = True
        else:
            await _log_event(db, user_id, "menu_repair_failed", {})
            # Deterministic safe fallback (never AI, so it cannot reintroduce
            # the same class of violation from model drift).
            fallback_menu = await recommendations.morning_menu(
                None, openai_model, profile, goal, today_has_workout, daily_flags, nutrition_request["context"],
            )
            fallback_result = _validate(fallback_menu)
            await _log_event(db, user_id, "deterministic_menu_fallback_used", {"still_has_violations": not fallback_result.ok})
            menu = fallback_menu
            result = fallback_result
            used_fallback = True

    meal_records = [
        menu_meal_record_from_menu_meal(meal, slot=(meal_slots[i] if i < len(meal_slots) else "unknown") or "unknown", index=i)
        for i, meal in enumerate(menu.meals)
    ]

    return MorningMenuPipelineResult(
        menu=menu,
        validation=result,
        used_fallback=used_fallback,
        repaired=repaired,
        meal_records=meal_records,
    )
