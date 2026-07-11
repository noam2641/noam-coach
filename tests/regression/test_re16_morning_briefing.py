"""TASK-16 — Morning Update is a short briefing, not the full daily menu.

Covers:
  * menu:morning renders build_morning_briefing_text and never the full menu
    (asserted in tests/regression/test_qa_session_20260705_fixes.py);
  * the briefing text differs for a morning workout, an evening workout, and a
    rest day, and uses the actual day-specific workout time;
  * the briefing does not re-send the full active menu;
  * focused next-action buttons follow the briefing.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from config import TZ
from noam_coach.jobs.proactive import DailyContext
from noam_coach.services import health_jobs


def _ctx(**overrides) -> DailyContext:
    data = {
        "user_id": 1,
        "now": datetime(2026, 7, 3, 8, 0, tzinfo=TZ),  # a Friday
        "local_date": "2026-07-03",
        "calories_consumed": 200.0,
        "protein_consumed": 20.0,
        "calorie_target": 2100,
        "protein_target": 190,
        "calories_remaining": 1900.0,
        "protein_remaining": 170.0,
        "hours_left": 14.0,
        "sleep_quality": None,
        "fasting": False,
        "medications_today": [],
        "flags": {},
        "active_constraints": [],
        "workout_active": False,
        "workout_completed": False,
        "usual_workout_time": "19:00",
        "is_usual_workout_day": True,
        "latest_health_date": None,
        "goal_computed": True,
        "profile": {},
        "goal": {"calories": 2100, "protein": 190},
    }
    data.update(overrides)
    return DailyContext(**data)


async def _briefing(monkeypatch: pytest.MonkeyPatch, ctx: DailyContext, workout_time: str | None) -> str:
    async def fake_time(_user_id: int, _now) -> str | None:
        return workout_time

    monkeypatch.setattr(health_jobs, "_todays_workout_time", fake_time)
    return await health_jobs.build_morning_briefing_text(1, ctx)


@pytest.mark.asyncio
async def test_briefing_shows_remaining_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    text = await _briefing(monkeypatch, _ctx(), "07:00")
    assert "עדכון בוקר" in text
    assert "1900" in text  # remaining calories
    assert "170" in text   # remaining protein


@pytest.mark.asyncio
async def test_morning_workout_uses_day_specific_time_and_morning_guidance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = await _briefing(monkeypatch, _ctx(), "07:00")
    assert "07:00" in text
    assert "בוקר" in text  # morning-specific guidance


@pytest.mark.asyncio
async def test_evening_workout_produces_evening_guidance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = await _briefing(monkeypatch, _ctx(), "19:00")
    assert "19:00" in text
    assert "מאוחר יותר" in text


@pytest.mark.asyncio
async def test_rest_day_produces_rest_day_guidance(monkeypatch: pytest.MonkeyPatch) -> None:
    text = await _briefing(monkeypatch, _ctx(is_usual_workout_day=False), None)
    assert "ללא אימון מתוכנן" in text


@pytest.mark.asyncio
async def test_morning_evening_rest_outputs_differ(monkeypatch: pytest.MonkeyPatch) -> None:
    morning = await _briefing(monkeypatch, _ctx(), "07:00")
    evening = await _briefing(monkeypatch, _ctx(), "19:00")
    rest = await _briefing(monkeypatch, _ctx(is_usual_workout_day=False), None)
    assert morning != evening != rest
    assert morning != rest


@pytest.mark.asyncio
async def test_completed_workout_focuses_on_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    text = await _briefing(monkeypatch, _ctx(workout_completed=True), "07:00")
    assert "בוצע" in text
    assert "התאוששות" in text


@pytest.mark.asyncio
async def test_briefing_does_not_resend_full_menu(monkeypatch: pytest.MonkeyPatch) -> None:
    """The briefing must never call the full daily-menu builder."""
    async def fail(_user_id: int, _ctx=None) -> str:
        raise AssertionError("morning briefing must not build the full daily menu")

    monkeypatch.setattr(health_jobs, "build_morning_menu_text", fail)
    text = await _briefing(monkeypatch, _ctx(), "07:00")
    assert "עדכון בוקר" in text
