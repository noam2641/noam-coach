from __future__ import annotations

from pathlib import Path

import pytest

import coach_bot
import planning
import user_model
from db import Database
from helpers import utc_now
from noam_coach.services.weekdays import WEEKDAY_SCHEMA_VERSION


async def _ready_db(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "planning.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'Test',NULL,?)",
        (utc_now(),),
    )
    facts = {
        "weight_kg": 90,
        "height_cm": 174,
        "age": 32,
        "sex": "male",
        "primary_goal": "fat_loss_muscle_retention",
        "diet_restrictions": "none",
        "allergies": "none",
        "training_days_per_week": 3,
        "active_pain": "none",
        "medical_avoidance": "none",
        "session_minutes": 50,
        "training_location": "חדר כושר",
        "equipment": "חדר כושר מלא",
        "strength_experience": "intermediate",
        "weekly_availability": [
            {"weekday": 0, "weekday_schema": WEEKDAY_SCHEMA_VERSION, "start": "19:00", "minutes": 50},
            {"weekday": 2, "weekday_schema": WEEKDAY_SCHEMA_VERSION, "start": "19:00", "minutes": 50},
            {"weekday": 4, "weekday_schema": WEEKDAY_SCHEMA_VERSION, "start": "10:00", "minutes": 60},
            {"weekday": 5, "weekday_schema": WEEKDAY_SCHEMA_VERSION, "start": "10:00", "minutes": 60},
        ],
    }
    for key, value in facts.items():
        await user_model.set_fact(
            db, 1, key, value, source=user_model.SOURCE_USER, confirmed=True
        )
    proposal = await planning.build_goal_proposal(db, 1)
    goal_id = await planning.persist_goal_proposal(db, 1, proposal)
    assert await planning.activate_goal(db, 1, goal_id)
    return db


@pytest.mark.asyncio
async def test_three_nutrition_and_workout_candidates_are_generated(tmp_path: Path) -> None:
    db = await _ready_db(tmp_path)
    nutrition = await planning.generate_candidates(db, 1, "nutrition")
    workout = await planning.generate_candidates(db, 1, "workout")
    assert len(nutrition) == 3
    assert len(workout) == 3
    assert {item.strategy for item in nutrition} == {"structured", "flexible", "low_effort"}
    assert {item.strategy for item in workout} == {"consistency", "balanced", "performance"}
    assert all(session["time"] for session in workout[1].payload["sessions"])
    assert all(
        "warmup_sets" in exercise
        for session in workout[1].payload["sessions"]
        for exercise in session["exercises"]
    )
    assert all(not planning.workout_quality_issues(item.payload) for item in workout)


@pytest.mark.asyncio
async def test_workout_candidates_keep_confirmed_four_days_and_1900_time(tmp_path: Path) -> None:
    """RE10-3 (D13): the three candidates now legitimately differ in
    frequency (consistency=desired-1, balanced=desired, performance=desired+1,
    each bounded by the user's confirmed availability) — so only the
    "balanced" candidate (which always uses the user's stated/resolved
    frequency) is asserted to keep exactly the 4 confirmed 19:00 slots.
    The other two must still only ever use days/times drawn from real
    confirmed availability, never invented ones.
    """
    db = await _ready_db(tmp_path)
    slots = [
        {"weekday": day, "weekday_schema": WEEKDAY_SCHEMA_VERSION, "start": "19:00", "minutes": 50}
        for day in [0, 2, 4, 6]
    ]
    await user_model.set_fact(db, 1, "training_days_per_week", 4, source=user_model.SOURCE_USER, confirmed=True)
    await user_model.set_fact(db, 1, "weekly_availability", slots, source=user_model.SOURCE_USER, confirmed=True)
    await user_model.set_fact(db, 1, "workout_window", "19:00", source=user_model.SOURCE_USER, confirmed=True)

    workout = await planning.generate_candidates(db, 1, "workout")

    assert len(workout) == 3
    balanced = next(c for c in workout if c.strategy == "balanced")
    sessions = balanced.payload["sessions"]
    assert balanced.payload["frequency"] == 4
    assert [session["weekday"] for session in sessions] == [0, 2, 4, 6]
    assert {session["time"] for session in sessions} == {"19:00"}

    confirmed_days = {0, 2, 4, 6}
    for candidate in workout:
        for session in candidate.payload["sessions"]:
            assert session["weekday"] in confirmed_days
            assert session["time"] == "19:00"


@pytest.mark.asyncio
async def test_workout_candidates_differ_in_frequency_and_content(tmp_path: Path) -> None:
    """RE10-3: the three workout candidates must be genuinely different, not
    just differently-labeled copies of the same days/exercises/sets."""
    db = await _ready_db(tmp_path)
    # desired=4 (explicit statement) with 6 confirmed slots gives performance
    # (desired+1=5) real headroom below MAX_FREQUENCY=6. TASK-07: consistency
    # no longer drops to desired-1 — it keeps all declared days and differs by
    # split/volume instead.
    slots = [
        {"weekday": day, "weekday_schema": WEEKDAY_SCHEMA_VERSION, "start": "19:00", "minutes": 50}
        for day in [0, 1, 2, 3, 4, 5]
    ]
    await user_model.set_fact(db, 1, "training_days_per_week", 4, source=user_model.SOURCE_USER, confirmed=True)
    await user_model.set_fact(db, 1, "weekly_availability", slots, source=user_model.SOURCE_USER, confirmed=True)

    workout = await planning.generate_candidates(db, 1, "workout")
    by_strategy = {c.strategy: c for c in workout}
    consistency, balanced, performance = (
        by_strategy["consistency"], by_strategy["balanced"], by_strategy["performance"],
    )

    # TASK-07: no candidate drops below the declared day count; consistency and
    # balanced both keep all four days, performance may add one.
    assert consistency.payload["frequency"] == balanced.payload["frequency"] == 4
    assert performance.payload["frequency"] >= balanced.payload["frequency"]

    # Content must differ too: at minimum, the payloads are not byte-identical
    # once "strategy"/"title" are excluded — same-day/same-split plans used to
    # be indistinguishable except for their marketing text.
    def _session_signature(candidate: planning.PlanCandidate) -> list[tuple]:
        return [
            (s["weekday"], s["code"], tuple(e["sets"] for e in s["exercises"]))
            for s in candidate.payload["sessions"]
        ]

    assert _session_signature(consistency) != _session_signature(balanced)
    assert _session_signature(balanced) != _session_signature(performance)


@pytest.mark.asyncio
async def test_consistency_uses_full_body_and_keeps_all_days(tmp_path: Path) -> None:
    """E3 / TASK-07: the consistency candidate uses varied Full-Body sessions
    (a real weakness of a 3-day A/B/C is training legs only once) AND keeps
    every declared training day — it must not drop a day to a lower frequency."""
    db = await _ready_db(tmp_path)
    await user_model.set_fact(db, 1, "training_days_per_week", 4, source=user_model.SOURCE_USER, confirmed=True)

    workout = await planning.generate_candidates(db, 1, "workout")
    consistency = next(c for c in workout if c.strategy == "consistency")
    # No day dropped: four declared days stay four varied Full-Body sessions.
    assert consistency.payload["frequency"] == 4
    codes = [s["code"] for s in consistency.payload["sessions"]]
    assert codes == ["FB1", "FB2", "FB3", "FB4"]

    # A/B/C (shared leg day) is still legitimate for other strategies at 3 days.
    balanced_at_three = planning._workout_candidate(
        "מאוזנת", "balanced", 3, await planning.collect_facts(db, 1),
        score=0.9, rationale=[], tradeoffs=[],
    )
    assert [s["code"] for s in balanced_at_three.payload["sessions"]] == ["A", "B", "C"]


@pytest.mark.asyncio
async def test_strategy_volume_bounds_are_respected(tmp_path: Path) -> None:
    """E4: consistency never drops sets below the safe floor; performance
    never exceeds the safe ceiling, for a non-advanced lifter."""
    db = await _ready_db(tmp_path)
    workout = await planning.generate_candidates(db, 1, "workout")
    by_strategy = {c.strategy: c for c in workout}
    for candidate in by_strategy.values():
        for session in candidate.payload["sessions"]:
            for exercise_entry in session["exercises"]:
                assert planning._MIN_SETS_PER_EXERCISE <= exercise_entry["sets"] <= planning._MAX_SETS_PER_EXERCISE


@pytest.mark.asyncio
async def test_plan_activation_is_versioned_and_single_active(tmp_path: Path) -> None:
    db = await _ready_db(tmp_path)
    candidates = await planning.generate_candidates(db, 1, "nutrition")
    first = await planning.activate_plan(db, 1, int(candidates[0].id or 0))
    second = await planning.activate_plan(db, 1, int(candidates[1].id or 0))
    assert first and second
    active = await planning.get_active_plan(db, 1, "nutrition")
    assert active and active["id"] == candidates[1].id
    old = await db.fetch_one("SELECT status FROM plan_versions WHERE id=?", (candidates[0].id,))
    assert old and old["status"] == "superseded"


@pytest.mark.asyncio
async def test_workout_activation_blocks_duplicate_exercise_payload(tmp_path: Path) -> None:
    db = await _ready_db(tmp_path)
    payload = {
        "frequency": 1,
        "sessions": [
            {
                "weekday": 0,
                "time": "18:00",
                "minutes": 45,
                "exercises": [
                    {"id": "bench", "name": "Bench", "sets": 3, "rmin": 8, "rmax": 10},
                    {"id": "bench", "name": "Bench duplicate", "sets": 3, "rmin": 8, "rmax": 10},
                ],
            }
        ],
    }
    plan_id = await db.execute(
        """
        INSERT INTO plan_versions(
            user_id, plan_type, title, strategy, fit_score, status, payload,
            rationale, tradeoffs, assumptions, based_on, created_at
        ) VALUES(1, 'workout', 'Bad Workout', 'bad', 0.1, 'candidate', ?, '[]', '[]', '[]', '{}', ?)
        """,
        (planning.json.dumps(payload, ensure_ascii=False), utc_now()),
    )

    with pytest.raises(planning.PlanningBlockedError) as exc:
        await planning.activate_plan(db, 1, plan_id)

    assert any("duplicate_exercise" in item for item in exc.value.missing)


def test_repair_workout_payload_drops_duplicate_and_fixes_params() -> None:
    """L-NEW-2: a safe repair removes duplicate exercises and fixes bad params."""
    payload = {
        "frequency": 1,
        "sessions": [
            {
                "weekday": 0,
                "time": "",          # missing time -> defaulted
                "minutes": 5,         # invalid -> clamped
                "exercises": [
                    {"id": "bench", "name": "Bench", "sets": 99, "rmin": 8, "rmax": 10},
                    {"id": "bench", "name": "Bench dup", "sets": 3, "rmin": 8, "rmax": 10},
                ],
            }
        ],
    }
    repaired = planning.repair_workout_payload(payload)
    session = repaired["sessions"][0]
    assert session["time"]                          # time filled in
    assert 20 <= session["minutes"] <= 150          # duration clamped
    ids = [ex["id"] for ex in session["exercises"]]
    assert ids == ["bench"]                          # duplicate dropped
    assert session["exercises"][0]["sets"] <= 6      # sets clamped
    issues = planning.workout_quality_issues(repaired)
    assert "session_1_duplicate_exercise_bench" not in issues
    assert "workout_plan_too_small" in issues
    # Original payload was not mutated.
    assert len(payload["sessions"][0]["exercises"]) == 2


def test_workout_quality_rejects_single_exercise_disguised_as_plan() -> None:
    payload = {
        "frequency": 1,
        "sessions": [
            {
                "code": "A",
                "name": "Workout A",
                "weekday": 0,
                "time": "18:00",
                "minutes": 45,
                "exercises": [
                    {"id": "leg_press", "name": "Leg Press", "sets": 3, "rmin": 8, "rmax": 12},
                ],
            }
        ],
    }

    assert "workout_plan_too_small" in planning.workout_quality_issues(payload)


def test_workout_quality_rejects_frequency_session_mismatch() -> None:
    payload = {
        "frequency": 4,
        "sessions": [
            {
                "code": "A",
                "name": "Workout A",
                "weekday": 0,
                "time": "18:00",
                "minutes": 45,
                "exercises": [
                    {"id": "squat", "name": "Squat", "sets": 3, "rmin": 8, "rmax": 12},
                    {"id": "bench", "name": "Bench", "sets": 3, "rmin": 8, "rmax": 12},
                ],
            }
        ],
    }

    assert "frequency_session_count_mismatch" in planning.workout_quality_issues(payload)


def test_repair_workout_candidates_never_emits_broken_candidate() -> None:
    """A candidate that repairs down to one exercise must not survive to the renderer."""
    broken = planning.PlanCandidate(
        plan_type="workout",
        title="broken",
        strategy="bad",
        score=0.9,
        rationale=[],
        tradeoffs=[],
        assumptions=[],
        payload={
            "frequency": 1,
            "sessions": [
                {
                    "weekday": 0,
                    "time": "18:00",
                    "minutes": 45,
                    "exercises": [
                        {"id": "squat", "name": "Squat", "sets": 3, "rmin": 5, "rmax": 8},
                        {"id": "squat", "name": "Squat dup", "sets": 3, "rmin": 5, "rmax": 8},
                    ],
                }
            ],
        },
    )
    result = planning._repair_workout_candidates([broken])
    assert result == []


def test_repair_workout_candidates_drops_unrepairable() -> None:
    """A candidate with no exercises at all cannot be repaired -> dropped."""
    empty = planning.PlanCandidate(
        plan_type="workout",
        title="empty",
        strategy="bad",
        score=0.9,
        rationale=[],
        tradeoffs=[],
        assumptions=[],
        payload={"frequency": 1, "sessions": [{"weekday": 0, "time": "18:00", "minutes": 45, "exercises": []}]},
    )
    assert planning._repair_workout_candidates([empty]) == []


@pytest.mark.asyncio
async def test_provisional_goal_cannot_be_activated(tmp_path: Path) -> None:
    """P0: a computed goal with missing mandatory data is not activatable."""
    db = Database(str(tmp_path / "goal_block.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'T',NULL,?)",
        (utc_now(),),
    )
    # Only weight is known — sex, age and primary_goal are missing.
    await user_model.set_fact(db, 1, "weight_kg", 90, source=user_model.SOURCE_USER, confirmed=True)
    proposal = await planning.build_goal_proposal(db, 1)
    assert proposal.provisional is True
    goal_id = await planning.persist_goal_proposal(db, 1, proposal)
    with pytest.raises(planning.GoalNotReady):
        await planning.activate_goal(db, 1, goal_id)
    missing = await planning.missing_goal_inputs(db, 1)
    assert "מין" in missing or "גיל" in missing


@pytest.mark.asyncio
async def test_manual_goal_activates_without_full_data(tmp_path: Path) -> None:
    """A manually-set goal is an explicit user value and is allowed."""
    db = Database(str(tmp_path / "manual_goal.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'T',NULL,?)",
        (utc_now(),),
    )
    goal_id = await db.execute(
        "INSERT INTO goal_versions(user_id, calories, protein, steps, phase, status, source, "
        "explanation, created_at) VALUES(1, 2000, 150, 8000, 'maintain', 'proposed', 'manual', '', ?)",
        (utc_now(),),
    )
    assert await planning.activate_goal(db, 1, int(goal_id)) is True


@pytest.mark.asyncio
async def test_activate_goal_supersedes_active_provisional_goal(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "active_provisional_goal.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'T',NULL,?)",
        (utc_now(),),
    )
    provisional_id = await db.execute(
        "INSERT INTO goal_versions(user_id, calories, protein, steps, phase, status, source, "
        "explanation, created_at) VALUES(1, 2100, 140, 8000, 'maintain', 'active_provisional', 'computed', '', ?)",
        (utc_now(),),
    )
    goal_id = await db.execute(
        "INSERT INTO goal_versions(user_id, calories, protein, steps, phase, status, source, "
        "explanation, created_at) VALUES(1, 2000, 150, 9000, 'maintain', 'proposed', 'manual', '', ?)",
        (utc_now(),),
    )

    assert await planning.activate_goal(db, 1, int(goal_id)) is True

    old = await db.fetch_one("SELECT status FROM goal_versions WHERE id=?", (provisional_id,))
    new = await db.fetch_one("SELECT status FROM goal_versions WHERE id=?", (goal_id,))
    assert old and old["status"] == "superseded"
    assert new and new["status"] == "active"


@pytest.mark.asyncio
async def test_fetch_goal_prefers_goal_versions_over_legacy_goals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = Database(str(tmp_path / "single_goal_source.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'T',NULL,?)",
        (utc_now(),),
    )
    await db.execute(
        """
        INSERT INTO goal_versions(user_id, calories, protein, steps, phase, status, source, created_at)
        VALUES(1, 1900, 155, 9000, 'fat_loss_muscle_retention', 'active', 'computed', ?)
        """,
        (utc_now(),),
    )
    await db.execute(
        """
        INSERT INTO goals(user_id, calories, protein, steps, phase, updated_at)
        VALUES(1, 3000, 90, 4000, 'legacy', ?)
        """,
        (utc_now(),),
    )
    monkeypatch.setattr(coach_bot, "DB", db)

    goal = await coach_bot.fetch_goal(1)

    assert goal["calories"] == 1900
    assert goal["protein"] == 155
    assert goal["steps"] == 9000
    assert goal["phase"] == "fat_loss_muscle_retention"


@pytest.mark.asyncio
async def test_gap_blocks_workout_readiness(tmp_path: Path) -> None:
    db = await _ready_db(tmp_path)
    await user_model.record_gap(db, 1, "equipment", why_matters="required")
    # record_gap does not overwrite a real fact; invalidate it to simulate a real gap.
    await db.execute("DELETE FROM user_facts WHERE user_id=1 AND key='equipment'")
    await user_model.record_gap(db, 1, "equipment", why_matters="required")
    with pytest.raises(planning.PlanningBlockedError):
        await planning.build_workout_candidates(db, 1)
