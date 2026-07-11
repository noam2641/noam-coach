"""TASK-10 — duplicate detection uses item/product identity, not weak similarity.

A duplicate warning must mean "the same consumed meal is being logged twice",
not "these meals are nutritionally similar" or "they were logged close in
time". Different primary foods must not trigger a warning even with similar
macros; the same product logged twice within minutes must.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import coach_bot
from db import Database
from helpers import utc_now
from models import FoodItem, MealAnalysis
from noam_coach.bot import meals as meals_bot


def _analysis(name: str, items: list[tuple[str, float, float, float]]) -> MealAnalysis:
    food = [
        FoodItem(name=n, grams=g, calories=c, protein=p, carbs=10, fat=5, confidence=0.85)
        for (n, g, c, p) in items
    ]
    return MealAnalysis(meal_name=name, confidence=0.85, items=food)


async def _db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Database:
    db = Database(str(tmp_path / "dup.db"))
    await db.init()
    await db.execute("INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)", (utc_now(),))
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(meals_bot, "DB", db, raising=False)
    return db


async def _save_meal(db: Database, name: str, calories: float, protein: float, items: list[str]) -> int:
    now = utc_now()
    meal_id = await db.execute(
        "INSERT INTO meals(user_id, name, calories, protein, carbs, fat, confidence, eaten_at, created_at) "
        "VALUES(1,?,?,?,20,10,0.9,?,?)",
        (name, calories, protein, now, now),
    )
    for item in items:
        await db.execute(
            "INSERT INTO meal_items(meal_id, name, grams, calories, protein, carbs, fat, confidence) "
            "VALUES(?,?,100,100,10,10,5,0.9)",
            (meal_id, item),
        )
    return meal_id


@pytest.mark.asyncio
async def test_protein_drink_vs_cottage_veg_not_duplicate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = await _db(tmp_path, monkeypatch)
    await _save_meal(db, "קוטג' 5% עם ירקות", 184, 18, ["קוטג' 5%", "מלפפון", "עגבנייה"])
    new = _analysis("שייק PRO 40 קרמל", [("שייק PRO 40 קרמל", 330, 193, 40)])
    assert await meals_bot.check_duplicate_meal(1, new) is None


@pytest.mark.asyncio
async def test_protein_yogurt_vs_protein_shake_not_duplicate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = await _db(tmp_path, monkeypatch)
    await _save_meal(db, "יוגורט חלבון", 150, 20, ["יוגורט חלבון"])
    new = _analysis("שייק חלבון", [("שייק חלבון", 300, 160, 30)])
    assert await meals_bot.check_duplicate_meal(1, new) is None


@pytest.mark.asyncio
async def test_chicken_rice_vs_salmon_potato_not_duplicate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = await _db(tmp_path, monkeypatch)
    await _save_meal(db, "עוף ואורז", 600, 50, ["חזה עוף", "אורז לבן"])
    new = _analysis("סלמון ותפוח אדמה", [("סלמון", 200, 400, 40), ("תפוח אדמה", 200, 200, 6)])
    assert await meals_bot.check_duplicate_meal(1, new) is None


@pytest.mark.asyncio
async def test_similar_macros_different_primary_food_not_duplicate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = await _db(tmp_path, monkeypatch)
    # Same calories (193) but a completely different food.
    await _save_meal(db, "קוטג' 5% עם ירקות", 193, 18, ["קוטג' 5%", "מלפפון"])
    new = _analysis("שייק PRO 40 קרמל", [("שייק PRO 40 קרמל", 330, 193, 40)])
    assert await meals_bot.check_duplicate_meal(1, new) is None


@pytest.mark.asyncio
async def test_same_product_twice_within_minutes_is_duplicate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = await _db(tmp_path, monkeypatch)
    await _save_meal(db, "שייק PRO 40 קרמל", 193, 40, ["שייק PRO 40 קרמל"])
    new = _analysis("שייק PRO 40 קרמל", [("שייק PRO 40 קרמל", 330, 193, 40)])
    dup = await meals_bot.check_duplicate_meal(1, new)
    assert dup is not None


@pytest.mark.asyncio
async def test_same_structured_meal_similar_quantities_is_duplicate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = await _db(tmp_path, monkeypatch)
    await _save_meal(db, "עוף ואורז", 600, 50, ["חזה עוף", "אורז לבן"])
    new = _analysis("עוף עם אורז", [("חזה עוף", 180, 320, 52), ("אורז לבן", 200, 300, 6)])
    dup = await meals_bot.check_duplicate_meal(1, new)
    assert dup is not None
