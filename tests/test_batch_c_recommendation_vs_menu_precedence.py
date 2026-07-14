"""Batch C (FIX 50): next-meal correction must be interpreted before the
daily-menu editor when both an active daily menu and an active next-meal
recommendation exist and the free text could plausibly match either.

Root cause (verified in code before this fix): meal_text.py::handle_text_message
called try_build_daily_menu_edit_reply() before handle_recommendation_correction(),
even though the surrounding comment always said the correction should be
interpreted first. With an active daily menu, generic text like "קטן יותר"
could be silently applied to the menu's snack slot instead of the next-meal
card the user was actually looking at.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import coach_bot
from db import Database
from helpers import utc_now
from noam_coach.bot import assistant as assistant_bot
from noam_coach.bot import callback_menu as callback_menu_bot
from noam_coach.bot import meal_text as meal_text_bot
from noam_coach.services import core as core_services
from noam_coach.services import goals as goal_services
from noam_coach.services.daily_menu_state import remember_active_daily_menu
from noam_coach.services.next_meal import (
    generate_next_meal_recommendation,
    remember_active_recommendation,
)


class FakeMessage:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.replies: list[str] = []
        self.markups: list[Any] = []
        self.message_id = 555
        self.chat = self

    async def reply_text(self, body: str, reply_markup: Any = None, parse_mode: str | None = None) -> "FakeMessage":
        del parse_mode
        self.replies.append(body)
        self.markups.append(reply_markup)
        return self

    async def send_action(self, *_a: Any, **_k: Any) -> None:
        return None


class FakeUpdate:
    def __init__(self, text: str, user_id: int = 1) -> None:
        self.effective_message = FakeMessage(text)
        self.effective_user = type("U", (), {"id": user_id})()
        self.effective_chat = type("C", (), {"id": user_id})()


async def _make_db(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "batch_c.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1, 'Noam', NULL, ?)",
        (utc_now(),),
    )
    return db


async def _active_goal(db: Database, *, calories: int, protein: int) -> None:
    await db.execute(
        """
        INSERT INTO goal_versions(user_id, calories, protein, steps, phase, status, source, created_at)
        VALUES(1, ?, ?, 8000, 'fat_loss_muscle_retention', 'active', 'user_approved', ?)
        """,
        (calories, protein, utc_now()),
    )


def _bind(monkeypatch: pytest.MonkeyPatch, db: Database) -> None:
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(callback_menu_bot, "DB", db)
    monkeypatch.setattr(assistant_bot, "DB", db)
    monkeypatch.setattr(meal_text_bot, "DB", db)
    monkeypatch.setattr(core_services, "DB", db)
    monkeypatch.setattr(goal_services, "DB", db)


def _allow_text_message(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _is_allowed(_update: Any) -> bool:
        return True

    async def _ensure_user(_update: Any) -> int:
        return 1

    async def _track_event(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(coach_bot, "is_allowed", _is_allowed)
    monkeypatch.setattr(coach_bot, "ensure_user", _ensure_user)
    monkeypatch.setattr(coach_bot, "track_event", _track_event)


def _ctx() -> Any:
    return type("Ctx", (), {"bot": object()})()


@pytest.mark.asyncio
async def test_correction_text_hits_active_recommendation_not_daily_menu(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact FIX 50 scenario: both an active daily menu and an active
    next-meal recommendation exist; ambiguous text ('קטן יותר') must be
    interpreted against the recommendation, not silently edit the menu."""
    db = await _make_db(tmp_path)
    _bind(monkeypatch, db)
    _allow_text_message(monkeypatch)
    await _active_goal(db, calories=2100, protein=160)

    await remember_active_daily_menu(db, 1, text="תפריט לדוגמה", strategy="balanced")

    rec = await generate_next_meal_recommendation(db, 1)
    await remember_active_recommendation(db, 1, rec)

    update = FakeUpdate("קטן יותר")
    await meal_text_bot.handle_text_message(update, _ctx())

    reply = update.effective_message.replies[-1]
    # The recommendation-correction reply format includes "עכשיו" (from
    # format_next_meal_recommendation's "מה לאכול עכשיו" header) -- the
    # daily-menu-edit reply format is structurally different (menu slot
    # language), so this reply shape proves which handler actually fired.
    assert "עכשיו" in reply


@pytest.mark.asyncio
async def test_menu_edit_text_still_reaches_menu_editor_without_active_recommendation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without an active recommendation, handle_recommendation_correction()
    returns None immediately, so menu-edit text must still reach the menu
    editor exactly as before this reorder."""
    db = await _make_db(tmp_path)
    _bind(monkeypatch, db)
    _allow_text_message(monkeypatch)
    await _active_goal(db, calories=2100, protein=160)

    await remember_active_daily_menu(db, 1, text="תפריט לדוגמה", strategy="balanced")

    update = FakeUpdate("תחליף לי את התפריט, בלי גלוטן")
    await meal_text_bot.handle_text_message(update, _ctx())

    assert update.effective_message.replies, "expected the menu editor to reply"
