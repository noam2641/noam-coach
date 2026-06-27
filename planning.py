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
    "equipment", "strength_experience", "active_pain", "medical_avoidance",
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
            "WHERE user_id=? AND status='active' AND id!=?",
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
    for weekday in range(7):
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

    cooking = str(_fact_value(facts, "cooking_capacity", "unknown"))
    has_breaks = _fact_value(facts, "meal_break_info") is not None
    assumptions: list[str] = []
    if not restrictions:
        assumptions.append("לא דווחו מגבלות תזונתיות")
    if cooking == "unknown":
        assumptions.append("יכולת הבישול טרם אושרה")

    common = {
        "calories": int(goal["calories"]),
        "protein": int(goal["protein"]),
        "facts": facts,
        "restrictions": restrictions,
        "canonical_ids": canonical_ids,
        "assumptions": assumptions,
    }
    candidates = [
        _nutrition_candidate(
            title="מסודרת וקבועה",
            strategy="structured",
            score=0.88 if has_breaks else 0.76,
            rationale=["פחות החלטות במהלך היום", "חלוקת חלבון עקבית", "מתאימה להכנה מראש"],
            tradeoffs=["דורשת הכנה מראש", "פחות גמישה לשינויים ספונטניים"],
            **common,
        ),
        _nutrition_candidate(
            title="גמישה ומאוזנת",
            strategy="flexible",
            score=0.9,
            rationale=["שלוש חלופות לכל ארוחה", "מתאימה למסעדות ולשעות משתנות", "שומרת רזרבה יומית"],
            tradeoffs=["דורשת יותר בחירות", "נדרש מעקב אחר הרזרבה"],
            **common,
        ),
        _nutrition_candidate(
            title="מינימום התעסקות",
            strategy="low_effort",
            score=0.92 if cooking in {"none", "basic", "unknown"} else 0.78,
            rationale=["מעט בישול", "פחות ארוחות", "מתאימה לימים עמוסים או לתיאבון נמוך"],
            tradeoffs=["פחות גיוון", "ארוחות גדולות יותר"],
            **common,
        ),
    ]
    return candidates


def _parse_availability(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        if isinstance(value.get("days"), list):
            return [item for item in value["days"] if isinstance(item, dict)]
        result = []
        for key, item in value.items():
            if isinstance(item, dict):
                result.append({"weekday": int(key), **item})
        return result
    return []


def _default_days(frequency: int) -> list[int]:
    spreads = {
        1: [1],
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
) -> tuple[list[dict[str, Any]], bool]:
    usable = [item for item in availability if item.get("available", True)]
    usable.sort(key=lambda item: int(item.get("weekday", 0)))
    assumed = len(usable) < frequency
    if assumed:
        selected = [
            {"weekday": day, "start": None, "minutes": default_minutes}
            for day in _default_days(frequency)
        ]
    else:
        selected = usable[:frequency]
    split = SPLIT_BY_FREQUENCY[frequency]
    sessions = []
    for index, (slot, code) in enumerate(zip(selected, split, strict=True)):
        sessions.append(
            {
                "index": index,
                "weekday": int(slot.get("weekday", 0)),
                "weekday_name": weekday_he(int(slot.get("weekday", 0))),
                "time": slot.get("start") or slot.get("time"),
                "minutes": int(slot.get("minutes") or default_minutes),
                "code": code,
                "name": PLANS[code]["name"],
                "exercises": copy.deepcopy(PLANS[code]["exercises"]),
            }
        )
    return sessions, assumed


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
) -> PlanCandidate:
    # REC-PROGRAM-04-01: Use resolved availability when provided
    minutes = resolved_session_minutes or int(_fact_value(facts, "session_minutes", 50) or 50)
    availability = _parse_availability(_fact_value(facts, "weekly_availability"))
    if resolved_preferred_days and not availability:
        # Synthesize availability slots from resolved preferred days
        availability = [{"weekday": d, "available": True, "minutes": minutes} for d in resolved_preferred_days]
    sessions, assumed = _schedule_sessions(frequency, availability, default_minutes=minutes)
    equipment_value = _fact_value(facts, "equipment")
    location = _fact_value(facts, "training_location")
    pain_value = _fact_value(facts, "active_pain")
    medical_avoidance = _fact_value(facts, "medical_avoidance")
    experience = str(_fact_value(facts, "strength_experience", "beginner") or "beginner")
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
            pain_value=pain_value,
            medical_avoidance=medical_avoidance,
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
    return PlanCandidate(
        plan_type="workout",
        title=title,
        strategy=strategy,
        score=round(max(0.0, min(1.0, score - (0.08 if assumed else 0.0))), 2),
        rationale=rationale,
        tradeoffs=tradeoffs,
        assumptions=assumptions,
        payload={
            "frequency": frequency,
            "sessions": sessions,
            "progression": {
                "method": "double_progression_rir",
                "target_rir": 2,
                "deload_trigger": "2-3 אימונים רצופים עם ירידה בביצועים או עייפות גבוהה",
            },
            "days_source": "default" if assumed else "confirmed_availability",
            "adaptation_audit": adaptation_audit,
        },
    )


async def build_workout_candidates(db: Any, user_id: int) -> list[PlanCandidate]:
    await _require_readiness(db, user_id, "workout")
    await _require_readiness(db, user_id, "safety")
    facts = await collect_facts(db, user_id)

    # REC-PROGRAM-04-01: Use resolved availability as the authoritative source
    from noam_coach.services.availability import resolve_availability
    avail = await resolve_availability(db, user_id)
    desired = max(MIN_FREQUENCY, min(MAX_FREQUENCY, avail.max_days_per_week))
    experience = str(_fact_value(facts, "strength_experience", "beginner"))
    consistency_freq = max(1, min(3, desired - 1 if desired > 2 else desired))
    performance_freq = min(MAX_FREQUENCY, desired + 1)
    if experience in {"beginner", "מתחיל", "none", "unknown"}:
        performance_freq = desired
    # REC-PROGRAM-04-01: Pass resolved availability to candidates
    _avail_kwargs = {
        "resolved_session_minutes": avail.session_minutes,
        "resolved_preferred_days": avail.preferred_days,
    }
    return [
        _workout_candidate(
            "מקסימום עקביות",
            "consistency",
            consistency_freq,
            facts,
            score=0.9,
            rationale=["פחות אימונים", "גרסאות קצרות מובנות", "סיכוי גבוה להתמדה"],
            tradeoffs=["נפח שבועי מתון יותר", "פחות התמחות בכל קבוצת שריר"],
            **_avail_kwargs,
        ),
        _workout_candidate(
            "מאוזנת",
            "balanced",
            desired,
            facts,
            score=0.92,
            rationale=["תואמת את התדירות שביקשת", "איזון בין נפח להתאוששות", "שומרת חלופות למכשיר תפוס"],
            tradeoffs=["דורשת לעמוד ברוב חלונות האימון"],
            **_avail_kwargs,
        ),
        _workout_candidate(
            "ביצועים",
            "performance",
            performance_freq,
            facts,
            score=0.82 if performance_freq > desired else 0.86,
            rationale=["יותר הזדמנויות לתרגול ולהתקדמות", "נפח גבוה יותר למשתמש מתאים"],
            tradeoffs=["דורשת יותר זמן והתאוששות", "פחות מתאימה לשבוע עמוס"],
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
    for weekday in range(7):
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
        days.append(
            {
                "weekday": weekday,
                "weekday_name": weekday_he(weekday),
                "meals": meals,
                "workouts": sessions,
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
        "SELECT calories, protein FROM meals WHERE user_id=? AND eaten_at>=? AND eaten_at<?",
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
