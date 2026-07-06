"""RE12 regression tests — watch-wear awareness + per-item import wizard.

Covers:
  * The importer emits one ``watch_wear`` row per local day with any
    watch-sourced sample (raw heart rate counts; iPhone-only days do not).
  * learn_workout_pattern burns complete weeks that contain a day the watch
    was not worn — weekly frequency is averaged only over fully-worn weeks.
  * average_daily_steps counts only days the watch covered morning-to-evening;
    partial and unworn days are burned.
  * The confirmation wizard shows the actual VALUE for every item (steps
    average, body fat...) — never a bare label — and echoes what was approved.
"""

from __future__ import annotations

import datetime as dt
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
from noam_coach.services import health_jobs

TZ = ZoneInfo("Asia/Jerusalem")


# ---------------------------------------------------------------------------
# Importer — watch_wear rows
# ---------------------------------------------------------------------------


def test_importer_emits_watch_wear_only_for_watch_days(tmp_path: Path) -> None:
    xml = tmp_path / "export.xml"
    xml.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<HealthData>
 <Record type="HKQuantityTypeIdentifierStepCount" sourceName="Noam&#8217;s Apple Watch"
         startDate="2026-07-01 08:00:00 +0300" endDate="2026-07-01 08:10:00 +0300" value="500"/>
 <Record type="HKQuantityTypeIdentifierHeartRate" sourceName="Noam&#8217;s Apple Watch"
         startDate="2026-07-01 21:30:00 +0300" endDate="2026-07-01 21:30:00 +0300" value="70"/>
 <Record type="HKQuantityTypeIdentifierStepCount" sourceName="iPhone"
         startDate="2026-07-02 09:00:00 +0300" endDate="2026-07-02 09:10:00 +0300" value="700"/>
</HealthData>
""",
        encoding="utf-8",
    )
    now = dt.datetime(2026, 7, 6, tzinfo=dt.timezone.utc)
    rows = list(health_import.iter_health_rows(xml, local_tz=TZ, now=now))
    wear = [r for r in rows if r.sample_type == "watch_wear"]
    assert len(wear) == 1
    assert wear[0].external_id == "ah:watch_wear:2026-07-01"
    # Span from the first (08:00) to the last (21:30) watch sample: 13.5h.
    assert wear[0].value == pytest.approx(13.5, abs=0.01)
    # The iPhone-only day (07-02) has no wear row, but its steps still import.
    steps_days = {r.external_id for r in rows if r.sample_type == "steps"}
    assert "ah:steps:2026-07-02" in steps_days


# ---------------------------------------------------------------------------
# Weekly frequency — weeks with an unworn day are burned
# ---------------------------------------------------------------------------


class RoutingDB:
    """Mock DB that answers each routine query with the right row set."""

    def __init__(
        self,
        workouts: list[dict[str, Any]] | None = None,
        wear: list[dict[str, Any]] | None = None,
        steps: list[dict[str, Any]] | None = None,
    ) -> None:
        self.workouts = workouts or []
        self.wear = wear or []
        self.steps = steps or []

    async def fetch_all(self, sql: str, parameters: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        if "watch_wear" in sql:
            return self.wear
        if "sample_type='workout'" in sql:
            return self.workouts
        if "sample_type='steps'" in sql:
            return self.steps
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


@pytest.mark.asyncio
async def test_weeks_with_unworn_days_are_burned() -> None:
    w1_monday, w2_monday = _last_complete_weeks(2)

    wear_rows = [_wear_row(w1_monday + dt.timedelta(days=i)) for i in range(7)]
    # Week 2: the watch was off on Wednesday — the whole week must burn.
    wear_rows += [
        _wear_row(w2_monday + dt.timedelta(days=i)) for i in range(7) if i != 2
    ]
    workouts = [
        _workout_row(w1_monday),                          # Mon
        _workout_row(w1_monday + dt.timedelta(days=2)),   # Wed
        _workout_row(w1_monday + dt.timedelta(days=4)),   # Fri
        _workout_row(w2_monday),                          # Mon (burned week)
        _workout_row(w2_monday + dt.timedelta(days=3)),   # Thu (burned week)
    ]
    db = RoutingDB(workouts=workouts, wear=wear_rows)
    pattern = await routine.learn_workout_pattern(db, 1, TZ)

    assert pattern.wear_filtered is True
    assert pattern.valid_weeks_sampled == 1  # only the fully-worn week counts
    assert pattern.weekly_frequency == 3.0   # 3 workouts in the valid week
    assert pattern.sessions_sampled == 5     # raw sample count is untouched


@pytest.mark.asyncio
async def test_no_wear_evidence_falls_back_to_naive_average() -> None:
    w1_monday, _w2 = _last_complete_weeks(2)
    workouts = [_workout_row(w1_monday + dt.timedelta(days=i)) for i in (0, 2, 4)]
    db = RoutingDB(workouts=workouts, wear=[])
    pattern = await routine.learn_workout_pattern(db, 1, TZ)
    assert pattern.wear_filtered is False
    assert pattern.valid_weeks_sampled == 0
    assert pattern.weekly_frequency == pytest.approx(3 / (45 / 7), abs=0.06)


# ---------------------------------------------------------------------------
# Steps average — only full-coverage wear days count
# ---------------------------------------------------------------------------


def _steps_row(day: dt.date, steps: float) -> dict[str, Any]:
    start = dt.datetime.combine(day, dt.time(0, 0), tzinfo=TZ)
    return {"value": steps, "start_time": start.isoformat()}


@pytest.mark.asyncio
async def test_steps_average_burns_partial_and_unworn_days() -> None:
    today = dt.datetime.now(TZ).date()
    full_day = today - dt.timedelta(days=3)      # worn 08:00-22:00 → counts
    partial_day = today - dt.timedelta(days=4)   # taken off at 15:00 → burned
    unworn_day = today - dt.timedelta(days=5)    # no wear row → burned

    steps = [
        _steps_row(full_day, 10000),
        _steps_row(partial_day, 4000),
        _steps_row(unworn_day, 300),
    ]
    wear = [
        _wear_row(full_day, 8.0, 22.0),
        _wear_row(partial_day, 8.0, 15.0),
    ]
    db = RoutingDB(steps=steps, wear=wear)
    result = await routine.average_daily_steps(db, 1, TZ)
    assert result.wear_filtered is True
    assert result.days_sampled == 1
    assert result.days_excluded == 2
    assert result.avg == pytest.approx(10000)


@pytest.mark.asyncio
async def test_steps_average_without_wear_data_uses_all_days() -> None:
    today = dt.datetime.now(TZ).date()
    steps = [
        _steps_row(today - dt.timedelta(days=3), 8000),
        _steps_row(today - dt.timedelta(days=4), 6000),
    ]
    db = RoutingDB(steps=steps, wear=[])
    result = await routine.average_daily_steps(db, 1, TZ)
    assert result.wear_filtered is False
    assert result.days_sampled == 2
    assert result.avg == pytest.approx(7000)


# ---------------------------------------------------------------------------
# Wizard — every prompt shows the detected value, every approval is echoed
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
    db = Database(str(tmp_path / "re12_wizard.db"))
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


@pytest.mark.asyncio
async def test_steps_prompt_shows_the_detected_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await user_model.set_fact(
        db, 1, "avg_steps", 9200,
        kind=user_model.KIND_FACT, source=user_model.SOURCE_APPLE_HEALTH, confirmed=False,
    )

    target = FakeTarget()
    started = await health_jobs.ask_next_health_confirm_step(target, 1)
    assert started is True
    prompt = target.messages[-1]
    assert "9,200" in prompt          # the value, not just the label
    assert "צעדים" in prompt
    assert "האם לאשר" in prompt       # uniform approval phrasing
    assert "אם לא" in prompt          # ...with an explicit correction hint


@pytest.mark.asyncio
async def test_body_fat_prompt_and_ack_show_the_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await user_model.set_fact(
        db, 1, "body_fat_pct", 23.5,
        kind=user_model.KIND_FACT, source=user_model.SOURCE_APPLE_HEALTH, confirmed=False,
    )
    await core_services.set_flow_state(
        1, health_jobs.HEALTH_POST_WIZARD_FLOW, "reconciliation", {"summary_text": "<b>סיכום</b>"}
    )

    target = FakeTarget()
    await health_jobs.ask_next_health_confirm_step(target, 1)
    assert "23.5" in target.messages[-1]

    handled = await callback_menu_bot.handle_menu_callback(
        target, 1, "health:confirm:body_fat_pct"
    )
    assert handled is True
    fact = await user_model.get_fact(db, 1, "body_fat_pct")
    assert fact["confirmed"] is True
    # The final screen echoes what was approved, including the value.
    assert "אושר" in target.messages[-1]
    assert "23.5" in target.messages[-1]
