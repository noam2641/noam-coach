"""RE10-11 regression tests — three-step workout wizard (type -> structure -> exercises).

Covers:
  * planv2:generate:workout lands on the type-choice screen, not the flat list.
  * The recommended strategy badge follows primary_goal.
  * type -> structure -> exercise-review transitions carry the right plan.
  * "back" buttons return to the previous step.
  * The final "אשר תוכנית" button reuses the existing planv2:select activation path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import coach_bot
import conversation
import planning
import user_model
from db import Database
from helpers import utc_now
from noam_coach.bot import callback_plans as callback_plans_bot
from noam_coach.bot import onboarding as onboarding_bot
from noam_coach.services import core as core_services

_STRATEGY_LABELS_FOR_TEST = {
    "consistency": "מקסימום עקביות",
    "balanced": "מאוזנת",
    "performance": "ביצועים",
}


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


def _button_labels(markup: Any) -> list[str]:
    return [btn.text for row in markup.inline_keyboard for btn in row]


def _button_callback(markup: Any, label_substring: str) -> str:
    for row in markup.inline_keyboard:
        for btn in row:
            if label_substring in btn.text:
                return btn.callback_data
    raise AssertionError(f"no button containing {label_substring!r}")


async def _ready_db(tmp_path: Path, *, primary_goal: str = "fat_loss_muscle_retention") -> Database:
    db = Database(str(tmp_path / "wizard.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'Test',NULL,?)",
        (utc_now(),),
    )
    facts = {
        "weight_kg": 90, "height_cm": 174, "age": 32, "sex": "male",
        "primary_goal": primary_goal,
        "diet_restrictions": "none", "allergies": "none",
        "training_days_per_week": 3, "active_pain": "none", "medical_avoidance": "none",
        "session_minutes": 50, "training_location": "gym", "equipment": "full_gym",
        "strength_experience": "intermediate",
        "weekly_availability": [
            {"weekday": d, "start": "19:00", "minutes": 50} for d in [0, 2, 4]
        ],
    }
    for key, value in facts.items():
        await user_model.set_fact(db, 1, key, value, source=user_model.SOURCE_USER, confirmed=True)
    return db


@pytest.mark.asyncio
async def test_generate_workout_lands_on_type_choice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = await _ready_db(tmp_path)
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(onboarding_bot, "DB", db)
    monkeypatch.setattr(core_services, "DB", db)
    monkeypatch.setattr(callback_plans_bot, "DB", db)

    target = FakeTarget()
    handled = await callback_plans_bot.handle_plan_callback(target, 1, "planv2:generate:workout")

    assert handled is True
    assert "שלב 1 מתוך 3" in target.messages[-1]
    labels = _button_labels(target.reply_markups[-1])
    assert any("מקסימום עקביות" in label for label in labels)
    assert any("מאוזנת" in label for label in labels)
    assert any("ביצועים" in label for label in labels)


@pytest.mark.asyncio
async def test_fat_loss_goal_recommends_consistency(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = await _ready_db(tmp_path, primary_goal="fat_loss_muscle_retention")
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(onboarding_bot, "DB", db)
    monkeypatch.setattr(core_services, "DB", db)
    monkeypatch.setattr(callback_plans_bot, "DB", db)
    await planning.generate_candidates(db, 1, "workout")

    target = FakeTarget()
    await onboarding_bot.render_workout_type_choice(target, 1)
    labels = _button_labels(target.reply_markups[-1])
    starred = [label for label in labels if "⭐" in label]
    assert len(starred) == 1
    assert any("מקסימום עקביות" in label and "⭐" in label for label in labels)
    top = (await planning.list_plan_candidates(db, 1, "workout"))[0]
    assert _STRATEGY_LABELS_FOR_TEST[top["strategy"]] in starred[0]


@pytest.mark.asyncio
async def test_muscle_gain_goal_recommends_performance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = await _ready_db(tmp_path, primary_goal="muscle_gain")
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(onboarding_bot, "DB", db)
    monkeypatch.setattr(core_services, "DB", db)
    monkeypatch.setattr(callback_plans_bot, "DB", db)
    await planning.generate_candidates(db, 1, "workout")

    target = FakeTarget()
    await onboarding_bot.render_workout_type_choice(target, 1)
    labels = _button_labels(target.reply_markups[-1])
    assert any("ביצועים" in label and "⭐" in label for label in labels)


@pytest.mark.asyncio
async def test_type_to_structure_to_review_flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = await _ready_db(tmp_path)
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(onboarding_bot, "DB", db)
    monkeypatch.setattr(core_services, "DB", db)
    monkeypatch.setattr(callback_plans_bot, "DB", db)
    await planning.generate_candidates(db, 1, "workout")

    # Step A
    step_a = FakeTarget()
    await onboarding_bot.render_workout_type_choice(step_a, 1)
    type_cb = _button_callback(step_a.reply_markups[-1], "מאוזנת")
    assert type_cb.startswith("planv2:wiz_type:balanced")

    # Step A -> B
    step_b = FakeTarget()
    handled = await callback_plans_bot.handle_plan_callback(step_b, 1, type_cb)
    assert handled is True
    assert "שלב 2 מתוך 3" in step_b.messages[-1]
    review_cb = _button_callback(step_b.reply_markups[-1], "המשך לאישור תרגילים")
    assert review_cb.startswith("planv2:wiz_review:")

    flow = await conversation.get_active_flow(db, 1)
    assert flow.payload.get("chosen_strategy") == "balanced"

    # Step B -> C
    step_c = FakeTarget()
    handled = await callback_plans_bot.handle_plan_callback(step_c, 1, review_cb)
    assert handled is True
    assert "שלב 3 מתוך 3" in step_c.messages[-1]
    confirm_cb = _button_callback(step_c.reply_markups[-1], "אשר תוכנית")
    assert confirm_cb.startswith("planv2:select:")

    # Step C -> activation (reuses the existing planv2:select path)
    step_d = FakeTarget()
    handled = await callback_plans_bot.handle_plan_callback(step_d, 1, confirm_cb)
    assert handled is True
    active = await planning.get_active_plan(db, 1, "workout")
    assert active is not None
    assert active["strategy"] == "balanced"


@pytest.mark.asyncio
async def test_back_from_structure_returns_to_type_choice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = await _ready_db(tmp_path)
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(onboarding_bot, "DB", db)
    monkeypatch.setattr(core_services, "DB", db)
    monkeypatch.setattr(callback_plans_bot, "DB", db)
    await planning.generate_candidates(db, 1, "workout")

    step_b = FakeTarget()
    await onboarding_bot.render_workout_structure_choice(step_b, 1, "consistency")
    back_cb = _button_callback(step_b.reply_markups[-1], "חזרה לבחירת סוג")
    assert back_cb.startswith("planv2:wiz_back_type:")

    step_a_again = FakeTarget()
    handled = await callback_plans_bot.handle_plan_callback(step_a_again, 1, back_cb)
    assert handled is True
    assert "שלב 1 מתוך 3" in step_a_again.messages[-1]


@pytest.mark.asyncio
async def test_all_sessions_shown_in_structure_step_no_truncation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D6: even a 5/6-day performance plan must show every session."""
    db = await _ready_db(tmp_path)
    await user_model.set_fact(db, 1, "training_days_per_week", 5, source=user_model.SOURCE_USER, confirmed=True)
    await user_model.set_fact(
        db, 1, "weekly_availability",
        [{"weekday": d, "start": "19:00", "minutes": 50} for d in [0, 1, 2, 3, 4, 5]],
        source=user_model.SOURCE_USER, confirmed=True,
    )
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(onboarding_bot, "DB", db)
    monkeypatch.setattr(core_services, "DB", db)
    monkeypatch.setattr(callback_plans_bot, "DB", db)
    await planning.generate_candidates(db, 1, "workout")

    candidates = await planning.list_plan_candidates(db, 1, "workout")
    performance = next(c for c in candidates if c["strategy"] == "performance")
    expected_sessions = len(performance["payload"]["sessions"])
    assert expected_sessions >= 5  # headroom actually applied

    target = FakeTarget()
    await onboarding_bot.render_workout_structure_choice(target, 1, "performance")
    text = target.messages[-1]
    assert text.count("📋") == expected_sessions


# ---------------------------------------------------------------------------
# TASK-19 — type wizard shows every supported strategy regardless of how many
# candidate rows are stored.
# ---------------------------------------------------------------------------


def _type_button_callbacks(markup: Any) -> list[str]:
    return [
        btn.callback_data
        for row in markup.inline_keyboard
        for btn in row
        if btn.callback_data.startswith("planv2:wiz_type:")
    ]


@pytest.mark.asyncio
async def test_type_wizard_shows_all_three_with_only_one_candidate_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bug: with a single stored candidate the wizard used to show one type.
    Now it must still expose all three supported strategies."""
    db = await _ready_db(tmp_path)
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(onboarding_bot, "DB", db)
    monkeypatch.setattr(core_services, "DB", db)
    monkeypatch.setattr(callback_plans_bot, "DB", db)
    await planning.generate_candidates(db, 1, "workout")
    # Leave only ONE candidate row (consistency) in the DB.
    await db.execute(
        "UPDATE plan_versions SET status='superseded' "
        "WHERE user_id=1 AND plan_type='workout' AND status='candidate' AND strategy!='consistency'"
    )
    remaining = await planning.list_plan_candidates(db, 1, "workout")
    assert len(remaining) == 1

    target = FakeTarget()
    await onboarding_bot.render_workout_type_choice(target, 1)
    labels = _button_labels(target.reply_markups[-1])
    assert any("מקסימום עקביות" in label for label in labels)
    assert any("מאוזנת" in label for label in labels)
    assert any("ביצועים" in label for label in labels)
    callbacks = _type_button_callbacks(target.reply_markups[-1])
    assert [cb.split(":", 2)[2].split(":", 1)[0] for cb in callbacks] == [
        "consistency",
        "balanced",
        "performance",
    ]


@pytest.mark.asyncio
async def test_type_wizard_shows_all_three_with_three_candidate_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _ready_db(tmp_path)
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(onboarding_bot, "DB", db)
    monkeypatch.setattr(core_services, "DB", db)
    monkeypatch.setattr(callback_plans_bot, "DB", db)
    await planning.generate_candidates(db, 1, "workout")

    target = FakeTarget()
    await onboarding_bot.render_workout_type_choice(target, 1)
    strategies = [cb.split(":", 2)[2].split(":", 1)[0] for cb in _type_button_callbacks(target.reply_markups[-1])]
    assert strategies == ["consistency", "balanced", "performance"]


@pytest.mark.asyncio
async def test_type_wizard_marks_exactly_one_recommended_and_others_selectable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _ready_db(tmp_path, primary_goal="muscle_gain")
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(onboarding_bot, "DB", db)
    monkeypatch.setattr(core_services, "DB", db)
    monkeypatch.setattr(callback_plans_bot, "DB", db)
    await planning.generate_candidates(db, 1, "workout")

    target = FakeTarget()
    await onboarding_bot.render_workout_type_choice(target, 1)
    labels = _button_labels(target.reply_markups[-1])
    starred = [label for label in labels if "⭐" in label]
    assert len(starred) == 1
    assert any("ביצועים" in label and "⭐" in label for label in labels)
    # The other two strategies remain selectable (have callbacks).
    callbacks = _type_button_callbacks(target.reply_markups[-1])
    assert any("consistency" in cb for cb in callbacks)
    assert any("balanced" in cb for cb in callbacks)


@pytest.mark.asyncio
async def test_selecting_non_recommended_strategy_reaches_structure_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-recommended, and even a not-currently-stored, strategy must reach
    the structure step (regenerating candidates if needed) — never loop back."""
    db = await _ready_db(tmp_path, primary_goal="fat_loss_muscle_retention")
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(onboarding_bot, "DB", db)
    monkeypatch.setattr(core_services, "DB", db)
    monkeypatch.setattr(callback_plans_bot, "DB", db)
    await planning.generate_candidates(db, 1, "workout")
    # Remove the performance candidate row so the strategy has no stored plan.
    await db.execute(
        "UPDATE plan_versions SET status='superseded' "
        "WHERE user_id=1 AND plan_type='workout' AND status='candidate' AND strategy='performance'"
    )

    target = FakeTarget()
    await onboarding_bot.render_workout_type_choice(target, 1)
    perf_cb = _button_callback(target.reply_markups[-1], "ביצועים")

    step_b = FakeTarget()
    handled = await callback_plans_bot.handle_plan_callback(step_b, 1, perf_cb)
    assert handled is True
    assert "שלב 2 מתוך 3" in step_b.messages[-1]
    flow = await conversation.get_active_flow(db, 1)
    assert flow.payload.get("chosen_strategy") == "performance"


def test_generation_telemetry_is_not_hardcoded_to_three() -> None:
    src = (Path(__file__).resolve().parents[2] / "noam_coach/bot/callback_plans.py").read_text(encoding="utf-8")
    assert '"count": 3' not in src
    assert '"count": len(generated or [])' in src
