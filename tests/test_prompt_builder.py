"""RE9-034/036/037: unified Prompt Builder envelope + safety contract."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from config import TZ
from db import Database
from helpers import utc_now
from noam_coach.services.next_meal import build_workout_nutrition_context
from noam_coach.services.nutrition_context import (
    build_nutrition_ai_request,
    build_nutrition_context,
)
from noam_coach.services.prompt_builder import (
    SAFETY_CONTRACT,
    build_workout_request,
)

_ENVELOPE_KEYS = {"domain", "user_request", "context", "context_quality", "safety"}


async def _db(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "pb.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1, 'T', NULL, ?)",
        (utc_now(),),
    )
    await db.execute(
        """
        INSERT INTO goal_versions(user_id, calories, protein, steps, phase, status, source, created_at)
        VALUES(1, 2100, 160, 8000, 'fat_loss_muscle_retention', 'active', 'user_approved', ?)
        """,
        (utc_now(),),
    )
    return db


def test_safety_contract_has_required_guards() -> None:
    for key in (
        "validate_against_allergies_after_generation",
        "require_output_validation",
        "avoid_medical_diagnosis_or_dosage_advice",
        "do_not_treat_planned_meals_as_consumed",
    ):
        assert SAFETY_CONTRACT.get(key) is True


@pytest.mark.asyncio
async def test_nutrition_request_uses_unified_envelope(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    ctx = await build_nutrition_context(db, 1, "menu", now=datetime.now(TZ))
    request = build_nutrition_ai_request(ctx, "Build today's menu")
    assert set(request) == _ENVELOPE_KEYS
    assert request["domain"] == "nutrition"
    assert request["safety"]["validate_against_allergies_after_generation"] is True


@pytest.mark.asyncio
async def test_workout_request_uses_same_envelope(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    wctx = await build_workout_nutrition_context(db, 1, now=datetime.now(TZ))
    request = build_workout_request(wctx, "Write a motivational line")
    assert set(request) == _ENVELOPE_KEYS
    assert request["domain"] == "workout"
    # Same safety contract instance-shape across domains.
    assert request["safety"] == dict(SAFETY_CONTRACT)
    assert "data_completeness" in request["context_quality"]
