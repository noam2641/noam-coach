"""End-to-end-ish training lifecycle regression.

This test keeps a realistic product trace in one place: availability memory,
training history, pain memory, and the next load decision. It intentionally
uses service-level APIs instead of Telegram objects so it stays fast and
deterministic while still catching broken wiring between modules.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import coach_bot
import user_model
from db import Database
from helpers import utc_now
from noam_coach.services import training
from noam_coach.services.availability import (
    parse_hebrew_availability_answer,
    resolve_availability,
)


async def _make_db(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "training_lifecycle.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1, 'Noam', NULL, ?)",
        (utc_now(),),
    )
    return db


async def _add_completed_session(
    db: Database,
    *,
    exercise_id: str,
    reps: int,
    rir: int,
    weight: float,
) -> None:
    plan = {
        "name": "Trace",
        "exercises": [
            {
                "id": exercise_id,
                "name": exercise_id,
                "sets": 3,
                "rmin": 8,
                "rmax": 12,
                "inc": 2.5,
                "weight": weight,
            }
        ],
    }
    sid = await db.execute(
        "INSERT INTO sessions(user_id, code, name, plan, status, exercise_index, set_number, started_at, ended_at) "
        "VALUES(1, 'T', 'Trace', ?, 'completed', 0, 4, ?, ?)",
        (json.dumps(plan, ensure_ascii=False), utc_now(), utc_now()),
    )
    for set_number in range(1, 4):
        await db.execute(
            "INSERT INTO sets(session_id, exercise_id, exercise_name, set_number, weight, reps, rir, source, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, 'telegram_adjusted', ?)",
            (sid, exercise_id, exercise_id, set_number, weight, reps, rir, utc_now()),
        )


@pytest.mark.asyncio
async def test_availability_history_pain_and_next_load_decision_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path)
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(training, "DB", db)

    parsed = parse_hebrew_availability_answer(
        "ראשון 19:00 שעה, שני 19:00 שעה, רביעי 18:30 45 דקות, שישי 10:00 שעה"
    )
    await user_model.set_fact(
        db,
        1,
        "weekly_availability",
        parsed.weekly_availability,
        source=user_model.SOURCE_USER,
        confirmed=True,
    )
    await user_model.set_fact(
        db,
        1,
        "session_minutes",
        parsed.session_minutes,
        source=user_model.SOURCE_USER,
        confirmed=True,
    )

    availability = await resolve_availability(db, 1)
    assert availability.max_days_per_week == 4
    assert availability.preferred_days == [0, 2, 4, 6]
    slots_by_day = {slot["weekday"]: slot for slot in parsed.weekly_availability}
    assert slots_by_day[6]["start"] == "19:00"
    assert slots_by_day[0]["start"] == "19:00"
    assert slots_by_day[2]["start"] == "18:30"
    assert slots_by_day[4]["start"] == "10:00"
    assert slots_by_day[2]["minutes"] == 45

    current = {
        "id": "one_arm_row",
        "name": "חתירה ביד אחת",
        "sets": 3,
        "rmin": 8,
        "rmax": 12,
        "inc": 2.5,
        "weight": 20,
    }

    await _add_completed_session(db, exercise_id="one_arm_row", reps=12, rir=3, weight=20)
    clean_decision = await coach_bot.recommend_load_decision(1, current)
    assert clean_decision.decision == "increase_load"
    assert clean_decision.weight > 20

    await db.execute(
        """
        INSERT INTO medical_constraints(
            user_id, kind, location, severity, status, note, affects, created_at
        ) VALUES(1, 'pain', 'טניס אלבו', 2, 'active', 'reported during workout',
                 '["exercise_selection"]', ?)
        """,
        (utc_now(),),
    )
    await user_model.set_fact(
        db,
        1,
        "active_pain",
        {"location": "מרפק", "status": "active"},
        source=user_model.SOURCE_USER,
        confirmed=True,
    )

    pain_decision = await training.recommend_load_decision(1, current)
    assert pain_decision.decision == "hold_for_active_pain"
    assert pain_decision.weight == 20
    assert "active_pain:elbow" in pain_decision.signals
    assert "מרפק" in pain_decision.explanation
