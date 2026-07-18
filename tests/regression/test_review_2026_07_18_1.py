"""Regression coverage for review 2026-07-18_1 approved findings.

Each test cites the finding it locks in. Evidence lives in
reviews/2026-07-18_1/ (events referenced by id in findings.json).
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

USER_ID = 1


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
        self.from_user = type("U", (), {"id": USER_ID})()
        self.data: str | None = None

    async def edit_message_text(self, text: str, reply_markup: Any = None, parse_mode: str | None = None) -> None:
        del parse_mode
        self.messages.append(text)
        self.reply_markups.append(reply_markup)

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        del text, show_alert


async def _make_db(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "review_r1.db"))
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


async def _seed_single_pending_item(db: Database) -> None:
    """The incident shape: sleep_schedule is the ONLY unconfirmed item."""
    await user_model.set_fact(
        db, USER_ID, "sleep_schedule",
        {"typical_bedtime": "23:15", "typical_wake_time": "06:45"},
        kind=user_model.KIND_ESTIMATE, source=user_model.SOURCE_DERIVED, confirmed=False,
    )


async def _press_skip_item(db: Database) -> FakeTarget:
    target = FakeTarget()
    target.data = "health:skip_item"
    handled = await callback_menu_bot.handle_menu_callback(target, USER_ID, "health:skip_item")
    assert handled is True
    return target


# ---------------------------------------------------------------------------
# F-01 — incident-specific: the exact sleep_schedule loop from events 66/75/84
# ---------------------------------------------------------------------------


async def test_f01_second_skip_on_last_item_finishes_the_wizard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await _seed_single_pending_item(db)

    first = FakeTarget()
    assert await health_jobs.ask_next_health_confirm_step(first, USER_ID) is True
    assert "שינה" in first.messages[-1]

    # First skip: the item is deferred and returns for one explicit final
    # pass — but NEVER as a byte-identical screen (the return is announced).
    second = await _press_skip_item(db)
    assert second.messages, "first skip must re-render, not go silent"
    assert "חוזר לפריט שדחית" in second.messages[-1]
    assert second.messages[-1] != first.messages[-1]

    # Second skip: terminal — the wizard finishes instead of looping.
    third = await _press_skip_item(db)
    assert third.messages
    final_text = third.messages[-1]
    assert "חוזר לפריט שדחית" not in final_text
    assert "לא זיהיתי דפוס שינה" not in final_text  # the question is GONE
    # The fact was neither applied nor invalidated — still pending for later.
    fact = await user_model.get_fact(db, USER_ID, "sleep_schedule")
    assert fact is not None and not fact.get("confirmed")


async def test_f01_skip_never_rerenders_identical_screen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The user-visible invariant behind the incident: pressing 'דלג כרגע'
    must always change the screen."""
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await _seed_single_pending_item(db)

    target = FakeTarget()
    await health_jobs.ask_next_health_confirm_step(target, USER_ID)
    seen = [target.messages[-1]]
    for _ in range(3):
        pressed = await _press_skip_item(db)
        if not pressed.messages:
            break
        text = pressed.messages[-1]
        assert text not in seen, "identical wizard screen re-rendered after skip"
        seen.append(text)


# ---------------------------------------------------------------------------
# F-01 — generic: deferral ordering and terminality across MULTIPLE items
# ---------------------------------------------------------------------------


async def test_f01_generic_deferred_item_returns_after_others_then_terminates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await user_model.set_fact(
        db, USER_ID, "weight_kg", 58.0,
        kind=user_model.KIND_FACT, source=user_model.SOURCE_APPLE_HEALTH, confirmed=False,
    )
    await _seed_single_pending_item(db)

    target = FakeTarget()
    await health_jobs.ask_next_health_confirm_step(target, USER_ID)
    first_question = target.messages[-1]

    # Skip the first item: with another pending item available, the wizard
    # must advance to it (not re-ask, not finish).
    after_skip = await _press_skip_item(db)
    assert after_skip.messages
    assert after_skip.messages[-1] != first_question

    # Deferred bookkeeping is explicit and the skipped-terminal list is empty.
    assert await health_jobs._wizard_deferred_steps(USER_ID) != []
    assert await health_jobs._wizard_skipped_steps(USER_ID) == []


async def test_f01_generic_every_item_skipped_twice_ends_wizard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Terminality holds for the whole wizard, not one item: two skips per
    item always terminate, regardless of item count or kind."""
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await user_model.set_fact(
        db, USER_ID, "weight_kg", 58.0,
        kind=user_model.KIND_FACT, source=user_model.SOURCE_APPLE_HEALTH, confirmed=False,
    )
    await _seed_single_pending_item(db)

    target = FakeTarget()
    assert await health_jobs.ask_next_health_confirm_step(target, USER_ID) is True

    for _ in range(8):  # 2 items × 2 skips each = 4 presses; bound the loop
        await _press_skip_item(db)
        state = await onboarding_bot.get_flow_state(USER_ID, health_jobs.HEALTH_CONFIRM_FLOW)
        if not state:
            break
    state = await onboarding_bot.get_flow_state(USER_ID, health_jobs.HEALTH_CONFIRM_FLOW)
    assert not state, "wizard flow state must be cleared after finite skips"
