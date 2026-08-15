"""Tests for recommendations.py — food flags, motivation, fallback menus."""

from __future__ import annotations

import pytest

import recommendations


def test_compute_food_flags_calorie_dense() -> None:
    meals = [{"name": "שוקולד", "grams": 100, "calories": 550, "protein": 5}]
    flags = recommendations.compute_food_flags(meals)
    reasons = [f["reason"] for f in flags]
    assert any("צפיפות" in r for r in reasons)


def test_compute_food_flags_low_protein() -> None:
    meals = [{"name": "לחם לבן", "grams": 200, "calories": 400, "protein": 8}]
    flags = recommendations.compute_food_flags(meals)
    reasons = [f["reason"] for f in flags]
    assert any("חלבון" in r for r in reasons)


def test_compute_food_flags_healthy_food_no_flags() -> None:
    meals = [{"name": "חזה עוף", "grams": 200, "calories": 330, "protein": 62}]
    flags = recommendations.compute_food_flags(meals)
    assert len(flags) == 0


def test_compute_food_flags_ignores_small_portions() -> None:
    # Calories < 150: low protein check shouldn't trigger
    meals = [{"name": "סוכריה", "grams": 20, "calories": 80, "protein": 0}]
    flags = recommendations.compute_food_flags(meals)
    # Only density flag possible (80/20*100=400 >= 350)
    for f in flags:
        assert "חלבון" not in f["reason"]


def test_compute_food_flags_missing_grams() -> None:
    meals = [{"name": "משהו", "grams": 0, "calories": 500, "protein": 10}]
    flags = recommendations.compute_food_flags(meals)
    # No density flag (grams=0), but low protein flag should appear
    reasons = [f["reason"] for f in flags]
    assert any("חלבון" in r for r in reasons)


def test_compute_food_flags_empty() -> None:
    assert recommendations.compute_food_flags([]) == []


@pytest.mark.asyncio
async def test_motivation_message_fallback() -> None:
    msg = await recommendations.motivation_message(None, "gpt-4")
    assert msg in recommendations.MOTIVATION_SEEDS


@pytest.mark.asyncio
async def test_morning_menu_fallback() -> None:
    profile = {"eating": {"first_meal_time": "08:00"}}
    goal = {"calories": 2000, "protein": 150}
    menu = await recommendations.morning_menu(None, "gpt-4", profile, goal, False)
    assert menu.headline == "תפריט הבוקר"
    assert len(menu.meals) == 3
    assert "(ללא AI" in menu.closing


@pytest.mark.asyncio
async def test_morning_menu_fallback_ritalin() -> None:
    profile = {"eating": {"first_meal_time": "08:00"}}
    goal = {"calories": 2000, "protein": 150}
    menu = await recommendations.morning_menu(
        None, "gpt-4", profile, goal, False, daily_flags={"ritalin": True}
    )
    assert len(menu.meals) == 3
    assert "ריטלין" in menu.closing


@pytest.mark.asyncio
async def test_morning_menu_fallback_fasting() -> None:
    profile = {"eating": {"first_meal_time": "08:00"}}
    goal = {"calories": 2000, "protein": 150}
    menu = await recommendations.morning_menu(
        None, "gpt-4", profile, goal, False, daily_flags={"fasting": True}
    )
    assert len(menu.meals) == 0
    assert "צום" in menu.closing


@pytest.mark.asyncio
async def test_morning_menu_fallback_workout() -> None:
    profile = {"eating": {"first_meal_time": "08:00"}}
    goal = {"calories": 2000, "protein": 150}
    menu = await recommendations.morning_menu(None, "gpt-4", profile, goal, True)
    assert "אימון" in menu.training_advice


@pytest.mark.asyncio
async def test_intraday_next_meals_fallback() -> None:
    result = await recommendations.intraday_next_meals(
        None, "gpt-4", {}, 800.0, 60.0, 6.0, False
    )
    assert result.headline
    assert len(result.suggestions) == 2  # splits into 2 meals for long day


@pytest.mark.asyncio
async def test_intraday_fallback_low_calories_warning() -> None:
    result = await recommendations.intraday_next_meals(
        None, "gpt-4", {}, 200.0, 30.0, 8.0, False
    )
    assert result.warning  # should warn about low calories + many hours


@pytest.mark.asyncio
async def test_evening_summary_fallback_on_target() -> None:
    meals = [{"name": "ארוחה", "grams": 300, "calories": 600, "protein": 40}]
    result = await recommendations.evening_summary(
        None, "gpt-4", {}, {"calories": 2000, "protein": 150}, 1950.0, 155.0, meals
    )
    assert result.headline == "סיכום היום"
    assert any("ביעד" in s for s in result.strengths)


@pytest.mark.asyncio
async def test_evening_summary_fallback_over_target() -> None:
    meals = [{"name": "ארוחה", "grams": 300, "calories": 600, "protein": 40}]
    result = await recommendations.evening_summary(
        None, "gpt-4", {}, {"calories": 2000, "protein": 150}, 2500.0, 100.0, meals
    )
    assert any("חריגה" in s for s in result.improvements)


# ---------------------------------------------------------------------------
# W1-20 — a degenerate / thin eating window must not reach the menu-generating
# AI as authoritative learned routine.
#
# These drive the REAL production path: recommendations.morning_menu with a
# client attached, so the prompt actually built and sent is what is asserted on
# (not a helper in isolation).
# ---------------------------------------------------------------------------


class _ParsedResponse:
    def __init__(self, parsed: object) -> None:
        self.output_parsed = parsed


class _CapturingResponses:
    """Records the exact input messages morning_menu sends to the model."""

    def __init__(self) -> None:
        self.inputs: list[dict[str, object]] = []

    async def parse(self, **kwargs: object) -> _ParsedResponse:
        self.inputs.append(kwargs)
        return _ParsedResponse(
            recommendations.MorningMenu(
                headline="ok",
                meals=[
                    recommendations.MenuMeal(
                        name="meal", time_hint="09:00", calories=300, protein=25
                    )
                ],
            )
        )


class _CapturingClient:
    def __init__(self) -> None:
        self.responses = _CapturingResponses()


async def _prompt_for(eating: dict[str, object]) -> str:
    """Run the real morning_menu AI path and return the learned-routine prompt."""
    client = _CapturingClient()
    await recommendations.morning_menu(
        client,
        "test-model",
        {"eating": eating},
        {"calories": 2200, "protein": 160},
        False,
    )
    assert client.responses.inputs, "morning_menu did not call the model"
    messages = client.responses.inputs[0]["input"]
    blocks = [
        m["content"] for m in messages if "שגרה שנלמדה" in str(m.get("content", ""))
    ]
    assert blocks, "learned-routine block missing from the prompt"
    return str(blocks[0])


_DEGENERATE = {
    "first_meal_time": "08:00",
    "last_meal_time": "08:00",
    "typical_meal_hours": ["08:00"],
    "avg_daily_calories": 140.0,
    "meals_sampled": 1,
}
_VALID = {
    "first_meal_time": "08:00",
    "last_meal_time": "21:00",
    "typical_meal_hours": ["08:00", "13:00", "21:00"],
    "avg_daily_calories": 2100.0,
    "meals_sampled": 120,
}
_NARROW_BUT_REAL = {
    "first_meal_time": "12:00",
    "last_meal_time": "17:00",
    "typical_meal_hours": ["12:00", "15:00", "17:00"],
    "avg_daily_calories": 2000.0,
    "meals_sampled": 90,
}


@pytest.mark.asyncio
async def test_degenerate_window_is_not_sent_to_the_ai_as_learned_routine() -> None:
    """The live defect: first == last from one logged day was interpolated raw
    into the menu prompt, so the AI planned a day around a zero-width window."""
    prompt = await _prompt_for(_DEGENERATE)

    assert "ארוחה ראשונה ~08:00" not in prompt
    assert "אחרונה ~08:00" not in prompt
    # And the AI is told *why* it has no window, not left to guess.
    assert "לא נלמדה" in prompt or "אין עדיין" in prompt


@pytest.mark.asyncio
async def test_degenerate_window_keeps_its_calorie_evidence_in_the_prompt() -> None:
    """Withhold the bad interpretation, not the underlying evidence: the daily
    calorie average does not depend on the window's width."""
    prompt = await _prompt_for(_DEGENERATE)
    assert "140.0" in prompt
    # The sample count is surfaced so the AI knows how thin the basis is.
    assert "1" in prompt


@pytest.mark.asyncio
async def test_valid_window_still_reaches_the_ai() -> None:
    prompt = await _prompt_for(_VALID)
    assert "ארוחה ראשונה ~08:00" in prompt
    assert "אחרונה ~21:00" in prompt
    assert "2100.0" in prompt


@pytest.mark.asyncio
async def test_legitimately_narrow_window_still_reaches_the_ai() -> None:
    """ANTI-OVER-CORRECTION: a real 12:00-17:00 fasting routine with 90 meals
    behind it is authoritative and must pass through untouched."""
    prompt = await _prompt_for(_NARROW_BUT_REAL)
    assert "ארוחה ראשונה ~12:00" in prompt
    assert "אחרונה ~17:00" in prompt
    assert "לא נלמדה" not in prompt


@pytest.mark.asyncio
async def test_absent_and_degenerate_windows_read_differently_in_the_prompt() -> None:
    """States 1 and 2 must not collapse into one another."""
    absent = await _prompt_for({})
    degenerate = await _prompt_for(_DEGENERATE)

    assert absent != degenerate
    assert "אין עדיין נתוני שגרת אכילה" in absent
    assert "אין עדיין נתוני שגרת אכילה" not in degenerate


@pytest.mark.asyncio
async def test_no_window_never_emits_a_none_valued_meal_time() -> None:
    """The old block interpolated ``eating.get(...)`` straight in, so an empty
    profile literally sent the AI "ארוחה ראשונה ~None"."""
    prompt = await _prompt_for({})
    eating_line = [
        line for line in prompt.splitlines() if line.startswith("- אכילה")
    ]
    assert eating_line, "eating line missing"
    assert "None" not in eating_line[0]


@pytest.mark.asyncio
async def test_fallback_menu_ignores_a_degenerate_first_meal_time() -> None:
    """recommendations.py:221 — the no-AI fallback used the learned first-meal
    time as its breakfast hint with only a truthiness check."""
    menu = await recommendations.morning_menu(
        None, "gpt-4", {"eating": _DEGENERATE}, {"calories": 2000, "protein": 150}, False
    )
    hints = [m.time_hint for m in menu.meals]
    assert "08:00" not in hints
    assert "בבוקר" in hints


@pytest.mark.asyncio
async def test_fallback_menu_uses_a_well_evidenced_first_meal_time() -> None:
    menu = await recommendations.morning_menu(
        None, "gpt-4", {"eating": _VALID}, {"calories": 2000, "protein": 150}, False
    )
    assert "08:00" in [m.time_hint for m in menu.meals]


@pytest.mark.asyncio
async def test_fallback_menu_uses_a_legitimately_narrow_first_meal_time() -> None:
    menu = await recommendations.morning_menu(
        None,
        "gpt-4",
        {"eating": _NARROW_BUT_REAL},
        {"calories": 2000, "protein": 150},
        False,
    )
    assert "12:00" in [m.time_hint for m in menu.meals]


def test_pydantic_models_validate() -> None:
    meal = recommendations.MenuMeal(
        name="test", time_hint="morning", calories=500, protein=30
    )
    assert meal.name == "test"

    menu = recommendations.MorningMenu(headline="test", meals=[meal])
    assert len(menu.meals) == 1

    summary = recommendations.EveningSummary(
        headline="test", strengths=["a"], improvements=["b"]
    )
    assert summary.headline == "test"
