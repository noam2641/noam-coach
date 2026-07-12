"""End-to-end regression tests for the morning-menu generate/validate/repair
pipeline (TASK-7/8/9/12)."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

import event_log
import recommendations
import user_model
from config import TZ
from db import Database
from helpers import utc_now
from noam_coach.services.morning_menu_pipeline import build_personalized_morning_menu


async def _db(tmp_path: Path, name: str) -> Database:
    db = Database(str(tmp_path / name))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'T',NULL,?)",
        (utc_now(),),
    )
    now = utc_now()
    await db.execute(
        """
        INSERT INTO goal_versions(
            user_id, calories, protein, steps, phase, status, source, explanation,
            created_at, decided_at
        )
        VALUES(1, 2200, 160, 8000, 'fat_loss_muscle_retention', 'active', 'computed', '', ?, ?)
        """,
        (now, now),
    )
    return db


class _ParsedResponse:
    def __init__(self, parsed: object) -> None:
        self.output_parsed = parsed


class _FakeResponses:
    def __init__(self, menu: recommendations.MorningMenu) -> None:
        self._menu = menu
        self.calls = 0

    async def parse(self, **kwargs: object) -> _ParsedResponse:
        self.calls += 1
        return _ParsedResponse(self._menu)


class _FakeClient:
    def __init__(self, menu: recommendations.MorningMenu) -> None:
        self.responses = _FakeResponses(menu)


def _menu(*meals: recommendations.MenuMeal) -> recommendations.MorningMenu:
    return recommendations.MorningMenu(headline="תפריט", meals=list(meals))


def _meal(name: str, calories: float, protein: float, time_hint: str, note: str = "") -> recommendations.MenuMeal:
    return recommendations.MenuMeal(name=name, time_hint=time_hint, calories=calories, protein=protein, note=note)


@pytest.mark.asyncio
async def test_clean_ai_menu_is_used_as_is(tmp_path: Path) -> None:
    db = await _db(tmp_path, "clean.db")
    ai_menu = _menu(
        _meal("ארוחת בוקר", 650, 50, "08:00", note="ביצים וירקות"),
        _meal("ארוחת צהריים", 800, 60, "13:00", note="עוף ואורז"),
        _meal("ארוחת ערב", 750, 50, "19:00", note="דג וסלט"),
    )
    client = _FakeClient(ai_menu)

    result = await build_personalized_morning_menu(
        db, 1,
        profile={"eating": {"first_meal_time": "08:00"}},
        goal={"calories": 2200, "protein": 160, "phase": "fat_loss"},
        today_has_workout=False,
        daily_flags={},
        openai_client=client,
        openai_model="test-model",
        now=datetime(2026, 7, 12, 7, 0, tzinfo=TZ),
    )

    assert result.used_fallback is False
    assert result.repaired is False
    assert result.validation.ok is True
    assert [m.name for m in result.menu.meals] == ["ארוחת בוקר", "ארוחת צהריים", "ארוחת ערב"]


@pytest.mark.asyncio
async def test_disliked_food_in_ai_menu_triggers_repair_and_is_removed(tmp_path: Path) -> None:
    db = await _db(tmp_path, "repair.db")
    await user_model.set_fact(db, 1, "disliked_foods", "טורטייה", source=user_model.SOURCE_USER, confirmed=True)

    ai_menu = _menu(
        _meal("ארוחת בוקר", 400, 30, "08:00", note="טורטיית חלבון עם ביצה"),
        _meal("ארוחת צהריים", 800, 60, "13:00", note="עוף ואורז"),
        _meal("ארוחת ערב", 750, 50, "19:00", note="דג וסלט"),
    )
    client = _FakeClient(ai_menu)

    result = await build_personalized_morning_menu(
        db, 1,
        profile={"eating": {"first_meal_time": "08:00"}},
        goal={"calories": 2200, "protein": 160, "phase": "fat_loss"},
        today_has_workout=False,
        daily_flags={},
        openai_client=client,
        openai_model="test-model",
        now=datetime(2026, 7, 12, 7, 0, tzinfo=TZ),
    )

    # The final menu must never contain the disliked food, whether via repair
    # or deterministic fallback.
    for meal in result.menu.meals:
        assert "טורטי" not in (meal.note or "")
        assert "טורטי" not in meal.name
    assert result.validation.ok is True

    events = await event_log.list_events(db, 1)
    event_names = {e.event for e in events}
    assert "menu_validation_failed" in event_names
    assert "disliked_food_rejected" in event_names
    assert "menu_repair_requested" in event_names
    assert ("menu_repair_succeeded" in event_names) or (
        "menu_repair_failed" in event_names and "deterministic_menu_fallback_used" in event_names
    )


@pytest.mark.asyncio
async def test_allergy_violation_never_reaches_final_menu(tmp_path: Path) -> None:
    db = await _db(tmp_path, "allergy_pipeline.db")
    await user_model.set_fact(db, 1, "allergies", "בוטנים", source=user_model.SOURCE_USER, confirmed=True)

    ai_menu = _menu(
        _meal("ארוחת בוקר", 400, 30, "08:00", note="חמאת בוטנים על טוסט"),
        _meal("ארוחת צהריים", 800, 60, "13:00", note="עוף ואורז"),
        _meal("ארוחת ערב", 750, 50, "19:00", note="דג וסלט"),
    )
    client = _FakeClient(ai_menu)

    result = await build_personalized_morning_menu(
        db, 1,
        profile={"eating": {"first_meal_time": "08:00"}},
        goal={"calories": 2200, "protein": 160, "phase": "fat_loss"},
        today_has_workout=False,
        daily_flags={},
        openai_client=client,
        openai_model="test-model",
        now=datetime(2026, 7, 12, 7, 0, tzinfo=TZ),
    )

    for meal in result.menu.meals:
        assert "בוטנים" not in (meal.note or "")
    assert result.validation.ok is True


@pytest.mark.asyncio
async def test_pipeline_persists_structured_meal_records(tmp_path: Path) -> None:
    db = await _db(tmp_path, "persist.db")
    ai_menu = _menu(
        _meal("ארוחת בוקר", 650, 50, "08:00", note="ביצים וירקות"),
        _meal("ארוחת צהריים", 800, 60, "13:00", note="עוף ואורז"),
        _meal("ארוחת ערב", 750, 50, "19:00", note="דג וסלט"),
    )
    client = _FakeClient(ai_menu)

    result = await build_personalized_morning_menu(
        db, 1,
        profile={"eating": {"first_meal_time": "08:00"}},
        goal={"calories": 2200, "protein": 160, "phase": "fat_loss"},
        today_has_workout=False,
        daily_flags={},
        openai_client=client,
        openai_model="test-model",
        now=datetime(2026, 7, 12, 7, 0, tzinfo=TZ),
    )
    assert len(result.meal_records) == 3
    assert result.meal_records[0].role == "ארוחת בוקר"
    assert result.used_fallback is False
