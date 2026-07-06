"""Health data quality policy (RE13).

Turns the raw wear-aware statistics (routine.py) into a product-level
quality report: which policy the numbers are based on, how trustworthy each
metric is, and ready-made Hebrew explanations. The goal is to stop the bot
from building a plan on data that is "technically correct but misleading" —
e.g. a weekly frequency derived from a single fully-worn week, or a sleep
schedule detected from two nights.

The module never mutates state; it only reads the ``health`` table through
``routine`` helpers, so it is safe to call from any wizard step or screen.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from zoneinfo import ZoneInfo

import routine

CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_LOW = "low"

_CONFIDENCE_RANK = {CONFIDENCE_LOW: 1, CONFIDENCE_MEDIUM: 2, CONFIDENCE_HIGH: 3}
_CONFIDENCE_HE = {
    CONFIDENCE_HIGH: "גבוהה",
    CONFIDENCE_MEDIUM: "בינונית",
    CONFIDENCE_LOW: "נמוכה",
}

# No wear evidence at all (import predates watch_wear tracking, or no watch).
POLICY_NO_WEAR = "no_wear_data"

# Sleep: below the minimum the wizard must NOT offer a regular confirmation —
# two nights are an anecdote, not a routine.
SLEEP_MIN_NIGHTS_FOR_CONFIRMATION = 7
SLEEP_NIGHTS_FOR_HIGH_CONFIDENCE = 10

# Steps: how many fully-covered days make the daily average trustworthy.
STEPS_DAYS_FOR_HIGH_CONFIDENCE = 15
STEPS_DAYS_FOR_MEDIUM_CONFIDENCE = 7

# Export freshness (days since the newest sample in the DB).
FRESHNESS_OK_DAYS = 7
FRESHNESS_STALE_DAYS = 14
FRESHNESS_VERY_STALE_DAYS = 30


def confidence_label_he(confidence: str) -> str:
    return _CONFIDENCE_HE.get(confidence, confidence)


def _min_confidence(*levels: str) -> str:
    ranked = [level for level in levels if level in _CONFIDENCE_RANK]
    if not ranked:
        return CONFIDENCE_LOW
    return min(ranked, key=lambda level: _CONFIDENCE_RANK[level])


def _cap_confidence(confidence: str, cap: str) -> str:
    return _min_confidence(confidence, cap)


def _workout_section(analysis: routine.TrainingWeekAnalysis | None) -> dict[str, Any]:
    if analysis is None:
        return {
            "confidence": CONFIDENCE_LOW,
            "policy": POLICY_NO_WEAR,
            "valid_weeks": 0,
            "valid_weeks_strict": 0,
            "valid_weeks_relaxed": 0,
            "burned_weeks": 0,
            "frequency": None,
            "frequency_raw": None,
            "frequency_normalized": None,
            "workouts_on_unworn_days": 0,
            "warning_he": (
                "אין נתוני לבישת שעון בייבוא הזה, ולכן לא ניתן לסנן שבועות "
                "חלקיים — התדירות מבוססת על כל האימונים שנרשמו."
            ),
            "explanation_he": "כדי לקבל חישוב מדויק יותר, ייבא קובץ Apple Health עדכני.",
        }

    if analysis.policy == routine.POLICY_STRICT:
        confidence = CONFIDENCE_HIGH
        frequency = analysis.frequency_raw
        valid_weeks = analysis.valid_weeks_strict
        warning = ""
        explanation = (
            f"החישוב מבוסס על {valid_weeks} שבועות מלאים עם נתוני שעון בכל הימים."
        )
    elif analysis.policy == routine.POLICY_RELAXED:
        confidence = CONFIDENCE_MEDIUM
        frequency = analysis.frequency_normalized
        valid_weeks = analysis.valid_weeks_relaxed
        warning = (
            f"רק {analysis.valid_weeks_strict} שבועות היו מלאים לחלוטין, לכן "
            f"נכללו גם שבועות עם לפחות {routine.RELAXED_MIN_WORN_DAYS} ימי "
            "לבישת שעון, והתדירות חושבה יחסית לימים עם נתונים."
        )
        explanation = (
            f"הערכה מבוססת על {valid_weeks} שבועות עם נתוני שעון חלקיים "
            "לפחות — מספיק להערכה ראשונית, כדאי לאשר ידנית."
        )
    else:  # POLICY_INSUFFICIENT
        confidence = CONFIDENCE_LOW
        frequency = None
        valid_weeks = analysis.valid_weeks_relaxed
        warning = (
            "אין מספיק שבועות עם נתוני שעון כדי לזהות שגרת אימונים אמינה."
        )
        explanation = "עדיף לקבוע את כמות האימונים ידנית עד שיצטברו נתונים."

    section = {
        "confidence": confidence,
        "policy": analysis.policy,
        "valid_weeks": valid_weeks,
        "valid_weeks_strict": analysis.valid_weeks_strict,
        "valid_weeks_relaxed": analysis.valid_weeks_relaxed,
        "burned_weeks": analysis.burned_weeks,
        "frequency": frequency,
        "frequency_raw": analysis.frequency_raw,
        "frequency_normalized": analysis.frequency_normalized,
        "workouts_on_unworn_days": analysis.workouts_on_unworn_days,
        "warning_he": warning,
        "explanation_he": explanation,
    }
    if analysis.workouts_on_unworn_days:
        section["warning_he"] = (
            section["warning_he"]
            + (" " if section["warning_he"] else "")
            + f"{analysis.workouts_on_unworn_days} אימונים נרשמו בימים ללא "
            "נתוני שעון ולא נספרו אוטומטית."
        ).strip()
    return section


def _steps_section(steps: routine.StepsAverage) -> dict[str, Any]:
    if steps.days_sampled >= STEPS_DAYS_FOR_HIGH_CONFIDENCE:
        confidence = CONFIDENCE_HIGH
    elif steps.days_sampled >= STEPS_DAYS_FOR_MEDIUM_CONFIDENCE:
        confidence = CONFIDENCE_MEDIUM
    else:
        confidence = CONFIDENCE_LOW

    if steps.avg is None:
        explanation = "אין נתוני צעדים בחלון הזמן האחרון."
        warning = ""
    elif steps.wear_filtered:
        explanation = (
            f"החישוב מבוסס על {steps.days_sampled} ימים שבהם היו מספיק נתוני שעון."
        )
        warning = (
            f"{steps.days_excluded} ימים לא נספרו כי לא היו בהם מספיק נתוני לבישה."
            if steps.days_excluded
            else ""
        )
    else:
        explanation = f"החישוב מבוסס על {steps.days_sampled} ימים."
        warning = "לא היו נתוני לבישת שעון, ולכן כל הימים נספרו ללא סינון."

    return {
        "confidence": confidence,
        "days_used": steps.days_sampled,
        "days_excluded": steps.days_excluded,
        "average": None if steps.avg is None else round(steps.avg),
        "wear_filtered": steps.wear_filtered,
        "warning_he": warning,
        "explanation_he": explanation,
    }


def _sleep_section(nights: int) -> dict[str, Any]:
    if nights >= SLEEP_NIGHTS_FOR_HIGH_CONFIDENCE:
        confidence = CONFIDENCE_HIGH
        warning = ""
    elif nights >= SLEEP_MIN_NIGHTS_FOR_CONFIRMATION:
        confidence = CONFIDENCE_MEDIUM
        warning = f"שגרת השינה מבוססת על {nights} לילות בלבד — אמינות בינונית."
    else:
        confidence = CONFIDENCE_LOW
        warning = (
            f"זוהתה שינה רק ב-{nights} לילות, ולכן אין מספיק נתונים כדי "
            "לקבוע שגרת שינה."
        )
    return {
        "confidence": confidence,
        "nights_used": nights,
        "minimum_required": SLEEP_MIN_NIGHTS_FOR_CONFIRMATION,
        "should_ask_confirmation": nights >= SLEEP_MIN_NIGHTS_FOR_CONFIRMATION,
        "warning_he": warning,
    }


async def _freshness_section(db: Any, user_id: int, tz: ZoneInfo) -> dict[str, Any]:
    rows = await db.fetch_all(
        "SELECT MAX(start_time) AS newest FROM health WHERE user_id=?",
        (user_id,),
    )
    newest_raw = rows[0].get("newest") if rows else None
    if not newest_raw:
        return {
            "latest_sample_date": None,
            "days_old": None,
            "is_stale": True,
            "warning_he": "אין נתוני Apple Health במאגר.",
        }
    try:
        newest = dt.datetime.fromisoformat(str(newest_raw))
        if newest.tzinfo is None:
            newest = newest.replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return {
            "latest_sample_date": None,
            "days_old": None,
            "is_stale": True,
            "warning_he": "לא ניתן לקבוע את תאריך הנתונים האחרון.",
        }
    newest_date = newest.astimezone(tz).date()
    days_old = max(0, (dt.datetime.now(tz).date() - newest_date).days)
    if days_old > FRESHNESS_VERY_STALE_DAYS:
        warning = (
            f"קובץ הבריאות מסתיים ב-{newest_date.isoformat()} — לפני "
            f"{days_old} ימים. הנתונים ישנים מאוד, וההערכות עלולות לא "
            "לשקף את המצב הנוכחי. מומלץ מאוד לייצא ZIP חדש מהאייפון."
        )
    elif days_old > FRESHNESS_OK_DAYS:
        warning = (
            f"שים לב: קובץ הבריאות האחרון מסתיים ב-{newest_date.isoformat()}, "
            "ולכן השבועות האחרונים לא נכנסו לחישוב. מומלץ לייצא ZIP חדש "
            "מהאייפון."
        )
    else:
        warning = ""
    return {
        "latest_sample_date": newest_date.isoformat(),
        "days_old": days_old,
        "is_stale": days_old > FRESHNESS_OK_DAYS,
        "warning_he": warning,
    }


async def build_health_quality_report(
    db: Any,
    user_id: int,
    tz: ZoneInfo,
    window_days: int = routine.DEFAULT_WINDOW_DAYS,
) -> dict[str, Any]:
    """One quality report covering training weeks, steps, sleep and export
    freshness, with an overall confidence level.

    Overall confidence is the weakest of the workout/steps signals (sleep is
    optional and never drags the whole report down), and a very stale export
    caps it at medium — old data can be internally consistent yet stale.
    """
    analysis = await routine.analyze_training_weeks(db, user_id, tz, window_days)
    steps = await routine.average_daily_steps(db, user_id, tz, window_days)
    sleep_schedule = await routine.learn_sleep_schedule(db, user_id, tz, window_days)

    workout = _workout_section(analysis)
    steps_section = _steps_section(steps)
    sleep = _sleep_section(sleep_schedule.nights_sampled)
    freshness = await _freshness_section(db, user_id, tz)

    signals = [workout["confidence"]]
    if steps_section["days_used"] or steps_section["days_excluded"]:
        signals.append(steps_section["confidence"])
    overall = _min_confidence(*signals)
    days_old = freshness.get("days_old")
    if days_old is not None and days_old > FRESHNESS_VERY_STALE_DAYS:
        overall = _cap_confidence(overall, CONFIDENCE_MEDIUM)

    return {
        "overall_confidence": overall,
        "workout_frequency": workout,
        "steps": steps_section,
        "sleep": sleep,
        "freshness": freshness,
    }


def quality_summary_lines_he(
    report: dict[str, Any], *, include_export_recommendation: bool = True
) -> list[str]:
    """Short user-facing summary shown at the end of the import wizard.

    Plain Hebrew, no JSON, one line per signal + one recommendation.
    ``include_export_recommendation=False`` drops the closing recommendation
    line — used when the surrounding screen already tells the user to export
    a fresh ZIP, so the same advice is not repeated twice in one message.
    """
    workout = report.get("workout_frequency", {})
    steps = report.get("steps", {})
    sleep = report.get("sleep", {})
    freshness = report.get("freshness", {})

    lines = ["<b>סיכום איכות הנתונים</b>"]
    lines.append(
        f"• אימונים: אמינות {confidence_label_he(workout.get('confidence', ''))}"
        + (
            f" ({workout.get('valid_weeks', 0)} שבועות עם נתונים)"
            if workout.get("policy") not in (POLICY_NO_WEAR, None)
            else ""
        )
    )
    if steps.get("days_used"):
        steps_line = f"• צעדים: נספרו {steps['days_used']} ימים"
        if steps.get("days_excluded"):
            steps_line += f", {steps['days_excluded']} לא נספרו בגלל מעט נתוני שעון"
        lines.append(steps_line)
    if sleep.get("should_ask_confirmation"):
        lines.append(f"• שינה: מבוססת על {sleep.get('nights_used', 0)} לילות")
    else:
        lines.append(
            f"• שינה: אין מספיק נתונים ({sleep.get('nights_used', 0)} לילות)"
        )
    days_old = freshness.get("days_old")
    if days_old is None:
        lines.append("• קובץ הבריאות: תאריך לא ידוע")
    elif freshness.get("is_stale"):
        lines.append(
            f"• קובץ הבריאות: לא עדכני — הנתון האחרון מ-{freshness.get('latest_sample_date')}"
        )
    else:
        lines.append("• קובץ הבריאות: עדכני")

    if include_export_recommendation:
        if freshness.get("is_stale"):
            lines.append("• המלצה: כדאי לייצא קובץ Apple Health חדש מהאייפון.")
        else:
            lines.append("• המלצה: אפשר להמשיך לבניית התוכנית.")
    return lines
