"""Tests for assistant.py — intent classification (keyword fallback)."""

from __future__ import annotations

import pytest

import assistant


def test_keyword_ritalin() -> None:
    intent = assistant.keyword_fallback("לקחתי ריטלין הבוקר")
    assert intent.action == "morning_flag"
    assert intent.slots.get("flag") == "ritalin"


def test_keyword_fasting() -> None:
    intent = assistant.keyword_fallback("היום צום")
    assert intent.action == "morning_flag"
    assert intent.slots.get("flag") == "fasting"


def test_keyword_pain() -> None:
    intent = assistant.keyword_fallback("כואב לי הגב")
    assert intent.action == "report_pain"


def test_keyword_next_meal() -> None:
    intent = assistant.keyword_fallback("מה לאכול עכשיו?")
    assert intent.action == "next_meal"


def test_keyword_today_menu() -> None:
    intent = assistant.keyword_fallback("תפריט להיום")
    assert intent.action == "today_menu"


def test_keyword_evening_summary() -> None:
    intent = assistant.keyword_fallback("סיכום יום")
    assert intent.action == "evening_summary"


def test_keyword_build_plan_with_frequency() -> None:
    intent = assistant.keyword_fallback("תבנה לי תוכנית 4 אימונים בשבוע")
    assert intent.action == "build_plan"
    assert intent.slots.get("frequency") == 4


def test_keyword_start_workout() -> None:
    intent = assistant.keyword_fallback("בוא נתאמן")
    assert intent.action == "start_workout"


def test_keyword_weight_update() -> None:
    intent = assistant.keyword_fallback("המשקל שלי 89")
    assert intent.action == "update_measurement"
    assert intent.slots.get("weight_kg") == 89.0


def test_keyword_height_update() -> None:
    intent = assistant.keyword_fallback("הגובה שלי 178")
    assert intent.action == "update_measurement"
    assert intent.slots.get("height_cm") == 178.0


def test_keyword_set_goal() -> None:
    intent = assistant.keyword_fallback("אני רוצה להגיע ל-82")
    assert intent.action == "set_goal"
    assert intent.slots.get("goal_weight") == 82.0


def test_keyword_log_meal() -> None:
    intent = assistant.keyword_fallback("אכלתי 2 ביצים וטוסט")
    assert intent.action == "log_meal_text"


def test_keyword_progress() -> None:
    intent = assistant.keyword_fallback("איך אני מתקדם?")
    assert intent.action == "request_progress"


def test_keyword_profile() -> None:
    intent = assistant.keyword_fallback("מה אתה יודע עליי?")
    assert intent.action == "show_profile"


def test_keyword_unknown() -> None:
    intent = assistant.keyword_fallback("שלום מה קורה")
    assert intent.action == "smalltalk_or_help"
    assert intent.confidence < 0.5


# --- Negation awareness ---


def test_negation_did_not_eat() -> None:
    intent = assistant.keyword_fallback("אני לא אכלתי היום")
    assert intent.action != "log_meal_text"


def test_negation_no_pain() -> None:
    intent = assistant.keyword_fallback("לא כואב לי כלום")
    assert intent.action != "report_pain"


def test_negation_no_workout() -> None:
    intent = assistant.keyword_fallback("לא רוצה אימון היום")
    assert intent.action != "start_workout"


def test_positive_still_works() -> None:
    intent = assistant.keyword_fallback("כואב לי הגב מאוד")
    assert intent.action == "report_pain"


@pytest.mark.asyncio
async def test_classify_intent_empty_text() -> None:
    intent = await assistant.classify_intent(None, "gpt-4", "")
    assert intent.action == "smalltalk_or_help"
    assert intent.confidence == 0.0


@pytest.mark.asyncio
async def test_classify_intent_no_client_uses_keyword() -> None:
    intent = await assistant.classify_intent(None, "gpt-4", "לקחתי ריטלין")
    assert intent.action == "morning_flag"


def test_looks_numeric() -> None:
    assert assistant._looks_numeric("89") == 89.0
    assert assistant._looks_numeric("אין מספר") is None
    assert assistant._looks_numeric("שוקל 92.5") == 92.5


# --- Dietary preference / restriction (P0: "לא אלכוהול" must not be a meal) ---


def test_bare_negated_food_is_restriction_not_meal() -> None:
    intent = assistant.keyword_fallback("לא אלכוהול")
    assert intent.action == "set_dietary_pref"
    assert intent.action != "log_meal_text"
    assert intent.slots.get("polarity") == "avoid"


def test_habitual_negation_is_restriction() -> None:
    intent = assistant.keyword_fallback("אני לא שותה אלכוהול")
    assert intent.action == "set_dietary_pref"
    intent2 = assistant.keyword_fallback("אני לא אוכל בשר")
    assert intent2.action == "set_dietary_pref"


def test_allergy_is_restriction() -> None:
    intent = assistant.keyword_fallback("אני אלרגי לבוטנים")
    assert intent.action == "set_dietary_pref"
    assert intent.slots.get("kind") == "allergy"


def test_diet_label_is_preference() -> None:
    intent = assistant.keyword_fallback("אני צמחוני")
    assert intent.action == "set_dietary_pref"


def test_past_tense_negation_is_not_restriction() -> None:
    # "I didn't eat today" is a report about today, not a standing rule.
    intent = assistant.keyword_fallback("אני לא אכלתי היום")
    assert intent.action != "set_dietary_pref"
    assert intent.action != "log_meal_text"


def test_clean_pref_item_strips_negation() -> None:
    from coach_bot import _clean_pref_item

    assert _clean_pref_item("לא אלכוהול") == "אלכוהול"
    assert _clean_pref_item("אני לא שותה אלכוהול") == "אלכוהול"
    assert _clean_pref_item("אני לא אוכל בשר") == "בשר"


# --- Empty meal guard (P0: cannot save a meal with no food / all zeros) ---


def test_meal_is_meaningful_rejects_empty_and_zero() -> None:
    from models import FoodItem, MealAnalysis

    empty = MealAnalysis(meal_name="ריק", items=[], confidence=0.5)
    assert empty.is_meaningful() is False

    all_zero = MealAnalysis(
        meal_name="אפס",
        items=[FoodItem(name="אלכוהול", grams=0, calories=0, protein=0, carbs=0, fat=0, confidence=0.5)],
        confidence=0.5,
    )
    assert all_zero.is_meaningful() is False

    real = MealAnalysis(
        meal_name="ביצים",
        items=[FoodItem(name="ביצה", grams=100, calories=150, protein=12, carbs=1, fat=10, confidence=0.8)],
        confidence=0.8,
    )
    assert real.is_meaningful() is True
