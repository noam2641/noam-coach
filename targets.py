"""Personalized daily targets — the bridge from the user model to numbers.

The coach collects weight, activity (steps), goal, and (once) sex/age/height,
but until now the calorie target was a static default. This module turns those
facts into a real, explainable daily calorie + protein + steps target so the
"living file" actually drives the recommendations.

Pure and dependency-free so it is trivially testable. Conservative defaults are
used when sex/age are unknown (the user is asked once, just-in-time).
"""

from __future__ import annotations

from dataclasses import dataclass

import user_model

# Goal → daily calorie adjustment (kcal) applied to maintenance.
GOAL_ADJUSTMENT = {
    "fat_loss_muscle_retention": -450,
    "muscle_gain": +250,
    "strength": +150,
    "general_health": 0,
}

# Goal → protein grams per kg bodyweight.
GOAL_PROTEIN_PER_KG = {
    "fat_loss_muscle_retention": 2.0,  # higher protein protects muscle in a deficit
    "muscle_gain": 1.8,
    "strength": 1.8,
    "general_health": 1.6,
}

# Conservative defaults when a fact is missing.
DEFAULT_SEX = "male"
DEFAULT_AGE = 30
DEFAULT_HEIGHT_CM = 175.0
DEFAULT_STEPS = 7000

# Safety floors so a target is never unhealthily low.
MIN_CALORIES = 1400
MIN_PROTEIN = 90


@dataclass
class Targets:
    calories: int
    protein: int
    steps: int
    maintenance: int  # estimated maintenance (TDEE) before adjustment
    basis: dict  # inputs used, for an explainable provenance line
    provisional: bool = False  # True when key inputs were assumed
    missing_inputs: list[str] | None = None  # which inputs were defaulted


# Piecewise-linear activity factor breakpoints (steps → multiplier).
# Using interpolation avoids the cliff where 8999 → 1.4 but 9000 → 1.55.
_ACTIVITY_BREAKPOINTS = [
    (0, 1.2),
    (3000, 1.3),
    (6000, 1.4),
    (9000, 1.55),
    (12000, 1.7),
]


def _activity_factor(avg_steps: float) -> float:
    """Map average daily steps to a TDEE activity multiplier (interpolated)."""
    if avg_steps <= _ACTIVITY_BREAKPOINTS[0][0]:
        return _ACTIVITY_BREAKPOINTS[0][1]
    for i in range(1, len(_ACTIVITY_BREAKPOINTS)):
        s1, f1 = _ACTIVITY_BREAKPOINTS[i - 1]
        s2, f2 = _ACTIVITY_BREAKPOINTS[i]
        if avg_steps <= s2:
            ratio = (avg_steps - s1) / (s2 - s1)
            return f1 + ratio * (f2 - f1)
    return _ACTIVITY_BREAKPOINTS[-1][1]


def mifflin_st_jeor(weight_kg: float, height_cm: float, age: int, sex: str) -> float:
    """Resting metabolic rate (BMR), Mifflin-St Jeor."""
    base = 10 * weight_kg + 6.25 * height_cm - 5 * age
    return base + (5 if sex == "male" else -161)


def compute_targets(
    weight_kg: float,
    *,
    avg_steps: float | None = None,
    goal_type: str = "fat_loss_muscle_retention",
    sex: str | None = None,
    height_cm: float | None = None,
    age: int | None = None,
    workouts_per_week: float | None = None,
    goal_weight_kg: float | None = None,
    body_fat_pct: float | None = None,
) -> Targets:
    """Compute daily calorie/protein/steps targets from the user's facts."""
    # Track which inputs were assumed BEFORE applying defaults.
    missing: list[str] = []
    if sex is None:
        missing.append("sex")
    if age is None:
        missing.append("age")
    if height_cm is None:
        missing.append("height_cm")
    if avg_steps is None:
        missing.append("avg_steps")

    sex = sex if sex is not None else DEFAULT_SEX
    height_cm = height_cm if height_cm is not None else DEFAULT_HEIGHT_CM
    age = age if age is not None else DEFAULT_AGE
    steps = DEFAULT_STEPS if avg_steps is None else avg_steps

    bmr = mifflin_st_jeor(weight_kg, height_cm, age, sex)
    factor = _activity_factor(steps)
    maintenance = bmr * factor
    # Steps already capture part of training activity.  Add only a modest
    # workout bonus and taper it when ambient activity is already high.
    workout_bonus = 0.0
    if workouts_per_week:
        per_session = 35 if steps < 9000 else 20
        workout_bonus = min(150, workouts_per_week * per_session)
        maintenance += workout_bonus

    adjustment = GOAL_ADJUSTMENT.get(goal_type, 0)
    calories = max(MIN_CALORIES, round((maintenance + adjustment) / 10) * 10)

    protein_per_kg = GOAL_PROTEIN_PER_KG.get(goal_type, 1.8)
    protein_reference_weight = weight_kg
    if goal_weight_kg is not None and 35 <= goal_weight_kg < weight_kg:
        # Avoid prescribing protein from body mass the user explicitly plans
        # to lose, while keeping the reference conservative.
        protein_reference_weight = max(goal_weight_kg, weight_kg * 0.75)
    elif body_fat_pct is not None and 5 <= body_fat_pct <= 65:
        lean_mass = weight_kg * (1 - body_fat_pct / 100)
        protein_reference_weight = min(weight_kg, lean_mass * 1.25)
    protein = max(
        MIN_PROTEIN,
        min(220, round(protein_reference_weight * protein_per_kg / 5) * 5),
    )

    # Steps target: nudge a little above current average, capped.
    steps_target = int(min(12000, max(8000, round(steps / 500) * 500 + 500)))

    provisional = bool(missing)

    return Targets(
        calories=int(calories),
        protein=int(protein),
        steps=steps_target,
        maintenance=int(round(maintenance)),
        basis={
            "weight_kg": weight_kg,
            "avg_steps": round(steps),
            "goal_type": goal_type,
            "sex": sex,
            "height_cm": height_cm,
            "age": age,
            "activity_factor": round(factor, 4),
            "workout_bonus": round(workout_bonus),
            "protein_reference_weight": round(protein_reference_weight, 1),
            "goal_weight_kg": goal_weight_kg,
            "body_fat_pct": body_fat_pct,
            "assumed": {k: True for k in missing} if missing else {},
        },
        provisional=provisional,
        missing_inputs=missing if missing else None,
    )


def explain_targets(t: Targets) -> str:
    """Short Hebrew explanation of how the target was derived (chapter 14)."""
    b = t.basis
    goal_he = {
        "fat_loss_muscle_retention": "ירידה בשומן עם שמירת שריר",
        "muscle_gain": "עלייה במסה",
        "strength": "כוח",
        "general_health": "בריאות כללית",
    }.get(b["goal_type"], b["goal_type"])
    text = (
        f'היעד חושב ממשקל {b["weight_kg"]:g} ק"ג, פעילות של ~{b["avg_steps"]:,} '
        f"צעדים ביום ומטרה: {goal_he}. אחזקה מוערכת ~{t.maintenance:,} קל׳, "
        f"ומכאן יעד יומי של {t.calories:,} קל׳ ו-{t.protein} ג׳ חלבון."
    )
    if t.provisional:
        missing_he = ", ".join(user_model.display_label(k) for k in (t.missing_inputs or []))
        text += f"\n⚠️ יעד זמני — חסרים: {missing_he}. השלם כדי לדייק."
    return text
