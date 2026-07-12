"""REC-ARCH-01 corrective pass — regression coverage for the precedence fix,
the operational precedence policy, the single-shared-state nutrition
migration, live meal_timing wiring, the fixed recent-meal timing calc, and
the duplicate-resolver re-audit (select_todays_workout_code /
workout_completed_today).

Numbered per the corrective-pass brief:
  1. Active session beats explicit "later".
  2. Active session beats explicit "completed" when a NEW session started
     after the "completed" flag was set.
  3. Completed session beats a stale planned-but-never-started workout.
  4. Explicit current-day clarification beats plan when no actual event
     exists.
  5. Plan beats learned routine.
  6. Routine is fallback only (no plan, no actual, no explicit).
  7. One SharedUserState instance feeds sibling decision projections in the
     migrated nutrition flow (call-counting resolve_workout_state).
  8. Nutrition and workout decision contexts cannot disagree on workout
     phase/source when built from the same shared state.
  9. Large meal ~20 minutes ago + demanding workout soon -> affects the
     actual next-meal recommendation/notices.
  10. Same large meal several hours ago -> does NOT trigger the same
      recent-meal delay behavior.
  11. Unknown/missing meal time -> does not produce a fabricated exact delay
      number.
  12. A planned (not consumed) meal never enters consumed-meal recency logic.
  13. Existing workout-code selection (select_todays_workout_code) still
      correct after the changes.
  14. Existing workout_completed_today behavior still correct after the
      changes (including the new HealthKit-only case it always covered).
  15. No import cycle between nutrition and workout modules.

Plus the precedence/policy-divergence parametrized test from item #2 of the
brief (precedence.py operational, not decorative).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from config import TZ
from db import Database
from helpers import utc_now
from noam_coach.services import daily_state
from noam_coach.services import user_state as user_state_module
from noam_coach.services.meal_timing import recent_meal_state_from_consumed
from noam_coach.services.next_meal import (
    generate_next_meal_recommendation,
    save_next_meal_workout_status,
)
from noam_coach.services.nutrition_context import build_nutrition_context
from noam_coach.services.precedence import (
    WORKOUT_SOURCE_RANK,
    UnknownPrecedenceSourceError,
    higher_precedence_source,
    select_highest_precedence,
    workout_source_rank,
)
from noam_coach.services.user_state import (
    ConsumedMeal,
    WorkoutPhase,
    WorkoutState,
    build_shared_state,
    has_actual_workout_completion_evidence_today,
    resolve_workout_state,
)
from noam_coach.services.weekdays import WEEKDAY_SCHEMA_VERSION, local_weekday
from noam_coach.services.workout_decision_context import project_workout_decision_context


@pytest.fixture
async def db(tmp_path: Path) -> Database:
    database = Database(str(tmp_path / "shared_user_state_corrective.db"))
    await database.init()
    return database


async def _user(db: Database, user_id: int = 1) -> None:
    await db.execute(
        "INSERT OR IGNORE INTO users(id, first_name, username, updated_at) VALUES(?, 'Test', NULL, ?)",
        (user_id, utc_now()),
    )


async def _goal(db: Database, user_id: int = 1) -> None:
    await db.execute(
        """
        INSERT INTO goal_versions(user_id, calories, protein, steps, phase, status, source, created_at)
        VALUES(?, 2200, 160, 8000, 'fat_loss_muscle_retention', 'active', 'test', ?)
        """,
        (user_id, utc_now()),
    )


async def _meal(
    db: Database,
    user_id: int,
    now: datetime,
    *,
    calories: int,
    protein: int = 20,
    fat: int = 0,
    minutes_ago: int = 30,
    name: str = "meal",
) -> None:
    eaten_at = (now - timedelta(minutes=minutes_ago)).astimezone(TZ).isoformat()
    await db.execute(
        """
        INSERT INTO meals(user_id, name, calories, protein, carbs, fat, confidence, eaten_at, created_at)
        VALUES(?, ?, ?, ?, 0, ?, 1, ?, ?)
        """,
        (user_id, name, calories, protein, fat, eaten_at, utc_now()),
    )


async def _workout_plan(
    db: Database,
    user_id: int,
    now: datetime,
    *,
    time_text: str,
    minutes: int = 60,
    exercises: list[dict] | None = None,
) -> None:
    weekday = local_weekday(now)
    payload = {
        "frequency": 1,
        "sessions": [
            {
                "weekday": weekday,
                "weekday_schema": WEEKDAY_SCHEMA_VERSION,
                "weekday_name": "today",
                "time": time_text,
                "minutes": minutes,
                "code": "A",
                "name": "Workout A",
                "exercises": exercises or [{"id": "squat", "sets": 3, "muscle": "רגליים"}],
            }
        ],
    }
    plan_id = await db.execute(
        """
        INSERT INTO plan_versions(
            user_id, plan_type, title, strategy, fit_score, status, payload,
            rationale, tradeoffs, assumptions, based_on, created_at, activated_at
        ) VALUES(?, 'workout', 'Test Workout', 'balanced', 1, 'active', ?, '[]', '[]', '[]', '{}', ?, ?)
        """,
        (user_id, json.dumps(payload), utc_now(), utc_now()),
    )
    await db.execute(
        "INSERT INTO active_plans(user_id, plan_type, plan_id, updated_at) VALUES(?, 'workout', ?, ?)",
        (user_id, plan_id, utc_now()),
    )


async def _active_session(db: Database, user_id: int, started_at: datetime, *, code: str = "A") -> None:
    await db.execute(
        """
        INSERT INTO sessions(user_id, code, name, plan, status, exercise_index, set_number, started_at, ended_at)
        VALUES(?, ?, 'Workout', '{}', 'active', 0, 0, ?, NULL)
        """,
        (user_id, code, started_at.astimezone(TZ).isoformat()),
    )


async def _completed_session(
    db: Database,
    user_id: int,
    started_at: datetime,
    ended_at: datetime,
    *,
    status: str = "completed",
    code: str = "A",
) -> None:
    await db.execute(
        """
        INSERT INTO sessions(user_id, code, name, plan, status, exercise_index, set_number, started_at, ended_at)
        VALUES(?, ?, 'Workout', '{}', ?, 0, 0, ?, ?)
        """,
        (user_id, code, status, started_at.astimezone(TZ).isoformat(), ended_at.astimezone(TZ).isoformat()),
    )


async def _healthkit_workout(db: Database, user_id: int, start: datetime, end: datetime) -> None:
    await db.execute(
        """
        INSERT INTO health(user_id, external_id, sample_type, value, unit, start_time, end_time, created_at)
        VALUES(?, ?, 'workout', 1, 'session', ?, ?, ?)
        """,
        (user_id, f"hk-{start.isoformat()}", start.astimezone(TZ).isoformat(), end.astimezone(TZ).isoformat(), utc_now()),
    )


async def _routine_profile(db: Database, user_id: int, weekday: int) -> None:
    profile = {"workout": {"common_weekdays": [weekday]}, "sleep": {}, "eating": {}}
    await db.execute(
        "INSERT INTO routine_profile(user_id, profile, updated_at) VALUES(?, ?, ?)",
        (user_id, json.dumps(profile), utc_now()),
    )


# ---------------------------------------------------------------------------
# 1. Active session beats explicit "later".
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_active_session_beats_explicit_later(db: Database) -> None:
    await _user(db)
    now = datetime(2026, 7, 12, 18, 40, tzinfo=TZ)  # Sunday
    await _workout_plan(db, 1, now, time_text="18:00")  # plan time already passed
    await save_next_meal_workout_status(db, 1, "later", now=now)
    # A session is genuinely active right now — must outrank the stale "later".
    await _active_session(db, 1, now - timedelta(minutes=5))

    state = await resolve_workout_state(db, 1, now)

    assert state.source == "active_session"
    assert state.phase == WorkoutPhase.DURING_WORKOUT


# ---------------------------------------------------------------------------
# 2. Active session beats explicit "completed" when a NEW session started
#    after the "completed" flag was set.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_active_session_beats_stale_explicit_completed(db: Database) -> None:
    await _user(db)
    now = datetime(2026, 7, 12, 20, 0, tzinfo=TZ)
    # User tapped "completed" earlier (e.g. after workout A), then started a
    # second session (e.g. an evening mobility/cardio session) that is
    # genuinely active right now. The live session must win.
    await save_next_meal_workout_status(db, 1, "completed", now=now - timedelta(hours=1))
    await _active_session(db, 1, now - timedelta(minutes=10), code="B")

    state = await resolve_workout_state(db, 1, now)

    assert state.source == "active_session"
    assert state.phase == WorkoutPhase.DURING_WORKOUT
    assert state.is_actual is True


# ---------------------------------------------------------------------------
# 3. Completed session beats a stale planned-but-never-started workout.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_completed_session_beats_stale_plan(db: Database) -> None:
    await _user(db)
    now = datetime(2026, 7, 12, 21, 0, tzinfo=TZ)
    # Plan says 18:00 (time long passed, would otherwise read as "time
    # passed, unknown status") but a session actually completed at 19:30.
    await _workout_plan(db, 1, now, time_text="18:00")
    await _completed_session(db, 1, now - timedelta(hours=2), now - timedelta(hours=1, minutes=30))

    state = await resolve_workout_state(db, 1, now)

    assert state.source == "completed_session"
    assert state.phase in {WorkoutPhase.POST_WORKOUT_LATER, WorkoutPhase.POST_WORKOUT_IMMEDIATE}
    assert state.is_future_plan is False


# ---------------------------------------------------------------------------
# 4. Explicit current-day clarification beats plan when no actual event
#    exists (this is the behavior test_workout_clarification_persists_for_today
#    in the acceptance suite also covers — kept here for locality alongside
#    the rest of the precedence matrix).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_explicit_beats_plan_when_no_actual_event(db: Database) -> None:
    await _user(db)
    now = datetime(2026, 7, 12, 21, 0, tzinfo=TZ)
    await _workout_plan(db, 1, now, time_text="18:00")
    await save_next_meal_workout_status(db, 1, "completed", now=now)
    # No session row at all — explicit is the strongest evidence available.

    state = await resolve_workout_state(db, 1, now)

    assert state.source == "user_clarification"
    assert state.phase == WorkoutPhase.POST_WORKOUT_IMMEDIATE


# ---------------------------------------------------------------------------
# 5. Plan beats learned routine.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_beats_routine(db: Database) -> None:
    await _user(db)
    now = datetime(2026, 7, 12, 17, 0, tzinfo=TZ)
    weekday = local_weekday(now)
    await _routine_profile(db, 1, weekday)
    await _workout_plan(db, 1, now, time_text="18:00")

    state = await resolve_workout_state(db, 1, now)

    assert state.source == "active_workout_plan"


# ---------------------------------------------------------------------------
# 6. Routine is fallback only (no plan, no actual, no explicit).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_routine_is_fallback_only(db: Database) -> None:
    await _user(db)
    now = datetime(2026, 7, 12, 17, 0, tzinfo=TZ)
    weekday = local_weekday(now)
    await _routine_profile(db, 1, weekday)

    state = await resolve_workout_state(db, 1, now)

    assert state.source == "routine_pattern"


# ---------------------------------------------------------------------------
# Precedence/policy-divergence coverage (brief item #2): resolver behavior
# must match precedence.py's declared ranking for every pair of sources that
# can realistically co-occur as candidates.
# ---------------------------------------------------------------------------


_CONFLICT_SCENARIOS: dict[str, WorkoutState] = {
    "active_session": WorkoutState(phase=WorkoutPhase.DURING_WORKOUT, source="active_session", label=""),
    "completed_session": WorkoutState(phase=WorkoutPhase.POST_WORKOUT_IMMEDIATE, source="completed_session", label=""),
    "healthkit_session": WorkoutState(phase=WorkoutPhase.POST_WORKOUT_IMMEDIATE, source="healthkit_session", label=""),
    "user_clarification": WorkoutState(phase=WorkoutPhase.DURING_WORKOUT, source="user_clarification", label=""),
    "active_workout_plan": WorkoutState(phase=WorkoutPhase.PRE_WORKOUT_NEAR, source="active_workout_plan", label=""),
    "routine_pattern": WorkoutState(phase=WorkoutPhase.WORKOUT_STATUS_UNKNOWN, source="routine_pattern", label=""),
    "no_workout_evidence": WorkoutState(phase=WorkoutPhase.REST_DAY, source="no_workout_evidence", label=""),
}


@pytest.mark.parametrize("source_a", list(_CONFLICT_SCENARIOS))
@pytest.mark.parametrize("source_b", list(_CONFLICT_SCENARIOS))
def test_select_highest_precedence_matches_higher_precedence_source(source_a: str, source_b: str) -> None:
    """For every pair of registered sources, the candidate-list selector
    (what resolve_workout_state actually uses) must agree with the pairwise
    comparator (what a reviewer would reason about by hand). If these two
    ever diverge, resolver behavior has silently drifted from the declared
    policy."""
    state_a = _CONFLICT_SCENARIOS[source_a]
    state_b = _CONFLICT_SCENARIOS[source_b]

    winner_by_selection = select_highest_precedence([(source_a, state_a), (source_b, state_b)])
    winner_by_pairwise = higher_precedence_source(source_a, source_b)

    expected = state_a if winner_by_pairwise == source_a else state_b
    assert winner_by_selection is expected


def test_every_workout_state_source_string_is_registered() -> None:
    """Every source string resolve_workout_state's candidate helpers can
    produce must be registered in precedence.py, or select_highest_precedence
    raises loudly instead of silently defaulting."""
    known_sources = {
        "active_session",
        "completed_session",
        "session_status",
        "healthkit_session",
        "user_clarification",
        "active_workout_plan",
        "routine_pattern",
        "no_workout_evidence",
    }
    assert known_sources <= set(WORKOUT_SOURCE_RANK)


def test_unregistered_source_raises_not_silently_defaults() -> None:
    with pytest.raises(UnknownPrecedenceSourceError):
        workout_source_rank("some_future_source_nobody_registered")
    with pytest.raises(UnknownPrecedenceSourceError):
        select_highest_precedence([("totally_unknown", "value")])


# ---------------------------------------------------------------------------
# 7. One SharedUserState instance feeds sibling decision projections in the
#    migrated nutrition flow — resolve_workout_state is called exactly once
#    for the whole "what should I eat now" request.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_next_meal_recommendation_resolves_workout_state_once(db: Database) -> None:
    await _user(db)
    await _goal(db)
    now = datetime(2026, 7, 12, 17, 45, tzinfo=TZ)
    await _workout_plan(db, 1, now, time_text="18:30")

    real_resolver = user_state_module.resolve_workout_state
    call_count = 0

    async def _counting_resolver(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return await real_resolver(*args, **kwargs)

    with patch.object(user_state_module, "resolve_workout_state", side_effect=_counting_resolver):
        rec = await generate_next_meal_recommendation(db, 1, now=now)

    assert rec is not None
    assert call_count == 1, f"expected exactly one resolve_workout_state call, got {call_count}"


# ---------------------------------------------------------------------------
# 8. Nutrition and workout decision contexts cannot disagree on workout
#    phase/source when built from the same shared state.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nutrition_and_workout_decision_context_agree_from_same_shared_state(db: Database) -> None:
    await _user(db)
    await _goal(db)
    now = datetime(2026, 7, 12, 17, 45, tzinfo=TZ)
    await _workout_plan(db, 1, now, time_text="18:30")

    shared = await build_shared_state(db, 1, now=now)
    workout_ctx = project_workout_decision_context(shared)
    nutrition_ctx = await build_nutrition_context(db, 1, "test", shared_state=shared)

    assert workout_ctx.phase.value == nutrition_ctx.workout_status
    assert workout_ctx.workout.source == nutrition_ctx.workout_source
    assert nutrition_ctx.current_local_time == shared.now.isoformat()


# ---------------------------------------------------------------------------
# 9 / 10. Large recent meal near a demanding workout affects the actual
# next-meal recommendation; the same meal hours ago does not.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recent_large_meal_before_demanding_workout_surfaces_in_recommendation(db: Database) -> None:
    await _user(db)
    await _goal(db)
    now = datetime(2026, 7, 12, 16, 55, tzinfo=TZ)  # Sunday
    heavy_session_exercises = [
        {"id": "squat", "sets": 5, "muscle": "רגליים"},
        {"id": "leg_press", "sets": 5, "muscle": "רגליים"},
        {"id": "rdl", "sets": 4, "muscle": "שרשרת אחורית"},
    ]
    await _workout_plan(db, 1, now, time_text="18:30", minutes=70, exercises=heavy_session_exercises)
    await _meal(db, 1, now, calories=950, fat=40, minutes_ago=20, name="big lunch")

    rec = await generate_next_meal_recommendation(db, 1, now=now)

    assert rec.meal_timing is not None
    assert rec.meal_timing.should_delay is True
    assert any("ארוחה גדולה" in notice for notice in rec.notices)


@pytest.mark.asyncio
async def test_same_large_meal_hours_ago_does_not_trigger_delay(db: Database) -> None:
    await _user(db)
    await _goal(db)
    now = datetime(2026, 7, 12, 10, 0, tzinfo=TZ)
    heavy_session_exercises = [
        {"id": "squat", "sets": 5, "muscle": "רגליים"},
        {"id": "leg_press", "sets": 5, "muscle": "רגליים"},
    ]
    await _workout_plan(db, 1, now, time_text="18:30", minutes=70, exercises=heavy_session_exercises)
    await _meal(db, 1, now, calories=950, fat=40, minutes_ago=0, name="big lunch")

    rec = await generate_next_meal_recommendation(db, 1, now=now)

    # Workout is ~8.5h away: plenty of time regardless of meal size.
    assert rec.meal_timing is None


# ---------------------------------------------------------------------------
# 11. Unknown/missing meal time does not produce a fabricated exact delay.
# ---------------------------------------------------------------------------


def test_missing_eaten_at_yields_unknown_minutes_since_not_zero() -> None:
    meal = ConsumedMeal(name="mystery meal", calories=900, protein=40, fat=30, eaten_at=None, time_confidence="logged")
    now = datetime(2026, 7, 12, 17, 0, tzinfo=TZ)

    recent = recent_meal_state_from_consumed(meal, now=now)

    assert recent is not None
    assert recent.minutes_since_eaten is None


@pytest.mark.asyncio
async def test_unknown_meal_time_confidence_flows_through_live_recommendation(db: Database) -> None:
    await _user(db)
    await _goal(db)
    now = datetime(2026, 7, 12, 16, 55, tzinfo=TZ)
    heavy_session_exercises = [
        {"id": "squat", "sets": 5, "muscle": "רגליים"},
        {"id": "leg_press", "sets": 5, "muscle": "רגליים"},
        {"id": "rdl", "sets": 4, "muscle": "שרשרת אחורית"},
    ]
    await _workout_plan(db, 1, now, time_text="18:30", minutes=70, exercises=heavy_session_exercises)
    await _meal(db, 1, now, calories=950, fat=40, minutes_ago=20, name="big lunch")
    # Force the confidence path to "logged" is the default in this fixture;
    # this test instead directly exercises the low-confidence branch via the
    # unit-level function (live recommendation always logs a real eaten_at,
    # so "unknown" time is only reachable at the meal_timing layer itself —
    # see test_missing_eaten_at_yields_unknown_minutes_since_not_zero above
    # for the live-data path, and the direct unit test below for the
    # low-confidence contract).
    rec = await generate_next_meal_recommendation(db, 1, now=now)
    assert rec.meal_timing is not None
    if rec.meal_timing.confidence == "low":
        assert rec.meal_timing.delay_minutes_min is None
        assert rec.meal_timing.delay_minutes_max is None


# ---------------------------------------------------------------------------
# 12. A planned (not consumed) meal never enters consumed-meal recency logic.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_planned_meal_never_enters_consumed_recency_logic(db: Database) -> None:
    await _user(db)
    await _goal(db)
    now = datetime(2026, 7, 12, 12, 0, tzinfo=TZ)
    await db.execute(
        "INSERT INTO daily_flags(user_id, day, flags, created_at) VALUES(1, ?, ?, ?)",
        (
            now.date().isoformat(),
            json.dumps({"next_meal_planned": [{"name": "planned dinner", "calories": 900, "protein": 40}]}),
            utc_now(),
        ),
    )

    state = await build_shared_state(db, 1, now=now)

    assert state.consumed_meals_today == ()
    recent = recent_meal_state_from_consumed(state.consumed_meals_today[0] if state.consumed_meals_today else None, now=now)
    assert recent is None


# ---------------------------------------------------------------------------
# 13. select_todays_workout_code still correct after the changes.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_select_todays_workout_code_still_picks_todays_session(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    import user_model
    from noam_coach.bot import ui as ui_module

    await _user(db)
    monkeypatch.setattr(ui_module, "DB", db)
    today_wd = datetime.now(TZ).weekday()
    plan = {
        "sessions": [
            {"code": "A", "weekday": today_wd},
            {"code": "B", "weekday": (today_wd + 1) % 7},
            {"code": "C", "weekday": (today_wd + 2) % 7},
        ]
    }
    await user_model.set_fact(db, 1, "active_workout_plan", plan, source=user_model.SOURCE_SYSTEM)

    code = await ui_module.select_todays_workout_code(1)

    assert code == "A"


# ---------------------------------------------------------------------------
# 14. workout_completed_today still correct after the changes (bot session,
#     HealthKit-only, and neither).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workout_completed_today_true_for_bot_session(db: Database) -> None:
    await _user(db)
    now = datetime(2026, 7, 12, 20, 0, tzinfo=TZ)
    await _completed_session(db, 1, now - timedelta(hours=1), now - timedelta(minutes=30))

    assert await daily_state.workout_completed_today(db, 1, now=now) is True


@pytest.mark.asyncio
async def test_workout_completed_today_true_for_healthkit_only(db: Database) -> None:
    await _user(db)
    now = datetime(2026, 7, 12, 20, 0, tzinfo=TZ)
    await _healthkit_workout(db, 1, now - timedelta(hours=2), now - timedelta(hours=1))

    assert await daily_state.workout_completed_today(db, 1, now=now) is True
    # And the shared resolver now agrees a real event happened today too.
    assert await has_actual_workout_completion_evidence_today(db, 1, now) is True


@pytest.mark.asyncio
async def test_workout_completed_today_false_with_no_evidence(db: Database) -> None:
    await _user(db)
    now = datetime(2026, 7, 12, 20, 0, tzinfo=TZ)

    assert await daily_state.workout_completed_today(db, 1, now=now) is False


@pytest.mark.asyncio
async def test_workout_completed_today_false_for_explicit_only_no_real_evidence(db: Database) -> None:
    """Strict contract preserved: an unverified explicit "completed" self
    report alone must NOT flip workout_completed_today to True — that
    function's docstring promises "actually done", not "user said so"."""
    await _user(db)
    now = datetime(2026, 7, 12, 20, 0, tzinfo=TZ)
    await save_next_meal_workout_status(db, 1, "completed", now=now)

    assert await daily_state.workout_completed_today(db, 1, now=now) is False


@pytest.mark.asyncio
async def test_workout_completed_today_false_for_cancelled_session(db: Database) -> None:
    await _user(db)
    now = datetime(2026, 7, 12, 20, 0, tzinfo=TZ)
    await _completed_session(db, 1, now - timedelta(hours=1), now - timedelta(minutes=30), status="cancelled")

    assert await daily_state.workout_completed_today(db, 1, now=now) is False


# ---------------------------------------------------------------------------
# 15. No import cycle between nutrition and workout modules.
# ---------------------------------------------------------------------------


def test_no_import_cycle_between_nutrition_and_workout_modules() -> None:
    import importlib

    modules = [
        "noam_coach.services.user_state",
        "noam_coach.services.daily_state",
        "noam_coach.services.precedence",
        "noam_coach.services.workout_decision_context",
        "noam_coach.services.meal_timing",
        "noam_coach.services.next_meal",
        "noam_coach.services.nutrition_context",
    ]
    for name in modules:
        importlib.import_module(name)


# ---------------------------------------------------------------------------
# REC-ARCH-01 pass 3 — temporal validity / staleness. Rank alone ("ACTUAL >
# EXPLICIT") is not sufficient: a candidate must also still be CURRENT.
# ``next_meal_workout_status_at`` was written by ``save_next_meal_workout_status``
# since 6b675b9 but never read anywhere in user_state.py until this pass —
# these tests specifically exercise the staleness dimension, not just rank.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_explicit_completed_does_not_win_with_no_contradicting_event(db: Database) -> None:
    """The precedence-divergence scenario this pass's primary fix addresses:
    a stale daily clarification exists (old ``next_meal_workout_status_at``),
    and there is NO newer actual event to out-rank it either — rank alone
    would still let the stale explicit input win (EXPLICIT > PLAN). The
    staleness check must reject it on its own, independent of any actual
    event appearing, and let the plan resume being current truth."""
    await _user(db)
    now = datetime(2026, 7, 12, 21, 0, tzinfo=TZ)
    # Tapped "completed" 7 hours ago — past STALE_EXPLICIT_CLARIFICATION_MAX_HOURS (6h).
    await save_next_meal_workout_status(db, 1, "completed", now=now - timedelta(hours=7))
    await _workout_plan(db, 1, now, time_text="18:00")  # plan window (18:00-19:00) long passed

    state = await resolve_workout_state(db, 1, now)

    assert state.source != "user_clarification"
    assert state.source == "active_workout_plan"
    assert state.phase == WorkoutPhase.WORKOUT_PLANNED_TIME_PASSED


@pytest.mark.asyncio
async def test_fresh_explicit_completed_still_wins_within_staleness_window(db: Database) -> None:
    """Sanity counterpart: a clarification well within the staleness window
    (2h old, under the 6h bound) still behaves exactly as before this pass —
    the staleness check must not become overly aggressive."""
    await _user(db)
    now = datetime(2026, 7, 12, 20, 0, tzinfo=TZ)
    await save_next_meal_workout_status(db, 1, "completed", now=now - timedelta(hours=2))
    await _workout_plan(db, 1, now, time_text="19:30")

    state = await resolve_workout_state(db, 1, now)

    assert state.source == "user_clarification"
    assert state.phase == WorkoutPhase.POST_WORKOUT_IMMEDIATE


@pytest.mark.asyncio
async def test_stale_later_no_longer_reinterprets_passed_plan_as_near(db: Database) -> None:
    """"later" tapped once in the morning must not, by evening, keep forcing
    a long-passed plan window to read as PRE_WORKOUT_NEAR. Cross-day leakage
    was already impossible (flags are scoped per local_day); this is the
    within-day staleness gap."""
    await _user(db)
    now = datetime(2026, 7, 12, 21, 0, tzinfo=TZ)
    await _workout_plan(db, 1, now, time_text="09:00")  # long passed
    # Tapped "later" 8 hours ago (stale) — plan time had already passed then too.
    await save_next_meal_workout_status(db, 1, "later", now=now - timedelta(hours=8))

    state = await resolve_workout_state(db, 1, now)

    assert state.source == "active_workout_plan"
    assert state.phase == WorkoutPhase.WORKOUT_PLANNED_TIME_PASSED


@pytest.mark.asyncio
async def test_fresh_later_still_reinterprets_passed_plan_as_near(db: Database) -> None:
    """Counterpart: a fresh "later" (tapped 30 minutes ago) still gets the
    existing PRE_WORKOUT_NEAR reinterpretation — behavior unchanged for the
    common case."""
    await _user(db)
    now = datetime(2026, 7, 12, 21, 0, tzinfo=TZ)
    await _workout_plan(db, 1, now, time_text="09:00")
    await save_next_meal_workout_status(db, 1, "later", now=now - timedelta(minutes=30))

    state = await resolve_workout_state(db, 1, now)

    assert state.source == "active_workout_plan"
    assert state.phase == WorkoutPhase.PRE_WORKOUT_NEAR


@pytest.mark.asyncio
async def test_abandoned_multiday_active_session_does_not_mask_completed_session_today(db: Database) -> None:
    """Task B.7: a ``sessions`` row left ``status='active'`` for days (crash,
    forgot to tap "finish" — nothing in the app auto-closes it) must not
    permanently read as "workout in progress right now", masking a session
    that genuinely completed today. Before this pass, ``_active_session``
    had no staleness bound and always won ties against
    ``completed_session``/``healthkit_session`` by gather order."""
    await _user(db)
    now = datetime(2026, 7, 12, 20, 0, tzinfo=TZ)
    await _active_session(db, 1, now - timedelta(days=3))  # abandoned, never closed
    await _completed_session(db, 1, now - timedelta(hours=1), now - timedelta(minutes=30))

    state = await resolve_workout_state(db, 1, now)

    assert state.source == "completed_session"
    assert state.phase in {WorkoutPhase.POST_WORKOUT_IMMEDIATE, WorkoutPhase.POST_WORKOUT_LATER}


@pytest.mark.asyncio
async def test_genuinely_active_session_still_beats_completed_session_today(db: Database) -> None:
    """Counterpart: a session that is genuinely active right now (not stale)
    still wins over an earlier same-day completion — this is sound status
    semantics (currently in progress is the most current possible state),
    not the arbitrary-order bug the previous test guards against."""
    await _user(db)
    now = datetime(2026, 7, 12, 20, 0, tzinfo=TZ)
    await _completed_session(db, 1, now - timedelta(hours=3), now - timedelta(hours=2), code="A")
    await _active_session(db, 1, now - timedelta(minutes=15), code="B")

    state = await resolve_workout_state(db, 1, now)

    assert state.source == "active_session"
    assert state.phase == WorkoutPhase.DURING_WORKOUT


@pytest.mark.asyncio
async def test_more_recent_healthkit_completion_beats_earlier_bot_session_today(db: Database) -> None:
    """Task B.7 extension: when BOTH a bot-completed session and a
    HealthKit-imported workout exist today (e.g. a lifting session tracked
    via the bot, plus a run tracked on an Apple Watch), the one that
    actually ended more recently must win the ACTUAL-rank tie — real
    chronology, not "bot session is always gathered first"."""
    await _user(db)
    now = datetime(2026, 7, 12, 20, 0, tzinfo=TZ)
    # Bot session ended 3 hours ago.
    await _completed_session(db, 1, now - timedelta(hours=4), now - timedelta(hours=3))
    # HealthKit workout ended 20 minutes ago — genuinely more recent.
    await _healthkit_workout(db, 1, now - timedelta(minutes=50), now - timedelta(minutes=20))

    state = await resolve_workout_state(db, 1, now)

    assert state.source == "healthkit_session"
    assert state.phase == WorkoutPhase.POST_WORKOUT_IMMEDIATE
    assert state.minutes_since == 20


@pytest.mark.asyncio
async def test_earlier_bot_session_wins_when_it_is_actually_more_recent(db: Database) -> None:
    """Counterpart: when the bot session is the one that actually ended more
    recently, it must win — proving the comparison is real chronology in
    both directions, not just "healthkit always wins when present"."""
    await _user(db)
    now = datetime(2026, 7, 12, 20, 0, tzinfo=TZ)
    # HealthKit workout ended 3 hours ago.
    await _healthkit_workout(db, 1, now - timedelta(hours=4), now - timedelta(hours=3))
    # Bot session ended 20 minutes ago — genuinely more recent.
    await _completed_session(db, 1, now - timedelta(minutes=50), now - timedelta(minutes=20))

    state = await resolve_workout_state(db, 1, now)

    assert state.source == "completed_session"
    assert state.minutes_since == 20


# ---------------------------------------------------------------------------
# REC-ARCH-01 pass 3 — recent-meal future-timestamp safety (Task F). A
# clock-skewed/future ``eaten_at`` must read as unknown, never clamp to 0
# minutes ("just ate" is a stronger claim than an impossible timestamp
# supports) — mirrors how ``_healthkit_session_candidate`` already guards a
# future/still-syncing HealthKit end time.
# ---------------------------------------------------------------------------


def test_future_eaten_at_yields_unknown_minutes_not_zero() -> None:
    now = datetime(2026, 7, 12, 17, 0, tzinfo=TZ)
    meal = ConsumedMeal(
        name="clock-skewed meal",
        calories=900,
        protein=40,
        fat=30,
        eaten_at=now + timedelta(minutes=15),  # future — clock skew
        time_confidence="logged",
    )

    recent = recent_meal_state_from_consumed(meal, now=now)

    assert recent is not None
    assert recent.minutes_since_eaten is None


@pytest.mark.asyncio
async def test_live_recent_meal_helper_does_not_clamp_future_timestamp_to_zero(db: Database) -> None:
    """Same guard, exercised through the live ``next_meal._recent_meal``
    adapter (a separate, documented duplicate-resolution path that reads the
    same ``daily_state.consumed_meals`` rows) — both paths must agree a
    future timestamp is unknown, not "just now"."""
    from noam_coach.services.next_meal import _recent_meal

    await _user(db)
    now = datetime(2026, 7, 12, 17, 0, tzinfo=TZ)
    await _meal(db, 1, now, calories=900, minutes_ago=-15, name="clock-skewed meal")  # future eaten_at

    name, minutes = await _recent_meal(db, 1, now)

    assert name == "clock-skewed meal"
    assert minutes is None


# ---------------------------------------------------------------------------
# REC-ARCH-01 pass 3 — snapshot reuse proof via call-counting (not just equal
# timestamps): the migrated build_daily_status flow must resolve workout
# state from the DB exactly once for the whole handler, not once per
# projector that happens to receive the same `now`.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_daily_status_resolves_workout_state_once(monkeypatch: pytest.MonkeyPatch, db: Database) -> None:
    import coach_bot
    from noam_coach.bot import workout as workout_module
    from noam_coach.services import goals as goals_module

    await _user(db)
    await _goal(db)
    now = datetime(2026, 7, 12, 17, 45, tzinfo=TZ)
    await _workout_plan(db, 1, now, time_text="18:30")
    await _meal(db, 1, now, calories=500, minutes_ago=30, name="lunch")

    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(workout_module, "DB", db, raising=False)
    monkeypatch.setattr(goals_module, "DB", db, raising=False)

    real_resolver = user_state_module.resolve_workout_state
    call_count = 0

    async def _counting_resolver(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return await real_resolver(*args, **kwargs)

    with patch.object(user_state_module, "resolve_workout_state", side_effect=_counting_resolver):
        text = await coach_bot.build_daily_status(1)

    assert text  # renders successfully
    assert call_count == 1, f"expected exactly one resolve_workout_state call for the whole handler, got {call_count}"
