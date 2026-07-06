"""Deterministic daily mission and score helpers.

These functions turn the shared DailyContext into a small product layer:
one mission for the day and a clear evening score. They do not write state and
do not invent new facts; they only explain what the existing context implies.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DailyMission:
    code: str
    title: str
    target: float
    current: float
    unit: str
    reason: str

    @property
    def progress_percent(self) -> int:
        if self.target <= 0:
            return 100
        return max(0, min(100, int(round((self.current / self.target) * 100))))

    @property
    def completed(self) -> bool:
        return self.progress_percent >= 100


@dataclass(frozen=True)
class DailyScore:
    nutrition: int
    workout: int
    recovery: int
    overall: int
    recommendation: str


def choose_daily_mission(ctx: Any) -> DailyMission:
    protein_remaining = float(getattr(ctx, "protein_remaining", 0) or 0)
    workout_due = bool(getattr(ctx, "is_usual_workout_day", False)) and not bool(
        getattr(ctx, "workout_completed", False)
    )
    if workout_due:
        return DailyMission(
            code="workout",
            title="לבצע את האימון של היום",
            target=1,
            current=0,
            unit="אימון",
            reason="זה יום אימון לפי השגרה שלך, ועדיין לא תועד אימון.",
        )
    if protein_remaining > 20:
        target = float(getattr(ctx, "protein_target", 0) or 0)
        consumed = float(getattr(ctx, "protein_consumed", 0) or 0)
        return DailyMission(
            code="protein",
            title=f"להגיע ל-{target:.0f} גרם חלבון",
            target=target,
            current=consumed,
            unit="גרם",
            reason="חלבון הוא העוגן הכי חשוב להיום לפי מה שנשאר להשלים.",
        )
    target_calories = float(getattr(ctx, "calorie_target", 0) or 0)
    consumed = float(getattr(ctx, "calories_consumed", 0) or 0)
    return DailyMission(
        code="calorie_budget",
        title="לסיים את היום בתוך התקציב הקלורי",
        target=target_calories,
        current=min(consumed, target_calories),
        unit="קלוריות",
        reason="החלבון כמעט סגור, אז המשימה היא לשמור על איזון עד סוף היום.",
    )


def calculate_daily_score(ctx: Any) -> DailyScore:
    calorie_target = max(1.0, float(getattr(ctx, "calorie_target", 1) or 1))
    protein_target = max(1.0, float(getattr(ctx, "protein_target", 1) or 1))
    calories = float(getattr(ctx, "calories_consumed", 0) or 0)
    protein = float(getattr(ctx, "protein_consumed", 0) or 0)

    calorie_ratio = calories / calorie_target
    calorie_score = max(0, min(100, int(round((1 - abs(1 - calorie_ratio)) * 100))))
    protein_score = max(0, min(100, int(round(min(1, protein / protein_target) * 100))))
    nutrition = int(round(calorie_score * 0.45 + protein_score * 0.55))

    if getattr(ctx, "workout_completed", False):
        workout = 100
    elif getattr(ctx, "is_usual_workout_day", False):
        workout = 40
    else:
        workout = 100

    sleep_quality = getattr(ctx, "sleep_quality", None)
    recovery = {"good": 100, "ok": 78, "bad": 45}.get(str(sleep_quality or "").lower(), 70)
    overall = int(round(nutrition * 0.5 + workout * 0.3 + recovery * 0.2))

    if overall >= 85:
        recommendation = "יום חזק. מחר כדאי לשמר את אותו קצב בלי להעמיס יותר מדי."
    elif protein_score < 85:
        recommendation = "מחר כדאי לפתוח עם חלבון מוקדם כדי לא לרדוף אחרי היעד בערב."
    elif workout < 70:
        recommendation = "מחר כדאי לסגור את האימון מוקדם יותר או לבחור גרסה קצרה."
    else:
        recommendation = "מחר נחזור למסלול עם משימה אחת פשוטה וברורה."
    return DailyScore(
        nutrition=nutrition,
        workout=workout,
        recovery=recovery,
        overall=overall,
        recommendation=recommendation,
    )


def format_daily_mission(mission: DailyMission) -> list[str]:
    done = "✅" if mission.completed else "🎯"
    return [
        f"<b>{done} המשימה של היום</b>",
        f"• {mission.title}",
        f"• התקדמות: {mission.current:.0f}/{mission.target:.0f} {mission.unit} ({mission.progress_percent}%)",
        f"<i>{mission.reason}</i>",
    ]


def format_daily_score(score: DailyScore) -> list[str]:
    return [
        "<b>Daily Score</b>",
        f"Nutrition: {score.nutrition}%",
        f"Workout: {score.workout}%",
        f"Recovery: {score.recovery}%",
        f"Overall: {score.overall}%",
        f"<i>{score.recommendation}</i>",
    ]
