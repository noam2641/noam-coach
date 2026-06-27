from __future__ import annotations

import json
from pathlib import Path

import pytest

import coach_bot
import mini_api
import user_model
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
