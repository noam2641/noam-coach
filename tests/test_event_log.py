from __future__ import annotations

from pathlib import Path

import pytest

import event_log
from db import Database
from helpers import utc_now


@pytest.mark.asyncio
async def test_event_replay_omits_secret_properties(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "events.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (utc_now(),),
    )
    await event_log.append_event(
        db,
        1,
        "TEST",
        properties={"token": "do-not-show", "value": 3},
    )
    replay = await event_log.replay_summary(db, 1)
    assert "do-not-show" not in replay
    assert '"value":3' in replay
