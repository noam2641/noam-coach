"""Full life-scenario end-to-end regression.

One continuous user journey through the real product surface, in order:

1. A new user finishes onboarding-equivalent setup: profile row, weekly
   availability parsed from the actual Hebrew sentence, an active goal.
2. Starts a workout session.
3. Reports the machine is occupied and accepts a substitution.
4. Logs a set, then reports mild elbow pain mid-workout.
5. Finishes the workout partially (finish -> "סיים חלקי").
6. The next workout is adapted: the load that would have increased is held
   because of the active pain, and the exercise card warns about the elbow.
7. Asks for the next meal and gets a real recommendation.
8. Corrects the meal ("בלי טורטיה") and the item is removed with totals
   recalculated.

Unlike the per-feature regression files, this test's value is the wiring:
every step runs against the same database so state written by one flow must
be discovered by the next one. It uses the same fake-query pattern as the
other regression files so the whole trace stays fast and deterministic.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

import coach_bot
import meal_intelligence
import user_model
from config import TZ
from db import Database
from helpers import utc_now
from models import FoodItem, MealAnalysis
from noam_coach.bot import callback_session as callback_session_bot
from noam_coach.bot import ui as ui_bot
from noam_coach.bot import workout as workout_bot
from noam_coach.services import training
from noam_coach.services.availability import (
    parse_hebrew_availability_answer,
    resolve_availability,
)
from noam_coach.services.next_meal import (
    format_next_meal_recommendation,
    generate_next_meal_recommendation,
)


class _FakeQuery:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.reply_markups: list[Any] = []

    async def edit_message_text(
        self,
        text: str,
        reply_markup: Any = None,
        parse_mode: str | None = None,
    ) -> None:
        del parse_mode
        self.messages.append(text)
        self.reply_markups.append(reply_markup)

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        del text, show_alert


class _FakeContext:
    job_queue = None


_WORKOUT_PLAN = {
    "name": "אימון A",
    "exercises": [
        {
            "id": "leg_press",
            "name": "לחיצת רגליים",
            "sets": 3,
            "rmin": 8,
            "rmax": 12,
            "inc": 5,
            "weight": 100,
            "muscle": "רגליים",
            "cues": ["גב צמוד למשענת"],
            "alts": [
                {"id": "hack_squat", "name": "האק סקוואט", "weight": 60},
                {"id": "bulgarian_split", "name": "מכרעים בולגריים", "weight": 20},
                {"id": "leg_extension", "name": "פשיטת ברך", "weight": 30},
            ],
        },
        {
            "id": "one_arm_row",
            "name": "חתירה ביד אחת",
            "sets": 3,
            "rmin": 8,
            "rmax": 12,
            "inc": 2.5,
            "weight": 20,
            "muscle": "גב",
            "cues": ["גב ניטרלי"],
            "alts": [],
        },
    ],
}


def _labels(markup: Any) -> list[str]:
    return [btn.text for row in markup.inline_keyboard for btn in row]


async def _add_completed_row_session(db: Database) -> None:
    """Last week's completed one_arm_row workout: strong (RIR 3) at 20kg,
    i.e. history that would normally justify a load increase."""
    plan = {
        "name": "History",
        "exercises": [
            {
                "id": "one_arm_row",
                "name": "חתירה ביד אחת",
                "sets": 3,
                "rmin": 8,
                "rmax": 12,
                "inc": 2.5,
                "weight": 20,
            }
        ],
    }
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    sid = await db.execute(
        "INSERT INTO sessions(user_id, code, name, plan, status, exercise_index, set_number, started_at, ended_at) "
        "VALUES(1, 'B', 'History', ?, 'completed', 0, 4, ?, ?)",
        (json.dumps(plan, ensure_ascii=False), week_ago, week_ago),
    )
    for set_number in range(1, 4):
        await db.execute(
            "INSERT INTO sets(session_id, exercise_id, exercise_name, set_number, weight, reps, rir, source, created_at) "
            "VALUES(?, 'one_arm_row', 'חתירה ביד אחת', ?, 20, 12, 3, 'telegram_adjusted', ?)",
            (sid, set_number, week_ago),
        )


@pytest.mark.asyncio
async def test_full_life_scenario_from_onboarding_to_meal_correction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = Database(str(tmp_path / "life_scenario.db"))
    await db.init()

    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(training, "DB", db)
    monkeypatch.setattr(callback_session_bot, "DB", db)
    monkeypatch.setattr(ui_bot, "DB", db)
    monkeypatch.setattr(workout_bot, "DB", db)

    # ------------------------------------------------------------------
    # 1. New user: profile row, availability from real Hebrew, active goal.
    # ------------------------------------------------------------------
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1, 'Noam', NULL, ?)",
        (utc_now(),),
    )
    parsed = parse_hebrew_availability_answer(
        "ראשון 19:00 שעה, שני 19:00 שעה, רביעי 18:30 45 דקות, שישי 10:00 שעה"
    )
    for key, value in (
        ("weekly_availability", parsed.weekly_availability),
        ("session_minutes", parsed.session_minutes),
        ("sleep_schedule", {"bedtime": "23:00"}),
    ):
        await user_model.set_fact(
            db, 1, key, value, source=user_model.SOURCE_USER, confirmed=True
        )
    await db.execute(
        """
        INSERT INTO goal_versions(user_id, calories, protein, steps, phase, status, source, created_at)
        VALUES(1, 2000, 150, 8000, 'fat_loss_muscle_retention', 'active', 'test', ?)
        """,
        (utc_now(),),
    )

    availability = await resolve_availability(db, 1)
    assert availability.max_days_per_week == 4
    assert availability.preferred_days == [0, 2, 4, 6]

    # Training history from a previous week that would justify progression.
    await _add_completed_row_session(db)

    # ------------------------------------------------------------------
    # 2. Start a workout session.
    # ------------------------------------------------------------------
    started_at = (datetime.now(timezone.utc) - timedelta(minutes=40)).isoformat()
    sid = await db.execute(
        "INSERT INTO sessions(user_id, code, name, plan, status, exercise_index, set_number, started_at) "
        "VALUES(1, 'A', 'אימון A', ?, 'active', 0, 1, ?)",
        (json.dumps(_WORKOUT_PLAN, ensure_ascii=False), started_at),
    )
    session = dict(await db.fetch_one("SELECT * FROM sessions WHERE id=?", (sid,)))

    # ------------------------------------------------------------------
    # 3. Machine occupied -> real alternatives + skip; user takes the first.
    # ------------------------------------------------------------------
    query = _FakeQuery()
    await callback_session_bot.handle_session_action_callback(
        query,
        context=None,
        user_id=1,
        data=coach_bot.session_action_data("occupied", session),
    )
    labels = _labels(query.reply_markups[-1])
    assert any("האק סקוואט" in label for label in labels)
    assert any(label == "דלג" for label in labels)

    await callback_session_bot.handle_session_action_callback(
        query,
        context=None,
        user_id=1,
        # A11b: alternatives are named by id, never by list position.
        data=coach_bot.session_action_data("sub", session, "hack_squat", "equipment"),
    )
    session = dict(await db.fetch_one("SELECT * FROM sessions WHERE id=?", (sid,)))
    plan_now = json.loads(session["plan"])
    assert plan_now["exercises"][0]["id"] == "hack_squat"
    assert plan_now["exercises"][0]["original_id"] == "leg_press"

    # ------------------------------------------------------------------
    # 4. One set done, then mild elbow pain reported mid-workout.
    # ------------------------------------------------------------------
    await db.execute(
        "INSERT INTO sets(session_id, exercise_id, exercise_name, set_number, weight, reps, rir, source, created_at) "
        "VALUES(?, 'hack_squat', 'האק סקוואט', 1, 60, 10, 2, 'telegram_adjusted', ?)",
        (sid, utc_now()),
    )
    await callback_session_bot.handle_session_action_callback(
        query,
        context=None,
        user_id=1,
        data=coach_bot.session_action_data("painloc", session, "elbow"),
    )
    session = dict(await db.fetch_one("SELECT * FROM sessions WHERE id=?", (sid,)))
    await callback_session_bot.handle_session_action_callback(
        query,
        context=None,
        user_id=1,
        data=coach_bot.session_action_data("painlevel", session, 2),
    )

    constraint = await db.fetch_one(
        "SELECT * FROM medical_constraints WHERE user_id=1 AND kind='pain' AND status='active'"
    )
    assert constraint is not None
    assert constraint["location"] == "elbow"
    fact = await user_model.get_fact(db, 1, "training_limitations")
    assert fact is not None
    assert "מרפק" in fact["value"]["location"]

    # ------------------------------------------------------------------
    # 5. Finish partially: confirm screen, then explicit "סיים חלקי".
    # ------------------------------------------------------------------
    session = dict(await db.fetch_one("SELECT * FROM sessions WHERE id=?", (sid,)))
    await callback_session_bot.handle_session_action_callback(
        query,
        context=None,
        user_id=1,
        data=coach_bot.session_action_data("finish", session),
    )
    assert "לסיים את האימון?" in query.messages[-1]
    # Audit F-A4: an incomplete workout (1 set logged) no longer offers a
    # false "full"; the truthful partial-finish option is 'סיים כחלקי'
    # and its callback is still wdone:...:partial.
    labels = _labels(query.reply_markups[-1])
    assert any("כחלקי" in label or "חלקי" in label for label in labels)
    assert not any("סיים מלא" in label for label in labels)

    await callback_session_bot.handle_session_action_callback(
        query,
        context=_FakeContext(),
        user_id=1,
        data=coach_bot.session_action_data("wdone", session, "partial"),
    )
    session = dict(await db.fetch_one("SELECT * FROM sessions WHERE id=?", (sid,)))
    assert session["status"] == "partial"
    assert session["ended_at"] is not None
    audit = await db.fetch_one(
        "SELECT * FROM audit WHERE user_id=1 AND action='finish' ORDER BY id DESC LIMIT 1"
    )
    assert audit is not None
    assert json.loads(audit["details"])["partial"] is True

    # ------------------------------------------------------------------
    # 6. Next workout is adapted: pain holds the load and the card warns.
    # ------------------------------------------------------------------
    next_exercise = dict(_WORKOUT_PLAN["exercises"][1])
    decision = await training.recommend_load_decision(1, next_exercise)
    assert decision.decision == "hold_for_active_pain"
    assert decision.weight == 20
    assert "active_pain:elbow" in decision.signals
    assert "מרפק" in decision.explanation

    next_plan = {"name": "אימון B", "exercises": [next_exercise]}
    next_sid = await db.execute(
        "INSERT INTO sessions(user_id, code, name, plan, status, exercise_index, set_number, started_at) "
        "VALUES(1, 'B', 'אימון B', ?, 'active', 0, 1, ?)",
        (json.dumps(next_plan, ensure_ascii=False), utc_now()),
    )
    card_query = _FakeQuery()
    await workout_bot.show_session(card_query, 1, next_sid)
    card = card_query.messages[-1]
    assert "כאב" in card and "מרפק" in card

    # ------------------------------------------------------------------
    # 7. "What should I eat now?" -> a real recommendation with options.
    # ------------------------------------------------------------------
    now = datetime.now(TZ).replace(hour=14, minute=0, second=0, microsecond=0)
    eaten_at = (now - timedelta(hours=3)).isoformat()
    await db.execute(
        """
        INSERT INTO meals(user_id, name, calories, protein, carbs, fat, confidence, eaten_at, created_at)
        VALUES(1, 'ארוחת בוקר', 700, 55, 0, 0, 1, ?, ?)
        """,
        (eaten_at, utc_now()),
    )
    recommendation = await generate_next_meal_recommendation(db, 1, now=now)
    assert recommendation.options, "a mid-day meal request must produce options"
    text = format_next_meal_recommendation(recommendation)
    assert text.strip()

    # ------------------------------------------------------------------
    # 8. Meal correction: "בלי טורטיה" removes the item and recalculates.
    # ------------------------------------------------------------------
    corrections = meal_intelligence.parse_meal_correction("בלי טורטיה")
    removals = [c for c in corrections if c.kind == "remove"]
    assert removals, "'בלי טורטיה' must parse as an item removal"

    analysis = MealAnalysis(
        meal_name="טורטיה עם עוף",
        confidence=0.9,
        items=[
            FoodItem(name="חזה עוף", grams=150, calories=248,
                     protein=46, carbs=0, fat=5, confidence=0.9),
            FoodItem(name="טורטיה", grams=60, calories=180,
                     protein=5, carbs=30, fat=4, confidence=0.9),
        ],
    )
    total_before = sum(item.calories for item in analysis.items)
    corrected = meal_intelligence.apply_item_removal_correction(analysis, removals[0])
    names = [item.name for item in corrected.items]
    assert "טורטיה" not in names
    assert any("עוף" in name for name in names)
    assert sum(item.calories for item in corrected.items) < total_before
