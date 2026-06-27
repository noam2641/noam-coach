"""Tests for build_daily_status — P1: no zero-as-fact, meal list, completeness."""

from __future__ import annotations

from pathlib import Path

import pytest

import coach_bot


async def _db_with_user(tmp_path: Path, monkeypatch) -> coach_bot.Database:
    db = coach_bot.Database(str(tmp_path / "status.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (coach_bot.utc_now(),),
    )
    monkeypatch.setattr(coach_bot, "DB", db)
    return db


@pytest.mark.asyncio
async def test_daily_status_no_meals_does_not_show_zeros_as_fact(tmp_path, monkeypatch) -> None:
    await _db_with_user(tmp_path, monkeypatch)
    text = await coach_bot.build_daily_status(1)
    # No meals reported: must not present "0 consumed / remaining" as if complete.
    assert "עדיין לא דיווחת ארוחות היום" in text
    assert "נותרו" not in text


@pytest.mark.asyncio
async def test_daily_status_lists_meals_and_completeness(tmp_path, monkeypatch) -> None:
    db = await _db_with_user(tmp_path, monkeypatch)
    now = coach_bot.utc_now()
    await db.execute(
        "INSERT INTO meals(user_id, name, calories, protein, carbs, fat, confidence, eaten_at, created_at) "
        "VALUES(1,'ביצים',150,12,1,10,0.8,?,?)",
        (now, now),
    )
    text = await coach_bot.build_daily_status(1)
    assert "ביצים" in text  # the composing meal is listed
    assert "דווחו" in text  # meal-count line present
    # The total is explicitly framed as "reported only", not a complete day.
    assert "שדווחו בלבד" in text


@pytest.mark.asyncio
async def test_profile_readiness_lists_missing_facts(tmp_path, monkeypatch) -> None:
    """P1: readiness must show WHAT is missing per category, not just a percent."""
    await _db_with_user(tmp_path, monkeypatch)
    # Brand-new user: nothing filled in -> categories not ready, with detail.
    text = await coach_bot.build_profile_text(1)
    assert "מוכנות" in text
    # The threshold is explained and missing items are surfaced.
    assert "כל נתוני החובה" in text
    assert "חסר:" in text
