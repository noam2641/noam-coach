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
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

import coach_bot
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


@pytest.mark.asyncio
async def test_very_stale_export_caps_overall_confidence() -> None:
    # Workout data alone would be HIGH (3 full weeks), but a 45-day-old
    # export caps the overall confidence at medium with a strong warning.
    wear: list[dict[str, Any]] = []
    workouts: list[dict[str, Any]] = []
    for monday in _last_complete_weeks(3):
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


async def _seed_wear_day(db: Database, day: dt.date, first: float = 8.0, last: float = 22.0) -> None:
    start = dt.datetime.combine(day, dt.time(int(first), 0), tzinfo=TZ)
    end = dt.datetime.combine(day, dt.time(int(last), 0), tzinfo=TZ)
    await _insert_health(db, "watch_wear", start, end, value=last - first)


def _buttons(markup: Any) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


@pytest.mark.asyncio
async def test_steps_prompt_reports_counted_and_excluded_days(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
        db, 1, "avg_steps", 9200,
        kind=user_model.KIND_FACT, source=user_model.SOURCE_APPLE_HEALTH, confirmed=False,
    )

    target = FakeTarget()
    started = await health_jobs.ask_next_health_confirm_step(target, 1)
    assert started is True
    prompt = target.messages[-1]
    assert "9,200" in prompt                       # the value itself
    assert "2 ימים" in prompt                      # counted days
    assert "לא נספרו" in prompt and "1" in prompt  # excluded days, soft phrasing
    assert "נשרף" not in prompt                    # no aggressive dev jargon


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
    buttons = _buttons(target.reply_markups[-1])
    assert "health:confirm:workout_pattern:trend:3" in buttons
    assert "health:confirm:workout_pattern.frequency" not in buttons

    # Choosing a number applies it and the wizard advances (hour step next).
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
    assert "הערכה" in prompt
    assert "לפחות 5 ימי" in prompt  # explains the relaxed basis
    buttons = _buttons(target.reply_markups[-1])
    trend_buttons = [b for b in buttons if ":trend:" in b]
    assert trend_buttons, buttons

    handled = await callback_menu_bot.handle_menu_callback(target, 1, trend_buttons[0])
    assert handled is True
    training_days = await user_model.get_fact(db, 1, "training_days_per_week")
    assert training_days["value"] == 3.0  # 2.85 normalized → conservative 3
    assert training_days["confirmed"] is True


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
    assert "לא מספיק כדי לקבוע שגרת שינה" in prompt
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
    assert "7 לילות" in prompt
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
async def test_quality_summary_appears_after_wizard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    today = dt.datetime.now(TZ).date()
    await _seed_wear_day(db, today - dt.timedelta(days=2))
    await _insert_health(
        db, "steps",
        dt.datetime.combine(today - dt.timedelta(days=2), dt.time(0, 0), tzinfo=TZ),
        value=9000,
    )
    await core_services.set_flow_state(
        1, health_jobs.HEALTH_POST_WIZARD_FLOW, "reconciliation", {"summary_text": "<b>סיכום</b>"}
    )

    target = FakeTarget()
    await health_jobs.finish_health_confirm_wizard(target, 1)
    final = target.messages[-1]
    assert "סיכום איכות הנתונים" in final
    assert "צעדים" in final


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
