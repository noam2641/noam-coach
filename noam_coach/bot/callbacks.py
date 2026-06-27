# ruff: noqa: F401
"""Backward-compatible callback facade.

Implementation is split by callback family; existing imports keep working.
"""

from noam_coach.bot.callback_meals import (
    handle_meal_callback,
)
from noam_coach.bot.callback_menu import (
    handle_flags_callback,
    handle_goal_callback,
    handle_menu_callback,
)
from noam_coach.bot.callback_plans import (
    handle_plan_callback,
    handle_workout_setup_callback,
)
from noam_coach.bot.callback_router import (
    _DEBOUNCE_PREFIXES,
    _LAST_CALLBACK,
    CALLBACK_DEBOUNCE_SECONDS,
    _is_duplicate_tap,
    handle_callback,
    on_error,
)
from noam_coach.bot.callback_session import (
    _handle_session_adjustment_actions,
    _handle_session_core_actions,
    _handle_session_lifecycle_actions,
    _handle_session_safety_actions,
    _handle_session_split_actions,
    handle_session_action_callback,
)

