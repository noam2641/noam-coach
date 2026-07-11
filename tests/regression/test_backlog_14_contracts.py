from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def src(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def block(text: str, start: str, end: str | None = None) -> str:
    i = text.index(start)
    if end is None:
        return text[i:]
    j = text.index(end, i)
    return text[i:j]


def test_task01_task02_nutrition_completion_is_domain_scoped() -> None:
    s = src("noam_coach/bot/onboarding.py")
    order = block(s, "def _plan_completion_profile_order", "@runtime_bound(RUNTIME_NAMES)\nasync def first_missing_plan_question")
    assert 'if plan_type == "nutrition"' in order
    assert 'return ("nutrition",)' in order
    assert 'return ("workout", "safety")' in order
    assert "never drift into workout questions" in order
    assert "render_profile_snapshot" in s


def test_task03_next_meal_is_one_focused_immediate_action() -> None:
    s = src("noam_coach/services/next_meal.py")
    generator = block(s, "async def generate_next_meal_recommendation", "async def record_next_meal_served")
    assert "options[:1]" in generator or "[:1]" in generator
    actions = block(s, "def next_meal_action_rows", "def _signed_balance_line")
    assert "nextmeal:save:1" in actions
    assert "nextmeal:refresh" in actions
    assert "nextmeal:editqty:1" in actions
    assert "menu:status" in actions
    assert "nextmeal:wkt" not in actions
    assert "תכנן אפשרות" not in actions
    assert "אכלתי אפשרות 2" not in actions
    assert actions.count("rows.append") <= 4
    formatter = block(s, "def format_next_meal_recommendation", "def format_next_meal_explanation")
    assert "אפשרות 1" not in formatter
    assert "אפשרות 2" not in formatter


def test_task04_task14_meal_state_and_short_post_meal_status_are_connected() -> None:
    nutrition = src("noam_coach/services/nutrition_context.py")
    daily_state = src("noam_coach/services/daily_state.py")
    db = src("db.py")
    assert "reported_meals" in nutrition
    assert "planned_meals" in nutrition
    assert "local_now" in nutrition and "now" in nutrition
    assert "status TEXT NOT NULL DEFAULT 'consumed'" in db
    assert "_migration_meal_status" in db
    assert "COALESCE(status, 'consumed')='consumed'" in daily_state
    meals = src("noam_coach/bot/callback_meals.py")
    assert "render_post_meal_confirmation_day_status" in meals
    assert "מה לאכול עכשיו" in meals
    assert "ערוך ארוחה" in meals
    assert "סמן אימון" in meals
    assert "menu:status" in meals
    workout = src("noam_coach/bot/workout.py")
    post = block(workout, "async def render_post_meal_confirmation_day_status", "async def _exercise_pain_warning_line")
    assert "נשמר" in post
    assert "מצב היום" in post
    assert "המשך היום" in post


def test_task05_task06_daily_menu_has_one_canonical_route_and_standalone_text() -> None:
    menu = src("noam_coach/bot/callback_menu.py")
    # TASK-16: menu:morning is now the short briefing branch; the full daily
    # menu is served by menu:today / menu:daily_menu / menu:refresh_daily_menu.
    assert '("menu:today", "menu:daily_menu", "menu:refresh_daily_menu"' in menu
    assert "build_morning_menu_text" in menu
    assert "build_morning_briefing_text" in menu
    assert "menu:replace_daily_meal" in menu
    ui = src("noam_coach/bot/ui.py")
    assert "menu:daily_menu" in ui
    assert "menu:morning" in ui  # ☀️ עדכון בוקר — short briefing button
    runtime = src("noam_coach/app/runtime.py")
    health_jobs = src("noam_coach/services/health_jobs.py")
    daily_menu_state = src("noam_coach/services/daily_menu_state.py")
    assert "job_morning" in runtime
    assert "remember_daily_menu_message" in health_jobs
    assert "daily_menu_message" in daily_menu_state
    assert "שלחתי לך את תפריט היום כהודעה עצמאית" in menu


def test_task07_task08_workout_plans_keep_requested_days_and_have_catalog_support() -> None:
    planning = src("planning.py")
    assert "consistency_freq = desired" in planning
    assert "desired - 1" not in planning
    assert "Full Body מותאם" in planning
    assert "Upper / Lower מאוזן" in planning
    assert "ABC + Full Body מותאם" in planning
    assert "resolve_availability" in planning
    assert "resolved_preferred_days" in planning
    exercise_plans = src("exercise_plans.py")
    for key in ("FB1", "FB2", "FB3", "FB4", "U1", "L1", "U2", "L2"):
        assert f'"{key}"' in exercise_plans
    training = src("training_intelligence.py")
    assert "class ExerciseProfile" in training
    assert "joint_load" in training
    assert "def substitutions_for_exercise" in training
    assert "def adapt_exercises" in training
    assert "pain" in training.lower()


def test_task09_global_workout_parameter_editing_has_scope_and_confirmation() -> None:
    plans = src("noam_coach/bot/callback_plans.py")
    text_parser = src("noam_coach/bot/meal_text.py")
    ui = src("noam_coach/bot/ui.py")
    combined = plans + text_parser + ui
    assert "לכל התרגילים" in combined
    assert "לכל התוכנית" in combined
    assert "scope" in combined
    assert "confirm" in combined.lower() or "אשר" in combined
    assert "affected_count" in plans or "תרגילים הושפעו" in plans


def test_task10_task11_profile_and_healthkit_manual_precedence_are_explicit() -> None:
    onboarding = src("noam_coach/bot/onboarding.py")
    profile = block(onboarding, "async def render_profile_snapshot", "async def render_unified_plan")
    assert "goal_weight_kg" in profile
    assert "goal_timeframe_weeks" in profile
    assert "weekly_availability" in profile  # mentioned only as intentionally not rendered raw
    assert "דיווח שלך" not in profile
    assert "HealthKit נשמר כרמז" in profile
    health = src("noam_coach/services/health_jobs.py")
    assert "SOURCE_USER" in health
    assert "_has_manual_training_days" in health
    assert "_has_manual_training_frequency" in health
    assert "POLICY_INSUFFICIENT" in health
    assert "בחר או כתוב" not in health
    assert "callback_data=f\"healthconfirm:frequency:" not in health
    availability = src("noam_coach/services/availability.py")
    assert "active_training_days" in availability
    assert "preferred_training_days" in availability
    assert "detected_training_days" in availability
    assert "source=SOURCE_USER" in availability


def test_task12_telegram_errors_are_wrapped_and_stale_safe() -> None:
    router = src("noam_coach/bot/callback_router.py")
    errors = src("noam_coach/services/telegram_errors.py")
    assert "safe_answer_callback" in router
    assert "classify_telegram_error" in errors
    assert "stale" in router.lower() or "לא עדכני" in router
    assert "stale_callback" in errors
    assert "getaddrinfo" in errors or "ConnectError" in errors or "transient" in errors.lower()


def test_task13_goal_weight_validation_is_connected_to_onboarding() -> None:
    goal = src("noam_coach/services/goal_validation.py")
    onboarding = src("noam_coach/bot/onboarding.py")
    assert "validate_goal_weight" in goal
    assert "GoalWeightValidation" in goal
    assert "validate_goal_weight" in onboarding
    assert "כן" in onboarding and "goal_weight_kg" in onboarding
