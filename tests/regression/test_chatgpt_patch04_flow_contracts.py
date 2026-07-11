"""PATCH-04 source-level contracts for the audited Telegram nutrition/training flows.

These tests intentionally read source files instead of importing the bot runtime.
They keep the audit executable even in a minimal environment that lacks Telegram,
aiosqlite or OpenAI packages, and they protect the product contracts extracted
from the screenshots/backlog.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _src(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_next_meal_screen_contract_is_single_immediate_recommendation() -> None:
    src = _src("noam_coach/services/next_meal.py")
    assert "[:1]" in src, "next-meal recommendation must expose only one option"
    assert "✅ אשר שאכלתי" in src
    assert "🔄 רענן הצעה" in src
    assert "✏️ שנה כמויות" in src
    assert "📊 חזור לסיכום היום" in src
    assert "תכנן אפשרות" not in src
    assert "אכלתי אפשרות" not in src
    assert "אפשרות {index}" not in src
    assert "אפשר לעדכן בכפתורים" not in src


def test_menu_daily_menu_is_the_single_daily_menu_entrypoint() -> None:
    menu_src = _src("noam_coach/bot/callback_menu.py")
    ui_src = _src("noam_coach/bot/ui.py")
    assert "menu:daily_menu" in menu_src
    assert "menu:refresh_daily_menu" in menu_src
    assert "menu:replace_daily_meal" in menu_src
    assert "menu:daily_menu" in ui_src
    assert "build_morning_menu_text(user_id)" in menu_src


def test_post_meal_confirmation_keeps_its_four_action_buttons() -> None:
    src = _src("noam_coach/bot/callback_meals.py")
    assert "render_post_meal_confirmation_day_status" in src
    assert "🍽️ מה לאכול עכשיו" in src
    assert "🏋️ סמן אימון" in src
    assert "✏️ ערוך ארוחה" in src
    assert "📊 מצב היום" in src
    assert "build_daily_status" not in src[src.find("approve_meal:"):src.find("undo_meal:")]


def test_manual_training_days_are_active_plan_source() -> None:
    availability_src = _src("noam_coach/services/availability.py")
    onboarding_src = _src("noam_coach/bot/onboarding.py")
    user_model_src = _src("user_model.py")
    assert "active_training_days" in availability_src
    assert "preferred_training_days" in availability_src
    assert "detected_training_days" in availability_src
    assert "save_user_training_availability" in onboarding_src
    assert "resolve_availability" in onboarding_src
    assert '"active_training_days"' in user_model_src
    assert "HealthKit may propose workout days" in availability_src


def test_learned_foods_are_connected_to_analysis_menu_and_next_meal() -> None:
    learned_src = _src("noam_coach/services/learned_foods.py")
    profile_src = _src("noam_coach/services/profile.py")
    nutrition_src = _src("noam_coach/services/nutrition_context.py")
    next_meal_src = _src("noam_coach/services/next_meal.py")
    recommendations_src = _src("recommendations.py")
    assert "FROM meal_items" in learned_src and "JOIN meals" in learned_src
    assert "learned_foods_prompt_block" in profile_src
    assert "learned_foods_from_meals" in nutrition_src
    assert "learned_food_keys" in next_meal_src
    assert "text_matches_learned_food" in next_meal_src
    assert "_learned_food_names" in recommendations_src


def test_training_catalog_exposes_professional_metadata_and_substitutions() -> None:
    src = _src("training_intelligence.py")
    assert "class ClientTrainingProfile" in src
    assert "client_training_profile_from_facts" in src
    assert "movement_pattern" in src
    assert "equipment_required" in src
    assert "difficulty_level" in src
    assert "coaching_cues" in src
    assert "common_mistakes" in src
    assert "safe_range_notes" in src
    assert "joint_load" in src
    assert "regressions" in src
    assert "progressions" in src
    assert "technique_cues" in src
    assert "exercise_catalog_entry" in src
    assert "substitutions_for_exercise" in src


def test_workout_plan_payload_stores_training_profile_and_rationale() -> None:
    src = _src("planning.py")
    assert '"training_profile": training_profile.public_payload()' in src
    assert '"plan_rationale"' in src
    assert '"why_this_split": list(rationale)' in src
    assert '"progression_rule": "double_progression_rir_with_pain_hold"' in src
    assert '"safety_rule": "do_not_increase_load_when_active_pain_matches_joint_load"' in src
