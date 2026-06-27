from __future__ import annotations

import training_intelligence as ti


def test_knee_pain_replaces_knee_loaded_exercise() -> None:
    exercises = [{"id": "squat", "name": "סקוואט", "sets": 3, "rest": 120, "weight": 40}]
    adapted, changes = ti.adapt_exercises(
        exercises,
        equipment_value="חדר כושר מלא",
        location="חדר כושר",
        pain_value="כאב בברך ימין",
        medical_avoidance="",
        experience="intermediate",
    )
    assert changes
    assert all(exercise["id"] != "squat" for exercise in adapted)


def test_home_equipment_removes_barbell_dependency() -> None:
    exercises = [{"id": "bench", "name": "לחיצת חזה", "sets": 3, "rest": 90, "weight": 50}]
    adapted, _ = ti.adapt_exercises(
        exercises,
        equipment_value="משקל גוף בלבד",
        location="בית",
        pain_value="none",
        medical_avoidance="none",
        experience="beginner",
    )
    assert adapted
    assert adapted[0]["id"] in {"pushup", "incline_pushup", "wall_push"}


def test_warmup_and_quick_session_are_deterministic() -> None:
    exercise = {"id": "bench", "sets": 4, "rest": 90, "weight": 80}
    assert len(ti.warmup_sets(exercise)) == 3
    session = {"minutes": 60, "exercises": [exercise, {**exercise, "id": "lat_pull"}]}
    quick = ti.quick_session(session, 15)
    assert quick["is_quick_version"] is True
    assert quick["minutes"] == 15
    assert quick["exercises"]


def test_fatigue_can_recommend_deload() -> None:
    assessment = ti.assess_fatigue(
        [{"avg_rir": 0, "e1rm": 100}, {"avg_rir": 1, "e1rm": 100}, {"avg_rir": 0, "e1rm": 100}],
        sleep_quality="poor",
        energy="low",
        soreness=8,
    )
    assert assessment.deload_recommended is True
    assert assessment.plateau is True


def test_readiness_score_never_diagnoses_but_reduces_load_recommendation() -> None:
    result = ti.readiness_score(
        sleep_quality="poor",
        energy="low",
        pain_severity=7,
        resting_hr_delta_pct=10,
        hrv_delta_pct=-20,
    )
    assert 0 <= result["score"] <= 100
    assert result["recommendation"] == "מנוחה או אימון קל"
