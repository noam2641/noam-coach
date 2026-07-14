"""Observability O6 — coaching and menu AI decision chain tests.

Acceptance criteria covered:
- intent AI result and final selected intent can differ; both stay visible
- low-confidence/failure fallback is reconstructable
- menu validation failure links (same trace, causal order) to the repair
  AI call; deterministic fallback is explicitly represented
- evening-summary computed food flags stay distinguishable from AI prose
- routine-extraction failure → empty/default result remains visible
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import assistant as assistant_module
import coach_bot
import event_log
import recommendations
from assistant import Intent
from db import Database
from helpers import utc_now
from noam_coach.observability import ObservabilityMode, interaction_scope, set_mode
from noam_coach.observability.ai_invocation import (
    ObservedOpenAIClient,
    install_ai_observability,
    uninstall_ai_observability,
)
from noam_coach.observability.decision_trace import (
    install_decision_trace,
    uninstall_decision_trace,
)
from noam_coach.observability.emit import reset_observability_health
from noam_coach.observability.modes import reset_mode
from noam_coach.services import morning_menu_pipeline as pipeline
from noam_coach.services.menu_validation import MealViolation, MenuValidationResult

USER_ID = 1


class FakeResponses:
    def __init__(self, result: Any = None) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def parse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return SimpleNamespace(output_parsed=self.result, usage=None)


class FakeClient:
    def __init__(self, responses: FakeResponses) -> None:
        self.responses = responses


@pytest.fixture
async def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Database:
    database = Database(str(tmp_path / "obs_o6.db"))
    await database.init()
    await database.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(?, 'Test', NULL, ?)",
        (USER_ID, utc_now()),
    )
    now = utc_now()
    await database.execute(
        """
        INSERT INTO goal_versions(user_id, calories, protein, steps, phase, status, source, created_at)
        VALUES(?, 2200, 160, 8000, 'fat_loss_muscle_retention', 'active', 'test', ?)
        """,
        (USER_ID, now),
    )
    monkeypatch.setattr(coach_bot, "DB", database)
    from config import SETTINGS

    monkeypatch.setattr(SETTINGS, "telegram_allowed_user_id", USER_ID, raising=False)
    return database


@pytest.fixture(autouse=True)
def _obs_isolation():
    set_mode(ObservabilityMode.CONTENT)
    reset_observability_health()
    install_ai_observability()
    install_decision_trace()
    yield
    uninstall_decision_trace()
    uninstall_ai_observability()
    reset_mode()
    reset_observability_health()


def _by_event(events: list, name: str) -> list:
    return [e for e in events if e.event == name]


# ---------------------------------------------------------------------------
# Intent classification decisions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_intent_ai_result_and_final_intent_differ_and_both_visible(db: Database) -> None:
    """Low-confidence AI answer overridden by the stronger keyword guess:
    'לקחתי ריטלין' keyword-classifies as morning_flag @0.9; the fake AI says
    smalltalk @0.2 — the product keeps the keyword guess."""
    low_confidence_ai = ObservedOpenAIClient(FakeClient(FakeResponses(
        Intent(action="smalltalk_or_help", confidence=0.2)
    )))
    with interaction_scope(user_id=USER_ID):
        final = await assistant_module.classify_intent(
            low_confidence_ai, "gpt-test", "לקחתי ריטלין"
        )
    assert final.action == "morning_flag"

    events = await event_log.list_events(db, USER_ID)
    decided = _by_event(events, "decision.finalized")[0]
    assert decided.properties["ai_action"] == "smalltalk_or_help"
    assert decided.properties["ai_confidence"] == 0.2
    assert decided.properties["final_action"] == "morning_flag"
    assert decided.properties["resolution"] == "fallback_overrode_ai"
    # The raw AI call itself is separately visible.
    assert _by_event(events, "ai.call.completed")


@pytest.mark.asyncio
async def test_intent_without_ai_client_is_reconstructable(db: Database) -> None:
    with interaction_scope(user_id=USER_ID):
        final = await assistant_module.classify_intent(None, "gpt-test", "לקחתי ריטלין")
    assert final.action == "morning_flag"
    decided = _by_event(await event_log.list_events(db, USER_ID), "decision.finalized")[0]
    assert decided.properties["resolution"] == "keyword_fallback_no_ai_result"
    assert decided.properties["ai_action"] is None


# ---------------------------------------------------------------------------
# Morning-menu decision chain
# ---------------------------------------------------------------------------


def _menu(*names: str) -> recommendations.MorningMenu:
    return recommendations.MorningMenu(
        headline="תפריט",
        meals=[
            recommendations.MenuMeal(
                name=name, time_hint=f"{8 + 4 * i:02d}:00", calories=650, protein=45, note="",
            )
            for i, name in enumerate(names)
        ],
    )


class _StatefulValidator:
    """First call fails with a meal-level violation; later calls pass."""

    def __init__(self, fail_times: int = 1, meal_index: int | None = 0) -> None:
        self.fail_times = fail_times
        self.meal_index = meal_index
        self.calls = 0

    def __call__(self, candidate: Any, **kwargs: Any) -> MenuValidationResult:
        self.calls += 1
        if self.calls <= self.fail_times:
            return MenuValidationResult(violations=[
                MealViolation(self.meal_index, "meal", "disliked_food", "simulated"),
            ])
        return MenuValidationResult(violations=[])


@pytest.mark.asyncio
async def test_menu_validation_failure_links_to_repair_ai_call(
    db: Database, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ai_client = ObservedOpenAIClient(FakeClient(FakeResponses(_menu("א", "ב", "ג"))))

    async def stub_repair(client: Any, model: str, **kwargs: Any) -> Any:
        # A real targeted repair performs one responses.parse call.
        response = await client.responses.parse(
            model=model, input=[{"role": "user", "content": "repair"}],
        )
        return response.output_parsed

    monkeypatch.setattr(recommendations, "repair_menu_meals", stub_repair)
    monkeypatch.setattr(pipeline, "validate_menu", _StatefulValidator(fail_times=1))

    with interaction_scope(user_id=USER_ID) as scope:
        result = await pipeline.build_personalized_morning_menu(
            db, USER_ID,
            profile={}, goal={"calories": 2200, "protein": 160},
            today_has_workout=False, daily_flags={},
            openai_client=ai_client, openai_model="gpt-test",
        )
    assert result.repaired is True

    events = await event_log.list_events(db, USER_ID)
    names = [e.event for e in events]
    # Causal chain, one trace: initial generation AI call → validation
    # failed → REPAIR AI call → repaired decision → finalized menu.
    failed_index = names.index("validation.failed")
    repair_call_indices = [
        i for i, name in enumerate(names) if name == "ai.call.started" and i > failed_index
    ]
    assert repair_call_indices, "the repair AI call must follow the validation failure"
    repair_completed = [
        i for i, name in enumerate(names) if name == "ai.call.completed" and i > failed_index
    ]
    assert repair_completed[0] < names.index("decision.repaired")
    assert names.index("decision.repaired") < names.index("decision.finalized")
    assert {e.trace_id for e in events} == {scope.trace_id}

    failed = _by_event(events, "validation.failed")[0]
    assert failed.properties["violations"][0]["code"] == "disliked_food"
    finalized = _by_event(events, "decision.finalized")[-1]
    assert finalized.outcome == "repaired"


@pytest.mark.asyncio
async def test_menu_repair_failure_selects_deterministic_fallback_explicitly(
    db: Database, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No AI client at all: meal-splice fallback, then (still failing) the
    deterministic menu fallback, then hard block — all explicit."""
    monkeypatch.setattr(pipeline, "validate_menu", _StatefulValidator(fail_times=99))

    with interaction_scope(user_id=USER_ID):
        with pytest.raises(pipeline.MenuGenerationBlocked):
            await pipeline.build_personalized_morning_menu(
                db, USER_ID,
                profile={}, goal={"calories": 2200, "protein": 160},
                today_has_workout=False, daily_flags={},
                openai_client=None, openai_model="gpt-test",
            )

    events = await event_log.list_events(db, USER_ID)
    fallbacks = _by_event(events, "decision.fallback_selected")
    reasons = [f.properties.get("reason") for f in fallbacks]
    assert "no_ai_client_meal_splice" in reasons
    assert "menu_repair_failed" in reasons
    blocked = [e for e in _by_event(events, "validation.failed") if e.outcome == "blocked"]
    assert blocked and blocked[0].properties["codes"] == ["disliked_food"]
    assert _by_event(events, "decision.finalized") == []  # nothing false-finalized


@pytest.mark.asyncio
async def test_clean_deterministic_menu_finalizes_with_source(db: Database) -> None:
    with interaction_scope(user_id=USER_ID):
        result = await pipeline.build_personalized_morning_menu(
            db, USER_ID,
            profile={}, goal={"calories": 2200, "protein": 160},
            today_has_workout=False, daily_flags={},
            openai_client=None, openai_model="gpt-test",
        )
    assert result.menu.meals
    events = await event_log.list_events(db, USER_ID)
    assert _by_event(events, "validation.completed")
    finalized = _by_event(events, "decision.finalized")[-1]
    assert finalized.outcome == "deterministic"
    assert finalized.properties["meal_count"] == len(result.menu.meals)


# ---------------------------------------------------------------------------
# Evening summary + routine extraction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evening_summary_flags_distinguishable_from_ai_prose(db: Database) -> None:
    meals = [
        {"name": "בורקס", "calories": 550, "protein": 8},
        {"name": "חזה עוף", "calories": 300, "protein": 55},
    ]
    with interaction_scope(user_id=USER_ID):
        summary = await recommendations.evening_summary(
            None, "gpt-test", {}, {"calories": 2200, "protein": 160},
            1800.0, 120.0, meals,
        )
    assert summary.headline

    events = await event_log.list_events(db, USER_ID)
    context = _by_event(events, "context.built")[0]
    assert context.entity == "food_flags"
    assert context.properties["deterministic"] is True
    flags = context.properties["content"]["computed_food_flags"]
    assert any("בורקס" in str(flag) for flag in flags)

    finalized = _by_event(events, "decision.finalized")[0]
    assert finalized.outcome == "deterministic_fallback"  # no client → no AI prose
    assert finalized.properties["computed_flag_count"] == len(flags)


@pytest.mark.asyncio
async def test_routine_extraction_failure_yields_visible_empty_default(
    db: Database, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from noam_coach.services import profile as profile_module

    monkeypatch.setattr(coach_bot, "OPENAI_CLIENT", None, raising=False)
    with interaction_scope(user_id=USER_ID):
        extraction = await profile_module.extract_daily_routine("יום רגיל שלי")

    dumped = extraction.model_dump()
    events = await event_log.list_events(db, USER_ID)
    fallback = _by_event(events, "decision.fallback_selected")[0]
    assert fallback.entity == "routine_extraction"
    assert fallback.outcome == "empty_default"
    assert fallback.properties["reason"] == "ai_unavailable_or_failed"
    finalized = _by_event(events, "decision.finalized")[0]
    assert finalized.outcome == "empty_default"
    assert finalized.properties["empty_result"] == (not any(dumped.values()))
