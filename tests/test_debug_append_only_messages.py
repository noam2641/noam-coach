"""DEBUG_APPEND_ONLY_MESSAGES: a feature-flagged debugging mode where every
place that would normally EDIT an existing Telegram message instead SENDS A
NEW MESSAGE, leaving prior screens visible for full conversation-history
traceability. Off by default -- production edit-in-place behavior must be
unaffected when the flag is off, and must remain fully available afterward
(this is not a permanent UX redesign).
"""
from __future__ import annotations

from pathlib import Path

import pytest

import coach_bot
import config
from noam_coach.bot.ui import safe_edit, safe_message_edit
from noam_coach.bot.workout_runtime import _MessageEditTarget


class _FakeMessage:
    def __init__(self) -> None:
        self.edit_calls: list[tuple[str, object]] = []
        self.reply_calls: list[tuple[str, object]] = []

    async def edit_text(self, text: str, reply_markup=None, **_kwargs) -> None:
        self.edit_calls.append((text, reply_markup))

    async def reply_text(self, text: str, reply_markup=None, **_kwargs) -> "_FakeMessage":
        self.reply_calls.append((text, reply_markup))
        return _FakeMessage()


class _FakeQuery:
    def __init__(self) -> None:
        self.message = _FakeMessage()
        self.edit_calls: list[tuple[str, object]] = []

    async def edit_message_text(self, text: str, reply_markup=None, **_kwargs) -> None:
        self.edit_calls.append((text, reply_markup))

    async def answer(self, *_a, **_k) -> None:
        return None


@pytest.fixture(autouse=True)
def _reset_debug_flag(monkeypatch: pytest.MonkeyPatch):
    # Always start from the documented default (off) and let each test opt in.
    monkeypatch.setattr(config.SETTINGS, "debug_append_only_messages", False)
    yield
    monkeypatch.setattr(config.SETTINGS, "debug_append_only_messages", False)


# ---------------------------------------------------------------------------
# 1-4: safe_edit's debug branch sends a new message, not an edit; content and
# keyboard match; the original message is untouched.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_debug_mode_sends_new_message_not_edit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config.SETTINGS, "debug_append_only_messages", True)
    query = _FakeQuery()

    await safe_edit(query, "next screen text", "keyboard-marker")

    assert query.edit_calls == [], "must not edit the existing message in debug mode"
    assert query.message.reply_calls == [("next screen text", "keyboard-marker")]


@pytest.mark.asyncio
async def test_debug_mode_new_message_carries_expected_keyboard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config.SETTINGS, "debug_append_only_messages", True)
    query = _FakeQuery()
    marker = object()

    await safe_edit(query, "text", marker)

    assert query.message.reply_calls[0][1] is marker


@pytest.mark.asyncio
async def test_debug_mode_leaves_original_message_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config.SETTINGS, "debug_append_only_messages", True)
    query = _FakeQuery()

    await safe_edit(query, "screen 1", None)
    await safe_edit(query, "screen 2", None)

    # Neither call ever touched edit_message_text -- the "original message"
    # (and every subsequent one) is left exactly as it was sent.
    assert query.edit_calls == []
    assert [text for text, _kb in query.message.reply_calls] == ["screen 1", "screen 2"]


@pytest.mark.asyncio
async def test_production_mode_still_edits_in_place(monkeypatch: pytest.MonkeyPatch) -> None:
    """5: with the flag off (the default), safe_edit's existing production
    behavior is completely unchanged -- it edits, it does not send new."""
    monkeypatch.setattr(config.SETTINGS, "debug_append_only_messages", False)
    query = _FakeQuery()

    await safe_edit(query, "next screen text", "keyboard-marker")

    assert query.edit_calls == [("next screen text", "keyboard-marker")]
    assert query.message.reply_calls == []


@pytest.mark.asyncio
async def test_safe_message_edit_respects_debug_flag_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Message.edit_text counterpart (progress-placeholder pattern) obeys
    the same flag -- covers the direct-bypass call sites this pass fixed
    (onboarding.py's routine-extraction confirm, meal_text.py/meals.py/
    assistant.py/health_jobs.py's "progress" placeholders)."""
    message = _FakeMessage()

    monkeypatch.setattr(config.SETTINGS, "debug_append_only_messages", True)
    await safe_message_edit(message, "result text", "kb")
    assert message.edit_calls == []
    assert message.reply_calls == [("result text", "kb")]

    monkeypatch.setattr(config.SETTINGS, "debug_append_only_messages", False)
    await safe_message_edit(message, "result text 2", "kb2")
    assert message.edit_calls == [("result text 2", "kb2")]


@pytest.mark.asyncio
async def test_message_edit_target_adapter_supports_debug_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The workout rest-timer auto-advance path uses _MessageEditTarget (no
    real CallbackQuery available, just bot+chat_id+message_id) -- it must
    also be able to fan out through safe_edit's debug branch via its
    send_new() method rather than crashing on a missing .message attribute."""
    sent: list[tuple[int, str]] = []

    class _FakeBot:
        async def edit_message_text(self, **_kwargs) -> None:
            raise AssertionError("must not edit in debug mode")

        async def send_message(self, chat_id: int, text: str, **_kwargs) -> None:
            sent.append((chat_id, text))

    target = _MessageEditTarget(_FakeBot(), chat_id=555, message_id=1)
    monkeypatch.setattr(config.SETTINGS, "debug_append_only_messages", True)

    await safe_edit(target, "next set", None)

    assert sent == [(555, "next set")]


# ---------------------------------------------------------------------------
# Repeated press of a previously-visible state-changing callback must not
# duplicate persisted data / duplicate a sent message, whether or not debug
# mode is on (this is the real, storage-level guard, not a UI trick).
# ---------------------------------------------------------------------------


async def _make_db(tmp_path: Path) -> coach_bot.Database:
    db = coach_bot.Database(str(tmp_path / "coach.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (coach_bot.utc_now(),),
    )
    return db


@pytest.mark.asyncio
async def test_repeated_approve_meal_does_not_duplicate_persisted_meal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real meal-flow duplicate-click scenario: approve_meal: tapped twice
    (e.g. an old, still-visible button re-tapped in debug mode) must not
    insert the meal twice. persist_meal's compare-and-swap on
    approvals.status='pending' is the actual guard (not the UI)."""
    from models import FoodItem, MealAnalysis
    from noam_coach.bot import meals as meals_bot
    from noam_coach.services.core import create_approval

    db = await _make_db(tmp_path)
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(meals_bot, "DB", db)

    analysis = MealAnalysis(
        meal_name="חביתה",
        items=[FoodItem(name="ביצים", grams=100, calories=150, protein=12, carbs=1, fat=10, confidence=0.9)],
        confidence=0.9,
    )
    approval_id = await create_approval(
        1, "meal", {"analysis": analysis.model_dump(), "image": None, "source": "manual_text"},
    )

    first = await meals_bot.persist_meal(1, approval_id)
    second = await meals_bot.persist_meal(1, approval_id)

    assert first is not None
    assert second is None  # already-decided guard rejects the duplicate

    rows = await db.fetch_all("SELECT id FROM meals WHERE user_id=1")
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_repeated_plan_select_does_not_resend_pinnable_menu(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """planv2:select: tapped twice for the same (already-active) plan must
    not send a second standalone pinnable daily-menu message -- the
    already_active guard added alongside DEBUG_APPEND_ONLY_MESSAGES."""
    from noam_coach.bot import callback_plans

    db = await _make_db(tmp_path)
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(callback_plans, "DB", db)

    import json as _json

    plan_id_row = await db.execute(
        """
        INSERT INTO plan_versions(user_id, plan_type, title, strategy, payload, fit_score, status, created_at)
        VALUES(1, 'nutrition', 'Plan A', 'balanced', ?, 0.9, 'candidate', ?)
        """,
        (_json.dumps({"days": [{"meals": []}]}), coach_bot.utc_now()),
    )
    plan_id = int(plan_id_row)

    # This test targets the callback handler's own duplicate-send guard
    # (added alongside DEBUG_APPEND_ONLY_MESSAGES), not planning.activate_plan
    # itself (already covered by its own tests) -- stub it to a plain,
    # already-idempotent no-op so the plan-readiness prerequisite chain
    # (unrelated to this fix) doesn't need to be fully reconstructed here.
    # The stub mirrors the real function's effect on status so the handler's
    # OWN "was it already active before I called activate_plan" check (read
    # directly from plan_versions, not from this stub) behaves correctly
    # across the two calls below.
    plan_row = {
        "id": plan_id, "plan_type": "nutrition", "title": "Plan A",
        "strategy": "balanced", "fit_score": 0.9,
    }

    async def _fake_activate_plan(db_arg, _user_id, _plan_id):
        await db_arg.execute(
            "UPDATE plan_versions SET status='active' WHERE id=?", (plan_id,)
        )
        return plan_row

    monkeypatch.setattr(callback_plans.planning, "activate_plan", _fake_activate_plan)

    async def _fake_menu_text(_user_id: int) -> str:
        return "menu text"

    monkeypatch.setattr(coach_bot, "build_morning_menu_text", _fake_menu_text, raising=False)

    class _Query(_FakeQuery):
        pass

    query = _Query()
    handled_first = await callback_plans.handle_plan_callback(query, 1, f"planv2:select:{plan_id}")
    assert handled_first is True
    first_pin_sends = len(query.message.reply_calls)
    assert first_pin_sends >= 1

    query2 = _Query()
    handled_second = await callback_plans.handle_plan_callback(query2, 1, f"planv2:select:{plan_id}")
    assert handled_second is True
    # activate_plan() itself is idempotent (no duplicate plan_versions rows),
    # and the standalone pinnable menu is NOT re-sent for an already-active plan.
    assert query2.message.reply_calls == []

    active_rows = await db.fetch_all(
        "SELECT status FROM plan_versions WHERE id=?", (plan_id,)
    )
    assert [r["status"] for r in active_rows] == ["active"]


# ---------------------------------------------------------------------------
# End-to-end: at least one real onboarding flow in append-only mode.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_onboarding_question_flow_in_append_only_mode_sends_new_messages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real onboarding screen render (menu:home) in debug mode sends a new
    message instead of editing, end to end through handle_menu_callback."""
    from noam_coach.bot import callback_menu

    db = await _make_db(tmp_path)
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(callback_menu, "DB", db)
    monkeypatch.setattr(config.SETTINGS, "debug_append_only_messages", True)

    query = _FakeQuery()
    handled = await callback_menu.handle_menu_callback(query, 1, "menu:home")

    assert handled is True
    assert query.edit_calls == [], "menu:home must not edit the tapped message in debug mode"
    assert len(query.message.reply_calls) >= 1
