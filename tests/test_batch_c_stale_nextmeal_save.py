"""Batch C (FIX 51): a stale nextmeal:save:N callback must not save from a
freshly generated option list when the recommendation it was rendered
against is no longer the active one.

Root cause (verified before this fix): handle_menu_callback's
"nextmeal:save:" branch fell back to generate_next_meal_recommendation()'s
freshly computed options whenever get_active_recommendation_options()
returned nothing (expired TTL, or cleared by a newer recommendation/meal
event). A stale "save option 2" button could therefore save whatever
option 2 happens to be in a brand-new list -- a different meal than the one
the user actually tapped under.
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
from noam_coach.services import core as core_services
from noam_coach.services import goals as goal_services
from noam_coach.services.next_meal import (
    clear_active_recommendation,
    generate_next_meal_recommendation,
    remember_active_recommendation,
)


class FakeMessage:
    def __init__(self) -> None:
        self.chat_id = 1
        self.message_id = 555


class FakeQuery:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.markups: list[Any] = []
        self.message = FakeMessage()

    async def edit_message_text(self, text: str, reply_markup: Any = None, parse_mode: str | None = None) -> None:
        del parse_mode
        self.messages.append(text)
        self.markups.append(reply_markup)

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        del text, show_alert


async def _make_db(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "batch_c_stale.db"))
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
    monkeypatch.setattr(core_services, "DB", db)
    monkeypatch.setattr(goal_services, "DB", db)


@pytest.mark.asyncio
async def test_stale_save_without_active_recommendation_is_rejected_not_regenerated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path)
    _bind(monkeypatch, db)
    await _active_goal(db, calories=2100, protein=160)

    # No active recommendation exists (e.g. it expired, or a newer meal
    # event cleared it) -- this simulates an old message's button being
    # tapped long after the card it belonged to stopped being current.
    await clear_active_recommendation(db, 1)

    async def _consumed() -> float:
        row = await db.fetch_one("SELECT COALESCE(SUM(calories),0) AS c FROM meals WHERE user_id=1")
        return float(row["c"])

    before = await _consumed()
    query = FakeQuery()
    handled = await callback_menu_bot.handle_menu_callback(query, 1, "nextmeal:save:1")

    assert handled is True
    # Nothing was silently saved from a freshly regenerated list.
    assert await _consumed() == before
    assert query.messages
    assert "לא פעילה" in query.messages[-1] or "לא מצאתי" in query.messages[-1]


@pytest.mark.asyncio
async def test_save_with_active_recommendation_still_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sanity check: the fix must not break the normal, non-stale path."""
    db = await _make_db(tmp_path)
    _bind(monkeypatch, db)
    await _active_goal(db, calories=2100, protein=160)

    rec = await generate_next_meal_recommendation(db, 1)
    await remember_active_recommendation(db, 1, rec)

    async def _consumed() -> float:
        row = await db.fetch_one("SELECT COALESCE(SUM(calories),0) AS c FROM meals WHERE user_id=1")
        return float(row["c"])

    before = await _consumed()
    query = FakeQuery()
    handled = await callback_menu_bot.handle_menu_callback(query, 1, "nextmeal:save:1")

    assert handled is True
    assert await _consumed() > before
