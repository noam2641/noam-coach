"""TASK-3 — every workout strategy button works + A/B/C/D explanations.

Problem A: pressing any strategy (incl. "מאוזנת") must advance to step 2 with an
acknowledgement and no silent callback failure.
Problem B: each strategy shows a consistent A/B/C/D comparison (weekly
structure / workout character / progression / best fit).
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


class FakeTarget:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.reply_markups: list[Any] = []
        self.message = self

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


async def _ready_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Database:
    db = Database(str(tmp_path / "task3.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'Test',NULL,?)",
        (utc_now(),),
    )
    for mod in (coach_bot, onboarding_bot, callback_plans_bot, core_services, planning):
        monkeypatch.setattr(mod, "DB", db, raising=False)
    facts = {
        "weight_kg": 90, "height_cm": 174, "age": 32, "sex": "male",
        "primary_goal": "fat_loss_muscle_retention", "diet_restrictions": "none", "allergies": "none",
        "training_days_per_week": 3, "active_pain": "none", "medical_avoidance": "none",
        "session_minutes": 50, "training_location": "gym", "equipment": "full_gym",
        "strength_experience": "intermediate",
        "weekly_availability": [{"weekday": d, "start": "19:00", "minutes": 50} for d in [0, 2, 4]],
    }
    for k, v in facts.items():
        await user_model.set_fact(db, 1, k, v, source=user_model.SOURCE_USER, confirmed=True)
    await planning.generate_candidates(db, 1, "workout")
    return db


def _wiz_type_callback(markup: Any, strategy: str) -> str:
    for row in markup.inline_keyboard:
        for btn in row:
            cb = btn.callback_data
            if cb.startswith("planv2:wiz_type:") and cb.split(":", 2)[2].split(":")[0] == strategy:
                return cb
    raise AssertionError(f"no wiz_type button for {strategy}")


@pytest.mark.parametrize("strategy", ["consistency", "balanced", "performance"])
@pytest.mark.asyncio
async def test_every_strategy_advances_to_step2_with_ack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, strategy: str
) -> None:
    db = await _ready_db(tmp_path, monkeypatch)
    step_a = FakeTarget()
    await onboarding_bot.render_workout_type_choice(step_a, 1)
    cb = _wiz_type_callback(step_a.reply_markups[-1], strategy)

    step_b = FakeTarget()
    handled = await callback_plans_bot.handle_plan_callback(step_b, 1, cb)
    assert handled is True
    text = step_b.messages[-1]
    assert "שלב 2 מתוך 3" in text  # advanced
    assert "בחרת" in text  # acknowledged
    # Persisted the chosen strategy in the flow.
    flow = await conversation.get_active_flow(db, 1)
    assert flow.payload.get("chosen_strategy") == strategy


@pytest.mark.asyncio
async def test_missing_candidate_shows_actionable_message_not_silent_bounce(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: selecting מאוזנת/ביצועים must never silently re-render step 1
    (a dead button). When no candidate can be produced for the chosen strategy,
    the user gets an explicit message + a retry action."""
    db = await _ready_db(tmp_path, monkeypatch)
    step_a = FakeTarget()
    await onboarding_bot.render_workout_type_choice(step_a, 1)
    cb = _wiz_type_callback(step_a.reply_markups[-1], "balanced")

    # Remove the balanced candidate AND make regeneration a no-op so the
    # strategy genuinely has no candidate row (the failure mode).
    await db.execute(
        "UPDATE plan_versions SET status='superseded' "
        "WHERE user_id=1 AND plan_type='workout' AND strategy='balanced'"
    )

    async def _noop_generate(_db, _uid, _ptype):
        return []

    monkeypatch.setattr(planning, "generate_candidates", _noop_generate)

    step_b = FakeTarget()
    handled = await callback_plans_bot.handle_plan_callback(step_b, 1, cb)
    assert handled is True
    text = step_b.messages[-1]
    # Not a silent step-1 refresh — an explicit message + a retry action.
    assert "שלב 1 מתוך 3" not in text
    labels = [btn.text for row in step_b.reply_markups[-1].inline_keyboard for btn in row]
    assert any("בנה מחדש" in x or "בחירת סוג" in x or "השלם" in x for x in labels)


@pytest.mark.asyncio
async def test_type_screen_shows_abcd_explanation_for_each_strategy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _ready_db(tmp_path, monkeypatch)
    del db
    target = FakeTarget()
    await onboarding_bot.render_workout_type_choice(target, 1)
    text = target.messages[-1]
    # The A/B/C/D comparison headers appear.
    assert "מבנה שבועי" in text
    assert "אופי האימון" in text
    assert "התקדמות" in text
    assert "למי מתאים" in text
    # Grounded, strategy-specific content.
    assert "Upper/Lower" in text  # balanced weekly structure
    assert "ABC" in text  # performance weekly structure
    assert "גוף מלא" in text  # consistency character


async def _ready_db_limited(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Database:
    """A limited profile (outdoor / no equipment / short sessions) where a
    split-heavy strategy cannot be built and would otherwise dead-end."""
    db = Database(str(tmp_path / "task3_limited.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'Test',NULL,?)",
        (utc_now(),),
    )
    for mod in (coach_bot, onboarding_bot, callback_plans_bot, core_services, planning):
        monkeypatch.setattr(mod, "DB", db, raising=False)
    facts = {
        "weight_kg": 90, "height_cm": 174, "age": 32, "sex": "male",
        "primary_goal": "muscle_gain", "diet_restrictions": "none", "allergies": "none",
        "training_days_per_week": 4, "active_pain": "none", "medical_avoidance": "none",
        "session_minutes": 30, "training_location": "outdoor", "equipment": "none",
        "strength_experience": "intermediate",
        "weekly_availability": [{"weekday": d, "start": "19:00", "minutes": 30} for d in [0, 1, 2, 3]],
    }
    for k, v in facts.items():
        await user_model.set_fact(db, 1, k, v, source=user_model.SOURCE_USER, confirmed=True)
    await planning.generate_candidates(db, 1, "workout")
    return db


@pytest.mark.asyncio
async def test_only_buildable_strategies_are_offered_and_all_advance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: a strategy that cannot be built for the user's profile must
    not be offered as a dead button. Every OFFERED strategy must advance to
    step 2 (no "לא נמצאה תוכנית" dead-end)."""
    db = await _ready_db_limited(tmp_path, monkeypatch)
    del db
    step_a = FakeTarget()
    await onboarding_bot.render_workout_type_choice(step_a, 1)
    callbacks = [
        btn.callback_data
        for row in step_a.reply_markups[-1].inline_keyboard
        for btn in row
        if btn.callback_data.startswith("planv2:wiz_type:")
    ]
    offered = [cb.split(":", 2)[2].split(":", 1)[0] for cb in callbacks]
    # consistency (full body) is always buildable; at least one split may be dropped.
    assert "consistency" in offered
    assert len(offered) >= 1
    # Every offered strategy actually advances to step 2.
    for cb in callbacks:
        step_b = FakeTarget()
        handled = await callback_plans_bot.handle_plan_callback(step_b, 1, cb)
        assert handled is True
        assert "שלב 2 מתוך 3" in step_b.messages[-1]


async def _ready_db_injured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Database:
    """A strong profile (full gym / advanced / 4 days) with an elbow limitation
    (tennis elbow). Every upper-body push/pull loads the elbow, so without
    cross-pattern backfill the split-heavy candidates would empty a session and
    be dropped — collapsing the user to only "מקסימום עקביות"."""
    db = Database(str(tmp_path / "task3_injured.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'Test',NULL,?)",
        (utc_now(),),
    )
    for mod in (coach_bot, onboarding_bot, callback_plans_bot, core_services, planning):
        monkeypatch.setattr(mod, "DB", db, raising=False)
    facts = {
        "weight_kg": 101, "height_cm": 174, "age": 32, "sex": "male",
        "primary_goal": "fat_loss_muscle_retention", "diet_restrictions": "none", "allergies": "none",
        "training_days_per_week": 4, "training_limitations": "טניס אלבו",
        "session_minutes": 55, "training_location": "gym", "equipment": "full_gym",
        "strength_experience": "advanced",
        "weekly_availability": [{"weekday": d, "start": "19:00", "minutes": 55} for d in [0, 2, 5, 6]],
    }
    for k, v in facts.items():
        await user_model.set_fact(db, 1, k, v, source=user_model.SOURCE_USER, confirmed=True)
    await planning.generate_candidates(db, 1, "workout")
    return db


@pytest.mark.asyncio
async def test_elbow_limitation_still_yields_all_three_strategies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression for the live bug: an elbow limitation must not collapse the
    plan choices to a single strategy. All three build, and each explains the
    limitation-driven substitution rather than silently dropping exercises."""
    db = await _ready_db_injured(tmp_path, monkeypatch)
    candidates = await planning.list_plan_candidates(db, 1, "workout")
    strategies = {c.get("strategy") for c in candidates}
    assert strategies == {"consistency", "balanced", "performance"}
    for candidate in candidates:
        # Every session is trainable (no empty session dead-ends the wizard).
        for session in candidate["payload"]["sessions"]:
            assert session["exercises"], f"{candidate['strategy']} has an empty session"
        # The limitation is surfaced, not hidden.
        assert any("המגבלה שדיווחת" in a for a in candidate.get("assumptions", []))


def test_strategy_explainer_lines_cover_all_four_dimensions() -> None:
    for strategy in ("consistency", "balanced", "performance"):
        lines = onboarding_bot._strategy_explainer_lines(strategy)
        joined = "\n".join(lines)
        assert "מבנה שבועי" in joined
        assert "אופי האימון" in joined
        assert "התקדמות" in joined
        assert "למי מתאים" in joined
