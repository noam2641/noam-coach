"""RE11 regression tests — recent-vs-overall training frequency trend proposal.

Covers:
  * learn_workout_pattern computes a separate recent-window frequency
    alongside the existing long-run average.
  * build_frequency_trend_proposal only fires when there's a real gap and
    enough data, and its recommendation is anchored on the recent trend.
  * The onboarding question flow surfaces this as a structured choice (stay /
    go up) while still accepting a typed number directly.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

import coach_bot
import questions
import routine
import user_model
from db import Database
from helpers import utc_now
from noam_coach.bot import onboarding as onboarding_bot
from noam_coach.services import core as core_services

TZ = ZoneInfo("Asia/Jerusalem")


class MockDB:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    async def fetch_all(self, sql: str, parameters: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        return self._rows


def _workout_row(days_ago: float, duration: float = 45.0) -> dict[str, Any]:
    when = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_ago)
    return {"start_time": when.isoformat(), "value": duration}


def test_learn_workout_pattern_computes_recent_window() -> None:
    import asyncio

    # Overall: 2.5/week over 45 days (10 sessions -> 45/7=6.43 weeks -> ~1.6)
    # so instead build an explicit scenario: dense early, sparse recently.
    rows = (
        [_workout_row(days_ago=d) for d in (40, 37, 33, 30, 26, 23, 19, 16)]  # 8 sessions, weeks 3-6 ago
        + [_workout_row(days_ago=d) for d in (10, 3)]  # 2 sessions in the last 14 days
    )
    db = MockDB(rows)
    result = asyncio.run(routine.learn_workout_pattern(db, 1, TZ))
    assert result.sessions_sampled == 10
    assert result.recent_sessions_sampled == 2
    assert result.recent_weekly_frequency == pytest.approx(1.0, abs=0.1)
    assert result.weekly_frequency is not None
    assert result.weekly_frequency > result.recent_weekly_frequency


def test_build_frequency_trend_proposal_none_when_no_recent_data() -> None:
    pattern = routine.WorkoutPattern(
        weekly_frequency=3.0, sessions_sampled=12,
        recent_weekly_frequency=None, recent_sessions_sampled=0,
    )
    assert routine.build_frequency_trend_proposal(pattern) is None


def test_build_frequency_trend_proposal_none_when_values_agree() -> None:
    pattern = routine.WorkoutPattern(
        weekly_frequency=3.0, sessions_sampled=12,
        recent_weekly_frequency=3.1, recent_sessions_sampled=4,
    )
    assert routine.build_frequency_trend_proposal(pattern) is None


def test_build_frequency_trend_proposal_none_when_too_little_data() -> None:
    pattern = routine.WorkoutPattern(
        weekly_frequency=3.0, sessions_sampled=2,
        recent_weekly_frequency=1.0, recent_sessions_sampled=1,
    )
    assert routine.build_frequency_trend_proposal(pattern) is None


def test_build_frequency_trend_proposal_matches_user_example() -> None:
    """2.5 overall, 2 recent -> recommend 3-4, offer 'stay at 2' too."""
    pattern = routine.WorkoutPattern(
        weekly_frequency=2.5, sessions_sampled=16,
        recent_weekly_frequency=2.0, recent_sessions_sampled=4,
    )
    proposal = routine.build_frequency_trend_proposal(pattern)
    assert proposal is not None
    assert "2" in proposal.message
    assert "3" in proposal.message and "4" in proposal.message
    values = [value for _label, value in proposal.choices]
    assert 2.0 in values  # "stay at 2" is offered
    assert 3.0 in values
    assert 4.0 in values


async def _make_db(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "frequency_trend.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'Test',NULL,?)",
        (utc_now(),),
    )
    return db


def _patch_db(monkeypatch: pytest.MonkeyPatch, db: Database) -> None:
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(onboarding_bot, "DB", db)
    monkeypatch.setattr(core_services, "DB", db)


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


class FakeUpdate:
    def __init__(self, text: str) -> None:
        self.effective_message = FakeMessage()
        self.effective_message.text = text


async def _seed_routine_profile(db: Database, *, overall: float, recent: float) -> None:
    import json

    payload = {
        "sleep": {}, "eating": {},
        "workout": {
            "weekly_frequency": overall,
            "sessions_sampled": 16,
            "recent_weekly_frequency": recent,
            "recent_sessions_sampled": 4,
            "typical_hour": None,
            "common_weekdays": [],
        },
    }
    await db.execute(
        "INSERT INTO routine_profile(user_id, profile, updated_at) VALUES(1, ?, ?)"
        " ON CONFLICT(user_id) DO UPDATE SET profile=excluded.profile",
        (json.dumps(payload, ensure_ascii=False), utc_now()),
    )


@pytest.mark.asyncio
async def test_onboarding_shows_trend_proposal_instead_of_plain_question(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await _seed_routine_profile(db, overall=2.5, recent=2.0)

    question = questions.question_by_id("q_training_days")
    assert question is not None
    target = FakeTarget()
    shown = await onboarding_bot._ask_training_frequency_trend_question(target, 1, question)
    assert shown is True
    text = target.messages[-1]
    assert "2" in text and "3" in text
    markup = target.reply_markups[-1]
    labels = [btn.text for row in markup.inline_keyboard for btn in row]
    assert any("2" in label for label in labels)
    assert any("3" in label for label in labels)


@pytest.mark.asyncio
async def test_onboarding_trend_button_records_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await _seed_routine_profile(db, overall=2.5, recent=2.0)

    question = questions.question_by_id("q_training_days")
    target = FakeTarget()
    await onboarding_bot._ask_training_frequency_trend_question(target, 1, question)

    await onboarding_bot.handle_onboarding_callback(target, 1, "qa:q_training_days:trend:3")
    fact = await user_model.get_fact(db, 1, "training_days_per_week")
    assert fact is not None
    assert fact["value"] == 3
    assert fact["confirmed"] is True


@pytest.mark.asyncio
async def test_onboarding_typing_number_directly_still_works_during_trend_proposal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await _seed_routine_profile(db, overall=2.5, recent=2.0)

    question = questions.question_by_id("q_training_days")
    target = FakeTarget()
    await onboarding_bot._ask_training_frequency_trend_question(target, 1, question)

    update = FakeUpdate("5")
    handled = await onboarding_bot.handle_onboarding_text(update, 1)
    assert handled is True
    fact = await user_model.get_fact(db, 1, "training_days_per_week")
    assert fact is not None
    assert fact["value"] == 5


@pytest.mark.asyncio
async def test_no_trend_proposal_when_already_confirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await _seed_routine_profile(db, overall=2.5, recent=2.0)
    await user_model.set_fact(
        db, 1, "training_days_per_week", 4,
        source=user_model.SOURCE_USER, confirmed=True,
    )

    question = questions.question_by_id("q_training_days")
    target = FakeTarget()
    shown = await onboarding_bot._ask_training_frequency_trend_question(target, 1, question)
    assert shown is False
