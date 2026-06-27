from __future__ import annotations

from pathlib import Path

import pytest

import coach_intelligence
import user_model
from db import Database
from helpers import utc_now


async def _db(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "coach-intelligence.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'Test',NULL,?)",
        (utc_now(),),
    )
    return db


@pytest.mark.asyncio
async def test_next_action_starts_with_safety_not_features(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    action = await coach_intelligence.next_best_action(db, 1)
    assert action.kind == "safety_check"
    assert action.priority == 100


@pytest.mark.asyncio
async def test_profile_conflict_detects_unrealistic_frequency(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    await user_model.set_fact(db, 1, "training_days_per_week", 5, confirmed=True)
    for index in range(2):
        await db.execute(
            """
            INSERT INTO sessions(user_id, code, name, plan, status, exercise_index,
                                 set_number, started_at, ended_at)
            VALUES(1,'A','A','{}','completed',0,1,?,?)
            """,
            (utc_now(), utc_now()),
        )
    conflicts = await coach_intelligence.profile_conflicts(db, 1)
    assert conflicts
    assert conflicts[0].key == "training_days_per_week"
