"""Retrospective reconciliation — runs after an Apple Health import.

Apple Health data is retrospective: only after the user sends a new export do we
learn what actually happened. This module compares what the user *reported* in
the bot (sleep quality, fasting, medications, workouts) against what the import
shows happened, looks for a pattern that *repeats* across enough days, and
proposes a single change for the user to approve — it never changes anything
silently.

The module is dependency-free (no Telegram / no global DB). It takes the bot's
``DB`` object (anything with awaitable ``fetch_all``) and a tzinfo, so it is
easy to unit-test.
"""

from __future__ import annotations

import datetime as dt
import statistics
from dataclasses import dataclass
from typing import Any, Protocol
from zoneinfo import ZoneInfo

# A pattern must hold on at least this many observed days, and on at least this
# fraction of them, before we propose acting on it. Conservative on purpose.
MIN_OBSERVATIONS = 4
MIN_FRACTION = 0.6


class SupportsFetchAll(Protocol):
    async def fetch_all(
        self, sql: str, parameters: tuple[Any, ...] = ()
    ) -> list[dict[str, Any]]: ...


@dataclass
class Insight:
    """A single, explainable finding with an optional proposed change."""

    key: str  # stable id, e.g. "bad_sleep_perf_drop"
    summary: str  # Hebrew, user-facing
    observations: int  # how many days/sessions support it
    confidence: float  # 0..1
    proposal: str | None  # the suggested change (None = informational)
    proposal_action: str | None  # machine key for the approval, e.g. "auto_hold_on_bad_sleep"


def _to_local_date(iso: str, tz: ZoneInfo) -> dt.date:
    parsed = dt.datetime.fromisoformat(iso)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(tz).date()


async def _reported_sleep_by_day(
    db: SupportsFetchAll, user_id: int, tz: ZoneInfo
) -> dict[dt.date, str]:
    rows = await db.fetch_all("SELECT day, flags FROM daily_flags WHERE user_id=?", (user_id,))
    out: dict[dt.date, str] = {}
    import json

    for row in rows:
        try:
            flags = json.loads(row["flags"])
        except Exception:  # noqa: BLE001
            continue
        q = flags.get("sleep_quality")
        if q:
            out[dt.date.fromisoformat(row["day"])] = q
    return out


async def _session_perf_by_day(
    db: SupportsFetchAll, user_id: int, tz: ZoneInfo
) -> dict[dt.date, float]:
    """Average reps per work-set per session day (a simple performance proxy)."""
    rows = await db.fetch_all(
        """
        SELECT ws.ended_at AS ended_at, AVG(s.reps) AS avg_reps
        FROM sessions ws
        JOIN sets s ON s.session_id = ws.id
        WHERE ws.user_id=? AND ws.status IN ('completed','partial')
          AND s.source != 'telegram_split_secondary' AND ws.ended_at IS NOT NULL
        GROUP BY ws.id
        """,
        (user_id,),
    )
    out: dict[dt.date, float] = {}
    for row in rows:
        if row["ended_at"] and row["avg_reps"] is not None:
            out[_to_local_date(row["ended_at"], tz)] = float(row["avg_reps"])
    return out


async def reconcile_bad_sleep_performance(
    db: SupportsFetchAll, user_id: int, tz: ZoneInfo
) -> Insight | None:
    """On days reported as bad sleep, did workout performance drop vs other days?"""
    reported = await _reported_sleep_by_day(db, user_id, tz)
    perf = await _session_perf_by_day(db, user_id, tz)
    if not reported or not perf:
        return None

    bad_days = [d for d, q in reported.items() if q == "bad"]
    good_days = [d for d, q in reported.items() if q in ("good", "ok")]
    bad_perf = [perf[d] for d in bad_days if d in perf]
    good_perf = [perf[d] for d in good_days if d in perf]

    if len(bad_perf) < MIN_OBSERVATIONS or len(good_perf) < MIN_OBSERVATIONS:
        return None

    bad_avg = statistics.fmean(bad_perf)
    good_avg = statistics.fmean(good_perf)
    if good_avg <= 0:
        return None
    drop = (good_avg - bad_avg) / good_avg
    # How consistently was bad-sleep perf below the good-sleep average?
    below = sum(1 for p in bad_perf if p < good_avg)
    fraction = below / len(bad_perf)

    if drop >= 0.08 and fraction >= MIN_FRACTION:
        pct = round(drop * 100)
        return Insight(
            key="bad_sleep_perf_drop",
            summary=(
                f"ב-{below} מתוך {len(bad_perf)} הימים שדיווחת בהם על שינה גרועה, "
                f"הביצועים באימון היו נמוכים בכ-{pct}% מהממוצע שלך."
            ),
            observations=len(bad_perf),
            confidence=min(0.9, 0.5 + fraction / 2),
            proposal=(
                "בפעם הבאה שתדווח על שינה גרועה — להציע אוטומטית לשמור משקל "
                "במקום לעלות, כדי להוריד סיכון לפציעה?"
            ),
            proposal_action="auto_hold_on_bad_sleep",
        )
    return None


async def reconcile_sleep_report_accuracy(
    db: SupportsFetchAll, user_id: int, tz: ZoneInfo
) -> Insight | None:
    """Compare reported sleep quality with measured sleep duration (informational)."""
    reported = await _reported_sleep_by_day(db, user_id, tz)
    if not reported:
        return None
    sleep_rows = await db.fetch_all(
        "SELECT start_time, value FROM health WHERE user_id=? AND sample_type='sleep_session'",
        (user_id,),
    )
    measured: dict[dt.date, float] = {}
    for row in sleep_rows:
        # sleep belongs to the night-of the prior evening (early morning -> prev day)
        local = dt.datetime.fromisoformat(row["start_time"])
        if local.tzinfo is None:
            local = local.replace(tzinfo=dt.timezone.utc)
        local = local.astimezone(tz)
        night = local.date() if local.hour >= 12 else local.date() - dt.timedelta(days=1)
        measured[night] = measured.get(night, 0.0) + float(row["value"] or 0.0)

    pairs = [(reported[d], measured[d]) for d in reported if d in measured]
    if len(pairs) < MIN_OBSERVATIONS:
        return None
    # Disagreements: reported "good" but measured < 6h, or "bad" but > 7h.
    mismatch = sum(
        1
        for q, mins in pairs
        if (q in ("good", "ok") and mins < 360) or (q == "bad" and mins > 420)
    )
    if mismatch / len(pairs) >= 0.4:
        return Insight(
            key="sleep_report_mismatch",
            summary=(
                f"ב-{mismatch} מתוך {len(pairs)} ימים התחושה שדיווחת לגבי השינה "
                "לא תאמה את המשך שנמדד בשעון. שניהם תקפים — תחושה ומדידה הם "
                "דברים שונים."
            ),
            observations=len(pairs),
            confidence=0.6,
            proposal=None,
            proposal_action=None,
        )
    return None


async def run_reconciliation(db: SupportsFetchAll, user_id: int, tz: ZoneInfo) -> list[Insight]:
    """Run all checks; return insights sorted by confidence (highest first).

    The bot should surface at most one *actionable* proposal at a time so the
    user isn't flooded.
    """
    checks = [
        reconcile_bad_sleep_performance,
        reconcile_sleep_report_accuracy,
    ]
    insights: list[Insight] = []
    for check in checks:
        try:
            result = await check(db, user_id, tz)
        except Exception:  # noqa: BLE001
            result = None
        if result:
            insights.append(result)
    insights.sort(key=lambda i: i.confidence, reverse=True)
    return insights
