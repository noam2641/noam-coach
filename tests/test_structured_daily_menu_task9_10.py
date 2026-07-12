"""Regression tests for structured active daily menu persistence and editing
(TASK-9/10). Covers required tests 11, 12, 13.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from db import Database
from helpers import utc_now
from noam_coach.services.daily_menu_edit import try_build_daily_menu_edit_reply
from noam_coach.services.daily_menu_state import (
    MenuMealRecord,
    get_active_daily_menu,
    is_structured_menu,
    remember_active_daily_menu,
    structured_meals,
)


async def _db(tmp_path: Path, name: str) -> Database:
    db = Database(str(tmp_path / name))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'T',NULL,?)",
        (utc_now(),),
    )
    return db


@pytest.mark.asyncio
async def test_required_11_structured_menu_survives_save_load_and_revision(tmp_path: Path) -> None:
    db = await _db(tmp_path, "structured.db")
    meals = [
        MenuMealRecord(meal_id="breakfast-0", slot="breakfast", time="08:00", role="ארוחת בוקר", calories=400, protein=30, note="ביצים וטוסט"),
        MenuMealRecord(meal_id="lunch-1", slot="lunch", time="13:00", role="ארוחת צהריים", calories=700, protein=50, note="עוף ואורז"),
    ]
    saved = await remember_active_daily_menu(
        db, 1, text="<b>תפריט</b>", strategy="balanced", meals=meals,
    )
    assert saved["revision"] == 1
    assert is_structured_menu(saved)

    loaded = await get_active_daily_menu(db, 1)
    assert loaded is not None
    assert is_structured_menu(loaded)
    assert loaded["revision"] == 1
    assert len(structured_meals(loaded)) == 2
    assert structured_meals(loaded)[0]["role"] == "ארוחת בוקר"

    # A second save increments revision and keeps the structure readable.
    saved_again = await remember_active_daily_menu(db, 1, text="<b>תפריט 2</b>", meals=meals)
    assert saved_again["revision"] == 2
    loaded_again = await get_active_daily_menu(db, 1)
    assert loaded_again["revision"] == 2
    assert is_structured_menu(loaded_again)


@pytest.mark.asyncio
async def test_required_12_legacy_text_only_menu_is_read_safely(tmp_path: Path) -> None:
    """The current production shape (text-only, no schema_version/meals) must
    not crash new readers."""
    db = await _db(tmp_path, "legacy.db")
    await remember_active_daily_menu(db, 1, text="<b>תפריט ישן</b>\nארוחת בוקר: ביצים", strategy="balanced")

    loaded = await get_active_daily_menu(db, 1)
    assert loaded is not None
    assert loaded["text"] == "<b>תפריט ישן</b>\nארוחת בוקר: ביצים"
    assert is_structured_menu(loaded) is False
    assert structured_meals(loaded) == []  # never crashes, just empty


@pytest.mark.asyncio
async def test_required_13_without_eggs_regenerates_only_egg_meals(tmp_path: Path) -> None:
    db = await _db(tmp_path, "eggs.db")
    meals = [
        MenuMealRecord(meal_id="breakfast-0", slot="breakfast", time="08:00", role="ארוחת בוקר", calories=400, protein=30, note="חביתת ביצים עם ירקות"),
        MenuMealRecord(meal_id="lunch-1", slot="lunch", time="13:00", role="ארוחת צהריים", calories=700, protein=50, note="עוף ואורז"),
    ]
    await remember_active_daily_menu(db, 1, text="<b>תפריט</b>", strategy="balanced", meals=meals)

    result = await try_build_daily_menu_edit_reply(db, 1, "בלי ביצים בבקשה")
    assert result is not None
    body, _rows = result
    assert "גרסה 2" in body

    updated = await get_active_daily_menu(db, 1)
    assert updated is not None
    assert updated["revision"] == 2
    updated_meals = structured_meals(updated)
    assert len(updated_meals) == 2
    breakfast = next(m for m in updated_meals if m["slot"] == "breakfast")
    lunch = next(m for m in updated_meals if m["slot"] == "lunch")
    # The egg-containing meal changed...
    assert "ביצ" not in breakfast["note"]
    # ...but the unaffected lunch meal is preserved exactly.
    assert lunch["note"] == "עוף ואורז"
    assert lunch["calories"] == 700
    assert lunch["protein"] == 50


@pytest.mark.asyncio
async def test_permanent_dislike_statement_persists_fact_but_temporary_does_not(tmp_path: Path) -> None:
    """TASK-10: 'אני לא אוהב X' should flow to the permanent fact store;
    'אין לי זמן לבשל היום' must stay daily-scoped only (no food item, so no
    fact write at all)."""
    import user_model

    db = await _db(tmp_path, "perm.db")
    meals = [
        MenuMealRecord(meal_id="breakfast-0", slot="breakfast", time="08:00", role="ארוחת בוקר", calories=400, protein=30, note="ביצים וטוסט"),
    ]
    await remember_active_daily_menu(db, 1, text="<b>תפריט</b>", meals=meals)

    await try_build_daily_menu_edit_reply(db, 1, "אני לא אוהב ביצים")
    disliked = await user_model.get_value(db, 1, "disliked_foods")
    assert disliked and "ביצים" in str(disliked)


@pytest.mark.asyncio
async def test_temporary_time_scoped_request_does_not_write_permanent_fact(tmp_path: Path) -> None:
    import user_model

    db = await _db(tmp_path, "temp.db")
    meals = [
        MenuMealRecord(meal_id="breakfast-0", slot="breakfast", time="08:00", role="ארוחת בוקר", calories=400, protein=30, note="ביצים וטוסט"),
    ]
    await remember_active_daily_menu(db, 1, text="<b>תפריט</b>", meals=meals)

    await try_build_daily_menu_edit_reply(db, 1, "בלי ביצים היום, אין לי זמן לבשל")
    disliked = await user_model.get_value(db, 1, "disliked_foods")
    assert not disliked or "ביצים" not in str(disliked)
