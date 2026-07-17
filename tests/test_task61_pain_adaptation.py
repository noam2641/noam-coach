"""TASK-61 — pain-aware exercise substitution and load adjustment.

Pins the approved decisions: DECISION 1 (mechanical demand metadata),
DECISION 2 (one-axis reduction, never below the increment), DECISION 3
(high severity never keeps a loading exercise; REPLACE, else OMIT),
DECISION 4 (short non-diagnostic explanations) — and the source incident:
a tennis-elbow limitation must adapt arm/upper work in place instead of
gutting the session and backfilling it with unrelated leg exercises.
"""

from __future__ import annotations

from typing import Any

import pytest

import training_intelligence as ti
from training_intelligence import (
    ADAPTATION_EXPLANATIONS_HE,
    CATALOG,
    HIGH_SEVERITY_MIN,
    ActivePainRegion,
    adapt_exercises,
    apply_load_reduction,
    decide_pain_adaptation,
    region_load_level,
)

GYM_EQUIPMENT = "חדר כושר מאובזר: מוטות, משקולות, מכונות, כבלים, ספסלים"


def _region(region: str, severity: int | None = None) -> ActivePainRegion:
    return ActivePainRegion(region=region, label=region, severity=severity, age_days=1.0)


def _exercise(exercise_id: str, **overrides: Any) -> dict[str, Any]:
    base = {
        "id": exercise_id, "name": exercise_id, "sets": 4, "rmin": 8, "rmax": 12,
        "rest": 120, "weight": 40.0, "inc": 2.5, "cues": [], "alts": [], "muscle": "",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# DECISION 1 — mechanical demand metadata
# ---------------------------------------------------------------------------


def test_demand_metadata_values_are_valid_across_the_catalog() -> None:
    for exercise_id, profile in CATALOG.items():
        assert profile.grip_demand in ("none", "low", "high_static", "high_dynamic"), exercise_id
        assert profile.elbow_flexion_load in ("none", "low", "high"), exercise_id
        assert profile.wrist_load in ("none", "low", "high"), exercise_id


def test_demand_metadata_distinguishes_pull_variants() -> None:
    assert CATALOG["lat_pull"].grip_demand == "high_dynamic"
    assert CATALOG["neutral_pull"].grip_demand == "high_static"
    assert CATALOG["squat"].grip_demand == "none"


def test_region_load_levels() -> None:
    # Pronated pull: dynamic grip → replace-level for the elbow.
    assert region_load_level(CATALOG["lat_pull"], "elbow") == "replace"
    # Neutral pull: static grip → reduce-level (the gentler same-pattern target).
    assert region_load_level(CATALOG["neutral_pull"], "elbow") == "reduce"
    # Press: elbow in joint_load but low grip/flexion → reduce, not replace.
    assert region_load_level(CATALOG["bench"], "elbow") == "reduce"
    # Legs never load the elbow.
    assert region_load_level(CATALOG["squat"], "elbow") == "none"
    assert region_load_level(CATALOG["leg_press"], "elbow") == "none"
    # Weight-bearing joints keep the accepted binary semantics.
    assert region_load_level(CATALOG["squat"], "knee") == "replace"
    assert region_load_level(CATALOG["bench"], "shoulder") == "replace"
    # Barbell curl also loads the wrist.
    assert region_load_level(CATALOG["bar_curl"], "wrist") == "replace"


# ---------------------------------------------------------------------------
# DECISION 2 — one-axis reduction, increment floor
# ---------------------------------------------------------------------------


def test_load_reduction_snaps_to_increment_and_keeps_sets() -> None:
    reduced = apply_load_reduction(_exercise("rdl", weight=60.0, inc=2.5, sets=4))
    assert reduced["weight"] == 35.0  # 60*0.6=36 → snapped down to 2.5s
    assert reduced["sets"] == 4       # exactly ONE axis changed
    assert 0.3 <= 1 - reduced["weight"] / 60.0 <= 0.5


def test_load_reduction_never_goes_below_one_increment() -> None:
    reduced = apply_load_reduction(_exercise("rdl", weight=3.0, inc=2.5))
    assert reduced["weight"] >= 2.5


def test_bodyweight_reduction_uses_the_volume_axis() -> None:
    reduced = apply_load_reduction(_exercise("pushup", weight=0.0, sets=4))
    assert reduced["sets"] == 3
    assert reduced.get("weight", 0.0) == 0.0  # the load axis untouched
    floor = apply_load_reduction(_exercise("pushup", weight=0.0, sets=2))
    assert floor["sets"] == 2  # never below the minimum


# ---------------------------------------------------------------------------
# DECISION 3 — high severity never keeps a loading exercise
# ---------------------------------------------------------------------------


def test_high_severity_escalates_past_keep_and_reduce() -> None:
    regions = {"elbow": _region("elbow", HIGH_SEVERITY_MIN)}
    # Even a reduce-level press must not be kept at high severity.
    operation, trigger = decide_pain_adaptation(CATALOG["bench"], regions)
    assert operation == "omit_if_unreplaceable" and trigger == "elbow"
    # An exercise that does not load the region at all stays kept.
    operation, _ = decide_pain_adaptation(CATALOG["squat"], regions)
    assert operation == "keep"


def test_high_severity_omits_when_no_clean_replacement_exists(
) -> None:
    regions = {"elbow": _region("elbow", 9)}
    lat_pull = _exercise(
        "lat_pull",
        alts=[{"id": "neutral_pull", "name": "פולי ניטרלי", "weight": 45.0}],
    )
    adapted, changes = adapt_exercises(
        [lat_pull],
        equipment_value=GYM_EQUIPMENT, location=None,
        pain_value="", medical_avoidance="",
        experience="beginner", pain_detail=regions,
    )
    # neutral_pull still loads the elbow at reduce-level → not clean → the
    # exercise is OMITTED, never kept (DECISION 3).
    assert adapted == []
    assert changes and changes[0]["replacement"] is None
    assert ADAPTATION_EXPLANATIONS_HE["omit"] in changes[0]["reasons"]


# ---------------------------------------------------------------------------
# The source incident — tennis elbow keeps the session's intent
# ---------------------------------------------------------------------------


def _upper_session() -> list[dict[str, Any]]:
    return [
        _exercise("bench", name="לחיצת חזה", weight=50.0,
                  alts=[{"id": "chest_machine", "name": "Chest Press", "weight": 55.0}]),
        _exercise("lat_pull", name="משיכה רחבה", weight=45.0,
                  alts=[{"id": "neutral_pull", "name": "פולי ניטרלי", "weight": 45.0}]),
        _exercise("bar_curl", name="כפיפת מרפקים במוט", weight=25.0,
                  alts=[{"id": "cable_curl", "name": "כפיפה בכבל", "weight": 20.0}]),
    ]


def test_tennis_elbow_adapts_in_place_without_leg_substitution() -> None:
    """The incident: an elbow limitation must not empty the upper session and
    refill it with squats — presses reduce, pulls modify to the neutral
    variant, curls modify to the cable variant, and no leg exercise appears."""
    regions = {"elbow": _region("elbow", severity=4)}
    adapted, changes = adapt_exercises(
        _upper_session(),
        equipment_value=GYM_EQUIPMENT, location=None,
        pain_value="כאב מרפק (טניס אלבו)", medical_avoidance="",
        experience="beginner", pain_detail=regions,
    )

    ids = [item["id"] for item in adapted]
    assert len(adapted) == 3  # the session keeps its size — no gutting
    # The press stayed, load-reduced (one axis).
    bench = next(item for item in adapted if item["id"] == "bench")
    assert bench["weight"] == 30.0  # 50*0.6 → snapped to 2.5
    assert bench["sets"] == 4
    # The pronated pull became its neutral-grip same-pattern variant.
    assert "neutral_pull" in ids
    # The barbell curl became the lower-demand cable variant, load-reduced.
    assert "cable_curl" in ids
    # No unrelated leg work invaded the upper session.
    leg_ids = {"squat", "leg_press", "hack", "goblet", "smith_squat", "chair_squat"}
    assert not leg_ids.intersection(ids)


def test_backfill_never_reintroduces_reduce_level_load_on_the_painful_region() -> None:
    """A gutted-session backfill must be fully clean for the active region:
    rdl/goblet carry a static grip (reduce-level for the elbow) and were
    historically let back in by the binary joint_load check."""
    candidates = ti._pain_safe_backfill_candidates(
        equipment=ti.normalize_equipment(GYM_EQUIPMENT, None),
        pain={"elbow"},
        experience="intermediate",
        present_ids=set(),
        regions=["elbow"],
    )
    assert candidates  # leg/core/isolation work is still available
    for item in candidates:
        profile = CATALOG[item["id"]]
        assert region_load_level(profile, "elbow") == "none", item["id"]


def test_knee_pain_keeps_accepted_replace_semantics_for_legs() -> None:
    regions = {"knee": _region("knee", severity=4)}
    squat = _exercise("squat", name="סקוואט", weight=60.0, alts=[])
    adapted, changes = adapt_exercises(
        [squat],
        equipment_value=GYM_EQUIPMENT, location=None,
        pain_value="כאב ברך", medical_avoidance="",
        experience="intermediate", pain_detail=regions,
    )
    # Direct knee load → the accepted replace-or-drop path (never a silent keep).
    assert all(item["id"] != "squat" for item in adapted)
    assert changes and changes[0].get("removed") == "סקוואט"


def test_upper_work_untouched_by_knee_pain() -> None:
    regions = {"knee": _region("knee", severity=5)}
    adapted, changes = adapt_exercises(
        _upper_session(),
        equipment_value=GYM_EQUIPMENT, location=None,
        pain_value="כאב ברך", medical_avoidance="",
        experience="beginner", pain_detail=regions,
    )
    assert [item["id"] for item in adapted] == ["bench", "lat_pull", "bar_curl"]
    assert changes == []


# ---------------------------------------------------------------------------
# DECISION 4 — explanations
# ---------------------------------------------------------------------------


def test_explanations_are_short_and_non_diagnostic() -> None:
    banned = ("אבחנה", "דלקת", "פציעה", "רופא קבע", "טיפול רפואי")
    for key, text in ADAPTATION_EXPLANATIONS_HE.items():
        assert len(text) <= 120, key
        assert not any(term in text for term in banned), key


def test_adapted_exercises_carry_their_explanation() -> None:
    regions = {"elbow": _region("elbow", severity=3)}
    adapted, changes = adapt_exercises(
        _upper_session(),
        equipment_value=GYM_EQUIPMENT, location=None,
        pain_value="כאב מרפק", medical_avoidance="",
        experience="beginner", pain_detail=regions,
    )
    for item in adapted:
        assert item.get("adaptation_note"), item["id"]
    for change in changes:
        assert change["reasons"] and all(len(reason) <= 120 for reason in change["reasons"])


# ---------------------------------------------------------------------------
# Plan-generation integration (severity plumbing)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_generation_threads_severity_into_adaptation(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: a stored high-severity elbow constraint reaches the
    adaptation engine through build_workout_candidates."""
    from pathlib import Path

    import coach_bot
    import planning
    import user_model
    from db import Database
    from helpers import utc_now

    db = Database(str(Path(tmp_path) / "task61.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1, 'Test', NULL, ?)",
        (utc_now(),),
    )
    monkeypatch.setattr(coach_bot, "DB", db)
    for key, value in {
        "primary_goal": "fat_loss_muscle_retention", "weight_kg": 80,
        "height_cm": 178, "sex": "male", "age": 30,
        "equipment": "full", "training_location": "gym",
        "strength_experience": "intermediate",
        "training_days_per_week": 3, "session_minutes": 60,
        "weekly_availability": [
            {"weekday": 0, "available": True, "minutes": 60},
            {"weekday": 2, "available": True, "minutes": 60},
            {"weekday": 4, "available": True, "minutes": 60},
        ],
        "diet_restrictions": "none", "allergies": "none",
        "active_pain": "כאב מרפק", "medical_avoidance": "none",
        "training_limitations": "כאב מרפק",
    }.items():
        await user_model.set_fact(
            db, 1, key, value,
            kind=user_model.KIND_FACT, source=user_model.SOURCE_USER, confirmed=True,
        )
    await db.execute(
        """
        INSERT INTO medical_constraints(user_id, kind, location, severity, status, note, affects, created_at)
        VALUES(1, 'pain', 'מרפק', 8, 'active', 'טניס אלבו', 'exercise_selection', ?)
        """,
        (utc_now(),),
    )

    candidates = await planning.build_workout_candidates(db, 1)
    assert candidates
    for candidate in candidates:
        for session in candidate.payload["sessions"]:
            ids = {str(item.get("id")) for item in session["exercises"]}
            # DECISION 3 at severity 8: nothing elbow-loading may remain.
            for exercise_id in ids:
                profile = ti.CATALOG.get(exercise_id)
                if profile is None:
                    continue
                assert region_load_level(profile, "elbow") == "none", (
                    candidate.title, session["name"], exercise_id,
                )
