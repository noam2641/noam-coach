"""Data-quality gates for meals, days, goals and proactive recommendations."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from models import MealAnalysis


@dataclass(frozen=True)
class QualityIssue:
    code: str
    severity: str
    message: str


@dataclass
class QualityReport:
    score: float
    usable: bool
    issues: list[QualityIssue] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def confidence_label(self) -> str:
        if self.score >= 0.85:
            return "גבוהה"
        if self.score >= 0.6:
            return "בינונית"
        return "נמוכה"


def assess_meal(analysis: MealAnalysis) -> QualityReport:
    """Assess whether an AI meal estimate is safe to use automatically."""
    issues: list[QualityIssue] = []
    score = float(analysis.confidence)
    totals = analysis.totals()

    if not analysis.items:
        issues.append(QualityIssue("no_items", "critical", "לא זוהו פריטי מזון"))
        return QualityReport(0.0, False, issues, totals)

    low_items = [item.name for item in analysis.items if item.confidence < 0.6]
    if low_items:
        score -= min(0.25, len(low_items) * 0.08)
        issues.append(
            QualityIssue(
                "low_item_confidence",
                "warning",
                "רמת ודאות נמוכה עבור: " + ", ".join(low_items[:4]),
            )
        )

    important_low_items = [
        item.name
        for item in analysis.items
        if item.confidence < 0.75 and (item.calories >= 120 or item.fat >= 8)
    ]
    if important_low_items:
        score -= min(0.2, len(important_low_items) * 0.08)
        issues.append(
            QualityIssue(
                "low_confidence_important_item",
                "warning",
                "רכיב משמעותי עם ודאות נמוכה: " + ", ".join(important_low_items[:4]),
            )
        )

    complexity = len(analysis.items)
    if complexity >= 6:
        score -= 0.12
        issues.append(
            QualityIssue(
                "high_complexity_meal",
                "warning",
                "ארוחה מורכבת דורשת בדיקה לפני שמירה אוטומטית",
            )
        )

    if any(
        token in item.name.casefold()
        for item in analysis.items
        for token in ("שמן", "רוטב", "טחינה", "מיונז", "חמאה", "oil", "sauce", "dressing")
    ):
        issues.append(
            QualityIssue(
                "calorie_dense_component_present",
                "info",
                "זוהה רכיב קלורי צפוף; מומלץ לוודא כמות",
            )
        )

    macro_kcal = totals["protein"] * 4 + totals["carbs"] * 4 + totals["fat"] * 9
    kcal = totals["calories"]
    macro_delta_pct = 0.0
    if kcal > 0 and macro_kcal > 0:
        macro_delta_pct = abs(kcal - macro_kcal) / max(kcal, macro_kcal)
        if macro_delta_pct > 0.25:
            score -= 0.25
            severity = "critical" if macro_delta_pct > 0.35 else "warning"
            issues.append(
                QualityIssue(
                    "macro_calorie_mismatch",
                    severity,
                    f"פער של {macro_delta_pct:.0%} בין הקלוריות לסכום המאקרו",
                )
            )

    if analysis.question or analysis.options:
        score -= 0.15
        issues.append(QualityIssue("needs_clarification", "info", "נדרשת הבהרה מהמשתמש"))

    if kcal <= 0 or kcal > 5000:
        score -= 0.4
        issues.append(QualityIssue("implausible_calories", "critical", "סך קלוריות לא סביר"))

    score = max(0.0, min(1.0, round(score, 3)))
    usable = score >= 0.55 and not any(i.severity == "critical" for i in issues)
    return QualityReport(
        score=score,
        usable=usable,
        issues=issues,
        metrics={
            **totals,
            "macro_kcal": round(macro_kcal, 1),
            "macro_delta_pct": macro_delta_pct,
            "meal_complexity": complexity,
            "important_low_confidence_items": important_low_items,
        },
    )


async def assess_day(
    db: Any,
    user_id: int,
    start_utc: str,
    end_utc: str,
    *,
    expected_meals: int | None = None,
) -> QualityReport:
    rows = await db.fetch_all(
        """
        SELECT id, confidence, calories, protein, created_at
        FROM meals
        WHERE user_id=? AND eaten_at>=? AND eaten_at<?
          AND COALESCE(status, 'consumed')='consumed'
        ORDER BY eaten_at
        """,
        (user_id, start_utc, end_utc),
    )
    issues: list[QualityIssue] = []
    meal_count = len(rows)
    avg_confidence = (
        sum(float(row["confidence"]) for row in rows) / meal_count if meal_count else 0.0
    )
    score = avg_confidence

    expected = expected_meals or 3
    completeness = min(1.0, meal_count / max(1, expected))
    score = score * 0.65 + completeness * 0.35
    if meal_count == 0:
        issues.append(QualityIssue("no_meals", "critical", "לא דווחו ארוחות"))
    elif completeness < 0.67:
        issues.append(QualityIssue("partial_day", "warning", "הדיווח היומי חלקי"))

    duplicate_rows = await db.fetch_all(
        """
        SELECT fingerprint, COUNT(*) AS c
        FROM meal_fingerprints
        WHERE user_id=? AND created_at>=? AND created_at<? AND fingerprint!=''
        GROUP BY fingerprint HAVING COUNT(*)>1
        """,
        (user_id, start_utc, end_utc),
    )
    if duplicate_rows:
        score -= 0.25
        issues.append(QualityIssue("duplicate_risk", "warning", "קיים חשד לארוחות כפולות"))

    score = max(0.0, min(1.0, round(score, 3)))
    return QualityReport(
        score=score,
        usable=score >= 0.65 and meal_count > 0 and not duplicate_rows,
        issues=issues,
        metrics={
            "meal_count": meal_count,
            "expected_meals": expected,
            "reporting_completeness": round(completeness, 2),
            "average_confidence": round(avg_confidence, 2),
            "calories": round(sum(float(r["calories"]) for r in rows), 1),
            "protein": round(sum(float(r["protein"]) for r in rows), 1),
        },
    )


async def active_goal_quality(db: Any, user_id: int) -> QualityReport:
    # 'active_provisional' is an ACTIVE goal the user chose to use before final
    # approval -- planning.active_goal and the db.py migrations both treat the
    # two statuses together. This query did not, so a user whose only goal was
    # provisional scored 0.0 ("no_active_goal") instead of 0.55, and
    # can_send_proactive refused every gated message with
    # goal_quality_insufficient. That silently blocked morning_menu, evening,
    # weekly_summary, overpace_alert and intraday_nudge indefinitely; the
    # provisional branch below existed but was unreachable for exactly the
    # users it was written for.
    goal = await db.fetch_one(
        "SELECT * FROM goal_versions WHERE user_id=? "
        "AND status IN ('active', 'active_provisional') ORDER BY id DESC LIMIT 1",
        (user_id,),
    )
    if not goal:
        return QualityReport(
            0.0,
            False,
            [QualityIssue("no_active_goal", "critical", "אין יעד פעיל")],
        )
    source = goal.get("source") or ""
    explanation = goal.get("explanation") or ""
    provisional = source in {"default", "computed_provisional"} or "זמני" in explanation
    score = 0.55 if provisional else 0.9
    issues = (
        [QualityIssue("provisional_goal", "warning", "היעד עדיין זמני")]
        if provisional
        else []
    )
    return QualityReport(score, not provisional, issues, dict(goal))


async def can_send_proactive(
    db: Any,
    user_id: int,
    *,
    require_goal_quality: bool = False,
    require_nutrition_quality: bool = False,
    start_utc: str | None = None,
    end_utc: str | None = None,
) -> tuple[bool, str]:
    flow = await db.fetch_one(
        "SELECT flow, updated_at FROM active_flow WHERE user_id=?",
        (user_id,),
    )
    if flow and flow.get("flow") not in {None, "", "idle"}:
        return False, "active_flow"

    recent = await db.fetch_one(
        """
        SELECT created_at FROM product_events
        WHERE user_id=? AND event IN ('USER_MESSAGE','USER_CALLBACK')
        ORDER BY id DESC LIMIT 1
        """,
        (user_id,),
    )
    if recent:
        try:
            ts = datetime.fromisoformat(recent["created_at"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - ts).total_seconds() < 10 * 60:
                return False, "user_recently_active"
        except (ValueError, TypeError):
            pass

    if require_goal_quality or require_nutrition_quality:
        goal = await active_goal_quality(db, user_id)
        if goal.score < 0.6:
            return False, "goal_quality_insufficient"

    if require_nutrition_quality and start_utc and end_utc:
        day = await assess_day(db, user_id, start_utc, end_utc)
        if not day.usable:
            return False, "nutrition_quality_insufficient"
    return True, "ok"
