from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

import coach_bot
import mini_api
import user_model
from config import TZ
from db import Database
from helpers import utc_now
from models import MiniProfileUpdate


@pytest.mark.asyncio
async def test_mini_profile_update_writes_only_public_facts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = Database(str(tmp_path / "mini-profile.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (utc_now(),),
    )
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(mini_api, "DB", db)
    payload = MiniProfileUpdate(
        work_start="08:00",
        work_end="17:00",
        commute_minutes=35,
        session_minutes=50,
        training_location="חדר כושר",
        equipment="חדר כושר מלא",
        weekly_availability=[
            {"weekday": 0, "available": True, "start": "19:00", "minutes": 50}
        ],
    )
    response = await coach_bot.mini_update_profile(payload, user_id=1)
    data = json.loads(response.body)
    assert "work_schedule" in data["changed"]
    assert await user_model.get_value(db, 1, "commute_minutes") == 35
    schedule = await user_model.get_value(db, 1, "work_schedule")
    assert schedule == {"start": "08:00", "end": "17:00"}
    assert await user_model.get_fact(db, 1, "pending_prompt") is None


@pytest.mark.asyncio
async def test_mini_profile_returns_resolved_availability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = Database(str(tmp_path / "mini-profile-availability.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (utc_now(),),
    )
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(mini_api, "DB", db)
    await user_model.set_fact(
        db,
        1,
        "training_days_per_week",
        4,
        source=user_model.SOURCE_USER,
        confirmed=True,
    )
    await user_model.set_fact(
        db,
        1,
        "workout_pattern",
        {"weekly_frequency": 6, "common_weekdays": [0, 1, 2, 3, 4, 5]},
        kind=user_model.KIND_ESTIMATE,
        source=user_model.SOURCE_APPLE_HEALTH,
        confirmed=False,
    )

    response = await mini_api.mini_profile(user_id=1)
    data = json.loads(response.body)
    assert data["availability"]["max_days_per_week"] == 4
    assert data["availability"]["source"] == "user_corrected"


@pytest.mark.asyncio
async def test_operational_snapshot_surfaces_active_pain_session_and_load_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = Database(str(tmp_path / "mini-ops.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (utc_now(),),
    )
    await db.execute(
        """
        INSERT INTO medical_constraints(
            user_id, kind, location, severity, status, note, affects, created_at
        ) VALUES(1, 'pain', 'elbow', 2, 'active', 'reported during workout',
                 '["exercise_selection"]', ?)
        """,
        (utc_now(),),
    )
    plan = {
        "name": "Ops",
        "exercises": [
            {
                "id": "one_arm_row",
                "name": "חתירה ביד אחת",
                "sets": 3,
                "rmin": 8,
                "rmax": 12,
                "inc": 2.5,
                "weight": 20,
                "cues": ["דגש טכני"],
            }
        ],
    }
    await db.execute(
        "INSERT INTO sessions(user_id, code, name, plan, status, exercise_index, set_number, started_at) "
        "VALUES(1, 'T', 'Ops', ?, 'active', 0, 1, ?)",
        (json.dumps(plan, ensure_ascii=False), utc_now()),
    )
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(mini_api, "DB", db)

    snapshot = await mini_api._operational_snapshot(1)

    assert snapshot["active_pain"][0]["region"] == "elbow"
    assert snapshot["active_session"]["name"] == "Ops"
    assert snapshot["latest_session"]["status"] == "active"
    assert snapshot["current_load_decision"]["decision"] == "planned_load"
    assert "active_pain:elbow" in snapshot["current_load_decision"]["signals"]


@pytest.mark.asyncio
async def test_mini_today_meals_returns_meals_logged_in_db(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = Database(str(tmp_path / "mini-meals.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (utc_now(),),
    )
    eaten_at = datetime.now(TZ).replace(hour=12, minute=30, second=0, microsecond=0).isoformat()
    await db.execute(
        """
        INSERT INTO meals(user_id, name, calories, protein, carbs, fat, confidence, eaten_at, created_at)
        VALUES(1, 'Chicken bowl', 620, 45, 50, 18, 0.9, ?, ?)
        """,
        (eaten_at, utc_now()),
    )
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(mini_api, "DB", db)

    response = await mini_api.mini_today_meals(user_id=1)
    data = json.loads(response.body)

    assert data["meals"][0]["name"] == "Chicken bowl"
    assert data["meals"][0]["calories"] == 620
    assert "quality" in data
