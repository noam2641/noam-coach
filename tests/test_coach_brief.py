from __future__ import annotations

from datetime import datetime

from config import TZ
from noam_coach.jobs.proactive import DailyContext
from noam_coach.services.health_jobs import (
    _daily_coach_brief_lines,
    _evening_coach_review_lines,
)


def _ctx(**overrides):
    data = {
        "user_id": 1,
        "now": datetime(2026, 7, 1, 9, 0, tzinfo=TZ),
        "local_date": "2026-07-01",
        "calories_consumed": 700.0,
        "protein_consumed": 90.0,
        "calorie_target": 2100,
        "protein_target": 190,
        "calories_remaining": 1400.0,
        "protein_remaining": 100.0,
        "hours_left": 14.0,
        "sleep_quality": None,
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


def test_daily_coach_brief_has_status_and_recommendation() -> None:
    text = "\n".join(_daily_coach_brief_lines(_ctx()))

    assert "מצב היום" in text
    assert "נשארו 1400 קלוריות" in text
    assert "נשארו 100 גרם חלבון" in text
    assert "מומלץ עכשיו" in text
    assert "מקור חלבון" in text


def test_evening_coach_review_has_score_like_outcome_and_tomorrow_action() -> None:
    text = "\n".join(
        _evening_coach_review_lines(
            _ctx(
                calories_consumed=1900,
                protein_consumed=190,
                calories_remaining=200,
                protein_remaining=0,
                workout_completed=True,
            )
        )
    )

    assert "סיכום היום" in text
    assert "חלבון: 190/190" in text
    assert "גרעון של 200 קלוריות" in text
    assert "מחר מומלץ" in text
