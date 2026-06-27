from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

import coach_bot
import conversation


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


async def _job_db(tmp_path: Path, monkeypatch) -> coach_bot.Database:
    db = coach_bot.Database(str(tmp_path / "coach.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (coach_bot.utc_now(),),
    )
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(coach_bot.SETTINGS, "telegram_allowed_user_id", 1)
    monkeypatch.setattr(coach_bot.SETTINGS, "proactive_quiet_start", "00:00")
    monkeypatch.setattr(coach_bot.SETTINGS, "proactive_quiet_end", "00:00")
    monkeypatch.setattr(coach_bot.SETTINGS, "proactive_daily_limit", 10)
    monkeypatch.setattr(coach_bot.SETTINGS, "proactive_min_gap_minutes", 0)
    return db


async def _insert_active_goal(db: coach_bot.Database) -> None:
    await db.execute(
        """
        INSERT INTO goal_versions(user_id, calories, protein, steps, phase, status, source, created_at)
        VALUES(1, 2200, 160, 9000, 'fat_loss_muscle_retention', 'active', 'computed', ?)
        """,
        (coach_bot.utc_now(),),
    )


@pytest.mark.asyncio
async def test_morning_checkin_is_not_blocked_by_empty_nutrition_day(
    tmp_path: Path,
    monkeypatch,
) -> None:
    await _job_db(tmp_path, monkeypatch)
    sent = False

    async def sender() -> None:
        nonlocal sent
        sent = True

    result = await coach_bot.deliver_proactive_message(
        SimpleNamespace(bot=SimpleNamespace(), job_queue=None),
        key="morning_checkin",
        sender=sender,
        priority=coach_bot.JOB_PRIORITY_SCHEDULED,
    )

    assert result is True
    assert sent is True


@pytest.mark.asyncio
async def test_morning_menu_requires_goal_but_not_already_reported_meals(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db = await _job_db(tmp_path, monkeypatch)
    await _insert_active_goal(db)
    sent = False

    async def sender() -> None:
        nonlocal sent
        sent = True

    result = await coach_bot.deliver_proactive_message(
        SimpleNamespace(bot=SimpleNamespace(), job_queue=None),
        key="morning_menu",
        sender=sender,
        priority=coach_bot.JOB_PRIORITY_SCHEDULED,
        counts_toward_budget=False,
    )

    assert result is True
    assert sent is True


@pytest.mark.asyncio
async def test_intraday_nudge_requires_usable_nutrition_day(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db = await _job_db(tmp_path, monkeypatch)
    await _insert_active_goal(db)
    sent = False

    async def sender() -> None:
        nonlocal sent
        sent = True

    result = await coach_bot.deliver_proactive_message(
        SimpleNamespace(bot=SimpleNamespace(), job_queue=None),
        key="intraday_nudge",
        sender=sender,
        priority=coach_bot.JOB_PRIORITY_COACHING,
    )

    assert result is False
    assert sent is False


@pytest.mark.asyncio
async def test_proactive_message_is_deferred_during_active_flow(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db = await _job_db(tmp_path, monkeypatch)
    await conversation.set_active_flow(
        db,
        1,
        conversation.FlowName.onboarding_question,
        step="ask",
    )
    sent = False

    async def sender() -> None:
        nonlocal sent
        sent = True

    result = await coach_bot.deliver_proactive_message(
        SimpleNamespace(bot=SimpleNamespace(), job_queue=None),
        key="morning_checkin",
        sender=sender,
        priority=coach_bot.JOB_PRIORITY_SCHEDULED,
    )

    assert result is False
    assert sent is False
