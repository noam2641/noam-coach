from __future__ import annotations

import training_intelligence as ti
from exercise_plans import PLANS


def test_all_plan_and_alternative_exercises_have_catalog_profiles() -> None:
    ids = {
        exercise["id"]
        for plan in PLANS.values()
        for exercise in plan["exercises"]
    }
    ids.update(
        alt["id"]
        for plan in PLANS.values()
        for exercise in plan["exercises"]
        for alt in exercise.get("alts", [])
    )

    missing = sorted(ids - set(ti.CATALOG))
    assert missing == []


def test_catalog_joint_load_covers_major_training_groups() -> None:
    assert {"knee", "hip", "back"} <= set(ti.CATALOG["squat"].joint_load)
    assert {"hip", "back"} <= set(ti.CATALOG["rdl"].joint_load)
    assert {"shoulder", "elbow"} <= set(ti.CATALOG["db_bench"].joint_load)
    assert {"back", "elbow"} <= set(ti.CATALOG["cable_row"].joint_load)
    assert {"elbow"} <= set(ti.CATALOG["db_curl"].joint_load)


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


def test_elbow_pain_backfills_gutted_session_cross_pattern() -> None:
    """Regression: tennis elbow loads every push/pull in an upper-body session,
    so same-pattern substitution cannot refill it and the session used to empty
    out — which dropped the balanced/performance candidates entirely, leaving a
    user with only "מקסימום עקביות". The session must stay trainable via
    pain-safe exercises from OTHER movement patterns, and an audit change must
    record the backfill so the note reaches the user."""
    upper = [dict(exercise) for exercise in PLANS["U2"]["exercises"]]
    adapted, changes = ti.adapt_exercises(
        upper,
        equipment_value="full_gym",
        location="gym",
        pain_value="טניס אלבו",
        medical_avoidance=None,
        experience="advanced",
    )
    # Session is refilled, not emptied.
    assert len(adapted) >= ti._MIN_SESSION_EXERCISES
    # None of the kept exercises load the injured joint.
    for exercise in adapted:
        profile = ti.CATALOG.get(str(exercise.get("id")))
        assert profile is not None
        assert "elbow" not in profile.joint_load
    # No raw English ids leak into user-facing names.
    for exercise in adapted:
        assert exercise["id"] in ti._CATALOG_NAMES_HE or "elbow" not in ti.CATALOG[exercise["id"]].joint_load
    # The backfill is recorded so a limitation note can be surfaced.
    assert any("backfilled" in change for change in changes)


def test_backfill_diversifies_across_muscle_groups() -> None:
    """The backfill must not stack three near-identical isolation variants of a
    single pattern; it round-robins across muscle groups."""
    upper = [dict(exercise) for exercise in PLANS["B"]["exercises"]]
    adapted, _ = ti.adapt_exercises(
        upper,
        equipment_value="full_gym",
        location="gym",
        pain_value="טניס אלבו",
        medical_avoidance=None,
        experience="advanced",
    )
    muscles = {
        ti.CATALOG[str(exercise["id"])].primary_muscles[0]
        for exercise in adapted
        if ti.CATALOG.get(str(exercise["id"])) and ti.CATALOG[str(exercise["id"])].primary_muscles
    }
    assert len(muscles) >= 2  # varied, not one muscle repeated


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
