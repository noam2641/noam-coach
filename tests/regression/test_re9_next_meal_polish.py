"""RE9 Tranche 1 regressions: next-meal after-meal line, ranking, plan-vs-eat, timeline.

Covers RE9-002, RE9-013, RE9-019, RE9-020, RE9-052, RE9-053.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from db import Database
from helpers import utc_now
from noam_coach.services.next_meal import (
    after_meal_balance,
    build_day_timeline,
    format_next_meal_explanation,
    format_next_meal_recommendation,
    generate_next_meal_recommendation,
    plan_chosen_meal,
)
from noam_coach.services.nutrition_context import build_nutrition_context


async def _db(tmp_path: Path, *, calories: int = 2100, protein: int = 160) -> Database:
    db = Database(str(tmp_path / "re9_nm.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1, 'Test', NULL, ?)",
        (utc_now(),),
    )
    await db.execute(
        """
        INSERT INTO goal_versions(user_id, calories, protein, steps, phase, status, source, created_at)
        VALUES(1, ?, ?, 8000, 'fat_loss_muscle_retention', 'active', 'user_approved', ?)
        """,
        (calories, protein, utc_now()),
    )
    return db


@pytest.mark.asyncio
async def test_re9_recommendation_shows_after_meal_remaining(tmp_path: Path) -> None:
    """RE9-020: the recommendation list shows 'after the meal' remaining per option."""
    db = await _db(tmp_path)
    rec = await generate_next_meal_recommendation(db, 1)
    text = format_next_meal_recommendation(rec)
    assert "אחרי הארוחה" in text
    # The number equals consumed-based balance minus the option calories.
    option = rec.options[0]
    after_cal, _ = after_meal_balance(rec.context.nutrition, option)
    assert after_cal == rec.context.nutrition.calorie_balance - option.calories


@pytest.mark.asyncio
async def test_re9_exactly_one_recommended_option_with_reason(tmp_path: Path) -> None:
    """RE9-013/052/053: exactly one option is marked recommended, with a reason."""
    db = await _db(tmp_path)
    rec = await generate_next_meal_recommendation(db, 1)
    recommended = [o for o in rec.options if o.recommended]
    assert len(recommended) == 1
    assert recommended[0].recommended_reason
    text = format_next_meal_recommendation(rec)
    assert "מומלץ עבורך" in text
    assert "התאמה" in text


@pytest.mark.asyncio
async def test_re9_options_sorted_by_score_desc(tmp_path: Path) -> None:
    """RE9-053: options are ordered by score, best first."""
    db = await _db(tmp_path)
    rec = await generate_next_meal_recommendation(db, 1)
    scores = [o.score for o in rec.options]
    assert scores == sorted(scores, reverse=True)
    assert rec.options[0].recommended is True


@pytest.mark.asyncio
async def test_re9_timeline_in_detail_view(tmp_path: Path) -> None:
    """RE9-002: the 'why it fits' detail view includes a rest-of-day timeline."""
    db = await _db(tmp_path)
    rec = await generate_next_meal_recommendation(db, 1)
    steps = build_day_timeline(rec.context)
    assert steps  # non-empty
    detail = format_next_meal_explanation(rec)
    assert "המשך היום" in detail
    assert "סוף היום" in detail


@pytest.mark.asyncio
async def test_re9_plan_for_later_is_planned_not_consumed(tmp_path: Path) -> None:
    """RE9-019: planning a meal stores it as planned, never as consumed."""
    db = await _db(tmp_path)
    rec = await generate_next_meal_recommendation(db, 1)
    option = rec.options[0]

    planned = await plan_chosen_meal(db, 1, option)
    assert planned is True

    # No meal row was created (not consumed).
    row = await db.fetch_one("SELECT COUNT(*) AS c FROM meals WHERE user_id=1")
    assert int(row["c"]) == 0

    # Nutrition context surfaces it as planned, and consumed remains zero.
    ctx = await build_nutrition_context(db, 1, "test", now=datetime.now(timezone.utc))
    assert any(m.get("source") == "next_meal_plan" for m in ctx.planned_meals)
    assert ctx.consumed_calories == 0

    # Planning the same option again is idempotent.
    assert await plan_chosen_meal(db, 1, option) is False
