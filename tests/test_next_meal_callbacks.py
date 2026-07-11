from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import coach_bot
import user_model
from db import Database
from helpers import utc_now
from noam_coach.bot import callback_menu as callback_menu_bot


class FakeQuery:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.reply_markups: list[Any] = []

    async def edit_message_text(
        self,
        text: str,
        reply_markup: Any = None,
        parse_mode: str | None = None,
    ) -> None:
        del parse_mode
        self.messages.append(text)
        self.reply_markups.append(reply_markup)

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        del text, show_alert


async def _make_ready_db(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "next_meal_callbacks.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1, 'Test', NULL, ?)",
        (utc_now(),),
    )
    await db.execute(
        """
        INSERT INTO goal_versions(user_id, calories, protein, steps, phase, status, source, created_at)
        VALUES(1, 2000, 150, 8000, 'fat_loss_muscle_retention', 'active', 'test', ?)
        """,
        (utc_now(),),
    )
    await user_model.set_fact(
        db,
        1,
        "sleep_schedule",
        {"bedtime": "23:00"},
        source=user_model.SOURCE_USER,
        confirmed=True,
    )
    return db


def _callback_data(markup: Any) -> set[str]:
    return {
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    }


@pytest.mark.asyncio
async def test_next_meal_menu_exposes_the_task03_four_buttons(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TASK-03: "מה לאכול עכשיו" shows exactly one recommendation with at
    most 4 actions — confirm eaten, refresh, change quantities, back to
    status. No numbered alternatives, no per-option "plan for later" button.
    """
    db = await _make_ready_db(tmp_path)
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(callback_menu_bot, "DB", db)

    query = FakeQuery()
    handled = await callback_menu_bot.handle_menu_callback(query, 1, "menu:nextmeal")

    assert handled is True
    callbacks = _callback_data(query.reply_markups[-1])
    assert len(callbacks) <= 4
    assert "nextmeal:save:1" in callbacks
    assert "nextmeal:refresh" in callbacks
    assert "nextmeal:editqty:1" in callbacks
    assert "menu:status" in callbacks
    assert "nextmeal:save:2" not in callbacks
    assert not any(data.startswith("nextmeal:plan:") for data in callbacks)
    assert not any(data.startswith("nextmeal:choose:") for data in callbacks)
    assert not any(data.startswith("nextmeal:dislike:") for data in callbacks)


@pytest.mark.asyncio
async def test_next_meal_not_suitable_is_temporary_not_permanent_dislike(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """re7 P1-7: 'לא מתאים לי' is a temporary rejection, NOT a standing dislike."""
    db = await _make_ready_db(tmp_path)
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(callback_menu_bot, "DB", db)

    query = FakeQuery()
    handled = await callback_menu_bot.handle_menu_callback(query, 1, "nextmeal:dislike:1")

    assert handled is True
    # No permanent dislike was stored.
    assert await user_model.get_value(db, 1, "disliked_foods") in (None, "", "none", [])
    # A refreshed (still single) recommendation is shown.
    assert "רעננתי" in query.messages[-1] or "אפשרות" in query.messages[-1]
    callbacks = _callback_data(query.reply_markups[-1])
    assert "nextmeal:save:1" in callbacks
    assert not any(data.startswith("nextmeal:plan:") for data in callbacks)
