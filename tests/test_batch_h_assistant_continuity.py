"""Batch H (FIX 53, partial): the general assistant's intent classifier
gains situational awareness -- active recommendation, active daily menu,
and active pain -- instead of only goal/weight/training-frequency.

Root cause (verified before this fix): assistant_profile_summary() built
the ONLY context string handed to classify_intent() from three raw facts.
It had no idea a next-meal recommendation or daily menu was currently
active, or that the user had reported pain, so it could not ground
references like "תעשה יותר קטן" (make it smaller) in anything except the
bare current message.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

import coach_bot
import user_model
from config import TZ
from db import Database
from helpers import utc_now
from noam_coach.bot.assistant import assistant_profile_summary
from noam_coach.bot.onboarding import save_medical_constraint
from noam_coach.services import core as core_services
from noam_coach.services.daily_menu_state import MenuMealRecord, remember_active_daily_menu
from noam_coach.services.next_meal import ACTIVE_RECOMMENDATION_FLOW


async def _make_db(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "batch_h_continuity.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (utc_now(),),
    )
    return db


@pytest.mark.asyncio
async def test_summary_still_includes_baseline_facts(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    coach_bot.DB.path = db.path
    core_services.DB = db

    await user_model.set_fact(db, 1, "primary_goal", "fat_loss", source=user_model.SOURCE_USER, confirmed=True)

    summary = await assistant_profile_summary(1)
    assert "מטרה=" in summary
    assert "משקל=" in summary
    assert "אימונים/שבוע=" in summary


@pytest.mark.asyncio
async def test_summary_mentions_active_recommendation(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    coach_bot.DB.path = db.path
    core_services.DB = db

    now = datetime.now(TZ)
    await core_services.set_flow_state(
        1, ACTIVE_RECOMMENDATION_FLOW, "active",
        {
            "options": [], "option_payloads": [],
            "option_titles": ["עוף ואורז", "סלט טונה"],
            "remaining_calories": 500, "budget_policy": "normal", "message_id": None,
            "created_at": now.isoformat(),
            "expiry": (now + timedelta(hours=6)).isoformat(),
        },
    )

    summary = await assistant_profile_summary(1)
    assert "המלצת ארוחה פעילה" in summary
    assert "עוף ואורז" in summary


@pytest.mark.asyncio
async def test_summary_mentions_active_daily_menu(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    coach_bot.DB.path = db.path

    meal = MenuMealRecord(meal_id="breakfast-0", slot="breakfast", time="08:00", role="ארוחת בוקר", calories=500, protein=40)
    await remember_active_daily_menu(db, 1, text="<b>תפריט</b>", meals=[meal])

    summary = await assistant_profile_summary(1)
    assert "תפריט יומי פעיל" in summary


@pytest.mark.asyncio
async def test_summary_mentions_active_pain(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    coach_bot.DB.path = db.path

    await save_medical_constraint(1, kind="pain", location="ברך ימין", note="test")

    summary = await assistant_profile_summary(1)
    assert "כאב פעיל" in summary
    assert "ברך ימין" in summary


@pytest.mark.asyncio
async def test_summary_omits_optional_sections_when_nothing_active(tmp_path: Path) -> None:
    """Sanity check: with no active recommendation/menu/pain, the summary
    is unchanged from the original baseline-only shape."""
    db = await _make_db(tmp_path)
    coach_bot.DB.path = db.path
    core_services.DB = db

    summary = await assistant_profile_summary(1)
    assert "המלצת ארוחה פעילה" not in summary
    assert "תפריט יומי פעיל" not in summary
    assert "כאב פעיל" not in summary
