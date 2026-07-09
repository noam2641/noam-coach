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
async def test_meals_have_explicit_status_and_only_consumed_counts(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "meal_status.db"))
    await db.init()
    await _user(db)
    cols = await db.fetch_all("PRAGMA table_info(meals)")
    assert "status" in {row["name"] for row in cols}

    local_now = datetime(2026, 7, 7, 12, 0, tzinfo=TZ)
    eaten_at = datetime(2026, 7, 7, 9, 0, tzinfo=TZ).astimezone(timezone.utc).isoformat()
    created = utc_now()
    await db.execute(
        """
        INSERT INTO meals(user_id, name, calories, protein, carbs, fat, confidence, eaten_at, created_at, status)
        VALUES(1, 'נאכל בפועל', 500, 40, 0, 0, 1, ?, ?, 'consumed')
        """,
        (eaten_at, created),
    )
    await db.execute(
        """
        INSERT INTO meals(user_id, name, calories, protein, carbs, fat, confidence, eaten_at, created_at, status)
        VALUES(1, 'רק מתוכנן', 900, 80, 0, 0, 1, ?, ?, 'planned')
        """,
        (eaten_at, created),
    )

    calories, protein = await daily_state.consumed_totals(db, 1, now=local_now)
    meals = await daily_state.consumed_meals(db, 1, now=local_now)

    assert calories == 500
    assert protein == 40
    assert [m["name"] for m in meals] == ["נאכל בפועל"]
