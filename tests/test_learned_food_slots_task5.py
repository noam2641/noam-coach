"""TASK-5 — learned foods are ranked by meal-slot relevance, not a flat list.

Learned foods carry a per-slot distribution derived from actual meal
timestamps, expose a dominant slot + slot relevance, and surface the slot in
the AI payload so menu generation can place familiar foods in the slot they are
actually eaten in.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from config import TZ
from db import Database
from helpers import utc_now
from noam_coach.services import learned_foods as lf


def test_meal_slot_for_hour_boundaries() -> None:
    assert lf.meal_slot_for_hour(8) == "breakfast"
    assert lf.meal_slot_for_hour(13) == "lunch"
    assert lf.meal_slot_for_hour(16) == "afternoon"
    assert lf.meal_slot_for_hour(20) == "dinner"
    assert lf.meal_slot_for_hour(23) == "late"
    assert lf.meal_slot_for_hour(3) == "late"


def test_learned_food_dominant_slot_and_relevance() -> None:
    food = lf.LearnedFood(
        key="oatmeal", display_name="שיבולת שועל", count=5,
        avg_grams=60, avg_calories=220, avg_protein=8, avg_carbs=40, avg_fat=4,
        last_eaten_at="2026-07-01T08:00:00",
        slot_counts={"breakfast": 4, "afternoon": 1},
    )
    assert food.dominant_slot == "breakfast"
    assert food.slot_relevance("breakfast") == pytest.approx(0.8)
    assert food.slot_relevance("dinner") == 0.0


def test_ai_payload_exposes_meal_slot() -> None:
    food = lf.LearnedFood(
        key="chicken", display_name="חזה עוף", count=3,
        avg_grams=180, avg_calories=300, avg_protein=56, avg_carbs=0, avg_fat=6,
        last_eaten_at="2026-07-01T13:00:00",
        slot_counts={"lunch": 3},
    )
    payload = food.ai_payload()
    assert payload["usual_meal_slot"] == "lunch"
    assert payload["meal_slot_counts"] == {"lunch": 3}


async def _db(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "slots.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'T',NULL,?)",
        (utc_now(),),
    )
    return db


async def _log_item(db: Database, name: str, hour: int, *, days_ago: int = 1) -> None:
    when = (datetime.now(TZ) - timedelta(days=days_ago)).replace(hour=hour, minute=0, second=0, microsecond=0)
    eaten = when.astimezone().isoformat()
    meal_id = await db.execute(
        "INSERT INTO meals(user_id, name, calories, protein, carbs, fat, confidence, eaten_at, created_at) "
        "VALUES(1,?,300,30,20,10,0.9,?,?)",
        (name, eaten, utc_now()),
    )
    await db.execute(
        "INSERT INTO meal_items(meal_id, name, grams, calories, protein, carbs, fat, confidence) "
        "VALUES(?,?,60,220,8,40,4,0.9)",
        (meal_id, name),
    )


@pytest.mark.asyncio
async def test_learned_foods_track_meal_slots_from_timestamps(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    # Same food eaten mostly at breakfast.
    for d in range(1, 4):
        await _log_item(db, "שיבולת שועל", 8, days_ago=d)
    await _log_item(db, "שיבולת שועל", 16, days_ago=5)

    foods = await lf.learned_foods_from_meals(db, 1, min_count=2)
    oat = next(f for f in foods if "שיבולת" in f.display_name)
    assert oat.dominant_slot == "breakfast"
    assert oat.slot_counts.get("breakfast", 0) >= 3
