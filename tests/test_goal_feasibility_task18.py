"""TASK-18 — validate the goal timeline against the daily calorie target.

Covers the deterministic feasibility assessment (targets.assess_goal_feasibility)
and that the goal-proposal screen surfaces the mismatch with decision options.
"""
from __future__ import annotations

import targets


def _t(*, calories: int, maintenance: int, weight: float, goal_weight: float | None,
       weeks: float | None) -> targets.Targets:
    basis = {
        "weight_kg": weight,
        "goal_type": "fat_loss_muscle_retention",
        "avg_steps": 9000,
    }
    if goal_weight is not None:
        basis["goal_weight_kg"] = goal_weight
    if weeks is not None:
        basis["goal_timeframe_weeks"] = weeks
    return targets.Targets(
        calories=calories, protein=180, steps=9000, maintenance=maintenance, basis=basis
    )


def test_infeasible_aggressive_goal_is_detected() -> None:
    # Spec example: 101.8 -> 83 kg in 3 months (~13 weeks), TDEE ~2836, intake 2130.
    f = targets.assess_goal_feasibility(
        _t(calories=2130, maintenance=2836, weight=101.8, goal_weight=83.0, weeks=13.0)
    )
    assert f.applicable is True
    assert f.feasible is False


def test_implied_daily_deficit_is_calculated() -> None:
    f = targets.assess_goal_feasibility(
        _t(calories=2130, maintenance=2836, weight=101.8, goal_weight=83.0, weeks=13.0)
    )
    assert f.implied_daily_deficit == 706  # 2836 - 2130


def test_expected_weekly_rate_is_calculated() -> None:
    f = targets.assess_goal_feasibility(
        _t(calories=2130, maintenance=2836, weight=101.8, goal_weight=83.0, weeks=13.0)
    )
    # 706 kcal/day * 7 / 7700 kcal/kg ≈ 0.64 kg/week.
    assert abs(f.actual_weekly_rate_kg - 0.64) < 0.05
    # The requested rate (18.8 kg over 13 weeks ≈ 1.45) is much higher.
    assert f.requested_weekly_rate_kg > f.actual_weekly_rate_kg


def test_projected_deadline_weight_is_calculated() -> None:
    f = targets.assess_goal_feasibility(
        _t(calories=2130, maintenance=2836, weight=101.8, goal_weight=83.0, weeks=13.0)
    )
    # Projected loss ≈ 0.64 * 13 ≈ 8.3 kg → ~93.5 kg, nowhere near 83.
    assert 92.0 <= f.projected_weight_at_deadline_kg <= 95.0


def test_mismatched_target_message_does_not_claim_deadline_is_met() -> None:
    f = targets.assess_goal_feasibility(
        _t(calories=2130, maintenance=2836, weight=101.8, goal_weight=83.0, weeks=13.0)
    )
    assert "כנראה לא יושג" in f.message
    # TASK-4: a single direct recommendation, framed as an estimate.
    assert f.recommendation
    assert "ההמלצה שלי" in f.recommendation
    assert "הערכה" in f.recommendation
    # Practical reasoning is included (muscle / adherence / sustainable pace).
    assert "שריר" in f.recommendation


def test_realistic_alternative_timeline_is_generated() -> None:
    f = targets.assess_goal_feasibility(
        _t(calories=2130, maintenance=2836, weight=101.8, goal_weight=83.0, weeks=13.0)
    )
    assert f.realistic_weeks_for_target is not None
    # ~18.8 kg / 0.64 kg per week ≈ 29 weeks, clearly longer than 13.
    assert f.realistic_weeks_for_target > 13


def test_feasible_goal_and_target_are_accepted_normally() -> None:
    # 90 -> 87 kg over 12 weeks with a gentle ~285 kcal deficit is achievable.
    f = targets.assess_goal_feasibility(
        _t(calories=2100, maintenance=2385, weight=90.0, goal_weight=87.0, weeks=12.0)
    )
    assert f.feasible is True
    assert abs(f.projected_weight_at_deadline_kg - 87.0) <= targets._FEASIBILITY_TOLERANCE_KG


def test_extreme_deficit_is_not_forced_by_the_assessment() -> None:
    # The assessment never lowers calories to satisfy the deadline — it only
    # reports the gap. The intake it evaluates stays exactly what was passed in.
    t = _t(calories=2130, maintenance=2836, weight=101.8, goal_weight=83.0, weeks=13.0)
    _f = targets.assess_goal_feasibility(t)
    assert t.calories == 2130  # unchanged


def test_no_weight_goal_is_not_applicable() -> None:
    f = targets.assess_goal_feasibility(
        _t(calories=2100, maintenance=2385, weight=90.0, goal_weight=None, weeks=None)
    )
    assert f.applicable is False
    assert f.feasible is True  # nothing to fail


def test_goal_weight_equal_to_current_is_not_applicable() -> None:
    f = targets.assess_goal_feasibility(
        _t(calories=2100, maintenance=2385, weight=90.0, goal_weight=90.0, weeks=12.0)
    )
    assert f.applicable is False
