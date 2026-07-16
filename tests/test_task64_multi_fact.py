"""TASK-64 — multi-fact free-text profile updates during active flows.

Required journey: user inside an active wizard sends one sentence with
several profile facts → the explicit facts are extracted, validated and
saved under canonical keys → the active wizard remains resumable → the next
interaction continues coherently.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import coach_bot
import conversation
import event_log
import user_model
from db import Database
from helpers import utc_now
from noam_coach.observability import ObservabilityMode, set_mode
from noam_coach.observability.emit import reset_observability_health
from noam_coach.observability.modes import reset_mode
from noam_coach.services.multi_fact import (
    install_multi_fact_updates,
    parse_multi_fact_update,
    uninstall_multi_fact_updates,
)

USER_ID = 1


class FakeMessage:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.message_id = 42
        self.chat = SimpleNamespace(id=USER_ID)
        self.replies: list[str] = []

    async def reply_text(self, text: str, reply_markup: Any = None, parse_mode: Any = None) -> "FakeMessage":
        self.replies.append(text)
        return self


def _update(text: str) -> Any:
    return SimpleNamespace(
        effective_message=FakeMessage(text),
        effective_user=SimpleNamespace(id=USER_ID),
        effective_chat=SimpleNamespace(id=USER_ID),
    )


@pytest.fixture
async def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Database:
    database = Database(str(tmp_path / "task64.db"))
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
    uninstall_multi_fact_updates()
    reset_mode()
    reset_observability_health()


# ---------------------------------------------------------------------------
# Parser: label proximity, never position alone
# ---------------------------------------------------------------------------


def test_incident_text_parses_both_facts() -> None:
    result = parse_multi_fact_update("גובה 174 אימונים מזוהים 4")
    assert result.recognized["height_cm"] == 174
    assert result.recognized["training_days_per_week"] == 4
    assert not result.ambiguous


def test_labels_bind_by_proximity_not_position() -> None:
    # Number BEFORE its label and another AFTER its label, in one message.
    result = parse_multi_fact_update("הגובה 174, משקל 101.8")
    assert result.recognized["height_cm"] == 174
    assert result.recognized["weight_kg"] == 101.8


def test_sleep_window_and_height_together() -> None:
    result = parse_multi_fact_update("שינה 00:20-06:50 וגובה 174")
    assert result.recognized["sleep_schedule"] == {
        "typical_bedtime": "00:20", "typical_wake_time": "06:50",
    }
    assert result.recognized["height_cm"] == 174


def test_frequency_duration_and_days() -> None:
    result = parse_multi_fact_update("4 אימונים בשבוע, 50 דקות, ראשון שני רביעי שישי")
    assert result.recognized["training_days_per_week"] == 4
    assert result.recognized["session_minutes"] == 50
    assert result.weekday_text and "ראשון" in result.weekday_text


def test_unlabeled_numbers_stay_ambiguous() -> None:
    result = parse_multi_fact_update("גובה 174 וגם 88")
    assert result.recognized["height_cm"] == 174
    assert result.ambiguous  # 88 is NOT silently assigned to anything


def test_single_fact_messages_are_not_multi_fact() -> None:
    assert parse_multi_fact_update("174").fact_count == 0
    assert parse_multi_fact_update("גובה 174").fact_count == 1


# ---------------------------------------------------------------------------
# The wrap: extraction during an active flow
# ---------------------------------------------------------------------------


async def _open_question(db: Database, question_id: str) -> None:
    from noam_coach.services import core as core_services

    await core_services.set_flow_state(
        USER_ID, "plan_completion", question_id,
        {"return_to": "menu:smartplan", "plan_type": "workout"},
    )
    await coach_bot.set_pending(USER_ID, question_id)


@pytest.mark.asyncio
async def test_incident_journey_updates_facts_and_keeps_the_flow(db: Database) -> None:
    """Acceptance 1+6: both facts saved, the active wizard stays resumable."""
    await _open_question(db, "q_session_minutes")
    install_multi_fact_updates()

    update = _update("גובה 174 אימונים מזוהים 4")
    consumed = await coach_bot.handle_onboarding_text(update, USER_ID)

    assert consumed is True
    assert await user_model.get_value(db, USER_ID, "height_cm") == 174
    assert await user_model.get_value(db, USER_ID, "training_days_per_week") == 4
    # The response lists what changed (acceptance 5)...
    summary = " ".join(update.effective_message.replies)
    assert "עדכנתי" in summary and "174" in summary
    # ...and the ACTIVE question is still pending + restated (acceptance 6).
    flow = await conversation.get_active_flow(db, USER_ID)
    assert flow.step == "q_session_minutes"
    events = await event_log.list_events(db, USER_ID)
    extraction = [e for e in events if e.entity == "multi_fact_extraction"]
    assert extraction and extraction[0].outcome == "applied"


@pytest.mark.asyncio
async def test_active_question_answer_still_completes_the_question(db: Database) -> None:
    """Acceptance 3: the message contains the active answer AND extra facts —
    the active question completes through its canonical path."""
    await _open_question(db, "q_session_minutes")
    install_multi_fact_updates()

    update = _update("50 דקות אימון וגובה 174")
    consumed = await coach_bot.handle_onboarding_text(update, USER_ID)

    assert consumed is True
    assert await user_model.get_value(db, USER_ID, "height_cm") == 174
    assert await user_model.get_value(db, USER_ID, "session_minutes") == 50
    flow = await conversation.get_active_flow(db, USER_ID)
    assert flow.step != "q_session_minutes"  # advanced past the question


@pytest.mark.asyncio
async def test_invalid_range_is_rejected_with_correction(db: Database) -> None:
    """Acceptance 4+7: out-of-range values are refused visibly, not saved."""
    await _open_question(db, "q_session_minutes")
    install_multi_fact_updates()

    update = _update("גובה 300 ומשקל 90")
    consumed = await coach_bot.handle_onboarding_text(update, USER_ID)

    assert consumed is True
    assert await user_model.get_value(db, USER_ID, "height_cm") is None
    assert await user_model.get_value(db, USER_ID, "weight_kg") == 90
    correction = " ".join(update.effective_message.replies)
    assert "בין" in correction  # the range correction is user-visible


@pytest.mark.asyncio
async def test_single_fact_answer_keeps_the_original_path(db: Database) -> None:
    """A plain single answer must NOT be hijacked by the multi-fact wrap."""
    await _open_question(db, "q_session_minutes")
    install_multi_fact_updates()

    update = _update("45")
    consumed = await coach_bot.handle_onboarding_text(update, USER_ID)
    assert consumed is True
    assert await user_model.get_value(db, USER_ID, "session_minutes") == 45


@pytest.mark.asyncio
async def test_ambiguous_number_triggers_targeted_clarification(db: Database) -> None:
    await _open_question(db, "q_session_minutes")
    install_multi_fact_updates()

    update = _update("גובה 174 משקל 101.8 וגם 88")
    await coach_bot.handle_onboarding_text(update, USER_ID)

    assert await user_model.get_value(db, USER_ID, "height_cm") == 174
    assert await user_model.get_value(db, USER_ID, "weight_kg") == 101.8
    replies = " ".join(update.effective_message.replies)
    assert "לא הייתי בטוח" in replies


@pytest.mark.asyncio
async def test_weekday_availability_is_saved_canonically(db: Database) -> None:
    await _open_question(db, "q_session_minutes")
    install_multi_fact_updates()

    update = _update("4 אימונים בשבוע, ראשון שלישי חמישי שבת")
    await coach_bot.handle_onboarding_text(update, USER_ID)

    # The canonical availability writer persisted the explicit days...
    days = await user_model.get_value(db, USER_ID, "active_training_days")
    assert days and len(days) == 4
    assert await user_model.get_value(db, USER_ID, "preferred_training_days") == days
