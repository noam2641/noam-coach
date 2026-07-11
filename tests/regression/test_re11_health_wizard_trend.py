"""RE11 regression tests — health-confirm wizard UX collapse + trend proposal.

Covers:
  * Typing a correction directly at the confirm-step prompt works without
    first tapping "ציין אחרת" (that button is removed; pending is set to the
    edit key from the start, mirroring the onboarding free-text-fallback fix).
  * "✅ אשר" still works as the one-tap fast path.
  * When Health data shows a meaningful recent-vs-overall training frequency
    shift, the wizard shows a trend proposal with choice buttons instead of
    the plain confirm prompt, and a trend button records both workout_pattern
    and training_days_per_week.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import coach_bot
import user_model
from db import Database
from helpers import utc_now
from noam_coach.bot import callback_menu as callback_menu_bot
from noam_coach.bot import onboarding as onboarding_bot
from noam_coach.services import core as core_services
from noam_coach.services import health_jobs


class FakeMessage:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.markups: list[Any] = []

    async def reply_text(self, text: str, reply_markup: Any = None, parse_mode: str | None = None) -> None:
        del parse_mode
        self.texts.append(text)
        self.markups.append(reply_markup)


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


class FakeUpdate:
    def __init__(self, text: str) -> None:
        self.effective_message = FakeMessage()
        self.effective_message.text = text


async def _make_db(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "health_wizard_trend.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'Test',NULL,?)",
        (utc_now(),),
    )
    return db


def _patch_db(monkeypatch: pytest.MonkeyPatch, db: Database) -> None:
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(health_jobs, "DB", db)
    monkeypatch.setattr(onboarding_bot, "DB", db)
    monkeypatch.setattr(core_services, "DB", db)
    monkeypatch.setattr(callback_menu_bot, "DB", db)


@pytest.mark.asyncio
async def test_typing_correction_directly_works_without_edit_button(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await user_model.set_fact(
        db, 1, "weight_kg", 101.8,
        kind=user_model.KIND_FACT, source=user_model.SOURCE_APPLE_HEALTH, confirmed=False,
    )

    target = FakeTarget()
    await health_jobs.ask_next_health_confirm_step(target, 1)
    # No "ציין אחרת" button anymore — just אשר + skip.
    labels = [btn.text for row in target.reply_markups[-1].inline_keyboard for btn in row]
    assert not any("ציין אחרת" in label for label in labels)

    update = FakeUpdate("95.5")
    handled = await onboarding_bot.handle_onboarding_text(update, 1)
    assert handled is True
    fact = await user_model.get_fact(db, 1, "weight_kg")
    assert fact["value"] == 95.5
    assert fact["source"] == user_model.SOURCE_USER
    assert fact["confirmed"] is True


@pytest.mark.asyncio
async def test_confirm_button_still_works_as_fast_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await user_model.set_fact(
        db, 1, "weight_kg", 101.8,
        kind=user_model.KIND_FACT, source=user_model.SOURCE_APPLE_HEALTH, confirmed=False,
    )

    target = FakeTarget()
    await health_jobs.ask_next_health_confirm_step(target, 1)
    handled = await callback_menu_bot.handle_menu_callback(target, 1, "health:confirm:weight_kg")
    assert handled is True
    fact = await user_model.get_fact(db, 1, "weight_kg")
    assert fact["confirmed"] is True
    assert fact["value"] == 101.8  # unchanged — confirm doesn't alter the value


async def _seed_workout_pattern_with_trend(db: Database) -> None:
    await user_model.set_fact(
        db, 1, "workout_pattern",
        {
            "weekly_frequency": 2.5,
            "sessions_sampled": 16,
            "recent_weekly_frequency": 2.0,
            "recent_sessions_sampled": 4,
            "typical_hour": "18:30",
        },
        kind=user_model.KIND_ESTIMATE, source=user_model.SOURCE_DERIVED, confirmed=False,
    )


@pytest.mark.asyncio
async def test_wizard_shows_trend_proposal_for_workout_pattern(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await _seed_workout_pattern_with_trend(db)

    target = FakeTarget()
    started = await health_jobs.ask_next_health_confirm_step(target, 1)
    assert started is True
    text = target.messages[-1]
    assert "2" in text and "3" in text  # recent=2, recommend 3-4
    labels = [btn.text for row in target.reply_markups[-1].inline_keyboard for btn in row]
    assert any("2" in label for label in labels)
    assert any("3" in label for label in labels)


@pytest.mark.asyncio
async def test_trend_button_records_frequency_and_advances(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await _seed_workout_pattern_with_trend(db)

    target = FakeTarget()
    await health_jobs.ask_next_health_confirm_step(target, 1)

    handled = await callback_menu_bot.handle_menu_callback(
        target, 1, "health:confirm:workout_pattern:trend:3"
    )
    assert handled is True

    training_days = await user_model.get_fact(db, 1, "training_days_per_week")
    assert training_days is not None
    assert training_days["value"] == 3.0
    assert training_days["confirmed"] is True

    workout_pattern = await user_model.get_fact(db, 1, "workout_pattern")
    assert workout_pattern["value"]["weekly_frequency"] == 3.0


@pytest.mark.asyncio
async def test_no_trend_proposal_when_recent_data_insufficient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await user_model.set_fact(
        db, 1, "workout_pattern",
        {"weekly_frequency": 3.0, "sessions_sampled": 12, "typical_hour": "18:30"},
        kind=user_model.KIND_ESTIMATE, source=user_model.SOURCE_DERIVED, confirmed=False,
    )

    target = FakeTarget()
    await health_jobs.ask_next_health_confirm_step(target, 1)
    text = target.messages[-1]
    assert "אימונים בשבוע" in text  # falls back to the plain confirm prompt
    labels = [btn.text for row in target.reply_markups[-1].inline_keyboard for btn in row]
    assert any("אשר" in label for label in labels)
