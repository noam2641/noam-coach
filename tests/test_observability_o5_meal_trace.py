"""Observability O5 — meal photo / correction end-to-end trace tests.

The required machine-provable core (not inferred from final DB state):
AI estimated 180g rice → user explicitly locked 250g → the trace preserves
the AI output (180), the override reason (user explicit quantity), the
final decision (250), and the media identity — with no base64 anywhere.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import coach_bot
import event_log
import meal_intelligence
from db import Database
from helpers import utc_now
from models import FoodItem, MealAnalysis
from noam_coach.observability import ObservabilityMode, interaction_scope, set_mode
from noam_coach.observability.ai_invocation import (
    ObservedOpenAIClient,
    install_ai_observability,
    uninstall_ai_observability,
)
from noam_coach.observability.emit import reset_observability_health
from noam_coach.observability.meal_trace import (
    install_meal_trace,
    media_id_for_bytes,
    uninstall_meal_trace,
)
from noam_coach.observability.modes import reset_mode

USER_ID = 1
IMAGE_BYTES = b"\xff\xd8\xff fake-jpeg-bytes for the meal trace test \x11\x22\x33"


def _rice_analysis(grams: float = 180.0) -> MealAnalysis:
    return MealAnalysis(
        meal_name="אורז לבן",
        items=[
            FoodItem(
                name="אורז לבן",
                grams=grams,
                calories=234.0,
                protein=4.3,
                carbs=50.0,
                fat=0.4,
                confidence=0.8,
            )
        ],
        confidence=0.8,
    )


class FakeResponses:
    def __init__(self) -> None:
        self.result: Any = None
        self.error: BaseException | None = None
        self.calls: list[dict[str, Any]] = []

    async def parse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(output_parsed=self.result, usage=None)


class FakeClient:
    def __init__(self, responses: FakeResponses) -> None:
        self.responses = responses


@pytest.fixture
async def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Database:
    database = Database(str(tmp_path / "obs_o5.db"))
    await database.init()
    await database.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(?, 'Test', NULL, ?)",
        (USER_ID, utc_now()),
    )
    monkeypatch.setattr(coach_bot, "DB", database)
    from config import SETTINGS

    monkeypatch.setattr(SETTINGS, "telegram_allowed_user_id", USER_ID, raising=False)
    return database


@pytest.fixture
def fake_ai(monkeypatch: pytest.MonkeyPatch) -> FakeResponses:
    responses = FakeResponses()
    responses.result = _rice_analysis(180.0)
    proxy = ObservedOpenAIClient(FakeClient(responses))
    monkeypatch.setattr(coach_bot, "OPENAI_CLIENT", proxy, raising=False)
    return responses


@pytest.fixture(autouse=True)
def _obs_isolation():
    set_mode(ObservabilityMode.CONTENT)
    reset_observability_health()
    install_ai_observability()
    install_meal_trace()
    yield
    uninstall_meal_trace()
    uninstall_ai_observability()
    reset_mode()
    reset_observability_health()


def _by_event(events: list, name: str) -> list:
    return [e for e in events if e.event == name]


@pytest.mark.asyncio
async def test_successful_image_analysis_trace(db: Database, fake_ai: FakeResponses) -> None:
    from noam_coach.services import profile as profile_module

    with interaction_scope(user_id=USER_ID) as scope:
        analysis = await profile_module.analyze_meal_image(
            IMAGE_BYTES, user_id=USER_ID, caption="צהריים"
        )

    assert isinstance(analysis, MealAnalysis)
    events = await event_log.list_events(db, USER_ID)

    media = _by_event(events, "media.received")[0]
    expected_media_id = media_id_for_bytes(IMAGE_BYTES)
    assert media.properties["media_id"] == expected_media_id
    assert media.properties["sha256"] == meal_intelligence.sha256_bytes(IMAGE_BYTES)
    assert media.properties["byte_size"] == len(IMAGE_BYTES)
    assert media.properties["perceptual_hash"]

    started = _by_event(events, "ai.call.started")[0]
    assert started.properties["purpose"] == "meal_image_analysis"
    assert started.properties["media_ids"] == [expected_media_id]
    assert started.properties["image_count"] == 1

    completed = _by_event(events, "ai.call.completed")[0]
    assert completed.properties["output_schema"] == "MealAnalysis"
    assert completed.properties["content"]["output"]["items"][0]["grams"] == 180.0

    validated = _by_event(events, "validation.completed")[0]
    assert validated.properties["stage"] == "image_analysis"
    assert validated.properties["media_id"] == expected_media_id
    assert validated.properties["content"]["final_analysis"]["items"]

    # Everything belongs to one interaction trace.
    assert {e.trace_id for e in events} == {scope.trace_id}
    # And no raw bytes / base64 image content anywhere in the store.
    serialized = str([e.properties for e in events])
    assert "base64," not in serialized
    assert "fake-jpeg-bytes" not in serialized


@pytest.mark.asyncio
async def test_locked_quantity_correction_is_machine_provable(
    db: Database, fake_ai: FakeResponses, tmp_path: Path
) -> None:
    """THE acceptance case: AI says 180g, the user locks 250g."""
    from noam_coach.services import profile as profile_module

    image_path = tmp_path / "meal.jpg"
    image_path.write_bytes(IMAGE_BYTES)
    fake_ai.result = _rice_analysis(180.0)  # the AI insists on 180g again

    with interaction_scope(user_id=USER_ID):
        final = await profile_module.reanalyze_meal_with_text_and_image(
            str(image_path),
            "אורז 250 גרם",
            None,
            nutrition_context={"user_id": USER_ID},
        )

    # Product contract: explicit user quantity wins.
    assert final.items[0].grams == 250.0

    events = await event_log.list_events(db, USER_ID)

    # 1) The AI's raw interpretation is preserved: 180g.
    completed = _by_event(events, "ai.call.completed")[0]
    assert completed.properties["purpose"] == "meal_reanalysis"
    assert completed.properties["content"]["output"]["items"][0]["grams"] == 180.0

    # 2) The final decision is preserved: 250g, with the override reason.
    finalized = _by_event(events, "decision.finalized")[0]
    overrides = finalized.properties["overrides"]
    rice = [o for o in overrides if o.get("ai_grams") == 180.0][0]
    assert rice["final_grams"] == 250.0
    assert rice["reason"] == "user_explicit_quantity"
    assert finalized.properties["content"]["final_analysis"]["items"][0]["grams"] == 250.0
    assert finalized.properties["content"]["correction_text"] == "אורז 250 גרם"
    assert finalized.outcome == "corrected"

    # 3) Media identity is stable across analysis and re-analysis.
    assert finalized.properties["media_id"] == media_id_for_bytes(IMAGE_BYTES)

    # 4) No image payload leaked.
    serialized = str([e.properties for e in events])
    assert "base64," not in serialized


@pytest.mark.asyncio
async def test_reanalysis_failure_is_visible_and_propagates(
    db: Database, fake_ai: FakeResponses, tmp_path: Path
) -> None:
    from noam_coach.services import profile as profile_module

    image_path = tmp_path / "meal.jpg"
    image_path.write_bytes(IMAGE_BYTES)
    fake_ai.error = RuntimeError("simulated vision outage")

    with interaction_scope(user_id=USER_ID):
        with pytest.raises(RuntimeError):
            await profile_module.reanalyze_meal_with_text_and_image(
                str(image_path), "אורז 250 גרם", None,
            )

    events = await event_log.list_events(db, USER_ID)
    failed = _by_event(events, "ai.call.failed")[0]
    assert failed.properties["purpose"] == "meal_reanalysis"
    assert failed.properties["error_type"] == "RuntimeError"
    # No false finalized decision on the failure path.
    assert _by_event(events, "decision.finalized") == []


@pytest.mark.asyncio
async def test_duplicate_detection_outcomes(db: Database, fake_ai: FakeResponses) -> None:
    with interaction_scope(user_id=USER_ID):
        result = await meal_intelligence.find_image_duplicate(
            db, user_id=USER_ID, image_bytes=IMAGE_BYTES,
            telegram_file_unique_id="u-777",
        )
    assert result is None
    events = await event_log.list_events(db, USER_ID)
    unique = [e for e in events if e.event == "validation.completed"][0]
    assert unique.entity == "media_duplicate"
    assert unique.outcome == "no_duplicate"

    # Register a saved meal fingerprint for the same provider identity.
    meal_id = await db.execute(
        "INSERT INTO meals(user_id, name, calories, protein, carbs, fat, confidence, eaten_at, created_at)"
        " VALUES(?, 'קודמת', 500, 30, 0, 0, 1, ?, ?)",
        (USER_ID, utc_now(), utc_now()),
    )
    await db.execute(
        """
        INSERT INTO meal_fingerprints(meal_id, user_id, telegram_file_unique_id, sha256, perceptual_hash, created_at)
        VALUES(?, ?, 'u-777', ?, ?, ?)
        """,
        (
            meal_id, USER_ID,
            meal_intelligence.sha256_bytes(IMAGE_BYTES),
            meal_intelligence.perceptual_hash(IMAGE_BYTES),
            utc_now(),
        ),
    )
    with interaction_scope(user_id=USER_ID):
        duplicate = await meal_intelligence.find_image_duplicate(
            db, user_id=USER_ID, image_bytes=IMAGE_BYTES,
            telegram_file_unique_id="u-777",
        )
    assert duplicate is not None
    events = await event_log.list_events(db, USER_ID)
    outcomes = [e.outcome for e in events if e.entity == "media_duplicate"]
    assert outcomes == ["no_duplicate", "duplicate"]
    dup_event = [e for e in events if e.outcome == "duplicate"][0]
    assert dup_event.properties["matched_meal_id"] == meal_id


@pytest.mark.asyncio
async def test_legacy_domain_events_join_the_ambient_trace(db: Database) -> None:
    """append_event's correlation defaulting: MEAL_ANALYSIS_COMPLETED-style
    legacy writes inside a handler now correlate to the interaction."""
    with interaction_scope(user_id=USER_ID) as scope:
        await event_log.append_event(
            db, USER_ID, "MEAL_ANALYSIS_COMPLETED", entity="meal",
            properties={"calories": 500},
        )
    event = (await event_log.list_events(db, USER_ID))[0]
    assert event.trace_id == scope.trace_id
    assert event.interaction_id == scope.interaction_id
    assert event.is_legacy_uncorrelated is False
    # Outside any scope the behavior is unchanged: explicitly uncorrelated.
    await event_log.append_event(db, USER_ID, "meal_saved", entity="meal")
    outside = (await event_log.list_events(db, USER_ID))[-1]
    assert outside.is_legacy_uncorrelated is True
