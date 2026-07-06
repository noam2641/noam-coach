from __future__ import annotations

from datetime import datetime

from config import TZ
from noam_coach.jobs.proactive import DailyContext
from noam_coach.services.daily_coaching import (
    calculate_daily_score,
    choose_daily_mission,
    format_daily_mission,
    format_daily_score,
)
from noam_coach.services.health_jobs import (
    _daily_coach_brief_lines,
    _evening_coach_review_lines,
)


def _ctx(**overrides):
    data = {
        "user_id": 1,
        "now": datetime(2026, 7, 2, 9, 0, tzinfo=TZ),
        "local_date": "2026-07-02",
        "calories_consumed": 700.0,
        "protein_consumed": 90.0,
        "calorie_target": 2100,
        "protein_target": 190,
        "calories_remaining": 1400.0,
        "protein_remaining": 100.0,
        "hours_left": 14.0,
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


def test_daily_mission_prefers_due_workout() -> None:
    mission = choose_daily_mission(_ctx())

    assert mission.code == "workout"
    assert mission.progress_percent == 0
    assert "אימון" in "\n".join(format_daily_mission(mission))


def test_daily_mission_uses_protein_when_no_workout_due() -> None:
    mission = choose_daily_mission(_ctx(is_usual_workout_day=False))

    assert mission.code == "protein"
    assert mission.progress_percent == 47


def test_daily_score_balances_nutrition_workout_recovery() -> None:
    score = calculate_daily_score(
        _ctx(
            calories_consumed=2000,
            protein_consumed=190,
            workout_completed=True,
            sleep_quality="good",
        )
    )

    assert score.nutrition >= 95
    assert score.workout == 100
    assert score.recovery == 100
    assert score.overall >= 95
    assert "Overall" in "\n".join(format_daily_score(score))


def test_brief_and_review_include_mission_and_score_cards() -> None:
    morning = "\n".join(_daily_coach_brief_lines(_ctx()))
    evening = "\n".join(_evening_coach_review_lines(_ctx(workout_completed=True)))

    assert "המשימה של היום" in morning
    assert "Daily Score" in evening
