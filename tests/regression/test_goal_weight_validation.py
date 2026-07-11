from __future__ import annotations

from noam_coach.services.goal_validation import (
    is_explicit_goal_weight_confirmation,
    validate_goal_weight,
)


def test_goal_weight_requires_confirmation_for_extreme_drop() -> None:
    result = validate_goal_weight(50, 101.8)
    assert result.needs_confirmation is True
    assert "101.8" in result.message
    assert "כן 50" in result.message


def test_goal_weight_accepts_plausible_target() -> None:
    assert validate_goal_weight(83, 101.8).needs_confirmation is False


def test_explicit_goal_weight_confirmation_phrase() -> None:
    assert is_explicit_goal_weight_confirmation("כן 50", 50) is True
    assert is_explicit_goal_weight_confirmation("50", 50) is False
