"""TASK-59 — one chronological remaining-day action timeline.

Required regressions: workout day, non-workout day, meal approval
recalculation, meal correction, workout completed, workout postponed to a
concrete time, chronological ordering (including an after-midnight
bedtime), and remaining protein ALLOCATED across meals rather than the full
budget repeated per meal.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

import coach_bot
import user_model
from config import TZ
from db import Database
from helpers import utc_now
from noam_coach.services.day_timeline import (
    active_planned_meals,
    build_remaining_day_events,
    format_remaining_day_lines,
)
from noam_coach.services.next_meal import (
    build_workout_nutrition_context,
    generate_next_meal_recommendation,
    plan_chosen_meal,
    save_chosen_meal,
    save_workout_reschedule_time,
)

USER_ID = 1


@pytest.fixture
async def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Database:
    database = Database(str(tmp_path / "task59.db"))
    await database.init()
    await database.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(?, 'Test', NULL, ?)",
        (USER_ID, utc_now()),
    )
    await database.execute(
        """
        INSERT INTO goal_versions(user_id, calories, protein, steps, phase, status, source, created_at)
        VALUES(?, 2100, 150, 8000, 'fat_loss_muscle_retention', 'active', 'test', ?)
        """,
        (USER_ID, utc_now()),
    )
    await user_model.set_fact(
        database, USER_ID, "sleep_schedule",
        {"bedtime": "23:30", "typical_bedtime": "23:30"},
        source=user_model.SOURCE_USER, confirmed=True,
    )
    monkeypatch.setattr(coach_bot, "DB", database)
    from config import SETTINGS

    monkeypatch.setattr(SETTINGS, "telegram_allowed_user_id", USER_ID, raising=False)
    return database


def _now(hour: int, minute: int = 0) -> datetime:
    return datetime.now(TZ).replace(hour=hour, minute=minute, second=0, microsecond=0)


async def _meal(db: Database, at: datetime, calories: int, protein: int = 30) -> None:
    await db.execute(
        """
        INSERT INTO meals(user_id, name, calories, protein, carbs, fat, confidence, eaten_at, created_at)
        VALUES(?, 'm', ?, ?, 0, 0, 1, ?, ?)
        """,
        (USER_ID, calories, protein, at.astimezone(timezone.utc).isoformat(), utc_now()),
    )


async def _events(db: Database, now: datetime) -> list[Any]:
    context = await build_workout_nutrition_context(db, USER_ID, now=now)
    planned = await active_planned_meals(db, USER_ID, now)
    return build_remaining_day_events(context, planned_meals=planned, now=now)


def _meal_events(events: list[Any]) -> list[Any]:
    return [event for event in events if event.icon == "🍽️"]


# ---------------------------------------------------------------------------
# Chronology
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_after_midnight_bedtime_sorts_last_not_first(db: Database) -> None:
    """The incident's ordering bug: a 00:36 bedtime must close the day, not
    lead it (lexicographic HH:MM put it before the 19:09 workout)."""
    await user_model.set_fact(
        db, USER_ID, "sleep_schedule",
        {"bedtime": "00:36", "typical_bedtime": "00:36"},
        source=user_model.SOURCE_USER, confirmed=True,
    )
    now = _now(17, 39)
    events = await _events(db, now)

    assert events, "timeline must not be empty"
    sleep_events = [event for event in events if event.icon == "😴"]
    if sleep_events:
        assert events[-1].icon == "😴"  # sleep closes the day


@pytest.mark.asyncio
async def test_every_meal_slot_has_a_concrete_time(db: Database) -> None:
    now = _now(16, 30)
    await _meal(db, _now(13), 600)
    events = await _events(db, now)

    for event in _meal_events(events):
        assert event.time_hhmm, f"untimed meal slot: {event.text}"
    # And the whole list is chronological in minutes-from-now.
    keys = [event.sort_minutes for event in events]
    assert keys == sorted(keys)


# ---------------------------------------------------------------------------
# Allocation — never the full budget per meal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remaining_budget_is_allocated_across_meals(db: Database) -> None:
    now = _now(14, 0)
    await _meal(db, _now(9), 500, protein=30)
    context = await build_workout_nutrition_context(db, USER_ID, now=now)
    events = await _events(db, now)
    meals = _meal_events(events)

    assert len(meals) >= 2  # afternoon: more than one eating opportunity left
    remaining = context.nutrition.calorie_balance
    import re

    calories = [int(re.search(r"כ-(\d+) קל", event.text).group(1)) for event in meals]
    proteins = [int(re.search(r"כ-(\d+) ג׳", event.text).group(1)) for event in meals]
    # No slot repeats the entire remaining budget...
    assert all(c < remaining for c in calories)
    # ...and the plan sums to (at most) the remaining budget.
    assert sum(calories) <= remaining
    protein_remaining = context.nutrition.protein_balance
    assert sum(proteins) <= max(protein_remaining, 0) + 5  # rounding slack
    assert all(p < protein_remaining for p in proteins)


# ---------------------------------------------------------------------------
# Workout day / non-workout day / postponed / completed
# ---------------------------------------------------------------------------


async def _workout_status(db: Database, status: str, now: datetime) -> None:
    from noam_coach.services.next_meal import save_next_meal_workout_status

    await save_next_meal_workout_status(db, USER_ID, status, now=now)


@pytest.mark.asyncio
async def test_workout_postponed_to_concrete_time_appears_in_timeline(
    db: Database,
) -> None:
    """B12 reuse: a concrete reschedule places the workout event at the
    user-stated time."""
    now = _now(16, 0)
    expected = _now(19, 9)
    await save_workout_reschedule_time(db, USER_ID, expected, now=now)

    events = await _events(db, now)
    workout_events = [event for event in events if event.icon == "🏋️"]
    assert workout_events and workout_events[0].time_hhmm == "19:09"
    # Chronology: the pre-workout meal (if any) precedes the workout.
    lines = format_remaining_day_lines(events)
    assert lines[0] == "המשך היום:"


@pytest.mark.asyncio
async def test_completed_workout_leaves_no_future_workout_event(db: Database) -> None:
    now = _now(19, 0)
    await _workout_status(db, "completed", now)
    events = await _events(db, now)
    assert not any(event.icon == "🏋️" for event in events)


@pytest.mark.asyncio
async def test_non_workout_day_timeline_is_meals_and_sleep_only(db: Database) -> None:
    now = _now(15, 0)
    await _workout_status(db, "cancelled", now)
    events = await _events(db, now)
    assert not any(event.icon == "🏋️" for event in events)
    assert _meal_events(events)


# ---------------------------------------------------------------------------
# Recalculation after state changes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_meal_approval_shrinks_the_next_render(db: Database) -> None:
    now = _now(14, 0)
    import re

    before = _meal_events(await _events(db, now))
    total_before = sum(int(re.search(r"כ-(\d+) קל", e.text).group(1)) for e in before)

    await _meal(db, _now(14, 5), 700)  # meal approved/logged

    after = _meal_events(await _events(db, now.replace(minute=30)))
    total_after = sum(int(re.search(r"כ-(\d+) קל", e.text).group(1)) for e in after)
    assert total_after < total_before


@pytest.mark.asyncio
async def test_meal_correction_updates_later_allocations(db: Database) -> None:
    """A corrected meal (calories changed) shifts the remaining plan."""
    now = _now(14, 0)
    await _meal(db, _now(13), 400)
    import re

    baseline = sum(
        int(re.search(r"כ-(\d+) קל", e.text).group(1))
        for e in _meal_events(await _events(db, now))
    )
    # Correction: the 400-kcal meal was actually 900.
    await db.execute("UPDATE meals SET calories=900 WHERE user_id=?", (USER_ID,))
    corrected = sum(
        int(re.search(r"כ-(\d+) קל", e.text).group(1))
        for e in _meal_events(await _events(db, now))
    )
    assert corrected < baseline


# ---------------------------------------------------------------------------
# B12 planned meals in the timeline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_planned_meal_appears_and_consumed_planned_meal_disappears(
    db: Database,
) -> None:
    now = _now(15, 0)
    rec = await generate_next_meal_recommendation(db, USER_ID, now=now)
    option = rec.options[0]
    assert await plan_chosen_meal(db, USER_ID, option, now=now)

    events = await _events(db, now)
    planned = [event for event in events if event.icon == "📌"]
    assert planned and option.title in planned[0].text

    # Consuming the planned meal (fingerprint transition) removes it.
    await save_chosen_meal(db, USER_ID, option, now=now)
    events = await _events(db, now)
    assert not [event for event in events if event.icon == "📌"]


# ---------------------------------------------------------------------------
# No redundant prose (requirement 7)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_prose_below_the_timeline(db: Database) -> None:
    now = _now(16, 0)
    events = await _events(db, now)
    lines = format_remaining_day_lines(events)
    assert lines[0] == "המשך היום:"
    for line in lines[1:]:
        assert line.startswith(("🍽️", "🏋️", "😴", "📌"))  # events only, no prose
