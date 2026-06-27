from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import coach_bot


@pytest.mark.asyncio
async def test_concurrent_job_claim_has_one_winner(tmp_path: Path, monkeypatch) -> None:
    db = coach_bot.Database(str(tmp_path / "coach.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (coach_bot.utc_now(),),
    )
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(coach_bot.SETTINGS, "proactive_quiet_start", "00:00")
    monkeypatch.setattr(coach_bot.SETTINGS, "proactive_quiet_end", "00:00")
    monkeypatch.setattr(coach_bot.SETTINGS, "proactive_daily_limit", 10)

    claims = await asyncio.gather(
        *[
            coach_bot.claim_job_delivery(
                1,
                "same-key",
                priority=coach_bot.JOB_PRIORITY_HIGH,
            )
            for _ in range(5)
        ]
    )
    winners = [claim for claim in claims if claim is not None]
    assert len(winners) == 1
    await coach_bot.complete_job_delivery(winners[0])
    assert (
        await coach_bot.claim_job_delivery(
            1,
            "same-key",
            priority=coach_bot.JOB_PRIORITY_HIGH,
        )
        is None
    )
