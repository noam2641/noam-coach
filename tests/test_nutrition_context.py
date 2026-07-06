from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

import coach_bot
import recommendations
import user_model
from config import TZ
from db import Database
from helpers import utc_now
from models import FoodItem, MealAnalysis
from noam_coach.services import profile as profile_service
from noam_coach.services.nutrition_context import (
    NOT_CAPTURED,
    build_nutrition_ai_request,
    build_nutrition_context,
)
from noam_coach.services.weekdays import local_weekday


async def _user(db: Database) -> None:
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1, 'Noam', NULL, ?)",
        (utc_now(),),
    )


@pytest.mark.asyncio
async def test_nutrition_context_counts_reported_not_planned_meals(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "coach.db"))
    await db.init()
    await _user(db)
    fixed_now = datetime(2026, 6, 28, 10, 0, tzinfo=TZ)
    now = fixed_now.isoformat()
    today_index = local_weekday(fixed_now)
    await db.execute(
        """
        INSERT INTO goal_versions(
            user_id, calories, protein, steps, phase, status, source, explanation,
            created_at, decided_at
        )
        VALUES(1, 2200, 160, 8000, 'fat_loss_muscle_retention', 'active',
               'computed', '', ?, ?)
        """,
        (now, now),
    )
    nutrition_plan_id = await db.execute(
        """
        INSERT INTO plan_versions(
            user_id, plan_type, title, strategy, fit_score, status, payload,
            rationale, tradeoffs, assumptions, based_on, created_at
        )
        VALUES(1, 'nutrition', 'Weekly nutrition', 'structured', 0.88, 'active', ?, '[]', '[]', '[]', '{}', ?)
        """,
        (
            json.dumps(
                {
                    "days": [
                        {
                            "weekday": today_index,
                            "meals": [
                                {"name": "breakfast", "calories": 500},
                                {"name": "lunch", "calories": 800},
                                {"name": "dinner", "calories": 700},
                            ],
                        }
                    ]
                }
            ),
            now,
        ),
    )
    await db.execute(
        "INSERT INTO active_plans(user_id, plan_type, plan_id, updated_at) VALUES(1, 'nutrition', ?, ?)",
        (nutrition_plan_id, now),
    )
    await db.execute(
        """
        INSERT INTO meals(
            user_id, name, calories, protein, carbs, fat, confidence, eaten_at, created_at
        )
        VALUES(1, 'reported lunch', 700, 45, 70, 20, 0.9, ?, ?)
        """,
        (now, now),
    )
    await user_model.set_fact(
        db,
        1,
        "allergies",
        "cashews",
        source=user_model.SOURCE_USER,
        confirmed=True,
    )

    context = await build_nutrition_context(db, 1, "morning_menu", now=fixed_now)

    assert context.consumed_calories == 700
    assert context.remaining_calories == 1500
    assert len(context.reported_meals) == 1
    assert len(context.planned_meals) == 3
    assert "tree_nuts" in context.allergies

    request = build_nutrition_ai_request(context, "menu")
    assert request["safety"]["do_not_treat_planned_meals_as_consumed"] is True
    assert request["safety"]["require_output_validation"] is True
    assert request["context_quality"]["data_completeness"] >= 0
    assert request["context_quality"]["recommendation_quality"] in {"high", "medium", "low"}
    assert request["context"]["consumed_calories"] == 700


@pytest.mark.asyncio
async def test_uncaptured_fields_are_sent_to_ai_as_not_captured(tmp_path: Path) -> None:
    """M-NEW-1: fields with no writer must be sent as 'not_captured', not as an
    empty list/None that the model would read as real (e.g. 'no ingredients')."""
    db = Database(str(tmp_path / "uncaptured.db"))
    await db.init()
    await _user(db)
    now = utc_now()
    await db.execute(
        """
        INSERT INTO goal_versions(
            user_id, calories, protein, steps, phase, status, source, explanation,
            created_at, decided_at
        )
        VALUES(1, 2200, 160, 8000, 'fat_loss_muscle_retention', 'active',
               'computed', '', ?, ?)
        """,
        (now, now),
    )

    context = await build_nutrition_context(db, 1, "next_meal")
    payload = context.to_ai_payload()

    for field_name in (
        "available_prep_minutes",
        "eating_location",
        "available_equipment",
        "available_ingredients",
        "recently_rejected_meals",
    ):
        assert payload[field_name] == NOT_CAPTURED, field_name
    # The honest sentinel must not leak the internal helper field.
    assert "uncaptured_fields" not in payload


class _ParsedResponse:
    def __init__(self, parsed: object) -> None:
        self.output_parsed = parsed


class _FakeResponses:
    def __init__(self) -> None:
        self.inputs: list[dict[str, object]] = []

    async def parse(self, **kwargs: object) -> _ParsedResponse:
        self.inputs.append(kwargs)
        return _ParsedResponse(
            recommendations.MorningMenu(
                headline="ok",
                meals=[
                    recommendations.MenuMeal(
                        name="meal",
                        time_hint="now",
                        calories=300,
                        protein=25,
                    )
                ],
            )
        )


class _FakeClient:
    def __init__(self) -> None:
        self.responses = _FakeResponses()


@pytest.mark.asyncio
async def test_morning_menu_passes_structured_nutrition_context_to_ai() -> None:
    client = _FakeClient()

    await recommendations.morning_menu(
        client,
        "test-model",
        {"eating": {"first_meal_time": "08:00"}},
        {"calories": 2200, "protein": 160},
        False,
        {"fasting": False},
        {"remaining_calories": 1500, "allergies": ["tree_nuts"]},
    )

    messages = client.responses.inputs[0]["input"]
    assert any(
        "Structured nutrition context" in message["content"]
        and "tree_nuts" in message["content"]
        for message in messages
    )


class _MealResponse:
    def __init__(self, parsed: MealAnalysis) -> None:
        self.output_parsed = parsed


class _MealResponses:
    def __init__(self) -> None:
        self.inputs: list[dict[str, object]] = []

    async def parse(self, **kwargs: object) -> _MealResponse:
        self.inputs.append(kwargs)
        return _MealResponse(
            MealAnalysis(
                meal_name="meal",
                items=[
                    FoodItem(
                        name="rice",
                        grams=100,
                        calories=130,
                        protein=3,
                        carbs=28,
                        fat=1,
                        confidence=0.9,
                    )
                ],
                confidence=0.9,
            )
        )


class _MealClient:
    def __init__(self) -> None:
        self.responses = _MealResponses()


@pytest.mark.asyncio
async def test_meal_reanalysis_passes_nutrition_context_to_ai(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ) -> None:
    client = _MealClient()
    monkeypatch.setattr(coach_bot, "OPENAI_CLIENT", client)
    monkeypatch.setattr(profile_service, "OPENAI_CLIENT", client)
    image_path = tmp_path / "meal.jpg"
    image_path.write_bytes(b"fake-image")

    await profile_service.reanalyze_meal_with_text_and_image(
        str(image_path),
        "rice",
        nutrition_context={"fasting_status": True, "allergies": ["tree_nuts"]},
    )

    messages = client.responses.inputs[0]["input"]
    assert any(
        isinstance(message.get("content"), str)
        and "Structured nutrition context" in message["content"]
        and "tree_nuts" in message["content"]
        for message in messages
    )
