from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import coach_bot
import db as db_module


@pytest.mark.asyncio
async def test_operational_retention_deletes_only_expired_rows(tmp_path: Path, monkeypatch) -> None:
    test_db = coach_bot.Database(str(tmp_path / "coach.db"))
    await test_db.init()
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=800)).isoformat()
    recent = now.isoformat()
    await test_db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (recent,),
    )
    for external_id, created in (("old", old), ("new", recent)):
        await test_db.execute(
            """
            INSERT INTO health(
                user_id,external_id,sample_type,value,unit,start_time,created_at
            ) VALUES(1,?,'steps',1,'count',?,?)
            """,
            (external_id, created, created),
        )
        await test_db.execute(
            "INSERT INTO analytics_events(user_id,event,properties,created_at) VALUES(1,?,'{}',?)",
            (external_id, created),
        )
    monkeypatch.setattr(db_module, "DB", test_db)
    monkeypatch.setattr(coach_bot, "DB", test_db)
    result = await coach_bot.cleanup_operational_data_once()
    assert result["health"] == 1
    assert result["analytics_events"] == 1
    assert (await test_db.fetch_one("SELECT COUNT(*) AS c FROM health"))["c"] == 1
    assert (await test_db.fetch_one("SELECT COUNT(*) AS c FROM analytics_events"))["c"] == 1
