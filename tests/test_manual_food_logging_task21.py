"""TASK-21 — natural-language manual food logging without a photo.

Covers:
  * menu:food offers two clearly-separate logging methods;
  * menu:food_text starts the dedicated manual-entry flow (__manual_meal__);
  * the first free-text message is analyzed through the normal meal pipeline
    (create_approval with source="manual_text", then render_meal);
  * the cumulative fixmeal correction path is enabled for the manual meal.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import coach_bot
import user_model
from db import Database
from helpers import utc_now
from models import FoodItem, MealAnalysis
from noam_coach.bot import assistant as assistant_bot
from noam_coach.bot import callback_menu as callback_menu_bot
from noam_coach.bot import meals as meals_bot
from noam_coach.bot import onboarding as onboarding_bot
from noam_coach.services import core as core_services


class FakeMessage:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.replies: list[str] = []

    async def reply_text(self, body: str, reply_markup: Any = None, parse_mode: str | None = None) -> "FakeMessage":
        del reply_markup, parse_mode
        self.replies.append(body)
        return self

    async def edit_text(self, body: str, reply_markup: Any = None, parse_mode: str | None = None) -> None:
        del reply_markup, parse_mode
        self.replies.append(body)


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


class FakeUpdate:
    def __init__(self, message: FakeMessage) -> None:
        self.effective_message = message


async def _make_db(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "manual_meal.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'Test',NULL,?)",
        (utc_now(),),
    )
    return db


def _bind(monkeypatch: pytest.MonkeyPatch, db: Database) -> None:
    for mod in (coach_bot, onboarding_bot, callback_menu_bot, assistant_bot, meals_bot, core_services):
        monkeypatch.setattr(mod, "DB", db, raising=False)


def _fake_analysis() -> MealAnalysis:
    return MealAnalysis(
        meal_name="קציצות ואורז",
        confidence=0.75,
        items=[
            FoodItem(name="קציצות בקר", grams=210, calories=420, protein=36, carbs=6, fat=28, confidence=0.7),
            FoodItem(name="אורז לבן מבושל", grams=150, calories=195, protein=4, carbs=42, fat=0.5, confidence=0.8),
        ],
    )


@pytest.mark.asyncio
async def test_menu_food_offers_two_logging_methods(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = await _make_db(tmp_path)
    _bind(monkeypatch, db)
    query = FakeQuery()
    handled = await callback_menu_bot.handle_menu_callback(query, 1, "menu:food")
    assert handled is True
    callbacks = [btn.callback_data for row in query.reply_markups[-1].inline_keyboard for btn in row]
    assert "menu:food_photo" in callbacks
    assert "menu:food_text" in callbacks


@pytest.mark.asyncio
async def test_menu_food_text_starts_manual_flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = await _make_db(tmp_path)
    _bind(monkeypatch, db)
    query = FakeQuery()
    handled = await callback_menu_bot.handle_menu_callback(query, 1, "menu:food_text")
    assert handled is True
    # The manual-entry pending step is now active.
    active = await onboarding_bot.conversation.get_active_flow(db, 1)
    assert active.step == "__manual_meal__"


@pytest.mark.asyncio
async def test_manual_text_creates_approval_with_manual_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path)
    _bind(monkeypatch, db)
    # Client must appear present; analysis is mocked. log_meal_from_text is
    # runtime-bound, so it reads OPENAI_CLIENT / analyze_meal_text from the
    # coach_bot facade — patch both there.
    monkeypatch.setattr(coach_bot, "OPENAI_CLIENT", object(), raising=False)

    async def fake_analyze(_text: str, user_id: int | None = None) -> MealAnalysis:
        return _fake_analysis()

    monkeypatch.setattr(coach_bot, "analyze_meal_text", fake_analyze, raising=False)

    # Start the manual flow, then send the description.
    query = FakeQuery()
    await callback_menu_bot.handle_menu_callback(query, 1, "menu:food_text")

    message = FakeMessage("אכלתי 3 קציצות עם אורז")
    handled = await onboarding_bot.handle_onboarding_text(FakeUpdate(message), 1)
    assert handled is True

    # Exactly one pending meal approval, tagged as manual text.
    rows = await db.fetch_all(
        "SELECT payload FROM approvals WHERE user_id=1 AND kind='meal' AND status='pending'"
    )
    assert len(rows) == 1
    import json

    payload = rows[0]["payload"]
    data = json.loads(payload) if isinstance(payload, str) else payload
    assert data.get("source") == "manual_text"
    assert data.get("image") is None

    # The cumulative correction path is enabled (fixmeal state set).
    approval_id, _refine = await core_services.get_meal_fix(1)
    assert approval_id is not None


@pytest.mark.asyncio
async def test_manual_meal_correction_with_no_deterministic_match_does_not_silently_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Audit correction (TASK-20): a manual-text-logged meal has no source
    image, so the old code path for "the deterministic parser didn't
    recognize this correction" (image re-analysis) would raise
    RuntimeError("לא נמצאה תמונת מקור לניתוח חוזר") and every such correction
    silently failed with a generic "couldn't update" message, even though the
    correction was a perfectly reasonable free-text edit
    (e.g. "תחליף את הקציצות בחזה עוף" — not a בלי/חצי/פי-2/replace pattern the
    deterministic parser recognizes). The fix re-describes the current meal +
    the correction as text and re-runs analyze_meal_text (the same primitive
    manual logging itself already uses), instead of raising.
    """
    from noam_coach.bot import meal_text as meal_text_bot

    db = await _make_db(tmp_path)
    _bind(monkeypatch, db)
    monkeypatch.setattr(meal_text_bot, "DB", db, raising=False)
    monkeypatch.setattr(coach_bot, "OPENAI_CLIENT", object(), raising=False)

    async def fake_analyze(_text: str, user_id: int | None = None) -> MealAnalysis:
        return _fake_analysis()

    monkeypatch.setattr(coach_bot, "analyze_meal_text", fake_analyze, raising=False)

    query = FakeQuery()
    await callback_menu_bot.handle_menu_callback(query, 1, "menu:food_text")
    message = FakeMessage("אכלתי 3 קציצות עם אורז")
    await onboarding_bot.handle_onboarding_text(FakeUpdate(message), 1)

    approval_id, refine_count = await core_services.get_meal_fix(1)
    assert approval_id is not None

    # A correction phrase the deterministic parser (remove/preparation/
    # quantity/scale/replace patterns) does not recognize.
    replacement = MealAnalysis(
        meal_name="חזה עוף ואורז",
        confidence=0.7,
        items=[
            FoodItem(name="חזה עוף", grams=180, calories=297, protein=54, carbs=0, fat=6, confidence=0.75),
            FoodItem(name="אורז לבן מבושל", grams=150, calories=195, protein=4, carbs=42, fat=0.5, confidence=0.8),
        ],
    )

    called_with: dict[str, Any] = {}

    async def fake_analyze_correction(description: str, user_id: int | None = None) -> MealAnalysis:
        called_with["description"] = description
        called_with["user_id"] = user_id
        return replacement

    monkeypatch.setattr(meal_text_bot, "analyze_meal_text", fake_analyze_correction)

    correction_message = FakeMessage("משהו שונה לגמרי, בוא נחליף לחזה עוף")
    await meal_text_bot._handle_meal_correction_text(
        FakeUpdate(correction_message), 1, approval_id, refine_count,
    )

    # The text-only fallback ran (not a raised/caught error) and produced the
    # replacement analysis — not the original meal untouched.
    assert called_with.get("description")
    assert "משהו שונה לגמרי" in called_with["description"]
    rows = await db.fetch_all(
        "SELECT payload FROM approvals WHERE id=?", (approval_id,)
    )
    import json as _json

    payload = rows[0]["payload"]
    data = _json.loads(payload) if isinstance(payload, str) else payload
    assert data["analysis"]["meal_name"] == "חזה עוף ואורז"
    # Confirm the generic "couldn't update" failure text was NOT shown.
    assert not any("לא הצלחתי לעדכן" in reply for reply in correction_message.replies)
