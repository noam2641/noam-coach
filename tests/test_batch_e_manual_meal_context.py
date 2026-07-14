"""Batch E (FIX 46, partial): manual text meal entry gains the same
planned-meal/day context photo analysis already receives, so a reference
like "אכלתי מה שתכננו" can resolve against an actual planned meal.

Root cause (verified before this fix): analyze_meal_text() received only
the description, narrow allergy/diet facts, and learned foods -- no
NutritionContext, no planned_meals. analyze_meal_image() always received
the full structured nutrition_context payload. This is a partial fix: the
context is now optionally passed through, not yet a unified
MealInterpretationContext shared by both paths.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import coach_bot
from db import Database
from helpers import utc_now
from models import MealAnalysis
from noam_coach.services.profile import analyze_meal_text


class _ParsedResponse:
    def __init__(self, parsed: object) -> None:
        self.output_parsed = parsed


class _CapturingResponses:
    def __init__(self, result: MealAnalysis) -> None:
        self._result = result
        self.calls: list[dict[str, object]] = []

    async def parse(self, **kwargs: object) -> _ParsedResponse:
        self.calls.append(kwargs)
        return _ParsedResponse(self._result)


class _CapturingClient:
    def __init__(self, result: MealAnalysis) -> None:
        self.responses = _CapturingResponses(result)


def _fake_analysis() -> MealAnalysis:
    return MealAnalysis(
        meal_name="עוף ואורז",
        items=[],
        confidence=0.8,
    )


async def _make_db(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "batch_e_manual_context.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (utc_now(),),
    )
    return db


@pytest.mark.asyncio
async def test_analyze_meal_text_without_context_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Existing callers that don't pass nutrition_context see no prompt
    change -- purely additive."""
    db = await _make_db(tmp_path)
    coach_bot.DB.path = db.path
    client = _CapturingClient(_fake_analysis())
    monkeypatch.setattr(coach_bot, "OPENAI_CLIENT", client, raising=False)

    await analyze_meal_text("אכלתי עוף ואורז", user_id=1)

    call = client.responses.calls[0]
    messages = call.get("input") or []
    joined = " ".join(str(m.get("content", "")) for m in messages)  # type: ignore[union-attr]
    assert "planned_meals" not in joined


@pytest.mark.asyncio
async def test_analyze_meal_text_with_context_includes_planned_meals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact FIX 46 scenario: when a nutrition_context IS supplied
    (the manual-entry call site now builds one), planned_meals data must
    actually reach the AI prompt, not be silently dropped."""
    db = await _make_db(tmp_path)
    coach_bot.DB.path = db.path
    client = _CapturingClient(_fake_analysis())
    monkeypatch.setattr(coach_bot, "OPENAI_CLIENT", client, raising=False)

    context = {
        "planned_meals": [{"name": "עוף ואורז", "calories": 600, "protein": 50}],
    }
    await analyze_meal_text("אכלתי מה שתכננו", user_id=1, nutrition_context=context)

    call = client.responses.calls[0]
    messages = call.get("input") or []
    joined = " ".join(str(m.get("content", "")) for m in messages)  # type: ignore[union-attr]
    assert "planned_meals" in joined
    assert "עוף ואורז" in joined
