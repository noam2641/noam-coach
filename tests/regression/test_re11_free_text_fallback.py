"""RE11 regression tests — collapsed "יש/אין" question UX.

Covers the product requirement: for the unified safety limitation question,
q_allergies and q_equipment, the user must be able to type a free-text answer
directly at the initial question prompt (no forced "יש"/"אחר" tap first), while
a single "אין" (or a genuine multi-choice, for equipment) button remains for
"nothing to report".
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


class FakeMessage:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.markups: list[Any] = []
        self.text = ""

    async def reply_text(self, text: str, reply_markup: Any = None, parse_mode: str | None = None) -> None:
        del parse_mode
        self.texts.append(text)
        self.markups.append(reply_markup)


class FakeUpdate:
    def __init__(self, text: str) -> None:
        self.effective_message = FakeMessage()
        self.effective_message.text = text


async def _make_db(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "free_text_fallback.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'Test',NULL,?)",
        (utc_now(),),
    )
    return db


def _patch_db(monkeypatch: pytest.MonkeyPatch, db: Database) -> None:
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(onboarding_bot, "DB", db)
    monkeypatch.setattr(core_services, "DB", db)


def test_target_questions_have_single_none_button_and_free_text_fallback() -> None:
    for qid, fact_key in (
        ("safety_training_limitations", "training_limitations"),
        ("q_allergies", "allergies"),
    ):
        q = questions.question_by_id(qid)
        assert q is not None
        assert q.fact_key == fact_key
        assert q.free_text_fallback is True
        assert [value for _label, value in q.options] == ["none"]

    equipment = questions.question_by_id("q_equipment")
    assert equipment is not None
    assert equipment.free_text_fallback is True
    values = [value for _label, value in equipment.options]
    assert "custom" not in values  # no more forced "אחר" tap before typing


@pytest.mark.asyncio
async def test_typing_pain_location_directly_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await onboarding_bot.set_pending(1, "safety_training_limitations")

    update = FakeUpdate("ברך ימין")
    handled = await onboarding_bot.handle_onboarding_text(update, 1)
    assert handled is True

    fact = await user_model.get_fact(db, 1, "training_limitations")
    assert fact is not None
    assert fact["value"] == "ברך ימין"
    assert fact["confirmed"] is True


@pytest.mark.asyncio
async def test_typing_allergy_directly_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await onboarding_bot.set_pending(1, "q_allergies")

    update = FakeUpdate("בוטנים")
    handled = await onboarding_bot.handle_onboarding_text(update, 1)
    assert handled is True
    # "בוטנים" parses as a dietary item -> classification buttons, not a
    # silently-dropped answer.
    text = update.effective_message.texts[-1]
    assert "בוטנים" in text


@pytest.mark.asyncio
async def test_typing_equipment_directly_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await onboarding_bot.set_pending(1, "q_equipment")

    update = FakeUpdate("מוט, משקולות, מתח")
    handled = await onboarding_bot.handle_onboarding_text(update, 1)
    assert handled is True

    fact = await user_model.get_fact(db, 1, "equipment")
    assert fact is not None
    assert fact["value"] == "מוט, משקולות, מתח"
    assert fact["confirmed"] is True


@pytest.mark.asyncio
async def test_none_button_still_works_for_pain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The single "אין" button (index 0) must still work via the qa: callback."""
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)

    class FakeTarget:
        def __init__(self) -> None:
            self.messages: list[str] = []
            self.reply_markups: list[Any] = []
            self.message = FakeMessage()

        async def edit_message_text(self, text: str, reply_markup: Any = None, parse_mode: str | None = None) -> None:
            del parse_mode
            self.messages.append(text)
            self.reply_markups.append(reply_markup)

        async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
            del text, show_alert

    await onboarding_bot.set_pending(1, "safety_training_limitations")
    target = FakeTarget()
    await onboarding_bot.handle_onboarding_callback(target, 1, "qa:safety_training_limitations:0")

    fact = await user_model.get_fact(db, 1, "training_limitations")
    assert fact is not None
    assert fact["value"] == "none"


@pytest.mark.asyncio
async def test_skip_callback_marks_question_as_skipped_not_reasked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TASK-02: tapping "דלג" on a question must not leave a plain gap that
    gets asked again on the very next question loop — it's a deliberate skip.
    """
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)

    class FakeTarget:
        def __init__(self) -> None:
            self.messages: list[str] = []
            self.reply_markups: list[Any] = []
            self.message = FakeMessage()

        async def edit_message_text(self, text: str, reply_markup: Any = None, parse_mode: str | None = None) -> None:
            del parse_mode
            self.messages.append(text)
            self.reply_markups.append(reply_markup)

        async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
            del text, show_alert

    await onboarding_bot.set_pending(1, "q_allergies")
    target = FakeTarget()
    await onboarding_bot.handle_onboarding_callback(target, 1, "qa:q_allergies:skip")

    fact = await user_model.get_fact(db, 1, "allergies")
    assert fact is not None
    assert user_model.is_skipped_gap(fact)

    result = await questions.next_question(
        db, 1, context={"planning_nutrition": True},
        pool=[questions.question_by_id("q_allergies")],
    )
    assert result is None
