from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import coach_bot
import planning
import user_model
from db import Database
from helpers import utc_now
from noam_coach.bot import callback_menu as callback_menu_bot
from noam_coach.bot import callback_plans as callback_plans_bot
from noam_coach.bot import onboarding as onboarding_bot
from noam_coach.services import core as core_services
from noam_coach.services import goals as goal_services


class FakeTarget:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.reply_markups: list[Any] = []

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
    db = Database(str(tmp_path / "plan_completion.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1, 'Test', NULL, ?)",
        (utc_now(),),
    )
    return db


async def _set(db: Database, key: str, value: Any) -> None:
    await user_model.set_fact(
        db,
        1,
        key,
        value,
        source=user_model.SOURCE_USER,
        confirmed=True,
    )


async def _ready_except_sex_and_age(db: Database) -> None:
    values = {
        "primary_goal": "fat_loss_muscle_retention",
        "training_days_per_week": 3,
        "active_pain": "none",
        "medical_avoidance": "none",
        "session_minutes": 45,
        "training_location": "gym",
        "equipment": "full gym",
        "strength_experience": "intermediate",
        "weekly_availability": [{"weekday": 0, "start": "18:00", "minutes": 45, "available": True}],
        "weight_kg": 80,
        "diet_restrictions": "none",
        "allergies": "none",
    }
    for key, value in values.items():
        await _set(db, key, value)


@pytest.mark.asyncio
async def test_plan_menu_clears_completion_flow_without_name_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = await _make_db(tmp_path)
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(onboarding_bot, "DB", db)
    monkeypatch.setattr(core_services, "DB", db)
    monkeypatch.setattr(callback_plans_bot, "DB", db)

    rendered: list[int] = []

    async def fake_render_smart_plan_hub(_query: Any, user_id: int) -> None:
        rendered.append(user_id)

    monkeypatch.setattr(coach_bot, "render_smart_plan_hub", fake_render_smart_plan_hub)
    monkeypatch.setattr(callback_plans_bot, "render_smart_plan_hub", fake_render_smart_plan_hub, raising=False)

    await core_services.set_flow_state(
        1,
        onboarding_bot.PLAN_COMPLETION_FLOW,
        "sex",
        {"fact_key": "sex"},
    )

    handled = await callback_plans_bot.handle_plan_callback(FakeTarget(), 1, "menu:smartplan")

    assert handled is True
    assert rendered == [1]
    assert await core_services.get_flow_state(1, onboarding_bot.PLAN_COMPLETION_FLOW) is None


@pytest.mark.asyncio
async def test_complete_missing_callback_starts_continuous_completion_flow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = await _make_db(tmp_path)
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(onboarding_bot, "DB", db)
    monkeypatch.setattr(core_services, "DB", db)
    monkeypatch.setattr(callback_plans_bot, "DB", db)
    await _ready_except_sex_and_age(db)

    target = FakeTarget()
    handled = await callback_plans_bot.handle_plan_callback(target, 1, "planv2:complete_missing")

    assert handled is True
    state = await core_services.get_flow_state(1, onboarding_bot.PLAN_COMPLETION_FLOW)
    assert state is not None
    assert state["step"] == "q_sex"

    sex_question = onboarding_bot.questions.question_by_fact_key("sex")
    assert sex_question is not None
    await onboarding_bot.questions.record_answer(db, 1, sex_question, "male")
    await onboarding_bot.clear_pending(1)
    continued = await onboarding_bot.continue_after_plan_completion_answer(target, 1)

    assert continued is True
    state = await core_services.get_flow_state(1, onboarding_bot.PLAN_COMPLETION_FLOW)
    assert state is not None
    assert state["step"] == "q_age"


@pytest.mark.asyncio
async def test_missing_active_goal_is_shown_upfront_without_calling_generate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RE10-8: an unapproved goal is now detected on the FIRST nutrition
    screen (before ever calling generate_candidates), instead of only
    surfacing after the user completes the other facts and hits a second
    PlanningBlockedError. See test below for that older two-screen bug case,
    which is now unreachable for active_goal specifically but is still
    exercised here for readiness-based blocks that aren't pre-checked.
    """
    db = await _make_db(tmp_path)
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(onboarding_bot, "DB", db)
    monkeypatch.setattr(core_services, "DB", db)
    monkeypatch.setattr(callback_plans_bot, "DB", db)

    async def ready(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"ready": True, "missing": [], "missing_labels": []}

    async def never_called(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("generate_candidates must not run while active_goal is missing")

    async def no_goal(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(user_model, "compute_readiness", ready)
    monkeypatch.setattr(planning, "generate_candidates", never_called)
    monkeypatch.setattr(planning, "active_goal", no_goal)

    target = FakeTarget()
    handled = await callback_plans_bot.handle_plan_callback(target, 1, "planv2:generate:nutrition")

    assert handled is True
    text = target.messages[-1]
    assert "חסרים פרטים" in text
    assert "active_goal" not in text
    assert "יעד יומי מאושר" in text
    markup = target.reply_markups[-1]
    button_labels = [btn.text for row in markup.inline_keyboard for btn in row]
    assert any("אשר יעד ואז נמשיך" in label for label in button_labels)


@pytest.mark.asyncio
async def test_planning_blocked_generation_saves_pending_action_without_error_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PlanningBlockedError raised by generate_candidates itself (for a
    reason that is NOT pre-checked upfront, e.g. readiness) must still be
    caught gracefully and never logged at ERROR level.
    """
    db = await _make_db(tmp_path)
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(onboarding_bot, "DB", db)
    monkeypatch.setattr(core_services, "DB", db)
    monkeypatch.setattr(callback_plans_bot, "DB", db)

    async def ready(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"ready": True, "missing": [], "missing_labels": []}

    async def has_goal(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"id": 1, "status": "active"}

    async def blocked(*_args: Any, **_kwargs: Any) -> None:
        raise planning.PlanningBlockedError("לא נמצאו מספיק נתונים לבניית תוכנית", missing=["some_other_key"])

    class NoErrorLogger:
        def exception(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("business planning blocks must not be logged as ERROR")

    monkeypatch.setattr(user_model, "compute_readiness", ready)
    monkeypatch.setattr(planning, "active_goal", has_goal)
    monkeypatch.setattr(planning, "generate_candidates", blocked)
    monkeypatch.setattr(coach_bot, "LOGGER", NoErrorLogger())

    target = FakeTarget()
    handled = await callback_plans_bot.handle_plan_callback(target, 1, "planv2:generate:nutrition")

    assert handled is True
    assert "אי אפשר לבנות" in target.messages[-1]
    state = await core_services.get_flow_state(1, callback_plans_bot.PENDING_PLAN_ACTION_FLOW)
    assert state is not None
    assert state["step"] == callback_plans_bot.PENDING_PLAN_GENERATE_STEP
    assert state["payload"]["plan_type"] == "nutrition"


@pytest.mark.asyncio
async def test_resume_pending_plan_action_generates_and_clears_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = await _make_db(tmp_path)
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(onboarding_bot, "DB", db)
    monkeypatch.setattr(core_services, "DB", db)
    monkeypatch.setattr(callback_plans_bot, "DB", db)
    await core_services.set_flow_state(
        1,
        callback_plans_bot.PENDING_PLAN_ACTION_FLOW,
        callback_plans_bot.PENDING_PLAN_GENERATE_STEP,
        {"plan_type": "nutrition"},
    )
    generated: list[str] = []
    rendered: list[str] = []

    async def generate(_db: Database, _user_id: int, plan_type: str) -> None:
        generated.append(plan_type)

    async def render(_query: Any, _user_id: int, plan_type: str) -> None:
        rendered.append(plan_type)

    monkeypatch.setattr(planning, "generate_candidates", generate)
    monkeypatch.setattr(coach_bot, "render_candidate_list", render)
    monkeypatch.setattr(callback_plans_bot, "render_candidate_list", render, raising=False)

    resumed = await callback_plans_bot.resume_pending_plan_action(FakeTarget(), 1)

    assert resumed is True
    assert generated == ["nutrition"]
    assert rendered == ["nutrition"]
    assert await core_services.get_flow_state(1, callback_plans_bot.PENDING_PLAN_ACTION_FLOW) is None


@pytest.mark.asyncio
async def test_goal_approval_resumes_pending_plan_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = await _make_db(tmp_path)
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(onboarding_bot, "DB", db)
    monkeypatch.setattr(core_services, "DB", db)
    monkeypatch.setattr(goal_services, "DB", db)
    monkeypatch.setattr(callback_menu_bot, "DB", db)
    monkeypatch.setattr(callback_plans_bot, "DB", db)
    approval_id = await core_services.create_approval(
        1,
        "goal",
        {
            "calories": 2200,
            "protein": 150,
            "steps": 9000,
            "phase": "fat_loss_muscle_retention",
            "provisional": False,
            "explanation": "test",
        },
    )
    resumed: list[int] = []

    async def activate(_db: Database, _user_id: int, _goal_id: int) -> bool:
        return True

    async def resume(_query: Any, user_id: int) -> bool:
        resumed.append(user_id)
        return True

    monkeypatch.setattr(planning, "activate_goal", activate)
    monkeypatch.setattr(callback_plans_bot, "resume_pending_plan_action", resume)

    handled = await callback_menu_bot.handle_goal_callback(FakeTarget(), 1, f"approve_goal:{approval_id}")

    assert handled is True
    assert resumed == [1]


@pytest.mark.asyncio
async def test_provisional_goal_approval_resumes_pending_plan_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M-NEW-2: approving a *provisional* goal must auto-continue the blocked plan.

    Full integration: blocked plan generation -> pending action saved ->
    provisional goal approved -> candidates generated automatically, pending
    state cleared, user never left on the provisional confirmation screen.
    """
    db = await _make_db(tmp_path)
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(onboarding_bot, "DB", db)
    monkeypatch.setattr(core_services, "DB", db)
    monkeypatch.setattr(goal_services, "DB", db)
    monkeypatch.setattr(callback_menu_bot, "DB", db)
    monkeypatch.setattr(callback_plans_bot, "DB", db)

    # 1) Plan generation is blocked because there is no active goal.
    async def ready(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"ready": True, "missing": [], "missing_labels": []}

    calls: list[str] = []

    async def generate(_db: Database, _user_id: int, plan_type: str) -> None:
        # First call (before a goal exists) blocks; after a provisional goal is
        # active it succeeds.
        if await planning.active_goal(db, 1) is None:
            raise planning.PlanningBlockedError(
                "צריך לאשר יעד לפני יצירת תוכנית תזונה", missing=["active_goal"]
            )
        calls.append(plan_type)

    rendered: list[str] = []

    async def render(_query: Any, _user_id: int, plan_type: str) -> None:
        rendered.append(plan_type)

    monkeypatch.setattr(user_model, "compute_readiness", ready)
    monkeypatch.setattr(planning, "generate_candidates", generate)
    monkeypatch.setattr(coach_bot, "render_candidate_list", render)
    monkeypatch.setattr(callback_plans_bot, "render_candidate_list", render, raising=False)

    target = FakeTarget()
    await callback_plans_bot.handle_plan_callback(target, 1, "planv2:generate:nutrition")
    pending = await core_services.get_flow_state(1, callback_plans_bot.PENDING_PLAN_ACTION_FLOW)
    assert pending is not None
    assert pending["payload"]["plan_type"] == "nutrition"

    # 2) Approve a PROVISIONAL goal.
    approval_id = await core_services.create_approval(
        1,
        "goal",
        {
            "calories": 2100,
            "protein": 150,
            "steps": 9000,
            "phase": "fat_loss_muscle_retention",
            "provisional": True,
            "explanation": "test provisional",
        },
    )
    handled = await callback_menu_bot.handle_goal_callback(target, 1, f"approve_goal:{approval_id}")

    # 3) The blocked nutrition plan continued automatically.
    assert handled is True
    assert calls == ["nutrition"]
    assert rendered == ["nutrition"]
    assert await core_services.get_flow_state(1, callback_plans_bot.PENDING_PLAN_ACTION_FLOW) is None
    assert await planning.active_goal(db, 1) is not None


@pytest.mark.asyncio
async def test_provisional_goal_approval_without_pending_action_shows_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No pending plan action -> normal provisional confirmation, no crash."""
    db = await _make_db(tmp_path)
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(onboarding_bot, "DB", db)
    monkeypatch.setattr(core_services, "DB", db)
    monkeypatch.setattr(goal_services, "DB", db)
    monkeypatch.setattr(callback_menu_bot, "DB", db)
    monkeypatch.setattr(callback_plans_bot, "DB", db)

    approval_id = await core_services.create_approval(
        1,
        "goal",
        {
            "calories": 2100,
            "protein": 150,
            "steps": 9000,
            "phase": "fat_loss_muscle_retention",
            "provisional": True,
            "explanation": "test provisional",
        },
    )
    target = FakeTarget()
    handled = await callback_menu_bot.handle_goal_callback(target, 1, f"approve_goal:{approval_id}")

    assert handled is True
    assert "זמני" in target.messages[-1]
    assert await core_services.get_flow_state(1, callback_plans_bot.PENDING_PLAN_ACTION_FLOW) is None


@pytest.mark.asyncio
async def test_complete_missing_plan_details_continues_until_plan_hub(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = await _make_db(tmp_path)
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(onboarding_bot, "DB", db)
    monkeypatch.setattr(core_services, "DB", db)
    await _ready_except_sex_and_age(db)
    target = FakeTarget()

    asked = await onboarding_bot.ask_next_plan_completion_question(target, 1)
    assert asked is True
    assert "מין" in target.messages[-1] or "המין" in target.messages[-1]

    sex_question = onboarding_bot.questions.question_by_fact_key("sex")
    assert sex_question is not None
    await onboarding_bot.questions.record_answer(db, 1, sex_question, "male")
    await onboarding_bot.clear_pending(1)
    continued = await onboarding_bot.continue_after_plan_completion_answer(target, 1)

    assert continued is True
    assert "בן כמה" in target.messages[-1] or "גיל" in target.messages[-1]

    age_question = onboarding_bot.questions.question_by_fact_key("age")
    assert age_question is not None
    await onboarding_bot.questions.record_answer(db, 1, age_question, 30)
    await onboarding_bot.clear_pending(1)
    continued = await onboarding_bot.continue_after_plan_completion_answer(target, 1)

    assert continued is True
    assert "מרכז" in target.messages[-1] or "תוכנית" in target.messages[-1]
    assert await core_services.get_flow_state(1, onboarding_bot.PLAN_COMPLETION_FLOW) is None
