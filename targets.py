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

# E1 (expert review): a flat -450 kcal deficit is unsafe for a light person
# (can exceed 25% of maintenance) and too gentle for a heavy one. When a goal
# timeframe is known, the deficit is derived from the implied weekly rate
# instead, clamped to a safe percentage of maintenance.
MIN_DEFICIT_PCT_OF_MAINTENANCE = 0.10
MAX_DEFICIT_PCT_OF_MAINTENANCE = 0.25
KCAL_PER_KG_FAT = 7700


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
    goal_timeframe_weeks: float | None = None,
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

    # E1 (expert review, CODEX_MASTER_WORK_PLAN chapter 11): a flat -450 kcal
    # deficit ignores maintenance — for a light person it can exceed 25% of
    # maintenance (unsafe/unsustainable); for a heavy person it is too gentle.
    # When a target weight AND timeframe are both known, derive the deficit
    # from the implied weekly rate of change instead, clamped to a safe
    # percentage of maintenance. Muscle-gain/strength surpluses use the same
    # clamp (in the positive direction) so an aggressive bulk rate cannot
    # produce an absurd surplus either.
    adjustment = GOAL_ADJUSTMENT.get(goal_type, 0)
    rate_based_kg_per_week: float | None = None
    if (
        goal_weight_kg is not None
        and goal_timeframe_weeks is not None
        and goal_timeframe_weeks > 0
        and goal_weight_kg != weight_kg
    ):
        rate_based_kg_per_week = (weight_kg - goal_weight_kg) / goal_timeframe_weeks
        implied_daily_adjustment = -(rate_based_kg_per_week * KCAL_PER_KG_FAT) / 7
        max_magnitude = maintenance * MAX_DEFICIT_PCT_OF_MAINTENANCE
        min_magnitude = maintenance * MIN_DEFICIT_PCT_OF_MAINTENANCE
        sign = 1 if implied_daily_adjustment >= 0 else -1
        magnitude = abs(implied_daily_adjustment)
        # Only enforce the safety floor when the goal actually calls for a
        # deficit/surplus in that direction; a near-zero implied rate (e.g.
        # the user is basically at their goal weight) should not be forced
        # into an artificial 10% deficit.
        if magnitude > 5:
            magnitude = max(min_magnitude, min(max_magnitude, magnitude))
            adjustment = sign * magnitude
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

    # D1: nudge gently above the current average instead of jumping straight
    # to an 8,000-step floor — a user averaging ~4,800 steps/day was getting a
    # +67% overnight target, which is neither realistic nor sustainable.
    # A ~12% increase (capped at +1,500/day) is a normal, achievable step-up;
    # re-evaluate and raise again once the user is consistently hitting it.
    nudged = steps * 1.12
    nudged = min(nudged, steps + 1500)
    steps_target = int(min(12000, max(6000, round(nudged / 500) * 500)))

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
            "goal_timeframe_weeks": goal_timeframe_weeks,
            "rate_based_kg_per_week": (
                round(rate_based_kg_per_week, 2) if rate_based_kg_per_week is not None else None
            ),
            "daily_adjustment": round(adjustment),
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
    rate = b.get("rate_based_kg_per_week")
    if rate is not None:
        direction = "ירידה" if rate > 0 else "עלייה"
        text += f" הקצב המשוער לפי היעד והזמן שבחרת: כ-{abs(rate):.2f} ק\"ג {direction} בשבוע."
        body_weight = float(b.get("weight_kg") or 0)
        aggressive_loss = rate > 1.0 or (rate > 0 and body_weight > 0 and rate / body_weight > 0.01)
        if aggressive_loss:
            text += (
                " שים לב: זה קצב ירידה אגרסיבי. אפשר לבחור יעד מאוזן יותר כדי "
                "לשמור על ביצועים, התאוששות ובריאות."
            )
    if t.provisional:
        missing_he = ", ".join(user_model.display_label(k) for k in (t.missing_inputs or []))
        text += f"\n⚠️ יעד זמני — חסרים: {missing_he}. השלם כדי לדייק."
    return text


@dataclass
class GoalFeasibility:
    """TASK-18: whether the proposed calorie target can reach the requested
    goal weight within the requested timeline.

    All values are estimates.  ``applicable`` is False when there is no
    weight-change goal + timeline to validate against (nothing to check).
    """
    applicable: bool
    feasible: bool
    implied_daily_deficit: int            # maintenance - intake (positive = deficit)
    actual_weekly_rate_kg: float          # kg/week the target actually produces
    requested_weekly_rate_kg: float       # kg/week the requested goal+timeline demands
    projected_weight_at_deadline_kg: float
    realistic_weeks_for_target: int | None
    message: str = ""
    # TASK-4: a direct, decision-oriented recommendation (what the system
    # professionally recommends), separate from the neutral explanation above.
    recommendation: str = ""


# A calorie target "achieves" the requested goal when its projected weight at
# the deadline is within this tolerance of the requested target weight.
_FEASIBILITY_TOLERANCE_KG = 1.5


def assess_goal_feasibility(t: Targets) -> GoalFeasibility:
    """Validate the calorie target against the requested goal weight + timeline.

    The daily target is clamped to a safe percentage of maintenance in
    ``compute_targets``.  When the requested rate exceeds that clamp, the target
    will *not* reach the goal by the deadline — this surfaces that gap instead
    of presenting the clamped target as if it met the requested timeline.
    Everything is deterministic and framed as an estimate.
    """
    b = t.basis
    weight = b.get("weight_kg")
    goal_weight = b.get("goal_weight_kg")
    weeks = b.get("goal_timeframe_weeks")
    if (
        weight is None
        or goal_weight is None
        or weeks is None
        or float(weeks) <= 0
        or float(goal_weight) == float(weight)
    ):
        return GoalFeasibility(
            applicable=False,
            feasible=True,
            implied_daily_deficit=0,
            actual_weekly_rate_kg=0.0,
            requested_weekly_rate_kg=0.0,
            projected_weight_at_deadline_kg=float(weight) if weight is not None else 0.0,
            realistic_weeks_for_target=None,
        )

    weight = float(weight)
    goal_weight = float(goal_weight)
    weeks = float(weeks)
    is_loss = goal_weight < weight

    # The target the user will actually eat produces this deficit/surplus.
    implied_daily = t.maintenance - t.calories  # +deficit when eating below TDEE
    actual_weekly_rate = implied_daily * 7 / KCAL_PER_KG_FAT  # kg/week lost (loss = +)
    requested_weekly_rate = (weight - goal_weight) / weeks     # loss = +

    # Projected weight if the user follows the (clamped) target for the window.
    projected = weight - actual_weekly_rate * weeks

    total_change_needed = abs(weight - goal_weight)
    realistic_weeks: int | None = None
    if abs(actual_weekly_rate) > 1e-3 and (actual_weekly_rate > 0) == is_loss:
        realistic_weeks = max(1, round(total_change_needed / abs(actual_weekly_rate)))

    feasible = abs(projected - goal_weight) <= _FEASIBILITY_TOLERANCE_KG

    direction = "ירידה" if is_loss else "עלייה"
    recommendation = ""
    requested_weeks = int(round(weeks))
    if feasible:
        message = (
            f"בקצב המשוער של כ-{abs(actual_weekly_rate):.2f} ק\"ג {direction} בשבוע, "
            f"היעד של {goal_weight:g} ק\"ג צפוי להיות בהישג יד בערך בתוך הזמן שבחרת. "
            "מדובר בהערכה שתתעדכן לפי המגמה בפועל."
        )
    else:
        projected_change = abs(weight - projected)
        # TASK-4: the message states the requested outcome and the gap once —
        # without repeating the deficit math across several warning paragraphs.
        message = (
            f"ביקשת להגיע מ-{weight:g} ק\"ג ל-{goal_weight:g} ק\"ג בתוך "
            f"{requested_weeks} שבועות. בקצב הבטוח של כ-{abs(actual_weekly_rate):.2f} "
            f"ק\"ג {direction} בשבוע הצפי בתקופה הזו הוא כ-{projected_change:.1f} ק\"ג "
            f"(כ-{projected:.1f} ק\"ג), כלומר היעד המקורי כנראה לא יושג בזמן שביקשת."
        )
        # TASK-4: a single direct recommendation with the practical reasoning,
        # instead of "here is your target, warning: it doesn't reach your goal".
        weeks_text = (
            f"כ-{realistic_weeks} שבועות" if realistic_weeks is not None else "טווח זמן ארוך יותר"
        )
        recommendation = (
            f"ההמלצה שלי: לשמור על יעד של {t.calories:,} קל׳ ביום ולהאריך את היעד "
            f"ל-{weeks_text}, עם התאמה לפי קצב ה{direction} בפועל. "
            "קצב מתון יותר שומר על מסת שריר, קל יותר להתמדה, מאפשר התאוששות "
            "וביצועים באימונים, ומוביל לתוצאה בת-קיימא. כל המספרים הם הערכה."
        )

    return GoalFeasibility(
        applicable=True,
        feasible=feasible,
        implied_daily_deficit=int(round(implied_daily)),
        actual_weekly_rate_kg=round(actual_weekly_rate, 2),
        requested_weekly_rate_kg=round(requested_weekly_rate, 2),
        projected_weight_at_deadline_kg=round(projected, 1),
        realistic_weeks_for_target=realistic_weeks,
        message=message,
        recommendation=recommendation,
    )
