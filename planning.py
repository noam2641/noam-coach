"""Constraint-driven personal planning for goals, nutrition and workouts.

The planner deliberately does not ask an LLM to invent an unconstrained plan.
It first builds a verified profile, applies hard constraints, creates three
meaningfully different candidates, validates them, persists them as versions,
and only activates the candidate explicitly selected by the user.
"""

from __future__ import annotations

import copy
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

import targets
import training_intelligence
import user_model
from exercise_plans import (
    MAX_FREQUENCY,
    MIN_FREQUENCY,
    PLANS,
    SPLIT_BY_FREQUENCY,
    weekday_he,
)
from helpers import utc_now
from noam_coach.services.food_environment import (
    normalize_food_environment_context,
    personal_fit_signals,
)
from noam_coach.services.weekdays import (
    WEEKDAY_SCHEMA_VERSION,
    normalize_weekday,
    sunday_first_order,
    with_weekday_schema,
)

PlanType = Literal["nutrition", "workout", "unified"]


class PlanningBlockedError(ValueError):
    """The requested plan cannot be safely generated or activated yet."""

    def __init__(self, message: str, *, missing: list[str] | None = None) -> None:
        super().__init__(message)
        self.missing = missing or []




@dataclass(frozen=True)
class Constraint:
    key: str
    value: Any
    hard: bool
    reason: str


@dataclass
class PlanCandidate:
    plan_type: PlanType
    title: str
    strategy: str
    score: float
    rationale: list[str]
    tradeoffs: list[str]
    assumptions: list[str]
    payload: dict[str, Any]
    id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GoalProposal:
    calories: int
    protein: int
    steps: int
    maintenance: int
    phase: str
    provisional: bool
    missing_inputs: list[str]
    explanation: str
    confidence: float
    basis: dict[str, Any] = field(default_factory=dict)


def _fact_label(key: str) -> str:
    """Return a friendly Hebrew label for a fact key via user_model.display_label."""
    return user_model.display_label(key)


# Legacy dict kept for backward compatibility; delegates to centralized labels.
FACT_LABELS = {key: user_model.display_label(key) for key in (
    "weight_kg", "height_cm", "age", "sex", "primary_goal",
    "training_days_per_week", "session_minutes", "training_location",
    "equipment", "strength_experience", "training_limitations", "active_pain", "medical_avoidance",
    "diet_restrictions", "allergies", "work_schedule", "meal_break_info",
    "cooking_capacity", "workout_window", "weekly_availability",
)}


def _fact_value(facts: dict[str, dict[str, Any]], key: str, default: Any = None) -> Any:
    fact = facts.get(key)
    if not fact or fact.get("kind") == user_model.KIND_GAP:
        return default
    return fact.get("value", default)


async def collect_facts(db: Any, user_id: int) -> dict[str, dict[str, Any]]:
    rows = await db.fetch_all(
        "SELECT * FROM user_facts WHERE user_id=? AND valid=1",
        (user_id,),
    )
    facts: dict[str, dict[str, Any]] = {}
    for row in rows:
        hydrated = dict(row)
        hydrated["value"] = json.loads(row["value"]) if row.get("value") else None
        hydrated["affects"] = json.loads(row["affects"]) if row.get("affects") else []
        hydrated["confirmed"] = bool(row.get("confirmed"))
        facts[row["key"]] = hydrated
    if "training_limitations" not in facts:
        limitation_fact = await user_model.get_training_limitations_fact(db, user_id)
        if limitation_fact is not None:
            facts["training_limitations"] = limitation_fact
    return facts


async def profile_snapshot(db: Any, user_id: int) -> dict[str, Any]:
    facts = await collect_facts(db, user_id)
    readiness = await user_model.compute_all_readiness(db, user_id)
    known: dict[str, Any] = {}
    inferred: dict[str, Any] = {}
    assumptions: dict[str, Any] = {}
    missing: list[str] = []
    for key, spec in user_model.FACT_REGISTRY.items():
        fact = facts.get(key)
        if not fact or fact.get("kind") == user_model.KIND_GAP:
            if spec.visibility != "internal" and spec.required_for:
                missing.append(key)
            continue
        if fact.get("kind") == user_model.KIND_ESTIMATE and not fact.get("confirmed"):
            inferred[key] = fact["value"]
        elif fact.get("source") == user_model.SOURCE_SYSTEM:
            assumptions[key] = fact["value"]
        else:
            known[key] = fact["value"]
    return {
        "facts": facts,
        "known": known,
        "inferred": inferred,
        "assumptions": assumptions,
        "missing": sorted(set(missing)),
        "readiness": readiness,
        "created_at": utc_now(),
    }


async def build_goal_proposal(db: Any, user_id: int) -> GoalProposal:
    facts = await collect_facts(db, user_id)
    weight = _fact_value(facts, "weight_kg")
    if weight is None:
        raise ValueError("חסר משקל נוכחי לחישוב יעד")
    goal = _fact_value(facts, "primary_goal", "fat_loss_muscle_retention")
    avg_steps = _fact_value(facts, "avg_steps")
    workouts = _fact_value(facts, "training_days_per_week")
    timeframe = _fact_value(facts, "goal_timeframe_weeks")
    result = targets.compute_targets(
        float(weight),
        avg_steps=float(avg_steps) if avg_steps is not None else None,
        goal_type=str(goal),
        sex=_fact_value(facts, "sex"),
        height_cm=(float(_fact_value(facts, "height_cm")) if _fact_value(facts, "height_cm") is not None else None),
        age=(int(_fact_value(facts, "age")) if _fact_value(facts, "age") is not None else None),
        workouts_per_week=(float(workouts) if workouts is not None else None),
        goal_weight_kg=(float(_fact_value(facts, "goal_weight_kg")) if _fact_value(facts, "goal_weight_kg") is not None else None),
        body_fat_pct=(float(_fact_value(facts, "body_fat_pct")) if _fact_value(facts, "body_fat_pct") is not None else None),
        goal_timeframe_weeks=(float(timeframe) if timeframe is not None else None),
    )
    confidence = 0.92 - 0.1 * len(result.missing_inputs or [])
    confidence = max(0.45, round(confidence, 2))
    return GoalProposal(
        calories=result.calories,
        protein=result.protein,
        steps=result.steps,
        maintenance=result.maintenance,
        phase=str(goal),
        provisional=result.provisional,
        missing_inputs=result.missing_inputs or [],
        explanation=targets.explain_targets(result),
        confidence=confidence,
        basis=result.basis,
    )


async def persist_goal_proposal(db: Any, user_id: int, proposal: GoalProposal) -> int:
    status = "provisional" if proposal.provisional else "proposed"
    return await db.execute(
        """
        INSERT INTO goal_versions(
            user_id, calories, protein, steps, phase, status, source,
            explanation, created_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            proposal.calories,
            proposal.protein,
            proposal.steps,
            proposal.phase,
            status,
            "computed_provisional" if proposal.provisional else "computed",
            proposal.explanation,
            utc_now(),
        ),
    )


# Facts that must be present (real, non-gap) before a goal can be activated.
GOAL_REQUIRED_FACTS = ("weight_kg", "primary_goal", "sex", "age")


async def missing_goal_inputs(db: Any, user_id: int) -> list[str]:
    """Return the human labels of mandatory goal facts that are still missing."""
    facts = await collect_facts(db, user_id)
    missing: list[str] = []
    for key in GOAL_REQUIRED_FACTS:
        fact = facts.get(key)
        if not fact or fact.get("kind") == user_model.KIND_GAP or fact.get("value") in (None, "", "none"):
            missing.append(FACT_LABELS.get(key, key))
    return missing


class GoalNotReady(Exception):
    """A provisional goal cannot be activated while mandatory data is missing."""

    def __init__(self, missing: list[str]) -> None:
        self.missing = missing
        super().__init__("goal is provisional / missing required data")


async def activate_goal(db: Any, user_id: int, goal_id: int) -> bool:
    async with db.transaction() as conn:
        cursor = await conn.execute(
            "SELECT id, status, source FROM goal_versions WHERE id=? AND user_id=?",
            (goal_id, user_id),
        )
        target = await cursor.fetchone()
        if not target:
            return False
        # A COMPUTED goal may not become fully active while MANDATORY data
        # (weight, goal, sex, age) is missing — those make the calorie target
        # meaningful. Soft gaps (e.g. avg_steps) have defaults and do not block.
        # A manually-set goal is an explicit user value and is allowed (P0).
        source = (target["source"] or "") if "source" in target.keys() else ""
        if not str(source).startswith("manual"):
            missing = await missing_goal_inputs(db, user_id)
            if missing:
                raise GoalNotReady(missing)
        now = utc_now()
        await conn.execute(
            "UPDATE goal_versions SET status='superseded', decided_at=? "
            "WHERE user_id=? AND status IN ('active', 'active_provisional') AND id!=?",
            (now, user_id, goal_id),
        )
        await conn.execute(
            "UPDATE goal_versions SET status='active', decided_at=? WHERE id=? AND user_id=?",
            (now, goal_id, user_id),
        )
        cursor = await conn.execute(
            "SELECT calories, protein, steps, phase FROM goal_versions WHERE id=?",
            (goal_id,),
        )
        row = await cursor.fetchone()
        await conn.execute(
            """
            INSERT INTO goals(user_id, calories, protein, steps, phase, updated_at)
            VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                calories=excluded.calories, protein=excluded.protein,
                steps=excluded.steps, phase=excluded.phase, updated_at=excluded.updated_at
            """,
            (user_id, row["calories"], row["protein"], row["steps"], row["phase"], now),
        )
    return True


async def active_goal(db: Any, user_id: int) -> dict[str, Any] | None:
    # Include 'active_provisional' so a temporary goal the user chose to use is
    # still the current goal for display, while remaining flagged as provisional.
    return await db.fetch_one(
        "SELECT * FROM goal_versions WHERE user_id=? "
        "AND status IN ('active', 'active_provisional') ORDER BY id DESC LIMIT 1",
        (user_id,),
    )


def _normalize_restrictions(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False)
    return {part.strip().casefold() for part in text.replace("/", ",").split(",") if part.strip()}


def _protein_options(restrictions: set[str], canonical_ids: set[str] | None = None) -> list[str]:
    """Return protein source suggestions respecting dietary restrictions.

    *canonical_ids* (if provided) is a set of canonical restriction IDs
    from the dietary-restriction service (e.g. ``{"dairy", "eggs"}``).
    When supplied, individual options are filtered against those IDs so
    the nutrition plan never recommends a restricted protein source.
    """
    vegan = any("טבע" in x or "vegan" in x for x in restrictions)
    vegetarian = vegan or any("צמח" in x or "vegetarian" in x for x in restrictions)
    if vegan:
        base = ["טופו/טמפה", "עדשים וקטניות", "סייטן", "יוגורט סויה עתיר חלבון"]
    elif vegetarian:
        base = ["ביצים", "גבינה/יוגורט עתיר חלבון", "טופו", "קטניות"]
    else:
        base = ["עוף/הודו", "דג", "ביצים", "יוגורט/גבינה", "טופו"]

    # REC-PROGRAM-04-10: Filter options against canonical restriction IDs
    if canonical_ids:
        _OPTION_CANONICAL: dict[str, set[str]] = {
            "ביצים": {"eggs"},
            "גבינה/יוגורט עתיר חלבון": {"dairy"},
            "יוגורט/גבינה": {"dairy"},
            "יוגורט סויה עתיר חלבון": {"soy"},
            "טופו/טמפה": {"soy"},
            "טופו": {"soy"},
            "דג": {"fish"},
        }
        filtered = [opt for opt in base if not (_OPTION_CANONICAL.get(opt, set()) & canonical_ids)]
        # Always keep at least one option
        if not filtered:
            filtered = ["קטניות ועדשים"]
        base = filtered
    return base


def _meal_slots(
    calories: int,
    protein: int,
    strategy: str,
    restrictions: set[str],
    canonical_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    proteins = _protein_options(restrictions, canonical_ids)
    if strategy == "structured":
        ratios = [("ארוחת בוקר", 0.22), ("ארוחת צהריים", 0.34), ("ארוחת ביניים", 0.12), ("ארוחת ערב", 0.32)]
    elif strategy == "flexible":
        ratios = [("ארוחה 1", 0.28), ("ארוחה 2", 0.36), ("ארוחה 3", 0.26), ("רזרבה גמישה", 0.10)]
    else:
        ratios = [("ארוחה גדולה 1", 0.40), ("ארוחת חלבון מהירה", 0.18), ("ארוחה גדולה 2", 0.42)]
    slots: list[dict[str, Any]] = []
    for index, (name, ratio) in enumerate(ratios):
        slots.append(
            {
                "name": name,
                "calories": round(calories * ratio / 10) * 10,
                "protein": max(15, round(protein * ratio)),
                "options": [
                    f"{proteins[index % len(proteins)]} + פחמימה + ירקות",
                    f"{proteins[(index + 1) % len(proteins)]} + תוספת לפי היעד",
                    "חלופה אישית בעלת אותו מחיר תזונתי",
                ],
            }
        )
    # Correct rounding drift on the last slot.
    slots[-1]["calories"] += calories - sum(int(slot["calories"]) for slot in slots)
    slots[-1]["protein"] += protein - sum(int(slot["protein"]) for slot in slots)
    slots[-1]["protein"] = max(15, slots[-1]["protein"])
    return slots


def _workday_meal_times(facts: dict[str, dict[str, Any]], slot_count: int) -> list[str]:
    break_info = _fact_value(facts, "meal_break_info")
    work_schedule = _fact_value(facts, "work_schedule")
    wake = None
    if isinstance(work_schedule, dict):
        wake = work_schedule.get("wake_time")
    base = ["08:00", "12:30", "16:30", "20:30"] if slot_count == 4 else ["10:00", "15:00", "20:30"]
    if isinstance(break_info, dict) and break_info.get("time"):
        base[1 if len(base) > 1 else 0] = str(break_info["time"])
    elif isinstance(break_info, str) and ":" in break_info:
        base[1 if len(base) > 1 else 0] = break_info[:5]
    if wake and isinstance(wake, str) and ":" in wake:
        base[0] = wake[:5]
    return base[:slot_count]


def _nutrition_candidate(
    *,
    title: str,
    strategy: str,
    score: float,
    calories: int,
    protein: int,
    facts: dict[str, dict[str, Any]],
    restrictions: set[str],
    canonical_ids: set[str] | None = None,
    rationale: list[str],
    tradeoffs: list[str],
    assumptions: list[str],
) -> PlanCandidate:
    slots = _meal_slots(calories, protein, strategy, restrictions, canonical_ids)
    times = _workday_meal_times(facts, len(slots))
    for slot, time_text in zip(slots, times, strict=False):
        slot["time"] = time_text
    days = []
    for weekday in sunday_first_order(range(7)):
        day_slots = copy.deepcopy(slots)
        if weekday in {4, 5}:  # Friday/Saturday: preserve social flexibility.
            day_slots[-1]["name"] = "ארוחה משפחתית/חברתית"
            day_slots[-1]["options"] = [
                "מנה עיקרית + ירקות + תוספת אחת",
                "חלופה משפחתית תוך שמירה על חלבון",
                "שימוש ברזרבה גמישה של היום",
            ]
        days.append({"weekday": weekday, "weekday_name": weekday_he(weekday), "meals": day_slots})
    return PlanCandidate(
        plan_type="nutrition",
        title=title,
        strategy=strategy,
        score=round(max(0.0, min(1.0, score)), 2),
        rationale=rationale,
        tradeoffs=tradeoffs,
        assumptions=assumptions,
        payload={
            "calories": calories,
            "protein": protein,
            "days": days,
            "tracking": {
                "plan_adherence": True,
                "nutrition_adherence": True,
                "reporting_completeness": True,
            },
        },
    )


async def _require_readiness(db: Any, user_id: int, profile_name: str) -> dict[str, Any]:
    readiness = await user_model.compute_readiness(db, user_id, profile_name)
    if not readiness["ready"]:
        labels = [FACT_LABELS.get(key, key) for key in readiness["missing"]]
        raise PlanningBlockedError(
            "חסר מידע לפני בניית התוכנית: " + ", ".join(labels),
            missing=list(readiness["missing"]),
        )
    return readiness


async def build_nutrition_candidates(db: Any, user_id: int) -> list[PlanCandidate]:
    await _require_readiness(db, user_id, "nutrition")
    goal = await active_goal(db, user_id)
    if not goal:
        raise PlanningBlockedError(
            "צריך לאשר יעד לפני יצירת תוכניות תזונה",
            missing=["active_goal"],
        )
    facts = await collect_facts(db, user_id)
    restrictions = _normalize_restrictions(_fact_value(facts, "diet_restrictions"))
    restrictions |= _normalize_restrictions(_fact_value(facts, "allergies"))

    # REC-PROGRAM-04-10: Build canonical restriction IDs for constraint-safe filtering
    from noam_coach.services.dietary_restrictions import load_restrictions_from_facts
    diet_raw = _fact_value(facts, "diet_restrictions")
    allergy_raw = _fact_value(facts, "allergies")
    typed_restrictions = load_restrictions_from_facts(
        str(diet_raw) if diet_raw else None,
        str(allergy_raw) if allergy_raw else None,
    )
    canonical_ids = {r.canonical_id for r in typed_restrictions}

    food_environment = normalize_food_environment_context(_fact_value(facts, "food_environment_context", {}))
    food_signals = personal_fit_signals(food_environment)
    has_breaks = _fact_value(facts, "meal_break_info") is not None
    assumptions: list[str] = []
    if not restrictions:
        assumptions.append("לא דווחו מגבלות תזונתיות")
    common = {
        "calories": int(goal["calories"]),
        "protein": int(goal["protein"]),
        "facts": facts,
        "restrictions": restrictions,
        "canonical_ids": canonical_ids,
        "assumptions": assumptions,
    }
    structured_score = 0.9 if food_signals["prep_friendly"] and has_breaks else 0.78 if food_signals["prep_friendly"] else 0.7
    structured_rationale = [
        "פחות החלטות במהלך היום",
        "חלוקת חלבון עקבית",
    ]
    if food_signals["prep_friendly"]:
        structured_rationale.append("מתאימה להכנה מראש לפי סביבת האוכל שאישרת")

    flexible_score = 0.94 if food_signals["restaurant_or_delivery"] or food_signals["variable_schedule"] else 0.86
    flexible_rationale = [
        "שלוש חלופות לכל ארוחה",
        "שומרת רזרבה יומית",
    ]
    if food_signals["restaurant_or_delivery"]:
        flexible_rationale.append("מתאימה למסעדות או משלוחים לפי מה שדיווחת")
    if food_signals["variable_schedule"]:
        flexible_rationale.append("מתאימה לשעות משתנות לפי ההקשר שאישרת")

    low_effort_score = 0.93 if food_signals["low_cooking"] or food_signals["quick_or_limited_access"] else 0.79
    low_effort_rationale = [
        "פחות ארוחות",
        "מעט התעסקות יומית",
    ]
    if food_signals["quick_or_limited_access"]:
        low_effort_rationale.append("מתאימה לימים עמוסים או גישה מוגבלת לאוכל מסודר")
    if food_signals["low_cooking"]:
        low_effort_rationale.append("מותאמת למעט בישול לפי התשובה שלך")
    candidates = [
        _nutrition_candidate(
            title="מסודרת וקבועה",
            strategy="structured",
            score=structured_score,
            rationale=structured_rationale,
            tradeoffs=["פחות גמישה לשינויים ספונטניים"],
            **common,
        ),
        _nutrition_candidate(
            title="גמישה ומאוזנת",
            strategy="flexible",
            score=flexible_score,
            rationale=flexible_rationale,
            tradeoffs=["דורשת יותר בחירות", "נדרש מעקב אחר הרזרבה"],
            **common,
        ),
        _nutrition_candidate(
            title="מינימום התעסקות",
            strategy="low_effort",
            score=low_effort_score,
            rationale=low_effort_rationale,
            tradeoffs=["פחות גיוון", "ארוחות גדולות יותר"],
            **common,
        ),
    ]
    return candidates


def _parse_availability(value: Any) -> list[dict[str, Any]]:
    def _normalized_slot(item: dict[str, Any]) -> dict[str, Any] | None:
        normalized = normalize_weekday(item.get("weekday"), item.get("weekday_schema"))
        if normalized.weekday is None:
            return None
        slot = dict(item)
        slot["weekday"] = normalized.weekday
        slot["weekday_schema"] = WEEKDAY_SCHEMA_VERSION
        if normalized.needs_confirmation:
            slot["needs_weekday_confirmation"] = True
        return slot

    if isinstance(value, list):
        return [
            slot
            for item in value
            if isinstance(item, dict)
            for slot in [_normalized_slot(item)]
            if slot
        ]
    if isinstance(value, dict):
        if isinstance(value.get("days"), list):
            return [
                slot
                for item in value["days"]
                if isinstance(item, dict)
                for slot in [_normalized_slot(item)]
                if slot
            ]
        result = []
        for key, item in value.items():
            if isinstance(item, dict):
                slot = _normalized_slot({"weekday": key, **item})
                if slot:
                    result.append(slot)
        return result
    return []


def _default_days(frequency: int) -> list[int]:
    spreads = {
        1: [0],
        2: [0, 3],
        3: [0, 2, 4],
        4: [0, 1, 3, 5],
        5: [0, 1, 2, 4, 5],
        6: [0, 1, 2, 3, 4, 5],
    }
    return spreads.get(frequency, spreads[3])


def _schedule_sessions(
    frequency: int,
    availability: list[dict[str, Any]],
    *,
    default_minutes: int,
    default_start: str | None = None,
    split_override: list[str] | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    usable = [item for item in availability if item.get("available", True)]
    usable.sort(key=lambda item: int(item.get("weekday", 0)))
    assumed = len(usable) < frequency
    if assumed:
        selected = [
            with_weekday_schema({"weekday": day, "start": default_start, "minutes": default_minutes})
            for day in _default_days(frequency)
        ]
    else:
        selected = usable[:frequency]
    # RE10-3 / E3: allow a strategy-specific split (e.g. Full-Body x3 for
    # "consistency" at a 3-day frequency) instead of always the one fixed
    # split per frequency — this is what actually differentiates the three
    # workout-plan candidates beyond their marketing copy.
    split = split_override if split_override is not None else SPLIT_BY_FREQUENCY[frequency]
    sessions = []
    for index, (slot, code) in enumerate(zip(selected, split, strict=True)):
        sessions.append(
            {
                "index": index,
                "weekday": int(slot.get("weekday", 0)),
                "weekday_name": weekday_he(int(slot.get("weekday", 0))),
                "time": slot.get("start") or slot.get("time") or default_start,
                "minutes": int(slot.get("minutes") or default_minutes),
                "code": code,
                "name": PLANS[code]["name"],
                "exercises": copy.deepcopy(PLANS[code]["exercises"]),
            }
        )
    return sessions, assumed


# E3 (expert review): for a 3-day/week frequency, A/B/C trains legs only once
# a week (shared with shoulders in session C) — a real programming weakness
# for fat-loss / general-health goals where legs are the biggest calorie
# driver. Full-Body x3 trains every major muscle group 3x/week instead, and
# is the more evidence-based default for "consistency" (fewer decisions,
# lower per-session fatigue, better adherence) and for beginners generally.
_CONSISTENCY_SPLIT_OVERRIDES: dict[int, list[str]] = {
    3: ["F", "F", "F"],
    4: ["FB1", "FB2", "FB3", "FB4"],
}

_BALANCED_SPLIT_OVERRIDES: dict[int, list[str]] = {
    4: ["U1", "L1", "U2", "L2"],
}

_PERFORMANCE_SPLIT_OVERRIDES: dict[int, list[str]] = {
    4: ["A", "B", "C", "F"],
}


def _strategy_split_override(strategy: str, frequency: int) -> list[str] | None:
    """Return a strategy-specific split without dropping requested days.

    PATCH-11 / TASK-07: PATCH-10 introduced professional 4-day structures in
    ``exercise_plans.py`` but the plan builder still called this helper before
    it existed.  This is a real runtime gap: workout candidates could fail when
    generated, even though compileall passed.

    The override is deliberately conservative:
    * consistency = 4 varied full-body sessions;
    * balanced = true Upper/Lower for four days;
    * performance = ABC + Full Body.

    Unknown frequencies fall back to ``SPLIT_BY_FREQUENCY``.  We also validate
    that all referenced plan codes exist so a typo cannot silently ship.
    """
    mapping_by_strategy = {
        "consistency": _CONSISTENCY_SPLIT_OVERRIDES,
        "balanced": _BALANCED_SPLIT_OVERRIDES,
        "performance": _PERFORMANCE_SPLIT_OVERRIDES,
    }
    split = mapping_by_strategy.get(str(strategy or ""), {}).get(int(frequency))
    if split is None:
        return None
    if len(split) != int(frequency):
        return None
    if any(code not in PLANS for code in split):
        return None
    return list(split)

# E4: sets-per-exercise multiplier by strategy, bounded so consistency never
# drops below a minimally-effective volume and performance never exceeds a
# recoverable one for a non-advanced lifter (~6-20 sets/muscle/week at this
# per-session set count and frequency).
_STRATEGY_SET_DELTA: dict[str, int] = {
    "consistency": -1,
    "balanced": 0,
    "performance": +1,
}
_MIN_SETS_PER_EXERCISE = 2
_MAX_SETS_PER_EXERCISE = 5


def _apply_strategy_volume(sessions: list[dict[str, Any]], strategy: str) -> None:
    """Adjust sets-per-exercise by strategy (E4), bounded to a safe range."""
    delta = _STRATEGY_SET_DELTA.get(strategy, 0)
    if delta == 0:
        return
    for session in sessions:
        for exercise_entry in session["exercises"]:
            current = int(exercise_entry.get("sets", 3))
            exercise_entry["sets"] = max(
                _MIN_SETS_PER_EXERCISE, min(_MAX_SETS_PER_EXERCISE, current + delta)
            )


def workout_quality_issues(payload: dict[str, Any]) -> list[str]:
    sessions = payload.get("sessions") or []
    issues: list[str] = []
    if not sessions:
        return ["missing_sessions"]
    try:
        frequency = int(payload.get("frequency") or 0)
    except (TypeError, ValueError):
        frequency = 0
    if frequency and len(sessions) != frequency:
        issues.append("frequency_session_count_mismatch")
    seen_weekdays: set[int] = set()
    weekly_sets = 0
    total_exercise_slots = 0
    for session_index, session in enumerate(sessions, 1):
        if not str(session.get("code") or "").strip():
            issues.append(f"session_{session_index}_missing_code")
        if not str(session.get("name") or "").strip():
            issues.append(f"session_{session_index}_missing_name")
        try:
            weekday = int(session.get("weekday"))
        except (TypeError, ValueError):
            issues.append(f"session_{session_index}_invalid_weekday")
            weekday = -1
        if weekday in seen_weekdays:
            issues.append(f"session_{session_index}_duplicate_weekday")
        seen_weekdays.add(weekday)
        try:
            minutes = int(session.get("minutes") or 0)
        except (TypeError, ValueError):
            minutes = 0
        if minutes < 20 or minutes > 150:
            issues.append(f"session_{session_index}_invalid_duration")
        if not session.get("time"):
            issues.append(f"session_{session_index}_missing_time")
        exercises = session.get("exercises") or []
        if not exercises:
            issues.append(f"session_{session_index}_missing_exercises")
            continue
        total_exercise_slots += len(exercises)
        seen_exercises: set[str] = set()
        session_sets = 0
        for exercise_index, exercise in enumerate(exercises, 1):
            exercise_id = str(exercise.get("id") or "").strip()
            if not exercise_id:
                issues.append(f"session_{session_index}_exercise_{exercise_index}_missing_id")
            elif exercise_id in seen_exercises:
                issues.append(f"session_{session_index}_duplicate_exercise_{exercise_id}")
            seen_exercises.add(exercise_id)
            if not str(exercise.get("name") or "").strip():
                issues.append(f"session_{session_index}_exercise_{exercise_index}_missing_name")
            try:
                sets = int(exercise.get("sets") or 0)
                rmin = int(exercise.get("rmin") or 0)
                rmax = int(exercise.get("rmax") or 0)
            except (TypeError, ValueError):
                issues.append(f"session_{session_index}_exercise_{exercise_index}_invalid_prescription")
                continue
            if sets < 1 or sets > 6 or rmin < 1 or rmax < rmin or rmax > 30:
                issues.append(f"session_{session_index}_exercise_{exercise_index}_invalid_prescription")
            session_sets += max(0, sets)
        if session_sets > 32:
            issues.append(f"session_{session_index}_excessive_volume")
        weekly_sets += session_sets
    if weekly_sets > 120:
        issues.append("weekly_excessive_volume")
    if total_exercise_slots <= 1:
        issues.append("workout_plan_too_small")
    return sorted(set(issues))


def repair_workout_payload(payload: dict[str, Any], *, default_minutes: int = 45) -> dict[str, Any]:
    """Deterministically repair common workout-candidate defects in place-safe copy.

    Fixes the issues `workout_quality_issues` detects where a safe automatic
    correction exists: duplicate exercises within a session (drop later repeats),
    missing/invalid session time (default to 18:00), out-of-range duration
    (clamp), and out-of-range set/rep prescriptions (clamp). Defects with no safe
    auto-fix (e.g. duplicate weekdays, missing exercises entirely) are left for
    the caller to reject. Returns a new payload; the input is not mutated.
    """
    repaired = copy.deepcopy(payload)
    sessions = repaired.get("sessions") or []
    for session in sessions:
        if not session.get("code"):
            session["code"] = "custom"
        if not session.get("name"):
            session["name"] = "Workout"
        # Session time / duration.
        if not session.get("time"):
            session["time"] = "18:00"
        try:
            minutes = int(session.get("minutes") or 0)
        except (TypeError, ValueError):
            minutes = 0
        if minutes < 20 or minutes > 150:
            session["minutes"] = max(20, min(150, minutes or default_minutes))

        # Drop duplicate exercises (by id), keeping the first occurrence.
        seen: set[str] = set()
        deduped: list[dict[str, Any]] = []
        for exercise in session.get("exercises") or []:
            exercise_id = str(exercise.get("id") or "").strip()
            if exercise_id and exercise_id in seen:
                continue
            if exercise_id:
                seen.add(exercise_id)
            # Clamp obviously invalid prescriptions.
            try:
                sets = int(exercise.get("sets") or 0)
                rmin = int(exercise.get("rmin") or 0)
                rmax = int(exercise.get("rmax") or 0)
            except (TypeError, ValueError):
                sets, rmin, rmax = 0, 0, 0
            if sets:
                exercise["sets"] = max(1, min(6, sets))
            if rmin:
                exercise["rmin"] = max(1, min(30, rmin))
            if rmax:
                exercise["rmax"] = max(int(exercise.get("rmin") or 1), min(30, rmax))
            deduped.append(exercise)
        session["exercises"] = deduped
    return repaired


def _repair_workout_candidates(candidates: list["PlanCandidate"]) -> list["PlanCandidate"]:
    """Repair-or-drop workout candidates so a broken one is never displayed.

    Each candidate is repaired deterministically; if it still has quality issues
    after repair it is dropped. Score penalty for repaired candidates is removed
    once they pass validation so a clean repaired plan competes fairly.
    """
    healthy: list[PlanCandidate] = []
    for candidate in candidates:
        if candidate.plan_type != "workout":
            healthy.append(candidate)
            continue
        if not workout_quality_issues(candidate.payload):
            healthy.append(candidate)
            continue
        candidate.payload = repair_workout_payload(candidate.payload)
        if workout_quality_issues(candidate.payload):
            continue  # unrepairable -> never shown
        # Repaired successfully: drop the quality penalty and note the repair.
        candidate.score = round(min(1.0, candidate.score + 0.12), 2)
        note = "התוכנית תוקנה אוטומטית לפני הצגה (הוסרו כפילויות/תוקנו פרמטרים)"
        if note not in candidate.assumptions:
            candidate.assumptions.append(note)
        healthy.append(candidate)
    return healthy


def _workout_candidate(
    title: str,
    strategy: str,
    frequency: int,
    facts: dict[str, dict[str, Any]],
    *,
    score: float,
    rationale: list[str],
    tradeoffs: list[str],
    resolved_session_minutes: int | None = None,
    resolved_preferred_days: list[int] | None = None,
    resolved_preferred_time: str | None = None,
) -> PlanCandidate:
    # REC-PROGRAM-04-01: Use resolved availability when provided
    minutes = resolved_session_minutes or int(_fact_value(facts, "session_minutes", 50) or 50)
    availability = _parse_availability(_fact_value(facts, "weekly_availability"))
    if resolved_preferred_days and not availability:
        # Synthesize availability slots from resolved preferred days
        availability = [
            with_weekday_schema(
                {
                    "weekday": d,
                    "available": True,
                    "minutes": minutes,
                    "start": resolved_preferred_time,
                }
            )
            for d in resolved_preferred_days
        ]
    split_override = _strategy_split_override(strategy, frequency)
    sessions, assumed = _schedule_sessions(
        frequency,
        availability,
        default_minutes=minutes,
        default_start=resolved_preferred_time,
        split_override=split_override,
    )
    _apply_strategy_volume(sessions, strategy)
    equipment_value = _fact_value(facts, "equipment")
    location = _fact_value(facts, "training_location")
    limitations = _fact_value(facts, "training_limitations")
    experience = str(_fact_value(facts, "strength_experience", "beginner") or "beginner")
    training_profile = training_intelligence.client_training_profile_from_facts(
        facts,
        available_days_per_week=frequency,
        preferred_training_days=resolved_preferred_days,
        time_per_workout_minutes=minutes,
    )
    assumptions: list[str] = []
    if assumed:
        assumptions.append("ימי האימון נבחרו זמנית וטעונים אישור")
    if equipment_value is None:
        assumptions.append("הונח ציוד בסיסי בלבד עד לאישור ציוד")
    adaptation_audit: list[dict[str, Any]] = []
    for session in sessions:
        adapted, changes = training_intelligence.adapt_exercises(
            session["exercises"],
            equipment_value=equipment_value,
            location=location,
            pain_value=limitations,
            medical_avoidance=limitations,
            experience=experience,
        )
        session["exercises"] = adapted
        for exercise in session["exercises"]:
            exercise["warmup_sets"] = training_intelligence.warmup_sets(exercise)
        if changes:
            adaptation_audit.append({"session": session["name"], "changes": changes})
        # Generate a complete quick fallback rather than only exercise IDs.
        session["fast_version"] = training_intelligence.quick_session(
            session,
            min(25, session["minutes"]),
        )
        session["warmup"] = [
            "5 דקות תנועה קלה",
            "בצע את סטי החימום המוצגים לכל תרגיל",
            "עצור אם כאב חד או חריג מחמיר",
        ]
    if adaptation_audit:
        assumptions.append("התוכנית הותאמה לציוד, לניסיון ולמגבלות שדווחו")
    payload = {
        "frequency": frequency,
        "training_profile": training_profile.public_payload(),
        "sessions": sessions,
        "progression": {
            "method": "double_progression_rir",
            "target_rir": 2,
            "deload_trigger": "2-3 אימונים רצופים עם ירידה בביצועים או עייפות גבוהה",
        },
        "days_source": "default" if assumed else "confirmed_availability",
        "adaptation_audit": adaptation_audit,
        "plan_rationale": {
            "split_type": strategy,
            "frequency": frequency,
            "why_this_split": list(rationale),
            "tradeoffs": list(tradeoffs),
            "days_source": "default" if assumed else "confirmed_availability",
            "equipment_considered": list(training_profile.available_equipment),
            "pain_areas_considered": list(training_profile.pain_areas),
            "progression_rule": "double_progression_rir_with_pain_hold",
            "safety_rule": "do_not_increase_load_when_active_pain_matches_joint_load",
        },
    }
    quality_issues = workout_quality_issues(payload)
    quality_penalty = 0.12 if quality_issues else 0.0
    if quality_issues:
        assumptions.append("נמצאו בעיות איכות במועמד האימון ונדרש תיקון לפני הפעלה")
    return PlanCandidate(
        plan_type="workout",
        title=title,
        strategy=strategy,
        score=round(max(0.0, min(1.0, score - (0.08 if assumed else 0.0) - quality_penalty)), 2),
        rationale=rationale,
        tradeoffs=tradeoffs,
        assumptions=assumptions,
        payload=payload,
    )


def _has_limitation(value: Any) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return False
    return text not in {"none", "no", "null", "אין", "ללא", "אין מגבלות"}


def _is_limited_equipment(value: Any, location: Any) -> bool:
    text = f"{value or ''} {location or ''}".lower()
    if not text.strip():
        return True
    limited_tokens = ("home", "bodyweight", "dumbbell", "limited", "בית", "משקולות יד", "ללא ציוד")
    full_tokens = ("full_gym", "gym", "חדר כושר", "מלא")
    return any(token in text for token in limited_tokens) and not any(token in text for token in full_tokens)


def _workout_strategy_score(
    strategy: str,
    facts: dict[str, dict[str, Any]],
    *,
    minutes: int,
    frequency: int,
) -> tuple[float, list[str], list[str], list[str]]:
    """Score a compatible workout structure from confirmed planning facts."""
    goal = str(_fact_value(facts, "primary_goal", "") or "")
    experience = str(_fact_value(facts, "strength_experience", "beginner") or "beginner").lower()
    location = _fact_value(facts, "training_location")
    equipment = _fact_value(facts, "equipment")
    limitations = _fact_value(facts, "training_limitations") or _fact_value(facts, "active_pain")
    limited_equipment = _is_limited_equipment(equipment, location)
    has_limitation = _has_limitation(limitations)

    scores = {
        "consistency": 0.84,
        "balanced": 0.86,
        "performance": 0.80,
    }
    rationale: dict[str, list[str]] = {
        "consistency": [f"שומר על כל {frequency} ימי האימון המאושרים עם עומס פשוט יותר"],
        "balanced": [f"תואם את {frequency} ימי האימון המאושרים ומחלק עומס/התאוששות באופן מאוזן"],
        "performance": [f"תואם את {frequency} ימי האימון המאושרים עם יותר נפח ודגש התקדמות"],
    }
    tradeoffs: dict[str, list[str]] = {
        "consistency": ["פחות התמחות לכל קבוצת שריר בכל אימון"],
        "balanced": ["דורש לעמוד ברוב חלונות האימון כדי לשמור על איזון השבוע"],
        "performance": ["דורש יותר התאוששות ודיוק בביצוע"],
    }
    fit_reasons: list[str] = []

    if goal in {"fat_loss_muscle_retention", "fat_loss", "general_fitness"}:
        scores["consistency"] += 0.08
        scores["balanced"] += 0.03
        rationale["consistency"].append("מתאים לשימור שגרה ושריפת אנרגיה בלי להעמיס מדי")
        fit_reasons.append("goal_prefers_adherence")
    elif goal in {"muscle_gain", "strength"}:
        scores["performance"] += 0.12
        scores["balanced"] += 0.02
        rationale["performance"].append("המטרה דורשת יותר הזדמנויות לנפח/עומס מתקדם")
        fit_reasons.append("goal_prefers_progression")
    elif goal == "general_health":
        scores["balanced"] += 0.07
        fit_reasons.append("goal_prefers_balance")

    if minutes < 45:
        scores["consistency"] += 0.07
        scores["performance"] -= 0.08
        rationale["consistency"].append(f"מתאים לחלונות קצרים של {minutes} דקות")
        tradeoffs["performance"].append(f"פחות מתאים ל-{minutes} דקות כי הנפח צפוף יותר")
        fit_reasons.append("short_sessions")
    elif minutes >= 60:
        scores["performance"] += 0.05
        scores["balanced"] += 0.03
        rationale["performance"].append(f"יש מספיק זמן לאימון של {minutes} דקות")
        fit_reasons.append("long_sessions")

    if experience in {"beginner", "novice", "מתחיל"}:
        scores["consistency"] += 0.06
        scores["performance"] -= 0.08
        rationale["consistency"].append("מתאים יותר לשלב שבו הטכניקה וההתמדה קודמות לנפח")
        fit_reasons.append("beginner")
    elif experience in {"advanced", "expert", "מתקדם"}:
        scores["performance"] += 0.06
        rationale["performance"].append("מתאים למתאמן מתקדם שיכול להתאושש מנפח גבוה יותר")
        fit_reasons.append("advanced")

    if has_limitation:
        scores["performance"] -= 0.08
        scores["consistency"] += 0.04
        tradeoffs["performance"].append("פחות מתאים כשיש כאב/מגבלה פעילה")
        fit_reasons.append("limitations")
    if limited_equipment:
        scores["performance"] -= 0.05
        scores["consistency"] += 0.03
        tradeoffs["performance"].append("דורש ציוד וגיוון גבוהים יותר")
        fit_reasons.append("limited_equipment")

    score = round(max(0.0, min(1.0, scores.get(strategy, 0.75))), 2)
    return (
        score,
        rationale.get(strategy, []),
        tradeoffs.get(strategy, []),
        fit_reasons,
    )


async def build_workout_candidates(db: Any, user_id: int) -> list[PlanCandidate]:
    await _require_readiness(db, user_id, "workout")
    await _require_readiness(db, user_id, "safety")
    facts = await collect_facts(db, user_id)

    # REC-PROGRAM-04-01: Use resolved availability as the authoritative source
    from noam_coach.services.availability import resolve_availability
    avail = await resolve_availability(db, user_id)
    desired = max(MIN_FREQUENCY, min(MAX_FREQUENCY, avail.max_days_per_week))
    # D13: differentiate frequency itself where the user's declared
    # availability allows it. The ceiling for "performance" is the number of
    # confirmed available time slots (avail.preferred_days) when that is
    # genuinely larger than the user's stated commitment level
    # (avail.max_days_per_week) — e.g. someone who said "4 days a week" but
    # confirmed 6 open slots has real, declared headroom to train more, never
    # invented availability.
    confirmed_days_available = len(avail.preferred_days) if avail.preferred_days else avail.max_days_per_week
    performance_ceiling = max(avail.max_days_per_week, confirmed_days_available)
    # TASK-07: explicit training availability must not lose a day.  The old
    # "consistency" candidate used desired-1, which recreated the screenshot bug:
    # a user who supplied four days (Sun/Mon/Wed/Fri) could still see a 3-day
    # plan.  All candidates now respect the active day count; they differ by
    # split/volume, not by silently dropping a training day.
    consistency_freq = desired
    performance_freq = min(MAX_FREQUENCY, performance_ceiling, desired + 1)
    # REC-PROGRAM-04-01: Pass resolved availability to candidates
    _avail_kwargs = {
        "resolved_session_minutes": avail.session_minutes,
        "resolved_preferred_days": avail.preferred_days,
        "resolved_preferred_time": avail.preferred_time,
    }
    strategy_inputs = {
        strategy: _workout_strategy_score(
            strategy,
            facts,
            minutes=avail.session_minutes,
            frequency=frequency,
        )
        for strategy, frequency in {
            "consistency": consistency_freq,
            "balanced": desired,
            "performance": performance_freq,
        }.items()
    }
    return [
        _workout_candidate(
            ("Full Body מותאם" if desired >= 4 else "מקסימום עקביות"),
            "consistency",
            consistency_freq,
            facts,
            score=strategy_inputs["consistency"][0],
            rationale=strategy_inputs["consistency"][1],
            tradeoffs=strategy_inputs["consistency"][2],
            **_avail_kwargs,
        ),
        _workout_candidate(
            ("Upper / Lower מאוזן" if desired >= 4 else "מאוזנת"),
            "balanced",
            desired,
            facts,
            score=strategy_inputs["balanced"][0],
            rationale=strategy_inputs["balanced"][1],
            tradeoffs=strategy_inputs["balanced"][2],
            **_avail_kwargs,
        ),
        _workout_candidate(
            ("ABC + Full Body מותאם" if desired >= 4 else "ביצועים"),
            "performance",
            performance_freq,
            facts,
            score=strategy_inputs["performance"][0],
            rationale=strategy_inputs["performance"][1],
            tradeoffs=strategy_inputs["performance"][2],
            **_avail_kwargs,
        ),
    ]


async def save_candidates(db: Any, user_id: int, candidates: list[PlanCandidate]) -> list[PlanCandidate]:
    if not candidates:
        return []
    plan_type = candidates[0].plan_type
    snapshot = await profile_snapshot(db, user_id)
    async with db.transaction() as conn:
        await conn.execute(
            "UPDATE plan_versions SET status='superseded' "
            "WHERE user_id=? AND plan_type=? AND status='candidate'",
            (user_id, plan_type),
        )
        for candidate in candidates:
            cursor = await conn.execute(
                """
                INSERT INTO plan_versions(
                    user_id, plan_type, title, strategy, fit_score, status,
                    payload, rationale, tradeoffs, assumptions, based_on, created_at
                ) VALUES(?, ?, ?, ?, ?, 'candidate', ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    candidate.plan_type,
                    candidate.title,
                    candidate.strategy,
                    candidate.score,
                    json.dumps(candidate.payload, ensure_ascii=False),
                    json.dumps(candidate.rationale, ensure_ascii=False),
                    json.dumps(candidate.tradeoffs, ensure_ascii=False),
                    json.dumps(candidate.assumptions, ensure_ascii=False),
                    json.dumps(snapshot, ensure_ascii=False),
                    utc_now(),
                ),
            )
            candidate.id = int(cursor.lastrowid or 0)
    return candidates


async def generate_candidates(db: Any, user_id: int, plan_type: PlanType) -> list[PlanCandidate]:
    if plan_type == "nutrition":
        candidates = await build_nutrition_candidates(db, user_id)
    elif plan_type == "workout":
        candidates = await build_workout_candidates(db, user_id)
    else:
        raise ValueError("Unified plan is composed from selected nutrition/workout plans")

    # REC-PLAN-MEAL-03-09: Warn if key plan facts are unconfirmed estimates.
    # Plans are still generated but each candidate's assumptions list is annotated
    # so the user knows which inputs haven't been explicitly confirmed yet.
    _CRITICAL_FACTS_BY_TYPE: dict[str, list[str]] = {
        "workout": [
            "primary_goal",
            "training_days_per_week",
            "weight_kg",
            "training_location",
            "equipment",
            "strength_experience",
            "session_minutes",
        ],
        "nutrition": [
            "primary_goal",
            "weight_kg",
        ],
    }
    critical_keys = _CRITICAL_FACTS_BY_TYPE.get(plan_type, [])
    if critical_keys:
        facts = await collect_facts(db, user_id)
        unconfirmed_labels: list[str] = []
        for key in critical_keys:
            fact = facts.get(key)
            if (
                fact is not None
                and fact.get("value") is not None
                and fact.get("kind") != user_model.KIND_GAP
                and not fact.get("confirmed")
            ):
                spec = user_model.FACT_REGISTRY.get(key)
                label = spec.label if spec else key
                unconfirmed_labels.append(label)
        if unconfirmed_labels:
            note = "מבוסס על הערכה לא מאושרת: " + ", ".join(unconfirmed_labels)
            for candidate in candidates:
                if note not in candidate.assumptions:
                    candidate.assumptions.append(note)

    # L-NEW-2: never display a broken workout candidate. Repair where safe, drop
    # the unrepairable, and block clearly if nothing usable remains.
    if plan_type == "workout":
        candidates = _repair_workout_candidates(candidates)
        if not candidates:
            raise PlanningBlockedError(
                "לא הצלחתי לבנות תוכנית אימון תקינה. בוא נשלים פרטים ונבנה מחדש.",
                missing=["workout_plan_quality"],
            )

    return await save_candidates(db, user_id, candidates)


def _decode_plan(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    for key in ("payload", "rationale", "tradeoffs", "assumptions", "based_on"):
        out[key] = json.loads(row.get(key) or ("{}" if key in {"payload", "based_on"} else "[]"))
    return out


async def list_plan_candidates(db: Any, user_id: int, plan_type: PlanType) -> list[dict[str, Any]]:
    rows = await db.fetch_all(
        """
        SELECT * FROM plan_versions
        WHERE user_id=? AND plan_type=? AND status='candidate'
        ORDER BY fit_score DESC, id
        """,
        (user_id, plan_type),
    )
    return [_decode_plan(row) for row in rows]


async def _validate_plan_for_activation(
    db: Any,
    user_id: int,
    row: dict[str, Any],
) -> None:
    plan_type = row["plan_type"]
    if row.get("status") not in {"candidate", "active"}:
        raise PlanningBlockedError("אפשר להפעיל רק תוכנית מועמדת פעילה")
    if plan_type in {"nutrition", "workout"}:
        await _require_readiness(db, user_id, plan_type)
    if plan_type == "workout":
        await _require_readiness(db, user_id, "safety")
        payload = json.loads(row.get("payload") or "{}")
        sessions = payload.get("sessions") or []
        if not sessions:
            raise PlanningBlockedError("תוכנית האימונים אינה מכילה אימונים")
        incomplete = [
            session for session in sessions
            if session.get("time") in {None, ""} or not session.get("minutes")
        ]
        if incomplete:
            raise PlanningBlockedError(
                "צריך לאשר יום, שעה ומשך לכל אימון לפני הפעלה",
                missing=["weekly_availability"],
            )
        if any(not session.get("exercises") for session in sessions):
            raise PlanningBlockedError("לפחות אימון אחד נשאר ללא תרגילים מתאימים")
        quality_issues = workout_quality_issues(payload)
        if quality_issues:
            raise PlanningBlockedError(
                "תוכנית האימונים צריכה תיקון איכות לפני הפעלה",
                missing=quality_issues[:5],
            )
    elif plan_type == "nutrition":
        if await active_goal(db, user_id) is None:
            raise PlanningBlockedError("אין יעד פעיל לתוכנית התזונה")
        payload = json.loads(row.get("payload") or "{}")
        if not payload.get("days"):
            raise PlanningBlockedError("תוכנית התזונה אינה מכילה ימים")
    elif plan_type == "unified":
        if not await get_active_plan(db, user_id, "nutrition"):
            raise PlanningBlockedError("אין תוכנית תזונה פעילה")
        if not await get_active_plan(db, user_id, "workout"):
            raise PlanningBlockedError("אין תוכנית אימונים פעילה")


async def activate_plan(db: Any, user_id: int, plan_id: int) -> dict[str, Any] | None:
    candidate = await db.fetch_one(
        "SELECT * FROM plan_versions WHERE id=? AND user_id=?",
        (plan_id, user_id),
    )
    if not candidate:
        return None
    await _validate_plan_for_activation(db, user_id, candidate)
    async with db.transaction() as conn:
        cursor = await conn.execute(
            "SELECT * FROM plan_versions WHERE id=? AND user_id=?",
            (plan_id, user_id),
        )
        row_obj = await cursor.fetchone()
        if not row_obj:
            return None
        row = dict(row_obj)
        plan_type = row["plan_type"]
        now = utc_now()
        await conn.execute(
            "UPDATE plan_versions SET status='superseded', superseded_at=? "
            "WHERE user_id=? AND plan_type=? AND status='active' AND id!=?",
            (now, user_id, plan_type, plan_id),
        )
        await conn.execute(
            "UPDATE plan_versions SET status='active', activated_at=? WHERE id=?",
            (now, plan_id),
        )
        await conn.execute(
            """
            INSERT INTO active_plans(user_id, plan_type, plan_id, updated_at)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(user_id, plan_type) DO UPDATE SET
                plan_id=excluded.plan_id, updated_at=excluded.updated_at
            """,
            (user_id, plan_type, plan_id, now),
        )
    result = await get_plan(db, user_id, plan_id)
    if result and result["plan_type"] == "workout":
        await user_model.set_fact(
            db,
            user_id,
            "active_workout_plan",
            {"plan_id": result["id"], **result["payload"]},
            kind=user_model.KIND_FACT,
            source=user_model.SOURCE_USER,
            confirmed=True,
        )
    return result


async def get_plan(db: Any, user_id: int, plan_id: int) -> dict[str, Any] | None:
    row = await db.fetch_one(
        "SELECT * FROM plan_versions WHERE id=? AND user_id=?",
        (plan_id, user_id),
    )
    return _decode_plan(row) if row else None


async def get_active_plan(db: Any, user_id: int, plan_type: PlanType) -> dict[str, Any] | None:
    row = await db.fetch_one(
        """
        SELECT p.* FROM active_plans a
        JOIN plan_versions p ON p.id=a.plan_id
        WHERE a.user_id=? AND a.plan_type=?
        """,
        (user_id, plan_type),
    )
    return _decode_plan(row) if row else None


async def build_unified_week(db: Any, user_id: int) -> PlanCandidate:
    nutrition = await get_active_plan(db, user_id, "nutrition")
    workout = await get_active_plan(db, user_id, "workout")
    if not nutrition or not workout:
        raise ValueError("יש לבחור תוכנית תזונה ותוכנית אימונים לפני יצירת שבוע מאוחד")

    nutrition_days = {int(day["weekday"]): day for day in nutrition["payload"]["days"]}
    workout_days: dict[int, list[dict[str, Any]]] = {}
    for session in workout["payload"]["sessions"]:
        workout_days.setdefault(int(session["weekday"]), []).append(session)

    days: list[dict[str, Any]] = []
    for weekday in sunday_first_order(range(7)):
        meals = copy.deepcopy(nutrition_days.get(weekday, {}).get("meals", []))
        sessions = copy.deepcopy(workout_days.get(weekday, []))
        if sessions and meals:
            session_time = sessions[0].get("time") or "19:00"
            meals.append(
                {
                    "name": "תזמון סביב האימון",
                    "time": session_time,
                    "guidance": "ארוחה קלה 60–120 דקות לפני וחלבון לאחר האימון לפי התוכנית",
                }
            )
        # D12: a single chronologically-sorted view of the day, merging meals
        # and workouts by "HH:MM" (a plain string sort is chronological for
        # this fixed-width format). "meals"/"workouts" stay on the payload
        # unchanged for backward compatibility with existing consumers/tests;
        # "items" is what the renderer should use so a mid-day workout no
        # longer always prints after every meal regardless of its own time.
        items: list[dict[str, Any]] = [
            {**meal, "type": "meal", "time": meal.get("time") or ""} for meal in meals
        ] + [
            {**session, "type": "workout", "time": session.get("time") or ""} for session in sessions
        ]
        items.sort(key=lambda item: (item["time"] == "", item["time"]))
        days.append(
            {
                "weekday": weekday,
                "weekday_name": weekday_he(weekday),
                "meals": meals,
                "workouts": sessions,
                "items": items,
            }
        )
    candidate = PlanCandidate(
        plan_type="unified",
        title="התוכנית השבועית שלי",
        strategy="selected_combination",
        score=round((float(nutrition["fit_score"]) + float(workout["fit_score"])) / 2, 2),
        rationale=["מחברת בין שעות האוכל, העבודה והאימון", "שומרת את התוכניות שבחרת כמקור אמת"],
        tradeoffs=[],
        assumptions=list(dict.fromkeys(nutrition["assumptions"] + workout["assumptions"])),
        payload={
            "nutrition_plan_id": nutrition["id"],
            "workout_plan_id": workout["id"],
            "days": days,
        },
    )
    saved = await save_candidates(db, user_id, [candidate])
    plan_id = saved[0].id
    if plan_id is None:
        raise RuntimeError("Failed to save unified plan candidate")
    active = await activate_plan(db, user_id, int(plan_id))
    if active:
        candidate.id = active["id"]
    return candidate


async def adherence_snapshot(db: Any, user_id: int, start_utc: str, end_utc: str) -> dict[str, Any]:
    goal = await active_goal(db, user_id)
    meals = await db.fetch_all(
        "SELECT calories, protein FROM meals WHERE user_id=? AND eaten_at>=? AND eaten_at<? AND COALESCE(status, 'consumed')='consumed'",
        (user_id, start_utc, end_utc),
    )
    sessions = await db.fetch_all(
        "SELECT status FROM sessions WHERE user_id=? AND started_at>=? AND started_at<?",
        (user_id, start_utc, end_utc),
    )
    calories = sum(float(row["calories"]) for row in meals)
    protein = sum(float(row["protein"]) for row in meals)
    nutrition_adherence = None
    if goal and meals:
        calorie_ratio = calories / max(1, float(goal["calories"]))
        protein_ratio = min(1.2, protein / max(1, float(goal["protein"])))
        nutrition_adherence = round(max(0.0, 1 - abs(1 - calorie_ratio)) * 0.6 + min(1.0, protein_ratio) * 0.4, 2)
    completed = sum(1 for row in sessions if row["status"] == "completed")
    return {
        "nutrition_adherence": nutrition_adherence,
        "reporting_completeness": min(1.0, len(meals) / 3),
        "meals_logged": len(meals),
        "calories": round(calories, 1),
        "protein": round(protein, 1),
        "workouts_completed": completed,
        "workouts_started": len(sessions),
    }
