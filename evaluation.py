"""Offline regression evaluation for deterministic high-risk behavior.

Cases are JSONL records.  The runner intentionally works without network
access, so it can run in CI for every release.  Every case asserts a concrete
deterministic property of a real domain function — not a brittle text match —
so a regression in the P0 logic fails the release.  AI-backed golden datasets
can be added later without changing the report format.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

import meal_intelligence
import targets as targets_mod
import training_intelligence
from models import FoodItem, MealAnalysis


@dataclass(frozen=True)
class EvaluationResult:
    case_id: str
    category: str
    passed: bool
    message: str
    metrics: dict[str, Any]


def _meal_from_payload(payload: dict[str, Any]) -> MealAnalysis:
    return MealAnalysis(
        meal_name=str(payload.get("meal_name", "evaluation meal")),
        items=[FoodItem(**item) for item in payload.get("items", [])],
        confidence=float(payload.get("confidence", 0.8)),
        question=payload.get("question"),
        options=payload.get("options", []),
    )


def _result(case_id: str, category: str, passed: bool, message: str, **metrics: Any) -> EvaluationResult:
    return EvaluationResult(case_id, category, passed, message, metrics)


def evaluate_case(case: dict[str, Any]) -> EvaluationResult:
    case_id = str(case.get("id", "unnamed"))
    category = str(case.get("category", "unknown"))

    if category == "locked_quantity":
        analysis = _meal_from_payload(case["analysis"])
        constraints = meal_intelligence.parse_locked_quantities(str(case["text"]))
        corrected, _unmatched = meal_intelligence.apply_locked_quantities(analysis, constraints)
        expected = float(case["expected_grams"])
        item = next((i for i in corrected.items if case.get("item", "") in i.name), None)
        actual = float(item.grams) if item else -1
        passed = item is not None and abs(actual - expected) < 0.01
        return _result(
            case_id, category, passed,
            f"expected {expected}, actual {actual}",
            expected_grams=expected, actual_grams=actual,
        )

    if category == "image_distance":
        distance = meal_intelligence.hamming_distance_hex(str(case["left"]), str(case["right"]))
        maximum = int(case.get("max_distance", 8))
        return _result(
            case_id, category, distance <= maximum,
            f"distance={distance}, max={maximum}",
            distance=distance, max_distance=maximum,
        )

    if category == "duplicate_detected":
        # A near-identical perceptual hash within tolerance is treated as a
        # duplicate; a different meal must not be.
        distance = meal_intelligence.hamming_distance_hex(str(case["left"]), str(case["right"]))
        maximum = int(case.get("max_distance", 8))
        is_dup = distance <= maximum
        expected = bool(case["expect_duplicate"])
        return _result(
            case_id, category, is_dup == expected,
            f"duplicate={is_dup}, expected={expected} (distance={distance})",
            distance=distance, max_distance=maximum, is_duplicate=is_dup,
        )

    if category == "meaningful_meal":
        # The zero-meal gate: mis-routed text like "לא אלכוהול" produces an
        # empty / all-zero meal which is NOT meaningful and must not be saved.
        analysis = _meal_from_payload(case["analysis"])
        actual = analysis.is_meaningful()
        expected = bool(case["expect_meaningful"])
        return _result(
            case_id, category, actual == expected,
            f"meaningful={actual}, expected={expected}",
            is_meaningful=actual,
        )

    if category == "macro_consistency":
        # An item whose declared calories diverge wildly from its macro sum has
        # its confidence forced down so the approval flow stops auto-save.
        item = FoodItem(**case["item"])
        max_conf = float(case["max_confidence"])
        passed = item.confidence <= max_conf
        return _result(
            case_id, category, passed,
            f"confidence={item.confidence}, max={max_conf}",
            confidence=item.confidence, max_confidence=max_conf,
        )

    if category == "cooked_raw":
        constraints = meal_intelligence.parse_locked_quantities(str(case["text"]))
        actual = meal_intelligence.requires_cooked_raw_clarification(constraints)
        expected = bool(case["expect_clarification"])
        return _result(
            case_id, category, actual == expected,
            f"clarify={actual}, expected={expected}",
            requires_clarification=actual,
        )

    if category == "workout_status":
        actual = training_intelligence.workout_status(
            int(case["logged_sets"]),
            int(case["planned_sets"]),
            user_choice=case.get("user_choice"),
            duration_seconds=case.get("duration_seconds"),
        )
        expected = str(case["expect_status"])
        return _result(
            case_id, category, actual == expected,
            f"status={actual}, expected={expected}",
            status=actual,
        )

    if category == "fatigue":
        assessment = training_intelligence.assess_fatigue(
            list(case.get("sessions", [])),
            sleep_quality=case.get("sleep_quality"),
            energy=case.get("energy"),
            soreness=case.get("soreness"),
        )
        expected = bool(case["expect_deload"])
        return _result(
            case_id, category, assessment.deload_recommended == expected,
            f"deload={assessment.deload_recommended}, expected={expected} (score={assessment.score})",
            score=assessment.score, deload=assessment.deload_recommended, plateau=assessment.plateau,
        )

    if category == "readiness":
        result = training_intelligence.readiness_score(
            sleep_quality=case.get("sleep_quality"),
            energy=case.get("energy"),
            pain_severity=case.get("pain_severity"),
            resting_hr_delta_pct=case.get("resting_hr_delta_pct"),
            hrv_delta_pct=case.get("hrv_delta_pct"),
        )
        lo = int(case.get("min_score", 0))
        hi = int(case.get("max_score", 100))
        score = int(result["score"])
        passed = lo <= score <= hi
        return _result(
            case_id, category, passed,
            f"score={score}, expected {lo}-{hi}",
            score=score, min_score=lo, max_score=hi,
        )

    if category == "goal_provisional":
        # A computed target with mandatory inputs missing must be provisional;
        # complete inputs must yield a non-provisional, safe target.
        result = targets_mod.compute_targets(
            float(case["weight_kg"]),
            avg_steps=case.get("avg_steps"),
            goal_type=case.get("goal_type", "fat_loss_muscle_retention"),
            sex=case.get("sex"),
            height_cm=case.get("height_cm"),
            age=case.get("age"),
            workouts_per_week=case.get("workouts_per_week"),
        )
        expected = bool(case["expect_provisional"])
        floor_ok = result.calories >= targets_mod.MIN_CALORIES and result.protein >= targets_mod.MIN_PROTEIN
        passed = result.provisional == expected and floor_ok
        return _result(
            case_id, category, passed,
            f"provisional={result.provisional}, expected={expected}; "
            f"calories={result.calories}, protein={result.protein}",
            provisional=result.provisional, calories=result.calories, protein=result.protein,
        )

    if category == "epley_1rm":
        actual = round(training_intelligence.epley_1rm(float(case["weight"]), int(case["reps"])), 1)
        expected = round(float(case["expected"]), 1)
        return _result(
            case_id, category, abs(actual - expected) < 0.5,
            f"e1rm={actual}, expected={expected}",
            e1rm=actual, expected=expected,
        )

    return EvaluationResult(case_id, category, False, "unsupported category", {})


def load_cases(path: str | Path) -> list[dict[str, Any]]:
    records = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            records.append(json.loads(line))
    return records


def run_evaluations(cases: Iterable[dict[str, Any]]) -> dict[str, Any]:
    results = [evaluate_case(case) for case in cases]
    passed = sum(1 for result in results if result.passed)
    by_category: dict[str, dict[str, int]] = {}
    for result in results:
        bucket = by_category.setdefault(result.category, {"passed": 0, "failed": 0})
        bucket["passed" if result.passed else "failed"] += 1
    return {
        "total": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "pass_rate": round(passed / len(results), 3) if results else 0.0,
        "by_category": by_category,
        "results": [asdict(result) for result in results],
    }
