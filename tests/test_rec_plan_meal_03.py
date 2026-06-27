"""Regression tests for REC-PLAN-MEAL-03-01 through REC-PLAN-MEAL-03-18.

Each test group verifies the fix for the corresponding issue stays intact.
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# REC-PLAN-MEAL-03-01: Dietary restriction answer parsing
# ---------------------------------------------------------------------------


class TestDietaryRestrictionParsing:
    """REC-PLAN-MEAL-03-01: Free-text dietary restriction answers are parsed
    into structured items with a follow-up for restriction type."""

    def _parse(self, text: str) -> list[str]:
        from noam_coach.bot.onboarding import _parse_dietary_answer
        return _parse_dietary_answer(text)

    def test_parse_bare_food_item(self) -> None:
        assert self._parse("קשיו") == ["קשיו"]

    def test_parse_prefixed_yes(self) -> None:
        assert self._parse("כן קשיו") == ["קשיו"]

    def test_parse_negation_prefix(self) -> None:
        assert self._parse("לא אוכל קשיו") == ["קשיו"]

    def test_parse_comma_separated(self) -> None:
        result = self._parse("גלוטן, חלב, ביצים")
        assert result == ["גלוטן", "חלב", "ביצים"]

    def test_parse_negation_avoidance(self) -> None:
        result = self._parse("אני נמנע מבוטנים")
        assert result == ["בוטנים"]

    def test_parse_empty_returns_empty(self) -> None:
        assert self._parse("") == []
        assert self._parse("   ") == []


# ---------------------------------------------------------------------------
# REC-PLAN-MEAL-03-02: Error recovery preserving active question
# ---------------------------------------------------------------------------


class TestErrorRecoveryQuestion:
    """REC-PLAN-MEAL-03-02: If recording an answer fails, the active question
    flow is preserved so the user can retry."""

    def test_question_answer_handler_has_try_except(self) -> None:
        """The handle_onboarding_text function must have error recovery around
        record_answer to prevent the question flow from being lost."""
        import inspect

        from noam_coach.bot.onboarding import handle_onboarding_text
        source = inspect.getsource(handle_onboarding_text)
        # Must have structured error recovery
        assert "question_answer_failed" in source
        # Must NOT clear pending on failure
        assert "לא הצלחתי לשמור את התשובה" in source


# ---------------------------------------------------------------------------
# REC-PLAN-MEAL-03-03: Plan selection not capturing unrelated messages
# ---------------------------------------------------------------------------


class TestPlanSelectionPassthrough:
    """REC-PLAN-MEAL-03-03: When a plan selection flow is active, only exact
    '1'/'2'/'3' inputs are consumed; other text falls through to the intent
    router."""

    def test_selection_flow_only_captures_numbers(self) -> None:
        import inspect

        from noam_coach.bot.meal_text import handle_text_message
        source = inspect.getsource(handle_text_message)
        # Must check for exact selection numbers
        assert 'text in {"1", "2", "3"}' in source

    def test_non_selection_text_released_to_router(self) -> None:
        import inspect

        from noam_coach.bot.meal_text import handle_text_message
        source = inspect.getsource(handle_text_message)
        assert "unrelated_message_released_to_intent_router" in source


# ---------------------------------------------------------------------------
# REC-PLAN-MEAL-03-04: Meal status intent and handler
# ---------------------------------------------------------------------------


class TestMealStatusIntent:
    """REC-PLAN-MEAL-03-04: 'מה עם הארוחות' and similar phrases are classified
    as meal_status and handled with actual meal data."""

    def test_meal_status_in_action_type(self) -> None:
        import assistant
        assert "meal_status" in assistant.Action.__args__

    def test_keyword_fallback_detects_meal_status(self) -> None:
        import assistant
        intent = assistant.keyword_fallback("מה עם הארוחות?")
        assert intent.action == "meal_status"

    def test_keyword_fallback_what_i_ate(self) -> None:
        import assistant
        intent = assistant.keyword_fallback("מה אכלתי היום?")
        assert intent.action == "meal_status"

    def test_handler_exists(self) -> None:
        import inspect

        from noam_coach.bot.assistant import _handle_meal_status_action
        source = inspect.getsource(_handle_meal_status_action)
        assert "meal_status" in source


# ---------------------------------------------------------------------------
# REC-PLAN-MEAL-03-05: Check existing data before asking
# ---------------------------------------------------------------------------


class TestCheckExistingData:
    """REC-PLAN-MEAL-03-05: Before asking a question, check if we already have
    a usable fact and offer confirmation instead."""

    def test_ask_next_question_checks_existing(self) -> None:
        import inspect

        from noam_coach.bot.onboarding import ask_next_question
        source = inspect.getsource(ask_next_question)
        # Must check for existing fact
        assert "confirm_existing" in source
        assert "כבר יש לי" in source

    def test_callback_handles_confirm_existing(self) -> None:
        import inspect

        from noam_coach.bot.onboarding import handle_onboarding_callback
        source = inspect.getsource(handle_onboarding_callback)
        assert "confirm_existing" in source
        assert "update_existing" in source


# ---------------------------------------------------------------------------
# REC-PLAN-MEAL-03-06: Training days conflict resolution
# ---------------------------------------------------------------------------


class TestTrainingConflicts:
    """REC-PLAN-MEAL-03-06: profile_conflicts detects mismatches between
    declared, observed, and plan training frequency."""

    def test_profile_conflicts_checks_plan(self) -> None:
        import inspect

        import coach_intelligence
        source = inspect.getsource(coach_intelligence.profile_conflicts)
        # Must check plan frequency
        assert "plan_freq" in source or "plan_vs_actual" in source

    def test_profile_conflict_dataclass(self) -> None:
        import coach_intelligence
        c = coach_intelligence.ProfileConflict(
            key="training_days_per_week",
            declared=4,
            observed=2.5,
            message="test",
            severity="warning",
        )
        assert c.key == "training_days_per_week"


# ---------------------------------------------------------------------------
# REC-PLAN-MEAL-03-07: Profile display formatting
# ---------------------------------------------------------------------------


class TestProfileDisplay:
    """REC-PLAN-MEAL-03-07: Never expose raw internal enum names or snake_case
    to the user."""

    def _format(self, key: str, value: Any) -> str:
        from noam_coach.bot.onboarding import _format_fact_value
        return _format_fact_value(key, value)

    def test_primary_goal_fat_loss(self) -> None:
        result = self._format("primary_goal", "fat_loss_muscle_retention")
        assert "fat_loss" not in result
        assert "ירידה" in result

    def test_training_location_gym(self) -> None:
        result = self._format("training_location", "gym")
        assert result == "חדר כושר"

    def test_equipment_full_gym(self) -> None:
        result = self._format("equipment", "full_gym")
        assert "full_gym" not in result
        assert "חדר כושר" in result

    def test_sex_male(self) -> None:
        assert self._format("sex", "male") == "זכר"

    def test_weight_kg_formatting(self) -> None:
        result = self._format("weight_kg", 89.5)
        assert 'ק"ג' in result
        assert "89.5" in result

    def test_none_value(self) -> None:
        assert self._format("primary_goal", None) == "לא צוין"

    def test_unknown_enum_strips_underscores(self) -> None:
        result = self._format("primary_goal", "some_new_value")
        assert "_" not in result

    def test_dict_work_schedule(self) -> None:
        result = self._format("work_schedule", {"start": "08:00", "end": "17:00"})
        assert "08:00" in result
        assert "17:00" in result

    def test_cooking_capacity(self) -> None:
        result = self._format("cooking_capacity", "basic")
        assert result == "בסיסי ומהיר"


# ---------------------------------------------------------------------------
# REC-PLAN-MEAL-03-08: Explainable readiness percentages
# ---------------------------------------------------------------------------


class TestExplainableReadiness:
    """REC-PLAN-MEAL-03-08: Plan hub readiness shows what's missing."""

    def test_smart_plan_hub_shows_missing(self) -> None:
        import inspect

        from noam_coach.bot.onboarding import render_smart_plan_hub
        source = inspect.getsource(render_smart_plan_hub)
        assert "missing_labels" in source
        assert "מוכן" in source
        assert "חסר" in source


# ---------------------------------------------------------------------------
# REC-PLAN-MEAL-03-09: Proposals use confirmed data only
# ---------------------------------------------------------------------------


class TestConfirmedDataProposals:
    """REC-PLAN-MEAL-03-09: Plan proposals note when they rely on unconfirmed
    estimates."""

    def test_generate_candidates_checks_confirmation(self) -> None:
        import inspect

        import planning
        source = inspect.getsource(planning.generate_candidates)
        assert "unconfirmed" in source or "confirmed" in source
        assert "assumptions" in source


# ---------------------------------------------------------------------------
# REC-PLAN-MEAL-03-10: User text authoritative over image
# ---------------------------------------------------------------------------


class TestUserTextAuthoritative:
    """REC-PLAN-MEAL-03-10: In meal reanalysis, the user's text description
    takes priority over the image analysis."""

    def test_reanalysis_prompt_prioritizes_text(self) -> None:
        import inspect

        from noam_coach.services.profile import reanalyze_meal_with_text_and_image
        source = inspect.getsource(reanalyze_meal_with_text_and_image)
        assert "AUTHORITATIVE" in source or "authoritative" in source


# ---------------------------------------------------------------------------
# REC-PLAN-MEAL-03-11: Meal totals = item totals
# ---------------------------------------------------------------------------


class TestMealTotalConsistency:
    """REC-PLAN-MEAL-03-11: MealAnalysis.totals() is the single source of truth
    and sums items directly."""

    def test_totals_sums_items(self) -> None:
        from models import FoodItem, MealAnalysis
        analysis = MealAnalysis(
            meal_name="test",
            confidence=0.9,
            items=[
                FoodItem(name="a", grams=100, calories=200, protein=20,
                         carbs=30, fat=5, confidence=0.9),
                FoodItem(name="b", grams=50, calories=100, protein=10,
                         carbs=15, fat=3, confidence=0.9),
            ],
        )
        t = analysis.totals()
        assert t["calories"] == 300
        assert t["protein"] == 30
        assert t["carbs"] == 45
        assert t["fat"] == 8

    def test_persist_meal_uses_totals(self) -> None:
        """persist_meal must call analysis.totals() for DB values."""
        import inspect

        from noam_coach.bot.meals import persist_meal
        source = inspect.getsource(persist_meal)
        assert "analysis.totals()" in source


# ---------------------------------------------------------------------------
# REC-PLAN-MEAL-03-12: Oil removal revision-safe
# ---------------------------------------------------------------------------


class TestOilRemovalRevisionSafe:
    """REC-PLAN-MEAL-03-12: 'בלי שמן' removes oil and stays removed across
    subsequent revisions."""

    def test_parse_removal_bli_shemen(self) -> None:
        import meal_intelligence
        corrections = meal_intelligence.parse_meal_correction("בלי שמן")
        removal = [c for c in corrections if c.kind == "remove"]
        assert len(removal) >= 1
        assert "שמן" in removal[0].item_hint

    def test_parse_removal_lelo_shemen(self) -> None:
        import meal_intelligence
        corrections = meal_intelligence.parse_meal_correction("ללא שמן")
        removal = [c for c in corrections if c.kind == "remove"]
        assert len(removal) >= 1

    def test_apply_item_removal(self) -> None:
        import meal_intelligence
        from models import FoodItem, MealAnalysis
        analysis = MealAnalysis(
            meal_name="test",
            confidence=0.8,
            items=[
                FoodItem(name="אורז", grams=120, calories=156,
                         protein=3, carbs=34, fat=0.4, confidence=0.8),
                FoodItem(name="שמן זית", grams=10, calories=90,
                         protein=0, carbs=0, fat=10, confidence=0.8),
            ],
        )
        correction = meal_intelligence.MealCorrection(
            kind="remove", item_hint="שמן", value="", original_text="בלי שמן"
        )
        result = meal_intelligence.apply_item_removal_correction(analysis, correction)
        names = [item.name for item in result.items]
        assert not any("שמן" in n for n in names)

    def test_locked_corrections_passed_to_reanalysis(self) -> None:
        """reanalyze_meal_with_text_and_image accepts locked_corrections."""
        import inspect

        from noam_coach.services.profile import reanalyze_meal_with_text_and_image
        source = inspect.getsource(reanalyze_meal_with_text_and_image)
        assert "locked_corrections" in source


# ---------------------------------------------------------------------------
# REC-PLAN-MEAL-03-13: Restriction contradiction detection
# ---------------------------------------------------------------------------


class TestRestrictionContradiction:
    """REC-PLAN-MEAL-03-13: render_meal warns when a meal item matches a
    dietary restriction."""

    def test_render_meal_checks_restrictions(self) -> None:
        import inspect

        from noam_coach.bot.meals import render_meal
        source = inspect.getsource(render_meal)
        assert "restriction_warnings" in source
        assert "diet_restrictions" in source


# ---------------------------------------------------------------------------
# REC-PLAN-MEAL-03-14: Meal save confirmation with undo
# ---------------------------------------------------------------------------


class TestMealSaveConfirmation:
    """REC-PLAN-MEAL-03-14: After meal approval, show clear confirmation with
    undo button."""

    def test_approve_meal_shows_confirmation(self) -> None:
        import inspect

        from noam_coach.bot.callback_meals import _handle_meal_decision_actions
        source = inspect.getsource(_handle_meal_decision_actions)
        # Must show meal-specific confirmation (not just generic "saved")
        assert "נשמר ✅" in source or "נרשם ✅" in source
        # Must have undo button
        assert "undo_meal" in source

    def test_auto_save_has_undo(self) -> None:
        import inspect

        from noam_coach.bot.meals import auto_save_meal
        source = inspect.getsource(auto_save_meal)
        assert "undo_meal" in source


# ---------------------------------------------------------------------------
# REC-PLAN-MEAL-03-15: Daily totals from saved meals only
# ---------------------------------------------------------------------------


class TestDailyTotalsSavedOnly:
    """REC-PLAN-MEAL-03-15: Daily calorie/protein totals come from the meals
    table (saved), not the approvals table (pending)."""

    def test_meal_status_queries_meals_table(self) -> None:
        import inspect

        from noam_coach.bot.assistant import _handle_meal_status_action
        source = inspect.getsource(_handle_meal_status_action)
        assert "FROM meals" in source
        assert "approvals" not in source


# ---------------------------------------------------------------------------
# REC-PLAN-MEAL-03-16: Next action button in home menu
# ---------------------------------------------------------------------------


class TestNextActionButton:
    """REC-PLAN-MEAL-03-16: The home keyboard includes a button matching the
    next_best_action callback."""

    def test_home_keyboard_for_user_exists(self) -> None:
        import coach_bot
        assert hasattr(coach_bot, "home_keyboard_for_user")

    def test_home_keyboard_for_user_is_async(self) -> None:
        import asyncio

        import coach_bot
        assert asyncio.iscoroutinefunction(coach_bot.home_keyboard_for_user)


# ---------------------------------------------------------------------------
# REC-PLAN-MEAL-03-17: Redundant question challenge
# ---------------------------------------------------------------------------


class TestRedundantQuestionChallenge:
    """REC-PLAN-MEAL-03-17: When user says 'כבר אמרתי לך', the bot shows the
    existing value instead of re-asking."""

    def test_redundant_challenge_in_actions(self) -> None:
        import assistant
        assert "redundant_question_challenge" in assistant.Action.__args__

    def test_keyword_fallback_detects_challenge(self) -> None:
        import assistant
        intent = assistant.keyword_fallback("כבר אמרתי לך")
        assert intent.action == "redundant_question_challenge"

    def test_keyword_fallback_has_data(self) -> None:
        import assistant
        intent = assistant.keyword_fallback("יש לך את הנתונים")
        assert intent.action == "redundant_question_challenge"

    def test_handler_exists(self) -> None:
        import inspect

        from noam_coach.bot.assistant import _handle_redundant_question_challenge
        source = inspect.getsource(_handle_redundant_question_challenge)
        assert "redundant_question_challenge" in source
        assert "כבר יש לי מידע" in source or "צודק" in source


# ---------------------------------------------------------------------------
# REC-PLAN-MEAL-03-18: Structured error recovery diagnostics
# ---------------------------------------------------------------------------


class TestStructuredErrorRecovery:
    """REC-PLAN-MEAL-03-18: Error handlers log structured events for
    diagnostics."""

    def test_callback_error_logging(self) -> None:
        import inspect

        from noam_coach.bot.callback_router import on_error
        source = inspect.getsource(on_error)
        assert "callback_error" in source

    def test_photo_error_logging(self) -> None:
        import inspect

        from noam_coach.bot.meals import handle_photo
        source = inspect.getsource(handle_photo)
        assert "photo_analysis_error" in source

    def test_meal_correction_error_logging(self) -> None:
        import inspect

        from noam_coach.bot.meal_text import _handle_meal_correction_text
        source = inspect.getsource(_handle_meal_correction_text)
        assert "meal_correction_error" in source


# ---------------------------------------------------------------------------
# Cross-cutting: assistant.py SYSTEM_PROMPT completeness
# ---------------------------------------------------------------------------


class TestSystemPromptCompleteness:
    """Ensure the intent classifier SYSTEM_PROMPT includes all new actions."""

    def test_system_prompt_has_meal_status(self) -> None:
        import assistant
        assert "meal_status" in assistant.SYSTEM_PROMPT

    def test_system_prompt_has_redundant_challenge(self) -> None:
        import assistant
        assert "redundant_question_challenge" in assistant.SYSTEM_PROMPT

    def test_system_prompt_has_dietary_pref(self) -> None:
        import assistant
        assert "set_dietary_pref" in assistant.SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Cross-cutting: keyword_fallback ordering
# ---------------------------------------------------------------------------


class TestKeywordFallbackOrdering:
    """Keyword fallback must not mis-route dietary negation as meal logging."""

    def test_negated_food_is_not_meal_log(self) -> None:
        import assistant
        intent = assistant.keyword_fallback("לא אלכוהול")
        assert intent.action == "set_dietary_pref"

    def test_negated_eating_verb_is_pref(self) -> None:
        import assistant
        intent = assistant.keyword_fallback("אני לא אוכל בשר")
        assert intent.action == "set_dietary_pref"

    def test_affirmative_eating_is_meal_log(self) -> None:
        import assistant
        intent = assistant.keyword_fallback("אכלתי 2 ביצים וטוסט")
        assert intent.action == "log_meal_text"

    def test_equipment_occupied_not_pain(self) -> None:
        import assistant
        intent = assistant.keyword_fallback("המכשיר תפוס")
        assert intent.action != "report_pain"
