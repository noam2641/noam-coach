"""W1-3 — learning windows must be anchored to the data, not to today.

The defect: ``learn_workout_pattern`` and ``learn_eating_windows`` measured
``window_days`` back from ``now()``. A HealthKit export that ends weeks before
the session therefore intersected the window almost nowhere — a real export
ending 2026-06-14, analyzed on 2026-07-27, matched 1 of 296 workouts and
produced ``weekly_frequency = 0.2`` sessions/week, while the file's own last
45 days contain 10 sessions.

The fix mirrors what ``average_daily_steps`` already does: anchor the window
to the dataset's own end (``newest_health_sample_date`` for health rows,
``MAX(eaten_at)`` for the live-logged ``meals`` table).

The central property tested here is EQUIVALENCE UNDER TRANSLATION: shifting an
entire dataset back in time must not change what is learned from it. That is
also the backwards-compatibility proof — a user whose data is current sits at
offset 0, the case every other test already pins.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict
from typing import Any
from zoneinfo import ZoneInfo

import pytest

import routine

TZ = ZoneInfo("Asia/Jerusalem")
USER_ID = 1

# How stale the fixture datasets are. Larger than DEFAULT_WINDOW_DAYS (45) so a
# now()-anchored window misses the data entirely — this is the real-world case
# (the reported export was 43 days old and the session window was 45 days).
STALE_OFFSET_DAYS = 70


class RoutingDB:
    """Mock DB that answers each learner's query with the right rows.

    ``routine`` issues several different queries per learner (workouts, wear,
    the newest-sample anchor, meals). Routing on the SQL text keeps one fixture
    usable for all of them, the way tests/regression/test_re12_wear_and_wizard
    does.
    """

    def __init__(
        self,
        *,
        workouts: list[dict[str, Any]] | None = None,
        wear: list[dict[str, Any]] | None = None,
        sleep: list[dict[str, Any]] | None = None,
        meals: list[dict[str, Any]] | None = None,
    ) -> None:
        self.workouts = workouts or []
        self.wear = wear or []
        self.sleep = sleep or []
        self.meals = meals or []

    def _newest_health(self) -> str | None:
        stamps = [
            row["start_time"]
            for row in (*self.workouts, *self.wear, *self.sleep)
            if row.get("start_time")
        ]
        return max(stamps) if stamps else None

    async def fetch_all(
        self, sql: str, parameters: tuple[Any, ...] = ()
    ) -> list[dict[str, Any]]:
        flat = " ".join(sql.split())
        if "MAX(eaten_at)" in flat:
            stamps = [row["eaten_at"] for row in self.meals if row.get("eaten_at")]
            return [{"newest": max(stamps) if stamps else None}]
        if "FROM meals" in flat:
            return _between(self.meals, "eaten_at", parameters)
        if "MAX(start_time)" in flat or "MAX(COALESCE(end_time, start_time))" in flat:
            newest = self._newest_health()
            key = "newest" if "MAX(start_time)" in flat else "latest"
            return [{key: newest}]
        if "sample_type='workout'" in flat:
            return _between(self.workouts, "start_time", parameters)
        if "sample_type='sleep_session'" in flat:
            return self.sleep  # sleep is deliberately unwindowed (see docstring)
        if "watch_wear" in flat:
            return _between(self.wear, "start_time", parameters)
        return []


def _between(
    rows: list[dict[str, Any]], key: str, parameters: tuple[Any, ...]
) -> list[dict[str, Any]]:
    """Apply the query's own [since, until) bounds, like real SQL would.

    A mock that ignores the bounds cannot detect a window bug at all, so the
    filtering has to be real. Timestamps are compared as aware datetimes rather
    than ISO strings because the fixtures are local-tz and the bounds are UTC.
    """
    bounds = [p for p in parameters[1:] if isinstance(p, str)]
    if not bounds:
        return rows
    since = dt.datetime.fromisoformat(bounds[0])
    until = dt.datetime.fromisoformat(bounds[1]) if len(bounds) > 1 else None
    kept = []
    for row in rows:
        when = dt.datetime.fromisoformat(row[key])
        if when < since:
            continue
        if until is not None and when >= until:
            continue
        kept.append(row)
    return kept


# ---------------------------------------------------------------------------
# Fixture builders. Every dataset is generated relative to ``end_day`` so the
# SAME routine can be produced as either current or stale simply by moving the
# end day back.
# ---------------------------------------------------------------------------


def _last_sunday(reference: dt.date) -> dt.date:
    """The most recent Sunday on/before ``reference``.

    Weeks are Mon-Sun, so ending a dataset on a Sunday makes the trailing weeks
    complete and keeps the wear-aware weekly-frequency path deterministic.
    """
    return reference - dt.timedelta(days=(reference.weekday() + 1) % 7)


def _workout_dataset(end_day: dt.date, *, weeks: int = 4) -> dict[str, list[Any]]:
    """``weeks`` fully-worn Mon-Sun weeks with 3 workouts each (Mon/Wed/Fri)."""
    workouts: list[dict[str, Any]] = []
    wear: list[dict[str, Any]] = []
    first_monday = end_day - dt.timedelta(days=7 * weeks - 1)
    for week in range(weeks):
        monday = first_monday + dt.timedelta(days=7 * week)
        for offset in range(7):
            day = monday + dt.timedelta(days=offset)
            wear.append({
                "sample_type": "watch_wear",
                "start_time": dt.datetime.combine(day, dt.time(8, 0), tzinfo=TZ).isoformat(),
                "end_time": dt.datetime.combine(day, dt.time(22, 0), tzinfo=TZ).isoformat(),
                "source_device": "apple_watch",
            })
        for offset in (0, 2, 4):  # Mon / Wed / Fri
            start = dt.datetime.combine(
                monday + dt.timedelta(days=offset), dt.time(18, 30), tzinfo=TZ
            )
            workouts.append({"start_time": start.isoformat(), "value": 45.0})
    return {"workouts": workouts, "wear": wear}


def _meal_dataset(end_day: dt.date, *, days: int = 14) -> list[dict[str, Any]]:
    """``days`` consecutive days of 3 logged meals at 08:00 / 13:00 / 19:30."""
    meals: list[dict[str, Any]] = []
    for offset in range(days):
        day = end_day - dt.timedelta(days=days - 1 - offset)
        for hour, minute, calories in ((8, 0, 400.0), (13, 0, 650.0), (19, 30, 550.0)):
            eaten = dt.datetime.combine(day, dt.time(hour, minute), tzinfo=TZ)
            meals.append({"eaten_at": eaten.isoformat(), "calories": calories})
    return meals


def _sleep_dataset(end_day: dt.date, *, nights: int = 12) -> list[dict[str, Any]]:
    """``nights`` consecutive nights of 23:00 -> 07:00 sleep.

    The last night STARTS on ``end_day``; routine attributes a 23:00 segment to
    the "night of" that date, so ``end_day`` is also the newest night key.
    """
    rows: list[dict[str, Any]] = []
    for offset in range(nights):
        night = end_day - dt.timedelta(days=nights - 1 - offset)
        start = dt.datetime.combine(night, dt.time(23, 0), tzinfo=TZ)
        end = dt.datetime.combine(night + dt.timedelta(days=1), dt.time(7, 0), tzinfo=TZ)
        rows.append({
            "sample_type": "sleep_session",
            "start_time": start.isoformat(),
            "end_time": end.isoformat(),
            "value": 480.0,
        })
    return rows


def _today() -> dt.date:
    return dt.datetime.now(TZ).date()


# ---------------------------------------------------------------------------
# learn_workout_pattern
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workout_stale_dataset_matches_fresh_dataset() -> None:
    """The headline fix: the same routine, 70 days stale, learns the same.

    Before the fix the stale dataset fell outside the now()-anchored 45-day
    window and produced an empty WorkoutPattern.
    """
    fresh_end = _last_sunday(_today())
    stale_end = fresh_end - dt.timedelta(days=STALE_OFFSET_DAYS)

    fresh = RoutingDB(**_workout_dataset(fresh_end))
    stale = RoutingDB(**_workout_dataset(stale_end))

    fresh_pattern = await routine.learn_workout_pattern(fresh, USER_ID, TZ)
    stale_pattern = await routine.learn_workout_pattern(stale, USER_ID, TZ)

    # The defect: the stale export learned nothing at all.
    assert stale_pattern.sessions_sampled > 0
    assert stale_pattern.weekly_frequency == pytest.approx(3.0)
    # Shifting a dataset in time must not change what is learned from it.
    assert asdict(stale_pattern) == asdict(fresh_pattern)


@pytest.mark.asyncio
async def test_workout_stale_dataset_reports_real_frequency_not_near_zero() -> None:
    """Reproduces the reported number: 3/week must not collapse to ~0.2/week.

    Pinned separately from the equivalence test because this is the
    user-visible symptom (a profile claiming 0.2 sessions/week for a user who
    trains three times a week).
    """
    stale_end = _last_sunday(_today()) - dt.timedelta(days=STALE_OFFSET_DAYS)
    db = RoutingDB(**_workout_dataset(stale_end))

    pattern = await routine.learn_workout_pattern(db, USER_ID, TZ)

    assert pattern.weekly_frequency is not None
    assert pattern.weekly_frequency >= 2.5
    assert pattern.sessions_sampled == 12  # 4 weeks x 3 sessions
    # Weekday/hour learning must survive the shift too.
    assert pattern.typical_hour == "18:30"
    assert sorted(pattern.common_weekdays) == [0, 2, 4]


@pytest.mark.asyncio
async def test_workout_recent_window_also_anchored() -> None:
    """RECENT_WINDOW_DAYS rides on the same anchor, not on wall-clock time."""
    stale_end = _last_sunday(_today()) - dt.timedelta(days=STALE_OFFSET_DAYS)
    db = RoutingDB(**_workout_dataset(stale_end))

    pattern = await routine.learn_workout_pattern(db, USER_ID, TZ)

    assert pattern.recent_sessions_sampled > 0
    assert pattern.recent_weekly_frequency == pytest.approx(3.0)


@pytest.mark.asyncio
async def test_workout_current_dataset_unchanged() -> None:
    """Backwards compatibility: a current dataset yields the pre-fix result.

    Pins the values the now()-anchored implementation produced for a user whose
    export is up to date — 4 fully-worn weeks, 3 sessions each.
    """
    db = RoutingDB(**_workout_dataset(_last_sunday(_today())))

    pattern = await routine.learn_workout_pattern(db, USER_ID, TZ)

    assert pattern.sessions_sampled == 12
    assert pattern.weekly_frequency == pytest.approx(3.0)
    assert pattern.wear_filtered is True
    assert pattern.valid_weeks_sampled == 4
    assert pattern.typical_hour == "18:30"
    assert pattern.avg_duration_minutes == pytest.approx(45.0)


@pytest.mark.asyncio
async def test_workout_explicit_anchor_is_honoured() -> None:
    """An explicit ``anchor`` overrides the dataset lookup, like average_daily_steps."""
    end_day = _last_sunday(_today()) - dt.timedelta(days=STALE_OFFSET_DAYS)
    db = RoutingDB(**_workout_dataset(end_day))

    # Anchoring far in the past excludes the dataset -> nothing learned.
    old = await routine.learn_workout_pattern(
        db, USER_ID, TZ, anchor=end_day - dt.timedelta(days=365)
    )
    assert old.sessions_sampled == 0

    at_end = await routine.learn_workout_pattern(db, USER_ID, TZ, anchor=end_day)
    assert at_end.sessions_sampled == 12


@pytest.mark.asyncio
async def test_workout_empty_dataset_does_not_crash() -> None:
    """No health rows at all: the anchor lookup returns None, no exception."""
    pattern = await routine.learn_workout_pattern(RoutingDB(), USER_ID, TZ)

    assert pattern == routine.WorkoutPattern()
    assert pattern.weekly_frequency is None
    assert pattern.sessions_sampled == 0


# ---------------------------------------------------------------------------
# learn_eating_windows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_eating_stale_dataset_matches_fresh_dataset() -> None:
    """Meals anchor to MAX(eaten_at), so a paused logger keeps their pattern."""
    fresh_end = _today()
    stale_end = fresh_end - dt.timedelta(days=STALE_OFFSET_DAYS)

    fresh = RoutingDB(meals=_meal_dataset(fresh_end))
    stale = RoutingDB(meals=_meal_dataset(stale_end))

    fresh_windows = await routine.learn_eating_windows(fresh, USER_ID, TZ)
    stale_windows = await routine.learn_eating_windows(stale, USER_ID, TZ)

    # The defect: an old logging streak returned a blank EatingWindows().
    assert stale_windows.meals_sampled > 0
    assert asdict(stale_windows) == asdict(fresh_windows)


@pytest.mark.asyncio
async def test_eating_current_dataset_unchanged() -> None:
    """Backwards compatibility: a currently-logging user is unaffected."""
    db = RoutingDB(meals=_meal_dataset(_today()))

    windows = await routine.learn_eating_windows(db, USER_ID, TZ)

    assert windows.meals_sampled == 42  # 14 days x 3 meals
    assert windows.first_meal_time == "08:00"
    assert windows.last_meal_time == "19:30"
    assert windows.typical_meal_hours == ["08:00", "13:00", "20:00"]
    assert windows.avg_daily_calories == pytest.approx(1600.0)


@pytest.mark.asyncio
async def test_eating_uses_meals_anchor_not_health_anchor() -> None:
    """The meals window must not borrow the HealthKit export's end date.

    A stale export alongside current meal logging is the common real case; if
    eating anchored on newest_health_sample_date it would window live meals
    against an unrelated clock and drop them all.
    """
    stale_health_end = _last_sunday(_today()) - dt.timedelta(days=STALE_OFFSET_DAYS)
    db = RoutingDB(
        **_workout_dataset(stale_health_end),
        meals=_meal_dataset(_today()),
    )

    windows = await routine.learn_eating_windows(db, USER_ID, TZ)

    assert windows.meals_sampled == 42
    assert windows.first_meal_time == "08:00"


@pytest.mark.asyncio
async def test_eating_empty_dataset_does_not_crash() -> None:
    """No meals at all: MAX(eaten_at) is NULL, no exception."""
    windows = await routine.learn_eating_windows(RoutingDB(), USER_ID, TZ)

    assert windows == routine.EatingWindows()
    assert windows.meals_sampled == 0
    assert windows.first_meal_time is None


# ---------------------------------------------------------------------------
# learn_sleep_schedule — deliberately NOT windowed; see its docstring.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sleep_stale_dataset_matches_fresh_dataset() -> None:
    """Sleep already anchored its half-life on the newest night in the data."""
    fresh_end = _today()
    stale_end = fresh_end - dt.timedelta(days=STALE_OFFSET_DAYS)

    fresh = await routine.learn_sleep_schedule(
        RoutingDB(sleep=_sleep_dataset(fresh_end)), USER_ID, TZ
    )
    stale = await routine.learn_sleep_schedule(
        RoutingDB(sleep=_sleep_dataset(stale_end)), USER_ID, TZ
    )

    assert stale.nights_sampled == 12
    # window_end tracks the data, so only that field may differ.
    assert stale.window_end != fresh.window_end
    fresh_fields = {k: v for k, v in asdict(fresh).items() if k != "window_end"}
    stale_fields = {k: v for k, v in asdict(stale).items() if k != "window_end"}
    assert stale_fields == fresh_fields


@pytest.mark.asyncio
async def test_sleep_nights_sampled_is_not_narrowed() -> None:
    """``nights_sampled`` must keep its old meaning — callers key confidence on it.

    health_quality._sleep_section and the health_jobs wizard both threshold on
    ``nights_sampled``; narrowing it here would silently move their behavior.
    W1-3 discloses the gap through a NEW field instead.
    """
    end_day = _today()
    # 10 nights inside the 45-day window, 6 far outside it.
    recent = _sleep_dataset(end_day, nights=10)
    old = _sleep_dataset(end_day - dt.timedelta(days=200), nights=6)
    db = RoutingDB(sleep=[*old, *recent])

    schedule = await routine.learn_sleep_schedule(db, USER_ID, TZ)

    assert schedule.nights_sampled == 16  # unchanged: all history
    assert schedule.nights_in_window == 10  # the honest recent count
    assert schedule.window_days == routine.DEFAULT_WINDOW_DAYS
    assert schedule.window_end == end_day.isoformat()
    assert schedule.nights_in_window < schedule.nights_sampled


@pytest.mark.asyncio
async def test_sleep_empty_dataset_does_not_crash() -> None:
    schedule = await routine.learn_sleep_schedule(RoutingDB(), USER_ID, TZ)

    assert schedule == routine.SleepSchedule()
    assert schedule.nights_sampled == 0
    assert schedule.nights_in_window == 0
    assert schedule.window_end is None


# ---------------------------------------------------------------------------
# learn_profile — the composed path callers actually use
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_learn_profile_stale_dataset_matches_fresh_dataset() -> None:
    """End to end: a wholly stale user profile equals the fresh one.

    health_service.save_routine_profile persists exactly this dict, so this is
    the shape the rest of the product reads.
    """
    fresh_end = _last_sunday(_today())
    stale_end = fresh_end - dt.timedelta(days=STALE_OFFSET_DAYS)

    def _db(end_day: dt.date) -> RoutingDB:
        return RoutingDB(
            **_workout_dataset(end_day),
            sleep=_sleep_dataset(end_day),
            meals=_meal_dataset(end_day),
        )

    fresh = (await routine.learn_profile(_db(fresh_end), USER_ID, TZ)).to_dict()
    stale = (await routine.learn_profile(_db(stale_end), USER_ID, TZ)).to_dict()

    assert stale["workout"]["sessions_sampled"] == 12
    assert stale["eating"]["meals_sampled"] == 42
    assert stale["sleep"]["nights_sampled"] == 12
    # window_end is the one field that legitimately tracks the data's date.
    stale["sleep"].pop("window_end")
    fresh["sleep"].pop("window_end")
    assert stale == fresh


@pytest.mark.asyncio
async def test_learn_profile_empty_dataset_does_not_crash() -> None:
    profile = await routine.learn_profile(RoutingDB(), USER_ID, TZ)

    assert profile.to_dict() == routine.RoutineProfile().to_dict()
