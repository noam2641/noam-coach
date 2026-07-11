"""Post-HealthKit transition menu and unified training-limitations regressions."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import coach_bot
import planning
import questions
import training_intelligence
import user_model
from db import Database
from helpers import utc_now
from noam_coach.bot import callback_menu as callback_menu_bot
from noam_coach.bot import callback_plans as callback_plans_bot
from noam_coach.bot import onboarding as onboarding_bot
from noam_coach.bot import ui as ui_bot
from noam_coach.services import core as core_services
from noam_coach.services import health_jobs


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

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        del text, show_alert


async def _make_db(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "post_import_menu_limitations.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'Test',NULL,?)",
        (utc_now(),),
    )
    return db


def _patch_db(monkeypatch: pytest.MonkeyPatch, db: Database) -> None:
    for module in (
        coach_bot,
        ui_bot,
        health_jobs,
        onboarding_bot,
        callback_menu_bot,
        callback_plans_bot,
        core_services,
    ):
        monkeypatch.setattr(module, "DB", db)


def _callbacks(markup: Any) -> list[str]:
    return [button.callback_data for row in markup.inline_keyboard for button in row]


async def _set_fact(db: Database, key: str, value: Any, *, confirmed: bool = True) -> None:
    await user_model.set_fact(
        db,
        1,
        key,
        value,
        kind=user_model.KIND_FACT,
        source=user_model.SOURCE_USER,
        confirmed=confirmed,
    )


async def _seed_complete_profile(db: Database) -> None:
    await _set_fact(db, "primary_goal", "fat_loss")
    await _set_fact(db, "weight_kg", 101.8)
    await _set_fact(db, "sex", "male")
    await _set_fact(db, "age", 35)
    await _set_fact(db, "diet_restrictions", "none")
    await _set_fact(db, "allergies", "none")
    await _set_fact(db, "training_days_per_week", 4)
    await _set_fact(db, "session_minutes", 50)
    await _set_fact(db, "training_location", "gym")
    await _set_fact(db, "equipment", "full_gym")
    await _set_fact(db, "strength_experience", "intermediate")
    await _set_fact(
        db,
        "weekly_availability",
        [{"weekday": day, "available": True} for day in (6, 0, 2, 4)],
    )
    await _set_fact(db, "training_limitations", "none")


@pytest.mark.asyncio
async def test_post_import_missing_profile_shows_focused_menu(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await _set_fact(db, "weight_kg", 101.8)
    await _set_fact(db, "primary_goal", "fat_loss")

    async def no_reconciliation(message: Any, user_id: int) -> None:
        del message, user_id

    monkeypatch.setattr(health_jobs, "run_post_import_reconciliation", no_reconciliation)
    await core_services.set_flow_state(
        1,
        health_jobs.HEALTH_POST_WIZARD_FLOW,
        "reconciliation",
        {"summary_text": "<b>ייבוא Apple Health הושלם ✅</b>"},
    )

    target = FakeTarget()
    await health_jobs.finish_health_confirm_wizard(target, 1)

    text = target.messages[-1]
    readiness = await user_model.compute_all_readiness(db, 1)
    dynamic_missing = []
    for profile in ("workout", "nutrition", "safety"):
        for key in readiness[profile]["missing"]:
            if key not in dynamic_missing:
                dynamic_missing.append(key)

    assert "השלב הבא" in text
    assert f"נשארו לך {len(dynamic_missing)} פרטים להשלמה" in text
    assert "🎯 השלם את התוכנית שלי" in text
    callbacks = _callbacks(target.reply_markups[-1])
    assert callbacks == ["planv2:complete_missing", "menu:nextmeal", "menu:food", "menu:status"]
    assert "menu:health" not in callbacks
    assert "menu:chart" not in callbacks
    assert "menu:app" not in callbacks


@pytest.mark.asyncio
async def test_complete_my_plan_resumes_next_pending_question_and_skips_confirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await _seed_complete_profile(db)
    await user_model.defer_fact(db, 1, "training_limitations")
    await user_model.invalidate_fact(db, 1, "sex")

    target = FakeTarget()
    handled = await callback_plans_bot.handle_plan_callback(target, 1, "planv2:complete_missing")

    assert handled is True
    assert "שאלה להשלמת התוכנית" in target.messages[-1]
    assert "כאב, פציעה או מגבלה" in target.messages[-1]
    assert "כמה אימונים" not in target.messages[-1]
    state = await onboarding_bot.get_flow_state(1, onboarding_bot.PLAN_COMPLETION_FLOW)
    assert state is not None
    assert state["step"] == "safety_training_limitations"


@pytest.mark.asyncio
async def test_complete_profile_restores_normal_home_keyboard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await _seed_complete_profile(db)

    keyboard = await ui_bot.home_keyboard_for_user(1)
    callbacks = _callbacks(keyboard)

    assert "planv2:complete_missing" not in callbacks
    assert "menu:health" in callbacks
    assert "menu:chart" in callbacks


@pytest.mark.asyncio
async def test_unified_limitations_question_missing_and_answered_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)

    safety_questions = questions.SAFETY_QUESTIONS
    assert [q.fact_key for q in safety_questions] == ["training_limitations"]
    readiness = await user_model.compute_all_readiness(db, 1)
    assert readiness["safety"]["missing"] == ["training_limitations"]
    assert readiness["safety"]["missing_labels"] == ["כאב, פציעה או מגבלה"]

    await _set_fact(db, "training_limitations", "ברך ימין; להימנע מסקוואט עמוק")
    assert await questions.pending_safety_questions(db, 1) == []

    await user_model.defer_fact(db, 1, "training_limitations")
    readiness = await user_model.compute_all_readiness(db, 1)
    assert readiness["safety"]["deferred"] == ["training_limitations"]
    assert readiness["safety"]["missing"] == ["training_limitations"]


@pytest.mark.asyncio
async def test_legacy_pain_and_doctor_avoidance_merge_without_duplication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await _set_fact(db, "active_pain", {"location": "ברך ימין", "status": "active"})
    await _set_fact(db, "medical_avoidance", "ברך ימין; להימנע מסקוואט עמוק")

    fact = await user_model.get_training_limitations_fact(db, 1)

    assert fact is not None
    assert fact["key"] == "training_limitations"
    assert "ברך ימין" in fact["value"]
    assert "להימנע מסקוואט עמוק" in fact["value"]
    assert fact["value"].count("להימנע מסקוואט עמוק") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("legacy_key", "legacy_value", "expected"),
    [
        ("active_pain", {"location": "מרפק טניס", "status": "active"}, "מרפק טניס"),
        ("medical_avoidance", "להימנע מלחיצת כתפיים כבדה", "להימנע מלחיצת כתפיים כבדה"),
    ],
)
async def test_single_legacy_limitation_field_is_preserved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    legacy_key: str,
    legacy_value: Any,
    expected: str,
) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await _set_fact(db, legacy_key, legacy_value)

    fact = await user_model.get_training_limitations_fact(db, 1)

    assert fact is not None
    assert fact["key"] == "training_limitations"
    assert expected in fact["value"]


@pytest.mark.asyncio
async def test_planner_and_exercise_selection_read_canonical_limitations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await _seed_complete_profile(db)
    await _set_fact(db, "training_limitations", "כאבי ברכיים ומרפק טניס; להימנע מאחיזה כבדה")

    facts = await planning.collect_facts(db, 1)
    profile = training_intelligence.client_training_profile_from_facts(facts)
    adapted, changes = training_intelligence.adapt_exercises(
        [{"id": "leg_press", "name": "Leg Press"}],
        equipment_value="full_gym",
        location="gym",
        pain_value=facts["training_limitations"]["value"],
        medical_avoidance=facts["training_limitations"]["value"],
        experience="intermediate",
    )

    assert "knee" in profile.pain_areas
    assert "elbow" in profile.pain_areas
    assert changes
    assert adapted[0]["id"] != "leg_press"


@pytest.mark.asyncio
async def test_final_health_planning_summary_displays_one_limitation_concept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await _set_fact(db, "training_limitations", "מרפק טניס; להימנע מאחיזה כבדה")

    text = await health_jobs._health_confirmed_planning_summary_text(1)

    assert "כאב, פציעה או מגבלה" in text
    assert "מרפק טניס" in text
    assert "כאב/פציעה פעילה" not in text
    assert "הימנעות לפי רופא" not in text
