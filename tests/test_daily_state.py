from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from config import TZ
from db import Database
from helpers import utc_now
from noam_coach.services import daily_state


async def _user(db: Database) -> None:
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1, 'Noam', NULL, ?)",
        (utc_now(),),
    )


@pytest.mark.asyncio
async def test_consumed_totals_use_local_day_not_utc_date(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "daily_state.db"))
    await db.init()
    await _user(db)
    local_now = datetime(2026, 7, 1, 1, 0, tzinfo=TZ)
    previous_local_day = datetime(2026, 6, 30, 20, 30, tzinfo=timezone.utc).isoformat()
    same_local_day = datetime(2026, 6, 30, 22, 30, tzinfo=timezone.utc).isoformat()
    now = utc_now()
    await db.execute(
        """
        INSERT INTO meals(user_id, name, calories, protein, carbs, fat, confidence, eaten_at, created_at)
        VALUES(1, 'previous local day', 400, 20, 0, 0, 1, ?, ?)
        """,
        (previous_local_day, now),
    )
    await db.execute(
        """
        INSERT INTO meals(user_id, name, calories, protein, carbs, fat, confidence, eaten_at, created_at)
        VALUES(1, 'same local day', 700, 55, 0, 0, 1, ?, ?)
        """,
        (same_local_day, now),
    )

    calories, protein = await daily_state.consumed_totals(db, 1, now=local_now)

    assert calories == 700
    assert protein == 55


@pytest.mark.asyncio
async def test_workout_completed_today_uses_local_day_not_utc_date(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "daily_state_workout.db"))
    await db.init()
    await _user(db)
    local_now = datetime(2026, 7, 1, 1, 0, tzinfo=TZ)
    same_local_day = datetime(2026, 6, 30, 22, 30, tzinfo=timezone.utc).isoformat()
    await db.execute(
        """
        INSERT INTO sessions(user_id, code, name, plan, status, exercise_index, set_number, started_at, ended_at)
        VALUES(1, 'A', 'Workout A', '{}', 'completed', 0, 0, ?, ?)
        """,
        (same_local_day, same_local_day),
    )

    completed = await daily_state.workout_completed_today(db, 1, now=local_now)

    assert completed is True
