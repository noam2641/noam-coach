"""Codex-audit follow-ups — small reliability/UX gaps closed after the round.

Covers:
  * Weight "trend": 2 measurements are NOT a trend (images 24-26) — a trend
    line appears only from 3 readings and says how many it is based on.
  * Each next-meal option carries a light/medium/large size label so the
    user sees at a glance what kind of meal it is (image 9).
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

import coach_bot
from noam_coach.bot import onboarding as onboarding_bot
from noam_coach.services.next_meal import meal_size_label_he


async def _db_with_user(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> coach_bot.Database:
    db = coach_bot.Database(str(tmp_path / "followups.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (coach_bot.utc_now(),),
    )
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(onboarding_bot, "DB", db)
    return db


async def _add_weight(db: coach_bot.Database, days_ago: int, value: float) -> None:
    when = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_ago)).isoformat()
    await db.execute(
        "INSERT INTO health(user_id, external_id, sample_type, value, unit, "
        "start_time, end_time, source_device, created_at) "
        "VALUES(1, ?, 'weight', ?, 'kg', ?, NULL, 'scale', ?)",
        (f"w:{days_ago}", value, when, coach_bot.utc_now()),
    )


@pytest.mark.asyncio
async def test_two_weight_readings_are_not_presented_as_a_trend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _db_with_user(tmp_path, monkeypatch)
    await _add_weight(db, 30, 101.0)
    await _add_weight(db, 2, 99.0)
    extras = await onboarding_bot.compute_basics_extras(1)
    assert "אין מגמה מהימנה" in extras["weight_trend_90d"]
    assert "ירידה" not in extras["weight_trend_90d"]


@pytest.mark.asyncio
async def test_three_weight_readings_show_trend_with_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _db_with_user(tmp_path, monkeypatch)
    await _add_weight(db, 40, 102.0)
    await _add_weight(db, 20, 100.5)
    await _add_weight(db, 2, 99.0)
    extras = await onboarding_bot.compute_basics_extras(1)
    assert "ירידה של 3.0" in extras["weight_trend_90d"]
    assert "3 מדידות" in extras["weight_trend_90d"]


def test_meal_size_labels() -> None:
    assert meal_size_label_he(250) == "קלה"
    assert meal_size_label_he(450) == "בינונית"
    assert meal_size_label_he(700) == "גדולה"
    assert meal_size_label_he(None) == ""
