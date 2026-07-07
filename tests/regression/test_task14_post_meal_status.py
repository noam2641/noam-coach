"""TASK-14 regression tests — short post-meal confirmation renderer.

render_post_meal_confirmation_day_status() replaces the long "מצב היום"
summary (build_daily_status) as the message shown right after a meal is
confirmed. It must stay short: saved-confirmation, the meal's own macros,
today's running totals/remaining budget, current time, and what's left of
the day — never the full daily summary, a menu, or a next-meal
recommendation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import coach_bot


async def _db_with_user(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> coach_bot.Database:
    db = coach_bot.Database(str(tmp_path / "task14.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (coach_bot.utc_now(),),
    )
    monkeypatch.setattr(coach_bot, "DB", db)
    return db


async def _approve_goal(db: coach_bot.Database, calories: int = 2100, protein: int = 190) -> None:
    now = coach_bot.utc_now()
    await db.execute(
        "INSERT INTO goal_versions(user_id, calories, protein, steps, phase, status, source, "
        "explanation, created_at) VALUES(1, ?, ?, 8000, 'fat_loss_muscle_retention', 'active', "
        "'manual', 'test goal', ?)",
        (calories, protein, now),
    )
    await db.execute(
        "INSERT INTO goals(user_id, calories, protein, steps, phase, updated_at) "
        "VALUES(1, ?, ?, 8000, 'fat_loss_muscle_retention', ?) "
        "ON CONFLICT(user_id) DO UPDATE SET calories=excluded.calories, protein=excluded.protein",
        (calories, protein, now),
    )


async def _log_meal(db: coach_bot.Database, name: str, calories: float, protein: float) -> None:
    now = coach_bot.utc_now()
    await db.execute(
        "INSERT INTO meals(user_id, name, calories, protein, carbs, fat, confidence, eaten_at, created_at) "
        "VALUES(1, ?, ?, ?, 10, 5, 0.9, ?, ?)",
        (name, calories, protein, now, now),
    )


@pytest.mark.asyncio
async def test_post_meal_status_is_short_and_has_required_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _db_with_user(tmp_path, monkeypatch)
    await _approve_goal(db)
    await _log_meal(db, "עוף ואורז", 520, 42)

    text = await coach_bot.render_post_meal_confirmation_day_status(
        1, {"calories": 520, "protein": 42}
    )

    assert "נשמר ✅" in text
    assert "520" in text
    assert "42" in text
    assert "נאכל עד עכשיו" in text
    assert "נשאר להיום" in text
    assert "מצב היום עכשיו" in text


@pytest.mark.asyncio
async def test_post_meal_status_excludes_long_summary_sections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Must NOT be the long daily summary, a menu, or a next-meal recommendation."""
    db = await _db_with_user(tmp_path, monkeypatch)
    await _approve_goal(db)
    await _log_meal(db, "עוף ואורז", 520, 42)

    text = await coach_bot.render_post_meal_confirmation_day_status(
        1, {"calories": 520, "protein": 42}
    )
    long_summary = await coach_bot.build_daily_status(1)

    assert text != long_summary
    assert len(text) < len(long_summary)
    assert "דווחו" not in text  # the long summary's meal-list header
    assert "מומלץ עבורך" not in text  # next-meal recommendation marker


@pytest.mark.asyncio
async def test_post_meal_status_works_with_no_meals_logged_yet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Must render even for the very first meal of the day (no prior totals)."""
    db = await _db_with_user(tmp_path, monkeypatch)
    await _approve_goal(db)
    await _log_meal(db, "ביצים", 300, 20)

    text = await coach_bot.render_post_meal_confirmation_day_status(
        1, {"calories": 300, "protein": 20}
    )
    assert "נשמר ✅" in text
    assert "300" in text
