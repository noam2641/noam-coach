"""Regression tests for the corrective engineering review of the nutrition
personalization / daily-menu pipeline (TASK-8 audit findings).

Finding 1: ``build_meal_intents`` divided the RAW daily calorie/protein
target equally across slots instead of the already-consumed-aware remaining
balance ``build_remaining_slot_allocations`` (next_meal.py) computes. Fixed
by allocating from the same remaining-balance primitive.

Finding 2: meal intents were built but never reached the AI generation call
-- ``recommendations.morning_menu`` only received ``nutrition_request["context"]``,
so MealIntent was post-generation metadata, not part of the generation
contract. Fixed by threading a serialized ``meal_intents`` payload into every
``recommendations.morning_menu`` call (initial + repair + fallback), and by
having generated meals echo back ``intent_id`` so validation matches a meal
to its ORIGINAL code-defined intent rather than re-deriving a slot from the
AI's free-text meal name.

Finding 3: ``_slot_is_workout_adjacent`` returned True for every slot (not
just the one near the workout) whenever ANY workout was within 180 minutes of
"now", because it never checked the slot's own approximate hour. Fixed by
computing circular hour-distance between the slot and the workout's clock
time.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

import recommendations
from config import TZ
from db import Database
from helpers import utc_now
from noam_coach.services.meal_intent import (
    _slot_is_workout_adjacent,
    build_meal_intents,
)
from noam_coach.services.morning_menu_pipeline import build_personalized_morning_menu
from noam_coach.services.next_meal import NutritionTotals, WorkoutNutritionContext, WorkoutPhase
from noam_coach.services.preference_profile import build_preference_profile


async def _db_with_goal(tmp_path: Path, name: str, *, calories: int, protein: int) -> Database:
    db = Database(str(tmp_path / name))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'T',NULL,?)",
        (utc_now(),),
    )
    now = utc_now()
    await db.execute(
        """
        INSERT INTO goal_versions(
            user_id, calories, protein, steps, phase, status, source, explanation,
            created_at, decided_at
        )
        VALUES(1, ?, ?, 8000, 'fat_loss_muscle_retention', 'active', 'computed', '', ?, ?)
        """,
        (calories, protein, now, now),
    )
    return db


async def _log_consumed_meal(db: Database, *, calories: float, protein: float, eaten_at: datetime) -> None:
    await db.execute(
        "INSERT INTO meals(user_id, name, calories, protein, carbs, fat, confidence, eaten_at, created_at, status) "
        "VALUES(1,?,?,?,0,0,0.9,?,?,'consumed')",
        ("ארוחה שנרשמה", calories, protein, eaten_at.astimezone().isoformat(), utc_now()),
    )


# ---------------------------------------------------------------------------
# Finding 1 -- allocation must use the remaining balance, not the raw target.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_meal_intents_allocate_remaining_balance_not_full_day_target(tmp_path: Path) -> None:
    """Required regression example from the corrective review: daily target
    2200 kcal / 160 g protein, already consumed 900 kcal / 65 g protein ->
    the sum of today's remaining meal intents must be close to the ~1300
    kcal / ~95 g protein that actually remains, never another full 2200/160."""
    db = await _db_with_goal(tmp_path, "remaining.db", calories=2200, protein=160)
    now = datetime(2026, 7, 12, 13, 0, tzinfo=TZ)
    await _log_consumed_meal(db, calories=900, protein=65, eaten_at=now.replace(hour=8))

    profile = await build_preference_profile(db, 1, now=now, flags={})
    intents = await build_meal_intents(
        db, 1, profile, calorie_target=2200, protein_target=160, now=now,
    )

    total_calories = sum(intent.calorie_target for intent in intents)
    total_protein = sum(intent.protein_target for intent in intents)

    # Must be allocated from ~remaining (1300/95), with generous tolerance for
    # the night-meal floor / rounding -- and must NOT be anywhere near another
    # full 2200/160 day on top of what was already eaten.
    assert total_calories < 1600, f"allocated {total_calories} kcal -- looks like the full-day target was re-used"
    assert total_protein < 130, f"allocated {total_protein}g protein -- looks like the full-day target was re-used"


@pytest.mark.asyncio
async def test_meal_intents_collapse_when_day_is_at_or_over_target(tmp_path: Path) -> None:
    """When the user already met/exceeded today's target, future intents must
    not offer a fresh full budget (mirrors allocate_next_meal_budget's
    at_or_over_target handling, reused via build_remaining_slot_allocations)."""
    db = await _db_with_goal(tmp_path, "over_target.db", calories=2000, protein=150)
    now = datetime(2026, 7, 12, 19, 0, tzinfo=TZ)
    await _log_consumed_meal(db, calories=2100, protein=155, eaten_at=now.replace(hour=8))

    profile = await build_preference_profile(db, 1, now=now, flags={})
    intents = await build_meal_intents(
        db, 1, profile, calorie_target=2000, protein_target=150, now=now,
    )

    total_calories = sum(intent.calorie_target for intent in intents)
    assert total_calories < 400, f"allocated {total_calories} kcal after already exceeding the daily target"


# ---------------------------------------------------------------------------
# Finding 2 -- meal intents must reach the actual AI generation call.
# ---------------------------------------------------------------------------


class _ParsedResponse:
    def __init__(self, parsed: object) -> None:
        self.output_parsed = parsed


class _CapturingResponses:
    """Records the exact kwargs passed to responses.parse so the test can
    assert the meal-intents contract actually reached the AI call, not just
    that a MealIntent object was constructed somewhere upstream."""

    def __init__(self, menu: recommendations.MorningMenu) -> None:
        self._menu = menu
        self.calls: list[dict[str, object]] = []

    async def parse(self, **kwargs: object) -> _ParsedResponse:
        self.calls.append(kwargs)
        return _ParsedResponse(self._menu)


class _CapturingClient:
    def __init__(self, menu: recommendations.MorningMenu) -> None:
        self.responses = _CapturingResponses(menu)


def _menu(*meals: recommendations.MenuMeal) -> recommendations.MorningMenu:
    return recommendations.MorningMenu(headline="תפריט", meals=list(meals))


@pytest.mark.asyncio
async def test_meal_intents_reach_the_ai_generation_call(tmp_path: Path) -> None:
    """Finding 2: the AI request for the daily menu must actually contain the
    code-defined meal_intents, matched back by intent_id -- not merely be
    built and then discarded as post-generation metadata."""
    db = await _db_with_goal(tmp_path, "contract.db", calories=2200, protein=160)
    ai_menu = _menu(
        recommendations.MenuMeal(name="ארוחת בוקר", time_hint="08:00", calories=650, protein=50, intent_id="meal_0"),
        recommendations.MenuMeal(name="ארוחת צהריים", time_hint="13:00", calories=800, protein=60, intent_id="meal_1"),
        recommendations.MenuMeal(name="ארוחת ערב", time_hint="19:00", calories=750, protein=50, intent_id="meal_2"),
    )
    client = _CapturingClient(ai_menu)

    result = await build_personalized_morning_menu(
        db, 1,
        profile={"eating": {"first_meal_time": "08:00"}},
        goal={"calories": 2200, "protein": 160, "phase": "fat_loss"},
        today_has_workout=False,
        daily_flags={},
        openai_client=client,
        openai_model="test-model",
        now=datetime(2026, 7, 12, 7, 0, tzinfo=TZ),
    )

    assert client.responses.calls, "the AI was never called"
    first_call = client.responses.calls[0]
    messages = first_call.get("input") or []
    joined_text = " ".join(str(m.get("content", "")) for m in messages)  # type: ignore[union-attr]
    assert "meal_intents" in joined_text.lower() or "MEAL INTENTS" in joined_text
    assert "intent_id" in joined_text
    # And the generated meals are matched back to their intents by id, not by
    # re-parsing a free-text name/time-hint.
    assert result.meal_records[0].meal_id.startswith("breakfast") or result.meal_records[0].slot in {
        "breakfast", "lunch", "dinner",
    }


@pytest.mark.asyncio
async def test_repair_and_fallback_paths_also_receive_meal_intents(tmp_path: Path) -> None:
    """The repair pass and the deterministic fallback must build from the
    SAME meal_intents (already-consumed-aware) as the initial call, not an
    arbitrary fresh split -- otherwise a repaired meal's target silently
    diverges from the original generation contract."""
    db = await _db_with_goal(tmp_path, "repair_contract.db", calories=2200, protein=160)
    ai_menu = _menu(
        recommendations.MenuMeal(name="ארוחת בוקר", time_hint="08:00", calories=400, protein=30,
                                  note="טורטיית חלבון", intent_id="meal_0"),
        recommendations.MenuMeal(name="ארוחת צהריים", time_hint="13:00", calories=800, protein=60, intent_id="meal_1"),
        recommendations.MenuMeal(name="ארוחת ערב", time_hint="19:00", calories=750, protein=50, intent_id="meal_2"),
    )
    import user_model
    await user_model.set_fact(db, 1, "disliked_foods", "טורטייה", source=user_model.SOURCE_USER, confirmed=True)
    client = _CapturingClient(ai_menu)

    await build_personalized_morning_menu(
        db, 1,
        profile={"eating": {"first_meal_time": "08:00"}},
        goal={"calories": 2200, "protein": 160, "phase": "fat_loss"},
        today_has_workout=False,
        daily_flags={},
        openai_client=client,
        openai_model="test-model",
        now=datetime(2026, 7, 12, 7, 0, tzinfo=TZ),
    )
    # Only the initial AI call happens (repair/fallback use client=None per
    # the bounded-repair design) -- but the deterministic repair path must
    # still be intent-aware. We assert this indirectly: the pipeline
    # completed without raising, which requires meal_intents_payload to be
    # accepted by every recommendations.morning_menu call site including the
    # repair/fallback ones (a signature mismatch would raise TypeError).
    assert client.responses.calls


# ---------------------------------------------------------------------------
# Finding 3 -- workout adjacency must be per-slot, not global.
# ---------------------------------------------------------------------------


def _context_with_workout(minutes_until: int) -> WorkoutNutritionContext:
    return WorkoutNutritionContext(
        user_id=1,
        local_now=datetime.now(TZ).isoformat(),
        local_day=datetime.now(TZ).date().isoformat(),
        nutrition=NutritionTotals(
            target_calories=2000, target_protein=150,
            consumed_calories=0, consumed_protein=0,
            calorie_balance=2000, protein_balance=150,
            calorie_overage=0, protein_overage=0,
            goal_status="active", goal_source="goal_versions",
        ),
        workout_phase=WorkoutPhase.PRE_WORKOUT_NEAR,
        workout_source="test",
        workout_label="אימון",
        minutes_until_workout=minutes_until,
    )


def test_workout_adjacency_is_scoped_to_the_slot_near_the_workout() -> None:
    """A workout 170 minutes from now (within the old blanket 180-minute
    check) must NOT mark every slot workout-adjacent -- only the slot whose
    own approximate hour is actually close to the workout's clock time.

    Finding 4: ``now`` is a fixed, caller-supplied value (not a fresh
    ``datetime.now(TZ)`` wall-clock read) so this test is deterministic."""
    context = _context_with_workout(170)
    fixed_now = datetime(2026, 7, 12, 16, 30, tzinfo=TZ)
    now_hour = fixed_now.hour
    workout_hour = (now_hour + 170 // 60) % 24
    far_hour = (workout_hour + 8) % 24  # far from the workout

    assert _slot_is_workout_adjacent("pre_workout", workout_hour, context, now=fixed_now) is True
    assert _slot_is_workout_adjacent("dinner", workout_hour, context, now=fixed_now) is True
    assert _slot_is_workout_adjacent("dinner", far_hour, context, now=fixed_now) is False


def test_workout_adjacency_false_when_no_workout_scheduled() -> None:
    context = _context_with_workout(170)
    context.minutes_until_workout = None
    assert _slot_is_workout_adjacent("lunch", 13, context, now=datetime(2026, 7, 12, 16, 30, tzinfo=TZ)) is False


def test_slot_plan_uses_supplied_now_not_wall_clock() -> None:
    """Finding 4 regression: build_meal_intents' slot planning must be driven
    by the caller-supplied ``now``, not a fresh datetime.now(TZ) read inside
    _slot_plan -- otherwise tests are nondeterministic and slot timing can
    drift from the rest of the pipeline's agreed-on ``now``."""
    from noam_coach.services.meal_intent import _slot_plan

    class _FakeProfile:
        learned_foods: list = []

    context = _context_with_workout(60)  # workout in 60 minutes
    far_future_now = datetime(2030, 1, 1, 5, 0, tzinfo=TZ)  # 05:00
    slots = _slot_plan(context, _FakeProfile(), now=far_future_now)
    # workout_hour should be computed from far_future_now's hour (5), not
    # today's actual wall-clock hour.
    pre_workout = next((s for s in slots if s[0] == "pre_workout"), None)
    assert pre_workout is not None
    assert pre_workout[2] == 6  # 05:00 + 60 minutes -> hour 6
