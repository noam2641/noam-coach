"""Regression tests for a follow-up QA pass on the nutrition/daily-menu
pipeline, covering four gaps flagged after the corrective review:

P1a: save_chosen_meal / the dailymenu:save callback must persist the meal's
actual composition as its name, not the behavioral role label -- a meal
saved as "ארוחת בוקר" is useless to learned_foods/repetition/routine
analysis, which key off the meal name.

P1b: save_chosen_meal must write real meal_items rows (with carbs/fat, not
hardcoded to 0) from MealOption.ingredient_details, and must not fabricate a
"1 יחידה" quantity when no gram weight is known.

P2: a targeted daily-menu edit ("בלי ביצים") must substitute only the
matching ingredient(s), preserving the rest of the meal's composition and
recomputing totals from what's left -- not replace the entire meal with a
single substitute food.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from db import Database
from helpers import utc_now
from noam_coach.services.daily_menu_edit import _regenerate_structured_meals
from noam_coach.services.next_meal import MealIngredient, MealOption, save_chosen_meal


async def _db(tmp_path: Path, name: str) -> Database:
    db = Database(str(tmp_path / name))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'T',NULL,?)",
        (utc_now(),),
    )
    return db


# ---------------------------------------------------------------------------
# P1a -- meal name is the composition, not the role.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_chosen_meal_persists_composition_as_name(tmp_path: Path) -> None:
    """A MealOption whose title is the actual dish (not a generic slot label)
    must be persisted with that title as meals.name."""
    db = await _db(tmp_path, "identity.db")
    option = MealOption(
        title="יוגורט חלבון, בננה ושיבולת שועל",
        ingredients=["יוגורט חלבון 200 גרם", "בננה 1 יחידה", "שיבולת שועל 40 גרם"],
        calories=420, protein=35, rationale="test",
    )
    saved = await save_chosen_meal(db, 1, option)
    assert saved is True
    row = await db.fetch_one("SELECT name FROM meals WHERE user_id=1")
    assert row["name"] == "יוגורט חלבון, בננה ושיבולת שועל"
    assert row["name"] != "ארוחת בוקר"


# ---------------------------------------------------------------------------
# P1b -- real meal_items rows with carbs/fat, no fabricated quantities.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_chosen_meal_writes_meal_items_with_real_macros(tmp_path: Path) -> None:
    """Previously save_chosen_meal never wrote meal_items at all (so a saved
    recommendation was invisible to learned_foods_from_meals, which reads
    from meal_items) and hardcoded carbs=0/fat=0 on the meals row regardless
    of the option's real macros."""
    db = await _db(tmp_path, "macros.db")
    option = MealOption(
        title="אורז ועדשים",
        ingredients=[],
        calories=0, protein=0, rationale="test",
        ingredient_details=[
            MealIngredient(
                food_id="rice_cooked", display_name="אורז מבושל", quantity=150,
                unit="גרם", calories=195, protein_g=4, carbs_g=42, fat_g=1,
            ),
            MealIngredient(
                food_id="lentils_cooked", display_name="עדשים מבושלות", quantity=120,
                unit="גרם", calories=139, protein_g=11, carbs_g=24, fat_g=0.5,
            ),
        ],
    )
    saved = await save_chosen_meal(db, 1, option)
    assert saved is True

    meal_row = await db.fetch_one("SELECT id, carbs, fat FROM meals WHERE user_id=1")
    assert meal_row is not None
    assert float(meal_row["carbs"]) == pytest.approx(66, abs=1)  # 42 + 24
    assert float(meal_row["fat"]) == pytest.approx(1.5, abs=1)  # 1 + 0.5, rounded

    items = await db.fetch_all("SELECT name, grams, carbs, fat FROM meal_items WHERE meal_id=?", (meal_row["id"],))
    assert len(items) == 2
    names = {row["name"] for row in items}
    assert names == {"אורז מבושל", "עדשים מבושלות"}
    for row in items:
        # Real gram weights persisted, not a fabricated "1 יחידה".
        assert float(row["grams"]) in (150.0, 120.0)
        assert float(row["carbs"]) > 0


@pytest.mark.asyncio
async def test_dailymenu_save_callback_writes_meal_items_from_structured_ingredients(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: a structured daily-menu meal with real ingredients must
    produce real meal_items rows (not '1 יחידה' fabrications) when saved via
    the dailymenu:save callback."""
    import coach_bot
    from noam_coach.bot import callback_menu as callback_menu_bot
    from noam_coach.services.daily_menu_state import MenuMealRecord, remember_active_daily_menu

    db = await _db(tmp_path, "callback_items.db")
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(callback_menu_bot, "DB", db)

    meal = MenuMealRecord(
        meal_id="lunch-0", slot="lunch", time="13:00", role="ארוחת צהריים",
        calories=500, protein=40, note="עוף ואורז",
        ingredients=[
            {"name": "חזה עוף", "grams": 150, "calories": 250, "protein": 46, "carbs": 0, "fat": 5},
            {"name": "אורז מבושל", "grams": 150, "calories": 195, "protein": 4, "carbs": 42, "fat": 1},
        ],
    )
    saved_state = await remember_active_daily_menu(db, 1, text="<b>תפריט</b>", meals=[meal])
    menu_id = str(saved_state["menu_id"])

    class _FakeQuery:
        async def edit_message_text(self, *a, **kw): pass
        async def answer(self, *a, **kw): pass

    await callback_menu_bot.handle_menu_callback(_FakeQuery(), 1, f"dailymenu:save:{menu_id}:lunch-0")

    meal_row = await db.fetch_one("SELECT id, name FROM meals WHERE user_id=1")
    assert meal_row["name"] == "עוף ואורז"  # composition, not "ארוחת צהריים"
    items = await db.fetch_all("SELECT name, grams, carbs FROM meal_items WHERE meal_id=?", (meal_row["id"],))
    names = {row["name"] for row in items}
    assert names == {"חזה עוף", "אורז מבושל"}
    grams = {row["name"]: float(row["grams"]) for row in items}
    assert grams["חזה עוף"] == 150.0
    assert grams["אורז מבושל"] == 150.0


# ---------------------------------------------------------------------------
# P2 -- targeted ingredient substitution, not whole-meal replacement.
# ---------------------------------------------------------------------------


def test_regenerate_structured_meals_preserves_unaffected_ingredients() -> None:
    """A meal composed of eggs + bread + cheese + vegetables, with 'בלי
    ביצים' requested, must keep bread/cheese/vegetables -- only the egg
    ingredient is removed and replaced."""
    meals = [
        {
            "meal_id": "breakfast-0", "slot": "breakfast", "time": "08:00", "role": "ארוחת בוקר",
            "calories": 500, "protein": 35, "note": "ביצים + לחם + גבינה + ירקות",
            "ingredients": [
                {"name": "ביצים", "grams": 100, "calories": 155, "protein": 13, "carbs": 1, "fat": 11},
                {"name": "לחם מלא", "grams": 60, "calories": 150, "protein": 6, "carbs": 28, "fat": 2},
                {"name": "גבינה צהובה", "grams": 30, "calories": 100, "protein": 7, "carbs": 1, "fat": 8},
                {"name": "ירקות", "grams": 100, "calories": 25, "protein": 1, "carbs": 5, "fat": 0},
            ],
        },
    ]
    updated, changed = _regenerate_structured_meals(
        meals, avoid="ביצים", replacement_name="קוטג׳", calories=120, protein=14,
    )
    assert changed == [0]
    updated_meal = updated[0]
    names = {item["name"] for item in updated_meal["ingredients"]}
    # Bread, cheese and vegetables all survive -- only eggs are gone.
    assert "לחם מלא" in names
    assert "גבינה צהובה" in names
    assert "ירקות" in names
    assert "ביצים" not in names
    assert "קוטג׳" in names
    # Totals are recomputed from what's actually left, not overwritten with
    # the substitute's own isolated totals.
    expected_calories = 150 + 100 + 25 + 120  # bread + cheese + veg + substitute
    expected_protein = 6 + 7 + 1 + 14
    assert updated_meal["calories"] == expected_calories
    assert updated_meal["protein"] == expected_protein
    # role is untouched (Finding 11 still holds).
    assert updated_meal["role"] == "ארוחת בוקר"
    # note reflects the real remaining composition, not just the substitute.
    assert "לחם מלא" in updated_meal["note"]
    assert "קוטג׳" in updated_meal["note"]


def test_regenerate_structured_meals_falls_back_to_whole_meal_when_no_structured_ingredients() -> None:
    """A legacy meal with no structured ingredients has nothing more granular
    to target -- whole-meal replacement remains the correct fallback."""
    meals = [
        {
            "meal_id": "breakfast-0", "slot": "breakfast", "time": "08:00", "role": "ארוחת בוקר",
            "calories": 400, "protein": 30, "note": "חביתת ביצים", "ingredients": [],
        },
    ]
    updated, changed = _regenerate_structured_meals(
        meals, avoid="ביצים", replacement_name="קוטג׳", calories=250, protein=28,
    )
    assert changed == [0]
    assert updated[0]["note"] == "קוטג׳"
    assert updated[0]["calories"] == 250
    assert updated[0]["role"] == "ארוחת בוקר"


def test_regenerate_structured_meals_untouched_meal_preserved_exactly() -> None:
    """A meal that doesn't match the avoided item is preserved byte-for-byte,
    including its ingredient list."""
    lunch = {
        "meal_id": "lunch-0", "slot": "lunch", "time": "13:00", "role": "ארוחת צהריים",
        "calories": 700, "protein": 50, "note": "עוף ואורז",
        "ingredients": [{"name": "חזה עוף", "grams": 150, "calories": 250, "protein": 46}],
    }
    meals = [dict(lunch)]
    updated, changed = _regenerate_structured_meals(
        meals, avoid="ביצים", replacement_name="קוטג׳", calories=250, protein=28,
    )
    assert changed == []
    assert updated[0] == lunch
