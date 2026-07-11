"""Apple Health export (ZIP/XML) importer.

Streams the very large Apple Health ``export.xml`` (often >1 GB) without loading
it into memory, keeps only the last ~18 months, and produces compact rows that
feed the coach bot's routine-learning engine.

Design notes
------------
The raw export contains hundreds of thousands of tiny samples (heart rate every
few seconds, step counts per minute, ...). Storing them verbatim would bloat the
``health`` table and slow every query. Instead we emit:

* **Daily aggregates** (one row per day per metric) for cumulative / averaged
  signals: steps, active/basal energy, exercise minutes, resting HR, HRV, body
  mass / fat. These power "how active was the day" and weight-trend logic.
* **Discrete event rows** for things whose *timing* matters to routine learning:
  one row per sleep session (asleep only) and one row per workout.

All rows reuse the existing ``health`` schema and ``insert_health_sample``
helper in ``coach_bot.py`` via a deterministic ``external_id`` so re-imports are
idempotent (duplicate rows are skipped, not duplicated).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import stat
import xml.etree.ElementTree as ET
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Iterator
from zoneinfo import ZoneInfo

LOGGER = logging.getLogger("noam_coach.health_import")

# How far back to keep data. ~18 months.
DEFAULT_RETENTION_DAYS = 548

# Apple "asleep" category values (any export version). "InBed" is intentionally
# excluded: it is presence in bed, not actual sleep.
ASLEEP_VALUES = {
    "HKCategoryValueSleepAnalysisAsleep",
    "HKCategoryValueSleepAnalysisAsleepCore",
    "HKCategoryValueSleepAnalysisAsleepDeep",
    "HKCategoryValueSleepAnalysisAsleepREM",
    "HKCategoryValueSleepAnalysisAsleepUnspecified",
}

# Quantity record types we aggregate to a per-day total (sum over the day).
DAILY_SUM_TYPES = {
    "HKQuantityTypeIdentifierStepCount": ("steps", "count"),
    "HKQuantityTypeIdentifierActiveEnergyBurned": ("active_calories", "kcal"),
    "HKQuantityTypeIdentifierBasalEnergyBurned": ("basal_calories", "kcal"),
    "HKQuantityTypeIdentifierAppleExerciseTime": ("exercise_minutes", "min"),
    "HKQuantityTypeIdentifierFlightsClimbed": ("flights_climbed", "count"),
}

# Quantity record types we aggregate to a per-day average (mean over the day).
DAILY_AVG_TYPES = {
    "HKQuantityTypeIdentifierRestingHeartRate": ("resting_heart_rate", "bpm"),
    "HKQuantityTypeIdentifierHeartRateVariabilitySDNN": ("hrv", "ms"),
    "HKQuantityTypeIdentifierWalkingHeartRateAverage": (
        "walking_heart_rate",
        "bpm",
    ),
}

# Quantity record types where we keep the *latest* reading of the day (a
# measurement, not something to sum/average across the day).
DAILY_LAST_TYPES = {
    "HKQuantityTypeIdentifierBodyMass": ("weight", "kg"),
    "HKQuantityTypeIdentifierBodyFatPercentage": ("body_fat", "%"),
    "HKQuantityTypeIdentifierLeanBodyMass": ("lean_body_mass", "kg"),
    "HKQuantityTypeIdentifierBodyMassIndex": ("bmi", "count"),
}

# Metrics where overlapping sources (Watch, iPhone, third-party apps) can cause
# double-counting. For these we accumulate per-source and pick the preferred one.
_DEDUP_SUM_METRICS = {"steps", "active_calories"}

# Daily watch-wear coverage row. One row per local day the Apple Watch produced
# any sample: value = hours between the first and last watch sample of the day,
# start_time/end_time = those first/last instants (UTC). Downstream weekly
# statistics use these rows to "burn" days/weeks where the watch was not worn
# (no data ≠ no activity).
WEAR_SAMPLE_TYPE = "watch_wear"


def _is_watch_source(source: str | None) -> bool:
    return bool(source) and "watch" in source.lower()

# Source preference: Apple Watch is most accurate for motion/energy, then iPhone,
# then anything else. Matched by substring (case-insensitive) in sourceName.
_SOURCE_PRIORITY = [
    "apple watch",
    "iphone",
    "ipad",
]


def _pick_preferred_source(
    per_source: dict[str, float],
) -> tuple[float, str]:
    """Pick the highest-priority source total from a per-source dict."""
    lower_map = {src.lower(): src for src in per_source}
    for preferred in _SOURCE_PRIORITY:
        for lower_src, original_src in lower_map.items():
            if preferred in lower_src:
                return per_source[original_src], original_src
    # No known source matched — pick the source with the highest total (most
    # likely the primary device).
    best_src = max(per_source, key=lambda s: per_source[s])
    return per_source[best_src], best_src


def parse_apple_datetime(raw: str) -> dt.datetime | None:
    """Parse Apple's ``YYYY-MM-DD HH:MM:SS +ZZZZ`` timestamps (tz-aware)."""
    if not raw:
        return None
    try:
        return dt.datetime.strptime(raw, "%Y-%m-%d %H:%M:%S %z")
    except ValueError:
        return None


@dataclass
class HealthRow:
    """A normalized row ready for insertion into the ``health`` table."""

    external_id: str
    sample_type: str
    value: float
    unit: str
    start_time: str  # ISO-8601 UTC
    end_time: str | None = None
    source_device: str | None = None


@dataclass
class ImportSummary:
    rows: int = 0
    by_type: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    min_date: str | None = None
    max_date: str | None = None
    workouts: int = 0
    sleep_sessions: int = 0
    weight_records: int = 0
    activity_records: int = 0
    invalid_records: int = 0

    def note(self, sample_type: str, day: dt.date) -> None:
        self.rows += 1
        self.by_type[sample_type] += 1
        iso = day.isoformat()
        if self.min_date is None or iso < self.min_date:
            self.min_date = iso
        if self.max_date is None or iso > self.max_date:
            self.max_date = iso


def is_valid_zip(path: Path) -> bool:
    """Validate ZIP structure without extracting it."""
    try:
        return zipfile.is_zipfile(path)
    except OSError:
        return False


def looks_like_xml(path: Path) -> bool:
    """Conservative magic check for a direct XML upload."""
    try:
        with path.open("rb") as handle:
            prefix = handle.read(4096)
    except OSError:
        return False
    prefix = prefix.lstrip(b"\xef\xbb\xbf \t\r\n")
    return prefix.startswith((b"<?xml", b"<HealthData", b"<!DOCTYPE"))


def find_export_xml(extract_dir: Path) -> Path | None:
    """Locate the main records XML (not ``export_cda.xml``) under a folder."""
    candidates = [p for p in extract_dir.rglob("*.xml") if "cda" not in p.name.lower()]
    if not candidates:
        return None
    # The records file is by far the largest.
    return max(candidates, key=lambda p: p.stat().st_size)


DEFAULT_MAX_ZIP_MEMBERS = 100_000
DEFAULT_MAX_XML_BYTES = 4 * 1024 * 1024 * 1024  # 4 GiB, streamed to disk
DEFAULT_MAX_COMPRESSION_RATIO = 250.0


def _safe_zip_member(info: zipfile.ZipInfo) -> bool:
    """Return whether a ZIP member is a normal relative file path.

    Apple Health exports may contain nested directories and many unrelated
    files, but the importer only needs the records XML. Reject absolute paths,
    parent traversal, Windows drive paths and symbolic links before any bytes
    are written to disk.
    """
    name = info.filename.replace("\\", "/")
    path = PurePosixPath(name)
    if not name or name.startswith("/") or path.is_absolute():
        return False
    if any(part in ("", ".", "..") for part in path.parts):
        return False
    if len(path.parts[0]) >= 2 and path.parts[0][1] == ":":
        return False
    mode = (info.external_attr >> 16) & 0o170000
    if mode == stat.S_IFLNK:
        return False
    return not info.is_dir()


def _select_export_xml(infos: list[zipfile.ZipInfo]) -> zipfile.ZipInfo | None:
    safe_xml = [
        info
        for info in infos
        if _safe_zip_member(info)
        and info.filename.lower().endswith(".xml")
        and "cda" not in PurePosixPath(info.filename).name.lower()
    ]
    if not safe_xml:
        return None

    exact = [info for info in safe_xml if PurePosixPath(info.filename).name.lower() == "export.xml"]
    candidates = exact or safe_xml
    return max(candidates, key=lambda info: info.file_size)


def extract_zip(
    zip_path: Path,
    extract_dir: Path,
    *,
    max_members: int = DEFAULT_MAX_ZIP_MEMBERS,
    max_xml_bytes: int = DEFAULT_MAX_XML_BYTES,
    max_compression_ratio: float = DEFAULT_MAX_COMPRESSION_RATIO,
) -> Path | None:
    """Safely stream only Apple Health's main records XML from a ZIP.

    The previous implementation called ``extractall`` and therefore trusted
    every path and decompressed member in the archive. This implementation
    never extracts unrelated files, rejects path traversal/symlinks/encrypted
    entries, and enforces both declared and actual output-size limits.
    """
    extract_dir.mkdir(parents=True, exist_ok=True)
    destination = extract_dir / "export.xml"

    try:
        with zipfile.ZipFile(zip_path) as zf:
            infos = zf.infolist()
            if len(infos) > max_members:
                raise ValueError(f"ZIP contains too many entries ({len(infos)} > {max_members})")

            selected = _select_export_xml(infos)
            if selected is None:
                return None
            if selected.flag_bits & 0x1:
                raise ValueError("Encrypted ZIP entries are not supported")
            if selected.file_size <= 0:
                raise ValueError("Apple Health export.xml is empty")
            if selected.file_size > max_xml_bytes:
                raise ValueError(f"export.xml is too large ({selected.file_size} bytes)")

            compressed = max(1, selected.compress_size)
            ratio = selected.file_size / compressed
            if ratio > max_compression_ratio:
                raise ValueError(f"Suspicious ZIP compression ratio ({ratio:.1f}:1)")

            written = 0
            with zf.open(selected, "r") as source, destination.open("wb") as target:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > max_xml_bytes:
                        raise ValueError("export.xml exceeded the extraction limit")
                    target.write(chunk)

            if written != selected.file_size:
                LOGGER.warning(
                    "ZIP member size mismatch: declared=%s actual=%s",
                    selected.file_size,
                    written,
                )
            return destination
    except (zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise ValueError("Invalid or unsupported ZIP file") from exc
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def _merge_intervals(
    intervals: list[tuple[dt.datetime, dt.datetime, str | None]],
) -> list[tuple[dt.datetime, dt.datetime, str | None]]:
    """Merge overlapping time intervals (interval union).

    Input:  [(start, end, source), ...]
    Output: merged list sorted by start, with overlaps combined.
    """
    if not intervals:
        return []
    sorted_ivs = sorted(intervals, key=lambda iv: iv[0])
    merged: list[tuple[dt.datetime, dt.datetime, str | None]] = [sorted_ivs[0]]
    for start, end, source in sorted_ivs[1:]:
        prev_start, prev_end, prev_source = merged[-1]
        if start <= prev_end:
            # Overlapping or adjacent — extend
            merged[-1] = (prev_start, max(prev_end, end), prev_source)
        else:
            merged.append((start, end, source))
    return merged


def iter_health_rows(
    xml_path: Path,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    local_tz: ZoneInfo | None = None,
    now: dt.datetime | None = None,
) -> Iterator[HealthRow]:
    """Stream ``xml_path`` and yield compact :class:`HealthRow` objects.

    Daily aggregates are accumulated in memory keyed by ``(metric, day)`` —
    that is at most a few thousand entries per metric over 18 months, which is
    tiny. Sleep sessions and workouts are emitted as they are encountered.

    ``local_tz`` determines which calendar day a sample belongs to. Without it,
    a measurement at 00:30 local time could land on the previous UTC day.
    Defaults to ``Asia/Jerusalem`` if not supplied.
    """
    if local_tz is None:
        local_tz = ZoneInfo("Asia/Jerusalem")
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(days=retention_days)

    sum_acc: dict[tuple[str, str, dt.date], list] = {}
    # Per-source accumulator for metrics prone to double-counting.
    # key → {sourceName: total}
    dedup_acc: dict[tuple[str, str, dt.date], dict[str, float]] = {}
    avg_acc: dict[tuple[str, str, dt.date], list] = {}
    last_acc: dict[tuple[str, str, dt.date], tuple] = {}
    # Sleep intervals grouped by night (local date), for interval-union dedup.
    sleep_intervals: dict[dt.date, list[tuple[dt.datetime, dt.datetime, str | None]]] = {}
    # Watch-wear bounds per local day: day → [first_local_dt, last_local_dt].
    # Built from EVERY watch-sourced sample (including record types we do not
    # otherwise keep, e.g. raw heart rate), since any watch sample proves the
    # watch was on the wrist at that instant.
    wear_acc: dict[dt.date, list[dt.datetime]] = {}

    def in_window(start: dt.datetime) -> bool:
        return start >= cutoff

    def note_wear(local_instant: dt.datetime) -> None:
        day_key = local_instant.date()
        bounds = wear_acc.get(day_key)
        if bounds is None:
            wear_acc[day_key] = [local_instant, local_instant]
        else:
            if local_instant < bounds[0]:
                bounds[0] = local_instant
            if local_instant > bounds[1]:
                bounds[1] = local_instant

    for _event, el in ET.iterparse(str(xml_path), events=("end",)):
        tag = el.tag
        if tag == "Record":
            rtype = el.get("type")
            start = parse_apple_datetime(el.get("startDate"))
            if start is None or not in_window(start):
                el.clear()
                continue
            local_start = start.astimezone(local_tz)
            day = local_start.date()
            source = el.get("sourceName")
            watch_source = _is_watch_source(source)
            if watch_source:
                note_wear(local_start)

            if rtype in DAILY_SUM_TYPES:
                metric, unit = DAILY_SUM_TYPES[rtype]
                try:
                    val = float(el.get("value"))
                except (TypeError, ValueError):
                    val = 0.0
                key = (metric, unit, day)
                if metric in _DEDUP_SUM_METRICS:
                    per_src = dedup_acc.setdefault(key, {})
                    src_name = source or "unknown"
                    per_src[src_name] = per_src.get(src_name, 0.0) + val
                else:
                    acc = sum_acc.setdefault(key, [0.0, source])
                    acc[0] += val
            elif rtype in DAILY_AVG_TYPES:
                metric, unit = DAILY_AVG_TYPES[rtype]
                try:
                    val = float(el.get("value"))
                except (TypeError, ValueError):
                    el.clear()
                    continue
                key = (metric, unit, day)
                acc = avg_acc.setdefault(key, [0.0, 0, source])
                acc[0] += val
                acc[1] += 1
            elif rtype in DAILY_LAST_TYPES:
                metric, unit = DAILY_LAST_TYPES[rtype]
                try:
                    val = float(el.get("value"))
                except (TypeError, ValueError):
                    el.clear()
                    continue
                key = (metric, unit, day)
                prev = last_acc.get(key)
                if prev is None or start > prev[1]:
                    last_acc[key] = (val, start, source)
            elif rtype == "HKCategoryTypeIdentifierSleepAnalysis":
                if el.get("value") in ASLEEP_VALUES:
                    end = parse_apple_datetime(el.get("endDate"))
                    if end is not None:
                        if watch_source:
                            # A sleep sample ending in the morning proves wear
                            # on the wake-up day too, not just the night's day.
                            note_wear(end.astimezone(local_tz))
                        # Group by "sleep night" — the local date the sleep
                        # started on (if before 18:00 local, assign to previous
                        # date since it's a nap continuation of the prior night).
                        night_date = local_start.date()
                        if local_start.hour < 18:
                            night_date -= dt.timedelta(days=1)
                        sleep_intervals.setdefault(night_date, []).append(
                            (start.astimezone(dt.timezone.utc),
                             end.astimezone(dt.timezone.utc), source)
                        )
            el.clear()
        elif tag == "Workout":
            start = parse_apple_datetime(el.get("startDate"))
            if start is None or not in_window(start):
                el.clear()
                continue
            end = parse_apple_datetime(el.get("endDate"))
            if _is_watch_source(el.get("sourceName")):
                note_wear(start.astimezone(local_tz))
                if end is not None:
                    note_wear(end.astimezone(local_tz))
            try:
                duration = float(el.get("duration") or 0.0)
            except ValueError:
                duration = 0.0
            activity = (el.get("workoutActivityType") or "").replace("HKWorkoutActivityType", "")
            start_utc = start.astimezone(dt.timezone.utc)
            yield HealthRow(
                external_id=f"ah:workout:{start_utc.isoformat()}",
                sample_type="workout",
                value=round(duration, 1),
                unit="min",
                start_time=start_utc.isoformat(),
                end_time=(end.astimezone(dt.timezone.utc).isoformat() if end else None),
                source_device=activity or el.get("sourceName"),
            )
            el.clear()
        elif tag in ("ExportDate", "Me"):
            el.clear()

    # Flush merged sleep sessions — one row per night with overlaps resolved.
    for night_date in sorted(sleep_intervals):
        intervals = sleep_intervals[night_date]
        merged = _merge_intervals(intervals)
        if not merged:
            continue
        total_minutes = sum(
            (end - start).total_seconds() / 60.0 for start, end, _ in merged
        )
        earliest_start = merged[0][0]
        latest_end = merged[-1][1]
        source_name = merged[0][2]
        yield HealthRow(
            external_id=f"ah:sleep:{night_date.isoformat()}",
            sample_type="sleep_session",
            value=round(total_minutes, 1),
            unit="min",
            start_time=earliest_start.isoformat(),
            end_time=latest_end.isoformat(),
            source_device=source_name,
        )

    # Flush watch-wear coverage — one row per local day the watch was worn.
    for day, (first_local, last_local) in wear_acc.items():
        span_hours = (last_local - first_local).total_seconds() / 3600.0
        yield HealthRow(
            external_id=f"ah:{WEAR_SAMPLE_TYPE}:{day.isoformat()}",
            sample_type=WEAR_SAMPLE_TYPE,
            value=round(span_hours, 2),
            unit="h",
            start_time=first_local.astimezone(dt.timezone.utc).isoformat(),
            end_time=last_local.astimezone(dt.timezone.utc).isoformat(),
            source_device="apple_watch",
        )

    # Flush dedup'd daily sums. For StepCount, keep source diagnostics and use
    # the dominant source as the conservative planning value. Apple Watch can
    # cover only part of a day while iPhone covers the rest, so "Watch always
    # wins" undercounts many mixed-source days.
    for (metric, unit, day), per_source in dedup_acc.items():
        if metric == "steps":
            source = max(per_source, key=lambda src: per_source[src])
            total = per_source[source]
            source_device = json.dumps(
                {
                    "selected_source": source,
                    "source_totals": per_source,
                    "raw_all_sources": sum(per_source.values()),
                    "conservative": total,
                    "selection_reason": "dominant_stepcount_source_for_day",
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        else:
            total, source = _pick_preferred_source(per_source)
            source_device = source
        yield HealthRow(
            external_id=f"ah:{metric}:{day.isoformat()}",
            sample_type=metric,
            value=round(total, 3),
            unit=unit,
            start_time=_day_start_iso(day, local_tz),
            source_device=source_device,
        )

    # Flush remaining daily sums (non-dedup metrics).
    for (metric, unit, day), (total, source) in sum_acc.items():
        yield HealthRow(
            external_id=f"ah:{metric}:{day.isoformat()}",
            sample_type=metric,
            value=round(total, 3),
            unit=unit,
            start_time=_day_start_iso(day, local_tz),
            source_device=source,
        )
    for (metric, unit, day), (total, count, source) in avg_acc.items():
        if count == 0:
            continue
        yield HealthRow(
            external_id=f"ah:{metric}:{day.isoformat()}",
            sample_type=metric,
            value=round(total / count, 3),
            unit=unit,
            start_time=_day_start_iso(day, local_tz),
            source_device=source,
        )
    for (metric, unit, day), (val, _ts, source) in last_acc.items():
        yield HealthRow(
            external_id=f"ah:{metric}:{day.isoformat()}",
            sample_type=metric,
            value=round(val, 3),
            unit=unit,
            start_time=_day_start_iso(day, local_tz),
            source_device=source,
        )


def _day_start_iso(day: dt.date, tz: ZoneInfo | None = None) -> str:
    """Return ISO-8601 UTC timestamp for midnight of *day* in the given tz."""
    tzinfo = tz or dt.timezone.utc
    local_midnight = dt.datetime(day.year, day.month, day.day, tzinfo=tzinfo)
    return local_midnight.astimezone(dt.timezone.utc).isoformat()


_ACTIVITY_TYPES = {"steps", "active_calories", "exercise_minutes", "flights_climbed"}
_WEIGHT_TYPES = {"weight", "body_fat_pct", "lean_body_mass", "bmi"}


def summarize(rows: Iterator[HealthRow]) -> tuple[list[HealthRow], ImportSummary]:
    """Materialize rows and compute a summary (used by tests / CLI)."""
    summary = ImportSummary()
    materialized: list[HealthRow] = []
    for row in rows:
        materialized.append(row)
        day = dt.datetime.fromisoformat(row.start_time).date()
        summary.note(row.sample_type, day)
        if row.sample_type == "workout":
            summary.workouts += 1
        elif row.sample_type == "sleep_session":
            summary.sleep_sessions += 1
        if row.sample_type in _WEIGHT_TYPES:
            summary.weight_records += 1
        elif row.sample_type in _ACTIVITY_TYPES:
            summary.activity_records += 1
    return materialized, summary


if __name__ == "__main__":  # pragma: no cover - manual CLI
    import argparse
    import sys

    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="Parse Apple Health export")
    parser.add_argument("path", help="export.xml or extracted folder")
    parser.add_argument("--days", type=int, default=DEFAULT_RETENTION_DAYS)
    args = parser.parse_args()

    target = Path(args.path)
    if target.is_dir():
        xml = find_export_xml(target)
    else:
        xml = target
    if not xml:
        raise SystemExit("Could not find records XML")

    _rows, summary = summarize(iter_health_rows(xml, retention_days=args.days))
    print(f"rows: {summary.rows}")
    print(f"date range: {summary.min_date} .. {summary.max_date}")
    print(f"workouts: {summary.workouts}  sleep sessions: {summary.sleep_sessions}")
    print("by type:")
    for stype, count in sorted(summary.by_type.items(), key=lambda x: -x[1]):
        print(f"  {count:>7} {stype}")
