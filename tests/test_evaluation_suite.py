"""The offline evaluation suite must stay green and broad.

This guards the deterministic P0 behaviors that the recording exposed: the
zero-meal gate, locked quantities, single-set workout status, fatigue/deload,
goal provisionality and duplicate detection. A regression in any of them fails
the suite here as well as in `scripts/run_evaluations.py`.
"""

from __future__ import annotations

from pathlib import Path

import training_intelligence
from evaluation import load_cases, run_evaluations

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "evaluations" / "core_cases.jsonl"


def test_all_core_cases_pass() -> None:
    report = run_evaluations(load_cases(CASES))
    failed = [r for r in report["results"] if not r["passed"]]
    assert report["failed"] == 0, f"failing eval cases: {failed}"


def test_suite_covers_many_categories() -> None:
    report = run_evaluations(load_cases(CASES))
    # Guard against the suite silently shrinking back to a couple of cases.
    assert report["total"] >= 25
    assert len(report["by_category"]) >= 8
    required = {
        "locked_quantity",
        "meaningful_meal",
        "workout_status",
        "fatigue",
        "goal_provisional",
        "duplicate_detected",
    }
    assert required.issubset(report["by_category"].keys())


def test_workout_status_single_set_is_partial() -> None:
    # The recording bug: one set, zero duration, marked "completed".
    assert training_intelligence.workout_status(1, 12, duration_seconds=0) == "partial"
    assert training_intelligence.workout_status(1, 12, duration_seconds=120) == "partial"


def test_workout_status_all_sets_completed() -> None:
    assert training_intelligence.workout_status(12, 12, duration_seconds=3000) == "completed"


def test_workout_status_explicit_partial_overrides_full_sets() -> None:
    assert (
        training_intelligence.workout_status(12, 12, user_choice="partial", duration_seconds=3000)
        == "partial"
    )


def test_workout_status_cancelled() -> None:
    assert (
        training_intelligence.workout_status(3, 12, user_choice="cancelled", duration_seconds=300)
        == "cancelled"
    )


def test_workout_status_no_sets_never_completed() -> None:
    assert training_intelligence.workout_status(0, 12, duration_seconds=600) == "partial"
