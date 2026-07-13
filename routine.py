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
import json
import statistics
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol, Sequence
from zoneinfo import ZoneInfo

from noam_coach.services.weekdays import WEEKDAY_SCHEMA_VERSION

# Default learning window. Long enough to be stable, short enough to track
# recent changes in routine.
DEFAULT_WINDOW_DAYS = 45

# Fixed recent window used to detect a shift away from the long-run average
# (e.g. "overall 2.5/week, but the last 14 days show 2/week"). Fixed rather
# than exponentially-weighted so the comparison is easy to explain to users.
RECENT_WINDOW_DAYS = 14

# Watch-wear coverage thresholds (local clock hours). A day counts as
# "covered until the evening" only when the watch produced samples from the
# morning (first sample by WEAR_MORNING_HOUR) through the evening (last sample
# at or after WEAR_EVENING_HOUR) — otherwise the day's step count is partial
# and the day is burned for daily-average purposes.
WEAR_MORNING_HOUR = 12.0
WEAR_EVENING_HOUR = 19.0


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


def weighted_mean(values: Sequence[float], weights: Sequence[float]) -> float | None:
    if not values or not weights or len(values) != len(weights):
        return None
    pairs = [
        (float(value), float(weight))
        for value, weight in zip(values, weights, strict=False)
        if weight > 0
    ]
    if not pairs:
        return None
    total_weight = sum(weight for _value, weight in pairs)
    if total_weight <= 0:
        return None
    return sum(value * weight for value, weight in pairs) / total_weight


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


def weighted_circular_hour_mean(
    hours: Sequence[float], weights: Sequence[float]
) -> float | None:
    import math

    if not hours or not weights or len(hours) != len(weights):
        return None
    pairs = [
        (float(hour), float(weight))
        for hour, weight in zip(hours, weights, strict=False)
        if weight > 0
    ]
    if not pairs:
        return None
    sin_sum = sum(weight * math.sin(2 * math.pi * hour / 24.0) for hour, weight in pairs)
    cos_sum = sum(weight * math.cos(2 * math.pi * hour / 24.0) for hour, weight in pairs)
    if sin_sum == 0 and cos_sum == 0:
        return None
    angle = math.atan2(sin_sum, cos_sum)
    return ((angle / (2 * math.pi)) * 24.0) % 24.0


def weighted_std_minutes(values: Sequence[float], weights: Sequence[float]) -> float | None:
    mean = weighted_mean(values, weights)
    if mean is None:
        return None
    pairs = [
        (float(value), float(weight))
        for value, weight in zip(values, weights, strict=False)
        if weight > 0
    ]
    if len(pairs) < 2:
        return None
    total_weight = sum(weight for _value, weight in pairs)
    if total_weight <= 0:
        return None
    variance = sum(weight * ((value - mean) ** 2) for value, weight in pairs) / total_weight
    return variance ** 0.5


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
    variability_minutes: float | None = None
    nights_sampled: int = 0


@dataclass
class WorkoutPattern:
    weekly_frequency: float | None = None
    typical_hour: str | None = None
    common_weekdays: list[int] = field(default_factory=list)  # 0=Mon
    avg_duration_minutes: float | None = None
    sessions_sampled: int = 0
    weekday_schema: str = WEEKDAY_SCHEMA_VERSION
    # Recent-window frequency, for detecting a shift away from the long-run
    # average (see RECENT_WINDOW_DAYS). None when there isn't enough recent
    # data sampled to compute it separately.
    recent_weekly_frequency: float | None = None
    recent_window_days: int = RECENT_WINDOW_DAYS
    recent_sessions_sampled: int = 0
    # True when weekly_frequency was computed only over complete weeks in
    # which the watch was worn every day (weeks with an unworn day are
    # burned — their data is unknown, not zero). valid_weeks_sampled is how
    # many such weeks the average is based on.
    wear_filtered: bool = False
    valid_weeks_sampled: int = 0


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


# ---------------------------------------------------------------------------
# Watch-wear awareness
#
# "No data" is not "no activity": a day without watch samples is unknown, and
# any weekly statistic that treats it as a zero silently drags the average
# down. Days the watch was not worn are therefore *burned* — and a week that
# contains a burned day is burned for weekly-frequency purposes.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DayWear:
    """Wear coverage for one local day.

    ``first_hour``/``last_hour`` are local clock hours of the first/last watch
    sample. They are ``None`` when wear was inferred from legacy data (rows
    imported before watch_wear tracking existed) — the day is known to be
    worn, but the intra-day coverage is unknown.
    """

    first_hour: float | None = None
    last_hour: float | None = None

    @property
    def covers_until_evening(self) -> bool:
        """Worn through the day, at least into the evening."""
        if self.last_hour is None or self.first_hour is None:
            return True  # legacy inference — coverage unknown, don't burn
        return (
            self.first_hour <= WEAR_MORNING_HOUR
            and self.last_hour >= WEAR_EVENING_HOUR
        )


_DATASET_SAMPLE_TYPES = (
    "watch_wear",
    "resting_heart_rate",
    "sleep_session",
    "workout",
    "steps",
    "active_energy",
)


async def newest_health_sample_date(
    db: SupportsFetchAll,
    user_id: int,
    tz: ZoneInfo,
) -> dt.date | None:
    """Local date of the newest relevant health sample — the dataset end.

    HealthKit exports are often stale (the ZIP ends days/weeks before today).
    Anchoring the analysis window to this date instead of "today" lets the
    bot still learn from the last period the file actually covers, rather than
    reporting "not enough recent data" just because the export is old.
    Returns None when the user has no relevant samples at all.
    """
    placeholders = ",".join("?" for _ in _DATASET_SAMPLE_TYPES)
    rows = await db.fetch_all(
        f"""
        SELECT MAX(start_time) AS newest
        FROM health
        WHERE user_id=? AND sample_type IN ({placeholders})
        """,
        (user_id, *_DATASET_SAMPLE_TYPES),
    )
    newest_raw = rows[0].get("newest") if rows else None
    if not newest_raw:
        return None
    try:
        return _to_local(str(newest_raw), tz).date()
    except ValueError:
        return None


async def load_wear_days(
    db: SupportsFetchAll,
    user_id: int,
    tz: ZoneInfo,
    window_days: int = DEFAULT_WINDOW_DAYS,
    *,
    anchor: dt.date | None = None,
) -> dict[dt.date, DayWear] | None:
    """Map each local day in the window to its watch-wear coverage.

    Days absent from the returned dict were NOT worn. Returns ``None`` when
    there is no wear evidence at all in the window (e.g. no watch, or an
    import from before wear tracking) — callers must then skip wear filtering
    rather than burn everything.

    Precise ``watch_wear`` rows (one per worn day, written by the importer)
    win. For data imported before those rows existed, wear is inferred from
    watch-only signals: resting heart rate, sleep sessions, workouts, and
    steps rows whose source is the watch.

    ``anchor`` is the end of the window (defaults to today). Pass the dataset
    end date to analyze a stale export's last available period instead of an
    empty "recent" window.
    """
    window_end = anchor or dt.datetime.now(tz).date()
    since = (
        dt.datetime.combine(window_end - dt.timedelta(days=window_days), dt.time.min, tzinfo=tz)
        .astimezone(dt.timezone.utc)
        .isoformat()
    )
    until = (
        dt.datetime.combine(window_end + dt.timedelta(days=1), dt.time.min, tzinfo=tz)
        .astimezone(dt.timezone.utc)
        .isoformat()
    )
    rows = await db.fetch_all(
        """
        SELECT sample_type, start_time, end_time, source_device
        FROM health
        WHERE user_id=? AND start_time>=? AND start_time<?
          AND sample_type IN ('watch_wear','resting_heart_rate','sleep_session','workout','steps')
        ORDER BY start_time
        """,
        (user_id, since, until),
    )
    if not rows:
        return None

    precise: dict[dt.date, DayWear] = {}
    inferred: set[dt.date] = set()
    for row in rows:
        stype = row.get("sample_type")
        start_raw = row.get("start_time")
        if not start_raw:
            continue
        try:
            start_local = _to_local(str(start_raw), tz)
        except ValueError:
            continue
        end_raw = row.get("end_time")
        end_local = None
        if end_raw:
            try:
                end_local = _to_local(str(end_raw), tz)
            except ValueError:
                end_local = None

        if stype == "watch_wear":
            last = end_local or start_local
            precise[start_local.date()] = DayWear(
                first_hour=_hour_of_day(start_local),
                last_hour=_hour_of_day(last) if last.date() == start_local.date() else 24.0,
            )
        elif stype == "steps":
            source = str(row.get("source_device") or "")
            if "watch" in source.lower():
                inferred.add(start_local.date())
        else:  # resting_heart_rate / sleep_session / workout — watch signals
            inferred.add(start_local.date())
            if end_local is not None:
                inferred.add(end_local.date())

    if precise:
        return precise
    if inferred:
        return {day: DayWear() for day in inferred}
    return None


def _complete_weeks(
    window_start: dt.date, today: dt.date, *, inclusive_end: bool = False
) -> list[tuple[dt.date, dt.date]]:
    """Monday-first calendar weeks fully inside [window_start, yesterday].

    ``inclusive_end=False`` (default) treats ``today`` as an in-progress day
    with no complete data yet — a week ending exactly on ``today`` is NOT
    counted as complete. Pass ``inclusive_end=True`` when ``today`` is
    actually an explicit anchor (e.g. a HealthKit export's dataset-end date)
    rather than the real current day: that date already has a full day of
    data, so a week ending exactly on it IS complete. Without this, a dataset
    end date that happens to fall on a Sunday silently drops the most recent
    complete week (health_quality.build_health_quality_report could
    under-report policy/confidence purely because of which weekday the export
    happened to end on).
    """
    first_monday = window_start + dt.timedelta(days=(7 - window_start.weekday()) % 7)
    weeks: list[tuple[dt.date, dt.date]] = []
    week_start = first_monday
    while True:
        week_end = week_start + dt.timedelta(days=6)
        if inclusive_end:
            if week_end > today:
                break
        elif week_end >= today:
            break
        weeks.append((week_start, week_end))
        week_start += dt.timedelta(days=7)
    return weeks


def _fully_worn(
    week: tuple[dt.date, dt.date], wear_days: dict[dt.date, DayWear]
) -> bool:
    start, _end = week
    return all(start + dt.timedelta(days=offset) in wear_days for offset in range(7))


# Steps use their own window: a 28-day baseline is the product default, wider
# than the wear-policy window because StepCount is reliable even without watch
# wear (it also comes from the iPhone).
STEPS_WINDOW_DAYS = 28


@dataclass(frozen=True)
class StepsAverage:
    """Daily-steps average.

    ``avg`` is the planning baseline: the mean of EVERY day that has a
    StepCount sample, because steps come from the iPhone too and a low-wear
    day must not be deleted (only its confidence is lower). ``avg_high_conf``
    is the mean restricted to full-wear days, shown alongside for transparency
    when the two disagree. ``days_sampled`` counts all step days; ``days_excluded``
    counts step days that lacked full wear (used for the confidence note, NOT
    removed from ``avg``).
    """

    avg: float | None  # baseline: all step days
    days_sampled: int  # all days with a StepCount sample
    days_excluded: int  # step days without full wear (confidence only)
    wear_filtered: bool  # True when some step days lacked full wear
    avg_high_conf: float | None = None  # mean over full-wear days only
    high_conf_days: int = 0
    window_start: dt.date | None = None
    window_end: dt.date | None = None
    raw_all_sources_avg: float | None = None
    dominant_or_priority_source_avg: float | None = None
    selected_planning_baseline: float | None = None
    sources_found: list[str] = field(default_factory=list)
    daily_breakdown: list[dict[str, Any]] = field(default_factory=list)


async def _last_complete_step_day(
    db: SupportsFetchAll, user_id: int, tz: ZoneInfo, *, window_days: int
) -> dt.date | None:
    """The last day inside the window that is a COMPLETE day of data.

    An export that ends mid-day (e.g. 2026-06-16 15:09) has a partial final
    day that would undercount steps, so it is excluded: the newest sample of
    any relevant type must fall on a later date than the candidate end day for
    that day to count as complete. Returns None when there is no data.
    """
    anchor = await newest_health_sample_date(db, user_id, tz)
    if anchor is None:
        return None
    # Decide whether the anchor day is COMPLETE or a mid-day export end. Use the
    # latest coverage timestamp on that day (end_time when present, else
    # start_time) across all relevant samples — a wear row that runs to 22:00 or
    # a sample after the evening cutoff means the day is complete; only a whole
    # day whose latest coverage is before the evening is treated as partial.
    latest_rows = await db.fetch_all(
        f"""
        SELECT MAX(COALESCE(end_time, start_time)) AS latest
        FROM health
        WHERE user_id=? AND sample_type IN ({",".join("?" for _ in _DATASET_SAMPLE_TYPES)})
        """,
        (user_id, *_DATASET_SAMPLE_TYPES),
    )
    latest_raw = latest_rows[0].get("latest") if latest_rows else None
    if latest_raw:
        try:
            latest_local = _to_local(str(latest_raw), tz)
            if latest_local.date() == anchor and latest_local.hour < WEAR_EVENING_HOUR:
                return anchor - dt.timedelta(days=1)
        except ValueError:
            pass
    return anchor


def _step_source_payload(row: dict[str, Any]) -> tuple[dict[str, float], float, str, str]:
    """Return (source totals, raw all-source steps, selected source, reason).

    New imports store JSON diagnostics in ``source_device``. Legacy rows only
    contain the already-selected daily value, so they are treated as a single
    unknown source.
    """
    selected = float(row.get("value") or 0.0)
    source_device = row.get("source_device")
    if isinstance(source_device, str) and source_device.strip().startswith("{"):
        try:
            payload = json.loads(source_device)
        except json.JSONDecodeError:
            payload = {}
        totals_raw = payload.get("source_totals") if isinstance(payload, dict) else None
        if isinstance(totals_raw, dict) and totals_raw:
            totals = {
                str(source): float(value or 0.0)
                for source, value in totals_raw.items()
            }
            raw = float(payload.get("raw_all_sources") or sum(totals.values()))
            selected_source = str(payload.get("selected_source") or max(totals, key=totals.get))
            reason = str(payload.get("selection_reason") or "source_diagnostics")
            return totals, raw, selected_source, reason
    source = str(source_device or "unknown")
    return {source: selected}, selected, source, "legacy_selected_daily_steps"


async def average_daily_steps(
    db: SupportsFetchAll,
    user_id: int,
    tz: ZoneInfo,
    window_days: int = STEPS_WINDOW_DAYS,
    *,
    anchor: dt.date | None = None,
) -> StepsAverage:
    """Average daily steps over the last ``window_days`` complete days.

    StepCount is NOT dropped for low watch wear — steps also come from the
    iPhone, so every day with a StepCount sample counts toward the baseline
    ``avg``. Wear coverage only affects confidence (``avg_high_conf`` /
    ``days_excluded``), never removes a day from the baseline. The window ends
    at the last COMPLETE day in the file (a partial export end is excluded),
    so a stale export is still analyzed over its own final weeks.
    """
    window_end = anchor or await _last_complete_step_day(db, user_id, tz, window_days=window_days)
    if window_end is None:
        return StepsAverage(avg=None, days_sampled=0, days_excluded=0, wear_filtered=False)

    since = (
        dt.datetime.combine(window_end - dt.timedelta(days=window_days - 1), dt.time.min, tzinfo=tz)
        .astimezone(dt.timezone.utc)
        .isoformat()
    )
    until = (
        dt.datetime.combine(window_end + dt.timedelta(days=1), dt.time.min, tzinfo=tz)
        .astimezone(dt.timezone.utc)
        .isoformat()
    )
    rows = await db.fetch_all(
        """
        SELECT value, start_time, source_device
        FROM health
        WHERE user_id=? AND sample_type='steps' AND start_time>=? AND start_time<?
        ORDER BY start_time
        """,
        (user_id, since, until),
    )
    window_start = window_end - dt.timedelta(days=window_days - 1)
    per_day: dict[dt.date, dict[str, Any]] = {}
    for row in rows:
        start_raw = row.get("start_time")
        if not start_raw:
            continue
        try:
            day = _to_local(str(start_raw), tz).date()
        except ValueError:
            continue
        # Guard the window explicitly (not every backend applies the SQL date
        # filter): exclude anything after the last complete day or before the
        # 28-day window start.
        if day < window_start or day > window_end:
            continue
        source_totals, raw_all, selected_source, reason = _step_source_payload(row)
        selected = max(source_totals.values()) if source_totals else float(row.get("value") or 0.0)
        existing = per_day.get(day)
        if existing is None or selected > float(existing["selected_step_count_for_baseline"]):
            per_day[day] = {
                "total_step_count_all_sources": raw_all,
                "total_by_source": source_totals,
                "dominant_source": selected_source,
                "selected_step_count_for_baseline": selected,
                "selection_reason": reason,
            }

    wear_days = await load_wear_days(db, user_id, tz, window_days, anchor=window_end)
    breakdown: list[dict[str, Any]] = []
    selected_values: list[float] = []
    raw_values: list[float] = []
    sources: set[str] = set()
    for offset in range(window_days):
        day = window_start + dt.timedelta(days=offset)
        item = per_day.get(day)
        wear = wear_days.get(day) if wear_days is not None else None
        has_full_wear = bool(wear is not None and wear.covers_until_evening)
        if item is None:
            breakdown.append({
                "date": day.isoformat(),
                "total_step_count_all_sources": 0,
                "total_by_source": {},
                "source_intervals_or_coverage": "daily_aggregate",
                "dominant_source": None,
                "selected_step_count_for_baseline": None,
                "has_watch_wear": wear is not None,
                "confidence": "high" if has_full_wear else "low",
                "included": False,
                "excluded": True,
                "reason": "no_stepcount_record_for_day",
                "selection_reason": "missing",
            })
            continue
        selected = float(item["selected_step_count_for_baseline"])
        raw = float(item["total_step_count_all_sources"])
        selected_values.append(selected)
        raw_values.append(raw)
        sources.update(str(source) for source in item["total_by_source"])
        confidence = "high" if has_full_wear else "medium"
        breakdown.append({
            "date": day.isoformat(),
            "total_step_count_all_sources": round(raw, 1),
            "total_by_source": {
                source: round(float(value), 1)
                for source, value in item["total_by_source"].items()
            },
            "source_intervals_or_coverage": "daily_aggregate_with_source_totals",
            "dominant_source": item["dominant_source"],
            "selected_step_count_for_baseline": round(selected, 1),
            "has_watch_wear": wear is not None,
            "confidence": confidence,
            "included": True,
            "excluded": False,
            "reason": "",
            "selection_reason": item["selection_reason"],
        })

    if not selected_values:
        return StepsAverage(
            avg=None,
            days_sampled=0,
            days_excluded=0,
            wear_filtered=False,
            window_start=window_start,
            window_end=window_end,
            daily_breakdown=breakdown,
        )

    # Baseline: EVERY step day. Steps are not deleted for missing wear.
    baseline_avg = statistics.fmean(selected_values)
    raw_avg = statistics.fmean(raw_values)

    high_conf = {
        day: float(item["selected_step_count_for_baseline"])
        for day, item in per_day.items()
        if wear_days is not None
        and (wear := wear_days.get(day)) is not None
        and wear.covers_until_evening
    }
    avg_high_conf = statistics.fmean(high_conf.values()) if high_conf else None
    days_excluded = len(selected_values) - len(high_conf) if wear_days is not None else 0

    return StepsAverage(
        avg=baseline_avg,
        days_sampled=len(selected_values),
        days_excluded=days_excluded,
        wear_filtered=wear_days is not None and days_excluded > 0,
        avg_high_conf=avg_high_conf,
        high_conf_days=len(high_conf),
        window_start=window_start,
        window_end=window_end,
        raw_all_sources_avg=raw_avg,
        dominant_or_priority_source_avg=baseline_avg,
        selected_planning_baseline=baseline_avg,
        sources_found=sorted(sources),
        daily_breakdown=breakdown,
    )


# ---------------------------------------------------------------------------
# Training-week policy analysis (RE13)
#
# The strict rule (a week counts only when the watch was worn all 7 days) is
# exact but can leave almost nothing for a real user who skips the watch one
# day a week. The relaxed policy accepts weeks with at least
# RELAXED_MIN_WORN_DAYS worn days and NORMALIZES the count (workouts seen on
# worn days scaled to a 7-day week). Workouts landing on unworn days are
# never silently counted — they are surfaced separately.
# ---------------------------------------------------------------------------

POLICY_STRICT = "strict_7_of_7"
POLICY_RELAXED = "relaxed_5_of_7_normalized"
POLICY_INSUFFICIENT = "insufficient"

# A relaxed-valid week needs at least this many worn days out of 7.
RELAXED_MIN_WORN_DAYS = 5
# A policy is trustworthy enough to auto-apply only with this many weeks.
MIN_POLICY_WEEKS = 3


@dataclass(frozen=True)
class WeekWearStats:
    start: dt.date
    end: dt.date
    worn_days: int
    workouts_on_worn_days: int
    workouts_total: int

    @property
    def strict_valid(self) -> bool:
        return self.worn_days == 7

    @property
    def relaxed_valid(self) -> bool:
        return self.worn_days >= RELAXED_MIN_WORN_DAYS

    @property
    def normalized_workouts(self) -> float:
        """Workouts on worn days scaled to a full week (2 in 5 days → 2.8)."""
        if self.worn_days <= 0:
            return 0.0
        return self.workouts_on_worn_days * 7.0 / self.worn_days


@dataclass(frozen=True)
class TrainingWeekAnalysis:
    weeks: list[WeekWearStats]
    valid_weeks_strict: int
    valid_weeks_relaxed: int
    burned_weeks: int  # weeks unusable even under the relaxed policy
    frequency_raw: float | None  # strict average (7/7 weeks only)
    frequency_normalized: float | None  # relaxed normalized average
    workouts_on_unworn_days: int
    policy: str  # POLICY_STRICT / POLICY_RELAXED / POLICY_INSUFFICIENT


async def analyze_training_weeks(
    db: SupportsFetchAll,
    user_id: int,
    tz: ZoneInfo,
    window_days: int = DEFAULT_WINDOW_DAYS,
    *,
    anchor: dt.date | None = None,
) -> TrainingWeekAnalysis | None:
    """Per-week wear/workout stats + the policy the product should use.

    Policy choice: prefer strict when at least MIN_POLICY_WEEKS full weeks
    exist; otherwise fall back to relaxed when it has enough weeks; otherwise
    the data is insufficient and the user should be asked directly.
    Returns None when there is no wear evidence at all (legacy import) —
    callers must then keep the pre-RE13 behavior.

    ``anchor`` ends the analysis window (defaults to today). Passing the
    dataset end date lets a stale export still be analyzed over its own last
    weeks instead of an empty window relative to today.
    """
    window_end = anchor or dt.datetime.now(tz).date()
    wear_days = await load_wear_days(db, user_id, tz, window_days, anchor=window_end)
    if wear_days is None:
        return None

    since = (
        dt.datetime.combine(window_end - dt.timedelta(days=window_days), dt.time.min, tzinfo=tz)
        .astimezone(dt.timezone.utc)
        .isoformat()
    )
    until = (
        dt.datetime.combine(window_end + dt.timedelta(days=1), dt.time.min, tzinfo=tz)
        .astimezone(dt.timezone.utc)
        .isoformat()
    )
    rows = await db.fetch_all(
        """
        SELECT start_time, value
        FROM health
        WHERE user_id=? AND sample_type='workout' AND start_time>=? AND start_time<?
        ORDER BY start_time
        """,
        (user_id, since, until),
    )
    workout_days: list[dt.date] = []
    for row in rows:
        start_raw = row.get("start_time")
        if not start_raw:
            continue
        try:
            workout_days.append(_to_local(str(start_raw), tz).date())
        except ValueError:
            continue

    today_local = window_end
    window_start = today_local - dt.timedelta(days=window_days)
    weeks: list[WeekWearStats] = []
    # An explicit anchor is a dataset end date (a fully-elapsed day already
    # covered by data), unlike the real "today" fallback, which is still in
    # progress — see `_complete_weeks`'s `inclusive_end`.
    for start, end in _complete_weeks(window_start, today_local, inclusive_end=anchor is not None):
        days = [start + dt.timedelta(days=offset) for offset in range(7)]
        worn = [day for day in days if day in wear_days]
        in_week = [day for day in workout_days if start <= day <= end]
        on_worn = sum(1 for day in in_week if day in wear_days)
        weeks.append(
            WeekWearStats(
                start=start,
                end=end,
                worn_days=len(worn),
                workouts_on_worn_days=on_worn,
                workouts_total=len(in_week),
            )
        )

    strict = [w for w in weeks if w.strict_valid]
    relaxed = [w for w in weeks if w.relaxed_valid]
    frequency_raw = (
        round(statistics.fmean(w.workouts_total for w in strict), 1) if strict else None
    )
    frequency_normalized = (
        round(statistics.fmean(w.normalized_workouts for w in relaxed), 1)
        if relaxed
        else None
    )
    if len(strict) >= MIN_POLICY_WEEKS:
        policy = POLICY_STRICT
    elif len(relaxed) >= MIN_POLICY_WEEKS:
        policy = POLICY_RELAXED
    else:
        policy = POLICY_INSUFFICIENT

    return TrainingWeekAnalysis(
        weeks=weeks,
        valid_weeks_strict=len(strict),
        valid_weeks_relaxed=len(relaxed),
        burned_weeks=len(weeks) - len(relaxed),
        frequency_raw=frequency_raw,
        frequency_normalized=frequency_normalized,
        workouts_on_unworn_days=sum(
            w.workouts_total - w.workouts_on_worn_days for w in weeks
        ),
        policy=policy,
    )


# ---------------------------------------------------------------------------
# Trend proposals — compare the long-run average against the recent window
# and, when they meaningfully disagree, recommend based on the recent trend
# rather than silently defaulting to whichever number was computed last.
# ---------------------------------------------------------------------------

# A recent vs. overall gap below this (in the metric's own units) is treated
# as noise, not a real behavior shift.
FREQUENCY_TREND_THRESHOLD = 0.5


@dataclass
class TrendProposal:
    metric: str
    overall_value: float
    recent_value: float
    recommended_value: float
    message: str
    # Button choices offered alongside free-text entry: (label, value).
    choices: list[tuple[str, float]] = field(default_factory=list)


def build_frequency_trend_proposal(
    pattern: WorkoutPattern,
    *,
    min_sessions_sampled: int = 3,
) -> TrendProposal | None:
    """Compare overall vs. recent weekly training frequency.

    Returns None when there isn't enough data to trust the comparison, or the
    recent window agrees with the overall average (nothing to surface).
    Otherwise returns a proposal anchored on the *recent* trend: the
    recommendation is the recent frequency rounded up by one training day,
    capped at a realistic increment, matching the product intent of nudging
    the plan toward what the user is actually doing lately rather than a
    stale long-run average.
    """
    overall = pattern.weekly_frequency
    recent = pattern.recent_weekly_frequency
    if overall is None or recent is None:
        return None
    if pattern.sessions_sampled < min_sessions_sampled or pattern.recent_sessions_sampled < min_sessions_sampled:
        return None
    if abs(overall - recent) < FREQUENCY_TREND_THRESHOLD:
        return None

    recent_rounded = max(1, round(recent))
    recommended_low = min(6, recent_rounded + 1)
    recommended_high = min(6, recent_rounded + 2)

    message = (
        f"זוהתה שגרת אימונים לאחרונה של כ-{recent_rounded} אימונים בשבוע "
        f"(ממוצע כללי: {overall:g}), ואמליץ על {recommended_low}-{recommended_high} "
        "אימונים בשבוע.\n"
        "האם לאשר גם עבור תוכנית האימונים? "
        "אם לא — בחר אפשרות או ציין כמות אימונים רצויה."
    )
    choices = [
        (f"המשך עם {recent_rounded}", float(recent_rounded)),
        (f"עבור ל-{recommended_low}", float(recommended_low)),
    ]
    if recommended_high != recommended_low:
        choices.append((f"עבור ל-{recommended_high}", float(recommended_high)))
    return TrendProposal(
        metric="training_frequency",
        overall_value=overall,
        recent_value=recent,
        recommended_value=float(recommended_low),
        message=message,
        choices=choices,
    )


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
    rows = await db.fetch_all(
        """
        SELECT start_time, end_time, value, source_device
        FROM health
        WHERE user_id=? AND sample_type='sleep_session'
        ORDER BY start_time
        """,
        (user_id,),
    )
    if not rows:
        return SleepSchedule()

    # Sleep validity is based on the sleep records themselves, not on watch-wear
    # coverage. HealthKit sleep may come from Apple Health, iPhone sleep
    # schedules, Apple Watch, manual entries, or third-party sources.
    #
    # Group asleep intervals into nights and union overlaps so duplicate source
    # intervals cannot count the same sleep period multiple times.
    nights: dict[dt.date, list[tuple[dt.datetime, dt.datetime]]] = {}
    for row in rows:
        start = _to_local(row["start_time"], tz)
        end = _to_local(row["end_time"], tz) if row.get("end_time") else start
        if end <= start:
            continue
        minutes = (end - start).total_seconds() / 60.0
        if minutes < 30 or minutes > 16 * 60:
            continue
        # Attribute a sleep segment to the "night of" the prior evening: if it
        # starts after noon it belongs to that calendar date, else the day
        # before (early-morning sleep belongs to the previous night).
        night_key = start.date() if start.hour >= 12 else (start.date() - dt.timedelta(days=1))
        nights.setdefault(night_key, []).append((start, end))

    bedtimes: list[float] = []
    wake_times: list[float] = []
    durations: list[float] = []
    night_dates: list[dt.date] = []
    for night_date, intervals in nights.items():
        if not intervals:
            continue
        intervals = sorted(intervals, key=lambda item: item[0])
        merged: list[tuple[dt.datetime, dt.datetime]] = []
        for start, end in intervals:
            if not merged or start > merged[-1][1]:
                merged.append((start, end))
            else:
                prev_start, prev_end = merged[-1]
                merged[-1] = (prev_start, max(prev_end, end))
        start = merged[0][0]
        end = merged[-1][1]
        total_minutes = sum((e - s).total_seconds() / 60.0 for s, e in merged)
        if total_minutes < 120 or total_minutes > 16 * 60:
            continue
        # Unwrap bedtime so late-night hours sort near 24-26 not 0-2.
        bh = _hour_of_day(start)
        bedtimes.append(bh if bh >= 12 else bh + 24)
        wake_times.append(_hour_of_day(end))
        durations.append(total_minutes)
        night_dates.append(night_date)

    if not night_dates:
        return SleepSchedule()

    newest_night = max(night_dates)
    half_life_days = max(float(window_days), 1.0)
    weights = [
        0.5 ** (max((newest_night - night_date).days, 0) / half_life_days)
        for night_date in night_dates
    ]

    bedtime_hour = weighted_mean(bedtimes, weights)
    _mean_duration = weighted_mean(durations, weights)
    variability = weighted_std_minutes(durations, weights)
    return SleepSchedule(
        typical_bedtime=hour_to_hhmm(None if bedtime_hour is None else bedtime_hour % 24),
        typical_wake_time=hour_to_hhmm(weighted_circular_hour_mean(wake_times, weights)),
        avg_duration_minutes=(round(_mean_duration, 1) if _mean_duration is not None else None),
        variability_minutes=(round(variability, 1) if variability is not None else None),
        nights_sampled=len(night_dates),
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
    workout_days: list[dt.date] = []
    for row in rows:
        local = _to_local(row["start_time"], tz)
        hours.append(_hour_of_day(local))
        durations.append(float(row["value"] or 0.0))
        workout_days.append(local.date())

    _mean_duration = robust_mean(durations)
    recent_cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=RECENT_WINDOW_DAYS)
    recent_rows = [row for row in rows if _to_local(row["start_time"], tz) >= recent_cutoff]

    # --- Wear-aware weekly frequency -----------------------------------
    # Only complete Mon-Sun weeks where the watch was worn on all 7 days are
    # trusted; a week containing an unworn day is burned (its true workout
    # count is unknown). When no wear evidence exists, or no week survives,
    # fall back to the naive window average so legacy data keeps working.
    today_local = dt.datetime.now(tz).date()
    window_start = today_local - dt.timedelta(days=window_days)
    wear_days = await load_wear_days(db, user_id, tz, window_days)
    valid_weeks: list[tuple[dt.date, dt.date]] = []
    if wear_days is not None:
        valid_weeks = [
            week
            for week in _complete_weeks(window_start, today_local)
            if _fully_worn(week, wear_days)
        ]

    counted_days = workout_days
    if valid_weeks:
        per_week = [
            sum(1 for day in workout_days if start <= day <= end)
            for start, end in valid_weeks
        ]
        weekly_frequency = round(sum(per_week) / len(valid_weeks), 1)
        counted_days = [
            day
            for day in workout_days
            if any(start <= day <= end for start, end in valid_weeks)
        ] or workout_days

        recent_start = today_local - dt.timedelta(days=RECENT_WINDOW_DAYS)
        recent_valid = [
            (start, end) for start, end in valid_weeks if end >= recent_start
        ]
        if recent_valid:
            recent_count = sum(
                1
                for day in workout_days
                if any(start <= day <= end for start, end in recent_valid)
            )
            recent_weekly_frequency = round(recent_count / len(recent_valid), 1)
            recent_sessions_sampled = recent_count
        else:
            recent_weekly_frequency = None
            recent_sessions_sampled = 0
    else:
        weeks = max(1.0, window_days / 7.0)
        weekly_frequency = round(len(rows) / weeks, 1)
        recent_weeks = max(1.0, RECENT_WINDOW_DAYS / 7.0)
        recent_weekly_frequency = (
            round(len(recent_rows) / recent_weeks, 1) if recent_rows else None
        )
        recent_sessions_sampled = len(recent_rows)

    weekday_counts: dict[int, int] = {}
    for day in counted_days:
        weekday_counts[day.weekday()] = weekday_counts.get(day.weekday(), 0) + 1
    common = sorted(weekday_counts, key=lambda d: weekday_counts[d], reverse=True)

    return WorkoutPattern(
        weekly_frequency=weekly_frequency,
        typical_hour=hour_to_hhmm(circular_hour_mean(hours)),
        common_weekdays=[d for d in common if weekday_counts[d] >= 2][:4] or common[:2],
        avg_duration_minutes=(round(_mean_duration, 1) if _mean_duration is not None else None),
        sessions_sampled=len(rows),
        recent_weekly_frequency=recent_weekly_frequency,
        recent_sessions_sampled=recent_sessions_sampled,
        wear_filtered=bool(valid_weeks),
        valid_weeks_sampled=len(valid_weeks),
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
