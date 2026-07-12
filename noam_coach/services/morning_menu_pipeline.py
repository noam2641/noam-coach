"""Generate -> validate -> repair-or-fallback pipeline for the daily menu.

TASK-6/7. Implements the target architecture end to end for the standalone
daily menu:

    CODE CONSTRAINS -> AI COMPOSES -> CODE VALIDATES -> CODE REPAIRS OR REJECTS
    -> SAFE PERSONALIZED OUTPUT -> [BLOCK IF STILL INVALID]

``build_personalized_morning_menu`` is the single entry point
``health_jobs.build_morning_menu_text`` calls. It:

  1. builds the personalization profile (Task 1/2) and nutrition context,
  2. derives meal intents (Task 8) from the same workout/budget primitives
     next_meal uses, allocated against the REMAINING balance (Finding 1),
  3. calls ``recommendations.morning_menu`` with the intents as an
     authoritative generation contract (Finding 2),
  4. deterministically validates the result (Task 5) against metadata
     recomputed from the CANDIDATE actually being validated, never stale
     metadata from the original AI menu (Finding 5),
  5. on a repairable failure, issues exactly ONE targeted AI repair call for
     only the flagged meals (Finding 6) — not a disguised deterministic
     index-splice presented as if it were an AI repair,
  6. on continued failure, falls back to the deterministic (no-AI) menu,
  7. HARD INVARIANT (Finding 7): a menu that still fails validation after
     repair + fallback is never returned as normal renderable output —
     ``MenuGenerationBlocked`` is raised instead, and the caller must render
     an honest failure message, never an invalid menu,
  8. logs observability events (Task 12) at each decision point.
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


class MenuGenerationBlocked(Exception):
    """Finding 7 hard invariant: raised instead of returning a menu whose
    ``validation.ok`` is False. A returned/renderable menu is ALWAYS valid —
    there is no path where invalid output reaches ``build_morning_menu_text``
    and gets rendered to the user. Callers must catch this and render an
    honest "couldn't build a menu that respects your constraints" message,
    never fall back to showing the invalid candidate."""

    def __init__(self, result: MenuValidationResult) -> None:
        self.result = result
        super().__init__(f"menu generation blocked: {sorted(result.hard_fail_codes)}")


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
    """Slot label per generated meal, for slot-affinity checks.

    TASK-8 Finding 2 fix: since ``meal_intents`` are now sent to the AI and
    each ``MenuMeal`` echoes back its ``intent_id``, that is the reliable,
    exact match — the previous role_label-string match broke the moment the
    AI phrased a meal name even slightly differently from the intent's Hebrew
    label, and silently fell through to time-hint guessing. intent_id lookup
    is tried first; role_label match and time-hint parsing remain as
    best-effort fallbacks for older callers / the plain deterministic
    fallback that does not always echo intent_id (e.g. the Ritalin path).
    """
    slots: list[str | None] = []
    intent_by_id = {intent.intent_id: intent.slot for intent in intents if intent.intent_id}
    intent_by_role = {intent.role_label: intent.slot for intent in intents}
    for meal in menu.meals:
        intent_id = str(getattr(meal, "intent_id", "") or "")
        if intent_id and intent_id in intent_by_id:
            slots.append(intent_by_id[intent_id])
            continue
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
    meal_intents_payload: list[dict[str, Any]],
    openai_client: Any,
    openai_model: str,
) -> recommendations.MorningMenu:
    """ONE bounded targeted repair pass (never unbounded retry).

    Finding 6: this is an HONEST targeted repair — when there is an AI client
    and specific meals are flagged, ONE real ``responses.parse`` call is made
    containing ONLY the flagged meals, their exact violations, and the
    immutable hard constraints (never a disguised deterministic index-splice
    presented as if it were an AI repair). Only meals referenced by
    ``affected_meal_indices`` may change; unaffected meals are spliced back in
    untouched. If the AI repair call is unavailable or fails, this falls back
    ONCE to replacing the flagged meals with the deterministic (no-AI)
    fallback's corresponding intent — never a second AI attempt (still
    bounded to one AI call total). A menu-level-only failure (no single meal
    at fault, e.g. calorie total drift) regenerates the whole menu once via
    the deterministic path, since there is no "flagged meal" to target.
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
            meal_intents_payload,
        )

    # Meal-level problem: try ONE real targeted AI repair call first.
    if openai_client is not None:
        try:
            violations_by_index: dict[int, list[str]] = {}
            for violation in result.violations:
                if violation.meal_index is not None:
                    violations_by_index.setdefault(violation.meal_index, []).append(violation.code)
            return await recommendations.repair_menu_meals(
                openai_client,
                openai_model,
                menu=menu,
                affected_meal_indices=sorted(affected),
                violations_by_index=violations_by_index,
                immutable_constraints=repair_request.immutable_constraints,
                meal_intents=meal_intents_payload,
            )
        except Exception:  # noqa: BLE001
            LOGGER.exception("targeted AI repair call failed; falling back to deterministic meal splice")

    # AI repair unavailable/failed: keep unaffected meals, replace only
    # flagged ones with the deterministic fallback's corresponding slot.
    fallback = await recommendations.morning_menu(
        None,
        "repair",
        {},
        goal,
        today_has_workout,
        daily_flags,
        nutrition_context_payload,
        meal_intents_payload,
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
    nutrition_context: Any | None = None,
) -> MorningMenuPipelineResult:
    """Finding 12: ``nutrition_context`` may be supplied by the caller (e.g.
    ``health_jobs.build_morning_menu_text``, which needs its own snapshot for
    the user-facing personalization-strength/quality-gate text) so there is
    ONE authoritative NutritionContext snapshot per generation request —
    remaining budget, meal intents, AI context, validation and the
    user-facing explanation all read the SAME consumed-meal/flags state, not
    two independently-timed reads of the database. When omitted (e.g. direct
    callers/tests), one is built here as before.
    """
    local_now = (now or datetime.now(TZ)).astimezone(TZ)
    pref_profile = await build_preference_profile(db, user_id, now=local_now, flags=daily_flags)
    if nutrition_context is None:
        nutrition_context = await build_nutrition_context(db, user_id, "morning_menu", now=local_now)
    nutrition_request = build_nutrition_ai_request(nutrition_context, "Build today's standalone nutrition menu")

    calorie_target = nutrition_context.calorie_target
    protein_target = nutrition_context.protein_target
    intents = await build_meal_intents(
        db, user_id, pref_profile,
        calorie_target=calorie_target, protein_target=protein_target, now=local_now,
    )
    # TASK-8 Finding 2: the generation contract — code-defined meal
    # opportunities passed as authoritative input to the AI call, not built
    # then discarded as post-generation metadata.
    intents_payload = [intent.ai_payload() for intent in intents]

    menu = await recommendations.morning_menu(
        openai_client,
        openai_model,
        profile,
        goal,
        today_has_workout,
        daily_flags,
        nutrition_request["context"],
        intents_payload,
    )

    restrictions = pref_profile.restrictions
    consumed_keys = _consumed_meal_keys(list(nutrition_context.reported_meals))
    rejected_keys = {
        item.strip().lower() for item in pref_profile.recently_rejected_meals if item.strip()
    }

    def _validate(candidate: recommendations.MorningMenu) -> MenuValidationResult:
        # Finding 5: meal_slots is recomputed FROM THE CANDIDATE actually
        # being validated (matched by intent_id, not by position), so a
        # repaired/reordered/regenerated candidate is never checked against
        # stale slot metadata derived from the ORIGINAL AI menu.
        candidate_slots = _meal_slots_for(candidate, intents)
        return validate_menu(
            candidate,
            restrictions=restrictions,
            profile=pref_profile,
            calorie_target=calorie_target,
            protein_target=protein_target,
            meal_slots=candidate_slots,
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
                meal_intents_payload=intents_payload,
                openai_client=openai_client,
                openai_model=openai_model,
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
                None, openai_model, profile, goal, today_has_workout, daily_flags,
                nutrition_request["context"], intents_payload,
            )
            fallback_result = _validate(fallback_menu)
            await _log_event(db, user_id, "deterministic_menu_fallback_used", {"still_has_violations": not fallback_result.ok})
            menu = fallback_menu
            result = fallback_result
            used_fallback = True

    # Finding 7 hard invariant: a menu is NEVER returned as normal output
    # while validation.ok is False. Repair + deterministic fallback have both
    # already been attempted above; if the day's hard constraints genuinely
    # cannot be satisfied (e.g. the deterministic fallback itself still
    # collides with something), block here rather than silently rendering an
    # unsafe menu.
    if not result.ok:
        await _log_event(db, user_id, "menu_generation_blocked", {"codes": sorted(result.hard_fail_codes)})
        raise MenuGenerationBlocked(result)

    final_slots = _meal_slots_for(menu, intents)
    meal_records = [
        menu_meal_record_from_menu_meal(meal, slot=(final_slots[i] if i < len(final_slots) else "unknown") or "unknown", index=i)
        for i, meal in enumerate(menu.meals)
    ]

    return MorningMenuPipelineResult(
        menu=menu,
        validation=result,
        used_fallback=used_fallback,
        repaired=repaired,
        meal_records=meal_records,
    )
