"""Routine-learning engine.

Turns the compact ``health`` rows (sleep sessions, workouts, daily activity)
and the ``meals`` table into a *learned daily routine*: when the user usually
sleeps, wakes, trains, and eats — plus typical daily calorie pacing.

Robustness
----------
Real-world watch/eating data is noisy (the odd 3am snack, a missed night, a
travel day). Every statistic here uses **median + IQR outlier removal** (a
robust "moving average" over a recent window) so a handful of unusual days do
not distort the learned routine.

The module is intentionally free of bot/Telegram imports so it can be unit
tested and reused. Async functions take the bot's ``DB`` object (anything with
an awaitable ``fetch_all(sql, params) -> list[dict]``) and a ``tzinfo``.
"""

from __future__ import annotations

import datetime as dt
import statistics
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol, Sequence
from zoneinfo import ZoneInfo

# Default learning window. Long enough to be stable, short enough to track
# recent changes in routine.
DEFAULT_WINDOW_DAYS = 45


class SupportsFetchAll(Protocol):
    async def fetch_all(
        self, sql: str, parameters: tuple[Any, ...] = ()
    ) -> list[dict[str, Any]]: ...


# ---------------------------------------------------------------------------
# Robust statistics
# ---------------------------------------------------------------------------


def remove_outliers(values: Sequence[float]) -> list[float]:
    """Drop values outside the 1.5*IQR fence (Tukey). Keeps small samples."""
    vals = sorted(float(v) for v in values)
    n = len(vals)
    if n < 4:
        return vals
    q1 = _quantile(vals, 0.25)
    q3 = _quantile(vals, 0.75)
    iqr = q3 - q1
    lo = q1 - 1.5 * iqr
    hi = q3 + 1.5 * iqr
    filtered = [v for v in vals if lo <= v <= hi]
    return filtered or vals


def _quantile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def robust_mean(values: Sequence[float]) -> float | None:
    cleaned = remove_outliers(values)
    if not cleaned:
        return None
    return statistics.fmean(cleaned)


def robust_median(values: Sequence[float]) -> float | None:
    cleaned = remove_outliers(values)
    if not cleaned:
        return None
    return statistics.median(cleaned)


def circular_hour_mean(hours: Sequence[float]) -> float | None:
    """Mean of clock hours treating them as angles (so 23:30 & 00:30 ~ 00:00).

    Returns an hour-of-day in ``[0, 24)``. Used for wake/sleep/workout times.
    Outliers are removed in the *unwrapped* domain first.
    """
    import math

    if not hours:
        return None
    cleaned = remove_outliers(hours)
    if not cleaned:
        return None
    sin_sum = sum(math.sin(2 * math.pi * h / 24.0) for h in cleaned)
    cos_sum = sum(math.cos(2 * math.pi * h / 24.0) for h in cleaned)
    if sin_sum == 0 and cos_sum == 0:
        return None
    angle = math.atan2(sin_sum, cos_sum)
    hour = (angle / (2 * math.pi)) * 24.0
    return hour % 24.0


def hour_to_hhmm(hour: float | None) -> str | None:
    if hour is None:
        return None
    h = int(hour) % 24
    m = int(round((hour - int(hour)) * 60)) % 60
    return f"{h:02d}:{m:02d}"


# ---------------------------------------------------------------------------
# Learned profiles
# ---------------------------------------------------------------------------


@dataclass
class SleepSchedule:
    typical_bedtime: str | None = None
    typical_wake_time: str | None = None
    avg_duration_minutes: float | None = None
    nights_sampled: int = 0


@dataclass
class WorkoutPattern:
    weekly_frequency: float | None = None
    typical_hour: str | None = None
    common_weekdays: list[int] = field(default_factory=list)  # 0=Mon
    avg_duration_minutes: float | None = None
    sessions_sampled: int = 0


@dataclass
class EatingWindows:
    first_meal_time: str | None = None
    last_meal_time: str | None = None
    typical_meal_hours: list[str] = field(default_factory=list)
    avg_daily_calories: float | None = None
    meals_sampled: int = 0


@dataclass
class RoutineProfile:
    sleep: SleepSchedule = field(default_factory=SleepSchedule)
    workout: WorkoutPattern = field(default_factory=WorkoutPattern)
    eating: EatingWindows = field(default_factory=EatingWindows)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _to_local(iso: str, tz: ZoneInfo) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(iso)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(tz)


def _hour_of_day(local: dt.datetime) -> float:
    return local.hour + local.minute / 60.0


async def learn_sleep_schedule(
    db: SupportsFetchAll,
    user_id: int,
    tz: ZoneInfo,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> SleepSchedule:
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=window_days)).isoformat()
    rows = await db.fetch_all(
        """
        SELECT start_time, end_time, value
        FROM health
        WHERE user_id=? AND sample_type='sleep_session' AND start_time>=?
        ORDER BY start_time
        """,
        (user_id, since),
    )
    if not rows:
        return SleepSchedule()

    # Group asleep segments into nights and take, per night, the earliest
    # bedtime and latest wake time so fragmented sleep stages collapse.
    nights: dict[dt.date, dict[str, Any]] = {}
    for row in rows:
        start = _to_local(row["start_time"], tz)
        end = _to_local(row["end_time"], tz) if row["end_time"] else start
        # Attribute a sleep segment to the "night of" the prior evening: if it
        # starts after noon it belongs to that calendar date, else the day
        # before (early-morning sleep belongs to the previous night).
        night_key = start.date() if start.hour >= 12 else (start.date() - dt.timedelta(days=1))
        bucket = nights.setdefault(night_key, {"start": start, "end": end, "minutes": 0.0})
        bucket["start"] = min(bucket["start"], start)
        bucket["end"] = max(bucket["end"], end)
        bucket["minutes"] += float(row["value"] or 0.0)

    bedtimes: list[float] = []
    wake_times: list[float] = []
    durations: list[float] = []
    for bucket in nights.values():
        # Unwrap bedtime so late-night hours sort near 24-26 not 0-2.
        bh = _hour_of_day(bucket["start"])
        bedtimes.append(bh if bh >= 12 else bh + 24)
        wake_times.append(_hour_of_day(bucket["end"]))
        durations.append(bucket["minutes"])

    bedtime_hour = robust_mean(bedtimes)
    _mean_duration = robust_mean(durations)
    return SleepSchedule(
        typical_bedtime=hour_to_hhmm(None if bedtime_hour is None else bedtime_hour % 24),
        typical_wake_time=hour_to_hhmm(circular_hour_mean(wake_times)),
        avg_duration_minutes=(round(_mean_duration, 1) if _mean_duration is not None else None),
        nights_sampled=len(nights),
    )


async def learn_workout_pattern(
    db: SupportsFetchAll,
    user_id: int,
    tz: ZoneInfo,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> WorkoutPattern:
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=window_days)).isoformat()
    rows = await db.fetch_all(
        """
        SELECT start_time, value
        FROM health
        WHERE user_id=? AND sample_type='workout' AND start_time>=?
        ORDER BY start_time
        """,
        (user_id, since),
    )
    if not rows:
        return WorkoutPattern()

    hours: list[float] = []
    durations: list[float] = []
    weekday_counts: dict[int, int] = {}
    for row in rows:
        local = _to_local(row["start_time"], tz)
        hours.append(_hour_of_day(local))
        durations.append(float(row["value"] or 0.0))
        weekday_counts[local.weekday()] = weekday_counts.get(local.weekday(), 0) + 1

    common = sorted(weekday_counts, key=lambda d: weekday_counts[d], reverse=True)
    weeks = max(1.0, window_days / 7.0)
    _mean_duration = robust_mean(durations)
    return WorkoutPattern(
        weekly_frequency=round(len(rows) / weeks, 1),
        typical_hour=hour_to_hhmm(circular_hour_mean(hours)),
        common_weekdays=[d for d in common if weekday_counts[d] >= 2][:4] or common[:2],
        avg_duration_minutes=(round(_mean_duration, 1) if _mean_duration is not None else None),
        sessions_sampled=len(rows),
    )


async def learn_eating_windows(
    db: SupportsFetchAll,
    user_id: int,
    tz: ZoneInfo,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> EatingWindows:
    """Learn eating times from logged meals.

    Assumption (matches the bot): the photo timestamp == the eating moment, so
    ``meals.eaten_at`` is the eating time.
    """
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=window_days)).isoformat()
    rows = await db.fetch_all(
        """
        SELECT eaten_at, calories
        FROM meals
        WHERE user_id=? AND eaten_at>=?
        ORDER BY eaten_at
        """,
        (user_id, since),
    )
    if not rows:
        return EatingWindows()

    hours: list[float] = []
    per_day_first: dict[dt.date, float] = {}
    per_day_last: dict[dt.date, float] = {}
    per_day_calories: dict[dt.date, float] = {}
    for row in rows:
        local = _to_local(row["eaten_at"], tz)
        h = _hour_of_day(local)
        hours.append(h)
        day = local.date()
        per_day_first[day] = min(per_day_first.get(day, h), h)
        per_day_last[day] = max(per_day_last.get(day, h), h)
        per_day_calories[day] = per_day_calories.get(day, 0.0) + float(row["calories"] or 0.0)

    # Cluster meal hours into typical slots by rounding to the nearest hour and
    # keeping hours that recur on multiple days.
    hour_buckets: dict[int, int] = {}
    for h in hours:
        _hk = int(round(h)) % 24
        hour_buckets[_hk] = hour_buckets.get(_hk, 0) + 1
    typical = sorted(
        (hr for hr, c in hour_buckets.items() if c >= 2),
        key=lambda hr: hour_buckets[hr],
        reverse=True,
    )[:5]
    typical_sorted = sorted(typical)

    return EatingWindows(
        first_meal_time=hour_to_hhmm(circular_hour_mean(list(per_day_first.values()))),
        last_meal_time=hour_to_hhmm(circular_hour_mean(list(per_day_last.values()))),
        typical_meal_hours=[hour_to_hhmm(float(hr)) for hr in typical_sorted],
        avg_daily_calories=(
            round(robust_mean(list(per_day_calories.values())), 0) if per_day_calories else None
        ),
        meals_sampled=len(rows),
    )


async def learn_profile(
    db: SupportsFetchAll,
    user_id: int,
    tz: ZoneInfo,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> RoutineProfile:
    return RoutineProfile(
        sleep=await learn_sleep_schedule(db, user_id, tz, window_days),
        workout=await learn_workout_pattern(db, user_id, tz, window_days),
        eating=await learn_eating_windows(db, user_id, tz, window_days),
    )
