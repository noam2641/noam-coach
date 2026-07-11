"""TASK-2 — infer + confirm typical workout duration during data validation.

The import wizard surfaces a representative workout duration inferred from the
imported workout records ("משך אימון טיפוסי: כ־55 דקות"), lets the user
confirm or correct it there, persists it as the confirmed session_minutes
planning preference, and the later single-workout-duration question is then
skipped. When no duration was inferred, the wizard step is not shown and the
manual question remains.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import coach_bot
import questions
import user_model
from db import Database
from helpers import utc_now
from noam_coach.bot import onboarding as onboarding_bot
from noam_coach.services import core as core_services
from noam_coach.services import health_jobs


async def _db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Database:
    db = Database(str(tmp_path / "task2.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'Test',NULL,?)",
        (utc_now(),),
    )
    for mod in (coach_bot, health_jobs, onboarding_bot, core_services):
        monkeypatch.setattr(mod, "DB", db, raising=False)
    return db


async def _seed_workout_pattern(db: Database, *, duration: float | None) -> None:
    value: dict[str, Any] = {
        "weekly_frequency": 3,
        "typical_hour": "18:30",
        "common_weekdays": [0, 2, 4],
    }
    if duration is not None:
        value["avg_duration_minutes"] = duration
    await user_model.set_fact(
        db, 1, "workout_pattern", value,
        kind=user_model.KIND_ESTIMATE, source=user_model.SOURCE_DERIVED, confirmed=False,
    )


@pytest.mark.asyncio
async def test_duration_substep_is_applicable_when_inferred(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = await _db(tmp_path, monkeypatch)
    await _seed_workout_pattern(db, duration=55.0)
    fact = await user_model.get_fact(db, 1, "workout_pattern")
    assert health_jobs._workout_substep_applicable(
        health_jobs.WIZARD_STEP_WORKOUT_DURATION, fact["value"]
    ) is True


@pytest.mark.asyncio
async def test_duration_substep_not_applicable_without_inference(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = await _db(tmp_path, monkeypatch)
    await _seed_workout_pattern(db, duration=None)
    fact = await user_model.get_fact(db, 1, "workout_pattern")
    assert health_jobs._workout_substep_applicable(
        health_jobs.WIZARD_STEP_WORKOUT_DURATION, fact["value"]
    ) is False


@pytest.mark.asyncio
async def test_duration_prompt_shows_inferred_minutes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = await _db(tmp_path, monkeypatch)
    await _seed_workout_pattern(db, duration=55.4)
    fact = await user_model.get_fact(db, 1, "workout_pattern")
    detected, scope, _hint = health_jobs._wizard_step_prompt(
        health_jobs.WIZARD_STEP_WORKOUT_DURATION, fact
    )
    assert "משך אימון טיפוסי" in detected
    assert "55" in detected
    assert scope == "תוכנית האימונים"


@pytest.mark.asyncio
async def test_confirming_duration_persists_session_minutes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = await _db(tmp_path, monkeypatch)
    await _seed_workout_pattern(db, duration=55.0)
    ack = await health_jobs.confirm_health_wizard_step(1, health_jobs.WIZARD_STEP_WORKOUT_DURATION)
    assert "55" in ack
    fact = await user_model.get_fact(db, 1, "session_minutes")
    assert fact is not None
    assert fact["confirmed"] is True
    assert int(fact["value"]) == 55


@pytest.mark.asyncio
async def test_correcting_duration_persists_manual_value(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = await _db(tmp_path, monkeypatch)
    await _seed_workout_pattern(db, duration=55.0)
    ok, msg = await health_jobs.apply_health_wizard_text_edit(
        1, health_jobs.WIZARD_STEP_WORKOUT_DURATION, "70"
    )
    assert ok is True
    assert "70" in msg
    fact = await user_model.get_fact(db, 1, "session_minutes")
    assert int(fact["value"]) == 70
    assert fact["confirmed"] is True


@pytest.mark.asyncio
async def test_confirmed_duration_skips_later_question(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = await _db(tmp_path, monkeypatch)
    # Simulate the wizard having confirmed session_minutes.
    await user_model.set_fact(
        db, 1, "session_minutes", 55,
        kind=user_model.KIND_FACT, source=user_model.SOURCE_USER, confirmed=True,
    )
    q = next(q for q in questions.ALL_QUESTIONS if q.fact_key == "session_minutes")
    relevant = await questions._is_relevant(db, 1, q, {})
    assert relevant is False  # confirmed → not re-asked
