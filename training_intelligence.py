"""Deterministic training safety, equipment and fatigue intelligence.

The module keeps exercise selection and load decisions auditable.  AI may
explain a recommendation, but it never bypasses these rules.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ExerciseProfile:
    exercise_id: str
    movement: str
    primary_muscles: tuple[str, ...]
    equipment: tuple[str, ...]
    joint_load: tuple[str, ...] = ()
    skill: str = "beginner"
    avoid_tokens: tuple[str, ...] = ()


CATALOG: dict[str, ExerciseProfile] = {
    "bench": ExerciseProfile("bench", "horizontal_push", ("chest",), ("barbell", "bench"), ("shoulder",)),
    "incline_db": ExerciseProfile("incline_db", "horizontal_push", ("chest",), ("dumbbell", "bench"), ("shoulder",)),
    "fly": ExerciseProfile("fly", "chest_isolation", ("chest",), ("dumbbell", "bench"), ("shoulder",)),
    "lat_pull": ExerciseProfile("lat_pull", "vertical_pull", ("back",), ("cable",), ("shoulder",)),
    "lat_pull_fb": ExerciseProfile("lat_pull_fb", "vertical_pull", ("back",), ("cable",), ("shoulder",)),
    "one_arm_row": ExerciseProfile("one_arm_row", "horizontal_pull", ("back",), ("dumbbell", "bench"), ("back",)),
    "rope_push": ExerciseProfile("rope_push", "elbow_extension", ("triceps",), ("cable",)),
    "military": ExerciseProfile("military", "vertical_push", ("shoulder",), ("barbell",), ("shoulder", "back")),
    "squat": ExerciseProfile("squat", "squat", ("quads", "glutes"), ("barbell", "rack"), ("knee", "back"), "intermediate"),
    "leg_press": ExerciseProfile("leg_press", "squat", ("quads", "glutes"), ("machine",), ("knee",)),
    "rdl": ExerciseProfile("rdl", "hinge", ("hamstrings", "glutes"), ("barbell",), ("back",), "intermediate"),
    "lateral": ExerciseProfile("lateral", "shoulder_isolation", ("shoulder",), ("dumbbell",), ("shoulder",)),
    "bar_curl": ExerciseProfile("bar_curl", "elbow_flexion", ("biceps",), ("barbell",)),
    "incline_curl": ExerciseProfile("incline_curl", "elbow_flexion", ("biceps",), ("dumbbell", "bench")),
    "crossover": ExerciseProfile("crossover", "chest_isolation", ("chest",), ("cable",), ("shoulder",)),
    "chest_machine": ExerciseProfile("chest_machine", "horizontal_push", ("chest", "triceps"), ("machine",), ("shoulder",)),
    # Home/bodyweight replacements.
    "pushup": ExerciseProfile("pushup", "horizontal_push", ("chest",), ("bodyweight",), ("shoulder",)),
    "incline_pushup": ExerciseProfile("incline_pushup", "horizontal_push", ("chest",), ("bodyweight",), ("shoulder",)),
    "backpack_row": ExerciseProfile("backpack_row", "horizontal_pull", ("back",), ("household",), ("back",)),
    "band_row": ExerciseProfile("band_row", "horizontal_pull", ("back",), ("band",)),
    "glute_bridge": ExerciseProfile("glute_bridge", "hinge", ("glutes",), ("bodyweight",), ("back",)),
    "chair_squat": ExerciseProfile("chair_squat", "squat", ("quads", "glutes"), ("bodyweight",), ("knee",)),
    "wall_push": ExerciseProfile("wall_push", "horizontal_push", ("chest",), ("bodyweight",), ("shoulder",)),
    "dead_bug": ExerciseProfile("dead_bug", "core", ("core",), ("bodyweight",), ("back",)),
}


REPLACEMENTS: dict[str, list[dict[str, Any]]] = {
    "horizontal_push": [
        {"id": "pushup", "name": "שכיבות סמיכה", "weight": 0.0},
        {"id": "incline_pushup", "name": "שכיבות סמיכה בשיפוע", "weight": 0.0},
        {"id": "wall_push", "name": "דחיפה מול קיר", "weight": 0.0},
    ],
    "horizontal_pull": [
        {"id": "band_row", "name": "חתירה עם גומייה", "weight": 0.0},
        {"id": "backpack_row", "name": "חתירה עם תיק", "weight": 0.0},
    ],
    "vertical_pull": [
        {"id": "band_row", "name": "חתירה עם גומייה", "weight": 0.0},
        {"id": "backpack_row", "name": "חתירה עם תיק", "weight": 0.0},
    ],
    "squat": [
        {"id": "chair_squat", "name": "קימה מכיסא בטווח ללא כאב", "weight": 0.0},
        {"id": "glute_bridge", "name": "גשר ישבן", "weight": 0.0},
    ],
    "hinge": [
        {"id": "glute_bridge", "name": "גשר ישבן", "weight": 0.0},
        {"id": "dead_bug", "name": "Dead Bug", "weight": 0.0},
    ],
    "vertical_push": [
        {"id": "incline_pushup", "name": "שכיבות סמיכה בשיפוע", "weight": 0.0},
        {"id": "wall_push", "name": "דחיפה מול קיר", "weight": 0.0},
    ],
}


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.casefold()
    return json.dumps(value, ensure_ascii=False).casefold()


def normalize_equipment(value: Any, location: Any = None) -> set[str]:
    text = f"{_text(value)} {_text(location)}"
    available: set[str] = set()
    mappings = {
        "dumbbell": ("דאמבל", "משקולות", "dumbbell"),
        "barbell": ("מוט", "barbell"),
        "bench": ("ספסל", "bench"),
        "rack": ("כלוב", "rack"),
        "cable": ("כבל", "פולי", "cable"),
        "machine": ("מכונה", "machine"),
        "band": ("גומיה", "גומייה", "band"),
        "bodyweight": ("משקל גוף", "bodyweight", "בית", "home"),
        "household": ("תיק", "בית", "home"),
    }
    for key, tokens in mappings.items():
        if any(token in text for token in tokens):
            available.add(key)
    if any(token in text for token in ("חדר כושר", "gym", "מלא")):
        available.update({"dumbbell", "barbell", "bench", "rack", "cable", "machine", "bodyweight"})
    if not available:
        available.add("bodyweight")
    return available


def pain_regions(pain: Any, medical_avoidance: Any) -> set[str]:
    text = f"{_text(pain)} {_text(medical_avoidance)}"
    regions = set()
    mapping = {
        "knee": ("ברך", "knee"),
        "shoulder": ("כתף", "shoulder"),
        "back": ("גב", "back", "מותן"),
        "elbow": ("מרפק", "elbow"),
        "wrist": ("שורש כף", "wrist"),
        "hip": ("ירך", "מפשעה", "hip"),
    }
    for region, tokens in mapping.items():
        if any(token in text for token in tokens):
            regions.add(region)
    return regions


def exercise_allowed(
    exercise: dict[str, Any],
    *,
    equipment: set[str],
    pain: set[str],
    experience: str = "beginner",
) -> tuple[bool, list[str]]:
    profile = CATALOG.get(str(exercise.get("id")))
    if profile is None:
        return True, []
    reasons: list[str] = []
    if profile.equipment and not set(profile.equipment).intersection(equipment):
        reasons.append("ציוד לא זמין")
    if pain.intersection(profile.joint_load):
        reasons.append("עומס אפשרי על אזור כאב")
    if profile.skill == "intermediate" and experience in {"none", "unknown", "beginner", "מתחיל"}:
        reasons.append("דורש מיומנות שטרם אושרה")
    return not reasons, reasons


def _replacement_exercise(
    original: dict[str, Any],
    *,
    equipment: set[str],
    pain: set[str],
    experience: str,
) -> dict[str, Any] | None:
    profile = CATALOG.get(str(original.get("id")))
    if not profile:
        return None
    for replacement in REPLACEMENTS.get(profile.movement, []):
        replacement_profile = CATALOG.get(replacement["id"])
        if not replacement_profile:
            continue
        candidate = {
            **original,
            **replacement,
            "alts": [],
            "cues": ["טווח ללא כאב", "שליטה מלאה", "עצור אם הכאב מחמיר"],
            "muscle": original.get("muscle", ""),
        }
        allowed, _ = exercise_allowed(
            candidate,
            equipment=equipment,
            pain=pain,
            experience=experience,
        )
        if allowed:
            return candidate
    return None


def adapt_exercises(
    exercises: list[dict[str, Any]],
    *,
    equipment_value: Any,
    location: Any,
    pain_value: Any,
    medical_avoidance: Any,
    experience: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return safe/equipment-compatible exercises plus an audit trail."""
    equipment = normalize_equipment(equipment_value, location)
    pain = pain_regions(pain_value, medical_avoidance)
    adapted: list[dict[str, Any]] = []
    changes: list[dict[str, Any]] = []
    for original in exercises:
        allowed, reasons = exercise_allowed(
            original,
            equipment=equipment,
            pain=pain,
            experience=experience,
        )
        if allowed:
            adapted.append(original)
            continue
        replacement = _replacement_exercise(
            original,
            equipment=equipment,
            pain=pain,
            experience=experience,
        )
        changes.append(
            {
                "removed": original.get("name"),
                "reasons": reasons,
                "replacement": replacement.get("name") if replacement else None,
            }
        )
        if replacement:
            adapted.append(replacement)
    return adapted, changes


def warmup_sets(exercise: dict[str, Any], working_weight: float | None = None) -> list[dict[str, Any]]:
    weight = float(working_weight if working_weight is not None else exercise.get("weight") or 0)
    if weight <= 0:
        return [{"reps": 8, "weight": 0.0, "note": "חזרות הכנה קלות ובשליטה"}]
    sets = [
        {"reps": 8, "weight": round(weight * 0.4 / 2.5) * 2.5, "note": "חימום"},
        {"reps": 5, "weight": round(weight * 0.65 / 2.5) * 2.5, "note": "הכנה"},
    ]
    if weight >= 60:
        sets.append({"reps": 3, "weight": round(weight * 0.8 / 2.5) * 2.5, "note": "סט הכנה אחרון"})
    return sets


def quick_session(session: dict[str, Any], minutes: int) -> dict[str, Any]:
    """Keep the highest-value exercises that fit a short time window."""
    budget = max(10, int(minutes)) * 60
    chosen: list[dict[str, Any]] = []
    spent = 0
    for exercise in session.get("exercises", []):
        set_seconds = int(exercise.get("rest", 90)) + 45
        exercise_seconds = set_seconds * min(int(exercise.get("sets", 3)), 3)
        if chosen and spent + exercise_seconds > budget:
            continue
        copy = dict(exercise)
        copy["sets"] = min(int(copy.get("sets", 3)), 3)
        chosen.append(copy)
        spent += exercise_seconds
    return {
        **session,
        "minutes": minutes,
        "exercises": chosen,
        "is_quick_version": True,
    }


def epley_1rm(weight: float, reps: int) -> float:
    if weight <= 0 or reps <= 0:
        return 0.0
    return weight * (1 + reps / 30.0)


def workout_status(
    logged_sets: int,
    planned_sets: int,
    *,
    user_choice: str | None = None,
    duration_seconds: float | None = None,
) -> str:
    """Single source of truth for a session's terminal status.

    Returns ``"completed"`` only when every planned set was logged (or the user
    explicitly chose a full finish) AND the session actually lasted a non-trivial
    amount of time. A single set out of many, a zero/negative duration, or an
    explicit ``"partial"``/``"cancelled"`` choice can never read as completed.
    This guards against the recording bug where one set with ``duration: 0`` was
    saved as "האימון הושלם".
    """
    choice = (user_choice or "").strip().lower()
    if choice == "cancelled":
        return "cancelled"
    if logged_sets <= 0:
        return "cancelled" if choice == "cancelled" else "partial"
    # A non-positive duration means we never really ran the session.
    if duration_seconds is not None and duration_seconds <= 0:
        return "partial"
    if choice == "partial":
        return "partial"
    if choice == "completed" and logged_sets >= max(1, planned_sets):
        return "completed"
    return "completed" if logged_sets >= max(1, planned_sets) else "partial"


@dataclass
class FatigueAssessment:
    score: float
    plateau: bool
    deload_recommended: bool
    reasons: list[str] = field(default_factory=list)


def assess_fatigue(
    sessions: list[dict[str, Any]],
    *,
    sleep_quality: str | None = None,
    energy: str | None = None,
    soreness: int | None = None,
) -> FatigueAssessment:
    """Assess short-term fatigue from comparable sessions and check-in data."""
    score = 0.0
    reasons: list[str] = []
    hard = [row for row in sessions[-3:] if float(row.get("avg_rir", 3)) <= 1]
    if len(hard) >= 2:
        score += 0.35
        reasons.append("שני אימונים קשים לפחות")
    if sleep_quality in {"poor", "גרועה", "נמוכה"}:
        score += 0.25
        reasons.append("שינה חלשה")
    if energy in {"low", "נמוכה"}:
        score += 0.2
        reasons.append("אנרגיה נמוכה")
    if soreness is not None and soreness >= 7:
        score += 0.25
        reasons.append("כאבי שרירים גבוהים")

    e1rms = [float(row.get("e1rm", 0)) for row in sessions[-4:] if float(row.get("e1rm", 0)) > 0]
    plateau = len(e1rms) >= 3 and max(e1rms) <= min(e1rms) * 1.015
    if plateau:
        reasons.append("ללא שיפור מובהק בשלושה אימונים")
    score = min(1.0, round(score, 2))
    return FatigueAssessment(
        score=score,
        plateau=plateau,
        deload_recommended=score >= 0.65 or (plateau and len(hard) >= 2),
        reasons=reasons,
    )


def readiness_score(
    *,
    sleep_quality: str | None,
    energy: str | None,
    pain_severity: int | None,
    resting_hr_delta_pct: float | None = None,
    hrv_delta_pct: float | None = None,
) -> dict[str, Any]:
    """Transparent daily readiness score; never a medical diagnosis."""
    score = 100.0
    reasons: list[str] = []
    if sleep_quality in {"poor", "גרועה", "נמוכה"}:
        score -= 20
        reasons.append("שינה חלשה")
    elif sleep_quality in {"average", "סבירה"}:
        score -= 8
    if energy in {"low", "נמוכה"}:
        score -= 18
        reasons.append("אנרגיה נמוכה")
    if pain_severity is not None:
        score -= min(35, max(0, pain_severity) * 4)
        if pain_severity >= 5:
            reasons.append("כאב מדווח")
    if resting_hr_delta_pct is not None and resting_hr_delta_pct > 8:
        score -= 12
        reasons.append("דופק מנוחה גבוה מהרגיל")
    if hrv_delta_pct is not None and hrv_delta_pct < -15:
        score -= 10
        reasons.append("HRV נמוך מהרגיל")
    score = max(0, min(100, round(score)))
    recommendation = "רגיל"
    if score < 45:
        recommendation = "מנוחה או אימון קל"
    elif score < 70:
        recommendation = "שמור עומס ואל תתקדם בכוח"
    return {"score": score, "recommendation": recommendation, "reasons": reasons}
