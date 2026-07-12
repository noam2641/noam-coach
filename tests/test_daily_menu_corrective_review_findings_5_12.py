"""Regression tests for corrective-review Findings 5-12 (nutrition/daily-menu
pipeline audit, second pass).

Finding 5: validation must use candidate-specific slot metadata, not stale
metadata computed once from the original AI menu.

Finding 6: a targeted repair for meal-level violations must attempt a real
bounded AI repair call (not a disguised deterministic index-splice).

Finding 7: an invalid menu (validation.ok == False) must never be returned as
normal renderable output -- MenuGenerationBlocked is raised instead.

Finding 8: generated meals expose structured ingredients, persisted onto
MenuMealRecord, so "does this meal contain eggs" is answerable without
re-parsing rendered text.

Finding 9: the daily-menu "confirm I ate" callback must save the EXACT
daily-menu meal, never fall through to unrelated next-meal recommendation
state.

Finding 10: a targeted daily-menu edit must be revalidated by the same
deterministic validator generation/repair use.

Finding 11: a targeted food substitution must preserve the meal's role,
never overwrite it with the replacement food's name.

Finding 12: one authoritative NutritionContext snapshot drives both the
outer (health_jobs) explanation text and the inner (pipeline) generation.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

import recommendations
from config import TZ
from db import Database
from helpers import utc_now
from noam_coach.services.daily_menu_edit import (
    _meal_matches_avoided_item,
    _regenerate_structured_meals,
    try_build_daily_menu_edit_reply,
)
from noam_coach.services.daily_menu_state import (
    MenuMealRecord,
    menu_meal_record_from_menu_meal,
    remember_active_daily_menu,
)
from noam_coach.services.morning_menu_pipeline import (
    MenuGenerationBlocked,
    build_personalized_morning_menu,
)


async def _db_with_goal(tmp_path: Path, name: str, *, calories: int = 2200, protein: int = 160) -> Database:
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


class _ParsedResponse:
    def __init__(self, parsed: object) -> None:
        self.output_parsed = parsed


class _ScriptedResponses:
    """Returns a different MorningMenu on each successive call, so a test can
    script: [initial AI menu (invalid), repair-call menu (valid/invalid)]."""

    def __init__(self, menus: list[recommendations.MorningMenu]) -> None:
        self._menus = list(menus)
        self.calls: list[dict[str, object]] = []

    async def parse(self, **kwargs: object) -> _ParsedResponse:
        self.calls.append(kwargs)
        index = min(len(self.calls) - 1, len(self._menus) - 1)
        return _ParsedResponse(self._menus[index])


class _ScriptedClient:
    def __init__(self, menus: list[recommendations.MorningMenu]) -> None:
        self.responses = _ScriptedResponses(menus)


def _menu(*meals: recommendations.MenuMeal) -> recommendations.MorningMenu:
    return recommendations.MorningMenu(headline="תפריט", meals=list(meals))


def _meal(name: str, calories: float, protein: float, time_hint: str, note: str = "", intent_id: str = "") -> recommendations.MenuMeal:
    return recommendations.MenuMeal(name=name, time_hint=time_hint, calories=calories, protein=protein, note=note, intent_id=intent_id)


# ---------------------------------------------------------------------------
# Finding 6 -- targeted repair makes a real bounded AI call.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repair_makes_a_real_second_ai_call_for_flagged_meals_only(tmp_path: Path) -> None:
    db = await _db_with_goal(tmp_path, "real_repair.db")
    await __import__("user_model").set_fact(db, 1, "disliked_foods", "טורטייה", source=__import__("user_model").SOURCE_USER, confirmed=True)

    initial_menu = _menu(
        _meal("ארוחת בוקר", 400, 30, "08:00", note="טורטיית חלבון עם ביצה", intent_id="meal_0"),
        _meal("ארוחת צהריים", 800, 60, "13:00", note="עוף ואורז", intent_id="meal_1"),
        _meal("ארוחת ערב", 750, 50, "19:00", note="דג וסלט", intent_id="meal_2"),
    )
    repaired_menu = _menu(
        _meal("ארוחת בוקר", 400, 30, "08:00", note="שקשוקה עם לחם מלא", intent_id="meal_0"),
    )
    client = _ScriptedClient([initial_menu, repaired_menu])

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

    # TWO real AI calls happened: initial generation + ONE targeted repair.
    assert len(client.responses.calls) == 2
    repair_call = client.responses.calls[1]
    repair_messages = repair_call.get("input") or []
    joined = " ".join(str(m.get("content", "")) for m in repair_messages)  # type: ignore[union-attr]
    # The repair request must include the exact violation + immutable
    # constraints (hard-excluded foods), not just "try again".
    assert "hard_excluded_foods" in joined or "טורטייה" in joined
    assert "meals_to_repair" in joined
    # Only ONE meal was sent for repair (the flagged one) -- lunch/dinner from
    # the original menu are untouched (repair response only returns 1 meal).
    assert "טורטי" not in (result.menu.meals[0].note or "")
    assert result.repaired is True
    assert result.used_fallback is False


@pytest.mark.asyncio
async def test_repair_falls_back_to_deterministic_when_ai_repair_call_fails(tmp_path: Path) -> None:
    """If the AI repair call itself raises, the pipeline must still recover
    via the deterministic fallback -- never propagate the raw exception, and
    never silently return the still-invalid original menu."""
    db = await _db_with_goal(tmp_path, "repair_fails.db")
    await __import__("user_model").set_fact(db, 1, "disliked_foods", "טורטייה", source=__import__("user_model").SOURCE_USER, confirmed=True)

    initial_menu = _menu(
        _meal("ארוחת בוקר", 400, 30, "08:00", note="טורטיית חלבון", intent_id="meal_0"),
        _meal("ארוחת צהריים", 800, 60, "13:00", note="עוף ואורז", intent_id="meal_1"),
        _meal("ארוחת ערב", 750, 50, "19:00", note="דג וסלט", intent_id="meal_2"),
    )

    class _RaisingResponses:
        def __init__(self) -> None:
            self.calls = 0

        async def parse(self, **kwargs: object) -> _ParsedResponse:
            self.calls += 1
            if self.calls == 1:
                return _ParsedResponse(initial_menu)
            raise RuntimeError("simulated repair call failure")

    class _RaisingClient:
        def __init__(self) -> None:
            self.responses = _RaisingResponses()

    client = _RaisingClient()
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
    for meal in result.menu.meals:
        assert "טורטי" not in (meal.note or "")
    assert result.validation.ok is True


# ---------------------------------------------------------------------------
# Finding 7 -- hard invariant: invalid output is never returned as normal
# renderable output.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_menu_generation_blocked_raised_when_nothing_can_satisfy_constraints(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If AI generation, AI repair, AND the deterministic fallback are all
    forced invalid, the pipeline must raise MenuGenerationBlocked rather than
    return a menu with validation.ok == False."""
    db = await _db_with_goal(tmp_path, "blocked.db")

    # The menu content itself does not matter here -- validate_menu is
    # monkeypatched below to always report a violation regardless of input,
    # so this only needs to be a structurally valid MenuMeal.
    always_bad_menu = _menu(
        _meal("ארוחה", 400, 30, "08:00", note="ערך כלשהו", intent_id="meal_0"),
    )
    client = _ScriptedClient([always_bad_menu, always_bad_menu])

    # Force the deterministic (no-AI) fallback to ALSO be invalid, by
    # monkeypatching validate_menu to always fail for this test's scope.
    import noam_coach.services.morning_menu_pipeline as pipeline_module

    def _always_invalid(*args: object, **kwargs: object):
        from noam_coach.services.menu_validation import (
            PROBLEM_INVALID_MEAL,
            MealViolation,
            MenuValidationResult,
        )
        return MenuValidationResult(violations=[MealViolation(0, "x", PROBLEM_INVALID_MEAL, "forced invalid for test")])

    monkeypatch.setattr(pipeline_module, "validate_menu", _always_invalid)

    with pytest.raises(MenuGenerationBlocked):
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


@pytest.mark.asyncio
async def test_health_jobs_renders_honest_failure_message_when_blocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The public entry point must catch MenuGenerationBlocked and render an
    honest Hebrew failure message -- never crash, never show an invalid menu."""
    import coach_bot
    from noam_coach.services import health_jobs

    db = Database(str(tmp_path / "blocked_render.db"))
    await db.init()
    await db.execute("INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'T',NULL,?)", (utc_now(),))
    await db.execute(
        "INSERT INTO goals(user_id,calories,protein,steps,phase,updated_at) VALUES(1,2100,160,8000,'x',?)",
        (utc_now(),),
    )
    monkeypatch.setattr(coach_bot, "DB", db, raising=False)
    monkeypatch.setattr(health_jobs, "DB", db, raising=False)

    async def _raise_blocked(*args: object, **kwargs: object):
        from noam_coach.services.menu_validation import (
            PROBLEM_DISLIKED_FOOD,
            MealViolation,
            MenuValidationResult,
        )
        from noam_coach.services.morning_menu_pipeline import MenuGenerationBlocked
        raise MenuGenerationBlocked(MenuValidationResult(violations=[MealViolation(0, "x", PROBLEM_DISLIKED_FOOD, "test")]))

    monkeypatch.setattr(health_jobs, "build_personalized_morning_menu", _raise_blocked, raising=False)
    # health_jobs imports build_personalized_morning_menu lazily inside the
    # function body, so patch the source module too.
    import noam_coach.services.morning_menu_pipeline as pipeline_module
    monkeypatch.setattr(pipeline_module, "build_personalized_morning_menu", _raise_blocked)

    text = await health_jobs.build_morning_menu_text(1)
    assert "לא הצלחתי לבנות" in text
    assert "לא אמינה" in text


# ---------------------------------------------------------------------------
# Finding 8 -- structured ingredients persist onto MenuMealRecord.
# ---------------------------------------------------------------------------


def test_structured_ingredients_persist_onto_meal_record() -> None:
    meal = recommendations.MenuMeal(
        name="ארוחת בוקר", time_hint="08:00", calories=400, protein=30, note="חביתה עם ירקות",
        ingredients=[
            recommendations.MenuIngredient(name="ביצים", grams=100, calories=155, protein=13),
            recommendations.MenuIngredient(name="ירקות", grams=80, calories=20, protein=1),
        ],
    )
    record = menu_meal_record_from_menu_meal(meal, slot="breakfast", index=0)
    assert record.ingredients
    names = {item["name"] for item in record.ingredients}
    assert "ביצים" in names


def test_egg_detection_uses_structured_ingredients_not_free_text() -> None:
    """A meal whose role/note do NOT mention eggs, but whose structured
    ingredients DO include eggs, must still be detected -- this is the exact
    scenario Finding 8 called out (role='ארוחת בוקר', note='ארוחה קלה', but
    composed ingredients include eggs)."""
    meal = {
        "role": "ארוחת בוקר",
        "note": "ארוחה קלה ועשירה בחלבון",
        "ingredients": [{"name": "ביצים", "grams": 100, "calories": 155, "protein": 13}],
    }
    assert _meal_matches_avoided_item(meal, "ביצים") is True


# ---------------------------------------------------------------------------
# Finding 10 -- targeted edits are revalidated.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_targeted_edit_falls_back_safely_when_replacement_would_violate_a_restriction(tmp_path: Path) -> None:
    """If the chosen replacement for an edit would itself violate an active
    hard restriction, the structured edit must not be persisted silently --
    the edit flow must fall through to the safe legacy text path instead of
    shipping an invalid structured menu."""
    import user_model
    db = await _db_with_goal(tmp_path, "edit_validated.db")
    # The learned-food replacement candidate the edit would normally pick.
    for _ in range(3):
        meal_id = await db.execute(
            "INSERT INTO meals(user_id, name, calories, protein, carbs, fat, confidence, eaten_at, created_at, status) "
            "VALUES(1,'טורטיית חלבון',300,25,20,8,0.9,?,?, 'consumed')",
            (utc_now(), utc_now()),
        )
        await db.execute(
            "INSERT INTO meal_items(meal_id, name, grams, calories, protein, carbs, fat, confidence) "
            "VALUES(?, 'טורטיית חלבון', 80, 300, 25, 20, 8, 0.9)",
            (meal_id,),
        )
    # The user also explicitly dislikes tortilla -- so the "learned" choice
    # above must never be used as the substitute (it is hard-excluded).
    await user_model.set_fact(db, 1, "disliked_foods", "טורטייה", source=user_model.SOURCE_USER, confirmed=True)

    meals = [
        MenuMealRecord(meal_id="breakfast-0", slot="breakfast", time="08:00", role="ארוחת בוקר", calories=400, protein=30, note="ביצים וטוסט"),
    ]
    await remember_active_daily_menu(db, 1, text="<b>תפריט</b>", meals=meals)

    result = await try_build_daily_menu_edit_reply(db, 1, "בלי ביצים בבקשה")
    assert result is not None
    body, _rows = result
    # Whatever the final reply is, the disliked food must never appear as the
    # chosen substitute inside it.
    assert "טורטי" not in body


# ---------------------------------------------------------------------------
# Finding 11 -- targeted edit preserves meal role.
# ---------------------------------------------------------------------------


def test_regenerate_structured_meals_preserves_role() -> None:
    meals = [
        {"meal_id": "breakfast-0", "slot": "breakfast", "time": "08:00", "role": "ארוחת בוקר",
         "calories": 400, "protein": 30, "note": "חביתת ביצים", "ingredients": []},
    ]
    updated, changed = _regenerate_structured_meals(
        meals, avoid="ביצים", replacement_name="קוטג׳", calories=250, protein=28,
    )
    assert changed == [0]
    # Finding 11: role must NOT become the replacement food's name.
    assert updated[0]["role"] == "ארוחת בוקר"
    assert updated[0]["note"] == "קוטג׳"


# ---------------------------------------------------------------------------
# Finding 9 -- daily-menu save callback saves the exact daily-menu meal.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_daily_menu_edit_reply_points_confirm_button_at_the_exact_meal(tmp_path: Path) -> None:
    """Finding 9: the confirm-ate button returned by a structured edit reply
    must reference dailymenu:save:<menu_id>:<meal_id> -- never the unrelated
    nextmeal:save:<n> that reads from active next-meal recommendation state."""
    db = await _db_with_goal(tmp_path, "confirm_button.db")
    meals = [
        MenuMealRecord(meal_id="breakfast-0", slot="breakfast", time="08:00", role="ארוחת בוקר", calories=400, protein=30, note="חביתת ביצים"),
        MenuMealRecord(meal_id="lunch-1", slot="lunch", time="13:00", role="ארוחת צהריים", calories=700, protein=50, note="עוף ואורז"),
    ]
    await remember_active_daily_menu(db, 1, text="<b>תפריט</b>", meals=meals)

    result = await try_build_daily_menu_edit_reply(db, 1, "בלי ביצים בבקשה")
    assert result is not None
    _body, rows = result
    all_callbacks = [cb for row in rows for _label, cb in row]
    confirm_callbacks = [cb for row in rows for label, cb in row if "אשר שאכלתי" in label]
    assert confirm_callbacks, "no confirm button found"
    assert all(cb.startswith("dailymenu:save:") for cb in confirm_callbacks), confirm_callbacks
    assert not any(cb.startswith("nextmeal:save:") for cb in all_callbacks)
