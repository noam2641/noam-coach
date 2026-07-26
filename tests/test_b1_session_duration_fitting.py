"""TASK-B1: the MAIN prescribed session must fit the user's resolved time budget.

Defect this pins: ``planning._schedule_sessions`` copied the full template
verbatim and ``_apply_strategy_volume`` adjusted sets by STRATEGY only, never
by time. The time-fitting engine was wired only to ``session["fast_version"]``.
A 30-minute user and a 90-minute user therefore received identical main-session
volume.

Acceptance covered here:
  * estimated duration of every MAIN session fits the requested minutes;
  * 30 / 45 / 90-minute plans differ materially when the original cannot fit;
  * fitting is trim-only (a plan that already fits is untouched, and a longer
    slot is never padded with extra volume);
  * volume is monotonic non-decreasing in the time budget;
  * primary movement patterns / compounds survive accessory trimming;
  * shared templates (``PLANS``) are never mutated in place.
"""
from __future__ import annotations

import copy
from typing import Any

import planning
import training_intelligence
from exercise_plans import PLANS

# A fully-equipped intermediate lifter: adaptation keeps the catalog exercises
# as prescribed, so what we measure is the DURATION fit and nothing else.
_GYM_FACTS: dict[str, dict[str, Any]] = {
    "equipment": {"value": "gym", "confirmed": True, "source": "user"},
    "training_location": {"value": "gym", "confirmed": True, "source": "user"},
    "strength_experience": {"value": "intermediate", "confirmed": True, "source": "user"},
}

_PRIMARY_MOVEMENTS = training_intelligence._PRIMARY_MOVEMENTS


def _candidate(minutes: int, *, strategy: str = "balanced", frequency: int = 3) -> Any:
    return planning._workout_candidate(
        "בדיקה",
        strategy,
        frequency,
        copy.deepcopy(_GYM_FACTS),
        score=0.9,
        rationale=[],
        tradeoffs=[],
        resolved_session_minutes=minutes,
        # Supplied so the quality gate does not trip on `missing_time`, which
        # is unrelated to duration fitting.
        resolved_preferred_time="18:00",
    )


def _sessions(minutes: int, **kwargs: Any) -> list[dict[str, Any]]:
    return _candidate(minutes, **kwargs).payload["sessions"]


def _total_sets(sessions: list[dict[str, Any]]) -> int:
    return sum(int(e.get("sets") or 0) for s in sessions for e in s["exercises"])


def _movements(session: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for exercise in session["exercises"]:
        profile = training_intelligence.CATALOG.get(str(exercise.get("id") or ""))
        if profile is not None:
            out.add(profile.movement)
    return out


# ---------------------------------------------------------------------------
# The duration model itself.
# ---------------------------------------------------------------------------


def test_duration_estimator_prices_sets_reps_and_rest() -> None:
    """The estimator is sets x (reps x tempo + rest) + ramp, plus fixed overhead."""
    exercise = {"id": "bench", "sets": 4, "rmin": 8, "rmax": 12, "rest": 120}
    # reps midpoint 10 -> 10 * 3s tempo + 120s rest = 150s/set; 4 sets = 600s,
    # plus the per-exercise warm-up ramp.
    expected = 4 * (10 * 3.0 + 120) + training_intelligence._WARMUP_SECONDS_PER_EXERCISE
    assert training_intelligence.estimate_exercise_seconds(exercise) == expected

    session = {"exercises": [exercise]}
    assert training_intelligence.estimate_session_minutes(session) == (
        training_intelligence._SESSION_OVERHEAD_SECONDS + expected
    ) / 60.0


def test_duration_estimator_handles_empty_and_malformed_entries() -> None:
    assert training_intelligence.estimate_session_minutes({"exercises": []}) == 0.0
    assert training_intelligence.estimate_session_minutes([]) == 0.0
    assert training_intelligence.estimate_exercise_seconds({"sets": 0}) == 0.0
    assert training_intelligence.estimate_exercise_seconds({"sets": "x"}) == 0.0


# ---------------------------------------------------------------------------
# ACCEPTANCE: estimated duration actually fits the requested minutes.
# ---------------------------------------------------------------------------


def test_main_sessions_fit_requested_minutes_at_30_45_and_90() -> None:
    """The MAIN session (not just fast_version) must fit the resolved budget."""
    tolerance = planning._DURATION_FIT_TOLERANCE_MINUTES
    for minutes in (30, 45, 90):
        for session in _sessions(minutes):
            estimated = training_intelligence.estimate_session_minutes(session)
            assert estimated <= minutes + tolerance, (
                f"{session['code']} at {minutes}min estimated {estimated:.1f}min"
            )
            # The payload advertises the same estimate the assertion used.
            assert session["estimated_minutes"] == round(estimated, 1)


def test_thirty_minute_plan_materially_differs_from_ninety_minute_plan() -> None:
    """The exact defect: a 30-min and a 90-min user used to get identical volume."""
    short = _sessions(30)
    long = _sessions(90)
    assert _total_sets(short) < _total_sets(long)
    short_shape = [[(e["id"], e["sets"]) for e in s["exercises"]] for s in short]
    long_shape = [[(e["id"], e["sets"]) for e in s["exercises"]] for s in long]
    assert short_shape != long_shape


def test_volume_is_monotonic_non_decreasing_in_the_time_budget() -> None:
    thirty = _total_sets(_sessions(30))
    forty_five = _total_sets(_sessions(45))
    ninety = _total_sets(_sessions(90))
    assert thirty <= forty_five <= ninety
    # And the constraint actually bites at the short end.
    assert thirty < ninety


# ---------------------------------------------------------------------------
# ACCEPTANCE: trim-only. Never pad, never touch a plan that already fits.
# ---------------------------------------------------------------------------


def test_a_session_that_already_fits_is_untouched() -> None:
    """Long-session behavior is preserved: no trimming, no padding."""
    ninety = _sessions(90)
    for session in ninety:
        assert session["estimated_minutes"] <= 90
    candidate = _candidate(90)
    fit = candidate.payload["plan_rationale"]["duration_fit"]
    assert fit["fitted"] is False
    assert fit["exercises_removed"] == 0
    assert fit["set_reductions"] == 0
    assert candidate.payload["duration_fit_audit"] == []


def test_longer_budget_never_adds_volume_beyond_the_template() -> None:
    """Fitting is trim-only: a 3-hour slot yields the same plan as a fitting one."""
    ninety = [[(e["id"], e["sets"]) for e in s["exercises"]] for s in _sessions(90)]
    huge = [[(e["id"], e["sets"]) for e in s["exercises"]] for s in _sessions(150)]
    assert ninety == huge


def test_fit_helper_returns_input_unchanged_when_it_already_fits() -> None:
    session = {
        "code": "X",
        "minutes": 60,
        "exercises": [{"id": "bench", "name": "b", "sets": 3, "rmin": 8, "rmax": 10, "rest": 90}],
    }
    fitted, changes = training_intelligence.fit_session_to_minutes(session, 60)
    assert changes == []
    assert fitted["exercises"] == session["exercises"]


# ---------------------------------------------------------------------------
# ACCEPTANCE: priority order — accessories go before compounds.
# ---------------------------------------------------------------------------


def test_primary_movement_coverage_is_preserved_under_a_tight_budget() -> None:
    """Compounds survive; the squat/press/pull patterns are not sacrificed."""
    for session in _sessions(30):
        assert session["exercises"], "fitting must never empty a session"
        assert _movements(session) & _PRIMARY_MOVEMENTS, (
            f"{session['code']} lost every primary movement pattern"
        )


def test_accessory_volume_is_trimmed_before_compound_volume() -> None:
    """Session A (bench/incline = compounds, fly/curl = accessories) at 30min."""
    session = next(s for s in _sessions(30) if s["code"] == "A")
    ids = [e["id"] for e in session["exercises"]]
    assert "bench" in ids, "the primary horizontal press must survive"
    # The isolation work (chest_isolation / elbow_flexion) is what gets cut.
    assert "fly" not in ids
    assert "incline_curl" not in ids


def test_fit_helper_reduces_accessory_sets_before_dropping_compounds() -> None:
    session = {
        "code": "X",
        "minutes": 30,
        "exercises": [
            {"id": "squat", "name": "squat", "sets": 4, "rmin": 8, "rmax": 10, "rest": 150},
            {"id": "lateral", "name": "lateral", "sets": 4, "rmin": 12, "rmax": 15, "rest": 60},
        ],
    }
    fitted, changes = training_intelligence.fit_session_to_minutes(session, 25)
    assert changes, "an over-budget session must be trimmed"
    ids = [e["id"] for e in fitted["exercises"]]
    assert "squat" in ids, "the compound must outlive the isolation exercise"
    # Accessory was hit first (reduced and/or removed) before any squat set.
    touched = [c["exercise"] for c in changes]
    assert touched[0] == "lateral"
    assert training_intelligence.estimate_session_minutes(fitted) <= 25


def test_fit_helper_never_empties_a_session() -> None:
    session = {
        "code": "X",
        "minutes": 5,
        "exercises": [
            {"id": "squat", "name": "squat", "sets": 5, "rmin": 8, "rmax": 10, "rest": 180},
            {"id": "lateral", "name": "lateral", "sets": 4, "rmin": 12, "rmax": 15, "rest": 60},
        ],
    }
    fitted, _ = training_intelligence.fit_session_to_minutes(session, 5)
    assert len(fitted["exercises"]) >= training_intelligence._MIN_FITTED_EXERCISES
    assert fitted["exercises"][0]["id"] == "squat"


# ---------------------------------------------------------------------------
# ACCEPTANCE: no shared-template mutation.
# ---------------------------------------------------------------------------


def test_building_sessions_never_mutates_the_shared_plans_template() -> None:
    before = copy.deepcopy(PLANS)
    for minutes in (30, 45, 90):
        for strategy in ("consistency", "balanced", "performance"):
            _sessions(minutes, strategy=strategy)
    assert PLANS == before, "PLANS (shared template) was mutated in place"


def test_building_twice_yields_identical_plans() -> None:
    """No cross-build state leaks through the shared templates."""
    first = [[(e["id"], e["sets"]) for e in s["exercises"]] for s in _sessions(30)]
    second = [[(e["id"], e["sets"]) for e in s["exercises"]] for s in _sessions(30)]
    assert first == second


def test_fit_helper_does_not_mutate_its_input_session() -> None:
    exercises = [
        {"id": "squat", "name": "squat", "sets": 4, "rmin": 8, "rmax": 10, "rest": 180},
        {"id": "lateral", "name": "lateral", "sets": 4, "rmin": 12, "rmax": 15, "rest": 60},
    ]
    session = {"code": "X", "minutes": 20, "exercises": exercises}
    snapshot = copy.deepcopy(session)
    training_intelligence.fit_session_to_minutes(session, 20)
    assert session == snapshot


# ---------------------------------------------------------------------------
# ACCEPTANCE: the rationale explains the requested duration and the adaptation.
# ---------------------------------------------------------------------------


def test_plan_rationale_states_requested_duration_and_how_it_was_adapted() -> None:
    candidate = _candidate(30)
    rationale = candidate.payload["plan_rationale"]
    assert rationale["requested_session_minutes"] == 30
    fit = rationale["duration_fit"]
    assert fit["requested_minutes"] == 30
    assert fit["fitted"] is True
    assert fit["method"] == "trim_only_accessory_first"
    assert fit["exercises_removed"] + fit["set_reductions"] > 0
    assert "30" in fit["summary"]
    assert fit["max_estimated_minutes"] <= 30
    # A user-visible assumption names the time constraint too.
    assert any("30" in note for note in candidate.assumptions)


def test_plan_rationale_says_nothing_was_cut_when_the_plan_already_fits() -> None:
    fit = _candidate(90).payload["plan_rationale"]["duration_fit"]
    assert fit["fitted"] is False
    assert "90" in fit["summary"]


def test_duration_fit_audit_records_each_trim() -> None:
    audit = _candidate(30).payload["duration_fit_audit"]
    assert audit, "trimming must be auditable"
    for entry in audit:
        assert entry["requested_minutes"] == 30
        assert entry["estimated_minutes"] <= 30
        assert entry["changes"]
        for change in entry["changes"]:
            assert change["reason"] == "time_budget"
            assert change["action"] in {"reduced_sets", "removed"}


# ---------------------------------------------------------------------------
# Unrelated fast-version behavior must be preserved.
# ---------------------------------------------------------------------------


def test_fast_version_still_present_and_capped() -> None:
    for session in _sessions(90):
        fast = session["fast_version"]
        assert fast["is_quick_version"] is True
        assert fast["minutes"] <= 25
        assert all(e["sets"] <= 3 for e in fast["exercises"])


def test_fitted_sessions_still_pass_the_workout_quality_gate() -> None:
    for minutes in (30, 45, 90):
        assert planning.workout_quality_issues(_candidate(minutes).payload) == []
