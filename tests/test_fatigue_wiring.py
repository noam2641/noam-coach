"""Tests for build_fatigue_assessment — wiring the fatigue engine to real data."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import coach_bot
import health_service

_PLAN = {"name": "Test", "exercises": [{"id": "squat", "name": "סקוואט", "sets": 3}]}


async def _db(tmp_path: Path, monkeypatch) -> coach_bot.Database:
    db = coach_bot.Database(str(tmp_path / "fatigue.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (coach_bot.utc_now(),),
    )
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(health_service, "DB", db)
    return db


async def _add_session(db, *, rir: int, weight: float, reps: int) -> int:
    sid = await db.execute(
        "INSERT INTO sessions(user_id, code, name, plan, status, exercise_index, set_number, started_at, ended_at) "
        "VALUES(1,'T','Test',?, 'completed', 0, 3, ?, ?)",
        (json.dumps(_PLAN, ensure_ascii=False), coach_bot.utc_now(), coach_bot.utc_now()),
    )
    for set_no in (1, 2, 3):
        await db.execute(
            "INSERT INTO sets(session_id, exercise_id, exercise_name, set_number, weight, reps, rir, source, created_at) "
            "VALUES(?, 'squat', 'סקוואט', ?, ?, ?, ?, 'telegram_adjusted', ?)",
            (sid, set_no, weight, reps, rir, coach_bot.utc_now()),
        )
    return int(sid)


@pytest.mark.asyncio
async def test_no_assessment_without_enough_history(tmp_path, monkeypatch) -> None:
    await _db(tmp_path, monkeypatch)
    assert await coach_bot.build_fatigue_assessment(1) is None


@pytest.mark.asyncio
async def test_hard_sessions_recommend_deload(tmp_path, monkeypatch) -> None:
    db = await _db(tmp_path, monkeypatch)
    # Two hard sessions (RIR 0) + a poor-sleep check-in -> deload territory.
    await _add_session(db, rir=0, weight=100, reps=5)
    await _add_session(db, rir=0, weight=100, reps=5)
    await coach_bot.set_daily_flags(1, {"sleep_quality": "bad", "energy": "low"})
    assessment = await coach_bot.build_fatigue_assessment(1)
    assert assessment is not None
    assert assessment.deload_recommended is True
    banner = await coach_bot.fatigue_banner(1)
    assert "deload" in banner.lower() or "הקלה" in banner


@pytest.mark.asyncio
async def test_easy_sessions_no_deload(tmp_path, monkeypatch) -> None:
    db = await _db(tmp_path, monkeypatch)
    # Fresh sessions (RIR 3) and progressing load -> no deload, no plateau.
    await _add_session(db, rir=3, weight=100, reps=8)
    await _add_session(db, rir=3, weight=105, reps=8)
    assessment = await coach_bot.build_fatigue_assessment(1)
    assert assessment is not None
    assert assessment.deload_recommended is False
    assert await coach_bot.fatigue_banner(1) == ""


@pytest.mark.asyncio
async def test_unknown_rir_not_treated_as_hard(tmp_path, monkeypatch) -> None:
    db = await _db(tmp_path, monkeypatch)
    # All RIR unknown -> avg falls back to "fresh", so no false deload.
    await _add_session(db, rir=coach_bot.RIR_UNKNOWN, weight=100, reps=6)
    await _add_session(db, rir=coach_bot.RIR_UNKNOWN, weight=100, reps=6)
    assessment = await coach_bot.build_fatigue_assessment(1)
    assert assessment is not None
    assert assessment.deload_recommended is False
