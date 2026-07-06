"""RE10-10 — AI-phrased goal explanation, with a strict numeric-integrity guardrail.

The AI is only allowed to rephrase; the deterministic calorie/protein numbers
must always reappear verbatim, or the module falls back to the plain template.
"""

from __future__ import annotations

from typing import Any

import pytest

import targets
from noam_coach.services.goal_explainer import (
    _numbers_present,
    _required_numbers,
    explain_targets_with_ai,
)


def _sample_targets() -> targets.Targets:
    return targets.compute_targets(
        90, avg_steps=8000, sex="male", height_cm=178, age=35,
        goal_type="fat_loss_muscle_retention",
    )


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.output_text = text


class _FakeClient:
    def __init__(self, text: str | Exception) -> None:
        self._text = text
        self.responses = self

    async def create(self, **_kwargs: Any) -> _FakeResponse:
        if isinstance(self._text, Exception):
            raise self._text
        return _FakeResponse(self._text)


@pytest.mark.asyncio
async def test_no_client_returns_deterministic_fallback() -> None:
    t = _sample_targets()
    result = await explain_targets_with_ai(t, openai_client=None, model="gpt-4.1-mini")
    assert result == targets.explain_targets(t)


@pytest.mark.asyncio
async def test_valid_ai_response_with_numbers_intact_is_used() -> None:
    t = _sample_targets()
    required = _required_numbers(t)
    text = f"היעד היומי שלך הוא {required[0]} קלוריות ו-{required[1]} גרם חלבון, מחושב מהפרופיל שלך."
    client = _FakeClient(text)
    result = await explain_targets_with_ai(t, openai_client=client, model="gpt-4.1-mini")
    assert result == text


@pytest.mark.asyncio
async def test_ai_response_missing_calorie_number_falls_back() -> None:
    t = _sample_targets()
    text = "היעד שלך חושב בקפידה על סמך הנתונים שלך."  # no numbers at all
    client = _FakeClient(text)
    result = await explain_targets_with_ai(t, openai_client=client, model="gpt-4.1-mini")
    assert result == targets.explain_targets(t)


@pytest.mark.asyncio
async def test_ai_response_with_wrong_calorie_number_falls_back() -> None:
    t = _sample_targets()
    wrong_calories = t.calories + 500
    text = f"היעד שלך הוא {wrong_calories:,} קלוריות ו-{t.protein} גרם חלבון."
    client = _FakeClient(text)
    result = await explain_targets_with_ai(t, openai_client=client, model="gpt-4.1-mini")
    assert result == targets.explain_targets(t)


@pytest.mark.asyncio
async def test_ai_exception_falls_back_gracefully() -> None:
    t = _sample_targets()
    client = _FakeClient(RuntimeError("network down"))
    result = await explain_targets_with_ai(t, openai_client=client, model="gpt-4.1-mini")
    assert result == targets.explain_targets(t)


@pytest.mark.asyncio
async def test_empty_ai_response_falls_back() -> None:
    t = _sample_targets()
    client = _FakeClient("")
    result = await explain_targets_with_ai(t, openai_client=client, model="gpt-4.1-mini")
    assert result == targets.explain_targets(t)


def test_numbers_present_helper() -> None:
    assert _numbers_present("יעד 2,180 קלוריות ו-200 גרם", ["2,180", "200"])
    assert not _numbers_present("יעד קלורי מחושב", ["2,180", "200"])
