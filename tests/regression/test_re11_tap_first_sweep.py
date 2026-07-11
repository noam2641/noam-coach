"""RE11 regression tests — sweep fixes for the "tap before you can type"
confirm/edit anti-pattern, beyond the safety/allergy/equipment questions and
the health-import wizard (already covered by other RE11 test files).

Covers:
  * show_onboarding_basics: a typed correction ("המשקל 90") works directly at
    the basics-summary screen, without first tapping "יש מה לתקן" (that
    button is removed since it's now redundant).
  * ask_next_question's "כבר יש לי: X — זה נכון?" screen: for a
    free_text_fallback question (e.g. allergies) with an existing value, a
    typed correction works directly, and the "לא, אעדכן" button is not shown
    since it would be redundant.
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


class FakeTarget:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.reply_markups: list[Any] = []

    async def edit_message_text(self, text: str, reply_markup: Any = None, parse_mode: str | None = None) -> None:
        del parse_mode
        self.messages.append(text)
        self.reply_markups.append(reply_markup)

    async def reply_text(self, text: str, reply_markup: Any = None, parse_mode: str | None = None) -> None:
        del parse_mode
        self.messages.append(text)
        self.reply_markups.append(reply_markup)

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        del text, show_alert


def _button_labels(markup: Any) -> list[str]:
    return [btn.text for row in markup.inline_keyboard for btn in row]


async def _make_db(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "tap_first_sweep.db"))
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


@pytest.mark.asyncio
async def test_basics_screen_has_no_separate_fix_button(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await user_model.set_fact(
        db, 1, "weight_kg", 101.8,
        kind=user_model.KIND_FACT, source=user_model.SOURCE_APPLE_HEALTH, confirmed=False,
    )

    target = FakeTarget()
    await onboarding_bot.show_onboarding_basics(target, 1)
    labels = _button_labels(target.reply_markups[-1])
    assert not any("יש מה לתקן" in label for label in labels)
    assert any("הכול נכון" in label for label in labels)


@pytest.mark.asyncio
async def test_basics_screen_shows_profile_audit_statuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await user_model.set_fact(
        db, 1, "weight_kg", 101.8,
        kind=user_model.KIND_FACT, source=user_model.SOURCE_APPLE_HEALTH, confirmed=False,
    )

    target = FakeTarget()
    await onboarding_bot.show_onboarding_basics(target, 1)
    text = target.messages[-1]

    assert "אישור נתוני בסיס" in text
    assert "דורש אישור" in text
    assert "Apple Health" in text
    assert "חסר" in text


@pytest.mark.asyncio
async def test_typing_correction_directly_at_basics_screen_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await user_model.set_fact(
        db, 1, "weight_kg", 101.8,
        kind=user_model.KIND_FACT, source=user_model.SOURCE_APPLE_HEALTH, confirmed=False,
    )

    target = FakeTarget()
    await onboarding_bot.show_onboarding_basics(target, 1)

    update = FakeUpdate("המשקל 90")
    handled = await onboarding_bot.handle_onboarding_text(update, 1)
    assert handled is True

    fact = await user_model.get_fact(db, 1, "weight_kg")
    assert fact is not None
    assert fact["value"] == 90.0
    assert fact["source"] == user_model.SOURCE_USER


@pytest.mark.asyncio
async def test_confirm_existing_pain_shows_no_update_button_and_accepts_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unified limitation question is free_text_fallback — the "already have"
    screen must not force a tap on "לא, אעדכן" before typing works."""
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await user_model.set_fact(
        db, 1, "active_pain", {"location": "ברך", "status": "active"},
        kind=user_model.KIND_FACT, source=user_model.SOURCE_APPLE_HEALTH, confirmed=False,
    )

    target = FakeTarget()
    shown = await onboarding_bot.ask_next_question(target, 1)
    assert shown is True
    text = target.messages[-1]
    markup = target.reply_markups[-1]
    labels = _button_labels(markup)
    assert "כבר יש לי" in text
    assert not any("אעדכן" in label for label in labels)
    assert any("נכון" in label for label in labels)

    update = FakeUpdate("כתף שמאל")
    handled = await onboarding_bot.handle_onboarding_text(update, 1)
    assert handled is True
    fact = await user_model.get_fact(db, 1, "training_limitations")
    assert fact is not None
    assert fact["value"] == "כתף שמאל"
    assert fact["source"] == user_model.SOURCE_USER


@pytest.mark.asyncio
async def test_confirm_existing_plain_multichoice_question_still_forces_button(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """q_sex has no free-text option at all — the update button must stay,
    since typing a correction there would be genuinely ambiguous."""
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await user_model.set_fact(
        db, 1, "sex", "male",
        kind=user_model.KIND_FACT, source=user_model.SOURCE_APPLE_HEALTH, confirmed=False,
    )

    # Force q_sex specifically, isolating the "already have" render logic
    # from the priority scheduler's question-ordering decisions.
    sex_question = questions.question_by_id("q_sex")

    async def fake_next_question(*_args: Any, **_kwargs: Any) -> Any:
        return sex_question

    monkeypatch.setattr(questions, "next_question", fake_next_question)

    target = FakeTarget()
    shown = await onboarding_bot.ask_next_question(target, 1)
    assert shown is True
    markup = target.reply_markups[-1]
    labels = _button_labels(markup)
    assert any("אעדכן" in label for label in labels)
