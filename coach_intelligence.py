"""Auditable coaching intelligence above facts, goals and plans.

This module chooses one useful next action, detects profile contradictions and
keeps stable plans frozen long enough to be evaluated.  It does not diagnose
medical conditions and never silently changes a permanent plan.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import data_quality
import planning
import user_model

ActionKind = Literal[
    "safety_check",
    "complete_profile",
    "approve_goal",
    "choose_nutrition_plan",
    "choose_workout_plan",
    "build_unified_plan",
    "start_workout",
    "log_meal",
    "protein_rescue",
    "review_week",
    "continue_plan",
]


@dataclass(frozen=True)
class NextAction:
    kind: ActionKind
    title: str
    reason: str
    priority: int
    callback: str | None = None
    confidence: float = 1.0
    assumptions: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ProfileConflict:
    key: str
    declared: Any
    observed: Any
    message: str
    severity: str = "info"


@dataclass
class CoachingBrief:
    next_action: NextAction
    readiness: dict[str, Any]
    conflicts: list[ProfileConflict] = field(default_factory=list)
    plan_frozen: bool = False
    plan_frozen_until: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "next_action": self.next_action.to_dict(),
            "readiness": self.readiness,
            "conflicts": [asdict(item) for item in self.conflicts],
            "plan_frozen": self.plan_frozen,
            "plan_frozen_until": self.plan_frozen_until,
        }


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def plan_freeze_status(plan: dict[str, Any] | None, *, days: int = 7) -> tuple[bool, str | None]:
    """Return whether a selected plan should remain stable for evaluation."""
    if not plan:
        return False, None
    activated = _parse_ts(plan.get("activated_at") or plan.get("created_at"))
    if not activated:
        return False, None
    until = activated + timedelta(days=max(1, days))
    now = datetime.now(timezone.utc)
    return now < until, until.isoformat()


async def profile_conflicts(db: Any, user_id: int, *, lookback_days: int = 28) -> list[ProfileConflict]:
    """Compare declared training frequency with actual completed sessions."""
    declared = await user_model.get_value(db, user_id, "training_days_per_week")
    since = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
    row = await db.fetch_one(
        """
        SELECT COUNT(*) AS c FROM sessions
        WHERE user_id=? AND status IN ('completed','partial') AND started_at>=?
        """,
        (user_id, since),
    )
    completed = int((row or {}).get("c") or 0)
    observed = round(completed / max(1, lookback_days / 7), 1)
    conflicts: list[ProfileConflict] = []
    try:
        _declared_float = float(declared) if declared is not None else None
    except (TypeError, ValueError):
        _declared_float = None
    if _declared_float is not None and abs(_declared_float - observed) >= 1.5 and completed >= 2:
        conflicts.append(
            ProfileConflict(
                key="training_days_per_week",
                declared=declared,
                observed=observed,
                message=(
                    f"תוכננו {declared} אימונים בשבוע, אבל בפועל בוצעו "
                    f"כ-{observed} בשבוע. כדאי לבדוק תוכנית מציאותית יותר."
                ),
                severity="warning",
            )
        )
    # REC-PLAN-MEAL-03-06: Also compare plan frequency with declared and observed.
    plan = await planning.get_active_plan(db, user_id, "workout")
    plan_freq = None
    if plan:
        plan_freq = plan.get("payload", {}).get("frequency")
        if plan_freq is not None:
            try:
                plan_freq = float(plan_freq)
            except (TypeError, ValueError):
                plan_freq = None
    if plan_freq is not None and _declared_float is not None:
        if abs(_declared_float - plan_freq) >= 1:
            conflicts.append(
                ProfileConflict(
                    key="training_days_per_week",
                    declared=declared,
                    observed=plan_freq,
                    message=(
                        f"הצהרת על {declared} אימונים בשבוע, "
                        f"אבל התוכנית הפעילה בנויה ל-{int(plan_freq)}. "
                        "כדאי לעדכן את התוכנית או ההצהרה."
                    ),
                    severity="warning",
                )
            )
    if plan_freq is not None and completed >= 2 and abs((plan_freq or 0) - observed) >= 1.5:
        conflicts.append(
            ProfileConflict(
                key="plan_vs_actual_frequency",
                declared=plan_freq,
                observed=observed,
                message=(
                    f"התוכנית בנויה ל-{int(plan_freq)} אימונים, "
                    f"אבל בפועל בוצעו כ-{observed}. "
                    "אולי כדאי להתאים את התוכנית למציאות."
                ),
                severity="info",
            )
        )
    return conflicts


async def next_best_action(
    db: Any,
    user_id: int,
    *,
    consumed_calories: float = 0,
    consumed_protein: float = 0,
    now_local_hour: int | None = None,
) -> NextAction:
    """Choose one action using readiness, active plans and today's progress."""
    readiness = await user_model.compute_all_readiness(db, user_id)
    safety = readiness["safety"]
    if not safety["ready"]:
        return NextAction(
            "safety_check",
            "להשלים שאלת בטיחות",
            "חסר מידע שיכול לשנות אילו תרגילים והמלצות בטוחים עבורך.",
            100,
            "planv2:profile",
            confidence=1.0,
        )

    nutrition_ready = readiness["nutrition"]["ready"]
    workout_ready = readiness["workout"]["ready"]
    if not nutrition_ready or not workout_ready:
        missing = list(dict.fromkeys(
            readiness["nutrition"].get("missing", [])
            + readiness["workout"].get("missing", [])
        ))
        return NextAction(
            "complete_profile",
            "להשלים את הפרופיל",
            "חסרים פרטים שמשפיעים ישירות על התוכנית: " + ", ".join(missing[:4]),
            90,
            "planv2:profile",
            confidence=0.98,
        )

    goal = await planning.active_goal(db, user_id)
    if not goal:
        return NextAction(
            "approve_goal",
            "לאשר יעד אישי",
            "לא ניתן למדוד התקדמות או לבנות תפריט בלי יעד פעיל אחד.",
            85,
            "menu:goals",
        )

    nutrition = await planning.get_active_plan(db, user_id, "nutrition")
    if not nutrition:
        return NextAction(
            "choose_nutrition_plan",
            "לבחור תוכנית תזונה",
            "הפרופיל והיעד מוכנים; השלב הבא הוא בחירה בין שלוש חלופות.",
            80,
            "planv2:generate:nutrition",
        )

    workout = await planning.get_active_plan(db, user_id, "workout")
    if not workout:
        return NextAction(
            "choose_workout_plan",
            "לבחור תוכנית אימונים",
            "הזמינות והמגבלות מוכנות; נשאר לבחור את רמת המחויבות המתאימה.",
            78,
            "planv2:generate:workout",
        )

    unified = await planning.get_active_plan(db, user_id, "unified")
    if not unified:
        return NextAction(
            "build_unified_plan",
            "לחבר את השבוע",
            "שתי התוכניות נבחרו, אבל עדיין לא חוברו ללוח שבועי אחד.",
            72,
            # TASK-16: the canonical single action is planv2:my_week
            # (callback_plans.py still accepts the legacy "planv2:unify" /
            # "planv2:show:unified" strings as aliases for old keyboards, but
            # this home-keyboard suggestion must construct the real one, not
            # perpetuate the retired name).
            "planv2:my_week",
        )

    calories_goal = float(goal.get("calories") or 0)
    protein_goal = float(goal.get("protein") or 0)
    hour = now_local_hour if now_local_hour is not None else 12
    if hour >= 17 and protein_goal > 0 and consumed_protein < protein_goal * 0.65:
        remaining = max(0, round(protein_goal - consumed_protein))
        return NextAction(
            "protein_rescue",
            "להשלים חלבון",
            f"נותרו בערך {remaining} גרם חלבון והיום כבר מתקדם.",
            68,
            "menu:nextmeal",
            confidence=0.9,
        )
    if hour >= 11 and consumed_calories <= 0:
        return NextAction(
            "log_meal",
            "לדווח את הארוחה האחרונה",
            "עדיין אין דיווח אוכל היום, ולכן לא ניתן לתת משוב אמין.",
            60,
            "menu:food",
            confidence=0.85,
        )
    if calories_goal and consumed_calories > calories_goal * 1.15:
        return NextAction(
            "continue_plan",
            "להמשיך בלי פיצוי קיצוני",
            "הצריכה מעל היעד, אך עדיף לחזור למסגרת בארוחה הבאה ולא לדלג בצורה קיצונית.",
            55,
            "menu:status",
            confidence=0.85,
        )
    return NextAction(
        "continue_plan",
        "להמשיך את התוכנית",
        "אין כרגע פער דחוף; העקביות חשובה יותר משינוי נוסף.",
        40,
        "menu:status",
        confidence=0.9,
    )


async def build_coaching_brief(
    db: Any,
    user_id: int,
    *,
    consumed_calories: float = 0,
    consumed_protein: float = 0,
    now_local_hour: int | None = None,
) -> CoachingBrief:
    readiness = await user_model.compute_all_readiness(db, user_id)
    action = await next_best_action(
        db,
        user_id,
        consumed_calories=consumed_calories,
        consumed_protein=consumed_protein,
        now_local_hour=now_local_hour,
    )
    conflicts = await profile_conflicts(db, user_id)
    unified = await planning.get_active_plan(db, user_id, "unified")
    frozen, until = plan_freeze_status(unified)
    return CoachingBrief(
        next_action=action,
        readiness=readiness,
        conflicts=conflicts,
        plan_frozen=frozen,
        plan_frozen_until=until,
    )


async def nutrition_feedback_allowed(
    db: Any,
    user_id: int,
    start_utc: str,
    end_utc: str,
) -> tuple[bool, str]:
    """Shared quality gate for summaries and nutrition-sensitive nudges."""
    day = await data_quality.assess_day(db, user_id, start_utc, end_utc)
    goal = await data_quality.active_goal_quality(db, user_id)
    if not day.usable:
        return False, "הדיווח היומי חלקי או מכיל חשד לכפילויות"
    if not goal.usable:
        return False, "היעד עדיין זמני"
    return True, "ok"
