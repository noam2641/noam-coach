"""RE13 regression tests — health data quality calibration.

Covers:
  * Week policies: strict_7_of_7 with enough full weeks, the relaxed
    5-of-7 normalized fallback, and the insufficient case.
  * Normalization math (2 workouts over 5 worn days → 2.8/week).
  * Sleep minimum-nights policy (2 → no regular confirmation, 7 → medium
    warning, 12 → normal).
  * Export freshness (3d → none, 21d → stale warning, 45d → strong warning
    that also caps the overall confidence).
  * Wizard integration: steps prompt shows counted/excluded days, the
    frequency prompt reflects the policy, an insufficient frequency asks the
    user directly, and a 2-night sleep detection is never offered as a
    regular confirmation.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

import coach_bot
import health_import
import routine
import user_model
from db import Database
from helpers import utc_now
from noam_coach.bot import callback_menu as callback_menu_bot
from noam_coach.bot import onboarding as onboarding_bot
from noam_coach.services import core as core_services
from noam_coach.services import health_jobs, health_quality

TZ = ZoneInfo("Asia/Jerusalem")


# ---------------------------------------------------------------------------
# Mock DB routing each routine/quality query to its own row set
# ---------------------------------------------------------------------------


class QualityDB:
    def __init__(
        self,
        wear: list[dict[str, Any]] | None = None,
        workouts: list[dict[str, Any]] | None = None,
        steps: list[dict[str, Any]] | None = None,
        sleep: list[dict[str, Any]] | None = None,
        newest: str | None = None,
    ) -> None:
        self.wear = wear or []
        self.workouts = workouts or []
        self.steps = steps or []
        self.sleep = sleep or []
        self.newest = newest

    async def fetch_all(self, sql: str, parameters: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        if "MAX(start_time)" in sql:
            return [{"newest": self.newest}]
        if "watch_wear" in sql:
            return self.wear
        if "sample_type='workout'" in sql:
            return self.workouts
        if "sample_type='steps'" in sql:
            return self.steps
        if "sample_type='sleep_session'" in sql:
            return self.sleep
        return []


def _last_complete_weeks(count: int) -> list[dt.date]:
    """Mondays of the last ``count`` complete Mon-Sun weeks (oldest first)."""
    today = dt.datetime.now(TZ).date()
    this_monday = today - dt.timedelta(days=today.weekday())
    return [this_monday - dt.timedelta(days=7 * i) for i in range(count, 0, -1)]


def _wear_row(day: dt.date, first_hour: float = 8.0, last_hour: float = 22.0) -> dict[str, Any]:
    start = dt.datetime.combine(day, dt.time(int(first_hour), int((first_hour % 1) * 60)), tzinfo=TZ)
    end = dt.datetime.combine(day, dt.time(int(last_hour), int((last_hour % 1) * 60)), tzinfo=TZ)
    return {
        "sample_type": "watch_wear",
        "start_time": start.isoformat(),
        "end_time": end.isoformat(),
        "source_device": "apple_watch",
    }


def _workout_row(day: dt.date, hour: int = 18) -> dict[str, Any]:
    start = dt.datetime.combine(day, dt.time(hour, 0), tzinfo=TZ)
    return {"start_time": start.isoformat(), "value": 45.0}


def _week_fixture(
    monday: dt.date, worn_days: int, workout_offsets: tuple[int, ...]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    wear = [_wear_row(monday + dt.timedelta(days=i)) for i in range(worn_days)]
    workouts = [_workout_row(monday + dt.timedelta(days=i)) for i in workout_offsets]
    return wear, workouts


def _newest_iso(days_old: int) -> str:
    day = dt.datetime.now(TZ) - dt.timedelta(days=days_old)
    return day.isoformat()


# ---------------------------------------------------------------------------
# Policy: strict / relaxed / insufficient
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_strict_policy_with_three_full_weeks() -> None:
    wear: list[dict[str, Any]] = []
    workouts: list[dict[str, Any]] = []
    for monday in _last_complete_weeks(3):
        w, k = _week_fixture(monday, worn_days=7, workout_offsets=(0, 2, 4))
        wear += w
        workouts += k

    analysis = await routine.analyze_training_weeks(QualityDB(wear=wear, workouts=workouts), 1, TZ)
    assert analysis is not None
    assert analysis.policy == routine.POLICY_STRICT
    assert analysis.valid_weeks_strict == 3
    assert analysis.frequency_raw == 3.0

    report = await health_quality.build_health_quality_report(
        QualityDB(wear=wear, workouts=workouts, newest=_newest_iso(1)), 1, TZ
    )
    wq = report["workout_frequency"]
    assert wq["policy"] == routine.POLICY_STRICT
    assert wq["confidence"] == health_quality.CONFIDENCE_HIGH
    assert wq["frequency"] == 3.0


@pytest.mark.asyncio
async def test_relaxed_policy_normalizes_partial_weeks() -> None:
    mondays = _last_complete_weeks(4)
    wear: list[dict[str, Any]] = []
    workouts: list[dict[str, Any]] = []
    # One fully-worn week with 3 workouts...
    w, k = _week_fixture(mondays[0], worn_days=7, workout_offsets=(0, 2, 4))
    wear += w
    workouts += k
    # ...and three 5-worn-day weeks with 2 workouts each (on worn days).
    for monday in mondays[1:]:
        w, k = _week_fixture(monday, worn_days=5, workout_offsets=(0, 2))
        wear += w
        workouts += k

    analysis = await routine.analyze_training_weeks(QualityDB(wear=wear, workouts=workouts), 1, TZ)
    assert analysis is not None
    assert analysis.policy == routine.POLICY_RELAXED
    assert analysis.valid_weeks_strict == 1
    assert analysis.valid_weeks_relaxed == 4
    # Normalization: 2 workouts over 5 worn days scales to 2.8 per week.
    partial = [w for w in analysis.weeks if w.worn_days == 5]
    assert partial and partial[0].normalized_workouts == pytest.approx(2.8)
    # Mean of (3.0, 2.8, 2.8, 2.8) → 2.85 → one-decimal rounding.
    assert analysis.frequency_normalized == pytest.approx(2.85, abs=0.06)
    assert analysis.frequency_raw == 3.0  # strict raw kept alongside

    report = await health_quality.build_health_quality_report(
        QualityDB(wear=wear, workouts=workouts, newest=_newest_iso(1)), 1, TZ
    )
    wq = report["workout_frequency"]
    assert wq["policy"] == routine.POLICY_RELAXED
    assert wq["confidence"] == health_quality.CONFIDENCE_MEDIUM
    assert wq["frequency"] == analysis.frequency_normalized
    assert "5" in wq["warning_he"]  # explains the 5-of-7 basis


@pytest.mark.asyncio
async def test_insufficient_weeks_yield_low_confidence() -> None:
    mondays = _last_complete_weeks(2)
    wear, workouts = _week_fixture(mondays[0], worn_days=7, workout_offsets=(0, 2))
    w2, k2 = _week_fixture(mondays[1], worn_days=3, workout_offsets=(0,))
    wear += w2
    workouts += k2

    analysis = await routine.analyze_training_weeks(QualityDB(wear=wear, workouts=workouts), 1, TZ)
    assert analysis is not None
    assert analysis.policy == routine.POLICY_INSUFFICIENT

    report = await health_quality.build_health_quality_report(
        QualityDB(wear=wear, workouts=workouts, newest=_newest_iso(1)), 1, TZ
    )
    wq = report["workout_frequency"]
    assert wq["confidence"] == health_quality.CONFIDENCE_LOW
    assert wq["frequency"] is None
    assert "אין מספיק" in wq["warning_he"]


@pytest.mark.asyncio
async def test_workouts_on_unworn_days_are_flagged_not_counted() -> None:
    mondays = _last_complete_weeks(3)
    wear: list[dict[str, Any]] = []
    workouts: list[dict[str, Any]] = []
    for monday in mondays:
        w, k = _week_fixture(monday, worn_days=5, workout_offsets=(0, 2))
        wear += w
        workouts += k
        # A workout logged on an unworn day (e.g. phone-logged gym session).
        workouts.append(_workout_row(monday + dt.timedelta(days=6)))

    analysis = await routine.analyze_training_weeks(QualityDB(wear=wear, workouts=workouts), 1, TZ)
    assert analysis is not None
    assert analysis.workouts_on_unworn_days == 3
    # Normalized frequency is based on worn-day workouts only (2 per week).
    assert analysis.frequency_normalized == pytest.approx(2.8)


# ---------------------------------------------------------------------------
# Sleep minimum nights
# ---------------------------------------------------------------------------


def test_sleep_two_nights_is_not_confirmable() -> None:
    section = health_quality._sleep_section(2)
    assert section["should_ask_confirmation"] is False
    assert section["confidence"] == health_quality.CONFIDENCE_LOW
    assert "לא מספיק" in section["warning_he"] or "אין מספיק" in section["warning_he"]


def test_sleep_five_nights_is_still_not_confirmable() -> None:
    section = health_quality._sleep_section(5)
    assert section["should_ask_confirmation"] is False
    assert section["confidence"] == health_quality.CONFIDENCE_LOW
    assert section["minimum_required"] == 7


def test_sleep_seven_nights_confirmable_with_warning() -> None:
    section = health_quality._sleep_section(7)
    assert section["should_ask_confirmation"] is True
    assert section["confidence"] == health_quality.CONFIDENCE_MEDIUM
    assert section["warning_he"]


def test_sleep_twelve_nights_normal() -> None:
    section = health_quality._sleep_section(12)
    assert section["should_ask_confirmation"] is True
    assert section["confidence"] == health_quality.CONFIDENCE_HIGH
    assert section["warning_he"] == ""


# ---------------------------------------------------------------------------
# Export freshness
# ---------------------------------------------------------------------------


def test_export_freshness_status_is_shared_policy() -> None:
    now = dt.datetime(2026, 7, 7, 12, 0, tzinfo=TZ)

    fresh = health_quality.export_freshness_status("2026-06-30", now=now, tz=TZ)
    assert fresh["latest_sample_date"] == "2026-06-30"
    assert fresh["days_old"] == 7
    assert fresh["is_stale"] is False
    assert fresh["is_very_stale"] is False

    stale = health_quality.export_freshness_status("2026-06-29", now=now, tz=TZ)
    assert stale["days_old"] == 8
    assert stale["is_stale"] is True
    assert stale["is_very_stale"] is False


def test_import_summary_and_quality_summary_share_dataset_end_date() -> None:
    import health_import

    summary = health_import.ImportSummary()
    summary.note("steps", dt.date(2026, 6, 1))
    summary.note("steps", dt.date(2026, 6, 15))
    outcome = health_jobs.HealthImportOutcome(
        inserted=2,
        duplicates=0,
        updated=0,
        invalid=0,
        summary=summary,
        profile={},
        source_file="export.zip",
        total_stored=2,
        import_started="2026-07-09T00:00:00+00:00",
        import_completed="2026-07-09T00:01:00+00:00",
        dataset_end_date="2026-06-16",
    )
    import_text = health_jobs._health_import_success_text(outcome)
    report = {
        "workout_frequency": {"confidence": health_quality.CONFIDENCE_LOW, "policy": None},
        "steps": {"days_used": 0, "days_excluded": 0},
        "sleep": {"should_ask_confirmation": False, "nights_used": 0},
        "freshness": {
            "latest_sample_date": "2026-06-16",
            "days_old": 23,
            "is_stale": True,
        },
    }
    quality_text = "\n".join(health_quality.quality_summary_lines_he(report))

    assert "2026-06-16" in import_text
    assert "2026-06-16" in quality_text
    assert "2026-06-15" not in import_text


@pytest.mark.asyncio
async def test_fresh_export_has_no_warning() -> None:
    report = await health_quality.build_health_quality_report(
        QualityDB(newest=_newest_iso(3)), 1, TZ
    )
    freshness = report["freshness"]
    assert freshness["is_stale"] is False
    assert freshness["warning_he"] == ""


@pytest.mark.asyncio
async def test_eight_day_old_export_warns() -> None:
    report = await health_quality.build_health_quality_report(
        QualityDB(newest=_newest_iso(8)), 1, TZ
    )
    freshness = report["freshness"]
    assert freshness["is_stale"] is True
    assert freshness["days_old"] == 8
    assert freshness["warning_he"]


@pytest.mark.asyncio
async def test_three_week_old_export_warns() -> None:
    report = await health_quality.build_health_quality_report(
        QualityDB(newest=_newest_iso(21)), 1, TZ
    )
    freshness = report["freshness"]
    assert freshness["is_stale"] is True
    assert freshness["days_old"] == 21
    assert "לייצא" in freshness["warning_he"]


def _complete_weeks_before(anchor: dt.date, count: int) -> list[dt.date]:
    """Mondays of the ``count`` complete Mon-Sun weeks ending on/before anchor."""
    this_monday = anchor - dt.timedelta(days=anchor.weekday())
    return [this_monday - dt.timedelta(days=7 * i) for i in range(count, 0, -1)]


@pytest.mark.asyncio
async def test_very_stale_export_caps_overall_confidence() -> None:
    # Stale-window fix: 3 full weeks of workouts sit in the file's LAST period
    # (ending 45 days ago), not near today. The analysis anchors to the
    # dataset end, so the workout signal is still HIGH — but a 45-day-old
    # export caps the OVERALL confidence at medium with a strong warning.
    anchor = dt.datetime.now(TZ).date() - dt.timedelta(days=45)
    wear: list[dict[str, Any]] = []
    workouts: list[dict[str, Any]] = []
    for monday in _complete_weeks_before(anchor, 3):
        w, k = _week_fixture(monday, worn_days=7, workout_offsets=(0, 2, 4))
        wear += w
        workouts += k
    report = await health_quality.build_health_quality_report(
        QualityDB(wear=wear, workouts=workouts, newest=_newest_iso(45)), 1, TZ
    )
    assert report["workout_frequency"]["confidence"] == health_quality.CONFIDENCE_HIGH
    assert report["freshness"]["days_old"] == 45
    assert "ישנים מאוד" in report["freshness"]["warning_he"]
    assert report["overall_confidence"] == health_quality.CONFIDENCE_MEDIUM


# ---------------------------------------------------------------------------
# Stale-window: analyze the file's LAST period, anchored to dataset_end_date
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_file_with_enough_weeks_still_gives_frequency() -> None:
    """A ZIP ending weeks ago but holding 3 full training weeks up to its end
    date must NOT report "not enough weeks" — it yields a real estimate."""
    anchor = dt.datetime.now(TZ).date() - dt.timedelta(days=30)
    wear: list[dict[str, Any]] = []
    workouts: list[dict[str, Any]] = []
    for monday in _complete_weeks_before(anchor, 3):
        w, k = _week_fixture(monday, worn_days=7, workout_offsets=(0, 2, 4))
        wear += w
        workouts += k

    report = await health_quality.build_health_quality_report(
        QualityDB(wear=wear, workouts=workouts, newest=_newest_iso(30)), 1, TZ
    )
    wq = report["workout_frequency"]
    assert wq["policy"] != routine.POLICY_INSUFFICIENT
    assert wq["frequency"] == 3.0
    assert "אין מספיק" not in (wq["warning_he"] or "")


@pytest.mark.asyncio
async def test_analysis_anchor_is_dataset_end_not_today() -> None:
    """analyze_training_weeks anchors its week window to the given end date:
    3 full weeks placed just before an old anchor read as 3 valid weeks, and
    the analyzed weeks end at (or before) that anchor — not near today."""
    anchor = dt.datetime.now(TZ).date() - dt.timedelta(days=60)
    wear: list[dict[str, Any]] = []
    workouts: list[dict[str, Any]] = []
    for monday in _complete_weeks_before(anchor, 3):
        w, k = _week_fixture(monday, worn_days=7, workout_offsets=(0, 2, 4))
        wear += w
        workouts += k
    db = QualityDB(wear=wear, workouts=workouts, newest=_newest_iso(60))

    anchored = await routine.analyze_training_weeks(db, 1, TZ, anchor=anchor)
    assert anchored is not None
    assert anchored.valid_weeks_strict == 3
    # Every analyzed week ends on/before the dataset anchor, never near today.
    assert all(week.end <= anchor for week in anchored.weeks)
    assert max(week.end for week in anchored.weeks) < dt.datetime.now(TZ).date() - dt.timedelta(days=30)


@pytest.mark.asyncio
async def test_newest_health_sample_date_reads_dataset_end(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    end = dt.datetime(2026, 6, 16, 18, 0, tzinfo=TZ)
    await db.execute(
        "INSERT INTO health(user_id, external_id, sample_type, value, unit, start_time, end_time, source_device, created_at) "
        "VALUES(1, 'w1', 'workout', 45, 'min', ?, ?, 'apple_watch', ?)",
        (end.isoformat(), end.isoformat(), utc_now()),
    )
    anchor = await routine.newest_health_sample_date(db, 1, TZ)
    assert anchor == dt.date(2026, 6, 16)


# ---------------------------------------------------------------------------
# Wizard integration (real sqlite Database)
# ---------------------------------------------------------------------------


class FakeMessage:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.markups: list[Any] = []

    async def reply_text(self, text: str, reply_markup: Any = None, parse_mode: str | None = None) -> None:
        del parse_mode
        self.texts.append(text)
        self.markups.append(reply_markup)


class FakeTarget:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.reply_markups: list[Any] = []
        self.message = FakeMessage()

    async def edit_message_text(self, text: str, reply_markup: Any = None, parse_mode: str | None = None) -> None:
        del parse_mode
        self.messages.append(text)
        self.reply_markups.append(reply_markup)

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        del text, show_alert


async def _make_db(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "re13.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'Test',NULL,?)",
        (utc_now(),),
    )
    return db


def _patch_db(monkeypatch: pytest.MonkeyPatch, db: Database) -> None:
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(health_jobs, "DB", db)
    monkeypatch.setattr(onboarding_bot, "DB", db)
    monkeypatch.setattr(core_services, "DB", db)
    monkeypatch.setattr(callback_menu_bot, "DB", db)


async def _insert_health(
    db: Database,
    sample_type: str,
    start: dt.datetime,
    end: dt.datetime | None = None,
    value: float = 0.0,
) -> None:
    await db.execute(
        "INSERT INTO health(user_id, external_id, sample_type, value, unit, "
        "start_time, end_time, source_device, created_at) "
        "VALUES(1, ?, ?, ?, 'u', ?, ?, 'apple_watch', ?)",
        (
            f"{sample_type}:{start.isoformat()}",
            sample_type,
            value,
            start.isoformat(),
            end.isoformat() if end else None,
            utc_now(),
        ),
    )


async def _insert_step_with_sources(
    db: Database,
    day: dt.date,
    sources: dict[str, float],
) -> None:
    selected_source = max(sources, key=sources.get)
    selected = sources[selected_source]
    source_device = json.dumps(
        {
            "selected_source": selected_source,
            "source_totals": sources,
            "raw_all_sources": sum(sources.values()),
            "conservative": selected,
            "selection_reason": "dominant_stepcount_source_for_day",
        },
        ensure_ascii=False,
    )
    start = dt.datetime.combine(day, dt.time(0, 0), tzinfo=TZ)
    await db.execute(
        "INSERT INTO health(user_id, external_id, sample_type, value, unit, "
        "start_time, end_time, source_device, created_at) "
        "VALUES(1, ?, 'steps', ?, 'count', ?, NULL, ?, ?)",
        (
            f"steps:{day.isoformat()}",
            selected,
            start.isoformat(),
            source_device,
            utc_now(),
        ),
    )


async def _seed_wear_day(db: Database, day: dt.date, first: float = 8.0, last: float = 22.0) -> None:
    start = dt.datetime.combine(day, dt.time(int(first), 0), tzinfo=TZ)
    end = dt.datetime.combine(day, dt.time(int(last), 0), tzinfo=TZ)
    await _insert_health(db, "watch_wear", start, end, value=last - first)


def _buttons(markup: Any) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


@pytest.mark.asyncio
async def test_steps_prompt_counts_all_days_and_shows_both_averages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Steps come from the iPhone too — a low-wear day is counted in the
    baseline, not deleted. When the all-days and full-wear averages disagree,
    the prompt shows both instead of silently collapsing to the worn days."""
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    today = dt.datetime.now(TZ).date()
    for offset, steps, full in ((2, 9000, True), (3, 9400, True), (4, 2500, False)):
        day = today - dt.timedelta(days=offset)
        await _seed_wear_day(db, day, last=22.0 if full else 14.0)
        await _insert_health(
            db, "steps", dt.datetime.combine(day, dt.time(0, 0), tzinfo=TZ), value=steps
        )
    await user_model.set_fact(
        db, 1, "avg_steps", 6967,
        kind=user_model.KIND_FACT, source=user_model.SOURCE_APPLE_HEALTH, confirmed=False,
    )

    target = FakeTarget()
    started = await health_jobs.ask_next_health_confirm_step(target, 1)
    assert started is True
    prompt = target.messages[-1]
    # All 3 step days feed the baseline (not just the 2 fully-worn days).
    assert "נמצאו נתוני צעדים עבור 3 ימים" in prompt
    assert "6,967" in prompt
    assert "החישוב השמרני" in prompt
    buttons = _buttons(target.reply_markups[-1])
    assert "health:confirm:avg_steps" in buttons
    assert "health:steps_breakdown" in buttons
    assert "נשרף" not in prompt                    # no aggressive dev jargon


@pytest.mark.asyncio
async def test_steps_breakdown_button_shows_reproducible_daily_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    day = dt.datetime.now(TZ).date() - dt.timedelta(days=2)
    await _insert_step_with_sources(db, day, {"iPhone": 7000, "Apple Watch": 2000})
    await _seed_wear_day(db, day)
    await user_model.set_fact(
        db, 1, "avg_steps", 7000,
        kind=user_model.KIND_FACT, source=user_model.SOURCE_APPLE_HEALTH, confirmed=False,
    )

    target = FakeTarget()
    handled = await callback_menu_bot.handle_menu_callback(target, 1, "health:steps_breakdown")

    assert handled is True
    prompt = target.messages[-1]
    assert "פירוט יומי לצעדים" in prompt
    assert day.isoformat() in prompt
    assert "9,000" in prompt
    assert "7,000" in prompt
    assert "selected_planning_baseline" in prompt


@pytest.mark.asyncio
async def test_steps_numeric_free_text_updates_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await user_model.set_fact(
        db, 1, "avg_steps", 4800,
        kind=user_model.KIND_FACT, source=user_model.SOURCE_APPLE_HEALTH, confirmed=False,
    )

    ok, reply = await health_jobs.apply_health_wizard_text_edit(1, "avg_steps", "7000")

    assert ok is True
    assert "7,000" in reply
    fact = await user_model.get_fact(db, 1, "avg_steps")
    assert fact["value"] == 7000
    assert fact["source"] == user_model.SOURCE_USER
    assert fact["confirmed"] is True


@pytest.mark.asyncio
async def test_steps_skip_for_now_defers_only_step_question(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await user_model.set_fact(
        db, 1, "avg_steps", 7000,
        kind=user_model.KIND_FACT, source=user_model.SOURCE_APPLE_HEALTH, confirmed=False,
    )
    await user_model.set_fact(
        db, 1, "weight_kg", 101.8,
        kind=user_model.KIND_FACT, source=user_model.SOURCE_APPLE_HEALTH, confirmed=False,
    )

    target = FakeTarget()
    await onboarding_bot.set_flow_state(1, health_jobs.HEALTH_CONFIRM_FLOW, "avg_steps", {"done": ["weight_kg"]})
    await callback_menu_bot.handle_menu_callback(target, 1, "health:skip_item")

    assert onboarding_bot.PENDING_QUESTION[1] == "__health_edit_avg_steps__"
    fact = await user_model.get_fact(db, 1, "avg_steps")
    assert fact["confirmed"] is False


@pytest.mark.asyncio
async def test_steps_local_calendar_day_used_for_late_utc_sample() -> None:
    utc_sample = dt.datetime(2026, 6, 1, 21, 30, tzinfo=dt.timezone.utc)
    db = QualityDB(
        steps=[{"value": 5000, "start_time": utc_sample.isoformat()}],
        newest=dt.datetime(2026, 6, 2, 22, 0, tzinfo=TZ).isoformat(),
    )

    result = await routine.average_daily_steps(db, 1, TZ, window_days=2)

    included = [row for row in result.daily_breakdown if row["included"]]
    assert included[0]["date"] == "2026-06-02"


@pytest.mark.asyncio
async def test_insufficient_frequency_asks_user_directly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    # Only ONE fully-worn week — not enough for strict or relaxed.
    monday = _last_complete_weeks(1)[0]
    for i in range(7):
        await _seed_wear_day(db, monday + dt.timedelta(days=i))
    await user_model.set_fact(
        db, 1, "workout_pattern", {"weekly_frequency": 2.0, "typical_hour": "18:30"},
        kind=user_model.KIND_ESTIMATE, source=user_model.SOURCE_DERIVED, confirmed=False,
    )

    target = FakeTarget()
    await health_jobs.ask_next_health_confirm_step(target, 1)
    prompt = target.messages[-1]
    assert "אין מספיק שבועות" in prompt
    assert "כמה אימונים בשבוע" in prompt
    assert "בחר או כתוב" not in prompt
    buttons = _buttons(target.reply_markups[-1])
    assert "health:confirm:workout_pattern:trend:3" not in buttons
    assert "health:confirm:workout_pattern.frequency" not in buttons
    assert buttons == ["health:skip_item", "health:skip_wizard"]

    # Legacy numeric callbacks remain supported even though the weak-data prompt
    # no longer exposes 2/3/4 suggestion buttons.
    handled = await callback_menu_bot.handle_menu_callback(
        target, 1, "health:confirm:workout_pattern:trend:3"
    )
    assert handled is True
    training_days = await user_model.get_fact(db, 1, "training_days_per_week")
    assert training_days["value"] == 3.0
    assert training_days["confirmed"] is True


@pytest.mark.asyncio
async def test_relaxed_frequency_prompt_and_normalized_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    mondays = _last_complete_weeks(4)
    for i in range(7):
        await _seed_wear_day(db, mondays[0] + dt.timedelta(days=i))
    for offset in (0, 2, 4):
        await _insert_health(
            db, "workout",
            dt.datetime.combine(mondays[0] + dt.timedelta(days=offset), dt.time(18, 0), tzinfo=TZ),
            value=45,
        )
    for monday in mondays[1:]:
        for i in range(5):
            await _seed_wear_day(db, monday + dt.timedelta(days=i))
        for offset in (0, 2):
            await _insert_health(
                db, "workout",
                dt.datetime.combine(monday + dt.timedelta(days=offset), dt.time(18, 0), tzinfo=TZ),
                value=45,
            )
    await user_model.set_fact(
        db, 1, "workout_pattern", {"weekly_frequency": 2.0, "typical_hour": "18:30"},
        kind=user_model.KIND_ESTIMATE, source=user_model.SOURCE_DERIVED, confirmed=False,
    )

    target = FakeTarget()
    await health_jobs.ask_next_health_confirm_step(target, 1)
    prompt = target.messages[-1]
    assert "אישור נתונים מהייבוא" in prompt
    assert "שגרת האימונים שלך נראית סביב 3 אימונים בשבוע" in prompt
    assert "רשום את כמות האימונים השבועית" in prompt
    buttons = _buttons(target.reply_markups[-1])
    trend_buttons = [b for b in buttons if ":trend:" in b]
    assert trend_buttons, buttons
    assert "health:skip_item" in buttons
    assert "health:skip_wizard" in buttons
    assert not any(b and b.startswith("health:edit:") for b in buttons)

    handled = await callback_menu_bot.handle_menu_callback(target, 1, trend_buttons[0])
    assert handled is True
    training_days = await user_model.get_fact(db, 1, "training_days_per_week")
    assert training_days["value"] == 3.0  # 2.85 normalized → conservative 3
    assert training_days["confirmed"] is True


@pytest.mark.asyncio
async def test_stale_relaxed_frequency_prompt_is_short_and_has_no_manual_button(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await user_model.set_fact(
        db, 1, "workout_pattern", {"weekly_frequency": 2.0, "typical_hour": "18:30"},
        kind=user_model.KIND_ESTIMATE, source=user_model.SOURCE_DERIVED, confirmed=False,
    )

    async def fake_quality(user_id: int) -> dict[str, Any]:
        del user_id
        return {
            "freshness": {"is_stale": True, "latest_sample_date": "2026-06-16"},
            "workout_frequency": {"policy": routine.POLICY_RELAXED, "frequency": 2.0},
        }

    monkeypatch.setattr(health_jobs, "_wizard_quality_report", fake_quality)

    target = FakeTarget()
    await health_jobs.ask_next_health_confirm_step(target, 1)
    prompt = target.messages[-1]
    assert prompt.count("2026-06-16") == 1
    assert "מומלץ לייצא ZIP חדש מהאייפון" in prompt
    assert "שגרת האימונים שלך נראית סביב 2 אימונים בשבוע" in prompt
    assert "ימי לבישת שעון" not in prompt
    assert "שבועות מלאים" not in prompt

    buttons = _buttons(target.reply_markups[-1])
    assert "health:confirm:workout_pattern:trend:2" in buttons
    assert "health:skip_item" in buttons
    assert "health:skip_wizard" in buttons
    assert not any(b and b.startswith("health:edit:") for b in buttons)


@pytest.mark.asyncio
async def test_two_night_sleep_is_not_offered_for_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await user_model.set_fact(
        db, 1, "sleep_schedule",
        {"typical_bedtime": "00:19", "typical_wake_time": "06:14", "nights_sampled": 2},
        kind=user_model.KIND_ESTIMATE, source=user_model.SOURCE_DERIVED, confirmed=False,
    )

    target = FakeTarget()
    started = await health_jobs.ask_next_health_confirm_step(target, 1)
    assert started is True
    prompt = target.messages[-1]
    assert "לא זיהיתי דפוס שינה מספיק ברור" in prompt
    assert "2 לילות" not in prompt
    buttons = _buttons(target.reply_markups[-1])
    assert "health:confirm:sleep_schedule" not in buttons
    assert "health:skip_item" in buttons

    # "השאר ריק והמשך" advances the wizard without confirming the schedule.
    handled = await callback_menu_bot.handle_menu_callback(target, 1, "health:skip_item")
    assert handled is True


@pytest.mark.asyncio
async def test_seven_night_sleep_confirmable_with_reliability_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await user_model.set_fact(
        db, 1, "sleep_schedule",
        {"typical_bedtime": "23:10", "typical_wake_time": "06:40", "nights_sampled": 7},
        kind=user_model.KIND_ESTIMATE, source=user_model.SOURCE_DERIVED, confirmed=False,
    )

    target = FakeTarget()
    await health_jobs.ask_next_health_confirm_step(target, 1)
    prompt = target.messages[-1]
    assert "23:10" in prompt
    assert "היסטוריית השינה" in prompt
    assert "7 לילות" not in prompt
    assert "נתתי משקל גבוה יותר" in prompt
    buttons = _buttons(target.reply_markups[-1])
    assert "health:confirm:sleep_schedule" in buttons


@pytest.mark.asyncio
async def test_stale_export_warning_shown_on_first_wizard_screen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    old_day = dt.datetime.now(TZ) - dt.timedelta(days=21)
    await _insert_health(db, "steps", old_day, value=8000)
    await user_model.set_fact(
        db, 1, "weight_kg", 98.8,
        kind=user_model.KIND_FACT, source=user_model.SOURCE_APPLE_HEALTH, confirmed=False,
    )

    target = FakeTarget()
    await health_jobs.ask_next_health_confirm_step(target, 1)
    prompt = target.messages[-1]
    assert "⚠️" in prompt
    assert "לייצא ZIP חדש" in prompt
    assert "98.8" in prompt  # the actual step still shows its value


@pytest.mark.asyncio
async def test_final_wizard_summary_uses_confirmed_planning_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await user_model.set_fact(
        db, 1, "workout_pattern",
        {"weekly_frequency": 2.0, "typical_hour": "18:47"},
        kind=user_model.KIND_ESTIMATE, source=user_model.SOURCE_DERIVED,
        confirmed=False,
    )
    await user_model.set_fact(
        db, 1, "training_days_per_week", 4,
        kind=user_model.KIND_FACT, source=user_model.SOURCE_USER, confirmed=True,
    )
    await user_model.set_fact(
        db, 1, "weekly_availability",
        [
            {"weekday": 6, "available": True},
            {"weekday": 0, "available": True},
            {"weekday": 2, "available": True},
            {"weekday": 4, "available": True},
        ],
        kind=user_model.KIND_FACT, source=user_model.SOURCE_USER, confirmed=True,
    )
    await user_model.set_fact(
        db, 1, "weight_kg", 101.8,
        kind=user_model.KIND_FACT, source=user_model.SOURCE_APPLE_HEALTH,
        confirmed=True,
    )
    await user_model.set_fact(
        db, 1, "avg_steps", 8000,
        kind=user_model.KIND_FACT, source=user_model.SOURCE_USER, confirmed=True,
    )
    await user_model.set_fact(
        db, 1, "sleep_schedule",
        {"typical_bedtime": "00:19", "typical_wake_time": "06:14", "nights_sampled": 2},
        kind=user_model.KIND_ESTIMATE, source=user_model.SOURCE_DERIVED,
        confirmed=False,
    )
    await core_services.set_flow_state(
        1,
        health_jobs.HEALTH_POST_WIZARD_FLOW,
        "reconciliation",
        {
            "summary_text": (
                "<b>ייבוא Apple Health הושלם ✅</b>\n"
                "⚠️ הקובץ יובא בהצלחה, אבל הנתונים אינם טריים: "
                "הרשומה האחרונה היא מ-2026-06-16."
            )
        },
    )

    target = FakeTarget()
    await health_jobs.finish_health_confirm_wizard(target, 1)
    final = target.messages[-1]
    assert final.count("2026-06-16") == 1
    assert "נתוני תכנון שאושרו עד עכשיו" in final
    assert "4 בשבוע" in final
    assert "2 בשבוע" not in final
    assert "ראשון" in final
    assert "רביעי" in final
    assert "101.8 ק\"ג" in final
    assert "8,000 צעדים" in final
    assert "שינה: עדיין לא אושרה" in final
    assert "00:19" not in final
    assert "06:14" not in final
    assert "סיכום איכות הנתונים" not in final


def test_import_summary_labels_sleep_count_as_records_not_confirmed_nights() -> None:
    summary = health_import.ImportSummary(
        rows=4154,
        min_date="2025-01-03",
        max_date="2026-06-16",
        workouts=296,
        sleep_sessions=136,
        weight_records=340,
        activity_records=1660,
    )
    outcome = health_jobs.HealthImportOutcome(
        inserted=4154,
        duplicates=0,
        updated=0,
        invalid=0,
        summary=summary,
        profile={
            "sleep": {"typical_bedtime": "00:19", "typical_wake_time": "06:14"},
            "workout": {"weekly_frequency": 2.0},
        },
        source_file="HealthKit.zip",
        total_stored=4154,
        import_started=utc_now(),
        import_completed=utc_now(),
    )

    text = health_jobs._health_import_success_text(outcome)

    assert "רשומות שינה מנורמלות: 136" in text
    assert "לילות שינה" not in text
    assert "00:19" not in text
    assert "2.0 אימונים" not in text


@pytest.mark.asyncio
async def test_quality_summary_can_skip_export_recommendation() -> None:
    """When the surrounding screen already tells the user to export a fresh
    ZIP, the quality digest must not repeat the same advice in the same
    message (Codex audit follow-up)."""
    report = await health_quality.build_health_quality_report(
        QualityDB(newest=_newest_iso(21)), 1, TZ
    )
    with_recommendation = health_quality.quality_summary_lines_he(report)
    without_recommendation = health_quality.quality_summary_lines_he(
        report, include_export_recommendation=False
    )
    assert any("המלצה" in line for line in with_recommendation)
    assert not any("המלצה" in line for line in without_recommendation)

@pytest.mark.asyncio
async def test_insufficient_frequency_does_not_reask_when_manual_training_days_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    # Only ONE fully-worn week — Health alone is insufficient.
    monday = _last_complete_weeks(1)[0]
    for i in range(7):
        await _seed_wear_day(db, monday + dt.timedelta(days=i))
    await user_model.set_fact(
        db,
        1,
        "workout_pattern",
        {"weekly_frequency": 2.0, "typical_hour": "18:30"},
        kind=user_model.KIND_ESTIMATE,
        source=user_model.SOURCE_DERIVED,
        confirmed=False,
    )
    # The user already corrected the real plan days manually. The Health wizard
    # must not re-open the frequency question from the old/partial export.
    await user_model.set_fact(
        db,
        1,
        "active_training_days",
        [6, 0, 2, 4],
        kind=user_model.KIND_FACT,
        source=user_model.SOURCE_USER,
        confirmed=True,
    )
    await user_model.set_fact(
        db,
        1,
        "training_days_per_week",
        4,
        kind=user_model.KIND_FACT,
        source=user_model.SOURCE_USER,
        confirmed=True,
    )

    target = FakeTarget()
    await health_jobs.ask_next_health_confirm_step(target, 1)
    prompt = target.messages[-1]
    assert "כמה אימונים בשבוע" not in prompt
    assert "אין מספיק שבועות" not in prompt
    assert "18:30" in prompt


@pytest.mark.asyncio
async def test_health_wizard_does_not_offer_detected_days_over_manual_active_days(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await user_model.set_fact(
        db,
        1,
        "workout_pattern",
        {
            "weekly_frequency": 2.0,
            "common_weekdays": [0, 2],
            "weekday_schema": "python_weekday_v1_mon0",
        },
        kind=user_model.KIND_ESTIMATE,
        source=user_model.SOURCE_DERIVED,
        confirmed=False,
    )
    await user_model.set_fact(
        db,
        1,
        "active_training_days",
        [6, 0, 2, 4],
        kind=user_model.KIND_FACT,
        source=user_model.SOURCE_USER,
        confirmed=True,
    )

    target = FakeTarget()
    has_prompt = await health_jobs.ask_next_health_confirm_step(target, 1)
    assert has_prompt is False
