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
    # TASK-17 merged the unify/show handlers into a single planv2:my_week block.
    select_block = select_block.split('if data in ("planv2:my_week"', 1)[0]
    assert 'selected["plan_type"] == "nutrition"' in select_block
    assert "build_morning_menu_text(user_id)" in select_block
    assert "query.message.reply_text" in select_block
    assert "nutrition_strategy_selected" in select_block


class _FakeQuery:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.message = self

    async def edit_message_text(self, text: str, reply_markup=None, parse_mode=None) -> None:
        del reply_markup, parse_mode
        self.messages.append(text)

    async def reply_text(self, text: str, reply_markup=None, parse_mode=None):
        del reply_markup, parse_mode
        self.messages.append(text)
        return self

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        del text, show_alert


@pytest.mark.asyncio
async def test_viewing_daily_menu_reuses_active_menu_instead_of_regenerating(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TASK-11 cross-path regression: a standing free-text exclusion
    ("בלי ביצים היום") must survive a plain "show me today's menu" tap.

    menu:today / menu:daily_menu used to call build_morning_menu_text
    unconditionally — a full AI regeneration that never consults
    daily_menu_state, silently discarding any active edit/exclusion and
    reintroducing a removed food. Only the explicit "🔄 רענן תפריט"
    (menu:refresh_daily_menu) action is allowed to regenerate from scratch.
    """
    import coach_bot
    import planning
    import user_model
    from helpers import utc_now
    from noam_coach.bot import callback_menu as callback_menu_bot

    db = Database(str(tmp_path / "daily_menu_reuse.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'T',NULL,?)",
        (utc_now(),),
    )
    # callback_menu.py uses @runtime_bound, which re-syncs its own DB global
    # from the coach_bot facade right before every call — both must be
    # patched or the facade's DB silently wins.
    monkeypatch.setattr(coach_bot, "DB", db, raising=False)
    monkeypatch.setattr(callback_menu_bot, "DB", db, raising=False)
    # Bypass the nutrition-readiness/goal gate — irrelevant to this test.
    async def _ready(*_a, **_k):
        return {"ready": True, "missing": []}

    async def _goal(*_a, **_k):
        return {"calories": 2000}

    monkeypatch.setattr(user_model, "compute_readiness", _ready)
    monkeypatch.setattr(planning, "active_goal", _goal)

    await remember_active_daily_menu(
        db,
        1,
        text="<b>תפריט יומי</b>\nארוחת בוקר: ביצים וטוסט\nללא ביצים: קוטג' וטוסט",
        strategy="balanced",
        revision=2,
        source="daily_menu_edit",
    )

    def _regeneration_should_not_run(*_a, **_k):
        raise AssertionError(
            "build_morning_menu_text must not run when an active menu already "
            "exists for menu:today/menu:daily_menu"
        )

    # callback_menu.py uses @runtime_bound, which refreshes names like
    # build_morning_menu_text from the coach_bot facade right before each
    # call — so the facade attribute is what must be patched.
    monkeypatch.setattr(coach_bot, "build_morning_menu_text", _regeneration_should_not_run, raising=False)

    for data in ("menu:today", "menu:daily_menu"):
        query = _FakeQuery()
        handled = await callback_menu_bot.handle_menu_callback(query, 1, data)
        assert handled is True
        assert any("ללא ביצים" in text for text in query.messages)
        assert not any("ביצים וטוסט" == text for text in query.messages)


@pytest.mark.asyncio
async def test_explicit_refresh_daily_menu_still_regenerates(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The explicit "🔄 רענן תפריט" action is still allowed to build a fresh
    menu from scratch even when an active menu already exists — only the
    passive "show me today's menu" taps must reuse it (TASK-11)."""
    import coach_bot
    import planning
    import user_model
    from helpers import utc_now
    from noam_coach.bot import callback_menu as callback_menu_bot

    db = Database(str(tmp_path / "daily_menu_refresh.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'T',NULL,?)",
        (utc_now(),),
    )
    monkeypatch.setattr(coach_bot, "DB", db, raising=False)
    monkeypatch.setattr(callback_menu_bot, "DB", db, raising=False)

    async def _ready(*_a, **_k):
        return {"ready": True, "missing": []}

    async def _goal(*_a, **_k):
        return {"calories": 2000}

    monkeypatch.setattr(user_model, "compute_readiness", _ready)
    monkeypatch.setattr(planning, "active_goal", _goal)

    await remember_active_daily_menu(
        db, 1, text="<b>תפריט יומי</b>\nארוחת בוקר: ביצים וטוסט", strategy="balanced", revision=1,
    )

    called = False

    async def _fake_regenerate(*_a, **_k):
        nonlocal called
        called = True
        return "<b>תפריט חדש</b>\nארוחת בוקר: שייק חלבון"

    monkeypatch.setattr(coach_bot, "build_morning_menu_text", _fake_regenerate, raising=False)

    query = _FakeQuery()
    handled = await callback_menu_bot.handle_menu_callback(query, 1, "menu:refresh_daily_menu")
    assert handled is True
    assert called is True
    assert any("שייק חלבון" in text for text in query.messages)
