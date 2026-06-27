"""Integration tests for the modular HealthKit, Shortcuts and Watch routes."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import coach_bot


async def _database(tmp_path: Path, monkeypatch) -> coach_bot.Database:
    database = coach_bot.Database(str(tmp_path / "device_routes.db"))
    await database.init()
    monkeypatch.setattr(coach_bot, "DB", database)
    return database


@pytest.mark.asyncio
async def test_healthkit_batch_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    database = await _database(tmp_path, monkeypatch)
    user_id = coach_bot.SETTINGS.telegram_allowed_user_id
    batch = coach_bot.HealthBatch(
        telegram_user_id=user_id,
        samples=[
            coach_bot.HealthSample(
                external_id="healthkit:weight:1",
                sample_type="weight",
                value=88.4,
                unit="kg",
                start_time=datetime(2026, 6, 24, 8, 0, tzinfo=timezone.utc),
            )
        ],
    )

    assert await coach_bot.healthkit_samples(batch) == {"inserted": 1, "duplicated": 0}
    assert await coach_bot.healthkit_samples(batch) == {"inserted": 0, "duplicated": 1}
    rows = await database.fetch_all(
        "SELECT external_id, value FROM health WHERE user_id=?",
        (user_id,),
    )
    assert rows == [{"external_id": "healthkit:weight:1", "value": 88.4}]


@pytest.mark.asyncio
async def test_shortcut_cumulative_values_overwrite_same_day(tmp_path: Path, monkeypatch) -> None:
    database = await _database(tmp_path, monkeypatch)
    user_id = coach_bot.SETTINGS.telegram_allowed_user_id
    base = dict(
        telegram_user_id=user_id,
        measured_at=datetime(2026, 6, 24, 8, 0, tzinfo=timezone.utc),
    )
    first = coach_bot.ShortcutHealthPayload(**base, steps=1_000, active_calories=200)
    second = coach_bot.ShortcutHealthPayload(**base, steps=1_500, active_calories=260)

    first_result = await coach_bot.shortcut_health(first)
    assert first_result["inserted"] == 2
    second_result = await coach_bot.shortcut_health(second)
    assert second_result["inserted"] == 0
    assert second_result["updated"] == 2
    rows = await database.fetch_all(
        "SELECT sample_type, value FROM health WHERE user_id=? ORDER BY sample_type",
        (user_id,),
    )
    assert rows == [
        {"sample_type": "active_calories", "value": 260.0},
        {"sample_type": "steps", "value": 1500.0},
    ]


@pytest.mark.asyncio
async def test_watch_current_and_set_are_idempotent(tmp_path: Path, monkeypatch) -> None:
    database = await _database(tmp_path, monkeypatch)
    user_id = coach_bot.SETTINGS.telegram_allowed_user_id
    await coach_bot.ensure_user_record(user_id)
    assert await coach_bot.watch_current(user_id) == {"active": False}

    plan = {
        "name": "Test",
        "exercises": [
            {
                "id": "bench",
                "name": "לחיצת חזה",
                "muscle": "חזה",
                "sets": 2,
                "rmin": 6,
                "rmax": 10,
                "inc": 2.5,
                "rest": 90,
                "weight": 50,
                "cues": ["שכמות אחורה"],
                "alts": [],
            }
        ],
    }
    session_id = await database.execute(
        "INSERT INTO sessions(user_id, code, name, plan, status, exercise_index, "
        "set_number, started_at) VALUES(?, 'T', 'Test', ?, 'active', 0, 1, ?)",
        (user_id, json.dumps(plan, ensure_ascii=False), coach_bot.utc_now()),
    )

    current = await coach_bot.watch_current(user_id)
    assert current["active"] is True
    assert current["session_id"] == session_id
    assert current["exercise_name"] == "לחיצת חזה"

    payload = coach_bot.WatchSetPayload(
        telegram_user_id=user_id,
        client_event_id="watch-event-1",
        reps=8,
        weight=50,
    )
    first = await coach_bot.watch_set(payload)
    second = await coach_bot.watch_set(payload)
    assert first["saved"] is True and first["duplicate"] is False
    assert second == {"saved": True, "duplicate": True}


@pytest.mark.asyncio
async def test_readyz_reports_ready_with_healthy_dependencies(
    tmp_path: Path,
    monkeypatch,
) -> None:
    await _database(tmp_path, monkeypatch)
    monkeypatch.setattr(coach_bot.SETTINGS, "storage_dir", str(tmp_path / "storage"))
    monkeypatch.setattr(coach_bot.SETTINGS, "readiness_min_free_mb", 0)
    monkeypatch.setattr(coach_bot.RUNTIME_STATE, "db_ready", True)
    monkeypatch.setattr(coach_bot.RUNTIME_STATE, "telegram_ready", True)

    response = await coach_bot.readyz()
    assert response.status_code == 200
    payload = json.loads(response.body)
    assert payload["status"] == "ready"
    assert payload["checks"]["database"] is True
    assert payload["checks"]["storage"] is True
