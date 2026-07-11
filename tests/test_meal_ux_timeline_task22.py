"""TASK-22 — simplified meal-analysis UX + activity-aware remaining-day timeline.

Covers:
  * meal-analysis items use bullets, not "1." / "2." numbering;
  * the dedicated "correct by text" button is gone (free text works directly);
  * the post-save "המשך היום" section is a chronological timeline with
    calorie/protein per remaining meal, recomputed from the current remaining
    budget, and only shows a bedtime when the sleep time is valid.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import coach_bot
from models import FoodItem, MealAnalysis
from noam_coach.bot import meals as meals_bot


class FakeTarget:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.markups: list = []

    async def edit_message_text(self, text: str, reply_markup=None, parse_mode=None) -> None:
        del parse_mode
        self.messages.append(text)
        self.markups.append(reply_markup)


async def _db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> coach_bot.Database:
    db = coach_bot.Database(str(tmp_path / "task22.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (coach_bot.utc_now(),),
    )
    monkeypatch.setattr(coach_bot, "DB", db)
    from noam_coach.bot import meals as meals_mod

    monkeypatch.setattr(meals_mod, "DB", db, raising=False)
    return db


async def _approve_goal(db: coach_bot.Database, calories: int = 2100, protein: int = 190) -> None:
    now = coach_bot.utc_now()
    await db.execute(
        "INSERT INTO goal_versions(user_id, calories, protein, steps, phase, status, source, "
        "explanation, created_at) VALUES(1, ?, ?, 8000, 'fat_loss_muscle_retention', 'active', "
        "'manual', 'test goal', ?)",
        (calories, protein, now),
    )
    await db.execute(
        "INSERT INTO goals(user_id, calories, protein, steps, phase, updated_at) "
        "VALUES(1, ?, ?, 8000, 'fat_loss_muscle_retention', ?)",
        (calories, protein, now),
    )


def _analysis() -> MealAnalysis:
    return MealAnalysis(
        meal_name="צלחת מעורבת",
        confidence=0.85,
        items=[
            FoodItem(name="חזה עוף", grams=180, calories=300, protein=56, carbs=0, fat=6, confidence=0.85),
            FoodItem(name="אורז לבן מבושל", grams=200, calories=260, protein=5, carbs=57, fat=0.5, confidence=0.85),
        ],
    )


async def _make_meal_approval(db: coach_bot.Database) -> str:
    from noam_coach.services.core import create_approval

    return await create_approval(
        1, "meal", {"analysis": _analysis().model_dump(), "image": None, "eaten_at": coach_bot.utc_now()}
    )


@pytest.mark.asyncio
async def test_meal_items_use_bullets_not_numbering(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = await _db(tmp_path, monkeypatch)
    await _approve_goal(db)
    approval_id = await _make_meal_approval(db)

    target = FakeTarget()
    await meals_bot.render_meal(target, 1, approval_id)
    text = target.messages[-1]
    assert "• חזה עוף" in text
    # No database-style ordered numbering on meal components.
    assert "1. חזה עוף" not in text
    assert "• 1. " not in text


@pytest.mark.asyncio
async def test_no_correct_by_text_button(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = await _db(tmp_path, monkeypatch)
    await _approve_goal(db)
    approval_id = await _make_meal_approval(db)

    target = FakeTarget()
    await meals_bot.render_meal(target, 1, approval_id)
    labels = [btn.text for row in target.markups[-1].inline_keyboard for btn in row]
    assert not any("תקן במלל" in label for label in labels)
    assert not any("תיאור נוסף" in label for label in labels)
    # Save / edit-quantities / reject remain.
    assert any("שמור" in label for label in labels)


@pytest.mark.asyncio
async def test_post_save_timeline_shows_calories_and_protein_per_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _db(tmp_path, monkeypatch)
    await _approve_goal(db)
    # One meal already logged so there is a remaining budget to allocate.
    await db.execute(
        "INSERT INTO meals(user_id, name, calories, protein, carbs, fat, confidence, eaten_at, created_at) "
        "VALUES(1,'ארוחה',400,40,20,10,0.9,?,?)",
        (coach_bot.utc_now(), coach_bot.utc_now()),
    )
    text = await coach_bot.render_post_meal_confirmation_day_status(1, {"calories": 400, "protein": 40})
    assert "המשך היום:" in text
    # Timeline entries carry per-slot calorie/protein allocations.
    assert "קל׳" in text.split("המשך היום:")[1]
    assert "חלבון" in text.split("המשך היום:")[1]


@pytest.mark.asyncio
async def test_post_save_timeline_omits_unconfirmed_bedtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no valid sleep schedule stored, the timeline must not invent a
    concrete bedtime and present it as fact."""
    db = await _db(tmp_path, monkeypatch)
    await _approve_goal(db)
    text = await coach_bot.render_post_meal_confirmation_day_status(1, {"calories": 400, "protein": 40})
    assert "😴" not in text
