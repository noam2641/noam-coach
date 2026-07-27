"""W1-4 — the weekday detector must disclose when it widened its window.

The defect: ``_historical_workout_weekdays`` retries at 28/60/90/180 days and
then full history until it can return ``target_count`` weekdays, and the
recurrence floor it applied was an ABSOLUTE 2 workouts per weekday. Two workouts
is nothing over 90 or 180 days, so past the default window every weekday
qualified and "I found a pattern" became unconditional.

Reproduced against the live database (user 322493274, 296 workouts spanning
2025-03-14..2026-06-14, target_count=4) BEFORE the fix::

     28d -> [0, 2]                 2 days  -> not enough, widen
     60d -> [0, 1, 2, 5, 6]        5 days  -> returns [0, 2, 5, 6]
     90d -> all 7 weekdays                 -> floor of 2 admits everything
    180d -> all 7 weekdays

and AFTER the fix, with the floor scaled to the window (2/3/4/4)::

     28d floor=2 -> [2, 0]              2 days -> not enough, widen
     60d floor=3 -> [6, 2, 0, 5]        4 days -> returns [6, 0, 2, 5], WIDENED
     90d floor=4 -> [6, 2, 0, 5]        4 days (was all 7)
    180d floor=4 -> [6, 2, 0, 1, 5, 3]  6 days (was all 7)

The answer for target_count=4 is the same set of days, but it is now correctly
flagged as widened, so the user is told the 28-day window failed instead of
being told these are "the days you trained most".

The user-visible half of the bug is the second one: the caller only special-cased
``full_history``, so ``last_60_days`` rendered word-for-word like
``last_28_days`` — "the days you trained most are..." — with no hint that the
default window had failed. That is where the user's disputed Saturday came from.

Two properties are pinned here:

1. DISCLOSURE — a dataset that needs widening is reported as widened; one that
   does not is not. This is what the caller keys its caveat on.
2. CALIBRATION — the floor scales with the window, so a long window no longer
   admits every weekday automatically.

The caller's contract (it returns at most ``target_count`` days, and treats
``len(proposed) < target_count`` as "ask the user instead of guessing") must
keep holding in both cases.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from noam_coach.services import health_jobs

USER_ID = 1

# The detector reads UTC-aware timestamps out of the ``health`` table.
_ANCHOR = dt.datetime(2026, 6, 14, 18, 0, tzinfo=dt.timezone.utc)


class StubDB:
    """Minimal stand-in for the module-level ``DB``.

    ``_historical_workout_weekdays`` issues exactly one query (all workout
    ``start_time`` rows for the user, newest first) and does its own windowing
    in Python, so a list of rows is a faithful substitute. Using a stub rather
    than a real connection also keeps the suite from creating a stray
    ``noam_coach.db`` in the repo root.
    """

    def __init__(self, dates: list[dt.datetime]) -> None:
        self.rows = [
            {"start_time": when.isoformat()}
            for when in sorted(dates, reverse=True)
        ]

    async def fetch_all(
        self, sql: str, parameters: tuple[Any, ...] = ()
    ) -> list[dict[str, Any]]:
        flat = " ".join(sql.split())
        assert "sample_type = 'workout'" in flat
        return list(self.rows)


@pytest.fixture
def stub_db(monkeypatch: pytest.MonkeyPatch):
    """Install a StubDB built from the weekdays/weeks a test asks for."""

    def _install(dates: list[dt.datetime]) -> StubDB:
        db = StubDB(dates)
        monkeypatch.setattr(health_jobs, "DB", db)
        return db

    return _install


def _on_weekday(weekday: int, *, weeks_back: int, anchor: dt.datetime = _ANCHOR) -> dt.datetime:
    """The occurrence of ``weekday`` in the week ``weeks_back`` before ``anchor``.

    Always at or before ``anchor``, so the newest sample never runs past it —
    the detector windows relative to the newest row, not to wall-clock now.
    """
    delta = (anchor.weekday() - weekday) % 7
    return anchor - dt.timedelta(days=delta + 7 * weeks_back)


def _sessions(
    weekdays: list[int], *, weeks: int, every: int = 1, anchor: dt.datetime = _ANCHOR
) -> list[dt.datetime]:
    """One workout on each named weekday, every ``every``-th week, ``weeks`` deep.

    ``every=1`` is a dense routine (a hit on each weekday every week);
    ``every=4`` is the sparse case that cannot clear the recurrence floor
    inside the default 28-day window and therefore forces widening.
    """
    return [
        _on_weekday(weekday, weeks_back=week, anchor=anchor)
        for week in range(0, weeks, every)
        for weekday in weekdays
    ]


# ---------------------------------------------------------------------------
# Disclosure — the headline fix
# ---------------------------------------------------------------------------


def test_dense_recent_pattern_is_not_reported_as_widened() -> None:
    """A clean 4-week Sun/Mon/Wed pattern resolves inside the default window."""
    assert health_jobs.weekday_source_is_widened("last_28_days") is False


@pytest.mark.asyncio
async def test_dense_dataset_resolves_without_widening(stub_db) -> None:
    """Someone training 3 fixed days for 8 weeks needs no widening at all.

    This is the control case: the default 28-day window already contains 4
    hits on each of Sunday/Monday/Wednesday, comfortably over the floor.
    """
    stub_db(_sessions([6, 0, 2], weeks=8))

    days, source = await health_jobs._historical_workout_weekdays(USER_ID, 3)

    assert source == "last_28_days"
    assert health_jobs.weekday_source_is_widened(source) is False
    assert sorted(days) == [0, 2, 6]


@pytest.mark.asyncio
async def test_sparse_dataset_that_requires_widening_is_reported_as_widened(
    stub_db,
) -> None:
    """The reported defect: a sparse recent window forces a wider one.

    Only ONE workout per weekday lands in the last 28 days, so nothing clears
    the recurrence floor there and the detector must widen. Before the fix the
    widened answer was indistinguishable from a default-window answer.
    """
    stub_db(_sessions([6, 0, 2], weeks=40, every=4))

    days, source = await health_jobs._historical_workout_weekdays(USER_ID, 3)

    assert source != "last_28_days"
    assert health_jobs.weekday_source_is_widened(source) is True
    # Still a usable answer — widening is disclosed, not removed.
    assert len(days) == 3


@pytest.mark.asyncio
async def test_widened_source_exposes_its_window_length(stub_db) -> None:
    """The caller needs the actual window to tell the user how far back it looked."""
    stub_db(_sessions([6, 0, 2], weeks=40, every=4))

    _days, source = await health_jobs._historical_workout_weekdays(USER_ID, 3)

    window = health_jobs.weekday_source_window_days(source)
    assert window is not None
    assert window > health_jobs._DEFAULT_WEEKDAY_WINDOW_DAYS


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("last_28_days", False),
        ("last_60_days", True),
        ("last_90_days", True),
        ("last_180_days", True),
        ("full_history", True),
        ("insufficient_history", True),
    ],
)
def test_every_widened_source_is_flagged(source: str, expected: bool) -> None:
    """The pre-fix caller flagged ONLY full_history; 60/90/180 slipped through."""
    assert health_jobs.weekday_source_is_widened(source) is expected


# ---------------------------------------------------------------------------
# Calibration — the floor must scale with the window
# ---------------------------------------------------------------------------


def test_recurrence_floor_scales_with_window() -> None:
    """A 2-workout floor is meaningless over 90+ days; the floor must grow."""
    floors = {
        window: health_jobs._min_recurring_workouts_for_window(window)
        for window in (28, 60, 90, 180)
    }

    assert floors[28] == 2  # unchanged at the default window
    assert floors[60] > floors[28]
    assert floors[90] > floors[60]
    # Non-decreasing throughout, and clamped at both ends.
    assert floors[180] >= floors[90]
    assert min(floors.values()) >= health_jobs._MIN_RECURRING_WORKOUTS_PER_WEEKDAY
    assert max(floors.values()) <= health_jobs._MAX_RECURRING_WORKOUTS_PER_WEEKDAY


def test_widening_never_shrinks_the_answer() -> None:
    """A wider window must never admit FEWER weekdays than a narrower one.

    Regression guard for a real trap found while calibrating this fix: with an
    uncapped scaled floor, a fixed weekday trained once every four weeks cleared
    the 60-day floor but not the 90- or 180-day one. Widening then *shrank* the
    result to nothing and a genuine (if infrequent) routine was reported as no
    pattern at all — strictly worse than the bug being fixed.
    """
    dates = _sessions([6, 0, 2], weeks=40, every=4)
    newest = max(dates)

    admitted_counts = []
    for window in health_jobs._WEEKDAY_WINDOW_LADDER:
        cutoff = newest - dt.timedelta(days=window)
        in_window = [when for when in dates if when >= cutoff]
        admitted_counts.append(
            len(health_jobs._recurring_weekdays_from_dates(in_window, window))
        )

    assert admitted_counts == sorted(admitted_counts), admitted_counts
    assert admitted_counts[-1] == 3  # the real Sun/Mon/Wed routine is found


def test_long_window_no_longer_admits_every_weekday() -> None:
    """The live-data symptom: at 90 days the old floor returned all 7 weekdays.

    Mirrors the real distribution measured at 90 days for user 322493274
    ({0:6, 1:3, 2:6, 3:2, 4:2, 5:5, 6:7}) — every weekday cleared an absolute
    floor of 2, so "found a pattern" was unconditional.
    """
    counts = {0: 6, 1: 3, 2: 6, 3: 2, 4: 2, 5: 5, 6: 7}
    dates = [
        _on_weekday(weekday, weeks_back=week)
        for weekday, count in counts.items()
        for week in range(count)
    ]

    admitted = health_jobs._recurring_weekdays_from_dates(dates, 90)

    assert len(admitted) < 7
    # The genuinely frequent days survive; the 2-hit noise days do not.
    assert 3 not in admitted
    assert 4 not in admitted
    assert 6 in admitted


def test_absolute_floor_still_applies_to_short_windows() -> None:
    """A weekday seen once in 28 days is not a pattern."""
    dates = [
        _ANCHOR - dt.timedelta(days=1),  # one lone workout
    ]

    assert health_jobs._recurring_weekdays_from_dates(dates, 28) == []


# ---------------------------------------------------------------------------
# The caller's contract must survive the recalibration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_never_returns_more_than_target_count(stub_db) -> None:
    """The caller writes the result straight into the plan; it must not overrun."""
    stub_db(_sessions([0, 1, 2, 3, 4, 5, 6], weeks=12))

    for target in (1, 2, 3, 4, 5):
        days, _source = await health_jobs._historical_workout_weekdays(USER_ID, target)
        assert len(days) <= target, f"target={target} returned {days}"


@pytest.mark.asyncio
async def test_sparse_history_still_gets_a_usable_answer(stub_db) -> None:
    """Widening is disclosed, NOT removed — sparse users still get days.

    Deleting the widening would push every sparse user into the "I could not
    tell, please type your days" branch, which is a worse product.
    """
    stub_db(_sessions([6, 0, 2], weeks=40, every=4))

    days, source = await health_jobs._historical_workout_weekdays(USER_ID, 3)

    assert days, "sparse history must still produce a proposal"
    assert len(days) == 3
    assert source != "no_history"


@pytest.mark.asyncio
async def test_no_history_is_unchanged(stub_db) -> None:
    """Empty history keeps its existing sentinel, and is treated as no answer."""
    stub_db([])

    days, source = await health_jobs._historical_workout_weekdays(USER_ID, 3)

    assert days == []
    assert source == "no_history"


@pytest.mark.asyncio
async def test_invalid_target_is_unchanged(stub_db) -> None:
    stub_db(_sessions([0, 2], weeks=8))

    assert await health_jobs._historical_workout_weekdays(USER_ID, 0) == (
        [],
        "invalid_target",
    )


@pytest.mark.asyncio
async def test_truly_insufficient_history_signals_rather_than_inventing(
    stub_db,
) -> None:
    """Asking for 5 days from a strict 2-day routine must not manufacture 5.

    The caller compares ``len(proposed) < target_count`` and asks the user
    instead; that branch only works if the detector declines to pad.
    """
    stub_db(_sessions([0, 2], weeks=10))

    days, source = await health_jobs._historical_workout_weekdays(USER_ID, 5)

    assert len(days) < 5
    assert source == "insufficient_history"
    assert health_jobs.weekday_source_is_widened(source) is True
