"""Tests for routine.py — robust statistics and learned patterns."""

from __future__ import annotations

import datetime as dt
from typing import Any
from zoneinfo import ZoneInfo

import pytest

import routine

TZ = ZoneInfo("Asia/Jerusalem")


# ---------------------------------------------------------------------------
# Robust statistics
# ---------------------------------------------------------------------------


def test_remove_outliers_keeps_small_samples() -> None:
    assert routine.remove_outliers([1, 2, 3]) == [1, 2, 3]


def test_remove_outliers_drops_extremes() -> None:
    vals = [10, 11, 12, 13, 100]
    cleaned = routine.remove_outliers(vals)
    assert 100 not in cleaned
    assert len(cleaned) >= 3


def test_remove_outliers_empty() -> None:
    assert routine.remove_outliers([]) == []


def test_robust_mean_with_outlier() -> None:
    vals = [10, 11, 12, 13, 100]
    result = routine.robust_mean(vals)
    assert result is not None
    assert 10 <= result <= 14


def test_robust_mean_empty() -> None:
    assert routine.robust_mean([]) is None


def test_robust_median_basic() -> None:
    vals = [5, 6, 7, 8, 50]
    result = routine.robust_median(vals)
    assert result is not None
    assert 5 <= result <= 9


def test_robust_median_empty() -> None:
    assert routine.robust_median([]) is None


def test_circular_hour_mean_wraps_midnight() -> None:
    # 23:30 and 00:30 should average to ~00:00, not 12:00
    result = routine.circular_hour_mean([23.5, 0.5])
    assert result is not None
    assert result > 23.0 or result < 1.0


def test_circular_hour_mean_empty() -> None:
    assert routine.circular_hour_mean([]) is None


def test_hour_to_hhmm() -> None:
    assert routine.hour_to_hhmm(8.5) == "08:30"
    assert routine.hour_to_hhmm(0.0) == "00:00"
    assert routine.hour_to_hhmm(23.75) == "23:45"
    assert routine.hour_to_hhmm(None) is None


# ---------------------------------------------------------------------------
# Learned profiles (async, with mock DB)
# ---------------------------------------------------------------------------


class MockDB:
    """Fake DB that returns pre-set rows."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self._rows = rows or []
        self._query_log: list[str] = []

    async def fetch_all(self, sql: str, parameters: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        self._query_log.append(sql)
        return self._rows


def _sleep_row(start: str, end: str, minutes: float = 420.0) -> dict[str, Any]:
    return {"start_time": start, "end_time": end, "value": minutes}


def _workout_row(start: str, duration: float = 45.0) -> dict[str, Any]:
    return {"start_time": start, "value": duration}


def _meal_row(eaten_at: str, calories: float = 500.0) -> dict[str, Any]:
    return {"eaten_at": eaten_at, "calories": calories}


@pytest.mark.asyncio
async def test_learn_sleep_schedule_empty() -> None:
    db = MockDB([])
    result = await routine.learn_sleep_schedule(db, 1, TZ)
    assert result.typical_bedtime is None
    assert result.nights_sampled == 0


@pytest.mark.asyncio
async def test_learn_sleep_schedule_with_data() -> None:
    rows = [
        _sleep_row("2026-06-10T23:00:00+03:00", "2026-06-11T07:00:00+03:00", 480),
        _sleep_row("2026-06-11T23:30:00+03:00", "2026-06-12T06:30:00+03:00", 420),
        _sleep_row("2026-06-12T22:45:00+03:00", "2026-06-13T07:15:00+03:00", 510),
    ]
    db = MockDB(rows)
    result = await routine.learn_sleep_schedule(db, 1, TZ)
    assert result.nights_sampled == 3
    assert result.typical_bedtime is not None
    assert result.typical_wake_time is not None
    assert result.avg_duration_minutes is not None


@pytest.mark.asyncio
async def test_learn_sleep_schedule_uses_full_history_without_wear_gate() -> None:
    rows = [
        _sleep_row("2026-01-10T23:05:00+03:00", "2026-01-11T07:05:00+03:00", 480),
        _sleep_row("2026-01-11T23:10:00+03:00", "2026-01-12T07:00:00+03:00", 470),
        _sleep_row("2026-01-12T23:00:00+03:00", "2026-01-13T07:15:00+03:00", 495),
        _sleep_row("2026-01-13T23:20:00+03:00", "2026-01-14T07:10:00+03:00", 470),
        _sleep_row("2026-01-14T23:15:00+03:00", "2026-01-15T07:05:00+03:00", 470),
        _sleep_row("2026-01-15T23:05:00+03:00", "2026-01-16T06:55:00+03:00", 470),
        _sleep_row("2026-01-16T23:10:00+03:00", "2026-01-17T07:10:00+03:00", 480),
    ]
    db = MockDB(rows)

    result = await routine.learn_sleep_schedule(db, 1, TZ)

    assert result.nights_sampled == 7
    assert result.typical_bedtime is not None
    assert result.typical_wake_time is not None
    assert result.avg_duration_minutes is not None
    assert result.variability_minutes is not None
    assert "watch_wear" not in db._query_log[-1]
    assert "start_time>=" not in db._query_log[-1].replace(" ", "")


@pytest.mark.asyncio
async def test_learn_sleep_schedule_merges_overlapping_sources_per_night() -> None:
    rows = [
        _sleep_row("2026-06-10T23:00:00+03:00", "2026-06-11T07:00:00+03:00", 480),
        _sleep_row("2026-06-10T23:30:00+03:00", "2026-06-11T06:30:00+03:00", 420),
    ]
    db = MockDB(rows)

    result = await routine.learn_sleep_schedule(db, 1, TZ)

    assert result.nights_sampled == 1
    assert result.avg_duration_minutes == pytest.approx(480.0)


@pytest.mark.asyncio
async def test_learn_sleep_schedule_weights_recent_history_more() -> None:
    old_rows = [
        _sleep_row(
            (dt.datetime(2026, 1, day, 1, 0, tzinfo=TZ)).isoformat(),
            (dt.datetime(2026, 1, day, 9, 0, tzinfo=TZ)).isoformat(),
            480,
        )
        for day in range(10, 17)
    ]
    recent_rows = [
        _sleep_row(
            (dt.datetime(2026, 6, day, 23, 0, tzinfo=TZ)).isoformat(),
            (dt.datetime(2026, 6, day + 1, 7, 0, tzinfo=TZ)).isoformat(),
            480,
        )
        for day in range(10, 13)
    ]
    db = MockDB([*old_rows, *recent_rows])

    result = await routine.learn_sleep_schedule(db, 1, TZ)

    assert result.nights_sampled == 10
    assert result.typical_bedtime is not None
    assert result.typical_bedtime.startswith("23")


@pytest.mark.asyncio
async def test_learn_workout_pattern_empty() -> None:
    db = MockDB([])
    result = await routine.learn_workout_pattern(db, 1, TZ)
    assert result.weekly_frequency is None
    assert result.sessions_sampled == 0


@pytest.mark.asyncio
async def test_learn_workout_pattern_with_data() -> None:
    rows = [
        _workout_row("2026-06-02T10:00:00+03:00", 50),
        _workout_row("2026-06-04T10:30:00+03:00", 45),
        _workout_row("2026-06-06T09:45:00+03:00", 55),
        _workout_row("2026-06-09T10:15:00+03:00", 48),
    ]
    db = MockDB(rows)
    result = await routine.learn_workout_pattern(db, 1, TZ)
    assert result.sessions_sampled == 4
    assert result.weekly_frequency is not None
    assert result.typical_hour is not None
    assert result.avg_duration_minutes is not None


@pytest.mark.asyncio
async def test_learn_eating_windows_empty() -> None:
    db = MockDB([])
    result = await routine.learn_eating_windows(db, 1, TZ)
    assert result.first_meal_time is None
    assert result.meals_sampled == 0


@pytest.mark.asyncio
async def test_learn_eating_windows_with_data() -> None:
    rows = [
        _meal_row("2026-06-10T08:00:00+03:00", 400),
        _meal_row("2026-06-10T13:00:00+03:00", 600),
        _meal_row("2026-06-10T20:00:00+03:00", 500),
        _meal_row("2026-06-11T08:30:00+03:00", 350),
        _meal_row("2026-06-11T13:30:00+03:00", 550),
        _meal_row("2026-06-11T19:30:00+03:00", 600),
    ]
    db = MockDB(rows)
    result = await routine.learn_eating_windows(db, 1, TZ)
    assert result.meals_sampled == 6
    assert result.first_meal_time is not None
    assert result.last_meal_time is not None
    assert result.avg_daily_calories is not None


@pytest.mark.asyncio
async def test_routine_profile_to_dict() -> None:
    profile = routine.RoutineProfile()
    d = profile.to_dict()
    assert "sleep" in d
    assert "workout" in d
    assert "eating" in d
