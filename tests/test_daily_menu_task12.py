from pathlib import Path

import pytest

from db import Database
from noam_coach.services.daily_menu_edit import (
    parse_daily_menu_edit,
    try_build_daily_menu_edit_reply,
)
from noam_coach.services.daily_menu_state import (
    get_active_daily_menu,
    remember_active_daily_menu,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
async def test_active_daily_menu_revision_uses_previous_revision(tmp_path) -> None:
    db = Database(str(tmp_path / "daily_menu_task12.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(?, ?, ?, ?)",
        (1, "Noam", "noam", "2026-07-11T00:00:00+03:00"),
    )

    await remember_active_daily_menu(
        db,
        1,
        text=(
            "<b>תפריט יומי</b>\n"
            "ארוחת בוקר: ביצים וטוסט\n"
            "ארוחת צהריים: עוף ואורז"
        ),
        strategy="balanced",
        revision=1,
    )

    first = await try_build_daily_menu_edit_reply(db, 1, "אני לא אוהב ביצים")
    assert first is not None
    first_text, _ = first
    assert "גרסה 2" in first_text
    assert "ארוחת בוקר: ביצים וטוסט" not in first_text
    assert "בלי ביצים" in first_text

    active_after_first = await get_active_daily_menu(db, 1)
    assert active_after_first is not None
    assert active_after_first["revision"] == 2
    assert "ביצים וטוסט" not in active_after_first["text"]

    second = await try_build_daily_menu_edit_reply(db, 1, "ארוחת צהריים גדולה יותר")
    assert second is not None
    second_text, _ = second
    assert "גרסה 3" in second_text
    assert "בלי ביצים" in second_text
    assert "ארוחת צהריים: עוף ואורז" in second_text


def test_daily_menu_edit_free_text_requires_active_menu_for_loose_requests() -> None:
    assert parse_daily_menu_edit("אני לא אוהב ביצים") is None
    intent = parse_daily_menu_edit("אני לא אוהב ביצים", active_menu_context=True)
    assert intent is not None
    assert intent.wants_no_item == "ביצים"


def test_nutrition_strategy_selection_sends_standalone_daily_menu() -> None:
    src = (ROOT / "noam_coach/bot/callback_plans.py").read_text(encoding="utf-8")
    select_block = src.split('if data.startswith("planv2:select:"):', 1)[1]
    select_block = select_block.split('if data == "planv2:unify":', 1)[0]
    assert 'selected["plan_type"] == "nutrition"' in select_block
    assert "build_morning_menu_text(user_id)" in select_block
    assert "query.message.reply_text" in select_block
    assert "nutrition_strategy_selected" in select_block
