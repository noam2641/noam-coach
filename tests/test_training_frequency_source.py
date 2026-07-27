"""The calorie target must use the training frequency the user confirmed.

Two stores hold this number and they disagreed by 20x in the 2026-07-27
session:

    routine_profile.workout.weekly_frequency   = 0.2
    user_facts.training_days_per_week          = 4

`routine_profile` is recomputed from the health export, and
`learn_workout_pattern` measures 45 days back from *now* while the export
ended 43 days earlier -- so 1 of 296 workouts survived the window and the
profile concluded 0.2 sessions/week.

compute_personal_targets read the profile, so the target was computed as if
the user barely trained:

    workouts_per_week=0.2 -> 2290 kcal
    workouts_per_week=4.0 -> 2420 kcal

130 kcal/day, ~910 a week, understated for the whole of a fat-loss phase.

Every other input to compute_targets already goes through
`get_decision_value`, which prefers confirmed user facts. Only this one
reached around it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import coach_bot
import user_model
from db import Database
from helpers import utc_now
from noam_coach.services import goals as goal_services


async def _make_db(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "freq.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1, 'T', NULL, ?)",
        (utc_now(),),
    )
    return db


def _bind(monkeypatch: pytest.MonkeyPatch, db: Database) -> None:
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(goal_services, "DB", db)


async def _profile(db: Database, weekly_frequency: float) -> None:
    import json

    await db.execute(
        """
        INSERT INTO routine_profile(user_id, profile, updated_at) VALUES(1, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET profile=excluded.profile
        """,
        (
            json.dumps(
                {"workout": {"weekly_frequency": weekly_frequency, "sessions_sampled": 1}},
                ensure_ascii=False,
            ),
            utc_now(),
        ),
    )


async def _stated(db: Database, value: object) -> None:
    await user_model.set_fact(
        db, 1, "training_days_per_week", value,
        kind=user_model.KIND_FACT, source=user_model.SOURCE_USER, confirmed=True,
    )


@pytest.mark.asyncio
async def test_the_confirmed_fact_wins_over_the_derived_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact live split: profile says 0.2, the user said 4."""
    db = await _make_db(tmp_path)
    _bind(monkeypatch, db)
    await _profile(db, 0.2)
    await _stated(db, 4)

    assert await goal_services._weekly_training_frequency(1) == 4.0


@pytest.mark.asyncio
async def test_the_profile_is_still_the_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user who never stated a frequency keeps the inferred one."""
    db = await _make_db(tmp_path)
    _bind(monkeypatch, db)
    await _profile(db, 3.5)

    assert await goal_services._weekly_training_frequency(1) == 3.5


@pytest.mark.asyncio
async def test_no_data_at_all_yields_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path)
    _bind(monkeypatch, db)

    assert await goal_services._weekly_training_frequency(1) is None


@pytest.mark.asyncio
async def test_a_zero_stated_frequency_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Zero is not a usable training frequency -- prefer the inference."""
    db = await _make_db(tmp_path)
    _bind(monkeypatch, db)
    await _profile(db, 2.0)
    await _stated(db, 0)

    assert await goal_services._weekly_training_frequency(1) == 2.0


@pytest.mark.asyncio
async def test_an_unparseable_stated_frequency_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malformed fact must not crash the target computation."""
    db = await _make_db(tmp_path)
    _bind(monkeypatch, db)
    await _profile(db, 2.0)
    await _stated(db, "not-a-number")

    assert await goal_services._weekly_training_frequency(1) == 2.0


@pytest.mark.asyncio
async def test_the_calorie_target_reflects_the_stated_frequency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: the 130 kcal/day gap the split produced."""
    db = await _make_db(tmp_path)
    _bind(monkeypatch, db)
    await _profile(db, 0.2)
    for key, value in (
        ("weight_kg", 98.84),
        ("avg_steps", 6366),
        ("sex", "male"),
        ("age", 32),
        ("body_fat_pct", 24.1),
        ("primary_goal", "fat_loss_muscle_retention"),
    ):
        await user_model.set_fact(
            db, 1, key, value,
            kind=user_model.KIND_FACT, source=user_model.SOURCE_USER, confirmed=True,
        )

    await _stated(db, 4)
    with_fact = await goal_services.compute_personal_targets(1)

    await user_model.set_fact(
        db, 1, "training_days_per_week", 0,
        kind=user_model.KIND_FACT, source=user_model.SOURCE_USER, confirmed=True,
    )
    from_profile = await goal_services.compute_personal_targets(1)

    assert with_fact is not None and from_profile is not None
    assert with_fact.calories > from_profile.calories, (
        "training 4x a week must not yield a lower target than barely training"
    )
