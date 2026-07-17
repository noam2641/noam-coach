"""TASK-60 — per-weekday workout-time averages and outlier-day approval.

Required trace: history with distinct Tuesday and Friday times → weekday
estimates built → default derived from the MEAN OF DAY MEANS → outlier
weekday approval rendered → confirmed values persisted to the canonical
weekly_availability slot.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest

import coach_bot
import event_log
import routine
import user_model
from db import Database
from helpers import utc_now
from noam_coach.observability import ObservabilityMode, set_mode
from noam_coach.observability.emit import reset_observability_health
from noam_coach.observability.modes import reset_mode
from noam_coach.services.workout_hours import (
    clock_distance_minutes,
    install_workout_hour_evidence,
    outlier_days,
    uninstall_workout_hour_evidence,
    weekday_hour_evidence,
)

USER_ID = 1
IL = ZoneInfo("Asia/Jerusalem")


@pytest.fixture
async def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Database:
    database = Database(str(tmp_path / "task60.db"))
    await database.init()
    await database.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(?, 'Test', NULL, ?)",
        (USER_ID, utc_now()),
    )
    monkeypatch.setattr(coach_bot, "DB", database)
    # confirm_health_wizard_step and friends are NOT runtime_bound — they
    # read health_jobs.DB directly.
    from noam_coach.services import health_jobs as health_jobs_module

    monkeypatch.setattr(health_jobs_module, "DB", database, raising=False)
    from config import SETTINGS

    monkeypatch.setattr(SETTINGS, "telegram_allowed_user_id", USER_ID, raising=False)
    return database


@pytest.fixture(autouse=True)
def _isolation():
    set_mode(ObservabilityMode.CONTENT)
    reset_observability_health()
    yield
    uninstall_workout_hour_evidence()
    reset_mode()
    reset_observability_health()


async def _workout(db: Database, when: dt.datetime, minutes: float = 60) -> None:
    await db.execute(
        """
        INSERT INTO health(user_id, external_id, sample_type, value, unit, start_time, end_time, created_at)
        VALUES(?, ?, 'workout', ?, 'min', ?, ?, ?)
        """,
        (
            USER_ID, f"w-{when.isoformat()}-{__import__('secrets').token_hex(3)}", minutes,
            when.astimezone(dt.timezone.utc).isoformat(),
            (when + dt.timedelta(minutes=minutes)).astimezone(dt.timezone.utc).isoformat(),
            utc_now(),
        ),
    )


async def _seed_history(db: Database) -> None:
    """Three weeks: Sunday ~19:00, Tuesday ~19:15, Friday ~10:00 (the spec's
    exact shape). Weekday indices are Monday-first: Sun=6, Tue=1, Fri=4."""
    base = dt.datetime.now(IL).replace(second=0, microsecond=0) - dt.timedelta(days=28)
    # Align base to a Monday.
    base -= dt.timedelta(days=base.weekday())
    for week in range(3):
        monday = base + dt.timedelta(days=7 * week)
        await _workout(db, (monday + dt.timedelta(days=6)).replace(hour=19, minute=0))   # Sunday
        await _workout(db, (monday + dt.timedelta(days=1)).replace(hour=19, minute=15))  # Tuesday
        await _workout(db, (monday + dt.timedelta(days=4)).replace(hour=10, minute=0))   # Friday
    # One lone anomalous Wednesday workout — sparse, must NOT become a pattern.
    await _workout(db, (base + dt.timedelta(days=2)).replace(hour=6, minute=0))


# ---------------------------------------------------------------------------
# Learning: per-day averages + mean-of-means default
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_per_weekday_averages_from_local_times(db: Database) -> None:
    await _seed_history(db)
    pattern = await routine.learn_workout_pattern(db, USER_ID, IL)

    evidence = weekday_hour_evidence(pattern.__dict__)
    assert evidence[6] == "19:00"   # Sunday
    assert evidence[1] == "19:15"   # Tuesday
    assert evidence[4] == "10:00"   # Friday


@pytest.mark.asyncio
async def test_default_is_mean_of_day_means_not_pooled_rows(db: Database) -> None:
    """Friday has one session per week like the others here, so pooled vs
    mean-of-means agree in shape — so ALSO overload Sunday with extra rows:
    a weekday with many records must not dominate the default."""
    await _seed_history(db)
    base = dt.datetime.now(IL).replace(second=0, microsecond=0) - dt.timedelta(days=28)
    base -= dt.timedelta(days=base.weekday())
    for week in range(3):  # extra Sunday sessions (double-count Sundays)
        monday = base + dt.timedelta(days=7 * week)
        await _workout(db, (monday + dt.timedelta(days=6)).replace(hour=19, minute=0))

    pattern = await routine.learn_workout_pattern(db, USER_ID, IL)
    # The default is the CIRCULAR mean of the three day-means — independent
    # of how many Sunday rows exist. A pooled mean over rows would be pulled
    # toward 19:00 by the six Sunday sessions.
    expected = routine.hour_to_hhmm(
        routine.circular_hour_mean([19.0, 19.25, 10.0])
    )
    assert pattern.typical_hour == expected
    pooled = routine.hour_to_hhmm(
        routine.circular_hour_mean([19.0] * 6 + [19.25] * 3 + [10.0] * 3)
    )
    assert clock_distance_minutes(pattern.typical_hour, pooled) > 45  # not row-dominated


@pytest.mark.asyncio
async def test_sparse_weekday_gets_no_day_average(db: Database) -> None:
    await _seed_history(db)
    pattern = await routine.learn_workout_pattern(db, USER_ID, IL)
    evidence = weekday_hour_evidence(pattern.__dict__)
    assert 2 not in evidence  # the lone Wednesday 06:00 is not a pattern


def test_midnight_adjacent_times_use_circular_distance() -> None:
    assert clock_distance_minutes("23:30", "00:30") == 60
    assert clock_distance_minutes("23:30", "12:00") == 690
    assert clock_distance_minutes("00:15", "23:45") == 30


def test_outlier_detection_matrix() -> None:
    value = {
        "typical_hour": "19:05",
        "weekday_hours": {"6": "19:00", "1": "19:15", "4": "10:00"},
        "weekday_hour_samples": {"6": 3, "1": 3, "4": 3},
    }
    outliers = outlier_days(value, [6, 1, 4])
    assert outliers == [(4, "10:00")]  # Friday only
    # Days outside the approved set are not surfaced.
    assert outlier_days(value, [6, 1]) == []


# ---------------------------------------------------------------------------
# Wizard integration
# ---------------------------------------------------------------------------


def _pattern_value() -> dict[str, Any]:
    return {
        "typical_hour": "19:05",
        "weekday_hours": {"6": "19:00", "1": "19:15", "4": "10:00"},
        "weekday_hour_samples": {"6": 3, "1": 3, "4": 3},
        "weekly_frequency": 3.0,
        "common_weekdays": [6, 1, 4],
        "sessions_sampled": 9,
        "weekday_schema": "monday_first_v1",
    }


async def _seed_wizard_state(db: Database) -> None:
    await user_model.set_fact(
        db, USER_ID, "workout_pattern", _pattern_value(),
        kind=user_model.KIND_ESTIMATE, source=user_model.SOURCE_DERIVED,
    )
    await user_model.set_fact(
        db, USER_ID, "active_training_days", [6, 1, 4],
        kind=user_model.KIND_FACT, source=user_model.SOURCE_USER, confirmed=True,
    )
    await user_model.set_fact(
        db, USER_ID, "weekly_availability",
        [
            {"weekday": 6, "start": "19:05", "available": True},
            {"weekday": 1, "start": "19:05", "available": True},
            {"weekday": 4, "start": "19:05", "available": True},
        ],
        kind=user_model.KIND_FACT, source=user_model.SOURCE_USER, confirmed=True,
    )


@pytest.mark.asyncio
async def test_hour_step_prompt_shows_per_day_evidence(db: Database) -> None:
    from noam_coach.services import health_jobs

    await _seed_wizard_state(db)
    install_workout_hour_evidence()

    fact = await user_model.get_fact(db, USER_ID, "workout_pattern")
    detected, _scope, _hint = health_jobs._wizard_step_prompt(
        health_jobs.WIZARD_STEP_WORKOUT_HOUR, fact, None
    )
    assert "לפי היסטוריית האימונים" in detected
    assert "סביב 10:00" in detected
    assert "19:05" in detected  # the mean-of-means default
    assert "שונים" in detected  # the outlier note


@pytest.mark.asyncio
async def test_confirm_queues_outlier_and_renders_day_specific_approval(
    db: Database,
) -> None:
    """THE required trace: confirm default → Friday approval rendered →
    approving persists Friday's own start in weekly_availability."""
    from noam_coach.services import health_jobs

    await _seed_wizard_state(db)
    install_workout_hour_evidence()

    ack = await health_jobs.confirm_health_wizard_step(
        USER_ID, health_jobs.WIZARD_STEP_WORKOUT_HOUR
    )
    assert "19:05" in ack  # the default hour was approved as workout_window
    assert await user_model.get_value(db, USER_ID, "workout_window") == "19:05"

    class FakeQuery:
        def __init__(self) -> None:
            self.message = SimpleNamespace(message_id=7, chat=SimpleNamespace(id=USER_ID))
            self.edits: list[str] = []
            self.markups: list[Any] = []

        async def edit_message_text(self, text: str, reply_markup: Any = None, parse_mode: Any = None) -> None:
            self.edits.append(text)
            self.markups.append(reply_markup)

        async def answer(self, *a: Any, **k: Any) -> None:
            return None

    # The wizard's next render is the day-specific approval.
    query = FakeQuery()
    shown = await health_jobs.ask_next_health_confirm_step(query, USER_ID)
    assert shown is True
    assert any("שונה מהשאר" in text and "10:00" in text for text in query.edits)
    controls = [
        btn.callback_data for row in query.markups[-1].inline_keyboard for btn in row
    ]
    assert "hrout:accept:4:1000" in controls  # entity-addressed: weekday+time
    assert "hrout:default:4" in controls

    # Approve → the slot keeps its own start; other days keep the default.
    # (The drained queue would legitimately finish the wizard — spy the
    # heavy finish path; slot persistence is what this test pins.)
    finished: list[int] = []

    async def fake_finish(target: Any, uid: int, ack_text: Any = None) -> None:
        finished.append(uid)

    import noam_coach.services.health_jobs as health_jobs_module

    health_jobs_module.finish_health_confirm_wizard, _real_finish = (
        fake_finish, health_jobs_module.finish_health_confirm_wizard,
    )
    approve = FakeQuery()
    handled = await coach_bot.handle_menu_callback(approve, USER_ID, "hrout:accept:4:1000")
    health_jobs_module.finish_health_confirm_wizard = _real_finish
    assert handled is True
    slots = await user_model.get_value(db, USER_ID, "weekly_availability")
    by_day = {int(slot["weekday"]): slot for slot in slots}
    assert by_day[4]["start"] == "10:00"
    assert by_day[6]["start"] == "19:05"
    assert by_day[1]["start"] == "19:05"
    events = await event_log.list_events(db, USER_ID)
    approved = [
        e for e in events
        if e.entity == "workout_hour_evidence" and e.outcome == "outlier_approved"
    ]
    assert approved and approved[0].properties["start"] == "10:00"


@pytest.mark.asyncio
async def test_declining_keeps_the_default_time(db: Database) -> None:
    from noam_coach.services import health_jobs

    await _seed_wizard_state(db)
    install_workout_hour_evidence()
    await health_jobs.confirm_health_wizard_step(
        USER_ID, health_jobs.WIZARD_STEP_WORKOUT_HOUR
    )

    class FakeQuery:
        def __init__(self) -> None:
            self.message = SimpleNamespace(message_id=7, chat=SimpleNamespace(id=USER_ID))
            self.edits: list[str] = []

        async def edit_message_text(self, text: str, reply_markup: Any = None, parse_mode: Any = None) -> None:
            self.edits.append(text)

        async def answer(self, *a: Any, **k: Any) -> None:
            return None

    async def fake_finish(target: Any, uid: int, ack_text: Any = None) -> None:
        return None

    import noam_coach.services.health_jobs as health_jobs_module

    _real_finish = health_jobs_module.finish_health_confirm_wizard
    health_jobs_module.finish_health_confirm_wizard = fake_finish
    try:
        handled = await coach_bot.handle_menu_callback(FakeQuery(), USER_ID, "hrout:default:4")
    finally:
        health_jobs_module.finish_health_confirm_wizard = _real_finish
    assert handled is True
    slots = await user_model.get_value(db, USER_ID, "weekly_availability")
    by_day = {int(slot["weekday"]): slot for slot in slots}
    assert by_day[4]["start"] == "19:05"  # unchanged
    # The queue is drained: the wizard resumes its normal steps.
    from noam_coach.services.workout_hours import _pending_outliers

    assert await _pending_outliers(USER_ID) == []


@pytest.mark.asyncio
async def test_restart_resumes_pending_outlier_approval(db: Database) -> None:
    """The queued approval is persisted state: a restart re-renders it."""
    from noam_coach.services import health_jobs

    await _seed_wizard_state(db)
    install_workout_hour_evidence()
    await health_jobs.confirm_health_wizard_step(
        USER_ID, health_jobs.WIZARD_STEP_WORKOUT_HOUR
    )
    # Simulated restart: wraps reinstalled from scratch.
    uninstall_workout_hour_evidence()
    install_workout_hour_evidence()

    class FakeMessage:
        def __init__(self) -> None:
            self.replies: list[str] = []

        async def reply_text(self, text: str, reply_markup: Any = None, parse_mode: Any = None) -> None:
            self.replies.append(text)

    message = FakeMessage()
    shown = await health_jobs.ask_next_health_confirm_step(message, USER_ID)
    assert shown is True
    assert any("שונה מהשאר" in reply for reply in message.replies)


@pytest.mark.asyncio
async def test_manual_hour_text_edit_still_works_and_skips_outliers(
    db: Database,
) -> None:
    from noam_coach.services import health_jobs
    from noam_coach.services.workout_hours import _pending_outliers

    await _seed_wizard_state(db)
    install_workout_hour_evidence()

    handled, reply = await health_jobs.apply_health_wizard_text_edit(
        USER_ID, health_jobs.WIZARD_STEP_WORKOUT_HOUR, "18:30"
    )
    assert handled is True and "18:30" in reply
    # Manual choice wins: no outlier questions are queued.
    assert await _pending_outliers(USER_ID) == []


@pytest.mark.asyncio
async def test_no_weekday_evidence_keeps_legacy_single_hour_behavior(
    db: Database,
) -> None:
    """Fallback preserved: without per-day data the prompt and confirm behave
    exactly as before."""
    from noam_coach.services import health_jobs

    legacy_value = {"typical_hour": "18:00", "weekly_frequency": 3.0, "sessions_sampled": 5}
    await user_model.set_fact(
        db, USER_ID, "workout_pattern", legacy_value,
        kind=user_model.KIND_ESTIMATE, source=user_model.SOURCE_DERIVED,
    )
    install_workout_hour_evidence()

    fact = await user_model.get_fact(db, USER_ID, "workout_pattern")
    detected, _scope, _hint = health_jobs._wizard_step_prompt(
        health_jobs.WIZARD_STEP_WORKOUT_HOUR, fact, None
    )
    assert "זוהתה שעת אימון טיפוסית סביב 18:00" in detected
    ack = await health_jobs.confirm_health_wizard_step(
        USER_ID, health_jobs.WIZARD_STEP_WORKOUT_HOUR
    )
    assert "18:00" in ack
    from noam_coach.services.workout_hours import _pending_outliers

    assert await _pending_outliers(USER_ID) == []
