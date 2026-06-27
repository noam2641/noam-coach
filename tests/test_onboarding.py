"""Tests for onboarding.py — stage management and summary screens."""

from __future__ import annotations

from pathlib import Path

import pytest

import coach_bot
import onboarding


@pytest.mark.asyncio
async def test_stage_lifecycle(tmp_path: Path) -> None:
    db = coach_bot.Database(str(tmp_path / "coach.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (coach_bot.utc_now(),),
    )
    # Initial stage is "none"
    assert await onboarding.get_stage(db, 1) == onboarding.S_NONE
    assert not await onboarding.is_onboarding(db, 1)

    # Start onboarding
    await onboarding.set_stage(db, 1, onboarding.S_OPEN)
    assert await onboarding.get_stage(db, 1) == onboarding.S_OPEN
    assert await onboarding.is_onboarding(db, 1)

    # Complete onboarding
    await onboarding.set_stage(db, 1, onboarding.S_DONE)
    assert await onboarding.get_stage(db, 1) == onboarding.S_DONE
    assert not await onboarding.is_onboarding(db, 1)


def test_basics_summary_with_data() -> None:
    profile_view = {
        "measured": [
            {"key": "height_cm", "value": 178},
            {"key": "weight_kg", "value": 85.0},
            {"key": "avg_steps", "value": 8500},
        ],
    }
    extras = {
        "weight_range": (83.0, 87.0),
        "weight_trend_90d": "ירידה",
        "avg_sleep": "7.5 שעות",
        "weekly_workouts": 3,
    }
    text = onboarding.basics_summary(profile_view, extras)
    assert "178" in text
    assert "85" in text
    assert "8500" in text
    assert "83" in text
    assert "ירידה" in text
    assert "7.5" in text
    assert "3" in text


def test_basics_summary_missing_data() -> None:
    profile_view = {"measured": []}
    text = onboarding.basics_summary(profile_view, {})
    assert "לא נמצא" in text


def test_patterns_text_with_data() -> None:
    profile = {
        "sleep": {"typical_bedtime": "23:30", "typical_wake_time": "07:00"},
        "workout": {"typical_hour": "10:00", "weekly_frequency": 3.5},
        "eating": {"typical_meal_hours": ["08:00", "13:00", "20:00"]},
    }
    text, items = onboarding.patterns_text(profile)
    assert len(items) == 3
    assert "23:30" in text
    assert "10:00" in text
    assert "08:00" in text


def test_patterns_text_empty() -> None:
    text, items = onboarding.patterns_text({})
    assert len(items) == 0
    assert "לא הצטברו" in text


def test_intro_text_exists() -> None:
    assert "היי" in onboarding.INTRO_TEXT
    assert "Apple Health" in onboarding.INTRO_TEXT


def test_export_help_text_exists() -> None:
    assert "בריאות" in onboarding.EXPORT_HELP_TEXT
    assert "ZIP" in onboarding.EXPORT_HELP_TEXT
