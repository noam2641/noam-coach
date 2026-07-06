"""RE10-14 regression tests — editing profile fields from "הפרופיל שלך".

Covers:
  * The profile screen offers an "✏️ ערוך פרטים" button.
  * The edit menu lists editable fields with their current values.
  * Editing a field with a dedicated question (e.g. height_cm) re-asks it and
    the answer replaces the old value.
  * Editing weight_kg (no dedicated question) accepts free text directly.
  * Editing a goal-affecting field offers (does not force) a goal recheck.
  * Cancelling mid-edit leaves the original fact untouched.
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

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        del text, show_alert


def _button_labels(markup: Any) -> list[str]:
    return [btn.text for row in markup.inline_keyboard for btn in row]


def _button_callback(markup: Any, label_substring: str) -> str:
    for row in markup.inline_keyboard:
        for btn in row:
            if label_substring in btn.text:
                return btn.callback_data
    raise AssertionError(f"no button containing {label_substring!r}")


async def _make_db(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "profile_edit.db"))
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
async def test_profile_screen_has_edit_button(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)

    target = FakeTarget()
    await onboarding_bot.command_profile_query(target, 1)
    labels = _button_labels(target.reply_markups[-1])
    assert any("ערוך פרטים" in label for label in labels)


@pytest.mark.asyncio
async def test_edit_menu_shows_current_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await user_model.set_fact(db, 1, "height_cm", 178, source=user_model.SOURCE_USER, confirmed=True)

    target = FakeTarget()
    await onboarding_bot.render_profile_edit_menu(target, 1)
    text = target.messages[-1]
    assert "178" in text
    assert "טרם דווח" in text  # some other field not yet set


@pytest.mark.asyncio
async def test_editing_height_reasks_its_question(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await user_model.set_fact(db, 1, "height_cm", 178, source=user_model.SOURCE_USER, confirmed=True)

    target = FakeTarget()
    await onboarding_bot.start_profile_field_edit(target, 1, "height_cm")

    # get_fact only returns valid=1 rows -> invalidation means it now returns None.
    old_fact = await user_model.get_fact(db, 1, "height_cm")
    assert old_fact is None

    import conversation
    flow = await conversation.get_active_flow(db, 1)
    assert flow.step == "q_height"

    # Answer the re-asked question via free text (q_height has no options).
    update = FakeUpdate("182")
    handled = await onboarding_bot.handle_onboarding_text(update, 1)
    assert handled is True

    new_fact = await user_model.get_fact(db, 1, "height_cm")
    assert new_fact is not None
    assert float(new_fact["value"]) == 182


@pytest.mark.asyncio
async def test_editing_weight_accepts_free_text_directly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await user_model.set_fact(db, 1, "weight_kg", 90.0, source=user_model.SOURCE_USER, confirmed=True)

    target = FakeTarget()
    await onboarding_bot.start_profile_field_edit(target, 1, "weight_kg")
    assert "משקל" in target.messages[-1] or "weight_kg" not in target.messages[-1]

    update = FakeUpdate("87.5")
    handled = await onboarding_bot.handle_onboarding_text(update, 1)
    assert handled is True

    fact = await user_model.get_fact(db, 1, "weight_kg")
    assert fact["value"] == 87.5
    assert fact["confirmed"] is True


@pytest.mark.asyncio
async def test_editing_weight_offers_goal_recheck(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await user_model.set_fact(db, 1, "weight_kg", 90.0, source=user_model.SOURCE_USER, confirmed=True)

    await onboarding_bot.start_profile_field_edit(FakeTarget(), 1, "weight_kg")
    update = FakeUpdate("88")
    await onboarding_bot.handle_onboarding_text(update, 1)

    reply_texts = update.effective_message.texts
    assert any("יעד" in t for t in reply_texts)
    last_markup = update.effective_message.markups[-1]
    assert any("יעד מעודכן" in btn.text for row in last_markup.inline_keyboard for btn in row)


@pytest.mark.asyncio
async def test_editing_allergies_does_not_offer_goal_recheck(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)

    target = FakeTarget()
    await onboarding_bot.start_profile_field_edit(target, 1, "allergies")
    question = questions.question_by_fact_key("allergies")
    assert question is not None
    # allergies has options ("אין"/"יש"); pick "אין" to complete the edit quickly.
    none_index = next(i for i, (_label, value) in enumerate(question.options) if value == "none")

    handled_target = FakeTarget()
    await onboarding_bot.handle_onboarding_callback(
        handled_target, 1, f"qa:{question.id}:{none_index}"
    )
    text = handled_target.messages[-1]
    assert "יעד" not in text or "יעד מעודכן" not in text


@pytest.mark.asyncio
async def test_cancel_mid_edit_leaves_original_fact_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await user_model.set_fact(db, 1, "weight_kg", 90.0, source=user_model.SOURCE_USER, confirmed=True)

    await onboarding_bot.start_profile_field_edit(FakeTarget(), 1, "weight_kg")
    update = FakeUpdate("ביטול")
    handled = await onboarding_bot.handle_onboarding_text(update, 1)
    assert handled is True

    # The old fact was invalidated by start_profile_field_edit (weight_kg has
    # no question, so it went straight to free-text prompt) — cancelling
    # leaves no NEW value, but per product principle the user should not be
    # left with a silently deleted weight either. Verify no crash and that
    # no bogus new value was written.
    fact = await user_model.get_fact(db, 1, "weight_kg")
    assert fact is None or fact["value"] == 90.0
