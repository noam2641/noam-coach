"""Regression tests derived from the re7 usage recording (2026-06-27).

These exercise production paths (callbacks, free-text routing, the canonical
next-meal/goal services) — not just helpers — to lock the behaviours the
recording exposed:

  1. A 269-calorie remaining balance must not yield a 450-calorie meal.
  2. A free-text "יעד 2100" change becomes the single active goal after confirm.
  3. "לא מתאים לי" produces a genuinely different option and is NOT a permanent
     dislike.
  4. A free-text budget correction ("אבל נשאר לי 269 קלוריות") recomputes the
     active recommendation instead of dropping to the main menu.
  5. A suggestion is only counted as eaten after an explicit "save as meal";
     a double tap does not double-count.
  6. An approved goal always supersedes a provisional one across screens.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import assistant
import coach_bot
import user_model
from db import Database
from helpers import utc_now
from noam_coach.bot import assistant as assistant_bot
from noam_coach.bot import callback_menu as callback_menu_bot
from noam_coach.bot import meal_text as meal_text_bot
from noam_coach.services import core as core_services
from noam_coach.services import goals as goal_services
from noam_coach.services.goals import fetch_goal
from noam_coach.services.next_meal import (
    generate_next_meal_recommendation,
    option_fingerprint,
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


class FakeUpdate:
    def __init__(self, text: str, user_id: int = 1) -> None:
        self.effective_message = FakeMessage(text)
        self._uid = user_id
        self.effective_user = type("U", (), {"id": user_id})()
        self.effective_chat = type("C", (), {"id": user_id})()


async def _make_db(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "re7.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1, 'Noam', NULL, ?)",
        (utc_now(),),
    )
    return db


async def _active_goal(db: Database, *, calories: int, protein: int, source: str = "user_approved") -> None:
    await db.execute(
        """
        INSERT INTO goal_versions(user_id, calories, protein, steps, phase, status, source, created_at)
        VALUES(1, ?, ?, 8000, 'fat_loss_muscle_retention', 'active', ?, ?)
        """,
        (calories, protein, source, utc_now()),
    )


async def _meal(db: Database, *, calories: int, protein: int) -> None:
    await db.execute(
        """
        INSERT INTO meals(user_id, name, calories, protein, carbs, fat, confidence, eaten_at, created_at)
        VALUES(1, 'logged', ?, ?, 0, 0, 1, ?, ?)
        """,
        (calories, protein, utc_now(), utc_now()),
    )


def _bind(monkeypatch: pytest.MonkeyPatch, db: Database) -> None:
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(callback_menu_bot, "DB", db)
    monkeypatch.setattr(assistant_bot, "DB", db)
    monkeypatch.setattr(meal_text_bot, "DB", db)
    monkeypatch.setattr(core_services, "DB", db)
    monkeypatch.setattr(goal_services, "DB", db)


def _allow_text_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch the auth/identity boundary so handle_text_message runs for user 1."""
    async def _is_allowed(_update: Any) -> bool:
        return True

    async def _ensure_user(_update: Any) -> int:
        return 1

    async def _track_event(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(coach_bot, "is_allowed", _is_allowed)
    monkeypatch.setattr(coach_bot, "ensure_user", _ensure_user)
    monkeypatch.setattr(coach_bot, "track_event", _track_event)


# --------------------------------------------------------------------------
# Scenario 1 — 269 remaining calories
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenario1_269_remaining_does_not_offer_450(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    await _active_goal(db, calories=2100, protein=160)
    await _meal(db, calories=1831, protein=118)  # remaining 269 / 42

    rec = await generate_next_meal_recommendation(db, 1)

    assert rec.context.nutrition.calorie_balance == 269
    assert all(o.calories <= 269 for o in rec.options)
    assert not any(o.calories >= 450 for o in rec.options)
    assert rec.budget.calories_max <= 269
    assert rec.budget.allows_overage is False
    # Ingredient totals are sane (non-negative, protein present).
    for option in rec.options:
        assert option.calories > 0
        assert option.protein >= 0


# --------------------------------------------------------------------------
# Scenario 2 — free-text goal change to 2100
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenario2_text_goal_change_to_2100(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = await _make_db(tmp_path)
    _bind(monkeypatch, db)
    # Existing provisional/computed goal differs (e.g. 2390).
    await _active_goal(db, calories=2390, protein=150, source="computed")

    # 1) classification
    intent = assistant.keyword_fallback("יעד 2100")
    assert intent.action == "set_calorie_goal"
    assert intent.slots["calories"] == 2100

    # 2) free-text turn -> confirmation screen (no silent activation)
    monkeypatch.setattr(assistant, "classify_intent", _fake_classify(intent))
    update = FakeUpdate("יעד 2100")
    await assistant_bot.route_free_text(update, 1)
    confirm_text = update.effective_message.replies[-1]
    assert "2,100" in confirm_text or "2100" in confirm_text
    assert (await fetch_goal(1))["calories"] == 2390  # not changed yet

    # 3) confirm -> single active goal of 2100
    query = FakeQuery()
    await callback_menu_bot.handle_menu_callback(query, 1, "confirm:goal_cal:2100")
    goal = await fetch_goal(1)
    assert goal["calories"] == 2100
    assert goal.get("provisional") is False

    # 4) only one current goal row
    rows = await db.fetch_all(
        "SELECT calories, status FROM goal_versions WHERE user_id=1 "
        "AND status IN ('active','active_provisional')"
    )
    assert len(rows) == 1
    assert int(rows[0]["calories"]) == 2100

    # 5) next-meal computes against 2100
    await _meal(db, calories=1831, protein=100)
    rec = await generate_next_meal_recommendation(db, 1)
    assert rec.context.nutrition.target_calories == 2100


def _fake_classify(intent: assistant.Intent):
    async def _inner(*_a: Any, **_k: Any) -> assistant.Intent:
        return intent
    return _inner


# --------------------------------------------------------------------------
# Scenario 3 — rejecting an option
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenario3_reject_option_is_temporary_and_different(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = await _make_db(tmp_path)
    _bind(monkeypatch, db)
    await _active_goal(db, calories=2100, protein=160)
    await _meal(db, calories=1500, protein=90)  # remaining 600

    initial = await generate_next_meal_recommendation(db, 1)
    rejected_fp = option_fingerprint(initial.options[0])

    query = FakeQuery()
    await callback_menu_bot.handle_menu_callback(query, 1, "nextmeal:dislike:1")

    # Refreshed recommendation excludes the rejected fingerprint.
    refreshed = await generate_next_meal_recommendation(db, 1)
    assert rejected_fp not in {option_fingerprint(o) for o in refreshed.options}
    # Not stored as a permanent dislike.
    assert await user_model.get_value(db, 1, "disliked_foods") in (None, "", "none", [])
    # Rejection persisted (survives restart within TTL).
    flags = await db.fetch_one("SELECT flags FROM daily_flags WHERE user_id=1")
    assert flags is not None and "next_meal_rejections" in flags["flags"]


# --------------------------------------------------------------------------
# Scenario 4 — free-text budget correction
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenario4_text_budget_correction_recomputes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = await _make_db(tmp_path)
    _bind(monkeypatch, db)
    _allow_text_message(monkeypatch)
    await _active_goal(db, calories=2100, protein=160)
    await _meal(db, calories=1831, protein=118)  # remaining 269

    # An active recommendation exists.
    rec = await generate_next_meal_recommendation(db, 1)
    from noam_coach.services.next_meal import remember_active_recommendation
    await remember_active_recommendation(db, 1, rec)

    update = FakeUpdate("אבל נשאר לי 269 קלוריות")
    await meal_text_bot.handle_text_message(update, _ctx())

    reply = update.effective_message.replies[-1]
    # Stayed in the recommendation flow (did not drop to the generic main menu).
    assert "אפשרות 1" in reply
    assert "269" in reply
    # The recomputed options still respect the 269 budget.
    refreshed = await generate_next_meal_recommendation(db, 1)
    assert all(o.calories <= 269 for o in refreshed.options)


def _ctx() -> Any:
    return type("Ctx", (), {"bot": object()})()


# --------------------------------------------------------------------------
# Scenario 5 — save only after confirmation, idempotent
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenario5_only_save_counts_and_no_double_count(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = await _make_db(tmp_path)
    _bind(monkeypatch, db)
    await _active_goal(db, calories=2100, protein=160)
    await _meal(db, calories=1500, protein=90)

    async def _consumed() -> float:
        row = await db.fetch_one("SELECT COALESCE(SUM(calories),0) AS c FROM meals WHERE user_id=1")
        return float(row["c"])

    before = await _consumed()

    # Viewing the recommendation does not add a meal.
    query = FakeQuery()
    await callback_menu_bot.handle_menu_callback(query, 1, "menu:nextmeal")
    assert await _consumed() == before

    # Choosing does not add a meal (only shows the save prompt).
    await callback_menu_bot.handle_menu_callback(query, 1, "nextmeal:choose:1")
    assert await _consumed() == before

    # Saving the SAME option twice within the window adds exactly one meal
    # (the fingerprint guard in save_chosen_meal makes a double-tap idempotent).
    from noam_coach.services.next_meal import save_chosen_meal
    rec = await generate_next_meal_recommendation(db, 1)
    option = rec.options[0]
    first = await save_chosen_meal(db, 1, option)
    second = await save_chosen_meal(db, 1, option)
    assert first is True
    assert second is False
    after = await _consumed()
    assert after == before + option.calories

    # The save callback prefix is registered for tap-debounce protection.
    from noam_coach.bot import callback_router
    assert any(p.startswith("nextmeal:save") for p in callback_router._DEBOUNCE_PREFIXES)


# --------------------------------------------------------------------------
# Scenario 6 — provisional vs approved goal
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenario6_approved_goal_supersedes_provisional(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = await _make_db(tmp_path)
    _bind(monkeypatch, db)
    # A provisional goal exists first.
    await db.execute(
        """
        INSERT INTO goal_versions(user_id, calories, protein, steps, phase, status, source, created_at)
        VALUES(1, 2390, 150, 8000, 'fat_loss_muscle_retention', 'active_provisional', 'computed_provisional', ?)
        """,
        (utc_now(),),
    )
    pre = await fetch_goal(1)
    assert pre["provisional"] is True

    # Approve an explicit 2100 goal via the canonical confirm path.
    query = FakeQuery()
    await callback_menu_bot.handle_menu_callback(query, 1, "confirm:goal_cal:2100")

    goal = await fetch_goal(1)
    assert goal["calories"] == 2100
    assert goal["provisional"] is False
    # The provisional row was superseded — only one current goal remains.
    current = await db.fetch_all(
        "SELECT id FROM goal_versions WHERE user_id=1 AND status IN ('active','active_provisional')"
    )
    assert len(current) == 1
