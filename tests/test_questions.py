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
