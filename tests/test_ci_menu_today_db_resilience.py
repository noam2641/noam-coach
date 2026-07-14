"""Regression coverage for a real test-isolation / dependency-boundary bug
CI caught on commit b3d77d2: "menu:today" (the legacy daily-menu alias)
unconditionally called get_active_daily_menu(DB, user_id) to check for a
reusable active menu, with no guard around that lookup. Every other
best-effort "reuse existing state" lookup on this same handler
(menu:morning's build_daily_context call, immediately above this branch in
callback_menu.py) is wrapped in suppress(Exception) with a None fallback;
this one was not, so any failure in the reuse-lookup step (a missing/
unreachable daily_flags table, or any other transient lookup failure)
aborted the entire on-demand-menu action instead of falling through to
build_morning_menu_text -- the same fallback the code already takes when no
active menu exists at all.

This file exercises the actual fix (a suppress(Exception) boundary around
just the optional reuse lookup), not merely the symptom the original CI
failure surfaced (tests/regression/test_qa_session_20260705_fixes.py's
test_menu_today_is_legacy_alias_for_full_daily_menu, which incidentally
never initializes coach_bot.DB and therefore also proves this contract,
but was not written to test the resilience boundary specifically).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import coach_bot
from db import Database
from helpers import utc_now
from noam_coach.bot import callback_menu as callback_menu_bot
from noam_coach.services.daily_menu_state import MenuMealRecord, remember_active_daily_menu


class FakeQuery:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.reply_markups: list[Any] = []

    async def edit_message_text(self, text: str, reply_markup: Any = None, parse_mode: str | None = None) -> None:
        del parse_mode
        self.messages.append(text)
        self.reply_markups.append(reply_markup)

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        del text, show_alert


async def _make_db(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "ci_menu_today.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'Test',NULL,?)",
        (utc_now(),),
    )
    await db.execute(
        """
        INSERT INTO goal_versions(user_id, calories, protein, steps, phase, status, source, created_at)
        VALUES(1, 2000, 150, 8000, 'fat_loss_muscle_retention', 'active', 'test', ?)
        """,
        (utc_now(),),
    )
    return db


@pytest.mark.asyncio
async def test_menu_today_falls_through_to_fresh_menu_when_reuse_lookup_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact root cause: the active-menu reuse lookup fails for a reason
    unrelated to 'no menu exists' (here: DB pointed at a database with no
    schema at all, reproducing the CI OperationalError). menu:today must
    still render a menu via build_morning_menu_text, not the generic
    on-demand-recommendation error text."""
    uninitialized_db = Database(str(tmp_path / "no_schema.db"))
    # Deliberately NOT calling await uninitialized_db.init() -- this
    # reproduces "no such table: daily_flags" exactly as CI hit it, without
    # depending on cross-test global state.
    monkeypatch.setattr(coach_bot, "DB", uninitialized_db)

    async def fake_build_morning_menu_text(_user_id: int) -> str:
        return "התפריט המלא של היום"

    monkeypatch.setattr(coach_bot, "build_morning_menu_text", fake_build_morning_menu_text)

    query = FakeQuery()
    handled = await callback_menu_bot.handle_menu_callback(query, 1, "menu:today")

    assert handled is True
    assert query.messages[-1] == "התפריט המלא של היום"
    assert "לא הצלחתי" not in query.messages[-1]


@pytest.mark.asyncio
async def test_menu_today_still_reuses_an_existing_active_menu(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sanity check the product contract is preserved: when the reuse
    lookup SUCCEEDS and an active menu exists, menu:today must still reuse
    it (TASK-11's contract), not regenerate from scratch."""
    db = await _make_db(tmp_path)
    monkeypatch.setattr(coach_bot, "DB", db)

    meal = MenuMealRecord(meal_id="breakfast-0", slot="breakfast", time="08:00", role="ארוחת בוקר", calories=500, protein=40)
    await remember_active_daily_menu(db, 1, text="תפריט קיים מהיום", meals=[meal])

    async def fail_if_regenerated(_user_id: int) -> str:
        raise AssertionError("menu:today must reuse the active menu, not regenerate it")

    monkeypatch.setattr(coach_bot, "build_morning_menu_text", fail_if_regenerated)

    query = FakeQuery()
    handled = await callback_menu_bot.handle_menu_callback(query, 1, "menu:today")

    assert handled is True
    assert query.messages[-1] == "תפריט קיים מהיום"


@pytest.mark.asyncio
async def test_menu_today_still_surfaces_a_genuine_menu_generation_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fix must not hide a REAL failure: if build_morning_menu_text
    itself (menu generation, not the optional reuse lookup) raises, the
    existing friendly-error fallback must still fire. This proves the
    suppress(Exception) boundary added by the fix is scoped to the reuse
    lookup only, not the whole on-demand-menu action."""
    db = await _make_db(tmp_path)
    monkeypatch.setattr(coach_bot, "DB", db)

    async def fail_generation(_user_id: int) -> str:
        raise RuntimeError("simulated genuine menu-generation failure")

    monkeypatch.setattr(coach_bot, "build_morning_menu_text", fail_generation)

    query = FakeQuery()
    handled = await callback_menu_bot.handle_menu_callback(query, 1, "menu:today")

    assert handled is True
    # The existing friendly_error fallback still fires -- the failure is not
    # silently swallowed.
    assert query.messages[-1] != ""
    assert "התפריט המלא של היום" not in query.messages[-1]
