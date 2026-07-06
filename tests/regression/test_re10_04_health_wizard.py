"""RE10-4 regression tests — per-fact confirmation wizard after Health import.

Covers:
  * ask_next_health_confirm_step walks pending facts in the documented order.
  * Confirming a fact hardens it (confirmed=1) and advances to the next one.
  * "ציין אחרת" (edit) accepts free text and persists it as a user-sourced fact.
  * "skip the rest" and "reaching the end" both finish the wizard and run the
    correct continuation (onboarding vs reconciliation).
  * The old bulk gate (health:activate / pending_import_facts) still works
    for the health:review path — RE10-4 adds the wizard, it does not remove it.
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


async def _make_db(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "health_wizard.db"))
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


async def _seed_pending_import(db: Database) -> None:
    await user_model.set_fact(
        db, 1, "workout_pattern", {"weekly_frequency": 3, "typical_hour": "18:30"},
        kind=user_model.KIND_ESTIMATE, source=user_model.SOURCE_DERIVED, confirmed=False,
    )
    await user_model.set_fact(
        db, 1, "weight_kg", 101.8,
        kind=user_model.KIND_FACT, source=user_model.SOURCE_APPLE_HEALTH, confirmed=False,
    )
    await user_model.set_fact(
        db, 1, "sleep_schedule", {"typical_bedtime": "23:15", "typical_wake_time": "06:45"},
        kind=user_model.KIND_ESTIMATE, source=user_model.SOURCE_DERIVED, confirmed=False,
    )


@pytest.mark.asyncio
async def test_wizard_walks_facts_in_documented_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await _seed_pending_import(db)

    target = FakeTarget()
    started = await health_jobs.ask_next_health_confirm_step(target, 1)
    assert started is True
    assert "אימונים בשבוע" in target.messages[-1]  # workout_pattern first


@pytest.mark.asyncio
async def test_confirming_each_item_separately_and_advancing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """RE12: frequency and hour are confirmed as SEPARATE steps; only after
    the last applicable workout sub-step is the pattern fact hardened."""
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await _seed_pending_import(db)

    target = FakeTarget()
    await health_jobs.ask_next_health_confirm_step(target, 1)
    # Step 1: weekly frequency (detected value shown, uniform phrasing).
    assert "אימונים בשבוע" in target.messages[-1]
    assert "האם לאשר" in target.messages[-1]

    handled = await callback_menu_bot.handle_menu_callback(
        target, 1, "health:confirm:workout_pattern.frequency"
    )
    assert handled is True
    training_days = await user_model.get_fact(db, 1, "training_days_per_week")
    assert training_days["value"] == 3
    assert training_days["confirmed"] is True
    # Pattern is NOT confirmed yet — the hour step is still pending.
    fact = await user_model.get_fact(db, 1, "workout_pattern")
    assert fact["confirmed"] is False
    assert "שעת אימון" in target.messages[-1]

    handled = await callback_menu_bot.handle_menu_callback(
        target, 1, "health:confirm:workout_pattern.hour"
    )
    assert handled is True
    window = await user_model.get_fact(db, 1, "workout_window")
    assert window["value"] == "18:30"
    assert window["confirmed"] is True
    fact = await user_model.get_fact(db, 1, "workout_pattern")
    assert fact["confirmed"] is True
    # Advanced to the next fact (weight_kg), echoing what was just approved.
    assert 'ק"ג' in target.messages[-1]
    assert "אושר" in target.messages[-1]


@pytest.mark.asyncio
async def test_detected_training_days_get_their_own_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RE12: detected training days are surfaced for explicit approval, and
    approving them feeds the plan's weekly_availability."""
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await user_model.set_fact(
        db, 1, "workout_pattern",
        {
            "weekly_frequency": 3,
            "typical_hour": "18:30",
            "common_weekdays": [6, 1, 3],  # Sun, Tue, Thu (Monday-first)
            "weekday_schema": "monday_first_v1",
        },
        kind=user_model.KIND_ESTIMATE, source=user_model.SOURCE_DERIVED, confirmed=False,
    )

    target = FakeTarget()
    await health_jobs.ask_next_health_confirm_step(target, 1)
    await callback_menu_bot.handle_menu_callback(
        target, 1, "health:confirm:workout_pattern.frequency"
    )
    # Step 2: the detected days are shown by name and offered for approval.
    days_prompt = target.messages[-1]
    assert "ימי אימון" in days_prompt
    assert "ראשון" in days_prompt and "שלישי" in days_prompt and "חמישי" in days_prompt

    handled = await callback_menu_bot.handle_menu_callback(
        target, 1, "health:confirm:workout_pattern.days"
    )
    assert handled is True
    availability = await user_model.get_fact(db, 1, "weekly_availability")
    assert availability["confirmed"] is True
    weekdays = sorted(slot["weekday"] for slot in availability["value"])
    assert weekdays == [1, 3, 6]

    # Hour step still follows; after it the pattern hardens.
    await callback_menu_bot.handle_menu_callback(
        target, 1, "health:confirm:workout_pattern.hour"
    )
    fact = await user_model.get_fact(db, 1, "workout_pattern")
    assert fact["confirmed"] is True


@pytest.mark.asyncio
async def test_edit_persists_user_value_and_advances(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await _seed_pending_import(db)

    target = FakeTarget()
    await health_jobs.ask_next_health_confirm_step(target, 1)

    class FakeUpdate:
        effective_message = FakeMessage()
        effective_message.text = "4"  # type: ignore[attr-defined]

    handled = await onboarding_bot.handle_onboarding_text(FakeUpdate(), 1)
    assert handled is True
    # RE12: a typed number at the frequency step corrects the frequency INSIDE
    # the pattern (not clobbering the whole dict) and records the plan fact.
    fact = await user_model.get_fact(db, 1, "workout_pattern")
    assert fact["value"]["weekly_frequency"] == 4.0
    assert fact["value"]["typical_hour"] == "18:30"
    training_days = await user_model.get_fact(db, 1, "training_days_per_week")
    assert training_days["value"] == 4
    assert training_days["source"] == user_model.SOURCE_USER
    assert training_days["confirmed"] is True


@pytest.mark.asyncio
async def test_edit_training_days_by_hebrew_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await user_model.set_fact(
        db, 1, "workout_pattern",
        {
            "weekly_frequency": 3,
            "typical_hour": "18:30",
            "common_weekdays": [0, 2],
            "weekday_schema": "monday_first_v1",
        },
        kind=user_model.KIND_ESTIMATE, source=user_model.SOURCE_DERIVED, confirmed=False,
    )

    target = FakeTarget()
    await health_jobs.ask_next_health_confirm_step(target, 1)
    await callback_menu_bot.handle_menu_callback(
        target, 1, "health:confirm:workout_pattern.frequency"
    )
    assert "ימי אימון" in target.messages[-1]

    class FakeUpdate:
        effective_message = FakeMessage()
        effective_message.text = "ראשון, שלישי, חמישי"  # type: ignore[attr-defined]

    handled = await onboarding_bot.handle_onboarding_text(FakeUpdate(), 1)
    assert handled is True
    availability = await user_model.get_fact(db, 1, "weekly_availability")
    assert availability["confirmed"] is True
    weekdays = sorted(slot["weekday"] for slot in availability["value"])
    assert weekdays == [1, 3, 6]  # Tue, Thu, Sun in Monday-first indices
    fact = await user_model.get_fact(db, 1, "workout_pattern")
    assert sorted(fact["value"]["common_weekdays"]) == [1, 3, 6]


@pytest.mark.asyncio
async def test_skip_wizard_finishes_and_runs_reconciliation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await _seed_pending_import(db)
    await core_services.set_flow_state(
        1, health_jobs.HEALTH_POST_WIZARD_FLOW, "reconciliation", {"summary_text": "<b>סיכום</b>"}
    )

    target = FakeTarget()
    handled = await callback_menu_bot.handle_menu_callback(target, 1, "health:skip_wizard")
    assert handled is True
    assert "סיכום הייבוא" in target.messages[-1] or "סיכום" in target.messages[-1]


@pytest.mark.asyncio
async def test_finish_wizard_runs_onboarding_when_requested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)

    calls: list[str] = []

    async def fake_show_onboarding_basics(_message: Any, _user_id: int) -> None:
        calls.append("onboarding")

    monkeypatch.setattr(onboarding_bot, "show_onboarding_basics", fake_show_onboarding_basics)

    await core_services.set_flow_state(1, health_jobs.HEALTH_POST_WIZARD_FLOW, "onboarding", {"summary_text": "x"})
    target = FakeTarget()
    await health_jobs.finish_health_confirm_wizard(target, 1)
    assert calls == ["onboarding"]


@pytest.mark.asyncio
async def test_bulk_gate_still_available_via_health_review(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """RE10-4 adds the wizard; it must not remove the existing bulk activation path."""
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await _seed_pending_import(db)

    pending = await health_jobs.pending_import_facts(1)
    assert len(pending) == 3
    activated = await health_jobs.activate_imported_health_facts(1)
    assert activated == 3
