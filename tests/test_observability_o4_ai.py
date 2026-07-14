"""Observability O4 — canonical AI invocation boundary acceptance tests.

Acceptance criteria covered:
- every inventoried production OpenAI call site is purpose-wrapped
- structured (Pydantic) return contracts stay byte-identical
- failed AI calls are visible with classification
- no image base64/data URL reaches event storage; media refs attach
- request/response are retained (mode-governed) with digests
- the proxy preserves ``client is None`` semantics and passthrough attrs
"""

from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import coach_bot
import event_log
from assistant import Intent
from db import Database
from helpers import utc_now
from noam_coach.observability import ObservabilityMode, set_mode
from noam_coach.observability.ai_invocation import (
    _PURPOSE_WRAPS,
    ObservedOpenAIClient,
    ai_purpose,
    install_ai_observability,
    media_refs_scope,
    uninstall_ai_observability,
)
from noam_coach.observability.emit import reset_observability_health
from noam_coach.observability.modes import reset_mode
from noam_coach.observability.obs_context import interaction_scope

USER_ID = 1


@pytest.fixture
async def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Database:
    database = Database(str(tmp_path / "obs_o4.db"))
    await database.init()
    await database.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(?, 'Test', NULL, ?)",
        (USER_ID, utc_now()),
    )
    monkeypatch.setattr(coach_bot, "DB", database)
    from config import SETTINGS

    monkeypatch.setattr(SETTINGS, "telegram_allowed_user_id", USER_ID, raising=False)
    return database


@pytest.fixture(autouse=True)
def _obs_isolation():
    set_mode(ObservabilityMode.CONTENT)
    reset_observability_health()
    yield
    uninstall_ai_observability()
    reset_mode()
    reset_observability_health()


class FakeResponses:
    def __init__(self, *, parse_result: Any = None, error: BaseException | None = None) -> None:
        self.parse_result = parse_result
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def parse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.parse_result

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(output_text="ניסוח חדש", usage=None)


class FakeClient:
    def __init__(self, responses: FakeResponses) -> None:
        self.responses = responses
        self.api_key = "sk-THIS-MUST-NEVER-BE-STORED-123456"


def _intent_response() -> SimpleNamespace:
    return SimpleNamespace(
        output_parsed=Intent(action="next_meal", confidence=0.92),
        usage=SimpleNamespace(input_tokens=120, output_tokens=18),
    )


@pytest.mark.asyncio
async def test_parse_call_is_observed_and_contract_preserved(db: Database) -> None:
    fake = FakeResponses(parse_result=_intent_response())
    proxy = ObservedOpenAIClient(FakeClient(fake))

    with interaction_scope(user_id=USER_ID), ai_purpose("intent_classification"):
        response = await proxy.responses.parse(
            model="gpt-test",
            input=[{"role": "user", "content": "מה לאכול עכשיו?"}],
            text_format=Intent,
        )

    # Contract preserved: the ORIGINAL object, exact Pydantic type.
    assert response.output_parsed == Intent(action="next_meal", confidence=0.92)
    assert isinstance(response.output_parsed, Intent)
    assert fake.calls[0]["model"] == "gpt-test"

    events = await event_log.list_events(db, USER_ID)
    started = [e for e in events if e.event == "ai.call.started"][0]
    completed = [e for e in events if e.event == "ai.call.completed"][0]
    assert started.properties["purpose"] == "intent_classification"
    assert started.properties["model"] == "gpt-test"
    assert started.properties["operation"] == "responses.parse"
    assert started.properties["output_schema"] == "Intent"
    assert "מה לאכול עכשיו?" in str(started.properties["content"]["request"])
    assert completed.properties["ai_call_id"] == started.properties["ai_call_id"]
    assert completed.properties["output_kind"] == "Intent"
    assert completed.properties["usage_input"] == 120
    assert completed.properties["duration_ms"] >= 0
    assert completed.properties["content"]["output"]["action"] == "next_meal"
    # Same interaction + AI call ran in its own span.
    assert completed.interaction_id == started.interaction_id
    assert started.span_id is not None


@pytest.mark.asyncio
async def test_failed_ai_call_is_visible_and_propagates(db: Database) -> None:
    fake = FakeResponses(error=TimeoutError("provider timeout"))
    proxy = ObservedOpenAIClient(FakeClient(fake))

    with interaction_scope(user_id=USER_ID), ai_purpose("morning_menu_generation"):
        with pytest.raises(TimeoutError):
            await proxy.responses.parse(model="gpt-test", input=[], text_format=Intent)

    events = await event_log.list_events(db, USER_ID)
    failed = [e for e in events if e.event == "ai.call.failed"][0]
    assert failed.properties["purpose"] == "morning_menu_generation"
    assert failed.properties["error_type"] == "TimeoutError"
    assert failed.properties["failure_class"] == "timeout"
    assert failed.outcome == "TimeoutError"
    assert not [e for e in events if e.event == "ai.call.completed"]


@pytest.mark.asyncio
async def test_image_payload_never_reaches_storage_and_media_refs_attach(db: Database) -> None:
    data_url = "data:image/jpeg;base64," + ("QUJDRA==" * 300)
    fake = FakeResponses(parse_result=_intent_response())
    proxy = ObservedOpenAIClient(FakeClient(fake))

    with interaction_scope(user_id=USER_ID), ai_purpose("meal_image_analysis"):
        with media_refs_scope(["md_abc123"]):
            await proxy.responses.parse(
                model="gpt-test",
                input=[
                    {"role": "user", "content": [
                        {"type": "input_text", "text": "מה בצלחת?"},
                        {"type": "input_image", "image_url": data_url},
                    ]},
                ],
                text_format=Intent,
            )

    events = await event_log.list_events(db, USER_ID)
    started = [e for e in events if e.event == "ai.call.started"][0]
    serialized = str(started.properties)
    assert "base64," not in serialized
    assert "QUJDRA" not in serialized
    assert started.properties["image_count"] == 1
    assert started.properties["media_ids"] == ["md_abc123"]
    # The image became a sha256 reference with its size.
    request_text = str(started.properties["content"]["request"])
    assert "image_ref" in request_text
    # And the provider still received the REAL data URL untouched.
    sent = fake.calls[0]["input"][0]["content"][1]["image_url"]
    assert sent == data_url


@pytest.mark.asyncio
async def test_api_key_on_client_never_leaks(db: Database) -> None:
    fake = FakeResponses(parse_result=_intent_response())
    proxy = ObservedOpenAIClient(FakeClient(fake))
    assert proxy.api_key.startswith("sk-")  # passthrough works...
    with interaction_scope(user_id=USER_ID):
        await proxy.responses.parse(model="gpt-test", input=[], text_format=Intent)
    events = await event_log.list_events(db, USER_ID)
    assert "sk-THIS-MUST-NEVER-BE-STORED" not in str([e.properties for e in events])


@pytest.mark.asyncio
async def test_unscoped_call_is_recorded_as_unclassified(db: Database) -> None:
    proxy = ObservedOpenAIClient(FakeClient(FakeResponses(parse_result=_intent_response())))
    with interaction_scope(user_id=USER_ID):
        await proxy.responses.parse(model="gpt-test", input=[], text_format=Intent)
    started = [e for e in await event_log.list_events(db, USER_ID) if e.event == "ai.call.started"][0]
    assert started.properties["purpose"] == "unclassified"


@pytest.mark.asyncio
async def test_metadata_mode_digests_request_and_output(db: Database) -> None:
    set_mode(ObservabilityMode.METADATA)
    proxy = ObservedOpenAIClient(FakeClient(FakeResponses(parse_result=_intent_response())))
    with interaction_scope(user_id=USER_ID):
        await proxy.responses.parse(
            model="gpt-test", input=[{"role": "user", "content": "סודי"}], text_format=Intent,
        )
    events = await event_log.list_events(db, USER_ID)
    for event in events:
        assert "content" not in event.properties
        assert "content_digest" in event.properties
        assert "סודי" not in str(event.properties)


def test_all_inventoried_call_sites_are_purpose_wrapped() -> None:
    """Install must wrap every mechanically inventoried production call site."""
    install_ai_observability()
    try:
        expected = {
            ("assistant", "classify_intent"): "intent_classification",
            ("recommendations", "motivation_message"): "motivation_rephrasing",
            ("recommendations", "morning_menu"): "morning_menu_generation",
            ("recommendations", "repair_menu_meals"): "menu_repair",
            ("recommendations", "intraday_next_meals"): "next_meal_recommendation",
            ("recommendations", "evening_summary"): "evening_summary",
            ("noam_coach.services.goal_explainer", "explain_targets_with_ai"): "goal_explanation",
            ("noam_coach.services.profile", "analyze_meal_image"): "meal_image_analysis",
            ("noam_coach.services.profile", "analyze_meal_text"): "meal_text_analysis",
            ("noam_coach.services.profile", "reanalyze_meal_with_text_and_image"): "meal_reanalysis",
            ("noam_coach.services.profile", "extract_daily_routine"): "routine_extraction",
        }
        assert {(m, f): p for m, f, p in _PURPOSE_WRAPS} == expected
        for (module_path, function_name), purpose in expected.items():
            module = importlib.import_module(module_path)
            wrapped = getattr(module, function_name)
            assert getattr(wrapped, "__obs_purpose__", None) == purpose, (module_path, function_name)
    finally:
        uninstall_ai_observability()


@pytest.mark.asyncio
async def test_classify_intent_end_to_end_with_purpose(db: Database) -> None:
    """Real call site through the wrap: purpose + schema + preserved Intent."""
    install_ai_observability()
    try:
        import assistant as assistant_module

        fake = FakeResponses(parse_result=_intent_response())
        client = ObservedOpenAIClient(FakeClient(fake))
        with interaction_scope(user_id=USER_ID):
            intent = await assistant_module.classify_intent(client, "gpt-test", "מה לאכול עכשיו?")
        assert isinstance(intent, Intent)
        assert intent.action == "next_meal"
        started = [e for e in await event_log.list_events(db, USER_ID) if e.event == "ai.call.started"][0]
        assert started.properties["purpose"] == "intent_classification"
        assert started.properties["output_schema"] == "Intent"
    finally:
        uninstall_ai_observability()


@pytest.mark.asyncio
async def test_uninstall_restores_originals(db: Database) -> None:
    import recommendations

    before = recommendations.morning_menu
    install_ai_observability()
    assert recommendations.morning_menu is not before
    uninstall_ai_observability()
    assert recommendations.morning_menu is before
