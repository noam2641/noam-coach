"""Tests for coach_bot.py — pure functions, keyboards, formatting, and DB-light logic."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest

import coach_bot

# ---------------------------------------------------------------------------
# admin_chat_id
# ---------------------------------------------------------------------------


def test_admin_chat_id_uses_admin(monkeypatch) -> None:
    monkeypatch.setattr(coach_bot.SETTINGS, "admin_chat_id", 999)
    assert coach_bot.admin_chat_id() == 999


def test_admin_chat_id_falls_back_to_user(monkeypatch) -> None:
    monkeypatch.setattr(coach_bot.SETTINGS, "admin_chat_id", 0)
    monkeypatch.setattr(coach_bot.SETTINGS, "telegram_allowed_user_id", 42)
    assert coach_bot.admin_chat_id() == 42


# ---------------------------------------------------------------------------
# button / session_action_data / is_current_session_step / session_action_arg
# ---------------------------------------------------------------------------

_SESSION = {"id": 10, "exercise_index": 2, "set_number": 3}


def test_button_creates_inline() -> None:
    btn = coach_bot.button("text", "data")
    assert btn.text == "text"
    assert btn.callback_data == "data"


# ---------------------------------------------------------------------------
# RIR sentinel (P0: RIR must never be invented)
# ---------------------------------------------------------------------------


def test_rir_unknown_is_negative_sentinel() -> None:
    assert coach_bot.RIR_UNKNOWN < 0


def test_rir_known_distinguishes_unknown() -> None:
    assert coach_bot._rir_known(0) is True
    assert coach_bot._rir_known(2) is True
    assert coach_bot._rir_known(coach_bot.RIR_UNKNOWN) is False
    assert coach_bot._rir_known(None) is False
    assert coach_bot._rir_known("x") is False


def test_known_rirs_filters_unknown() -> None:
    rows = [
        {"rir": 2},
        {"rir": coach_bot.RIR_UNKNOWN},
        {"rir": 0},
        {"rir": None},
    ]
    assert coach_bot._known_rirs(rows) == [2, 0]


def test_session_action_data_basic() -> None:
    result = coach_bot.session_action_data("skip", _SESSION)
    assert result == "skip:10:2:3"


def test_session_action_data_with_extras() -> None:
    result = coach_bot.session_action_data("painlevel", _SESSION, 5, "high")
    assert result == "painlevel:10:2:3:5:high"


def test_is_current_session_step_valid() -> None:
    assert coach_bot.is_current_session_step(["skip", "10", "2", "3"], _SESSION)


def test_is_current_session_step_wrong_exercise() -> None:
    assert not coach_bot.is_current_session_step(["skip", "10", "1", "3"], _SESSION)


def test_is_current_session_step_wrong_set() -> None:
    assert not coach_bot.is_current_session_step(["skip", "10", "2", "2"], _SESSION)


def test_is_current_session_step_too_few_parts() -> None:
    assert not coach_bot.is_current_session_step(["skip", "10"], _SESSION)


def test_is_current_session_step_non_digit_parts() -> None:
    assert not coach_bot.is_current_session_step(["skip", "10", "abc", "3"], _SESSION)


def test_session_action_arg_valid() -> None:
    assert coach_bot.session_action_arg(["skip", "10", "2", "3", "hello"]) == "hello"


def test_session_action_arg_second_index() -> None:
    assert coach_bot.session_action_arg(["skip", "10", "2", "3", "a", "b"], index=1) == "b"


def test_session_action_arg_missing_raises() -> None:
    with pytest.raises(ValueError, match="missing"):
        coach_bot.session_action_arg(["skip", "10", "2", "3"])


# ---------------------------------------------------------------------------
# Keyboard builders (pure functions)
# ---------------------------------------------------------------------------


def test_home_keyboard_is_the_single_unified_menu() -> None:
    """RE14: there is exactly ONE menu — no secondary "עוד" screen."""
    kb = coach_bot.home_keyboard()
    flat = [btn for row in kb.inline_keyboard for btn in row]
    callbacks = {btn.callback_data for btn in flat}
    # Daily actions and settings/rare actions all live on the same screen.
    assert {
        "menu:morning", "menu:nextmeal", "menu:workout", "menu:smartplan",
        "menu:status", "menu:evening", "menu:flags", "menu:profile",
        "menu:goal", "menu:chart", "menu:weekly", "menu:health", "menu:about",
    } <= callbacks
    # No second menu type anymore.
    assert "menu:more" not in callbacks


@pytest.mark.asyncio
async def test_home_keyboard_for_user_includes_next_action_button(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    """REC-PLAN-MEAL-03-16: home_keyboard_for_user must include a button whose
    callback matches the next_best_action callback when one is available."""
    from unittest.mock import AsyncMock

    import coach_intelligence

    sentinel_callback = "planv2:profile"
    fake_action = coach_intelligence.NextAction(
        kind="complete_profile",
        title="להשלים את הפרופיל",
        reason="test",
        priority=90,
        callback=sentinel_callback,
    )

    monkeypatch.setattr(
        coach_bot,
        "_resolve_home_action",
        AsyncMock(return_value=fake_action),
    )

    kb = await coach_bot.home_keyboard_for_user(user_id=1)
    all_callbacks = {btn.callback_data for row in kb.inline_keyboard for btn in row}
    # The next-action callback must appear as a dedicated button.
    assert sentinel_callback in all_callbacks, (
        "home_keyboard_for_user must add a button for the next_best_action callback"
    )
    # The static home buttons must still be present.
    assert "menu:profile" in all_callbacks
    assert "menu:morning" in all_callbacks


@pytest.mark.asyncio
async def test_home_keyboard_for_user_falls_back_when_no_callback(
    monkeypatch: Any,
) -> None:
    """When next_best_action returns an action with no callback, fall back to static keyboard."""
    from unittest.mock import AsyncMock

    import coach_intelligence

    fake_action = coach_intelligence.NextAction(
        kind="continue_plan",
        title="להמשיך את התוכנית",
        reason="test",
        priority=40,
        callback=None,  # no callback
    )

    monkeypatch.setattr(
        coach_bot,
        "_resolve_home_action",
        AsyncMock(return_value=fake_action),
    )

    kb = await coach_bot.home_keyboard_for_user(user_id=1)
    # Without a callback, the keyboard must equal the static home keyboard.
    static_kb = coach_bot.home_keyboard()
    assert len(kb.inline_keyboard) == len(static_kb.inline_keyboard)


@pytest.mark.asyncio
async def test_home_keyboard_for_user_falls_back_on_error(
    monkeypatch: Any,
) -> None:
    """When _resolve_home_action raises, home_keyboard_for_user returns the static keyboard."""
    from unittest.mock import AsyncMock

    monkeypatch.setattr(
        coach_bot,
        "_resolve_home_action",
        AsyncMock(side_effect=RuntimeError("boom")),
    )

    kb = await coach_bot.home_keyboard_for_user(user_id=1)
    static_kb = coach_bot.home_keyboard()
    assert len(kb.inline_keyboard) == len(static_kb.inline_keyboard)


def test_more_keyboard_is_alias_of_the_single_menu() -> None:
    """RE14: more_keyboard is kept only for old messages — same unified menu."""
    kb = coach_bot.more_keyboard()
    home = coach_bot.home_keyboard()
    assert [
        [(btn.text, btn.callback_data) for btn in row] for row in kb.inline_keyboard
    ] == [
        [(btn.text, btn.callback_data) for btn in row] for row in home.inline_keyboard
    ]


def test_plans_keyboard_has_all_plans() -> None:
    kb = coach_bot.plans_keyboard()
    text = str(kb)
    assert "A" in text and "B" in text and "C" in text and "אימון גוף מלא" in text


def test_onboarding_frequency_keyboard() -> None:
    kb = coach_bot.onboarding_frequency_keyboard()
    flat = [btn for row in kb.inline_keyboard for btn in row]
    labels = {btn.text for btn in flat}
    assert {"2", "3", "4", "5", "6"} <= labels


def test_workout_overview_keyboard_has_start() -> None:
    kb = coach_bot.workout_overview_keyboard("A")
    flat = [btn for row in kb.inline_keyboard for btn in row]
    assert any("startworkout" in btn.callback_data for btn in flat)


def test_exercise_picker_keyboard_has_exercises() -> None:
    kb = coach_bot.exercise_picker_keyboard("A")
    flat = [btn for row in kb.inline_keyboard for btn in row]
    plan_a = coach_bot.PLANS["A"]
    assert len(flat) == len(plan_a["exercises"]) + 1  # + back button


def test_exercise_params_keyboard_uses_text_edit_confirmation() -> None:
    kb = coach_bot.exercise_params_keyboard("A", 0)
    flat = [btn for row in kb.inline_keyboard for btn in row]
    callbacks = {btn.callback_data for btn in flat}
    assert callbacks == {"workout:A", "editparams_menu:A"}
    assert not any(str(btn.callback_data).startswith("param:") for btn in flat)


def test_onboarding_open_keyboard() -> None:
    kb = coach_bot.onboarding_open_keyboard()
    assert len(kb.inline_keyboard) >= 1


# ---------------------------------------------------------------------------
# Split-set keyboard builders
# ---------------------------------------------------------------------------


def test_split_weight_keyboard() -> None:
    kb = coach_bot.split_weight_keyboard(_SESSION, part=1, base_weight=50.0, increment=2.5)
    flat = [btn for row in kb.inline_keyboard for btn in row]
    labels = [btn.text for btn in flat]
    assert any("50" in label for label in labels)
    assert any("ביטול" in label for label in labels)


def test_split_weight_keyboard_zero_base() -> None:
    kb = coach_bot.split_weight_keyboard(_SESSION, part=1, base_weight=0.0, increment=2.5)
    flat = [btn for row in kb.inline_keyboard for btn in row]
    # All weight candidates should be >= 0
    for btn in flat:
        if "splitw" in btn.callback_data:
            weight_str = btn.callback_data.split(":")[-1]
            assert float(weight_str) >= 0


def test_split_reps_keyboard() -> None:
    kb = coach_bot.split_reps_keyboard(_SESSION, part=1, center=8)
    flat = [btn for row in kb.inline_keyboard for btn in row]
    labels = [btn.text for btn in flat if btn.text.isdigit()]
    values = [int(x) for x in labels]
    assert 8 in values
    assert min(values) == 6
    assert max(values) == 10


def test_split_reps_keyboard_low_center() -> None:
    kb = coach_bot.split_reps_keyboard(_SESSION, part=1, center=1)
    flat = [btn for row in kb.inline_keyboard for btn in row]
    labels = [btn.text for btn in flat if btn.text.isdigit()]
    assert all(int(x) >= 1 for x in labels)


def test_split_rir_keyboard() -> None:
    kb = coach_bot.split_rir_keyboard(_SESSION)
    flat = [btn for row in kb.inline_keyboard for btn in row]
    labels = {btn.text for btn in flat}
    assert "0/כשל" in labels
    assert "3+" in labels


def test_split_summary_line() -> None:
    result = coach_bot.split_summary_line(50.0, 5, 45.0, 8, 2)
    assert "50" in result
    assert "45" in result
    assert "13" in result  # total reps
    assert "RIR 2" in result


def test_split_summary_line_zero_weight() -> None:
    result = coach_bot.split_summary_line(0.0, 10, 0.0, 10, 0)
    assert "0" in result
    assert "20" in result  # total reps
    assert "RIR 0 / כשל" in result


# ---------------------------------------------------------------------------
# _split_flow
# ---------------------------------------------------------------------------


def test_split_flow_format() -> None:
    assert coach_bot._split_flow(42) == "split:42"


# ---------------------------------------------------------------------------
# rest_job_name / rest_keyboard / rest_text
# ---------------------------------------------------------------------------


def test_rest_job_name() -> None:
    assert coach_bot.rest_job_name(1, 99) == "rest:1:99"


def test_rest_keyboard_active() -> None:
    kb = coach_bot.rest_keyboard(_SESSION, finished=False)
    flat = [btn for row in kb.inline_keyboard for btn in row]
    assert len(flat) == 3
    assert any("מוכן" in btn.text for btn in flat)
    assert any("30" in btn.text for btn in flat)
    assert any("בטל" in btn.text for btn in flat)  # immediate set undo (P1)


def test_rest_keyboard_finished() -> None:
    kb = coach_bot.rest_keyboard(_SESSION, finished=True)
    flat = [btn for row in kb.inline_keyboard for btn in row]
    assert len(flat) == 1
    assert "הבא" in flat[0].text


def test_rest_text_countdown() -> None:
    result = coach_bot.rest_text(50.0, 8, 2, remaining=90, total_seconds=120)
    assert "01:30" in result
    assert "50" in result
    assert "RIR 2" in result
    assert "█" in result


def test_rest_text_finished() -> None:
    result = coach_bot.rest_text(50.0, 8, 2, remaining=0, total_seconds=120)
    assert "הסתיימה" in result
    assert "🔔" in result


def test_rest_text_labels_rir_zero_as_failure() -> None:
    result = coach_bot.rest_text(50.0, 8, 0, remaining=90, total_seconds=120)
    assert "RIR 0 / כשל" in result


def test_rest_text_negative_remaining() -> None:
    result = coach_bot.rest_text(50.0, 8, 2, remaining=-5, total_seconds=120)
    assert "הסתיימה" in result


def test_rest_text_with_summary_line() -> None:
    result = coach_bot.rest_text(50.0, 8, 2, remaining=60, total_seconds=120, summary_line="custom line")
    assert "custom line" in result
    assert "50" not in result  # summary_line overrides default


# ---------------------------------------------------------------------------
# _is_duplicate_tap
# ---------------------------------------------------------------------------


def test_is_duplicate_tap_non_debounce_prefix() -> None:
    coach_bot._LAST_CALLBACK.clear()
    # "workout:A" doesn't start with any debounce prefix → always False
    assert not coach_bot._is_duplicate_tap(1, "workout:A")
    assert not coach_bot._is_duplicate_tap(1, "workout:A")


def test_is_duplicate_tap_first_tap() -> None:
    coach_bot._LAST_CALLBACK.clear()
    assert not coach_bot._is_duplicate_tap(1, "confirm:abc")


def test_is_duplicate_tap_rapid_second_tap() -> None:
    coach_bot._LAST_CALLBACK.clear()
    coach_bot._is_duplicate_tap(1, "confirm:abc")
    assert coach_bot._is_duplicate_tap(1, "confirm:abc")


def test_is_duplicate_tap_different_users() -> None:
    coach_bot._LAST_CALLBACK.clear()
    coach_bot._is_duplicate_tap(1, "confirm:abc")
    assert not coach_bot._is_duplicate_tap(2, "confirm:abc")


# ---------------------------------------------------------------------------
# _clock_minutes / _within_quiet_hours
# ---------------------------------------------------------------------------


def test_clock_minutes_valid() -> None:
    assert coach_bot._clock_minutes("00:00") == 0
    assert coach_bot._clock_minutes("12:30") == 750
    assert coach_bot._clock_minutes("23:59") == 1439


def test_clock_minutes_invalid() -> None:
    with pytest.raises(RuntimeError, match="Invalid"):
        coach_bot._clock_minutes("25:00")
    with pytest.raises(RuntimeError, match="Invalid"):
        coach_bot._clock_minutes("abc")


def test_within_quiet_hours_no_overlap(monkeypatch) -> None:
    monkeypatch.setattr(coach_bot.SETTINGS, "proactive_quiet_start", "22:00")
    monkeypatch.setattr(coach_bot.SETTINGS, "proactive_quiet_end", "07:00")
    # 23:00 → in quiet
    late = datetime(2026, 6, 1, 23, 0, tzinfo=timezone.utc)
    assert coach_bot._within_quiet_hours(late)
    # 12:00 → not in quiet
    noon = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    assert not coach_bot._within_quiet_hours(noon)


def test_within_quiet_hours_same_start_end(monkeypatch) -> None:
    monkeypatch.setattr(coach_bot.SETTINGS, "proactive_quiet_start", "00:00")
    monkeypatch.setattr(coach_bot.SETTINGS, "proactive_quiet_end", "00:00")
    assert not coach_bot._within_quiet_hours(datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc))


def test_within_quiet_hours_normal_range(monkeypatch) -> None:
    monkeypatch.setattr(coach_bot.SETTINGS, "proactive_quiet_start", "09:00")
    monkeypatch.setattr(coach_bot.SETTINGS, "proactive_quiet_end", "17:00")
    # 10:00 → in quiet
    assert coach_bot._within_quiet_hours(datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc))
    # 08:00 → not in quiet
    assert not coach_bot._within_quiet_hours(datetime(2026, 6, 1, 8, 0, tzinfo=timezone.utc))


# ---------------------------------------------------------------------------
# hours_left_until_sleep
# ---------------------------------------------------------------------------


def test_hours_left_until_sleep_with_bedtime() -> None:
    profile = {"sleep": {"typical_bedtime": "23:00"}}
    hours = coach_bot.hours_left_until_sleep(profile)
    assert hours >= 0.5


def test_hours_left_until_sleep_no_bedtime() -> None:
    profile = {}
    hours = coach_bot.hours_left_until_sleep(profile)
    assert hours >= 0.5


def test_hours_left_until_sleep_empty_sleep_dict() -> None:
    profile = {"sleep": {}}
    hours = coach_bot.hours_left_until_sleep(profile)
    assert hours >= 0.5


def test_hours_left_until_sleep_none_sleep() -> None:
    profile = {"sleep": None}
    hours = coach_bot.hours_left_until_sleep(profile)
    assert hours >= 0.5


# ---------------------------------------------------------------------------
# _goal_type_from_fact
# ---------------------------------------------------------------------------


def test_goal_type_from_fact_dict_with_strategy() -> None:
    assert coach_bot._goal_type_from_fact({"strategy": "muscle_gain"}) == "muscle_gain"


def test_goal_type_from_fact_dict_with_type() -> None:
    assert coach_bot._goal_type_from_fact({"type": "strength"}) == "strength"


def test_goal_type_from_fact_dict_invalid() -> None:
    assert coach_bot._goal_type_from_fact({"type": "goal_weight"}) == "fat_loss_muscle_retention"


def test_goal_type_from_fact_dict_empty() -> None:
    assert coach_bot._goal_type_from_fact({}) == "fat_loss_muscle_retention"


def test_goal_type_from_fact_string_valid() -> None:
    assert coach_bot._goal_type_from_fact("general_health") == "general_health"


def test_goal_type_from_fact_string_invalid() -> None:
    assert coach_bot._goal_type_from_fact("random") == "fat_loss_muscle_retention"


def test_goal_type_from_fact_none() -> None:
    assert coach_bot._goal_type_from_fact(None) == "fat_loss_muscle_retention"


def test_goal_type_from_fact_int() -> None:
    assert coach_bot._goal_type_from_fact(42) == "fat_loss_muscle_retention"


# ---------------------------------------------------------------------------
# medication_name_from_text
# ---------------------------------------------------------------------------


def test_medication_ritalin_flag() -> None:
    assert coach_bot.medication_name_from_text("anything", "ritalin") == "ריטלין"


def test_medication_name_in_text() -> None:
    assert coach_bot.medication_name_from_text("לקחתי אומפרזול", None) == "אומפרזול"


def test_medication_ritalin_in_text() -> None:
    assert coach_bot.medication_name_from_text("took ritalin today", None) == "ריטלין"


def test_medication_custom_known_list() -> None:
    result = coach_bot.medication_name_from_text("לקחתי אנטיביוטיקה", None, ["אנטיביוטיקה"])
    assert result == "אנטיביוטיקה"


def test_medication_generic_pill() -> None:
    assert coach_bot.medication_name_from_text("לקחתי כדור", None, []) == "תרופה"


def test_medication_generic_medicine() -> None:
    assert coach_bot.medication_name_from_text("לקחתי תרופה", None, []) == "תרופה"


def test_medication_no_match() -> None:
    assert coach_bot.medication_name_from_text("שלום", None) is None


# ---------------------------------------------------------------------------
# format_morning_menu / format_next_meals / format_evening_summary
# ---------------------------------------------------------------------------


def test_format_morning_menu() -> None:
    import recommendations as rec
    menu = rec.MorningMenu(
        headline="תפריט הבוקר",
        meals=[
            rec.MenuMeal(name="ארוחת בוקר", time_hint="08:00", calories=500, protein=30, note="חשוב"),
            rec.MenuMeal(name="צהריים", time_hint="13:00", calories=700, protein=50),
        ],
        training_advice="לפני אימון",
        closing="בהצלחה!",
    )
    result = coach_bot.format_morning_menu(menu)
    assert "תפריט הבוקר" in result
    assert "ארוחת בוקר" in result
    assert "חשוב" in result
    assert "לפני אימון" in result
    assert "בהצלחה!" in result


def test_format_morning_menu_empty() -> None:
    import recommendations as rec
    menu = rec.MorningMenu(headline="תפריט", meals=[])
    result = coach_bot.format_morning_menu(menu)
    assert "תפריט" in result


def test_format_next_meals() -> None:
    import recommendations as rec
    suggestion = rec.NextMealSuggestion(
        headline="מה לאכול",
        suggestions=[
            rec.MenuMeal(name="חטיף", time_hint="16:00", calories=200, protein=15),
        ],
        warning="שים לב!",
    )
    result = coach_bot.format_next_meals(suggestion)
    assert "מה לאכול" in result
    assert "חטיף" in result
    assert "שים לב!" in result


def test_format_next_meals_no_warning() -> None:
    import recommendations as rec
    suggestion = rec.NextMealSuggestion(
        headline="title",
        suggestions=[rec.MenuMeal(name="a", time_hint="12:00", calories=100, protein=10)],
    )
    result = coach_bot.format_next_meals(suggestion)
    assert "⚠️" not in result


def test_format_evening_summary() -> None:
    import recommendations as rec
    summary = rec.EveningSummary(
        headline="סיכום היום",
        strengths=["אכלת חלבון מספיק"],
        improvements=["חריגה בקלוריות"],
        food_flags=[rec.FoodFlag(food="שוקולד", reason="צפיפות גבוהה")],
        closing="המשך כך!",
    )
    result = coach_bot.format_evening_summary(summary)
    assert "סיכום היום" in result
    assert "אכלת חלבון מספיק" in result
    assert "חריגה בקלוריות" in result
    assert "שוקולד" in result
    assert "המשך כך!" in result


def test_format_evening_summary_minimal() -> None:
    import recommendations as rec
    summary = rec.EveningSummary(headline="test", strengths=[], improvements=[])
    result = coach_bot.format_evening_summary(summary)
    assert "test" in result
    assert "✅" not in result
    assert "🔸" not in result


# ---------------------------------------------------------------------------
# format_weekly_plan
# ---------------------------------------------------------------------------


def test_format_weekly_plan() -> None:
    plan = {
        "frequency": 3,
        "sessions": [
            {"weekday": 0, "time": "08:00", "name": "אימון A — חזה ויד קדמית"},
            {"weekday": 2, "time": "17:30", "name": "אימון B — גב ויד אחורית"},
            {"weekday": 4, "name": "אימון C — כתפיים ורגליים"},
        ],
    }
    result = coach_bot.format_weekly_plan(plan)
    assert "3 אימונים" in result
    assert "08:00" in result
    assert "שני" in result  # weekday 0
    assert "רביעי" in result  # weekday 2
    assert "שישי" in result  # weekday 4


# ---------------------------------------------------------------------------
# _CANCEL_WORDS
# ---------------------------------------------------------------------------


def test_cancel_words_set() -> None:
    assert "ביטול" in coach_bot._CANCEL_WORDS
    assert "cancel" in coach_bot._CANCEL_WORDS


# ---------------------------------------------------------------------------
# Flow state functions (DB-backed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_flow_state(tmp_path) -> None:
    db = coach_bot.Database(str(tmp_path / "coach.db"))
    await db.init()
    await db.execute("INSERT INTO users(id, updated_at) VALUES(1, ?)", (coach_bot.utc_now(),))

    import coach_bot as cb
    orig_db = cb.DB
    cb.DB = db
    try:
        await cb.set_flow_state(1, "test_flow", "step1", {"key": "value"})
        state = await cb.get_flow_state(1, "test_flow")
        assert state is not None
        assert state["step"] == "step1"
        assert state["payload"] == {"key": "value"}
    finally:
        cb.DB = orig_db


@pytest.mark.asyncio
async def test_get_flow_state_missing(tmp_path) -> None:
    db = coach_bot.Database(str(tmp_path / "coach.db"))
    await db.init()
    await db.execute("INSERT INTO users(id, updated_at) VALUES(1, ?)", (coach_bot.utc_now(),))

    import coach_bot as cb
    orig_db = cb.DB
    cb.DB = db
    try:
        state = await cb.get_flow_state(1, "nonexistent")
        assert state is None
    finally:
        cb.DB = orig_db


@pytest.mark.asyncio
async def test_clear_flow_state(tmp_path) -> None:
    db = coach_bot.Database(str(tmp_path / "coach.db"))
    await db.init()
    await db.execute("INSERT INTO users(id, updated_at) VALUES(1, ?)", (coach_bot.utc_now(),))

    import coach_bot as cb
    orig_db = cb.DB
    cb.DB = db
    try:
        await cb.set_flow_state(1, "test_flow", "step1")
        await cb.clear_flow_state(1, "test_flow")
        assert await cb.get_flow_state(1, "test_flow") is None
    finally:
        cb.DB = orig_db


# ---------------------------------------------------------------------------
# Meal-fix flow state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_meal_fix_flow(tmp_path) -> None:
    db = coach_bot.Database(str(tmp_path / "coach.db"))
    await db.init()
    await db.execute("INSERT INTO users(id, updated_at) VALUES(1, ?)", (coach_bot.utc_now(),))

    import coach_bot as cb
    orig_db = cb.DB
    cb.DB = db
    try:
        # Initially empty
        aid, rc = await cb.get_meal_fix(1)
        assert aid is None and rc == 0

        # Set
        await cb.set_meal_fix(1, "approval-123", refine_count=2)
        aid, rc = await cb.get_meal_fix(1)
        assert aid == "approval-123"
        assert rc == 2

        # Clear
        await cb.clear_meal_fix(1)
        aid, rc = await cb.get_meal_fix(1)
        assert aid is None
    finally:
        cb.DB = orig_db


# ---------------------------------------------------------------------------
# track_event (suppresses errors)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_track_event(tmp_path) -> None:
    db = coach_bot.Database(str(tmp_path / "coach.db"))
    await db.init()
    await db.execute("INSERT INTO users(id, updated_at) VALUES(1, ?)", (coach_bot.utc_now(),))

    import coach_bot as cb
    orig_db = cb.DB
    cb.DB = db
    try:
        await cb.track_event(1, "test_event", foo="bar")
        rows = await db.fetch_all("SELECT * FROM analytics_events WHERE user_id=1")
        assert len(rows) == 1
        assert rows[0]["event"] == "test_event"
        props = json.loads(rows[0]["properties"])
        assert props["foo"] == "bar"
    finally:
        cb.DB = orig_db


# ---------------------------------------------------------------------------
# get_daily_flags / set_daily_flags
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_daily_flags_roundtrip(tmp_path) -> None:
    db = coach_bot.Database(str(tmp_path / "coach.db"))
    await db.init()
    await db.execute("INSERT INTO users(id, updated_at) VALUES(1, ?)", (coach_bot.utc_now(),))

    import coach_bot as cb
    import health_service
    orig_db = cb.DB
    orig_hs_db = health_service.DB
    cb.DB = db
    health_service.DB = db
    try:
        # Initially empty
        flags = await cb.get_daily_flags(1)
        assert flags == {}

        # Set
        await cb.set_daily_flags(1, {"ritalin": True, "sleep": "good"})
        flags = await cb.get_daily_flags(1)
        assert flags["ritalin"] is True
        assert flags["sleep"] == "good"
    finally:
        cb.DB = orig_db
        health_service.DB = orig_hs_db


# ---------------------------------------------------------------------------
# set_exercise_override / get_user_plan
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exercise_override(tmp_path) -> None:
    db = coach_bot.Database(str(tmp_path / "coach.db"))
    await db.init()
    await db.execute("INSERT INTO users(id, updated_at) VALUES(1, ?)", (coach_bot.utc_now(),))

    import coach_bot as cb
    orig_db = cb.DB
    cb.DB = db
    try:
        # Default plan
        plan = await cb.get_user_plan(1, "A")
        plan["exercises"][0]["weight"]

        # Override weight
        await cb.set_exercise_override(1, "A", 0, "weight", 100.0)
        plan = await cb.get_user_plan(1, "A")
        assert plan["exercises"][0]["weight"] == 100.0
        # Other exercises unchanged
        assert plan["exercises"][1]["weight"] == coach_bot.PLANS["A"]["exercises"][1]["weight"]
    finally:
        cb.DB = orig_db


@pytest.mark.asyncio
async def test_exercise_override_sets_as_int(tmp_path) -> None:
    db = coach_bot.Database(str(tmp_path / "coach.db"))
    await db.init()
    await db.execute("INSERT INTO users(id, updated_at) VALUES(1, ?)", (coach_bot.utc_now(),))

    import coach_bot as cb
    orig_db = cb.DB
    cb.DB = db
    try:
        await cb.set_exercise_override(1, "A", 0, "sets", 5.0)
        plan = await cb.get_user_plan(1, "A")
        assert plan["exercises"][0]["sets"] == 5
        assert isinstance(plan["exercises"][0]["sets"], int)
    finally:
        cb.DB = orig_db


# ---------------------------------------------------------------------------
# DailyContext dataclass
# ---------------------------------------------------------------------------


def _make_ctx(**overrides: Any) -> coach_bot.DailyContext:
    defaults: dict[str, Any] = dict(
        user_id=1,
        now=datetime.now(timezone.utc),
        local_date="2026-06-21",
        calories_consumed=0,
        protein_consumed=0,
        calorie_target=2000,
        protein_target=150,
        calories_remaining=2000,
        protein_remaining=150,
        hours_left=12,
        sleep_quality=None,
        fasting=False,
        medications_today=[],
        flags={},
        active_constraints=[],
        workout_active=False,
        workout_completed=False,
        usual_workout_time=None,
        is_usual_workout_day=False,
        latest_health_date=None,
        goal_computed=False,
    )
    defaults.update(overrides)
    return coach_bot.DailyContext(**defaults)


def test_daily_context_fields() -> None:
    ctx = _make_ctx(
        calories_consumed=1200,
        protein_consumed=80,
        fasting=False,
        medications_today=["ריטלין"],
        flags={"ritalin": True},
    )
    assert ctx.user_id == 1
    assert ctx.fasting is False
    assert ctx.medications_today == ["ריטלין"]


# ---------------------------------------------------------------------------
# _ctx_has_workout
# ---------------------------------------------------------------------------


def test_ctx_has_workout_false() -> None:
    ctx = _make_ctx(workout_completed=False, is_usual_workout_day=False)
    assert not coach_bot._ctx_has_workout(ctx)


def test_ctx_has_workout_completed() -> None:
    ctx = _make_ctx(workout_completed=True)
    assert coach_bot._ctx_has_workout(ctx)


# ---------------------------------------------------------------------------
# local_day_str
# ---------------------------------------------------------------------------


def test_local_day_str_format() -> None:
    result = coach_bot.local_day_str()
    # Should be YYYY-MM-DD
    parts = result.split("-")
    assert len(parts) == 3
    assert len(parts[0]) == 4


# ---------------------------------------------------------------------------
# ensure_user_record (DB-light)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_user_record(tmp_path) -> None:
    db = coach_bot.Database(str(tmp_path / "coach.db"))
    await db.init()

    import coach_bot as cb
    orig_db = cb.DB
    cb.DB = db
    try:
        await cb.ensure_user_record(42, "Noam", "noam_user")
        row = await db.fetch_one("SELECT * FROM users WHERE id=42")
        assert row is not None
        assert row["first_name"] == "Noam"
        assert row["username"] == "noam_user"

        # Second call updates
        await cb.ensure_user_record(42, "Noam2")
        row = await db.fetch_one("SELECT * FROM users WHERE id=42")
        assert row["first_name"] == "Noam2"
    finally:
        cb.DB = orig_db


# ---------------------------------------------------------------------------
# create_approval / fetch_approval / decide_approval
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approval_lifecycle(tmp_path) -> None:
    db = coach_bot.Database(str(tmp_path / "coach.db"))
    await db.init()
    await db.execute("INSERT INTO users(id, updated_at) VALUES(1, ?)", (coach_bot.utc_now(),))

    import coach_bot as cb
    orig_db = cb.DB
    cb.DB = db
    try:
        # Create
        aid = await cb.create_approval(1, "meal", {"food": "test"})
        assert aid is not None

        # Fetch (pending)
        approval = await cb.fetch_approval(1, aid)
        assert approval is not None
        assert approval["kind"] == "meal"
        assert approval["data"]["food"] == "test"

        # Decide
        await cb.decide_approval(aid, "approved")
        # After deciding, fetch_approval returns None (it only returns pending)
        approval = await cb.fetch_approval(1, aid)
        assert approval is None
    finally:
        cb.DB = orig_db


@pytest.mark.asyncio
async def test_fetch_approval_missing(tmp_path) -> None:
    db = coach_bot.Database(str(tmp_path / "coach.db"))
    await db.init()

    import coach_bot as cb
    orig_db = cb.DB
    cb.DB = db
    try:
        result = await cb.fetch_approval(1, "nonexistent")
        assert result is None
    finally:
        cb.DB = orig_db


# ---------------------------------------------------------------------------
# record_medication
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_medication(tmp_path) -> None:
    db = coach_bot.Database(str(tmp_path / "coach.db"))
    await db.init()
    await db.execute("INSERT INTO users(id, updated_at) VALUES(1, ?)", (coach_bot.utc_now(),))

    import coach_bot as cb
    import health_service
    orig_db = cb.DB
    orig_hs_db = health_service.DB
    cb.DB = db
    health_service.DB = db
    try:
        event_id = await cb.record_medication(1, "ריטלין", source="user_button")
        assert event_id > 0

        rows = await db.fetch_all("SELECT * FROM medication_events WHERE user_id=1")
        assert len(rows) == 1
        assert rows[0]["name"] == "ריטלין"
        assert rows[0]["source"] == "user_button"
    finally:
        cb.DB = orig_db
        health_service.DB = orig_hs_db


# ---------------------------------------------------------------------------
# all_medication_names
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_medication_names(tmp_path) -> None:
    db = coach_bot.Database(str(tmp_path / "coach.db"))
    await db.init()
    await db.execute("INSERT INTO users(id, updated_at) VALUES(1, ?)", (coach_bot.utc_now(),))

    import coach_bot as cb
    import health_service
    orig_db = cb.DB
    orig_hs_db = health_service.DB
    cb.DB = db
    health_service.DB = db
    try:
        names = await cb.all_medication_names(1)
        # Should include defaults
        assert "ריטלין" in names
        assert "אומפרזול" in names
        # No duplicates
        assert len(names) == len(set(n.lower() for n in names))
    finally:
        cb.DB = orig_db
        health_service.DB = orig_hs_db


# ---------------------------------------------------------------------------
# known_medications
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_known_medications(tmp_path) -> None:
    db = coach_bot.Database(str(tmp_path / "coach.db"))
    await db.init()
    await db.execute("INSERT INTO users(id, updated_at) VALUES(1, ?)", (coach_bot.utc_now(),))

    import coach_bot as cb
    orig_db = cb.DB
    cb.DB = db
    try:
        meds = await cb.known_medications(1)
        assert isinstance(meds, list)
        # New user has no custom meds
        assert meds == []
    finally:
        cb.DB = orig_db


# ---------------------------------------------------------------------------
# write_audit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_audit(tmp_path) -> None:
    db = coach_bot.Database(str(tmp_path / "coach.db"))
    await db.init()
    await db.execute("INSERT INTO users(id, updated_at) VALUES(1, ?)", (coach_bot.utc_now(),))

    import coach_bot as cb
    orig_db = cb.DB
    cb.DB = db
    try:
        await cb.write_audit(1, "test_action", "test_entity", "123", detail_a="hello")
        rows = await db.fetch_all("SELECT * FROM audit WHERE user_id=1")
        assert len(rows) == 1
        assert rows[0]["action"] == "test_action"
        details = json.loads(rows[0]["details"])
        assert details["detail_a"] == "hello"
    finally:
        cb.DB = orig_db


# ---------------------------------------------------------------------------
# today_consumed / today_meal_items
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_today_consumed_empty(tmp_path) -> None:
    db = coach_bot.Database(str(tmp_path / "coach.db"))
    await db.init()
    await db.execute("INSERT INTO users(id, updated_at) VALUES(1, ?)", (coach_bot.utc_now(),))

    import coach_bot as cb
    orig_db = cb.DB
    cb.DB = db
    try:
        cal, prot = await cb.today_consumed(1)
        assert cal == 0.0
        assert prot == 0.0
    finally:
        cb.DB = orig_db


@pytest.mark.asyncio
async def test_today_meal_items_empty(tmp_path) -> None:
    db = coach_bot.Database(str(tmp_path / "coach.db"))
    await db.init()
    await db.execute("INSERT INTO users(id, updated_at) VALUES(1, ?)", (coach_bot.utc_now(),))

    import coach_bot as cb
    orig_db = cb.DB
    cb.DB = db
    try:
        items = await cb.today_meal_items(1)
        assert items == []
    finally:
        cb.DB = orig_db


# ---------------------------------------------------------------------------
# fetch_goal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_goal_empty(tmp_path) -> None:
    db = coach_bot.Database(str(tmp_path / "coach.db"))
    await db.init()
    await db.execute("INSERT INTO users(id, updated_at) VALUES(1, ?)", (coach_bot.utc_now(),))

    import coach_bot as cb
    orig_db = cb.DB
    cb.DB = db
    try:
        goal = await cb.fetch_goal(1)
        # Should return defaults
        assert "calories" in goal
        assert "protein" in goal
    finally:
        cb.DB = orig_db


# ---------------------------------------------------------------------------
# active_session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_active_session_none(tmp_path) -> None:
    db = coach_bot.Database(str(tmp_path / "coach.db"))
    await db.init()
    await db.execute("INSERT INTO users(id, updated_at) VALUES(1, ?)", (coach_bot.utc_now(),))

    import coach_bot as cb
    orig_db = cb.DB
    cb.DB = db
    try:
        result = await cb.active_session(1)
        assert result is None
    finally:
        cb.DB = orig_db


# ---------------------------------------------------------------------------
# workout_completed_today
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workout_completed_today_no_session(tmp_path) -> None:
    db = coach_bot.Database(str(tmp_path / "coach.db"))
    await db.init()
    await db.execute("INSERT INTO users(id, updated_at) VALUES(1, ?)", (coach_bot.utc_now(),))

    import coach_bot as cb
    orig_db = cb.DB
    cb.DB = db
    try:
        result = await cb.workout_completed_today(1)
        assert result is False
    finally:
        cb.DB = orig_db
