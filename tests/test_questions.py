"""Tests for questions.py — question engine, priority, and safety gate."""

from __future__ import annotations

from pathlib import Path

import pytest

import coach_bot
import questions
import user_model


def test_question_priority_formula() -> None:
    q = questions.Question(
        id="test_q",
        fact_key="test",
        text="test?",
        options=[],
        affects=("a",),
        safety=5,
        plan_impact=4,
        urgency=3,
        uncertainty=2,
        burden=1,
    )
    # safety*3 + plan*2 + urgency + uncertainty - burden
    assert q.priority() == 5 * 3 + 4 * 2 + 3 + 2 - 1


def test_question_without_affects_raises() -> None:
    with pytest.raises(ValueError, match="no 'affects'"):
        questions.Question(
            id="bad", fact_key="x", text="?", options=[], affects=()
        )


def test_safety_questions_have_high_priority() -> None:
    for q in questions.SAFETY_QUESTIONS:
        assert q.safety >= 4
        assert q.priority() > 10


def test_question_by_id_found() -> None:
    q = questions.question_by_id("safety_pain")
    assert q is not None
    assert q.fact_key == "active_pain"


def test_question_by_id_not_found() -> None:
    assert questions.question_by_id("nonexistent_id") is None


def test_onboarding_questions_subset() -> None:
    ids = {q.id for q in questions.ONBOARDING_QUESTIONS}
    assert "safety_pain" in ids
    assert "q_primary_goal" in ids
    assert "q_training_days" in ids


def test_diet_and_allergy_questions_use_distinct_user_language() -> None:
    allergy_q = questions.question_by_id("q_allergies")
    diet_q = questions.question_by_id("q_diet_restrictions")
    assert allergy_q is not None
    assert diet_q is not None

    assert "רגישות" in allergy_q.text
    assert "אסורים" in allergy_q.text
    assert allergy_q.options == [("אין אלרגיות/רגישויות", "none")]
    assert "מעדיף לא לאכול" in diet_q.text
    assert diet_q.fact_key == "diet_restrictions"
    assert allergy_q.fact_key == "allergies"


def test_numeric_answers_are_contextual_for_height_weight_and_goal() -> None:
    height = questions.question_by_id("q_height")
    goal = questions.question_by_id("q_goal_weight")
    assert height is not None
    assert goal is not None

    current_weight = questions.Question(
        id="q_current_weight",
        fact_key="weight_kg",
        text="weight?",
        options=[],
        numeric=True,
        min_value=30,
        max_value=300,
        unit="kg",
        affects=("calorie_target",),
    )

    assert questions.normalize_answer(height, "174") == 174
    assert questions.normalize_answer(goal, "83") == 83
    assert questions.normalize_answer(current_weight, "101.8") == 101.8


def test_time_range_is_not_accepted_as_numeric_answer() -> None:
    calories = questions.Question(
        id="q_calories",
        fact_key="calorie_target",
        text="calories?",
        options=[],
        numeric=True,
        min_value=1000,
        max_value=5000,
        unit="cal",
        affects=("calorie_target",),
    )

    assert questions.looks_like_time_range("00:20-06:50")
    with pytest.raises(ValueError, match="טווח שעות"):
        questions.normalize_answer(calories, "00:20-06:50")


def test_all_questions_have_affects() -> None:
    for q in questions.ALL_QUESTIONS:
        assert q.affects, f"{q.id} has no affects"


@pytest.mark.asyncio
async def test_next_question_returns_highest_priority(tmp_path: Path) -> None:
    db = coach_bot.Database(str(tmp_path / "coach.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (coach_bot.utc_now(),),
    )
    q = await questions.next_question(db, 1)
    assert q is not None
    # Safety questions should come first (highest priority)
    assert q.safety >= 4


@pytest.mark.asyncio
async def test_next_question_skips_answered(tmp_path: Path) -> None:
    db = coach_bot.Database(str(tmp_path / "coach.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (coach_bot.utc_now(),),
    )
    # Answer all safety questions
    for q in questions.SAFETY_QUESTIONS:
        await questions.record_answer(db, 1, q, "none")

    next_q = await questions.next_question(db, 1)
    # Should be a plan question now, not a safety question
    if next_q is not None:
        assert next_q.id not in {q.id for q in questions.SAFETY_QUESTIONS}


@pytest.mark.asyncio
async def test_pending_safety_questions(tmp_path: Path) -> None:
    db = coach_bot.Database(str(tmp_path / "coach.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (coach_bot.utc_now(),),
    )
    pending = await questions.pending_safety_questions(db, 1)
    assert len(pending) == len(questions.SAFETY_QUESTIONS)

    # Answer one
    await questions.record_answer(db, 1, questions.SAFETY_QUESTIONS[0], "none")
    pending = await questions.pending_safety_questions(db, 1)
    assert len(pending) == len(questions.SAFETY_QUESTIONS) - 1


@pytest.mark.asyncio
async def test_record_answer_stores_as_user_fact(tmp_path: Path) -> None:
    db = coach_bot.Database(str(tmp_path / "coach.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (coach_bot.utc_now(),),
    )
    q = questions.SAFETY_QUESTIONS[0]
    await questions.record_answer(db, 1, q, "none")

    fact = await user_model.get_fact(db, 1, q.fact_key)
    assert fact is not None
    assert fact["source"] == user_model.SOURCE_USER
    assert fact["confirmed"] is True


@pytest.mark.asyncio
async def test_next_question_respects_when_filter(tmp_path: Path) -> None:
    db = coach_bot.Database(str(tmp_path / "coach.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (coach_bot.utc_now(),),
    )
    # Diet questions have when=lambda ctx: ctx.get("planning_nutrition", False)
    diet_q = questions.question_by_id("q_diet_restrictions")
    assert diet_q is not None

    # Without the context flag, diet question should not appear
    q = await questions.next_question(db, 1, context={}, pool=[diet_q])
    assert q is None

    # With the flag, it should appear
    q = await questions.next_question(db, 1, context={"planning_nutrition": True}, pool=[diet_q])
    assert q is not None
