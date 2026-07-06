"""RE10-5 regression tests — reported-vs-measured routine discrepancy detection.

Covers:
  * A confirmed, user-reported bedtime/wake-time that disagrees with the
    Health-derived measurement by >= 60 minutes is detected and NOT silently
    overwritten.
  * A small (<60 min) disagreement is treated as noise, not a discrepancy,
    and the Health-derived value is written normally.
  * No confirmed report at all -> no discrepancy, normal sync proceeds.
  * A real workout-frequency disagreement (>=1 session/week) is detected
    (informational only — training_days_per_week is never overwritten here).
"""

from __future__ import annotations

from pathlib import Path

import pytest

import health_service
import user_model
from db import Database
from helpers import utc_now


async def _make_db(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "discrepancy.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'Test',NULL,?)",
        (utc_now(),),
    )
    return db


@pytest.mark.asyncio
async def test_large_bedtime_gap_is_detected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = await _make_db(tmp_path)
    monkeypatch.setattr(health_service, "DB", db)
    await user_model.set_fact(
        db, 1, "sleep_schedule", {"bedtime": "23:00", "wake_time": "07:00"},
        kind=user_model.KIND_FACT, source=user_model.SOURCE_USER, confirmed=True,
    )

    measured = {"sleep": {"typical_bedtime": "00:15", "typical_wake_time": "07:00", "nights_sampled": 10}}
    discrepancies = await health_service.detect_routine_discrepancies(1, measured)

    bedtime_issues = [d for d in discrepancies if d["field"] == "bedtime"]
    assert len(bedtime_issues) == 1
    assert bedtime_issues[0]["reported"] == "23:00"
    assert bedtime_issues[0]["measured"] == "00:15"


@pytest.mark.asyncio
async def test_small_bedtime_gap_is_not_a_discrepancy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = await _make_db(tmp_path)
    monkeypatch.setattr(health_service, "DB", db)
    await user_model.set_fact(
        db, 1, "sleep_schedule", {"bedtime": "23:00", "wake_time": "07:00"},
        kind=user_model.KIND_FACT, source=user_model.SOURCE_USER, confirmed=True,
    )

    measured = {"sleep": {"typical_bedtime": "23:20", "typical_wake_time": "07:00", "nights_sampled": 10}}
    discrepancies = await health_service.detect_routine_discrepancies(1, measured)
    assert discrepancies == []


@pytest.mark.asyncio
async def test_no_confirmed_report_means_no_discrepancy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = await _make_db(tmp_path)
    monkeypatch.setattr(health_service, "DB", db)
    # No sleep_schedule fact at all.
    measured = {"sleep": {"typical_bedtime": "00:15", "typical_wake_time": "07:00", "nights_sampled": 10}}
    discrepancies = await health_service.detect_routine_discrepancies(1, measured)
    assert discrepancies == []


@pytest.mark.asyncio
async def test_unconfirmed_report_does_not_trigger_discrepancy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path)
    monkeypatch.setattr(health_service, "DB", db)
    await user_model.set_fact(
        db, 1, "sleep_schedule", {"bedtime": "23:00", "wake_time": "07:00"},
        kind=user_model.KIND_FACT, source=user_model.SOURCE_USER, confirmed=False,
    )
    measured = {"sleep": {"typical_bedtime": "00:15", "typical_wake_time": "07:00", "nights_sampled": 10}}
    discrepancies = await health_service.detect_routine_discrepancies(1, measured)
    assert discrepancies == []


@pytest.mark.asyncio
async def test_confirmed_report_is_not_silently_overwritten_on_discrepancy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The core bug this closes: sync_routine_to_facts used to silently
    replace a user-confirmed sleep_schedule with the Health-derived one."""
    db = await _make_db(tmp_path)
    monkeypatch.setattr(health_service, "DB", db)
    await user_model.set_fact(
        db, 1, "sleep_schedule", {"bedtime": "23:00", "wake_time": "07:00"},
        kind=user_model.KIND_FACT, source=user_model.SOURCE_USER, confirmed=True,
    )

    await health_service.sync_routine_to_facts(
        1, {"sleep": {"typical_bedtime": "00:15", "typical_wake_time": "07:00", "nights_sampled": 10}}
    )

    fact = await user_model.get_fact(db, 1, "sleep_schedule")
    assert fact["source"] == user_model.SOURCE_USER
    assert fact["value"]["bedtime"] == "23:00"  # unchanged — not silently overwritten


@pytest.mark.asyncio
async def test_agreeing_values_sync_normally(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = await _make_db(tmp_path)
    monkeypatch.setattr(health_service, "DB", db)
    await user_model.set_fact(
        db, 1, "sleep_schedule", {"bedtime": "23:00", "wake_time": "07:00"},
        kind=user_model.KIND_FACT, source=user_model.SOURCE_USER, confirmed=True,
    )

    await health_service.sync_routine_to_facts(
        1, {"sleep": {"typical_bedtime": "23:05", "typical_wake_time": "07:00", "nights_sampled": 10}}
    )

    fact = await user_model.get_fact(db, 1, "sleep_schedule")
    # Small agreement -> normal sync proceeds, value becomes the measured one.
    assert fact["source"] == user_model.SOURCE_DERIVED
    assert fact["value"]["typical_bedtime"] == "23:05"


@pytest.mark.asyncio
async def test_workout_frequency_discrepancy_detected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = await _make_db(tmp_path)
    monkeypatch.setattr(health_service, "DB", db)
    await user_model.set_fact(
        db, 1, "training_days_per_week", 3,
        kind=user_model.KIND_FACT, source=user_model.SOURCE_USER, confirmed=True,
    )
    measured = {"workout": {"weekly_frequency": 5, "sessions_sampled": 12}}
    discrepancies = await health_service.detect_routine_discrepancies(1, measured)
    freq_issues = [d for d in discrepancies if d["fact_key"] == "training_days_per_week"]
    assert len(freq_issues) == 1
    assert freq_issues[0]["reported"] == 3.0
    assert freq_issues[0]["measured"] == 5


@pytest.mark.asyncio
async def test_workout_frequency_small_gap_not_flagged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = await _make_db(tmp_path)
    monkeypatch.setattr(health_service, "DB", db)
    await user_model.set_fact(
        db, 1, "training_days_per_week", 3,
        kind=user_model.KIND_FACT, source=user_model.SOURCE_USER, confirmed=True,
    )
    measured = {"workout": {"weekly_frequency": 3.2, "sessions_sampled": 12}}
    discrepancies = await health_service.detect_routine_discrepancies(1, measured)
    assert discrepancies == []
