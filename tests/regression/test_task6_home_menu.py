"""TASK-6 — focused primary home menu + settings screen.

The primary home menu is a small personal-coach menu; secondary/system actions
move behind "⚙️ הגדרות ועוד" (menu:settings). Every relocated route is preserved
and reachable, and no stale callback is left dangling.
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


class FakeQuery:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.reply_markups: list[Any] = []
        self.message = self

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


async def _db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Database:
    db = Database(str(tmp_path / "task6.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'T',NULL,?)",
        (utc_now(),),
    )
    for mod in (coach_bot, callback_menu_bot, core_services, ui_bot):
        monkeypatch.setattr(mod, "DB", db, raising=False)
    return db


def test_primary_menu_has_the_required_focused_actions() -> None:
    labels = [btn.text for row in coach_bot.home_keyboard().inline_keyboard for btn in row]
    assert any("מה לאכול עכשיו" in x for x in labels)
    assert any("אימון" in x for x in labels)
    assert any("מצב היום" in x for x in labels)
    assert any("התוכנית שלי" in x for x in labels)
    assert any("עדכון בוקר" in x for x in labels)
    assert any("עדכונים" in x for x in labels)
    assert any("הגדרות ועוד" in x for x in labels)
    # Removed overloaded top-level actions.
    assert not any("תפריט להיום" in x for x in labels)
    assert not any("סיכום יומי" in x for x in labels)


@pytest.mark.asyncio
async def test_menu_settings_renders_the_secondary_screen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _db(tmp_path, monkeypatch)
    query = FakeQuery()
    handled = await callback_menu_bot.handle_menu_callback(query, 1, "menu:settings")
    assert handled is True
    assert "הגדרות ועוד" in query.messages[-1]
    callbacks = [btn.callback_data for row in query.reply_markups[-1].inline_keyboard for btn in row]
    for expected in ("menu:profile", "menu:goal", "menu:daily_menu", "menu:weekly",
                     "menu:chart", "menu:evening", "menu:health", "menu:about", "menu:home"):
        assert expected in callbacks


def test_every_settings_route_is_claimed_by_a_handler() -> None:
    """No stale callbacks: every settings button's callback appears in one of
    the menu/plan callback handlers' source (the dispatch chain the router
    uses), so no button dangles."""
    from pathlib import Path as _P

    root = _P(__file__).resolve().parents[2]
    menu_src = (root / "noam_coach/bot/callback_menu.py").read_text(encoding="utf-8")
    plans_src = (root / "noam_coach/bot/callback_plans.py").read_text(encoding="utf-8")
    handled_src = menu_src + plans_src

    routes = [
        btn.callback_data
        for row in ui_bot.settings_keyboard().inline_keyboard
        for btn in row
        if btn.callback_data != "menu:app"  # only present with a public URL
    ]
    for route in routes:
        assert f'"{route}"' in handled_src, f"settings route not handled: {route}"
