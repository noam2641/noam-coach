"""RE14 regression tests — one single menu type.

The bot used to have two different menus: a compact home menu plus a
separate "עוד פעולות והגדרות" screen. RE14 unifies them:
  * home_keyboard contains ALL actions (daily + settings) on one screen.
  * more_keyboard is a backward-compat alias returning the same menu.
  * Tapping a legacy "menu:more" button renders the unified home screen,
    not a second menu type.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import coach_bot
from db import Database
from helpers import utc_now
from noam_coach.bot import callback_menu as callback_menu_bot
from noam_coach.bot import ui as ui_bot
from noam_coach.services import core as core_services


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
    db = Database(str(tmp_path / "re14.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'Test',NULL,?)",
        (utc_now(),),
    )
    return db


def _patch_db(monkeypatch: pytest.MonkeyPatch, db: Database) -> None:
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(callback_menu_bot, "DB", db)
    monkeypatch.setattr(ui_bot, "DB", db)
    monkeypatch.setattr(core_services, "DB", db)


def _callbacks(markup: Any) -> set[str]:
    return {btn.callback_data for row in markup.inline_keyboard for btn in row}


def test_single_menu_has_no_more_button() -> None:
    from noam_coach.bot.ui import settings_keyboard

    callbacks = _callbacks(coach_bot.home_keyboard())
    assert "menu:more" not in callbacks
    # TASK-6: the former "עוד" secondary actions now live behind the
    # "⚙️ הגדרות ועוד" (menu:settings) entry, not directly on the home menu.
    assert "menu:settings" in callbacks
    settings = _callbacks(settings_keyboard())
    assert {"menu:profile", "menu:chart", "menu:weekly",
            "menu:health", "menu:about"} <= settings
    # TASK-7: no standalone Goal entry point anywhere, including Settings —
    # the goal is only reachable as the first step of "Complete now".
    assert "menu:goal" not in settings


@pytest.mark.asyncio
async def test_legacy_more_callback_renders_the_home_menu(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)

    home_target = FakeTarget()
    handled = await callback_menu_bot.handle_menu_callback(home_target, 1, "menu:home")
    assert handled is True

    more_target = FakeTarget()
    handled = await callback_menu_bot.handle_menu_callback(more_target, 1, "menu:more")
    assert handled is True

    # Same screen: same title and the same unified keyboard.
    assert "המאמן האישי שלך" in more_target.messages[-1]
    assert _callbacks(more_target.reply_markups[-1]) == _callbacks(home_target.reply_markups[-1])
    assert "menu:more" not in _callbacks(more_target.reply_markups[-1])


@pytest.mark.asyncio
async def test_about_screen_returns_to_the_single_menu(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)

    target = FakeTarget()
    handled = await callback_menu_bot.handle_menu_callback(target, 1, "menu:about")
    assert handled is True
    callbacks = _callbacks(target.reply_markups[-1])
    assert callbacks == {"menu:home"}  # one back button, to the one menu
