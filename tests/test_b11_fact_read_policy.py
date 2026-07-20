"""B11 / ARCH-15 — fact read-policy migration.

Read-policy classification (the mechanical inventory's outcome):
- CONFIRMED-ONLY (get_decision_value): body metrics driving goal/target
  computation (weight_kg, height_cm, sex, age, goal_weight_kg,
  body_fat_pct, goal_timeframe_weeks, avg_steps, primary_goal) and
  food_environment_context driving menu-style decisions — an unconfirmed
  AI/derived estimate must never silently drive them.
- SAFETY-CONSERVATIVE RAW (get_value, documented): diet_restrictions,
  allergies, known_medications, disliked/preferred foods — an unconfirmed
  restriction failing CLOSED (over-restricting) is the safe direction, so
  these deliberately keep raw reads.
- DISPLAY/DRAFT (get_value / get_display_value): onboarding question
  flows, profile screens, edit forms — designed to work with unconfirmed
  data.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import coach_bot
import user_model
from db import Database
from helpers import utc_now

USER_ID = 1

_BODY_FACTS: dict[str, Any] = {
    "weight_kg": 80,
    "height_cm": 178,
    "sex": "male",
    "age": 30,
    "goal_weight_kg": 74,
    "goal_timeframe_weeks": 16,
    "primary_goal": "fat_loss_muscle_retention",
}


@pytest.fixture
async def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Database:
    database = Database(str(tmp_path / "b11.db"))
    await database.init()
    await database.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(?, 'Test', NULL, ?)",
        (USER_ID, utc_now()),
    )
    monkeypatch.setattr(coach_bot, "DB", database)
    from config import SETTINGS

    monkeypatch.setattr(SETTINGS, "telegram_allowed_user_id", USER_ID, raising=False)
    return database


async def _seed_confirmed_body_facts(db: Database) -> None:
    for key, value in _BODY_FACTS.items():
        await user_model.set_fact(
            db, USER_ID, key, value,
            kind=user_model.KIND_FACT, source=user_model.SOURCE_USER,
            confirmed=True,
        )


# ---------------------------------------------------------------------------
# Required regression 1+2: unconfirmed does NOT drive, confirmed DOES
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirmed_facts_drive_personal_targets(db: Database) -> None:
    from noam_coach.services.goals import compute_personal_targets

    await _seed_confirmed_body_facts(db)
    computed = await compute_personal_targets(USER_ID)
    assert computed is not None
    assert computed.calories > 0


@pytest.mark.asyncio
async def test_unconfirmed_estimate_does_not_silently_drive_targets(
    db: Database,
) -> None:
    """An AI-derived, never-confirmed weight estimate must not silently
    produce personal calorie targets as if the user had confirmed it."""
    from noam_coach.services.goals import compute_personal_targets

    await _seed_confirmed_body_facts(db)
    await user_model.set_fact(
        db, USER_ID, "weight_kg", 95,
        kind=user_model.KIND_ESTIMATE, source=user_model.SOURCE_DERIVED,
        confirmed=False,
    )
    computed = await compute_personal_targets(USER_ID)
    assert computed is None  # the confirmed-only gate refused the estimate


@pytest.mark.asyncio
async def test_unconfirmed_food_environment_does_not_drive_menu_style(
    db: Database,
) -> None:
    from noam_coach.services.nutrition_context import build_nutrition_context

    await _seed_confirmed_body_facts(db)
    await db.execute(
        """
        INSERT INTO goal_versions(user_id, calories, protein, steps, phase, status, source, created_at)
        VALUES(?, 2100, 150, 8000, 'fat_loss_muscle_retention', 'active', 'test', ?)
        """,
        (USER_ID, utc_now()),
    )
    environment = {
        "source_schema": "food_environment_v1",
        "cooking_level": "none",
        "restaurant_frequency": "high",
        "delivery_or_takeaway": True,
        "needs_quick_meals": True,
        "schedule_variability": True,
        "limited_food_access": True,
    }
    await user_model.set_fact(
        db, USER_ID, "food_environment_context", environment,
        kind=user_model.KIND_ESTIMATE, source=user_model.SOURCE_DERIVED,
        confirmed=False,
    )
    context = await build_nutrition_context(db, USER_ID, "morning_menu")
    assert context.food_environment_context in (None, {}, [])

    # ...and once the user confirms it, it DOES drive.
    await user_model.set_fact(
        db, USER_ID, "food_environment_context", environment,
        kind=user_model.KIND_FACT, source=user_model.SOURCE_USER,
        confirmed=True,
    )
    context = await build_nutrition_context(db, USER_ID, "morning_menu")
    assert context.food_environment_context


# ---------------------------------------------------------------------------
# Required regression 3: justified raw consumers keep their semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unconfirmed_allergy_still_restricts_the_menu(db: Database) -> None:
    """SAFETY-CONSERVATIVE RAW: an unconfirmed allergy estimate must keep
    restricting (failing closed is the safe direction) — this consumer is a
    deliberate, documented raw read."""
    from noam_coach.services.nutrition_context import build_nutrition_context

    await _seed_confirmed_body_facts(db)
    await db.execute(
        """
        INSERT INTO goal_versions(user_id, calories, protein, steps, phase, status, source, created_at)
        VALUES(?, 2100, 150, 8000, 'fat_loss_muscle_retention', 'active', 'test', ?)
        """,
        (USER_ID, utc_now()),
    )
    await user_model.set_fact(
        db, USER_ID, "allergies", "בוטנים",
        kind=user_model.KIND_ESTIMATE, source=user_model.SOURCE_DERIVED,
        confirmed=False,
    )
    context = await build_nutrition_context(db, USER_ID, "morning_menu")
    # "בוטנים" canonicalizes to the "peanuts" restriction id.
    assert any("peanuts" in str(item) for item in context.allergies)
