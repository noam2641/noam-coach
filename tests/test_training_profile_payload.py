from __future__ import annotations

import training_intelligence


def _fact(value):
    return {"value": value, "kind": "fact"}


def test_client_training_profile_from_facts_is_structured() -> None:
    facts = {
        "age": _fact(32),
        "height_cm": _fact(174),
        "weight_kg": _fact(101.8),
        "goal_weight_kg": _fact(83),
        "primary_goal": _fact("fat_loss_muscle_retention"),
        "strength_experience": _fact("intermediate"),
        "training_location": _fact("gym"),
        "equipment": _fact("full gym"),
        "active_pain": _fact({"location": "knee", "status": "active"}),
        "session_minutes": _fact(50),
    }

    profile = training_intelligence.client_training_profile_from_facts(
        facts,
        user_id=7,
        available_days_per_week=4,
        preferred_training_days=[0, 2, 4, 6],
        time_per_workout_minutes=50,
    ).public_payload()

    assert profile["user_id"] == 7
    assert profile["height"] == 174
    assert profile["current_weight"] == 101.8
    assert profile["target_weight"] == 83
    assert profile["goal_type"] == "fat_loss"
    assert profile["training_experience"] == "intermediate"
    assert profile["available_days_per_week"] == 4
    assert profile["preferred_training_days"] == [0, 2, 4, 6]
    assert "machine" in profile["available_equipment"]
    assert "knee" in profile["pain_areas"]


def test_exercise_catalog_entry_exposes_required_professional_fields() -> None:
    entry = training_intelligence.exercise_catalog_entry("leg_press")

    assert entry is not None
    assert entry["movement_pattern"] == "squat"
    assert "quads" in entry["primary_muscles"]
    assert "equipment_required" in entry
    assert "difficulty_level" in entry
    assert "coaching_cues" in entry
    assert "common_mistakes" in entry
    assert "safe_range_notes" in entry
    assert "substitutions" in entry
