from __future__ import annotations

from pathlib import Path

import pytest

import user_model
from db import Database
from helpers import utc_now
from noam_coach.services.food_preferences import (
    DISLIKE_FACT,
    PREFERENCE_FACT,
    clean_food_item,
    preference_restrictions_from_facts,
    record_food_preference_from_slots,
)


@pytest.fixture
async def db(tmp_path: Path) -> Database:
    database = Database(str(tmp_path / "food_preferences.db"))
    await database.init()
    await database.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1, 'Test', NULL, ?)",
        (utc_now(),),
    )
    return database


@pytest.mark.asyncio
async def test_disliked_food_is_stored_separately_from_restrictions(db: Database) -> None:
    update = await record_food_preference_from_slots(
        db,
        1,
        {"kind": "preference", "polarity": "avoid", "item": "אני לא אוהב טורטייה"},
        "אני לא אוהב טורטייה",
    )

    assert update.fact_key == DISLIKE_FACT
    assert update.item == "טורטייה"
    assert await user_model.get_value(db, 1, DISLIKE_FACT) == "טורטייה"
    assert await user_model.get_value(db, 1, "diet_restrictions") is None
    assert await user_model.get_value(db, 1, "allergies") is None


@pytest.mark.asyncio
async def test_preference_update_removes_opposite_fact(db: Database) -> None:
    await record_food_preference_from_slots(
        db,
        1,
        {"kind": "preference", "polarity": "prefer", "item": "אני מעדיף אורז"},
        "אני מעדיף אורז",
    )

    update = await record_food_preference_from_slots(
        db,
        1,
        {"kind": "preference", "polarity": "avoid", "item": "אני לא אוהב אורז"},
        "אני לא אוהב אורז",
    )

    assert update.fact_key == DISLIKE_FACT
    assert update.removed_from == [PREFERENCE_FACT]
    assert await user_model.get_value(db, 1, DISLIKE_FACT) == "אורז"
    assert await user_model.get_value(db, 1, PREFERENCE_FACT) == ""


@pytest.mark.asyncio
async def test_allergy_and_diet_rule_keep_canonical_fact_targets(db: Database) -> None:
    allergy = await record_food_preference_from_slots(
        db,
        1,
        {"kind": "allergy", "polarity": "avoid", "item": "בוטנים"},
        "אני אלרגי לבוטנים",
    )
    vegetarian = await record_food_preference_from_slots(
        db,
        1,
        {"kind": "preference", "polarity": "prefer", "item": "אני צמחוני"},
        "אני צמחוני",
    )

    assert allergy.fact_key == "allergies"
    assert vegetarian.fact_key == "diet_restrictions"
    assert await user_model.get_value(db, 1, "allergies") == "בוטנים"
    assert await user_model.get_value(db, 1, "diet_restrictions") == "צמחוני"


@pytest.mark.asyncio
async def test_disliked_foods_are_exposed_as_preference_restrictions(db: Database) -> None:
    await user_model.set_fact(
        db,
        1,
        DISLIKE_FACT,
        "טורטייה",
        kind=user_model.KIND_FACT,
        source=user_model.SOURCE_USER,
        confirmed=True,
    )

    restrictions = await preference_restrictions_from_facts(db, 1)

    assert restrictions
    assert restrictions[0].restriction_type == "preference"
    assert restrictions[0].severity == "low"
    assert restrictions[0].source == f"facts:{DISLIKE_FACT}"


def test_clean_food_item_removes_common_hebrew_prefixes() -> None:
    assert clean_food_item("אני לא שותה אלכוהול") == "אלכוהול"
    assert clean_food_item("לא אוהב את טורטייה") == "טורטייה"
    assert clean_food_item("אני מעדיף אורז") == "אורז"
