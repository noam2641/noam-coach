"""B10 / ARCH-12 — coaching-memory taxonomy and persistence policy.

The six-class contract, and THE required regression: the trace must answer
"why did the coach assume 250g?" with a machine-readable provenance chain
(observations → auto-proposal → explicit confirmation → the decision-grade
prompt line meal analysis consumed).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import coach_bot
import event_log
from db import Database
from helpers import utc_now
from noam_coach.observability import ObservabilityMode, set_mode
from noam_coach.observability.emit import reset_observability_health
from noam_coach.observability.modes import reset_mode
from noam_coach.services.coaching_memory import (
    MemoryClass,
    MemoryPolicyViolation,
    coaching_memory_snapshot,
    confirm_food_identity,
    food_identity_prompt_lines,
    install_coaching_memory_capture,
    record_coaching_memory,
    record_food_identity_observation,
    uninstall_coaching_memory_capture,
)

USER_ID = 1


@pytest.fixture
async def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Database:
    database = Database(str(tmp_path / "b10.db"))
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
def _isolation():
    set_mode(ObservabilityMode.CONTENT)
    reset_observability_health()
    yield
    uninstall_coaching_memory_capture()
    reset_mode()
    reset_observability_health()


def _memory_events(events: list[Any]) -> list[Any]:
    return [
        e for e in events
        if e.event == "state.mutated"
        and e.properties.get("domain") == "coaching_memory"
    ]


# ---------------------------------------------------------------------------
# The persistence contract per class
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_turn_constraints_are_never_persisted(db: Database) -> None:
    with pytest.raises(MemoryPolicyViolation):
        await record_coaching_memory(
            db, USER_ID, MemoryClass.ONE_TURN, "no_tuna_now", True,
            provenance="explicit_statement",
        )
    assert _memory_events(await event_log.list_events(db, USER_ID)) == []


@pytest.mark.asyncio
async def test_meal_instance_corrections_require_the_instance(db: Database) -> None:
    with pytest.raises(MemoryPolicyViolation):
        await record_coaching_memory(
            db, USER_ID, MemoryClass.MEAL_INSTANCE, "cottage_grams", 250,
            provenance="meal_instance_correction",
        )
    await record_coaching_memory(
        db, USER_ID, MemoryClass.MEAL_INSTANCE, "cottage_grams", 250,
        provenance="meal_instance_correction", entity_ref="approval-7",
    )
    events = _memory_events(await event_log.list_events(db, USER_ID))
    assert events and events[0].properties["entity_ref"] == "approval-7"
    assert events[0].properties["confirmation_state"] == "instance_scoped"


@pytest.mark.asyncio
async def test_food_identity_direct_write_is_refused(db: Database) -> None:
    with pytest.raises(MemoryPolicyViolation):
        await record_coaching_memory(
            db, USER_ID, MemoryClass.FOOD_IDENTITY, "קוטג'", {"grams": 250},
            provenance="behavioral_inference",
        )


@pytest.mark.asyncio
async def test_alias_requires_explicit_provenance(db: Database) -> None:
    with pytest.raises(MemoryPolicyViolation):
        await record_coaching_memory(
            db, USER_ID, MemoryClass.TERMINOLOGY_ALIAS, "הלבן", "קוטג' 5%",
            provenance="single_ambiguous_use",
        )
    await record_coaching_memory(
        db, USER_ID, MemoryClass.TERMINOLOGY_ALIAS, "הלבן", "קוטג' 5%",
        provenance="explicit_statement",
    )
    snapshot = await coaching_memory_snapshot(db, USER_ID)
    assert snapshot["aliases"] == [{"alias": "הלבן", "target": "קוטג' 5%"}]
    events = _memory_events(await event_log.list_events(db, USER_ID))
    assert events[-1].properties["memory_class"] == "terminology_alias"
    assert events[-1].properties["provenance"] == "explicit_statement"


@pytest.mark.asyncio
async def test_safety_medical_refuses_behavioral_inference(db: Database) -> None:
    with pytest.raises(MemoryPolicyViolation):
        await record_coaching_memory(
            db, USER_ID, MemoryClass.SAFETY_MEDICAL, "knee_pain", "avoid squats",
            provenance="behavioral_inference",
        )
    await record_coaching_memory(
        db, USER_ID, MemoryClass.SAFETY_MEDICAL, "knee_pain", "avoid squats",
        provenance="user_explicit", entity_ref="constraint-1",
    )
    events = _memory_events(await event_log.list_events(db, USER_ID))
    assert events and events[-1].properties["memory_class"] == "safety_medical"


@pytest.mark.asyncio
async def test_preference_delegates_to_the_existing_service(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded: list[dict[str, Any]] = []

    async def fake_record(db_: Any, user_id: int, slots: dict[str, Any]) -> None:
        recorded.append(slots)

    from noam_coach.services import food_preferences

    monkeypatch.setattr(
        food_preferences, "record_food_preference_from_slots", fake_record
    )
    await record_coaching_memory(
        db, USER_ID, MemoryClass.PREFERENCE, "no_alcohol",
        {"kind": "preference", "polarity": "avoid", "item": "אלכוהול"},
        provenance="explicit_statement",
    )
    assert recorded and recorded[0]["item"] == "אלכוהול"


# ---------------------------------------------------------------------------
# Food identity: observation → proposal → explicit confirmation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_correction_is_an_observation_not_a_proposal(db: Database) -> None:
    state = await record_food_identity_observation(
        db, USER_ID, "קוטג'", 250, provenance="meal_instance_correction"
    )
    assert state["state"] == "observed"
    lines = await food_identity_prompt_lines(db, USER_ID)
    assert lines == []  # not even a calibration hint yet


@pytest.mark.asyncio
async def test_two_consistent_corrections_create_a_proposal_only(db: Database) -> None:
    await record_food_identity_observation(
        db, USER_ID, "קוטג'", 250, provenance="meal_instance_correction"
    )
    state = await record_food_identity_observation(
        db, USER_ID, "קוטג'", 245, provenance="meal_instance_correction"
    )
    assert state["state"] == "proposal"
    lines = await food_identity_prompt_lines(db, USER_ID)
    assert len(lines) == 1
    assert "UNCONFIRMED" in lines[0]  # calibration-only, never decision-grade


@pytest.mark.asyncio
async def test_inconsistent_corrections_do_not_propose(db: Database) -> None:
    await record_food_identity_observation(
        db, USER_ID, "קוטג'", 250, provenance="meal_instance_correction"
    )
    state = await record_food_identity_observation(
        db, USER_ID, "קוטג'", 120, provenance="meal_instance_correction"
    )
    assert state["state"] == "observed"


@pytest.mark.asyncio
async def test_why_did_the_coach_assume_250g(db: Database) -> None:
    """THE required regression: the machine-readable provenance chain."""
    await record_food_identity_observation(
        db, USER_ID, "קוטג'", 250,
        provenance="meal_instance_correction", entity_ref="approval-1",
    )
    await record_food_identity_observation(
        db, USER_ID, "קוטג'", 250,
        provenance="meal_instance_correction", entity_ref="approval-2",
    )
    confirmed = await confirm_food_identity(db, USER_ID, "קוטג'")
    assert confirmed and confirmed["state"] == "confirmed"

    # The decision-grade prompt line the analyzer consumes:
    lines = await food_identity_prompt_lines(db, USER_ID)
    assert any("250" in line and "CONFIRMED" in line for line in lines)

    # The machine-readable chain: observation → proposal → confirmation,
    # each with class + provenance + confirmation state.
    events = _memory_events(await event_log.list_events(db, USER_ID))
    outcomes = [e.outcome for e in events]
    assert outcomes == ["observation_recorded", "proposal_created", "confirmed"]
    assert all(e.properties["memory_class"] == "food_identity" for e in events)
    assert events[0].properties["provenance"] == "meal_instance_correction"
    assert events[-1].properties["provenance"] == "explicit_confirmation"
    assert events[-1].properties["grams"] == 250
    # And the fact itself carries the observation provenance chain.
    assert len(confirmed["observations"]) == 2
    assert {o["entity_ref"] for o in confirmed["observations"]} == {
        "approval-1", "approval-2",
    }


# ---------------------------------------------------------------------------
# Capture at the meal-correction boundary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reanalysis_corrections_feed_observations_and_offer_confirmation(
    db: Database,
) -> None:
    analyses: list[Any] = []

    async def fake_reanalyze(
        image_path: str, correction_text: str,
        locked_corrections: Any = None, nutrition_context: Any = None,
    ) -> Any:
        analysis = SimpleNamespace(notes=[])
        analyses.append(analysis)
        return analysis

    real = coach_bot.reanalyze_meal_with_text_and_image
    coach_bot.reanalyze_meal_with_text_and_image = fake_reanalyze
    try:
        install_coaching_memory_capture()
        ctx = {"user_id": USER_ID}
        first = await coach_bot.reanalyze_meal_with_text_and_image(
            "x.jpg", "זה היה 250 גרם קוטג'", None, ctx
        )
        assert first.notes == []  # one observation: no offer yet
        second = await coach_bot.reanalyze_meal_with_text_and_image(
            "x.jpg", "שוב, 250 גרם קוטג'", None, ctx
        )
        # Second consistent correction → proposal → explicit-confirmation offer.
        assert any("קבע" in note for note in second.notes)
        snapshot = await coaching_memory_snapshot(db, USER_ID)
        states = {i["food"]: i["state"] for i in snapshot["food_identities"]}
        assert list(states.values()) == ["proposal"]
    finally:
        uninstall_coaching_memory_capture()
        coach_bot.reanalyze_meal_with_text_and_image = real


@pytest.mark.asyncio
async def test_snapshot_is_bounded(db: Database) -> None:
    for index in range(15):
        await record_coaching_memory(
            db, USER_ID, MemoryClass.TERMINOLOGY_ALIAS,
            f"כינוי{index}", f"מוצר {index}", provenance="explicit_statement",
        )
    snapshot = await coaching_memory_snapshot(db, USER_ID)
    assert len(snapshot["aliases"]) <= 10
    assert set(snapshot) == {"food_identities", "aliases"}


# ---------------------------------------------------------------------------
# Consumer integrations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirm_phrase_resolves_deterministically(db: Database) -> None:
    """The offered "קבע קוטג' 250 גרם" phrase confirms via the turn-context
    pipeline — the explicit confirmation that makes knowledge decision-grade."""
    from noam_coach.services.turn_context import (
        install_turn_context,
        uninstall_turn_context,
    )

    await record_food_identity_observation(
        db, USER_ID, "קוטג'", 250, provenance="meal_instance_correction"
    )
    await record_food_identity_observation(
        db, USER_ID, "קוטג'", 250, provenance="meal_instance_correction"
    )

    class _Msg:
        text = "קבע קוטג' 250 גרם"
        message_id = 1
        chat = SimpleNamespace(id=USER_ID)

        def __init__(self) -> None:
            self.replies: list[str] = []

        async def reply_text(self, text: str, **kwargs: Any) -> None:
            self.replies.append(text)

    message = _Msg()
    update = SimpleNamespace(
        effective_message=message,
        effective_user=SimpleNamespace(id=USER_ID),
    )
    try:
        install_turn_context()
        await coach_bot.route_free_text(update, USER_ID)
    finally:
        uninstall_turn_context()

    assert message.replies and "250" in message.replies[0]
    snapshot = await coaching_memory_snapshot(db, USER_ID)
    assert snapshot["food_identities"][0]["state"] == "confirmed"
    lines = await food_identity_prompt_lines(db, USER_ID)
    assert any("CONFIRMED" in line for line in lines)


@pytest.mark.asyncio
async def test_meal_analysis_prompt_block_carries_confirmed_memory(
    db: Database,
) -> None:
    """The analyzer consumers (analysis + reanalysis both build their prompt
    through learned_foods_prompt_block) see the decision-grade line."""
    from noam_coach.services.learned_foods import learned_foods_prompt_block

    await record_food_identity_observation(
        db, USER_ID, "קוטג'", 250, provenance="meal_instance_correction"
    )
    await record_food_identity_observation(
        db, USER_ID, "קוטג'", 250, provenance="meal_instance_correction"
    )
    await confirm_food_identity(db, USER_ID, "קוטג'")
    await record_coaching_memory(
        db, USER_ID, MemoryClass.TERMINOLOGY_ALIAS, "הלבן", "קוטג' 5%",
        provenance="explicit_statement",
    )

    block = await learned_foods_prompt_block(db, USER_ID)
    assert "250" in block and "CONFIRMED" in block
    assert "הלבן" in block  # the explicit alias travels too
