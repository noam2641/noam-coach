"""TASK-11 — post-meal continuation timeline is chronological and clean.

No raw ISO/RFC3339 timestamps (only HH:MM); events sorted by clock time; the
remaining budget is distributed across remaining eating opportunities rather
than dumped into a single meal.
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
from noam_coach.services import next_meal as nm


async def _db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, calories: int, protein: int) -> Database:
    db = Database(str(tmp_path / "task11.db"))
    await db.init()
    await db.execute("INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)", (utc_now(),))
    now = utc_now()
    await db.execute(
        "INSERT INTO goals(user_id,calories,protein,steps,phase,updated_at) VALUES(1,?,?,8000,'x',?)",
        (calories, protein, now),
    )
    monkeypatch.setattr(coach_bot, "DB", db)
    from noam_coach.bot import workout as workout_bot

    monkeypatch.setattr(workout_bot, "DB", db, raising=False)
    monkeypatch.setattr(nm, "DB", db, raising=False)
    return db


def test_hhmm_from_iso_renders_clock_only() -> None:
    assert nm._hhmm_from_iso("2026-07-12T19:09:00+03:00") == "19:09"


@pytest.mark.asyncio
async def test_timeline_has_no_raw_iso_timestamp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = await _db(tmp_path, monkeypatch, calories=2000, protein=165)
    # A future workout later today + a logged meal so there is a remaining budget.
    await db.execute(
        "INSERT INTO meals(user_id,name,calories,protein,carbs,fat,confidence,eaten_at,created_at) "
        "VALUES(1,'ארוחה',400,40,20,10,0.9,?,?)",
        (utc_now(), utc_now()),
    )
    text = await coach_bot.render_post_meal_confirmation_day_status(1, {"calories": 400, "protein": 40})
    # No ISO date/time fragments in the user-facing text.
    assert "T" not in text.replace("Telegram", "") or "2026-" not in text
    assert "2026-" not in text
    assert "+03:00" not in text
    assert "+02:00" not in text


@pytest.mark.asyncio
async def test_large_remaining_gap_is_split_across_meals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Early evening with a large remaining budget → more than one meal slot, so
    # no single slot should carry the entire remaining balance.
    db = await _db(tmp_path, monkeypatch, calories=2000, protein=165)
    await user_model.set_fact(
        db, 1, "sleep_schedule", {"typical_bedtime": "23:30", "typical_wake_time": "07:00"},
        source=user_model.SOURCE_USER, confirmed=True,
    )
    early_evening = datetime.now(TZ).replace(hour=17, minute=0, second=0, microsecond=0)
    ctx = await nm.build_workout_nutrition_context(db, 1, now=early_evening)
    allocations = nm.build_remaining_slot_allocations(ctx)
    assert len(allocations) >= 2
    remaining = ctx.nutrition.calorie_balance or 0
    # No single slot carries the whole remaining balance.
    assert all(a.calories < remaining for a in allocations)
