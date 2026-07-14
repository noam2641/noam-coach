"""Batch D (FIX 39): workout clarification acknowledgment in the evening
summary. daily_state.workout_completed_today() intentionally stays
evidence-only (bot session/HealthKit, never self-report) -- that policy is
documented and correct. The confirmed gap this closes: the evening summary
line flatly said "no workout recorded" even when the user had explicitly
reported finishing or cancelling hours earlier, which reads as the coach
forgetting what it was just told.
"""
from __future__ import annotations

from datetime import datetime

from config import TZ
from noam_coach.jobs.proactive import DailyContext
from noam_coach.services.health_jobs import _evening_coach_review_lines


def _ctx(**overrides):
    data = {
        "user_id": 1,
        "now": datetime(2026, 7, 2, 21, 0, tzinfo=TZ),
        "local_date": "2026-07-02",
        "calories_consumed": 1800.0,
        "protein_consumed": 150.0,
        "calorie_target": 2100,
        "protein_target": 190,
        "calories_remaining": 300.0,
        "protein_remaining": 40.0,
        "hours_left": 3.0,
        "sleep_quality": "ok",
        "fasting": False,
        "medications_today": [],
        "flags": {},
        "active_constraints": [],
        "workout_active": False,
        "workout_completed": False,
        "usual_workout_time": "20:00",
        "is_usual_workout_day": True,
        "latest_health_date": None,
        "goal_computed": True,
        "profile": {},
        "goal": {"calories": 2100, "protein": 190},
    }
    data.update(overrides)
    return DailyContext(**data)


def test_workout_self_reported_property_true_for_completed_status() -> None:
    ctx = _ctx(flags={"next_meal_workout_status": "completed"})
    assert ctx.workout_self_reported is True
    assert ctx.workout_cancelled_today is False


def test_workout_self_reported_property_true_for_during_status() -> None:
    ctx = _ctx(flags={"next_meal_workout_status": "during"})
    assert ctx.workout_self_reported is True


def test_workout_cancelled_property_true_for_cancelled_status() -> None:
    ctx = _ctx(flags={"next_meal_workout_status": "cancelled"})
    assert ctx.workout_cancelled_today is True
    assert ctx.workout_self_reported is False


def test_workout_self_reported_false_when_no_flag() -> None:
    ctx = _ctx(flags={})
    assert ctx.workout_self_reported is False
    assert ctx.workout_cancelled_today is False


def test_evening_summary_acknowledges_self_report_not_flatly_contradicts_it() -> None:
    """The exact FIX 39 scenario: 'I finished the workout' -> evening summary
    must not say 'no workout was recorded' with no acknowledgment."""
    ctx = _ctx(workout_completed=False, flags={"next_meal_workout_status": "completed"})
    lines = _evening_coach_review_lines(ctx)
    joined = "\n".join(lines)
    assert "לא תועד אימון" not in joined
    assert "דיווחת שהתאמנת" in joined


def test_evening_summary_acknowledges_cancellation() -> None:
    ctx = _ctx(workout_completed=False, flags={"next_meal_workout_status": "cancelled"})
    lines = _evening_coach_review_lines(ctx)
    joined = "\n".join(lines)
    assert "לא תועד אימון" not in joined
    assert "בוטל" in joined


def test_evening_summary_strict_completion_still_wins_over_self_report_wording() -> None:
    """When bot/HealthKit evidence DOES confirm completion, use the strong
    confirmed wording, not the softer self-report caveat."""
    ctx = _ctx(workout_completed=True, flags={"next_meal_workout_status": "completed"})
    lines = _evening_coach_review_lines(ctx)
    joined = "\n".join(lines)
    assert "התאמנת" in joined
    assert "דיווחת שהתאמנת" not in joined


def test_evening_summary_unchanged_when_nothing_reported() -> None:
    """No self-report, no completion evidence -> original wording preserved."""
    ctx = _ctx(workout_completed=False, flags={})
    lines = _evening_coach_review_lines(ctx)
    joined = "\n".join(lines)
    assert "לא תועד אימון" in joined
