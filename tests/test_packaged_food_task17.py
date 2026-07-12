"""TASK-17 — packaged-food evidence: count vs weight, no false precision.

Visible count and gram weight are separate structured fields. When the gram
weight is only a visual estimate, the confirmation renders the count
("3 עוגייה") rather than an unsupported exact gram value. A user-confirmed or
package-derived weight is rendered in grams.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import coach_bot
from db import Database
from helpers import utc_now
from models import FoodItem, MealAnalysis


def test_food_item_has_separate_count_and_weight_fields() -> None:
    item = FoodItem(
        name="עוגיות שומשום אסם", grams=30, calories=130, protein=2, carbs=15, fat=8,
        confidence=0.7, quantity_count=3, quantity_unit="עוגייה", quantity_source="visual_count",
    )
    assert item.quantity_count == 3
    assert item.quantity_unit == "עוגייה"
    assert item.quantity_source == "visual_count"
    # Weight is still present but is a separate field.
    assert item.grams == 30


def test_food_item_defaults_are_backward_compatible() -> None:
    item = FoodItem(name="אורז", grams=150, calories=200, protein=4, carbs=44, fat=1, confidence=0.8)
    assert item.quantity_count is None
    assert item.quantity_unit is None
    assert item.quantity_source is None


class _Target:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def edit_message_text(self, text: str, reply_markup: Any = None, parse_mode: str | None = None) -> None:
        del reply_markup, parse_mode
        self.messages.append(text)


async def _render(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, item: FoodItem) -> str:
    db = Database(str(tmp_path / "task17.db"))
    await db.init()
    await db.execute("INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)", (utc_now(),))
    await db.execute("INSERT INTO goals(user_id,calories,protein,steps,phase,updated_at) VALUES(1,2100,165,8000,'x',?)", (utc_now(),))
    monkeypatch.setattr(coach_bot, "DB", db)
    from noam_coach.bot import meals as meals_bot

    monkeypatch.setattr(meals_bot, "DB", db, raising=False)
    from noam_coach.services.core import create_approval

    analysis = MealAnalysis(meal_name=item.name, confidence=0.7, items=[item])
    aid = await create_approval(1, "meal", {"analysis": analysis.model_dump(), "image": None, "eaten_at": utc_now()})
    target = _Target()
    await meals_bot.render_meal(target, 1, aid)
    return target.messages[-1]


@pytest.mark.asyncio
async def test_visual_count_estimate_renders_as_count_not_grams(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = FoodItem(
        name="עוגיות שומשום אסם", grams=30, calories=130, protein=2, carbs=15, fat=8,
        confidence=0.7, quantity_count=3, quantity_unit="עוגייה", quantity_source="visual_count",
    )
    text = await _render(tmp_path, monkeypatch, item)
    assert "3 עוגייה" in text
    assert "30 גרם" not in text  # unsupported gram estimate not presented as exact


@pytest.mark.asyncio
async def test_package_or_user_derived_weight_renders_grams(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = FoodItem(
        name="עוגיות שומשום אסם", grams=45, calories=195, protein=3, carbs=22, fat=12,
        confidence=0.85, quantity_count=3, quantity_unit="עוגייה", quantity_source="package_label",
    )
    text = await _render(tmp_path, monkeypatch, item)
    # A reliable weight source is rendered in grams.
    assert "45 גרם" in text
