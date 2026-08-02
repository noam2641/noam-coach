"""Deterministic training safety, equipment and fatigue intelligence.

The module keeps exercise selection and load decisions auditable.  AI may
explain a recommendation, but it never bypasses these rules.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
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
    regressions: tuple[str, ...] = ()
    progressions: tuple[str, ...] = ()
    technique_cues: tuple[str, ...] = ()
    secondary_muscles: tuple[str, ...] = ()
    contraindications: tuple[str, ...] = ()
    common_mistakes: tuple[str, ...] = ()
    safe_range_notes: tuple[str, ...] = ()
    # TASK-61 (DECISION 1): mechanical demand metadata for deterministic
    # pain adaptation. Distinguishes HOW an exercise loads the elbow/wrist
    # region — a pronated pulling grip (tennis-elbow aggravator) is not the
    # same as a static carrying grip or a low-grip press, even though all
    # three list "elbow" in joint_load.
    grip_demand: str = "none"          # none | low | high_static | high_dynamic
    elbow_flexion_load: str = "none"   # none | low | high
    wrist_load: str = "none"           # none | low | high

    def public_metadata(self) -> dict[str, Any]:
        return {
            "exercise_id": self.exercise_id,
            "name_he": self.exercise_id,
            "name_en": self.exercise_id,
            "movement_pattern": self.movement,
            "primary_muscles": list(self.primary_muscles),
            "secondary_muscles": list(self.secondary_muscles),
            "equipment": list(self.equipment),
            "equipment_required": list(self.equipment),
            "joint_load": list(self.joint_load),
            "skill": self.skill,
            "difficulty_level": self.skill,
            "regressions": list(self.regressions),
            "regression_options": list(self.regressions),
            "progressions": list(self.progressions),
            "progression_options": list(self.progressions),
            "technique_cues": list(self.technique_cues),
            "coaching_cues": list(self.technique_cues),
            "contraindications": list(self.contraindications),
            "common_mistakes": list(self.common_mistakes),
            "safe_range_notes": list(self.safe_range_notes),
            "grip_demand": self.grip_demand,
            "elbow_flexion_load": self.elbow_flexion_load,
            "wrist_load": self.wrist_load,
        }


@dataclass(frozen=True)
class ClientTrainingProfile:
    user_id: int | None = None
    age: Any = None
    sex: Any = None
    height: Any = None
    current_weight: Any = None
    goal_type: str = "general_fitness"
    target_weight: Any = None
    training_experience: str = "beginner"
    training_history_text: str = ""
    available_days_per_week: int | None = None
    preferred_training_days: tuple[int, ...] = ()
    time_per_workout_minutes: int | None = None
    training_location: Any = None
    available_equipment: tuple[str, ...] = ()
    injuries: tuple[str, ...] = ()
    pain_areas: tuple[str, ...] = ()
    movement_limitations: tuple[str, ...] = ()
    medical_flags: tuple[str, ...] = ()
    #: A10: True when we do not KNOW whether the user has limitations, as
    #: distinct from knowing they have none. Both previously produced empty
    #: tuples above, so a plan built under uncertainty was indistinguishable
    #: from one built under a confirmed "no limitations" -- and every joint was
    #: loaded freely on that basis. Consumers must branch on this rather than
    #: inferring safety from an empty `injuries`.
    safety_unknown: bool = False
    sleep_quality: Any = None
    average_steps: Any = None
    stress_level: Any = None
    preferred_style: Any = None
    disliked_exercises: tuple[str, ...] = ()

    def public_payload(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "age": self.age,
            "sex": self.sex,
            "height": self.height,
            "current_weight": self.current_weight,
            "goal_type": self.goal_type,
            "target_weight": self.target_weight,
            "training_experience": self.training_experience,
            "training_history_text": self.training_history_text,
            "available_days_per_week": self.available_days_per_week,
            "preferred_training_days": list(self.preferred_training_days),
            "time_per_workout_minutes": self.time_per_workout_minutes,
            "training_location": self.training_location,
            "available_equipment": list(self.available_equipment),
            "injuries": list(self.injuries),
            "pain_areas": list(self.pain_areas),
            "movement_limitations": list(self.movement_limitations),
            "medical_flags": list(self.medical_flags),
            # A10: a bounded boolean, never the limitation text. Without it the
            # stored profile cannot say whether those empty lists above mean
            # "no limitations" or "never asked" -- the same conflation at rest
            # that the flag fixes in memory.
            "safety_unknown": self.safety_unknown,
            "sleep_quality": self.sleep_quality,
            "average_steps": self.average_steps,
            "stress_level": self.stress_level,
            "preferred_style": self.preferred_style,
            "disliked_exercises": list(self.disliked_exercises),
        }


CATALOG: dict[str, ExerciseProfile] = {
    "bench": ExerciseProfile("bench", "horizontal_push", ("chest",), ("barbell", "bench"), ("shoulder", "elbow")),
    "db_bench": ExerciseProfile("db_bench", "horizontal_push", ("chest",), ("dumbbell", "bench"), ("shoulder", "elbow")),
    "smith_bench": ExerciseProfile("smith_bench", "horizontal_push", ("chest",), ("barbell", "bench", "machine"), ("shoulder", "elbow")),
    "incline_db": ExerciseProfile("incline_db", "horizontal_push", ("chest",), ("dumbbell", "bench"), ("shoulder", "elbow")),
    "incline_bar": ExerciseProfile("incline_bar", "horizontal_push", ("chest",), ("barbell", "bench"), ("shoulder", "elbow")),
    "incline_machine": ExerciseProfile("incline_machine", "horizontal_push", ("chest",), ("machine",), ("shoulder", "elbow")),
    "incline_smith": ExerciseProfile("incline_smith", "horizontal_push", ("chest",), ("barbell", "bench", "machine"), ("shoulder", "elbow")),
    "fly": ExerciseProfile("fly", "chest_isolation", ("chest",), ("dumbbell", "bench"), ("shoulder",)),
    "cable_fly": ExerciseProfile("cable_fly", "chest_isolation", ("chest",), ("cable",), ("shoulder",)),
    "one_arm_fly": ExerciseProfile("one_arm_fly", "chest_isolation", ("chest",), ("cable",), ("shoulder",)),
    "pec_deck": ExerciseProfile("pec_deck", "chest_isolation", ("chest",), ("machine",), ("shoulder",)),
    "lat_pull": ExerciseProfile("lat_pull", "vertical_pull", ("back",), ("cable",), ("shoulder", "elbow")),
    "lat_pull_fb": ExerciseProfile("lat_pull_fb", "vertical_pull", ("back",), ("cable",), ("shoulder", "elbow")),
    "assisted_pullup": ExerciseProfile("assisted_pullup", "vertical_pull", ("back",), ("machine",), ("shoulder", "elbow", "back")),
    "neutral_pull": ExerciseProfile("neutral_pull", "vertical_pull", ("back",), ("cable",), ("shoulder", "elbow", "back")),
    "one_arm_pull": ExerciseProfile("one_arm_pull", "vertical_pull", ("back",), ("cable",), ("shoulder", "elbow", "back")),
    "one_arm_row": ExerciseProfile("one_arm_row", "horizontal_pull", ("back",), ("dumbbell", "bench"), ("back", "elbow")),
    "cable_row": ExerciseProfile("cable_row", "horizontal_pull", ("back",), ("cable",), ("back", "elbow")),
    "supported_row": ExerciseProfile("supported_row", "horizontal_pull", ("back",), ("dumbbell", "bench"), ("back", "elbow")),
    "row_machine": ExerciseProfile("row_machine", "horizontal_pull", ("back",), ("machine",), ("back", "elbow")),
    "rope_push": ExerciseProfile("rope_push", "elbow_extension", ("triceps",), ("cable",), ("elbow",)),
    "bar_push": ExerciseProfile("bar_push", "elbow_extension", ("triceps",), ("cable",), ("elbow",)),
    "one_arm_push": ExerciseProfile("one_arm_push", "elbow_extension", ("triceps",), ("cable",), ("elbow",)),
    "dip_machine": ExerciseProfile("dip_machine", "horizontal_push", ("chest", "triceps"), ("machine",), ("shoulder", "elbow")),
    "military": ExerciseProfile("military", "vertical_push", ("shoulder",), ("barbell",), ("shoulder", "elbow", "back")),
    "db_shoulder": ExerciseProfile("db_shoulder", "vertical_push", ("shoulder",), ("dumbbell",), ("shoulder", "elbow")),
    "shoulder_machine": ExerciseProfile("shoulder_machine", "vertical_push", ("shoulder",), ("machine",), ("shoulder", "elbow")),
    "landmine": ExerciseProfile("landmine", "vertical_push", ("shoulder",), ("barbell",), ("shoulder", "elbow", "back")),
    "squat": ExerciseProfile("squat", "squat", ("quads", "glutes"), ("barbell", "rack"), ("knee", "hip", "back"), "intermediate"),
    "leg_press": ExerciseProfile("leg_press", "squat", ("quads", "glutes"), ("machine",), ("knee", "hip")),
    "hack": ExerciseProfile("hack", "squat", ("quads", "glutes"), ("machine",), ("knee", "hip", "back")),
    "smith_squat": ExerciseProfile("smith_squat", "squat", ("quads", "glutes"), ("barbell", "machine"), ("knee", "hip", "back")),
    "goblet": ExerciseProfile("goblet", "squat", ("quads", "glutes"), ("dumbbell",), ("knee", "hip", "back")),
    "rdl": ExerciseProfile("rdl", "hinge", ("hamstrings", "glutes"), ("barbell",), ("back", "hip"), "intermediate"),
    "hip_thrust": ExerciseProfile("hip_thrust", "hinge", ("glutes",), ("barbell", "bench"), ("hip", "back")),
    "back_ext": ExerciseProfile("back_ext", "hinge", ("hamstrings", "glutes", "back"), ("machine",), ("hip", "back")),
    "cable_pull_through": ExerciseProfile("cable_pull_through", "hinge", ("hamstrings", "glutes"), ("cable",), ("hip", "back")),
    "lateral": ExerciseProfile("lateral", "shoulder_isolation", ("shoulder",), ("dumbbell",), ("shoulder",)),
    "cable_lateral": ExerciseProfile("cable_lateral", "shoulder_isolation", ("shoulder",), ("cable",), ("shoulder",)),
    "lateral_machine": ExerciseProfile("lateral_machine", "shoulder_isolation", ("shoulder",), ("machine",), ("shoulder",)),
    "lean_lateral": ExerciseProfile("lean_lateral", "shoulder_isolation", ("shoulder",), ("dumbbell",), ("shoulder",)),
    "bar_curl": ExerciseProfile("bar_curl", "elbow_flexion", ("biceps",), ("barbell",), ("elbow",)),
    "incline_curl": ExerciseProfile("incline_curl", "elbow_flexion", ("biceps",), ("dumbbell", "bench"), ("elbow",)),
    "cable_curl": ExerciseProfile("cable_curl", "elbow_flexion", ("biceps",), ("cable",), ("elbow",)),
    "preacher": ExerciseProfile("preacher", "elbow_flexion", ("biceps",), ("machine", "bench"), ("elbow",)),
    "db_curl": ExerciseProfile("db_curl", "elbow_flexion", ("biceps",), ("dumbbell",), ("elbow",)),
    "crossover": ExerciseProfile("crossover", "chest_isolation", ("chest",), ("cable",), ("shoulder",)),
    "chest_machine": ExerciseProfile("chest_machine", "horizontal_push", ("chest", "triceps"), ("machine",), ("shoulder", "elbow")),
    # Home/bodyweight replacements.
    "pushup": ExerciseProfile("pushup", "horizontal_push", ("chest",), ("bodyweight",), ("shoulder", "elbow")),
    "incline_pushup": ExerciseProfile("incline_pushup", "horizontal_push", ("chest",), ("bodyweight",), ("shoulder", "elbow")),
    "backpack_row": ExerciseProfile("backpack_row", "horizontal_pull", ("back",), ("household",), ("back", "elbow")),
    "band_row": ExerciseProfile("band_row", "horizontal_pull", ("back",), ("band",), ("back", "elbow")),
    "glute_bridge": ExerciseProfile("glute_bridge", "hinge", ("glutes",), ("bodyweight",), ("back", "hip")),
    "chair_squat": ExerciseProfile("chair_squat", "squat", ("quads", "glutes"), ("bodyweight",), ("knee", "hip")),
    "wall_push": ExerciseProfile("wall_push", "horizontal_push", ("chest",), ("bodyweight",), ("shoulder", "elbow")),
    "dead_bug": ExerciseProfile("dead_bug", "core", ("core",), ("bodyweight",), ("back",)),
}

# TASK-61 (DECISION 1): mechanical demand metadata per exercise, applied over
# the catalog below. One table, keyed by exercise id — entries not listed keep
# the "none" defaults (legs/core/laterals: no meaningful grip/elbow/wrist
# demand). Derivation rationale:
# - pronated/dynamic pulling grips and curls: grip_demand=high_dynamic +
#   elbow_flexion_load=high (the classic lateral-epicondyle aggravators);
# - neutral-grip / machine-supported pull variants: high_static grip with
#   LOW flexion strain — the same-pattern "gentler" targets;
# - hinge holds (RDL, hip thrust bar): static carrying grip, no flexion;
# - presses / pushdowns: low grip; the elbow entry in joint_load stays (the
#   joint moves under load) but the grip/flexion pathway is low;
# - barbell curls additionally load the wrist (fixed pronation-supination).
_DEMAND_METADATA: dict[str, dict[str, str]] = {
    # Presses (low grip, elbow moves under load, low flexion-tendon strain).
    "bench": {"grip_demand": "low", "elbow_flexion_load": "low", "wrist_load": "low"},
    "db_bench": {"grip_demand": "low", "elbow_flexion_load": "low"},
    "smith_bench": {"grip_demand": "low", "elbow_flexion_load": "low"},
    "incline_db": {"grip_demand": "low", "elbow_flexion_load": "low"},
    "incline_bar": {"grip_demand": "low", "elbow_flexion_load": "low", "wrist_load": "low"},
    "incline_machine": {"grip_demand": "low", "elbow_flexion_load": "low"},
    "incline_smith": {"grip_demand": "low", "elbow_flexion_load": "low"},
    "chest_machine": {"grip_demand": "low", "elbow_flexion_load": "low"},
    "dip_machine": {"grip_demand": "low", "elbow_flexion_load": "low"},
    "military": {"grip_demand": "low", "elbow_flexion_load": "low", "wrist_load": "low"},
    "db_shoulder": {"grip_demand": "low", "elbow_flexion_load": "low"},
    "shoulder_machine": {"grip_demand": "low", "elbow_flexion_load": "low"},
    "landmine": {"grip_demand": "low", "elbow_flexion_load": "low"},
    "pushup": {"grip_demand": "none", "elbow_flexion_load": "low", "wrist_load": "high"},
    "incline_pushup": {"grip_demand": "none", "elbow_flexion_load": "low", "wrist_load": "low"},
    "wall_push": {"grip_demand": "none", "elbow_flexion_load": "low"},
    # Pronated/dynamic pulls — the tennis-elbow aggravators.
    "lat_pull": {"grip_demand": "high_dynamic", "elbow_flexion_load": "high", "wrist_load": "low"},
    "lat_pull_fb": {"grip_demand": "high_dynamic", "elbow_flexion_load": "high", "wrist_load": "low"},
    "assisted_pullup": {"grip_demand": "high_dynamic", "elbow_flexion_load": "high", "wrist_load": "low"},
    "one_arm_row": {"grip_demand": "high_dynamic", "elbow_flexion_load": "high", "wrist_load": "low"},
    "supported_row": {"grip_demand": "high_dynamic", "elbow_flexion_load": "high", "wrist_load": "low"},
    "backpack_row": {"grip_demand": "high_dynamic", "elbow_flexion_load": "high"},
    "band_row": {"grip_demand": "high_dynamic", "elbow_flexion_load": "high"},
    # Neutral-grip / machine-supported pulls — the gentler same-pattern
    # variants (static grip, reduced flexion strain).
    "neutral_pull": {"grip_demand": "high_static", "elbow_flexion_load": "low"},
    "one_arm_pull": {"grip_demand": "high_static", "elbow_flexion_load": "low"},
    "cable_row": {"grip_demand": "high_static", "elbow_flexion_load": "low"},
    "row_machine": {"grip_demand": "high_static", "elbow_flexion_load": "low"},
    # Curls: direct dynamic flexion; barbell also fixes the wrist.
    "bar_curl": {"grip_demand": "high_dynamic", "elbow_flexion_load": "high", "wrist_load": "high"},
    "incline_curl": {"grip_demand": "high_dynamic", "elbow_flexion_load": "high", "wrist_load": "low"},
    "db_curl": {"grip_demand": "high_dynamic", "elbow_flexion_load": "high", "wrist_load": "low"},
    "cable_curl": {"grip_demand": "high_static", "elbow_flexion_load": "high", "wrist_load": "low"},
    "preacher": {"grip_demand": "high_static", "elbow_flexion_load": "high", "wrist_load": "low"},
    # Triceps pushdowns: low grip, elbow extension.
    "rope_push": {"grip_demand": "low", "elbow_flexion_load": "low"},
    "bar_push": {"grip_demand": "low", "elbow_flexion_load": "low", "wrist_load": "low"},
    "one_arm_push": {"grip_demand": "low", "elbow_flexion_load": "low"},
    # Hinge holds: static carrying grip.
    "rdl": {"grip_demand": "high_static", "wrist_load": "low"},
    "hip_thrust": {"grip_demand": "low"},
    "cable_pull_through": {"grip_demand": "high_static"},
    # Isolation flys/laterals: light handles.
    "fly": {"grip_demand": "low"},
    "cable_fly": {"grip_demand": "low"},
    "one_arm_fly": {"grip_demand": "low"},
    "pec_deck": {"grip_demand": "low"},
    "crossover": {"grip_demand": "low"},
    "lateral": {"grip_demand": "low"},
    "cable_lateral": {"grip_demand": "low"},
    "lateral_machine": {"grip_demand": "low"},
    "lean_lateral": {"grip_demand": "low"},
    "goblet": {"grip_demand": "high_static"},
}


def _apply_demand_metadata() -> None:
    from dataclasses import replace as _dc_replace

    for exercise_id, demands in _DEMAND_METADATA.items():
        profile = CATALOG.get(exercise_id)
        if profile is not None:
            CATALOG[exercise_id] = _dc_replace(profile, **demands)


_apply_demand_metadata()


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


def _fact_value(facts: dict[str, dict[str, Any]], key: str, default: Any = None) -> Any:
    fact = facts.get(key)
    if not fact:
        return default
    value = fact.get("value", default)
    return default if value is None else value


def _tuple_value(value: Any) -> tuple[Any, ...]:
    if value is None or value == "":
        return ()
    if isinstance(value, (list, tuple, set)):
        return tuple(item for item in value if item not in (None, ""))
    return (value,)


def _normalize_goal_type(value: Any) -> str:
    text = _text(value)
    if any(token in text for token in ("fat_loss", "fat_loss_muscle_retention", "weight_loss")):
        return "fat_loss"
    if any(token in text for token in ("muscle", "׳׳¡׳”", "׳©׳¨׳™׳¨")):
        return "muscle_gain"
    if any(token in text for token in ("strength", "׳›׳•׳—")):
        return "strength"
    if any(token in text for token in ("rehab", "return", "׳₪׳¦׳™׳¢", "׳—׳–׳¨׳”")):
        return "rehab_or_return"
    if any(token in text for token in ("health", "general_health")):
        return "health"
    return "general_fitness"


def client_training_profile_from_facts(
    facts: dict[str, dict[str, Any]],
    *,
    user_id: int | None = None,
    available_days_per_week: int | None = None,
    preferred_training_days: list[int] | tuple[int, ...] | None = None,
    time_per_workout_minutes: int | None = None,
) -> ClientTrainingProfile:
    """Build the professional training-profile snapshot stored with a plan."""
    equipment = normalize_equipment(
        _fact_value(facts, "equipment"),
        _fact_value(facts, "training_location"),
    )
    limitations = _fact_value(facts, "training_limitations")
    # A10: absence here is UNKNOWN, not "none". Measured before the fix: with no
    # training_limitations, no active_pain and no medical_avoidance this yielded
    # injuries=(), pain_areas=(), movement_limitations=(), medical_flags=() --
    # byte-identical to an answered "none".
    safety_unknown = limitations is None
    if limitations is None:
        limitations = " ".join(
            str(value or "")
            for value in (
                _fact_value(facts, "active_pain"),
                _fact_value(facts, "medical_avoidance"),
            )
            if value
        )
    # Any real signal (active pain, a medical avoidance) means the state is
    # known after all -- only a genuinely empty fallback is an unknown.
    if safety_unknown and str(limitations).strip():
        safety_unknown = False
    pain = pain_regions(limitations, None)
    medical = _tuple_value(limitations)
    return ClientTrainingProfile(
        user_id=user_id,
        age=_fact_value(facts, "age"),
        sex=_fact_value(facts, "sex"),
        height=_fact_value(facts, "height_cm"),
        current_weight=_fact_value(facts, "weight_kg"),
        goal_type=_normalize_goal_type(_fact_value(facts, "primary_goal")),
        target_weight=_fact_value(facts, "goal_weight_kg"),
        training_experience=str(_fact_value(facts, "strength_experience", "beginner") or "beginner"),
        training_history_text=str(_fact_value(facts, "training_history_text", "") or ""),
        available_days_per_week=available_days_per_week,
        preferred_training_days=tuple(int(day) for day in (preferred_training_days or ()) if isinstance(day, int)),
        time_per_workout_minutes=time_per_workout_minutes,
        training_location=_fact_value(facts, "training_location"),
        available_equipment=tuple(sorted(equipment)),
        injuries=medical,
        pain_areas=tuple(sorted(pain)),
        movement_limitations=medical,
        medical_flags=medical,
        safety_unknown=safety_unknown,
        sleep_quality=_fact_value(facts, "sleep_quality"),
        average_steps=_fact_value(facts, "average_steps"),
        stress_level=_fact_value(facts, "stress_level"),
        preferred_style=_fact_value(facts, "preferred_style"),
        disliked_exercises=_tuple_value(_fact_value(facts, "disliked_exercises")),
    )


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


PAIN_REGION_TOKENS: dict[str, tuple[str, ...]] = {
    "knee": ("ברך", "ברכיים", "knee"),
    "shoulder": ("כתף", "shoulder"),
    "back": ("גב", "back", "מותן"),
    "elbow": ("מרפק", "elbow", "טניס אלכן", "טניס אלבו", "אלבו"),
    "wrist": ("שורש כף", "wrist"),
    "hip": ("ירך", "מפשעה", "hip"),
}

# Clean Hebrew labels for each region — used anywhere a region token from
# medical_constraints/pain_regions is shown to the user, so raw English
# tokens (e.g. "elbow") or mixed free text never reach the UI unmapped.
PAIN_REGION_LABELS: dict[str, str] = {
    "knee": "ברך",
    "shoulder": "כתף",
    "back": "גב",
    "elbow": "מרפק",
    "wrist": "שורש כף יד",
    "hip": "ירך",
}


def pain_region_label(region: str) -> str:
    return PAIN_REGION_LABELS.get(region, region)


def pain_regions(pain: Any, medical_avoidance: Any) -> set[str]:
    text = f"{_text(pain)} {_text(medical_avoidance)}"
    regions = set()
    for region, tokens in PAIN_REGION_TOKENS.items():
        if any(token in text for token in tokens):
            regions.add(region)
    return regions


# A reported pain stays "active" for this many days without the user
# confirming it again — matches the recommended 7-14 day window; a
# constraint row's own status/resolved_at (set elsewhere) always wins over
# this if the user has already said it's resolved.
PAIN_CONSTRAINT_TTL_DAYS = 14


@dataclass(frozen=True)
class ActivePainRegion:
    region: str
    label: str
    severity: int | None
    age_days: float


def _parse_created_at(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def active_pain_regions(
    constraint_rows: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    ttl_days: int = PAIN_CONSTRAINT_TTL_DAYS,
) -> dict[str, ActivePainRegion]:
    """Reduce raw medical_constraints rows (kind='pain') to the set of body
    regions that are still within the TTL window, each with its worst
    (highest) reported severity.

    Rows with an explicit status other than "active" are skipped (the
    resolved/cleared decision from elsewhere always wins). Rows older than
    ``ttl_days`` are treated as expired even if still marked "active", since
    nothing currently prompts the user to confirm/clear old reports.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=ttl_days)
    result: dict[str, ActivePainRegion] = {}
    for row in constraint_rows:
        if row.get("kind") != "pain":
            continue
        if row.get("status") != "active":
            continue
        created_at = _parse_created_at(row.get("created_at"))
        if created_at is not None and created_at < cutoff:
            continue
        age_days = (now - created_at).total_seconds() / 86400 if created_at else 0.0
        location_text = f"{_text(row.get('location'))} {_text(row.get('note'))}"
        severity = row.get("severity")
        try:
            severity_int = int(severity) if severity is not None else None
        except (TypeError, ValueError):
            severity_int = None
        for region, tokens in PAIN_REGION_TOKENS.items():
            if not any(token in location_text for token in tokens):
                continue
            existing = result.get(region)
            if existing is None or (severity_int or 0) > (existing.severity or 0):
                result[region] = ActivePainRegion(
                    region=region,
                    label=PAIN_REGION_LABELS.get(region, region),
                    severity=severity_int,
                    age_days=age_days,
                )
    return result


def pain_safety_guidance(region: ActivePainRegion) -> str:
    """User-facing, non-diagnostic safety guidance for an active pain region."""
    base = (
        f"בגלל שדיווחת לאחרונה על כאב ב{region.label}, "
        "שמרתי עומס שמרני בתרגיל הזה. בצע רק בטווח ללא כאב."
    )
    if region.severity is not None and region.severity >= 4:
        return (
            f"כאב 4/10 ומעלה ב{region.label} הוא סימן לעצור את התרגיל עכשיו. "
            "אל תנסה לעבוד סביב כאב חד. אם הכאב מתגבר, מופיעה נפיחות, הקרנה "
            "או מגבלה בתנועה, כדאי לפנות לבדיקה מקצועית."
        )
    if region.age_days >= 7:
        return (
            f"הכאב ב{region.label} עדיין מסומן כפעיל כבר כמה ימים. "
            "נמשיך להימנע מהעמסה ישירה, ואם הוא לא משתפר כדאי בדיקה מקצועית."
        )
    return base


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


# Human-readable Hebrew names for catalog exercises used as cross-pattern
# backfill. Kept next to the catalog so a backfilled exercise never surfaces
# a raw English id to the user.
_CATALOG_NAMES_HE: dict[str, str] = {
    "squat": "סקוואט",
    "smith_squat": "סקוואט סמית",
    "leg_press": "לחיצת רגליים",
    "goblet": "סקוואט גובלט",
    "hack": "סקוואט האק",
    "hip_thrust": "הרמת אגן (Hip Thrust)",
    "back_ext": "יישור גב תחתון",
    "cable_pull_through": "משיכת אגן בכבל",
    "glute_bridge": "גשר ישבן",
    "rdl": "מתח רומני (RDL)",
    "lateral": "הרחקות צד",
    "cable_lateral": "הרחקות צד בכבל",
    "lateral_machine": "הרחקות צד במכונה",
    "lean_lateral": "הרחקות צד בהטיה",
    "fly": "פרפר עם דאמבל",
    "cable_fly": "פרפר בכבל",
    "one_arm_fly": "פרפר חד-צדדי בכבל",
    "pec_deck": "פרפר במכונה (Pec Deck)",
    "crossover": "קרוסאובר בכבל",
    "dead_bug": "Dead Bug",
}


def _pain_safe_backfill_candidates(
    *,
    equipment: set[str],
    pain: set[str],
    experience: str,
    present_ids: set[str],
    regions: Iterable[str] = (),
    allowed_muscles: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Deterministic pain-safe, equipment-available exercises from the catalog.

    When pain removes so many exercises from a session that same-pattern
    substitution cannot refill it (e.g. tennis-elbow zeroes out every push/pull
    in an upper-body session because they all load the elbow), the session is
    backfilled with exercises from *other* movement patterns that are safe for
    the injured joint. Catalog order is stable, so the selection is
    deterministic. IDs already present in the session are skipped.

    A backfill exists to make a gutted session trainable, so candidates must
    be fully clean (``region_load_level == "none"``) for every active pain
    region — a reduce-level exercise may survive adaptation in place, but it
    must never be *added* to a session on the injured joint's account. When
    ``allowed_muscles`` is given, only exercises whose primary muscle goal is
    in that set qualify: a slot removed for pain may only be refilled with
    work toward the same goal, never with an unrelated movement inserted just
    to preserve the session's exercise count.
    """
    region_tokens = [token for token in regions if token] or sorted(pain)
    allowed_by_muscle: dict[str, list[dict[str, Any]]] = {}
    for exercise_id, profile in CATALOG.items():
        if exercise_id in present_ids:
            continue
        probe = {"id": exercise_id, "name": _CATALOG_NAMES_HE.get(exercise_id, exercise_id)}
        allowed, _ = exercise_allowed(
            probe, equipment=equipment, pain=pain, experience=experience
        )
        if not allowed:
            continue
        if any(region_load_level(profile, token) != "none" for token in region_tokens):
            continue
        muscle = profile.primary_muscles[0] if profile.primary_muscles else ""
        if allowed_muscles is not None and muscle not in allowed_muscles:
            continue
        allowed_by_muscle.setdefault(muscle, []).append(
            {
                "id": exercise_id,
                "name": _CATALOG_NAMES_HE.get(exercise_id, exercise_id),
                "muscle": muscle,
                "sets": 3,
                "rmin": 8,
                "rmax": 12,
                "alts": [],
                "cues": ["טווח ללא כאב", "שליטה מלאה", "עצור אם הכאב מחמיר"],
            }
        )
    # Round-robin across muscle groups so a backfilled session trains varied
    # muscles (e.g. legs + shoulders + chest) instead of three near-identical
    # isolation variants of whichever pattern happens to sort first.
    candidates: list[dict[str, Any]] = []
    muscle_order = list(allowed_by_muscle.keys())
    while any(allowed_by_muscle[m] for m in muscle_order):
        for muscle in muscle_order:
            bucket = allowed_by_muscle[muscle]
            if bucket:
                candidates.append(bucket.pop(0))
    return candidates


# The smallest session we are willing to present. Below this a session reads as
# broken (the quality gate rejects an empty session), so it is backfilled.
_MIN_SESSION_EXERCISES = 3


# ---------------------------------------------------------------------------
# TASK-61 — deterministic pain-aware adaptation decisions
# ---------------------------------------------------------------------------

# DECISION 3: at or above this reported severity (medical_constraints.severity,
# 1-10) an exercise that loads the painful region may never be KEPT — it is
# replaced with a region-clean alternative or omitted. Only explicit future
# rules may override this.
HIGH_SEVERITY_MIN = 7

# DECISION 2: reduction bands. Load reduction targets ~40% (within the
# approved 30-50% band), snapped DOWN to the exercise's own increment and
# never below one increment; when the exercise has no meaningful load the
# volume axis is used instead (-1 set, never below 2). Exactly ONE axis
# changes per adaptation.
REDUCE_LOAD_FACTOR = 0.6
REDUCE_SETS_DELTA = 1
MIN_SETS_AFTER_REDUCTION = 2

# DECISION 4: short, natural, non-diagnostic user-facing explanations.
ADAPTATION_EXPLANATIONS_HE = {
    "replace": (
        "החלפתי את התרגיל הזה כי הוא עלול להעמיס על האזור שדיווחת עליו — "
        "הבחירה החדשה שומרת על מטרת האימון של היום."
    ),
    "modify": (
        "עברתי לגרסה עדינה יותר של אותו תרגיל כדי להפחית עומס מהאזור הרגיש."
    ),
    "reduce": (
        "השארתי את התרגיל אבל הפחתתי את העומס כדי להקטין לחץ על האזור הרגיש."
    ),
    "omit": (
        "הורדתי את התרגיל מהאימון של היום כי לא מצאתי לו חלופה מתאימה לאזור "
        "שדיווחת עליו."
    ),
}


def region_load_level(profile: ExerciseProfile, region: str) -> str:
    """How an exercise loads a pain region: "none" | "reduce" | "replace".

    The weight-bearing joints (knee/shoulder/back/hip) keep the accepted
    binary semantics: a direct joint_load entry means REPLACE. The elbow and
    wrist are refined by the DECISION-1 demand metadata — a pronated dynamic
    grip or high flexion strain still means REPLACE, but a static carrying
    grip or a low-grip press only warrants a load REDUCTION, which is what
    keeps an arm/upper session trainable under tennis elbow instead of
    gutting it and backfilling with unrelated leg work.
    """
    direct = region in profile.joint_load
    if region == "elbow":
        # The aggravating pathway is the dynamic pronated grip (wrist
        # extensors); high flexion with a STATIC/neutral grip (cable curl,
        # preacher pad, neutral pull) is the classic tolerable variant and
        # only warrants load reduction.
        if profile.grip_demand == "high_dynamic":
            return "replace"
        if (
            profile.grip_demand == "high_static"
            or profile.elbow_flexion_load == "high"
            or direct
        ):
            return "reduce"
        return "none"
    if region == "wrist":
        if profile.wrist_load == "high" or profile.grip_demand == "high_dynamic":
            return "replace"
        if profile.grip_demand == "high_static" or profile.wrist_load == "low" or direct:
            return "reduce"
        return "none"
    return "replace" if direct else "none"


def decide_pain_adaptation(
    profile: ExerciseProfile,
    regions: dict[str, Any],
) -> tuple[str, str | None]:
    """The deterministic adaptation operation for one exercise.

    ``regions`` maps active region -> ActivePainRegion (or None when the
    severity is unknown). Returns (operation, triggering_region):
    operation in {"keep", "reduce", "replace", "omit_if_unreplaceable"}.
    The strongest requirement across active regions wins; DECISION 3 makes
    any high-severity region escalate straight past KEEP/REDUCE.
    """
    strongest = "keep"
    trigger: str | None = None
    for region, detail in regions.items():
        level = region_load_level(profile, region)
        if level == "none":
            continue
        severity = getattr(detail, "severity", None)
        high = severity is not None and severity >= HIGH_SEVERITY_MIN
        if high:
            return "omit_if_unreplaceable", region
        if level == "replace":
            strongest, trigger = "replace", region
        elif level == "reduce" and strongest == "keep":
            strongest, trigger = "reduce", region
    return strongest, trigger


def apply_load_reduction(exercise: dict[str, Any]) -> dict[str, Any]:
    """Return a reduced copy of the exercise (DECISION 2: one axis only)."""
    reduced = dict(exercise)
    weight = float(exercise.get("weight") or 0)
    increment = float(exercise.get("inc") or 0) or 2.5
    if weight > increment:
        target = weight * REDUCE_LOAD_FACTOR
        snapped = max(increment, increment * int(target / increment))
        reduced["weight"] = round(snapped, 2)
    else:
        sets = int(exercise.get("sets") or 3)
        reduced["sets"] = max(MIN_SETS_AFTER_REDUCTION, sets - REDUCE_SETS_DELTA)
    return reduced


def _adaptive_replacement(
    original: dict[str, Any],
    *,
    triggering_region: str,
    regions: dict[str, Any],
    equipment: set[str],
    pain: set[str],
    experience: str,
    require_clean: bool,
    exclude_ids: set[str] | None = None,
) -> dict[str, Any] | None:
    """A semantic-goal-preserving replacement vetted by the demand-aware engine.

    Candidate tiers follow the approved hierarchy strictly: (1) the exercise's
    own alternatives (regression/variation of the same exercise — the packet's
    MODIFY source, e.g. pronated pull → neutral-grip pull), (2) same-movement
    fallbacks from REPLACEMENTS, (3) a fully-clean catalog exercise with the
    SAME primary muscle goal, then the caller omits. A candidate is never
    accepted merely because it is clean for the region — an unrelated movement
    must not enter the slot. ``require_clean`` (high severity, DECISION 3)
    accepts only candidates that do not load the region at all; otherwise a
    "reduce"-level tier-1/2 candidate is acceptable and is returned already
    load-reduced.
    """
    profile = CATALOG.get(str(original.get("id")))
    if not profile:
        return None
    exclude = exclude_ids or set()
    ranked: list[tuple[tuple[int, int], dict[str, Any]]] = []

    def _blocked_by_other_regions(candidate_profile: ExerciseProfile) -> bool:
        # Other active pain regions must not be loaded at replace-level
        # either (and never at all when they are high-severity).
        for other_region, other_detail in regions.items():
            if other_region == triggering_region:
                continue
            other_level = region_load_level(candidate_profile, other_region)
            severity = getattr(other_detail, "severity", None)
            other_high = severity is not None and severity >= HIGH_SEVERITY_MIN
            if other_level == "replace" or (other_high and other_level != "none"):
                return True
        return False

    def _allowed_for_user(candidate_profile: ExerciseProfile) -> bool:
        if candidate_profile.equipment and not set(candidate_profile.equipment).intersection(equipment):
            return False
        if candidate_profile.skill == "intermediate" and experience in {
            "none", "unknown", "beginner", "מתחיל",
        }:
            return False
        return True

    # Tiers 1+2: the exercise's own alternatives, then same-movement fallbacks.
    candidates: list[dict[str, Any]] = [
        dict(alt) for alt in (original.get("alts") or []) if isinstance(alt, dict)
    ]
    candidates.extend(REPLACEMENTS.get(profile.movement, []))
    for candidate in candidates:
        candidate_profile = CATALOG.get(str(candidate.get("id")))
        if candidate_profile is None:
            continue
        level = region_load_level(candidate_profile, triggering_region)
        if level == "replace":
            continue
        if require_clean and level != "none":
            continue
        if _blocked_by_other_regions(candidate_profile):
            continue
        if not _allowed_for_user(candidate_profile):
            continue
        replacement = {
            "id": candidate.get("id"),
            "name": candidate.get("name"),
            "muscle": candidate_profile.primary_muscles[0] if candidate_profile.primary_muscles else "",
            "sets": original.get("sets", 3),
            "rmin": original.get("rmin", 8),
            "rmax": original.get("rmax", 12),
            "rest": original.get("rest", 120),
            "weight": candidate.get("weight", original.get("weight", 0)),
            "inc": original.get("inc", 2.5),
            "cues": ["טווח ללא כאב", "שליטה מלאה", "עצור אם הכאב מחמיר"],
            "alts": [],
        }
        if level == "reduce":
            replacement = apply_load_reduction(replacement)
        ranked.append(((0, 0 if level == "none" else 1), replacement))

    # Tier 3: same-primary-muscle-goal catalog exercise, fully clean for every
    # active region (never a load compromise at this distance from the
    # original), deduplicated against the session.
    goal_muscle = profile.primary_muscles[0] if profile.primary_muscles else ""
    if goal_muscle:
        for candidate_id, candidate_profile in CATALOG.items():
            if candidate_id == profile.exercise_id or candidate_id in exclude:
                continue
            if not candidate_profile.primary_muscles:
                continue
            if candidate_profile.primary_muscles[0] != goal_muscle:
                continue
            if any(
                region_load_level(candidate_profile, region) != "none"
                for region in regions
            ):
                continue
            if not _allowed_for_user(candidate_profile):
                continue
            ranked.append(
                (
                    (1, 0),
                    {
                        "id": candidate_id,
                        "name": _CATALOG_NAMES_HE.get(candidate_id, candidate_id),
                        "muscle": goal_muscle,
                        "sets": original.get("sets", 3),
                        "rmin": original.get("rmin", 8),
                        "rmax": original.get("rmax", 12),
                        "rest": original.get("rest", 120),
                        "cues": ["טווח ללא כאב", "שליטה מלאה", "עצור אם הכאב מחמיר"],
                        "alts": [],
                    },
                )
            )
    if not ranked:
        return None
    ranked.sort(key=lambda item: item[0])
    return ranked[0][1]


def adapt_exercises(
    exercises: list[dict[str, Any]],
    *,
    equipment_value: Any,
    location: Any,
    pain_value: Any,
    medical_avoidance: Any,
    experience: str,
    pain_detail: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return safe/equipment-compatible exercises plus an audit trail.

    TASK-61: pain handling is a structured adaptation decision per exercise
    (KEEP / REDUCE / MODIFY / REPLACE / OMIT) instead of the old binary
    remove-or-replace. ``pain_detail`` (region -> ActivePainRegion) carries
    reported severities: at HIGH severity an exercise loading the region is
    never kept (DECISION 3). Equipment/skill gating is unchanged.
    """
    equipment = normalize_equipment(equipment_value, location)
    pain = pain_regions(pain_value, medical_avoidance)
    regions: dict[str, Any] = {region: None for region in pain}
    for region, detail in (pain_detail or {}).items():
        regions[region] = detail
    adapted: list[dict[str, Any]] = []
    changes: list[dict[str, Any]] = []
    removed_for_pain = False
    # Goal muscles of pain-omitted slots: the only muscles a backfill may
    # serve (an unrelated movement never enters a removed slot).
    omitted_goal_muscles: set[str] = set()
    session_ids = {str(item.get("id") or "") for item in exercises}
    for original in exercises:
        # Equipment/skill problems keep the pre-TASK-61 replace path.
        allowed, reasons = exercise_allowed(
            original,
            equipment=equipment,
            pain=set(),
            experience=experience,
        )
        if not allowed:
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
            continue

        profile = CATALOG.get(str(original.get("id")))
        operation, trigger = (
            decide_pain_adaptation(profile, regions)
            if profile is not None and regions
            else ("keep", None)
        )
        if operation == "keep":
            adapted.append(original)
            continue
        region_label = pain_region_label(trigger or "")
        if operation == "reduce":
            reduced = apply_load_reduction(original)
            reduced["adaptation_note"] = ADAPTATION_EXPLANATIONS_HE["reduce"]
            adapted.append(reduced)
            changes.append(
                {
                    "reduced": original.get("name"),
                    "region": region_label,
                    "reasons": [ADAPTATION_EXPLANATIONS_HE["reduce"]],
                }
            )
            continue
        # replace / omit_if_unreplaceable
        require_clean = operation == "omit_if_unreplaceable"
        replacement = _adaptive_replacement(
            original,
            triggering_region=trigger or "",
            regions=regions,
            equipment=equipment,
            pain=pain,
            experience=experience,
            require_clean=require_clean,
            exclude_ids=session_ids | {str(item.get("id") or "") for item in adapted},
        )
        removed_for_pain = True
        if replacement is not None:
            original_profile = profile
            replacement_profile = CATALOG.get(str(replacement.get("id")))
            same_movement = (
                original_profile is not None
                and replacement_profile is not None
                and original_profile.movement == replacement_profile.movement
            )
            explanation_key = "modify" if same_movement else "replace"
            replacement["adaptation_note"] = ADAPTATION_EXPLANATIONS_HE[explanation_key]
            # A11b: the replacement FILLS the original's professional need -- it
            # is the same slot with a different implementation, which is the
            # distinction the slot model exists to express. It must therefore
            # inherit the original's identity rather than be minted a new one
            # from its final list position: a slot substituted for pain would
            # otherwise look like a brand-new need, losing its history and
            # making the swap invisible to A12's pattern detection.
            for inherited in ("slot_id", "slot_key"):
                if original.get(inherited):
                    replacement[inherited] = original[inherited]
            replacement["original_id"] = original.get("original_id") or original.get("id")
            adapted.append(replacement)
            changes.append(
                {
                    "removed": original.get("name"),
                    "region": region_label,
                    "reasons": [ADAPTATION_EXPLANATIONS_HE[explanation_key]],
                    "replacement": replacement.get("name"),
                }
            )
        else:
            if profile is not None and profile.primary_muscles:
                omitted_goal_muscles.add(profile.primary_muscles[0])
            changes.append(
                {
                    "removed": original.get("name"),
                    "region": region_label,
                    "reasons": [ADAPTATION_EXPLANATIONS_HE["omit"]],
                    "replacement": None,
                }
            )

    # Goal-preserving backfill: if pain gutted the session and neither a
    # same-movement nor a same-muscle-goal replacement could refill it, look
    # once more for clean exercises serving the OMITTED slots' muscle goals.
    # An omitted slot's goal is the only thing a backfill may serve — it never
    # inserts an unrelated movement just to preserve the exercise count, so a
    # session may legitimately stay short.
    if pain and removed_for_pain and len(adapted) < _MIN_SESSION_EXERCISES:
        present_ids = {str(item.get("id") or "") for item in adapted} | session_ids
        needed = _MIN_SESSION_EXERCISES - len(adapted)
        backfill = _pain_safe_backfill_candidates(
            equipment=equipment,
            pain=pain,
            experience=experience,
            present_ids=present_ids,
            regions=regions,
            allowed_muscles=omitted_goal_muscles,
        )[:needed]
        for item in backfill:
            item["warmup_sets"] = warmup_sets(item)
            adapted.append(item)
        if backfill:
            changes.append(
                {
                    "backfilled": [item["name"] for item in backfill],
                    "reasons": [
                        "הוספתי תרגילים מאותה מטרת אימון במקום תרגילים שהוסרו בגלל המגבלה שדיווחת"
                    ],
                }
            )
    return adapted, changes




def exercise_catalog_entry(exercise_id: str) -> dict[str, Any] | None:
    """Return structured metadata for a catalog exercise.

    TASK-08: callers should not hard-code joint load or movement-pattern rules.
    This exposes the existing deterministic catalog as the single place for
    substitutions, pain-aware checks and plan-quality gates.
    """
    profile = CATALOG.get(str(exercise_id))
    if profile is None:
        return None
    replacements = REPLACEMENTS.get(profile.movement, [])
    result = profile.public_metadata()
    result["substitutions"] = [dict(item) for item in replacements]
    result["contraindications"] = [
        f"active_{joint}_pain" for joint in profile.joint_load
    ]
    return result


def substitutions_for_exercise(
    exercise_id: str,
    *,
    equipment_value: Any = None,
    location: Any = None,
    pain_value: Any = None,
    medical_avoidance: Any = None,
    experience: str = "beginner",
) -> list[dict[str, Any]]:
    """Return allowed substitutions from the same movement pattern.

    It respects equipment and current pain regions.  If knee pain is active,
    for example, squat-pattern replacements that still load the knee are
    filtered out before they reach the UI.
    """
    profile = CATALOG.get(str(exercise_id))
    if profile is None:
        return []
    equipment = normalize_equipment(equipment_value, location)
    pain = pain_regions(pain_value, medical_avoidance)
    allowed: list[dict[str, Any]] = []
    for replacement in REPLACEMENTS.get(profile.movement, []):
        candidate_profile = CATALOG.get(str(replacement.get("id")))
        if candidate_profile is None:
            continue
        ok, reasons = exercise_allowed(
            {"id": replacement.get("id")},
            equipment=equipment,
            pain=pain,
            experience=experience,
        )
        if not ok:
            continue
        allowed.append({
            **replacement,
            "movement_pattern": candidate_profile.movement,
            "primary_muscles": list(candidate_profile.primary_muscles),
            "joint_load": list(candidate_profile.joint_load),
            "reason": "אותה תבנית תנועה, עומס מותאם לנתונים שלך",
        })
    return allowed

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


# ---------------------------------------------------------------------------
# TASK-B1: session duration model + trim-only time fitting for the MAIN plan.
# ---------------------------------------------------------------------------
#
# DURATION MODEL (deterministic, no per-user calibration available).
#
# There was no estimator for a *prescribed* session anywhere in the codebase
# (``routine.py``/``health_jobs.py`` only average *observed* workouts), so this
# is a new, explicitly documented one. Cost of one working set is:
#
#     work_seconds(reps) + rest_seconds
#
# with ``work_seconds = reps * _SECONDS_PER_REP`` at a controlled ~3 s/rep
# tempo (roughly 1 s concentric + 2 s eccentric, the tempo the plan cues
# prescribe), and ``rest`` taken from the exercise's own prescription — the
# templates already carry per-exercise ``rest`` in seconds, so rest is real
# data, not an assumption. Reps use the midpoint of the ``rmin``/``rmax``
# range. The last set of an exercise still charges rest, which stands in for
# the setup/transition to the next station; that keeps the model simple and
# slightly conservative (it never under-estimates a session into overrun).
#
# On top of the working sets:
#   * ``_SESSION_OVERHEAD_SECONDS`` — the fixed general warm-up the planner
#     always prescribes ("5 דקות תנועה קלה") plus a minute of wrap-up;
#   * ``_WARMUP_SECONDS_PER_EXERCISE`` — the per-exercise warm-up ramp sets
#     that ``warmup_sets()`` attaches (1-3 short sets with short rests).
#
# ASSUMPTIONS (stated so they can be revisited with real logged data):
#   * a controlled 3 s/rep tempo for every exercise;
#   * prescribed rest is actually taken;
#   * no supersets, no circuits, single trainee, equipment available on
#     arrival at each station.
_SECONDS_PER_REP = 3.0
_SESSION_OVERHEAD_SECONDS = 6 * 60
_WARMUP_SECONDS_PER_EXERCISE = 60


def estimate_exercise_seconds(exercise: dict[str, Any]) -> float:
    """Estimated wall-clock seconds for one exercise (working sets + ramp).

    See the duration-model note above for the assumptions behind this.
    """
    try:
        sets = int(exercise.get("sets") or 0)
    except (TypeError, ValueError):
        sets = 0
    if sets <= 0:
        return 0.0
    try:
        rmin = int(exercise.get("rmin") or 0)
        rmax = int(exercise.get("rmax") or 0)
    except (TypeError, ValueError):
        rmin = rmax = 0
    reps = (rmin + rmax) / 2.0 if rmin and rmax >= rmin else float(rmin or rmax or 10)
    try:
        rest = float(exercise.get("rest") or 90)
    except (TypeError, ValueError):
        rest = 90.0
    per_set = reps * _SECONDS_PER_REP + rest
    return sets * per_set + _WARMUP_SECONDS_PER_EXERCISE


def estimate_session_minutes(session_or_exercises: Any) -> float:
    """Estimated duration in minutes for a session (or a bare exercise list).

    Accepts either a session dict (uses its ``exercises``) or the list itself,
    so callers can price a candidate trim without building a session first.
    """
    if isinstance(session_or_exercises, dict):
        exercises = session_or_exercises.get("exercises") or []
    else:
        exercises = session_or_exercises or []
    if not exercises:
        return 0.0
    total = _SESSION_OVERHEAD_SECONDS + sum(
        estimate_exercise_seconds(item) for item in exercises if isinstance(item, dict)
    )
    return total / 60.0


# Movement patterns that carry the session's primary training stimulus. These
# are protected from removal for as long as the time budget allows: dropping a
# squat pattern to keep a lateral raise would be a worse plan, not a shorter
# one. Everything else counts as lower-priority accessory volume.
_PRIMARY_MOVEMENTS = frozenset(
    {
        "horizontal_push",
        "vertical_push",
        "horizontal_pull",
        "vertical_pull",
        "squat",
        "hinge",
    }
)

# Trimming floors: an exercise is never reduced below this many working sets
# (below it the exercise stops being a meaningful stimulus and should be
# dropped instead), and a fitted session always keeps at least this many
# exercises so "fitting" can never degenerate into an empty plan.
_MIN_FITTED_SETS = 2
_MIN_FITTED_EXERCISES = 1


def _is_accessory(exercise: dict[str, Any]) -> bool:
    """True when the exercise is lower-priority accessory volume."""
    profile = CATALOG.get(str(exercise.get("id") or ""))
    if profile is None:
        # Unknown to the catalog: treat as accessory so an unrecognised
        # add-on is trimmed before a known compound.
        return True
    return profile.movement not in _PRIMARY_MOVEMENTS


def fit_session_to_minutes(
    session: dict[str, Any],
    minutes: int,
    *,
    tolerance_minutes: float = 0.0,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Trim a session so its estimated duration fits ``minutes``.

    TASK-B1. The prescribed main session used to ignore the user's available
    time entirely, so a 30-minute user and a 90-minute user received identical
    volume. This fits the canonical session to the resolved time budget.

    The operation is **trim-only**: a session that already fits is returned
    unchanged (never padded to fill a longer slot). Trimming happens in
    increasing order of damage to the program:

    1. reduce sets on lower-priority accessory work (down to
       ``_MIN_FITTED_SETS``);
    2. drop accessory exercises entirely (last one first, so the earliest —
       and generally most important — accessory survives longest);
    3. only if still over budget, reduce sets on primary compounds;
    4. as a last resort, drop trailing primary compounds, always keeping at
       least ``_MIN_FITTED_EXERCISES``.

    Movement-pattern coverage is preserved as far as the budget permits: a
    primary movement is only dropped once every accessory is already gone and
    every remaining exercise is at the set floor.

    Returns ``(fitted_session, changes)``. The input session and its exercise
    dicts are never mutated — exercises are copied before any edit — so shared
    templates upstream (``PLANS``) can never be written through.
    """
    budget = max(0.0, float(minutes)) + max(0.0, float(tolerance_minutes))
    exercises = [
        dict(item) for item in (session.get("exercises") or []) if isinstance(item, dict)
    ]
    changes: list[dict[str, Any]] = []
    if not exercises or budget <= 0:
        return {**session, "exercises": exercises}, changes

    def over_budget() -> bool:
        return estimate_session_minutes(exercises) > budget

    if not over_budget():
        # Already fits: trim-only means we leave it exactly as it is.
        return {**session, "exercises": exercises}, changes

    def reduce_sets(accessory: bool) -> bool:
        """Shave one set off the last eligible exercise. True if anything changed."""
        for index in range(len(exercises) - 1, -1, -1):
            entry = exercises[index]
            if _is_accessory(entry) is not accessory:
                continue
            try:
                sets = int(entry.get("sets") or 0)
            except (TypeError, ValueError):
                continue
            if sets <= _MIN_FITTED_SETS:
                continue
            entry["sets"] = sets - 1
            changes.append(
                {
                    "exercise": entry.get("name") or entry.get("id"),
                    "action": "reduced_sets",
                    "from_sets": sets,
                    "to_sets": sets - 1,
                    "reason": "time_budget",
                }
            )
            return True
        return False

    def drop_exercise(accessory: bool) -> bool:
        """Drop the last eligible exercise. True if anything changed."""
        if len(exercises) <= _MIN_FITTED_EXERCISES:
            return False
        for index in range(len(exercises) - 1, -1, -1):
            entry = exercises[index]
            if _is_accessory(entry) is not accessory:
                continue
            exercises.pop(index)
            changes.append(
                {
                    "exercise": entry.get("name") or entry.get("id"),
                    "action": "removed",
                    "reason": "time_budget",
                }
            )
            return True
        return False

    # Escalating trim ladder; each step is retried until it stops helping.
    for step in (
        lambda: reduce_sets(True),
        lambda: drop_exercise(True),
        lambda: reduce_sets(False),
        lambda: drop_exercise(False),
    ):
        while over_budget() and step():
            pass
        if not over_budget():
            break

    return {**session, "exercises": exercises}, changes


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
