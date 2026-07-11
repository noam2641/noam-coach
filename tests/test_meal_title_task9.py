"""TASK-9 — meal-photo title cannot hallucinate foods; readable confirmation.

The meal title is reconciled against the detected items: a title that adds
foods absent from the item list is replaced by a canonical item-derived title.
Single packaged products render simply (volume units for drinks, no duplicate
totals).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import coach_bot
from db import Database
from helpers import utc_now
from models import FoodItem, MealAnalysis, derive_meal_title


def _item(name: str, **kw) -> FoodItem:
    base = dict(grams=100, calories=100, protein=10, carbs=10, fat=2, confidence=0.8)
    base.update(kw)
    return FoodItem(name=name, **base)


def test_title_cannot_add_foods_absent_from_items() -> None:
    m = MealAnalysis(
        meal_name="פרו 40 גבינה ותפוח",
        confidence=0.8,
        items=[_item("שייק PRO 40 קרמל", grams=330, calories=193, protein=40)],
    )
    assert "גבינה" not in m.meal_name
    assert "תפוח" not in m.meal_name
    assert "PRO 40" in m.meal_name


def test_supported_multi_item_title_is_kept() -> None:
    m = MealAnalysis(
        meal_name="עוף ואורז",
        confidence=0.8,
        items=[_item("חזה עוף"), _item("אורז לבן")],
    )
    assert m.meal_name == "עוף ואורז"


def test_single_item_title_derived_from_item() -> None:
    m = MealAnalysis(
        meal_name="ארוחה מפוארת עם גבינה",
        confidence=0.8,
        items=[_item("שייק PRO 40 קרמל", grams=330, calories=193, protein=40)],
    )
    # "גבינה" is unsupported → title falls back to the item name.
    assert m.meal_name == "שייק PRO 40 קרמל"


def test_derive_meal_title_single_and_multi() -> None:
    assert derive_meal_title([_item("במבה")]) == "במבה"
    assert derive_meal_title([_item("חזה עוף"), _item("אורז")]) == "חזה עוף + אורז"
    assert derive_meal_title([]) == "ארוחה"


def test_empty_title_is_replaced() -> None:
    m = MealAnalysis(meal_name="עם", confidence=0.8, items=[_item("קוטג' 5%")])
    assert m.meal_name == "קוטג' 5%"


class _Target:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.markups: list[Any] = []

    async def edit_message_text(self, text: str, reply_markup: Any = None, parse_mode: str | None = None) -> None:
        del parse_mode
        self.messages.append(text)
        self.markups.append(reply_markup)


async def _approval(db: Database, analysis: MealAnalysis) -> str:
    from noam_coach.services.core import create_approval

    return await create_approval(1, "meal", {"analysis": analysis.model_dump(), "image": None, "eaten_at": utc_now()})


@pytest.mark.asyncio
async def test_single_drink_renders_volume_and_no_duplicate_total(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = Database(str(tmp_path / "task9.db"))
    await db.init()
    await db.execute("INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)", (utc_now(),))
    now = utc_now()
    await db.execute("INSERT INTO goals(user_id,calories,protein,steps,phase,updated_at) VALUES(1,2100,165,8000,'x',?)", (now,))
    monkeypatch.setattr(coach_bot, "DB", db)
    from noam_coach.bot import meals as meals_bot

    monkeypatch.setattr(meals_bot, "DB", db, raising=False)

    analysis = MealAnalysis(
        meal_name="שייק PRO 40 קרמל",
        confidence=0.85,
        items=[_item("שייק PRO 40 קרמל", grams=330, calories=193, protein=40, carbs=10, fat=2)],
    )
    aid = await _approval(db, analysis)
    target = _Target()
    await meals_bot.render_meal(target, 1, aid)
    text = target.messages[-1]
    assert "מ״ל" in text            # drink volume unit
    assert "330 גרם" not in text    # not grams for a drink
    assert text.count("193") == 1   # calories shown once, not duplicated
