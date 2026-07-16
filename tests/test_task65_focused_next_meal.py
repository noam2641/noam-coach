"""TASK-65 — focused "מה לאכול עכשיו" first screen.

The first response answers the immediate question only: one recommendation,
its macros, a short reason, safety caveats — no daily status, no workout
status section, no remaining-day timeline. Those live behind the secondary
controls (nextmeal:why → the explanation surface, menu:status).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

import coach_bot
from config import TZ
from db import Database
from helpers import utc_now
from noam_coach.services.next_meal import (
    format_next_meal_explanation,
    format_next_meal_recommendation,
    generate_next_meal_recommendation,
    next_meal_action_rows,
)

USER_ID = 1
NOW = datetime.now(TZ).replace(minute=0, second=0, microsecond=0)


@pytest.fixture
async def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Database:
    database = Database(str(tmp_path / "task65.db"))
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
    monkeypatch.setattr(coach_bot, "DB", database)
    from config import SETTINGS

    monkeypatch.setattr(SETTINGS, "telegram_allowed_user_id", USER_ID, raising=False)
    return database


async def _recommendation(db: Database) -> Any:
    return await generate_next_meal_recommendation(db, USER_ID, now=NOW)


@pytest.mark.asyncio
async def test_first_screen_is_answer_only(db: Database) -> None:
    """Acceptance 1-3, 7: one recommendation; no daily-status heading, no
    workout-status heading, no timeline heading, no future meal slots."""
    recommendation = await _recommendation(db)
    text = format_next_meal_recommendation(recommendation)

    assert "הארוחה המומלצת עכשיו" in text
    option = recommendation.options[0]
    assert option.title in text
    assert f"כ-{option.calories} קל׳" in text
    # The removed surfaces:
    assert "סטטוס אימון" not in text
    assert "תכנון שאר היום" not in text
    assert "מצב היום" not in text
    assert "נשארו" not in text  # the daily remaining headline
    assert "ארוחת ערב" not in text and "ארוחת לילה" not in text  # future slots
    # Compact: a short scannable message, not a planning screen.
    assert len([line for line in text.split("\n") if line.strip()]) <= 9


@pytest.mark.asyncio
async def test_reason_stays_short_and_present(db: Database) -> None:
    recommendation = await _recommendation(db)
    text = format_next_meal_recommendation(recommendation)
    assert "למה עכשיו:" in text


@pytest.mark.asyncio
async def test_safety_notices_still_render_on_the_first_screen(db: Database) -> None:
    """Acceptance 6: warnings that affect the immediate food stay visible."""
    recommendation = await _recommendation(db)
    recommendation.notices.insert(0, "שים לב: רגישות לאגוזים — נבחרה חלופה בלי אגוזים.")
    text = format_next_meal_recommendation(recommendation)
    assert "רגישות לאגוזים" in text


@pytest.mark.asyncio
async def test_workout_clarification_note_still_renders(db: Database) -> None:
    recommendation = await _recommendation(db)
    if not recommendation.needs_workout_clarification:
        object.__setattr__(recommendation, "needs_workout_clarification", True)
    text = format_next_meal_recommendation(recommendation)
    assert "סטטוס האימון" in text  # the clarification hint, not a status dump


@pytest.mark.asyncio
async def test_details_move_behind_the_why_surface(db: Database) -> None:
    """Acceptance 4: the calculation/status/timeline all remain reachable."""
    recommendation = await _recommendation(db)
    explanation = format_next_meal_explanation(recommendation)
    assert "מצב היום" in explanation
    assert "איך חושב?" in explanation
    assert "אימון" in explanation


@pytest.mark.asyncio
async def test_action_rows_expose_the_why_control(db: Database) -> None:
    recommendation = await _recommendation(db)
    rows = next_meal_action_rows(recommendation)
    callbacks = [cb for row in rows for _label, cb in row]
    assert "nextmeal:save:1" in callbacks
    assert "nextmeal:refresh" in callbacks
    assert "nextmeal:editqty:1" in callbacks
    assert "nextmeal:why" in callbacks  # the detail surface is one tap away
    assert "menu:status" in callbacks
    assert len(callbacks) <= 5


@pytest.mark.asyncio
async def test_goal_caveat_still_qualifies_the_budget(db: Database) -> None:
    """A default/provisional goal is a caveat about the immediate meal's
    budget — it stays, compactly."""
    await db.execute("UPDATE goal_versions SET status='superseded'")
    recommendation = await _recommendation(db)
    text = format_next_meal_recommendation(recommendation)
    assert "ברירת מחדל" in text
