"""TASK-63 — plan-completion question deduplication and continuation.

The invariant: after a successful answer save, the next rendered question
must not have the same fact_key unless a structured clarification for that
same answer is still unresolved — and that clarification is itself a real,
resumable pending question.

Required trace exercised end-to-end with the REAL protected handlers:
question A answered → interrupt (restart) → resume → next render is
question B; question A is never asked again.
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
from noam_coach.services.question_dedup import (
    classify_type_from_text,
    install_plan_question_dedup,
    is_none_answer,
    uninstall_plan_question_dedup,
)

USER_ID = 1


class FakeMessage:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.message_id = 42
        self.chat = SimpleNamespace(id=USER_ID)
        self.replies: list[str] = []
        self.markups: list[Any] = []

    async def reply_text(self, text: str, reply_markup: Any = None, parse_mode: Any = None) -> "FakeMessage":
        self.replies.append(text)
        self.markups.append(reply_markup)
        return self


def _update(text: str) -> Any:
    return SimpleNamespace(
        effective_message=FakeMessage(text),
        effective_user=SimpleNamespace(id=USER_ID),
        effective_chat=SimpleNamespace(id=USER_ID),
    )


@pytest.fixture
async def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Database:
    database = Database(str(tmp_path / "task63.db"))
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
    uninstall_plan_question_dedup()
    reset_mode()
    reset_observability_health()


async def _open_allergy_question(db: Database) -> None:
    """Arm the exact incident state: plan-completion pending on allergies."""
    from noam_coach.services import core as core_services

    await core_services.set_flow_state(
        USER_ID, "plan_completion", "q_allergies",
        {"return_to": "menu:smartplan", "plan_type": "nutrition"},
    )
    await coach_bot.set_pending(USER_ID, "q_allergies")


async def _allergy_fact(db: Database) -> Any:
    return await user_model.get_fact(db, USER_ID, "allergies")


def test_none_answer_detection() -> None:
    for text in ("אין", "אין אלרגיות", "אין לי רגישויות", "שום דבר", "כלום", "אוכל הכל"):
        assert is_none_answer(text), text
    for text in ("אגוזים", "אין לי מושג", "בוטנים"):
        assert not is_none_answer(text), text


def test_classification_keywords() -> None:
    assert classify_type_from_text("אלרגיה מאובחנת") == "allergy"
    assert classify_type_from_text("רגישות") == "sensitivity"
    assert classify_type_from_text("סתם העדפה") == "preference"
    assert classify_type_from_text("זה עושה לי נפיחות") == "intolerance"
    assert classify_type_from_text("טעות שלי") == "cancel"
    assert classify_type_from_text("אגוזים") is None


@pytest.mark.asyncio
async def test_none_answer_resolves_and_advances(db: Database) -> None:
    """Acceptance 3: 'אין אלרגיות' marks the fact complete and advances —
    never asks how to classify the phrase 'אין אלרגיות'."""
    await _open_allergy_question(db)
    install_plan_question_dedup()

    update = _update("אין אלרגיות")
    consumed = await coach_bot.handle_onboarding_text(update, USER_ID)

    assert consumed is True
    fact = await _allergy_fact(db)
    assert fact["value"] == "none" and fact["confirmed"]
    assert not any("איך להתייחס" in reply for reply in update.effective_message.replies)
    events = await event_log.list_events(db, USER_ID)
    outcomes = [e.outcome for e in events if e.entity == "question_dedup"]
    assert "none_answer" in outcomes


@pytest.mark.asyncio
async def test_answer_is_persisted_before_classification(db: Database) -> None:
    """Acceptance 1: 'אגוזים' records the answer immediately; the top-level
    question can never be re-rendered even if classification is abandoned."""
    await _open_allergy_question(db)
    install_plan_question_dedup()

    update = _update("אגוזים")
    consumed = await coach_bot.handle_onboarding_text(update, USER_ID)

    assert consumed is True
    # Classification was asked ONCE...
    assert any("איך להתייחס לאגוזים" in reply for reply in update.effective_message.replies)
    # ...but the answer is already persisted (held fail-closed) and the
    # allergies gap is resolved, so the wizard cannot re-ask question A.
    restrictions = await user_model.get_value(db, USER_ID, "diet_restrictions")
    assert "אגוזים" in str(restrictions)
    fact = await _allergy_fact(db)
    assert fact is not None and fact.get("kind") != user_model.KIND_GAP
    from noam_coach.bot import onboarding as onboarding_bot

    next_question = await onboarding_bot.first_missing_plan_question(USER_ID, "nutrition")
    assert next_question is None or next_question.fact_key != "allergies"
    # The clarification itself is a real pending question.
    flow = await conversation.get_active_flow(db, USER_ID)
    assert str(flow.step).startswith("__diet_classify__:")


@pytest.mark.asyncio
async def test_typed_classification_resolves_through_the_canonical_handler(
    db: Database,
) -> None:
    """Acceptance 2: answering the classification by TEXT ('אלרגיה') works,
    moves the item to allergies, and the flow continues."""
    await _open_allergy_question(db)
    install_plan_question_dedup()

    await coach_bot.handle_onboarding_text(_update("אגוזים"), USER_ID)
    update = _update("אלרגיה")
    consumed = await coach_bot.handle_onboarding_text(update, USER_ID)

    assert consumed is True
    allergies = await user_model.get_value(db, USER_ID, "allergies")
    assert "אגוזים" in str(allergies)
    restrictions = await user_model.get_value(db, USER_ID, "diet_restrictions")
    assert "אגוזים" not in str(restrictions)  # moved, not duplicated
    flow = await conversation.get_active_flow(db, USER_ID)
    assert not str(flow.step or "").startswith("__diet_classify__:")
    events = await event_log.list_events(db, USER_ID)
    outcomes = [e.outcome for e in events if e.entity == "question_dedup"]
    assert "classification_text_resolved" in outcomes


@pytest.mark.asyncio
async def test_unrecognized_classification_reprompts_instead_of_dropping(
    db: Database,
) -> None:
    await _open_allergy_question(db)
    install_plan_question_dedup()
    await coach_bot.handle_onboarding_text(_update("אגוזים"), USER_ID)

    update = _update("מה זאת אומרת?")
    consumed = await coach_bot.handle_onboarding_text(update, USER_ID)

    assert consumed is True
    assert any("לא זיהיתי את הסיווג" in reply for reply in update.effective_message.replies)
    flow = await conversation.get_active_flow(db, USER_ID)
    assert str(flow.step).startswith("__diet_classify__:")  # still pending


@pytest.mark.asyncio
async def test_required_trace_interrupt_restart_resume_next_question(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """question A answered → restart → resume → next render is question B."""
    await _open_allergy_question(db)
    install_plan_question_dedup()
    await coach_bot.handle_onboarding_text(_update("אגוזים"), USER_ID)

    # Simulated process restart: caches lost, real startup reload runs.
    coach_bot.PENDING_QUESTION.clear()
    await coach_bot.load_pending_state()
    flow = await conversation.get_active_flow(db, USER_ID)
    assert str(flow.step).startswith("__diet_classify__:")  # survived restart

    # Resume by typing the classification.
    update = _update("רגישות")
    await coach_bot.handle_onboarding_text(update, USER_ID)

    # The wizard continued: the next rendered question is NOT allergies.
    rendered = " ".join(update.effective_message.replies)
    assert "איזה מזונות" not in rendered
    from noam_coach.bot import onboarding as onboarding_bot

    next_question = await onboarding_bot.first_missing_plan_question(USER_ID, "nutrition")
    assert next_question is None or next_question.fact_key != "allergies"
    # And the item was preserved (kept fail-closed in diet_restrictions).
    restrictions = await user_model.get_value(db, USER_ID, "diet_restrictions")
    assert "אגוזים" in str(restrictions)


@pytest.mark.asyncio
async def test_button_classification_still_works_and_clears_the_pending(
    db: Database,
) -> None:
    """Acceptance 6: the button path — qa:diet_type tap resolves the pending
    sub-question through the same canonical handler."""
    await _open_allergy_question(db)
    install_plan_question_dedup()
    await coach_bot.handle_onboarding_text(_update("אגוזים"), USER_ID)

    class FakeQuery:
        def __init__(self) -> None:
            self.message = SimpleNamespace(message_id=7, chat=SimpleNamespace(id=USER_ID))
            self.edits: list[str] = []

        async def edit_message_text(self, text: str, reply_markup: Any = None, parse_mode: Any = None) -> None:
            self.edits.append(text)

        async def answer(self, *a: Any, **k: Any) -> None:
            return None

    await coach_bot.handle_onboarding_callback(FakeQuery(), USER_ID, "qa:diet_type:allergy:אגוזים")

    allergies = await user_model.get_value(db, USER_ID, "allergies")
    assert "אגוזים" in str(allergies)
    flow = await conversation.get_active_flow(db, USER_ID)
    assert not str(flow.step or "").startswith("__diet_classify__:")


@pytest.mark.asyncio
async def test_non_dietary_questions_are_untouched(db: Database) -> None:
    """The wrap must not interfere with other question types."""
    from noam_coach.services import core as core_services

    await core_services.set_flow_state(
        USER_ID, "plan_completion", "q_session_minutes",
        {"return_to": "menu:smartplan", "plan_type": "workout"},
    )
    await coach_bot.set_pending(USER_ID, "q_session_minutes")
    install_plan_question_dedup()

    update = _update("45")
    consumed = await coach_bot.handle_onboarding_text(update, USER_ID)
    assert consumed is True
    value = await user_model.get_value(db, USER_ID, "session_minutes")
    assert value is not None
