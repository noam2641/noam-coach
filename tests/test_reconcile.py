"""Tests for reconcile.py — retrospective reconciliation insights."""

from __future__ import annotations

import json
from typing import Any
from zoneinfo import ZoneInfo

import pytest

import reconcile

TZ = ZoneInfo("Asia/Jerusalem")


class MockDB:
    def __init__(self, data: dict[str, list[dict[str, Any]]]) -> None:
        self._data = data

    async def fetch_all(self, sql: str, parameters: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        for key, rows in self._data.items():
            if key in sql:
                return rows
        return []


def _flag_row(day: str, sleep_quality: str) -> dict[str, Any]:
    return {"day": day, "flags": json.dumps({"sleep_quality": sleep_quality})}


def _session_row(ended_at: str, avg_reps: float) -> dict[str, Any]:
    return {"ended_at": ended_at, "avg_reps": avg_reps}


def _sleep_health_row(start: str, minutes: float) -> dict[str, Any]:
    return {"start_time": start, "value": minutes}


@pytest.mark.asyncio
async def test_reconcile_no_data() -> None:
    db = MockDB({"daily_flags": [], "sessions": [], "health": []})
    insights = await reconcile.run_reconciliation(db, 1, TZ)
    assert insights == []


@pytest.mark.asyncio
async def test_bad_sleep_performance_drop() -> None:
    flags = [
        _flag_row("2026-06-01", "bad"),
        _flag_row("2026-06-02", "bad"),
        _flag_row("2026-06-03", "bad"),
        _flag_row("2026-06-04", "bad"),
        _flag_row("2026-06-05", "good"),
        _flag_row("2026-06-06", "good"),
        _flag_row("2026-06-07", "good"),
        _flag_row("2026-06-08", "good"),
    ]
    sessions = [
        # Bad sleep days: low reps
        _session_row("2026-06-01T18:00:00+03:00", 6.0),
        _session_row("2026-06-02T18:00:00+03:00", 5.5),
        _session_row("2026-06-03T18:00:00+03:00", 6.0),
        _session_row("2026-06-04T18:00:00+03:00", 5.0),
        # Good sleep days: high reps
        _session_row("2026-06-05T18:00:00+03:00", 10.0),
        _session_row("2026-06-06T18:00:00+03:00", 10.5),
        _session_row("2026-06-07T18:00:00+03:00", 9.5),
        _session_row("2026-06-08T18:00:00+03:00", 10.0),
    ]
    db = MockDB({"daily_flags": flags, "sessions": sessions})
    result = await reconcile.reconcile_bad_sleep_performance(db, 1, TZ)
    assert result is not None
    assert result.key == "bad_sleep_perf_drop"
    assert result.proposal is not None
    assert result.proposal_action == "auto_hold_on_bad_sleep"


@pytest.mark.asyncio
async def test_bad_sleep_no_pattern() -> None:
    flags = [
        _flag_row("2026-06-01", "bad"),
        _flag_row("2026-06-02", "bad"),
        _flag_row("2026-06-03", "bad"),
        _flag_row("2026-06-04", "bad"),
        _flag_row("2026-06-05", "good"),
        _flag_row("2026-06-06", "good"),
    ]
    sessions = [
        # Same performance on all days — no drop
        _session_row("2026-06-01T18:00:00+03:00", 10.0),
        _session_row("2026-06-02T18:00:00+03:00", 10.0),
        _session_row("2026-06-03T18:00:00+03:00", 10.0),
        _session_row("2026-06-04T18:00:00+03:00", 10.0),
        _session_row("2026-06-05T18:00:00+03:00", 10.0),
        _session_row("2026-06-06T18:00:00+03:00", 10.0),
    ]
    db = MockDB({"daily_flags": flags, "sessions": sessions})
    result = await reconcile.reconcile_bad_sleep_performance(db, 1, TZ)
    assert result is None


@pytest.mark.asyncio
async def test_sleep_report_mismatch() -> None:
    flags = [
        _flag_row("2026-06-01", "good"),
        _flag_row("2026-06-02", "good"),
        _flag_row("2026-06-03", "good"),
        _flag_row("2026-06-04", "good"),
    ]
    # All reported "good" but measured < 6 hours (360 min)
    sleep_rows = [
        _sleep_health_row("2026-06-01T23:00:00+03:00", 300),
        _sleep_health_row("2026-06-02T23:00:00+03:00", 280),
        _sleep_health_row("2026-06-03T23:00:00+03:00", 310),
        _sleep_health_row("2026-06-04T23:00:00+03:00", 290),
    ]
    db = MockDB({"daily_flags": flags, "health": sleep_rows})
    result = await reconcile.reconcile_sleep_report_accuracy(db, 1, TZ)
    assert result is not None
    assert result.key == "sleep_report_mismatch"
    assert result.proposal is None  # informational only


@pytest.mark.asyncio
async def test_run_reconciliation_sorts_by_confidence() -> None:
    flags = [
        _flag_row("2026-06-01", "bad"),
        _flag_row("2026-06-02", "bad"),
        _flag_row("2026-06-03", "bad"),
        _flag_row("2026-06-04", "bad"),
        _flag_row("2026-06-05", "good"),
        _flag_row("2026-06-06", "good"),
        _flag_row("2026-06-07", "good"),
        _flag_row("2026-06-08", "good"),
    ]
    sessions = [
        _session_row("2026-06-01T18:00:00+03:00", 5.0),
        _session_row("2026-06-02T18:00:00+03:00", 5.0),
        _session_row("2026-06-03T18:00:00+03:00", 5.0),
        _session_row("2026-06-04T18:00:00+03:00", 5.0),
        _session_row("2026-06-05T18:00:00+03:00", 10.0),
        _session_row("2026-06-06T18:00:00+03:00", 10.0),
        _session_row("2026-06-07T18:00:00+03:00", 10.0),
        _session_row("2026-06-08T18:00:00+03:00", 10.0),
    ]
    sleep_rows = [
        _sleep_health_row("2026-06-01T23:00:00+03:00", 300),
        _sleep_health_row("2026-06-02T23:00:00+03:00", 280),
        _sleep_health_row("2026-06-03T23:00:00+03:00", 310),
        _sleep_health_row("2026-06-04T23:00:00+03:00", 290),
        _sleep_health_row("2026-06-05T23:00:00+03:00", 300),
        _sleep_health_row("2026-06-06T23:00:00+03:00", 280),
        _sleep_health_row("2026-06-07T23:00:00+03:00", 310),
        _sleep_health_row("2026-06-08T23:00:00+03:00", 290),
    ]
    db = MockDB({"daily_flags": flags, "sessions": sessions, "health": sleep_rows})
    insights = await reconcile.run_reconciliation(db, 1, TZ)
    assert len(insights) >= 1
    # Should be sorted by confidence descending
    for i in range(len(insights) - 1):
        assert insights[i].confidence >= insights[i + 1].confidence
