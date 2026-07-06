"""RE9-X1/X2: central Decision Engine + context completeness gate."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from config import TZ
from db import Database
from helpers import utc_now
from noam_coach.services.decision_engine import (
    context_completeness_gate,
    evaluate_workout_decision,
)
from noam_coach.services.next_meal import generate_next_meal_recommendation


async def _db(tmp_path: Path, *, with_goal: bool = True) -> Database:
    db = Database(str(tmp_path / "de.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1, 'T', NULL, ?)",
        (utc_now(),),
    )
    if with_goal:
        await db.execute(
            """
            INSERT INTO goal_versions(user_id, calories, protein, steps, phase, status, source, created_at)
            VALUES(1, 2100, 160, 8000, 'fat_loss_muscle_retention', 'active', 'user_approved', ?)
            """,
            (utc_now(),),
        )
    return db


@pytest.mark.asyncio
async def test_next_meal_audit_flows_through_engine(tmp_path: Path) -> None:
    """The engine produces the same internal decision audit for next-meal."""
    db = await _db(tmp_path)
    rec = await generate_next_meal_recommendation(db, 1, now=datetime.now(TZ))
    audit = rec.decision_audit
    assert set(audit) >= {"confidence", "data_completeness", "recommendation_quality", "missing_context"}
    assert 0 <= audit["confidence"] <= 100
    assert 0 <= audit["data_completeness"] <= 100


def test_completeness_gate_flags_partial_info() -> None:
    quality = {"data_completeness": 40, "missing_context": ["confirmed_goal", "sleep_time"]}
    gate = context_completeness_gate("nutrition", quality)
    assert gate.complete is False
    assert gate.based_on_partial_info is True
    assert "confirmed_goal" in gate.missing
    assert gate.tag()  # non-empty honest tag


def test_completeness_gate_passes_when_context_full() -> None:
    quality = {"data_completeness": 95, "missing_context": []}
    gate = context_completeness_gate("nutrition", quality)
    assert gate.complete is True
    assert gate.based_on_partial_info is False
    assert gate.tag() == ""


def test_workout_decision_audit_from_quality() -> None:
    quality = {"data_completeness": 80, "missing_context": ["sleep_time"], "recommendation_quality": "medium"}
    audit = evaluate_workout_decision(quality)
    assert audit.data_completeness == 80
    assert audit.recommendation_quality == "medium"
    assert audit.missing_context == ["sleep_time"]
