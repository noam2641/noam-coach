"""RE10-9 regression tests — guided goal wizard before the calorie proposal.

Covers:
  * Missing height/goal_weight/goal_timeframe are asked in order, one at a
    time, before the proposal screen is shown.
  * Once all three are known (or answered), menu:goal renders the proposal
    directly.
  * D3: repeated menu:goal taps supersede the previous pending goal approval
    instead of leaving several pending approvals dangling.
  * The manual calorie override is reachable via a button (goal:manual) and
    ends up on the standard confirm:goal_cal path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import coach_bot
import user_model
from db import Database
from helpers import utc_now
from noam_coach.bot import callback_plans as callback_plans_bot
from noam_coach.bot import onboarding as onboarding_bot
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

    async def reply_text(self, text: str, reply_markup: Any = None, parse_mode: str | None = None) -> None:
        del parse_mode
        self.messages.append(text)
        self.reply_markups.append(reply_markup)

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        del text, show_alert


async def _make_db(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "goal_wizard.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1, 'Test', NULL, ?)",
        (utc_now(),),
    )
    return db


async def _set(db: Database, key: str, value: Any) -> None:
    await user_model.set_fact(
        db, 1, key, value, source=user_model.SOURCE_USER, confirmed=True,
    )


async def _bootstrap_goal_ready(db: Database) -> None:
    """Facts needed for build_goal_proposal itself to succeed (weight/etc)."""
    for key, value in {
        "weight_kg": 90.0,
        "primary_goal": "fat_loss_muscle_retention",
        "sex": "male",
        "age": 32,
    }.items():
        await _set(db, key, value)


@pytest.mark.asyncio
async def test_menu_goal_asks_height_before_proposal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = await _make_db(tmp_path)
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(onboarding_bot, "DB", db)
    monkeypatch.setattr(core_services, "DB", db)
    monkeypatch.setattr(callback_plans_bot, "DB", db)
    await _bootstrap_goal_ready(db)

    target = FakeTarget()
    handled = await callback_plans_bot.handle_workout_setup_callback(target, None, 1, "menu:goal")

    assert handled is True
    assert "שאלה להשלמת היעד" in target.messages[-1]
    state = await core_services.get_flow_state(1, onboarding_bot.GOAL_WIZARD_FLOW)
    assert state is not None
    assert state["step"] == "q_height"


@pytest.mark.asyncio
async def test_goal_wizard_asks_goal_weight_then_timeframe_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path)
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(onboarding_bot, "DB", db)
    monkeypatch.setattr(core_services, "DB", db)
    monkeypatch.setattr(callback_plans_bot, "DB", db)
    await _bootstrap_goal_ready(db)
    await _set(db, "height_cm", 178)

    target = FakeTarget()
    await callback_plans_bot.handle_workout_setup_callback(target, None, 1, "menu:goal")
    state = await core_services.get_flow_state(1, onboarding_bot.GOAL_WIZARD_FLOW)
    assert state["step"] == "q_goal_weight"

    await _set(db, "goal_weight_kg", 85)
    target2 = FakeTarget()
    await callback_plans_bot.handle_workout_setup_callback(target2, None, 1, "menu:goal")
    state2 = await core_services.get_flow_state(1, onboarding_bot.GOAL_WIZARD_FLOW)
    assert state2["step"] == "q_goal_timeframe"


@pytest.mark.asyncio
async def test_menu_goal_renders_proposal_once_wizard_facts_all_known(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path)
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(onboarding_bot, "DB", db)
    monkeypatch.setattr(core_services, "DB", db)
    monkeypatch.setattr(callback_plans_bot, "DB", db)
    monkeypatch.setattr(coach_bot, "OPENAI_CLIENT", None)
    await _bootstrap_goal_ready(db)
    await _set(db, "height_cm", 178)
    await _set(db, "goal_weight_kg", 85)
    await _set(db, "goal_timeframe_weeks", 16)

    target = FakeTarget()
    handled = await callback_plans_bot.handle_workout_setup_callback(target, None, 1, "menu:goal")

    assert handled is True
    text = target.messages[-1]
    assert "יעד יומי" in text  # TASK-4: renamed from "הצעת יעד"
    assert "שנה קלוריות" in "".join(
        btn.text for row in target.reply_markups[-1].inline_keyboard for btn in row
    )
    # The wizard flow must be cleared once the proposal is shown.
    assert await core_services.get_flow_state(1, onboarding_bot.GOAL_WIZARD_FLOW) is None


@pytest.mark.asyncio
async def test_repeated_menu_goal_taps_supersede_old_pending_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D3: two consecutive menu:goal renders must not leave two pending approvals."""
    db = await _make_db(tmp_path)
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(onboarding_bot, "DB", db)
    monkeypatch.setattr(core_services, "DB", db)
    monkeypatch.setattr(callback_plans_bot, "DB", db)
    monkeypatch.setattr(coach_bot, "OPENAI_CLIENT", None)
    await _bootstrap_goal_ready(db)
    await _set(db, "height_cm", 178)
    await _set(db, "goal_weight_kg", 85)
    await _set(db, "goal_timeframe_weeks", 16)

    await callback_plans_bot.handle_workout_setup_callback(FakeTarget(), None, 1, "menu:goal")
    await callback_plans_bot.handle_workout_setup_callback(FakeTarget(), None, 1, "menu:goal")

    pending = await db.fetch_all(
        "SELECT id FROM approvals WHERE user_id=1 AND kind='goal' AND status='pending'"
    )
    assert len(pending) == 1


@pytest.mark.asyncio
async def test_goal_manual_button_leads_to_confirm_goal_cal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path)
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(onboarding_bot, "DB", db)
    monkeypatch.setattr(core_services, "DB", db)
    monkeypatch.setattr(callback_plans_bot, "DB", db)

    target = FakeTarget()
    handled = await callback_plans_bot.handle_workout_setup_callback(target, None, 1, "goal:manual")
    assert handled is True
    assert "יעד הקלוריות" in target.messages[-1]

    # Now simulate the user typing a number while __manual_goal_calories__ is pending.
    import conversation

    flow = await conversation.get_active_flow(db, 1)
    assert flow.step == "__manual_goal_calories__"

    class FakeUpdate:
        class effective_message:  # noqa: N801 - test shim mirrors telegram Update shape
            text = "2100"

            @staticmethod
            async def reply_text(text: str, reply_markup: Any = None, parse_mode: str | None = None) -> None:
                del text, reply_markup, parse_mode

    handled_text = await onboarding_bot.handle_onboarding_text(FakeUpdate(), 1)
    assert handled_text is True


async def _goal_screen_text_and_buttons(
    db: Database, monkeypatch: pytest.MonkeyPatch, *, weight: float, goal_weight: float, weeks: int
) -> tuple[str, list[str]]:
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(onboarding_bot, "DB", db)
    monkeypatch.setattr(core_services, "DB", db)
    monkeypatch.setattr(callback_plans_bot, "DB", db)
    monkeypatch.setattr(coach_bot, "OPENAI_CLIENT", None)
    for key, value in {
        "weight_kg": weight, "primary_goal": "fat_loss_muscle_retention",
        "sex": "male", "age": 32, "height_cm": 178,
        "goal_weight_kg": goal_weight, "goal_timeframe_weeks": weeks,
    }.items():
        await _set(db, key, value)
    # Persist + activate a goal so fetch_goal returns the computed target.
    import planning

    proposal = await planning.build_goal_proposal(db, 1)
    goal_id = await planning.persist_goal_proposal(db, 1, proposal)
    await planning.activate_goal(db, 1, goal_id)

    target = FakeTarget()
    await callback_plans_bot.handle_workout_setup_callback(target, None, 1, "menu:goal")
    text = target.messages[-1]
    labels = [btn.text for row in target.reply_markups[-1].inline_keyboard for btn in row]
    callbacks = [btn.callback_data for row in target.reply_markups[-1].inline_keyboard for btn in row]
    return text, callbacks


@pytest.mark.asyncio
async def test_infeasible_goal_shows_warning_and_decision_buttons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TASK-18: a heavy user with an aggressive 3-month target gets a feasibility
    warning and explicit decision options (extend timeline / change target)."""
    db = await _make_db(tmp_path)
    text, callbacks = await _goal_screen_text_and_buttons(
        db, monkeypatch, weight=101.8, goal_weight=83.0, weeks=13
    )
    assert "כנראה לא יושג" in text
    assert "onb:edit:goal_timeframe_weeks" in callbacks
    assert "onb:edit:goal_weight_kg" in callbacks


@pytest.mark.asyncio
async def test_feasible_goal_has_no_extra_decision_buttons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A gentle, achievable target does not add the timeline/target decision
    buttons and does not warn that the deadline is unreachable."""
    db = await _make_db(tmp_path)
    text, callbacks = await _goal_screen_text_and_buttons(
        db, monkeypatch, weight=90.0, goal_weight=87.0, weeks=16
    )
    assert "כנראה לא יושג" not in text
    assert "onb:edit:goal_timeframe_weeks" not in callbacks


@pytest.mark.asyncio
async def test_goal_screen_uses_daily_target_heading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TASK-4: the heading is "יעד יומי", not "הצעת יעד"."""
    db = await _make_db(tmp_path)
    text, _cb = await _goal_screen_text_and_buttons(
        db, monkeypatch, weight=90.0, goal_weight=87.0, weeks=16
    )
    assert "יעד יומי" in text
    assert "הצעת יעד" not in text


@pytest.mark.asyncio
async def test_infeasible_goal_is_decision_oriented(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TASK-4: an infeasible goal leads with a direct recommendation and offers
    explicit decisions (approve recommended / change weight / change timeline /
    change calories) instead of a vague "❌ דחה"."""
    db = await _make_db(tmp_path)
    text, callbacks = await _goal_screen_text_and_buttons(
        db, monkeypatch, weight=101.8, goal_weight=83.0, weeks=13
    )
    labels_src = callbacks  # callbacks list; check the text + callbacks
    del labels_src
    # Direct recommendation is shown.
    assert "ההמלצה שלי" in text
    assert "ביקשת להגיע" in text
    # Explicit decision callbacks; no reject_goal.
    assert any(cb.startswith("approve_goal:") for cb in callbacks)
    assert "onb:edit:goal_weight_kg" in callbacks
    assert "onb:edit:goal_timeframe_weeks" in callbacks
    assert "goal:manual" in callbacks
    assert not any(cb.startswith("reject_goal:") for cb in callbacks)
