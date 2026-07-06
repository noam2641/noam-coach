from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

import assistant
import coach_bot
import conversation
import user_model
from config import TZ
from db import Database
from helpers import utc_now
from noam_coach.bot import assistant as assistant_bot
from noam_coach.bot import callback_menu as callback_menu_bot
from noam_coach.bot import callback_plans as callback_plans_bot
from noam_coach.bot import meal_text as meal_text_bot
from noam_coach.bot import ui as ui_bot
from noam_coach.services import core as core_services
from noam_coach.services import profile as profile_services
from noam_coach.services.next_meal import (
    format_next_meal_recommendation,
    generate_next_meal_recommendation,
    get_active_recommendation_options,
    handle_recommendation_correction,
    mark_active_recommendation_selection,
    next_meal_action_rows,
    remember_active_recommendation,
)


class FakeMessage:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.replies: list[str] = []
        self.markups: list[Any] = []
        self.message_id = 1001
        self.chat = self

    async def reply_text(
        self,
        body: str,
        reply_markup: Any = None,
        parse_mode: str | None = None,
    ) -> "FakeMessage":
        del parse_mode
        self.replies.append(body)
        self.markups.append(reply_markup)
        return self

    async def send_action(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class FakeQuery:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.reply_markups: list[Any] = []
        self.message = FakeMessage()

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


class FakeUpdate:
    def __init__(self, text: str, user_id: int = 1) -> None:
        self.effective_message = FakeMessage(text)
        self.effective_user = type("U", (), {"id": user_id})()
        self.effective_chat = type("C", (), {"id": user_id})()


async def _ready_db(tmp_path: Path, *, calories: int = 2100, protein: int = 150) -> Database:
    db = Database(str(tmp_path / "recording_20260628_re8.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1, 'Noam', NULL, ?)",
        (utc_now(),),
    )
    await db.execute(
        """
        INSERT INTO goal_versions(user_id, calories, protein, steps, phase, status, source, created_at)
        VALUES(1, ?, ?, 8000, 'fat_loss_muscle_retention', 'active', 'test', ?)
        """,
        (calories, protein, utc_now()),
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


def _bind(monkeypatch: pytest.MonkeyPatch, db: Database) -> None:
    for module in (
        coach_bot,
        assistant_bot,
        callback_menu_bot,
        callback_plans_bot,
        meal_text_bot,
        ui_bot,
        core_services,
        profile_services,
    ):
        monkeypatch.setattr(module, "DB", db, raising=False)


def _callbacks(markup: Any) -> set[str]:
    return {
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    }


def _ctx() -> Any:
    return type("Ctx", (), {"bot": object()})()


def _allow_text(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _is_allowed(_update: Any) -> bool:
        return True

    async def _ensure_user(_update: Any) -> int:
        return 1

    async def _track_event(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(coach_bot, "is_allowed", _is_allowed)
    monkeypatch.setattr(coach_bot, "ensure_user", _ensure_user)
    monkeypatch.setattr(coach_bot, "track_event", _track_event)


@pytest.mark.asyncio
async def test_next_meal_keyboard_is_direct_and_keeps_planned_consumed_explicit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = await _ready_db(tmp_path)
    _bind(monkeypatch, db)
    rec = await generate_next_meal_recommendation(db, 1)

    rows = next_meal_action_rows(rec)
    callbacks = [callback for row in rows for _label, callback in row]
    assert sum(callback.startswith("nextmeal:save:") for callback in callbacks) == len(rec.options)
    assert sum(callback.startswith("nextmeal:plan:") for callback in callbacks) == len(rec.options)
    assert not any(callback.startswith("nextmeal:choose:") for callback in callbacks)
    assert "nextmeal:refresh" in callbacks
    assert not any(
        callback.startswith((
            "nextmeal:smaller:",
            "nextmeal:bigger:",
            "nextmeal:editqty:",
            "nextmeal:nostock:",
            "nextmeal:dislikeitem:",
        ))
        for callback in callbacks
    )
    assert all(len(callback.encode("utf-8")) <= 64 for callback in callbacks)

    query = FakeQuery()
    await callback_menu_bot.handle_menu_callback(query, 1, "menu:nextmeal")
    before = await db.fetch_one("SELECT COUNT(*) AS c FROM meals WHERE user_id=1")
    await callback_menu_bot.handle_menu_callback(query, 1, "nextmeal:plan:2")
    after = await db.fetch_one("SELECT COUNT(*) AS c FROM meals WHERE user_id=1")
    assert int(before["c"]) == int(after["c"]) == 0
    assert "עדיין לא נספר" in query.messages[-1]
    assert {"menu:status", "menu:home"} <= _callbacks(query.reply_markups[-1])


@pytest.mark.asyncio
async def test_tortilla_free_text_constraints_are_enforced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = await _ready_db(tmp_path)
    _bind(monkeypatch, db)
    now = datetime(2026, 6, 28, 20, 11, tzinfo=TZ)
    rec = await generate_next_meal_recommendation(db, 1, now=now)
    assert any("טורט" in format_next_meal_recommendation(rec) for _ in [0])
    # Ranking (RE9-053) can reorder options; select whichever option holds the
    # tortilla by content rather than a fixed position.
    tortilla_number = next(
        i for i, option in enumerate(rec.options, 1)
        if any("טורט" in ing for ing in option.ingredients)
    )

    await remember_active_recommendation(db, 1, rec, now=now)
    await mark_active_recommendation_selection(db, 1, tortilla_number, now=now)
    avoid = await handle_recommendation_correction(db, 1, "בלי טורטיה", now=now)
    assert avoid is not None
    _prefix, refreshed = avoid
    assert "טורט" not in format_next_meal_recommendation(refreshed)

    await remember_active_recommendation(db, 1, rec, now=now)
    await mark_active_recommendation_selection(db, 1, tortilla_number, now=now)
    dislike = await handle_recommendation_correction(db, 1, "לא אוהב טורטיה", now=now)
    assert dislike is not None
    stored = await user_model.get_value(db, 1, "disliked_foods")
    assert "טורטיה" in str(stored)
    future = await generate_next_meal_recommendation(db, 1, now=now)
    assert "טורט" not in format_next_meal_recommendation(future)


@pytest.mark.asyncio
async def test_selected_option_quantity_text_recalculates_without_saving(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = await _ready_db(tmp_path)
    _bind(monkeypatch, db)
    rec = await generate_next_meal_recommendation(db, 1)
    await remember_active_recommendation(db, 1, rec)
    # Ranking (RE9-053) can reorder options; select the turkey option by content.
    turkey_number = next(
        i for i, option in enumerate(rec.options, 1)
        if any("הודו" in ing for ing in option.ingredients)
    )
    await mark_active_recommendation_selection(db, 1, turkey_number)

    before = await db.fetch_one("SELECT COUNT(*) AS c FROM meals WHERE user_id=1")
    correction = await handle_recommendation_correction(db, 1, "חזה הודו 100 גרם")
    after = await db.fetch_one("SELECT COUNT(*) AS c FROM meals WHERE user_id=1")
    assert correction is not None
    assert int(before["c"]) == int(after["c"]) == 0
    active_options = await get_active_recommendation_options(db, 1)
    selected = active_options[turkey_number - 1]
    turkey = [
        item
        for item in selected.ingredient_details
        if "הודו" in item.display_name
    ][0]
    assert round(turkey.quantity) == 100
    assert selected.calories == round(sum(item.calories for item in selected.ingredient_details))


@pytest.mark.asyncio
async def test_high_remaining_balance_is_not_tiny_and_explains_allocation(tmp_path: Path) -> None:
    db = await _ready_db(tmp_path, calories=2600, protein=150)
    rec = await generate_next_meal_recommendation(
        db,
        1,
        now=datetime(2026, 6, 28, 20, 11, tzinfo=TZ),
    )

    assert rec.context.nutrition.calorie_balance == 2600
    assert rec.budget.policy == "normal"
    assert rec.budget.calories_max >= 650
    assert all(option.calories >= rec.budget.calories_min for option in rec.options)
    assert all(option.calories > 500 for option in rec.options)
    notices = "\n".join(rec.notices)
    assert "יתרה גדולה" in notices
    assert "לא צריך להשלים את כל היתרה בבת אחת" in notices


@pytest.mark.asyncio
async def test_route_free_text_next_meal_bypasses_ai_and_never_routes_to_workout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = await _ready_db(tmp_path)
    _bind(monkeypatch, db)

    async def _must_not_classify(*_args: Any, **_kwargs: Any) -> assistant.Intent:
        raise AssertionError("AI classifier should not run for deterministic next-meal text")

    monkeypatch.setattr(assistant, "classify_intent", _must_not_classify)
    update = FakeUpdate("למה במסך הזה אין מה לאכול עכשיו?")
    await assistant_bot.route_free_text(update, 1)

    reply = update.effective_message.replies[-1]
    assert "מה לאכול עכשיו" in reply
    assert "לא לאימון" in reply
    assert "אפשרות 1" in reply


def test_profile_display_never_leaks_missing_dict() -> None:
    rendered = user_model.display_value(
        "training_days_per_week",
        {"missing": True, "why_matters": "needed for workout planning"},
    )

    assert rendered
    assert "missing" not in rendered
    assert "why_matters" not in rendered
    assert "{" not in rendered and "}" not in rendered


@pytest.mark.asyncio
async def test_workout_parameter_text_requires_preview_then_applies_all_rest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = await _ready_db(tmp_path)
    _bind(monkeypatch, db)
    _allow_text(monkeypatch)
    await conversation.set_active_flow(
        db,
        1,
        conversation.FlowName.workout_parameter_edit,
        step="awaiting_text",
        payload={"code": "A", "exercise_index": 0},
    )

    update = FakeUpdate("מנוחה 1:30 לכל התרגילים")
    await meal_text_bot.handle_text_message(update, _ctx())
    flow = await conversation.get_active_flow(db, 1)
    assert flow.name == conversation.FlowName.workout_parameter_edit
    assert flow.step == "preview"
    assert "לא שמרתי עדיין" in update.effective_message.replies[-1]

    query = FakeQuery()
    handled = await callback_plans_bot.handle_workout_setup_callback(
        query,
        _ctx(),
        1,
        "wparamtext:apply",
    )
    assert handled is True
    plan = await profile_services.get_user_plan(1, "A")
    assert {exercise["rest"] for exercise in plan["exercises"]} == {90}
    assert "שמרתי" in query.messages[-1]
