"""Continuous user model — the "living health/nutrition/training file".

Every fact about the user carries full provenance so the coach can tell the
difference between something it *measured*, something it *inferred*, something
the *user said*, and something it simply doesn't know yet. This is the
foundation the rest of the product rests on (questions, safety, plan versions,
explanations).

Each fact stores:
    value, kind (fact/estimate/gap), source, confidence, confirmed, valid,
    affects (which decisions it influences), and timestamps.

Value changes are never destroyed — they are appended to ``user_fact_history``
so we can always explain how a number moved over time.

The module avoids Telegram imports so it can be unit-tested. Async functions
take the bot's ``DB`` object (anything with awaitable ``execute`` /
``fetch_one`` / ``fetch_all``).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Iterator, Protocol

# ---------------------------------------------------------------------------
# Kinds, sources, and the registry that classifies every known fact
# ---------------------------------------------------------------------------

KIND_FACT = "fact"  # measured / hard data
KIND_ESTIMATE = "estimate"  # inferred from data — must be confirmed to harden
KIND_GAP = "gap"  # meaningful missing information

SOURCE_APPLE_HEALTH = "apple_health"
SOURCE_DERIVED = "derived"
SOURCE_USER = "user_report"
SOURCE_SYSTEM = "system"

# Confirmation statuses (REC-ONBOARD-02-04)
CONFIRM_INFERRED = "inferred"
CONFIRM_CONFIRMED = "confirmed"
CONFIRM_CORRECTED = "corrected"
CONFIRM_NOT_APPLICABLE = "not_applicable"
CONFIRM_DEFERRED = "deferred"
CONFIRM_STALE = "stale"
CONFIRM_INVALID = "invalid"

# Default confidence per source (overridable per set_fact call).
SOURCE_CONFIDENCE = {
    SOURCE_APPLE_HEALTH: 0.9,
    SOURCE_DERIVED: 0.55,
    SOURCE_USER: 0.85,
    SOURCE_SYSTEM: 0.7,
}

# ---------------------------------------------------------------------------
# Evidence weighting (W1-5)
# ---------------------------------------------------------------------------
#
# Confidence used to be a pure function of the *source string*: a
# ``workout_pattern`` derived from ONE sampled session and one derived from
# 120 both scored 0.55, and one confirmation tap lifted either to 0.90. The
# stored number described where a fact came from, never how much evidence
# stood behind it.
#
# Derived facts already carry their own sample counts inside the value dict —
# ``sessions_sampled``, ``meals_sampled``, ``nights_sampled``,
# ``valid_weeks_sampled``, ``days_sampled``, ``weekday_hour_samples`` — a
# convention established in routine.py and read all over health_jobs.py. We
# key off that *naming convention* rather than a hardcoded list of keys, so a
# new derived fact that follows the house style is covered automatically and
# the list cannot drift out of sync with its producers.

# A value-dict key counts as a sample tally when its name ends in one of
# these. ``weekday_hour_samples`` style dicts count their summed tallies.
SAMPLE_KEY_SUFFIXES = ("_sampled", "_samples")

# Sample count at (or above) which evidence is considered complete and the
# source confidence applies unattenuated.
EVIDENCE_FULL_N = 10

# The evidence factor floor. n=1 never scores zero — one real observation is
# still information — but it is worth roughly half of a saturated sample.
EVIDENCE_MIN_FACTOR = 0.5


def _sample_tally(candidate: Any) -> int | None:
    """Coerce one sample-metadata value into a count, or None if it isn't one.

    Handles the two shapes actually used: a plain integer tally
    (``sessions_sampled: 4``) and a histogram of tallies
    (``weekday_hour_samples: {"6": 1}`` → 1).
    """
    if isinstance(candidate, bool):
        return None
    if isinstance(candidate, int):
        return max(0, candidate)
    if isinstance(candidate, float):
        # Counts arrive as floats through JSON round-trips.
        return max(0, int(candidate)) if candidate == candidate else None
    if isinstance(candidate, dict):
        total = 0
        for sub in candidate.values():
            tally = _sample_tally(sub)
            if tally is None:
                return None
            total += tally
        return total
    if isinstance(candidate, (list, tuple)):
        return len(candidate)
    return None


def sample_size(value: Any) -> int | None:
    """Return the evidence count behind ``value``, or None when unknown.

    Scans a fact's value dict for keys following the ``*_sampled`` /
    ``*_samples`` house convention and returns the **minimum** positive tally
    found. Minimum, not maximum or mean: a fact is only as well-evidenced as
    its weakest supporting dimension. ``workout_pattern`` claiming four
    training days from ``sessions_sampled=1`` with ``weekday_hour_samples
    {"6": 1}`` is an n=1 fact no matter how many other counters look healthy.

    Returns None — meaning "no sample metadata, don't attenuate" — when the
    value is not a dict or carries no recognised tally. This is what keeps
    every scalar fact (weight_kg, session_minutes, age) on exactly today's
    behaviour.
    """
    if not isinstance(value, dict):
        return None
    tallies: list[int] = []
    for key, candidate in value.items():
        if not isinstance(key, str):
            continue
        if not key.endswith(SAMPLE_KEY_SUFFIXES):
            continue
        tally = _sample_tally(candidate)
        if tally is None:
            continue
        tallies.append(tally)
    if not tallies:
        return None
    return min(tallies)


def evidence_factor(n: int | None) -> float:
    """Map a sample count onto a multiplier in [EVIDENCE_MIN_FACTOR, 1.0].

    ``None`` (no sample metadata) → 1.0, i.e. unchanged from the pre-W1-5
    behaviour. n=0 is *claimed* evidence that does not exist, so it takes the
    floor. The curve rises linearly to 1.0 at EVIDENCE_FULL_N and never
    exceeds it, which guarantees the new confidence is always <= the old one:
    no existing threshold can be crossed upward by this change.
    """
    if n is None:
        return 1.0
    if n >= EVIDENCE_FULL_N:
        return 1.0
    if n <= 1:
        return EVIDENCE_MIN_FACTOR
    span = 1.0 - EVIDENCE_MIN_FACTOR
    return EVIDENCE_MIN_FACTOR + span * ((n - 1) / (EVIDENCE_FULL_N - 1))


def evidence_weighted_confidence(base_confidence: float, value: Any) -> float:
    """Attenuate ``base_confidence`` by the evidence behind ``value``."""
    factor = evidence_factor(sample_size(value))
    if factor >= 1.0:
        return float(base_confidence)
    return round(float(base_confidence) * factor, 4)


@dataclass(frozen=True)
class FactSpec:
    """Static description of a known fact key."""

    key: str
    label: str  # Hebrew label for display
    category: str  # 'measured' | 'inferred' | 'reported'
    affects: tuple[str, ...]  # decisions this fact influences
    visibility: str = "user"  # 'user' | 'internal' | 'admin'
    required_for: tuple[str, ...] = ()  # readiness profiles that require this
    expires_after_days: int | None = None  # None = never expires
    sensitivity: str = "normal"  # 'normal' | 'sensitive' | 'pii'


# Which decisions each fact touches. Drives the "affects / which decision did
# it change" requirement and lets the question engine know what is at stake.
FACT_REGISTRY: dict[str, FactSpec] = {
    # --- measured (from Apple Health) ---
    "weight_kg": FactSpec(
        "weight_kg",
        "משקל נוכחי",
        "measured",
        ("calorie_target", "rate_of_loss", "trend_tracking"),
        required_for=("nutrition",),
        expires_after_days=14,
    ),
    "height_cm": FactSpec(
        "height_cm",
        "גובה",
        "measured",
        ("bmi", "calorie_target"),
    ),
    "body_fat_pct": FactSpec(
        "body_fat_pct",
        "אחוז שומן",
        "measured",
        ("calorie_target", "trend_tracking"),
        expires_after_days=30,
    ),
    "avg_steps": FactSpec(
        "avg_steps",
        "ממוצע צעדים",
        "measured",
        ("activity_target", "calorie_target"),
    ),
    "resting_hr": FactSpec(
        "resting_hr",
        "דופק מנוחה",
        "measured",
        ("recovery_tracking",),
    ),
    # --- reported one-time facts needed for an accurate calorie target ---
    "sex": FactSpec("sex", "מין", "reported", ("calorie_target",)),
    "age": FactSpec("age", "גיל", "reported", ("calorie_target",)),
    "calorie_target": FactSpec(
        "calorie_target",
        "יעד קלוריות",
        "inferred",
        ("menu_planning", "rate_of_loss"),
        visibility="internal",
    ),
    "manual_calorie_override": FactSpec(
        "manual_calorie_override",
        "יעד קלוריות (ידני)",
        "reported",
        ("menu_planning", "rate_of_loss"),
        visibility="internal",
    ),
    "approved_goal": FactSpec(
        "approved_goal",
        "יעד מאושר",
        "reported",
        ("calorie_target", "protein_target", "menu_planning"),
        visibility="internal",
    ),
    # --- inferred (derived/estimates) ---
    "weight_trend": FactSpec(
        "weight_trend",
        "מגמת משקל",
        "inferred",
        ("calorie_target", "rate_of_loss"),
        visibility="internal",
    ),
    "sleep_schedule": FactSpec(
        "sleep_schedule",
        "שגרת שינה",
        "inferred",
        ("meal_timing", "workout_timing", "recovery_tracking"),
        visibility="internal",
    ),
    "workout_pattern": FactSpec(
        "workout_pattern",
        "דפוס אימונים",
        "inferred",
        ("workout_schedule", "workout_timing"),
        visibility="internal",
    ),
    "eating_windows": FactSpec(
        "eating_windows",
        "חלונות אכילה",
        "inferred",
        ("meal_timing", "menu_planning"),
        visibility="internal",
    ),
    # --- reported (only from the user) ---
    "primary_goal": FactSpec(
        "primary_goal",
        "מטרה ראשית",
        "reported",
        ("calorie_target", "workout_schedule", "menu_planning"),
    ),
    "goal_weight_kg": FactSpec(
        "goal_weight_kg",
        "משקל יעד",
        "reported",
        ("calorie_target", "rate_of_loss"),
    ),
    "goal_timeframe_weeks": FactSpec(
        "goal_timeframe_weeks",
        "משך זמן ליעד",
        "reported",
        ("calorie_target", "rate_of_loss"),
    ),
    "training_days_per_week": FactSpec(
        "training_days_per_week",
        "ימי אימון בשבוע",
        "reported",
        ("workout_schedule",),
    ),
    "detected_training_days": FactSpec(
        "detected_training_days",
        "ימי אימון שזוהו מנתוני בריאות",
        "inferred",
        ("workout_schedule", "workout_timing"),
        visibility="internal",
    ),
    "preferred_training_days": FactSpec(
        "preferred_training_days",
        "ימי אימון שהמשתמש בחר",
        "reported",
        ("workout_schedule", "workout_timing"),
    ),
    "active_training_days": FactSpec(
        "active_training_days",
        "ימי אימון פעילים לתוכנית",
        "reported",
        ("workout_schedule", "workout_timing"),
    ),
    "active_workout_plan": FactSpec(
        "active_workout_plan",
        "תוכנית אימונים פעילה",
        "reported",
        ("workout_schedule",),
        visibility="internal",
    ),
    "session_minutes": FactSpec(
        "session_minutes",
        "משך זמן פנוי לכל אימון",
        "reported",
        ("workout_volume",),
    ),
    "training_location": FactSpec(
        "training_location",
        "מקום אימון",
        "reported",
        ("exercise_selection",),
    ),
    "equipment": FactSpec(
        "equipment",
        "ציוד זמין",
        "reported",
        ("exercise_selection",),
    ),
    "strength_experience": FactSpec(
        "strength_experience",
        "ניסיון באימוני כוח",
        "reported",
        ("exercise_selection", "workout_volume"),
    ),
    "diet_restrictions": FactSpec(
        "diet_restrictions",
        # Review 2026-07-18_1 / F-08: this fact holds preferences,
        # intolerances and sensitivities alike — the old hard-prohibition
        # label ("איסורים תזונתיים") misrepresented a user who explicitly
        # chose the softest option ("מעדיף להימנע"). Diagnosed allergies
        # stay a separate fact with their own label.
        "העדפות והגבלות תזונה",
        "reported",
        ("menu_planning",),
    ),
    "allergies": FactSpec(
        "allergies",
        "אלרגיות",
        "reported",
        ("menu_planning", "safety"),
    ),
    # --- lifestyle (extracted from daily routine description) ---
    "work_schedule": FactSpec(
        "work_schedule",
        "שעות עבודה",
        "reported",
        ("meal_timing", "workout_timing"),
    ),
    "commute_minutes": FactSpec(
        "commute_minutes",
        "זמן נסיעה",
        "reported",
        ("workout_timing",),
    ),
    "meal_break_info": FactSpec(
        "meal_break_info",
        "הפסקות אוכל",
        "reported",
        ("meal_timing", "menu_planning"),
    ),
    "workout_window": FactSpec(
        "workout_window",
        "שעת אימון מועדפת",
        "reported",
        ("workout_timing", "workout_schedule"),
    ),
    "cooking_capacity": FactSpec(
        "cooking_capacity",
        "יכולת בישול",
        "reported",
        ("menu_planning",),
    ),
    "food_environment_context": FactSpec(
        "food_environment_context",
        "סביבת אוכל יומיומית",
        "reported",
        ("menu_planning", "meal_timing", "shopping"),
        required_for=("nutrition",),
        expires_after_days=180,
    ),
    "weekly_availability": FactSpec(
        "weekly_availability",
        "זמינות שבועית",
        "reported",
        ("workout_schedule", "meal_timing", "workout_timing"),
        required_for=("workout",),
        expires_after_days=90,
    ),
    "work_days": FactSpec(
        "work_days",
        "ימי עבודה",
        "reported",
        ("meal_timing", "workout_timing"),
        expires_after_days=90,
    ),
    "work_type": FactSpec(
        "work_type",
        "סוג עבודה",
        "reported",
        ("calorie_target", "meal_timing", "recovery_tracking"),
        expires_after_days=180,
    ),
    "food_budget_level": FactSpec(
        "food_budget_level",
        "תקציב מזון",
        "reported",
        ("menu_planning", "shopping"),
        sensitivity="sensitive",
    ),
    "meal_structure_preference": FactSpec(
        "meal_structure_preference",
        "מבנה ארוחות מועדף",
        "reported",
        ("menu_planning", "meal_timing"),
    ),
    "restaurant_frequency": FactSpec(
        "restaurant_frequency",
        "תדירות מסעדות ומשלוחים",
        "reported",
        ("menu_planning",),
    ),
    "family_meals": FactSpec(
        "family_meals",
        "ארוחות משפחתיות קבועות",
        "reported",
        ("menu_planning", "meal_timing"),
        expires_after_days=180,
    ),
    "coaching_style": FactSpec(
        "coaching_style",
        "סגנון אימון מועדף",
        "reported",
        ("notification_style", "motivation"),
    ),
    "notification_preference": FactSpec(
        "notification_preference",
        "העדפות התראות",
        "reported",
        ("notifications",),
    ),
    "training_preferences": FactSpec(
        "training_preferences",
        "העדפות אימון",
        "reported",
        ("exercise_selection", "workout_schedule"),
    ),
    "performance_goal": FactSpec(
        "performance_goal",
        "מטרה ביצועית",
        "reported",
        ("exercise_selection", "progression"),
    ),
    "sleep_quality_reported": FactSpec(
        "sleep_quality_reported",
        "איכות שינה מדווחת",
        "reported",
        ("recovery_tracking", "workout_load"),
        expires_after_days=7,
    ),
    "stress_level": FactSpec(
        "stress_level",
        "רמת לחץ",
        "reported",
        ("recovery_tracking", "notification_style"),
        expires_after_days=7,
    ),
    "main_barrier": FactSpec(
        "main_barrier",
        "המכשול המרכזי",
        "reported",
        ("menu_planning", "workout_schedule", "motivation"),
    ),
    "known_medications": FactSpec(
        "known_medications",
        "תרופות שדווחו",
        "reported",
        ("meal_timing", "hydration", "recovery_tracking"),
        visibility="internal",
        sensitivity="sensitive",
    ),
    "daily_routine_summary": FactSpec(
        "daily_routine_summary",
        "סיכום שגרת יום",
        "reported",
        ("meal_timing", "workout_timing", "menu_planning"),
        visibility="internal",
    ),
    # --- safety (reported) ---
    "active_pain": FactSpec(
        "active_pain",
        "כאב/פציעה פעילה",
        "reported",
        ("exercise_selection", "safety"),
        expires_after_days=7,
    ),
    "medical_avoidance": FactSpec(
        "medical_avoidance",
        "הימנעות לפי רופא",
        "reported",
        ("exercise_selection", "safety"),
    ),
    "training_limitations": FactSpec(
        "training_limitations",
        "כאב, פציעה או מגבלה",
        "reported",
        ("exercise_selection", "safety"),
        required_for=("safety",),
        expires_after_days=30,
    ),
    # --- internal system state (never shown in profile) ---
    "onboarding_stage": FactSpec(
        "onboarding_stage",
        "שלב הצטרפות",
        "reported",
        ("onboarding",),
        visibility="internal",
    ),
}


# ---------------------------------------------------------------------------
# Display labels — friendly Hebrew for internal keys shown to the user
# (REC-ONBOARD-02-03)
# ---------------------------------------------------------------------------

FACT_DISPLAY_LABELS: dict[str, str] = {
    # Not a user_facts row — a planning prerequisite (an approved goal_versions
    # record). Included here so any missing-prerequisite screen (RE10-8) can
    # translate it like any other gap instead of leaking the raw key.
    "active_goal": "יעד יומי מאושר",
    # Pattern / inferred fact IDs used in confirmation buttons
    "workout_pattern": "דפוס האימונים שלך",
    "sleep_schedule": "שעות השינה שלך",
    "eating_windows": "שעות האכילה שלך",
    "work_schedule": "שעות העבודה שלך",
    "workout_window": "שעת האימון המועדפת",
    "training_days_per_week": "מספר ימי אימון בשבוע",
    "session_minutes": "משך זמן פנוי לכל אימון",
    "weekly_availability": "ימים זמינים לאימון",
    "preferred_workout_time": "שעת אימון מועדפת",
    # Goal phases
    "fat_loss_muscle_retention": "ירידה בשומן תוך שמירה על מסת שריר",
    "fat_loss": "ירידה בשומן",
    "muscle_gain": "בניית שריר",
    "maintenance": "שמירה על משקל",
    "lean_bulk": "עלייה נקייה במסה",
    "recomp": "שיפור הרכב גוף",
    # Experience levels
    "beginner": "מתחיל",
    "intermediate": "בינוני",
    "advanced": "מתקדם",
    # Training locations
    "gym": "חדר כושר",
    "home": "בבית",
    "outdoor": "בחוץ",
    "mixed": "משולב",
    # Safety completeness
    "active_pain": "כאב, פציעה או מגבלה",
    "medical_avoidance": "כאב, פציעה או מגבלה",
    "training_limitations": "כאב, פציעה או מגבלה",
    # Profile fields
    "primary_goal": "מטרה ראשית",
    "strength_experience": "ניסיון באימוני כוח",
    "training_location": "מקום אימון",
    "equipment": "ציוד זמין",
    "diet_restrictions": "העדפות והגבלות תזונה",
    "allergies": "אלרגיות",
    "weight_kg": "משקל",
    "height_cm": "גובה",
    "body_fat_pct": "אחוז שומן",
    "age": "גיל",
    "sex": "מין",
}


def display_label(key: str) -> str:
    """Return the friendly Hebrew display label for an internal key.

    Falls back to the FACT_REGISTRY label, then to the key itself (should
    never happen if coverage is complete).
    """
    if key in FACT_DISPLAY_LABELS:
        return FACT_DISPLAY_LABELS[key]
    spec = FACT_REGISTRY.get(key)
    if spec:
        return spec.label
    return key


def display_value(key: str, value: Any) -> str:
    """Return a user-friendly Hebrew string for a fact value.

    REC-PROGRAM-04-08: Never display raw dicts, JSON, snake_case keys,
    or internal metadata.  Body-fat values are normalized by source unit
    (fraction vs percentage).
    """
    if value is None:
        return "לא צוין"
    # REC-PROGRAM-04-08: Intercept raw gap/internal dicts before they leak.
    if isinstance(value, dict):
        if value.get("missing"):
            return "לא צוין"
        # Internal dict with structured data — show only the meaningful part
        if "value" in value:
            return display_value(key, value["value"])
        return "לא צוין"
    # Check display labels for enum-like values
    str_val = str(value)
    if str_val in FACT_DISPLAY_LABELS:
        return FACT_DISPLAY_LABELS[str_val]
    # Numeric formatting
    try:
        num = float(str_val)
        if key == "weight_kg":
            return f'{num:.1f} ק"ג'
        if key == "height_cm":
            return f'{num:.0f} ס"מ'
        if key == "body_fat_pct":
            # REC-PROGRAM-04-08: Delegate to centralized normalizer.
            from noam_coach.services.body_fat import display_body_fat, normalize_body_fat
            result = normalize_body_fat(num)
            return display_body_fat(result)
        if key == "avg_steps":
            return f"{round(num):,} צעדים"
        if key == "resting_hr":
            return f"{round(num)} פעימות/דקה"
        if key == "training_days_per_week":
            return f"{round(num)} בשבוע"
        if key == "session_minutes":
            return f"{round(num)} דקות"
        if key == "age":
            return str(round(num))
    except (TypeError, ValueError):
        pass
    return str_val


# ---------------------------------------------------------------------------
# Readiness profiles — structured view of which decisions the system can make
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ReadinessProfile:
    """A readiness category with required/optional fact keys and their weights."""
    name: str
    label: str
    required: tuple[str, ...]
    optional: tuple[str, ...]


READINESS_PROFILES = {
    "nutrition": ReadinessProfile(
        name="nutrition",
        label="תזונה",
        required=(
            "primary_goal", "weight_kg", "sex", "age",
            "diet_restrictions", "allergies", "food_environment_context",
        ),
        optional=(
            "height_cm", "body_fat_pct", "cooking_capacity", "meal_break_info",
            "meal_structure_preference", "restaurant_frequency", "family_meals",
            "food_budget_level", "work_schedule",
        ),
    ),
    "workout": ReadinessProfile(
        name="workout",
        label="אימון",
        required=(
            "primary_goal", "training_days_per_week", "training_limitations",
            "session_minutes", "training_location",
            "equipment", "strength_experience", "weekly_availability",
        ),
        optional=("workout_window", "training_preferences", "performance_goal"),
    ),
    "safety": ReadinessProfile(
        name="safety",
        label="שאלון בטיחות",
        required=("training_limitations",),
        optional=(),
    ),
    "tracking": ReadinessProfile(
        name="tracking",
        label="נתוני מעקב",
        required=(),
        optional=("weight_kg", "avg_steps", "resting_hr", "sleep_schedule",
                  "eating_windows", "workout_pattern"),
    ),
}


def fact_is_fresh(key: str, fact: dict[str, Any] | None, *, now: dt.datetime | None = None) -> bool:
    """Return whether a fact exists, is not a gap, and has not expired.

    Freshness is part of readiness: a three-month-old work schedule or a
    week-old pain report must not silently drive a new plan.
    """
    if fact is None or fact.get("kind") == KIND_GAP or not fact.get("valid", True):
        return False
    spec = FACT_REGISTRY.get(key)
    if not spec or spec.expires_after_days is None:
        return True
    raw = fact.get("updated_at") or fact.get("created_at")
    if not raw:
        return False
    try:
        updated = dt.datetime.fromisoformat(str(raw))
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=dt.timezone.utc)
    except (TypeError, ValueError):
        return False
    current = now or dt.datetime.now(dt.timezone.utc)
    return current - updated <= dt.timedelta(days=spec.expires_after_days)


def fact_is_usable_for_decision(
    key: str,
    fact: dict[str, Any] | None,
    *,
    now: dt.datetime | None = None,
) -> bool:
    """Return whether a fact may drive a plan or safety decision.

    Unconfirmed estimates are useful for drafts and confirmation screens, but
    they must not satisfy a readiness gate or activate a permanent plan.
    """
    if not fact_is_fresh(key, fact, now=now):
        return False
    assert fact is not None
    if fact.get("kind") == KIND_ESTIMATE and not fact.get("confirmed"):
        return False
    if fact.get("source") == SOURCE_DERIVED and not fact.get("confirmed"):
        return False
    return True


async def compute_readiness(
    db: "SupportsDB", user_id: int, profile_name: str,
) -> dict[str, Any]:
    """Compute readiness score for one profile.

    Returns a rich dict with score, missing, present, deferred, stale,
    not_applicable, and friendly labels (REC-ONBOARD-02-09).
    """
    profile = READINESS_PROFILES.get(profile_name)
    if not profile:
        return {"score": 0.0, "ready": False, "missing": [], "present": [],
                "deferred": [], "stale": [], "not_applicable": [],
                "missing_labels": [], "label": ""}

    missing: list[str] = []
    present: list[str] = []
    deferred: list[str] = []
    stale: list[str] = []
    not_applicable: list[str] = []

    for key in profile.required:
        fact = (
            await get_training_limitations_fact(db, user_id)
            if key == "training_limitations"
            else await get_fact(db, user_id, key)
        )
        status = fact_confirmation_status(fact, key)
        if status == CONFIRM_DEFERRED:
            deferred.append(key)
            missing.append(key)
        elif status == CONFIRM_NOT_APPLICABLE:
            not_applicable.append(key)
            present.append(key)  # satisfied — intentionally skipped
        elif status == CONFIRM_STALE:
            stale.append(key)
            missing.append(key)
        elif not fact_is_usable_for_decision(key, fact):
            missing.append(key)
        else:
            present.append(key)

    optional_present = 0
    for key in profile.optional:
        fact = (
            await get_training_limitations_fact(db, user_id)
            if key == "training_limitations"
            else await get_fact(db, user_id, key)
        )
        if fact_is_usable_for_decision(key, fact):
            optional_present += 1
            present.append(key)

    required_count = len(profile.required)
    required_present = required_count - len(missing)
    required_score = required_present / required_count if required_count else 1.0
    optional_score = (
        optional_present / len(profile.optional) if profile.optional else 1.0
    )
    # Required facts are 70% of the score, optional are 30%.
    score = round(required_score * 0.7 + optional_score * 0.3, 2)
    return {
        "score": score,
        "ready": len(missing) == 0,
        "missing": missing,
        "present": present,
        "deferred": deferred,
        "stale": stale,
        "not_applicable": not_applicable,
        "missing_labels": [display_label(k) for k in missing],
        "label": profile.label,
    }


async def compute_all_readiness(
    db: "SupportsDB", user_id: int,
) -> dict[str, dict[str, Any]]:
    """Compute readiness for all four profiles."""
    return {
        name: await compute_readiness(db, user_id, name)
        for name in READINESS_PROFILES
    }


class SupportsDB(Protocol):
    async def execute(self, sql: str, parameters: tuple[Any, ...] = ()) -> int: ...
    async def fetch_one(
        self, sql: str, parameters: tuple[Any, ...] = ()
    ) -> dict[str, Any] | None: ...
    async def fetch_all(
        self, sql: str, parameters: tuple[Any, ...] = ()
    ) -> list[dict[str, Any]]: ...


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Core API
# ---------------------------------------------------------------------------


#: Facts whose write surface is governed. A fact lands here when it is a
#: DERIVED MIRROR of an authoritative store: writing it directly is an
#: authority claim, and two independently-writable copies of the same truth is
#: how stores drift apart.
#:
#: `active_workout_plan` mirrors `plan_versions`, which is copy-on-write and
#: test-enforced. The mirror had no protection at all, so the weaker store had
#: the weaker guarantee -- backwards, and the reason this exists.
GOVERNED_FACT_KEYS: frozenset[str] = frozenset({"active_workout_plan"})

#: Set while an authorized owner is performing a governed write.
#:
#: A ContextVar rather than frame inspection: `runtime_bound` re-syncs module
#: globals from the coach_bot facade on every call, so the immediate caller's
#: module is not a reliable signal, and async wrappers add further indirection.
#: A ContextVar follows the logical flow instead of the stack, including across
#: `await`.
#:
#: The value carries the owning task's id, not just a reason. `contextvars` are
#: COPIED into a task at `asyncio.create_task`, so a bare reason would let an
#: owner spawn a background task that inherits the authorization and writes
#: after the `with` block has already exited -- authorization outliving the
#: operation that granted it. Recording the task makes that inheritance
#: detectable, and it is refused.
#: (reason, owning task object | _ANY_TASK | None).
#:
#: The task OBJECT, never `id(task)`: CPython reuses ids after collection, so a
#: finished task's id could later belong to an unrelated object and be mistaken
#: for the authorizing task. The ContextVar holds the reference only for the
#: lifetime of the `with` block, which is strictly shorter than the task's own.
_governed_fact_write: ContextVar[tuple[str, object] | None] = ContextVar(
    "_governed_fact_write", default=None
)

#: Sentinel for an authorization that is NOT bound to the task that opened it.
#: Distinct from `None`, which is a real value meaning "granted outside any
#: task" -- conflating the two would silently unbind every synchronous
#: authorization.
_ANY_TASK = object()


def _current_task() -> "asyncio.Task[Any] | None":
    """The running asyncio task, or None outside a loop.

    Returns the OBJECT, never `id(task)`. CPython reuses `id()` after an object
    is collected, so a finished task's id can be handed to an unrelated object
    later -- an authorization could then be honoured in a task that merely
    inherited a recycled number. Comparing objects makes that impossible.

    None is a legitimate value: synchronous callers and `asyncio.run` entry
    points have no task, and an authorization granted there is honoured within
    that same synchronous flow.
    """
    try:
        task = asyncio.current_task()
    except RuntimeError:  # no running loop
        return None
    return task


@contextmanager
def authorize_governed_fact_write(
    reason: str, *, bind_to_task: bool = True
) -> Iterator[None]:
    """Authorize a governed fact write for the duration of this block.

    The static guard in tests narrows *where* a governed write may appear; it
    cannot prove what a dynamic key writes. This is the half that actually
    holds at runtime, and it fails loudly rather than silently permitting.

    `reason` is required and surfaced in errors -- an authorization with no
    stated purpose is not one, and the reason is what makes a temporary
    authorization removable later rather than permanent by inertia.

    Scope is the block AND, by default, the task that opened it. Nesting
    restores the outer authorization on exit; an exception inside still
    releases it.

    `bind_to_task=False` drops the task check, so an authorization opened in
    one task is honoured in a child task. It exists for **test setup**, where a
    synchronous fixture and an async test body legitimately run in different
    tasks (pytest-asyncio creates one per test) and the inheritance is not the
    background-write hazard the binding guards against. Production code must
    not use it: perform the write in the authorizing flow, or open a fresh
    authorization inside the task that genuinely owns it.
    """
    owning_task = _current_task() if bind_to_task else _ANY_TASK
    token = _governed_fact_write.set((reason, owning_task))
    try:
        yield
    finally:
        # `reset` restores the PREVIOUS value, so a nested authorization
        # returns to the outer one rather than clearing it outright.
        _governed_fact_write.reset(token)


def _assert_governed_fact_write_is_authorized(key: str) -> None:
    """Refuse a governed write that no owner claimed responsibility for.

    Raising rather than logging is deliberate. A silently-dropped write leaves
    the mirror stale while the caller believes it succeeded, which is the exact
    divergence this guards against -- worse than an error, because nothing
    surfaces until a user sees the wrong plan.
    """
    if key not in GOVERNED_FACT_KEYS:
        return
    authorization = _governed_fact_write.get()
    if authorization is not None:
        reason, owning_task = authorization
        # Identity comparison, not equality: two distinct Task objects are
        # never the same authorization even if they compare equal somehow.
        if owning_task is _ANY_TASK or owning_task is _current_task():
            return
        # Inherited by a child task at create_task. The authorizing block may
        # already have exited, so honouring it here would let a background
        # write happen under an authority that no longer exists. A governed
        # write must be made by the flow that claimed responsibility for it.
        raise PermissionError(
            f"Governed write to {key!r} attempted from a task that inherited "
            f"authorization granted elsewhere ({reason!r}). contextvars are "
            "copied into tasks at create_task, so the authorizing block may "
            "already have exited. Perform the write in the authorizing flow, "
            "or open a new authorization inside the task if it is genuinely "
            "the owner."
        )
    raise PermissionError(
        f"Unauthorized write to the governed fact {key!r}. This fact is a "
        "derived mirror -- it must be written by its owner, inside "
        "user_model.authorize_governed_fact_write(reason). Writing the mirror "
        "directly lets it diverge from the authoritative store. If a new route "
        "needs this, route it through an existing owner rather than wrapping "
        "the authorization around the new call site."
    )


async def set_fact(
    db: SupportsDB,
    user_id: int,
    key: str,
    value: Any,
    *,
    kind: str = KIND_FACT,
    source: str = SOURCE_SYSTEM,
    confidence: float | None = None,
    confirmed: bool | None = None,
    affects: tuple[str, ...] | None = None,
) -> bool:
    """Upsert a fact. Returns True if the stored value actually changed.

    On a value, source, or confidence change the previous row is appended to
    ``user_fact_history`` so the evolution is never lost.

    ``confirmed=None`` (default) preserves the existing confirmation state.
    ``confirmed=True/False`` explicitly sets or clears it.

    The read, history-write, and upsert are wrapped in a single transaction
    so concurrent calls cannot interleave and corrupt history.
    """
    _assert_governed_fact_write_is_authorized(key)
    if confidence is None:
        confidence = SOURCE_CONFIDENCE.get(source, 0.7)
    # W1-5: the source only sets a *ceiling*. What the fact is actually worth
    # depends on how much evidence stands behind it. Applied to explicitly
    # passed confidences too — otherwise a caller rewriting a one-sample
    # derived value as ``confidence=0.85`` would launder it right back.
    confidence = evidence_weighted_confidence(confidence, value)
    spec = FACT_REGISTRY.get(key)
    if affects is None:
        affects = spec.affects if spec else ()

    new_value_json = _dumps(value)
    now = _now()

    # --- atomic transaction (if db supports it, otherwise falls back) ---
    if hasattr(db, "transaction"):
        return await _set_fact_txn(
            db, user_id, key, new_value_json, kind, source,
            float(confidence), confirmed, affects, now,
        )

    # Fallback for plain SupportsDB without transaction support (tests).
    return await _set_fact_plain(
        db, user_id, key, new_value_json, kind, source,
        float(confidence), confirmed, affects, now,
    )


async def _set_fact_txn(
    db: Any,
    user_id: int,
    key: str,
    new_value_json: str,
    kind: str,
    source: str,
    confidence: float,
    confirmed: bool | None,
    affects: tuple[str, ...],
    now: str,
) -> bool:
    """Atomic set_fact using db.transaction()."""
    async with db.transaction() as conn:
        cursor = await conn.execute(
            "SELECT value, source, confidence, confirmed "
            "FROM user_facts WHERE user_id=? AND key=?",
            (user_id, key),
        )
        existing = await cursor.fetchone()
        if existing is not None:
            existing = dict(existing)

        value_changed = existing is None or existing["value"] != new_value_json
        source_changed = existing is not None and existing["source"] != source
        conf_changed = existing is not None and existing["confidence"] != confidence
        changed = value_changed or source_changed or conf_changed

        if existing is not None and changed:
            await conn.execute(
                """
                INSERT INTO user_fact_history(
                    user_id, key, value, source, confidence, recorded_at
                ) VALUES(?, ?, ?, ?, ?, ?)
                """,
                (user_id, key, existing["value"], existing["source"],
                 existing["confidence"], now),
            )

        # confirmed=None → preserve existing; True/False → explicit set/clear
        keep_confirmed = (
            (existing["confirmed"] if existing else 0)
            if confirmed is None
            else int(confirmed)
        )

        await conn.execute(
            """
            INSERT INTO user_facts(
                user_id, key, value, kind, source, confidence, confirmed,
                valid, affects, updated_at, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
            ON CONFLICT(user_id, key) DO UPDATE SET
                value=excluded.value,
                kind=excluded.kind,
                source=excluded.source,
                confidence=excluded.confidence,
                confirmed=excluded.confirmed,
                valid=1,
                affects=excluded.affects,
                updated_at=excluded.updated_at
            """,
            (user_id, key, new_value_json, kind, source, confidence,
             keep_confirmed, _dumps(list(affects)), now, now),
        )
        return value_changed


async def _set_fact_plain(
    db: SupportsDB,
    user_id: int,
    key: str,
    new_value_json: str,
    kind: str,
    source: str,
    confidence: float,
    confirmed: bool | None,
    affects: tuple[str, ...],
    now: str,
) -> bool:
    """Non-transactional fallback (plain SupportsDB without .transaction())."""
    existing = await db.fetch_one(
        "SELECT value, source, confidence, confirmed "
        "FROM user_facts WHERE user_id=? AND key=?",
        (user_id, key),
    )

    value_changed = existing is None or existing["value"] != new_value_json
    source_changed = existing is not None and existing["source"] != source
    conf_changed = existing is not None and existing["confidence"] != confidence
    changed = value_changed or source_changed or conf_changed

    if existing is not None and changed:
        await db.execute(
            """
            INSERT INTO user_fact_history(
                user_id, key, value, source, confidence, recorded_at
            ) VALUES(?, ?, ?, ?, ?, ?)
            """,
            (user_id, key, existing["value"], existing["source"],
             existing["confidence"], now),
        )

    keep_confirmed = (
        (existing["confirmed"] if existing else 0)
        if confirmed is None
        else int(confirmed)
    )

    await db.execute(
        """
        INSERT INTO user_facts(
            user_id, key, value, kind, source, confidence, confirmed,
            valid, affects, updated_at, created_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
        ON CONFLICT(user_id, key) DO UPDATE SET
            value=excluded.value,
            kind=excluded.kind,
            source=excluded.source,
            confidence=excluded.confidence,
            confirmed=excluded.confirmed,
            valid=1,
            affects=excluded.affects,
            updated_at=excluded.updated_at
        """,
        (user_id, key, new_value_json, kind, source, confidence,
         keep_confirmed, _dumps(list(affects)), now, now),
    )
    return value_changed


# The confidence a confirmation tap can reach on its own.
CONFIRMED_CONFIDENCE = 0.9


async def confirm_fact(db: SupportsDB, user_id: int, key: str) -> None:
    """User confirmed an estimate — harden it (confidence floor + confirmed).

    W1-5: a confirmation IS real evidence — the user looked at the value and
    said yes — so it still raises confidence, and for a fact with no sample
    metadata it reaches the historical 0.9 floor unchanged.

    But a tap does not retroactively create observations. When the fact
    declares its own sample count, the confirmed floor is capped by that
    evidence, so an ``eating_windows`` built from ``meals_sampled=1`` cannot
    reach 0.9 by being approved once. The user is confirming that the *value
    looks right*, not attesting that it rests on 100 meals. Confirmation is
    still worth strictly more than not confirming: the floor is the larger of
    the evidence-capped ceiling and whatever the fact already held, so
    ``confirmed`` never lowers a confidence.
    """
    fact = await get_fact(db, user_id, key)
    ceiling = CONFIRMED_CONFIDENCE
    if fact is not None:
        ceiling = evidence_weighted_confidence(CONFIRMED_CONFIDENCE, fact.get("value"))
    await db.execute(
        """
        UPDATE user_facts
        SET confirmed=1,
            confidence=MAX(confidence, ?),
            updated_at=?
        WHERE user_id=? AND key=?
        """,
        (float(ceiling), _now(), user_id, key),
    )


async def invalidate_fact(db: SupportsDB, user_id: int, key: str) -> None:
    await db.execute(
        "UPDATE user_facts SET valid=0, updated_at=? WHERE user_id=? AND key=?",
        (_now(), user_id, key),
    )


async def defer_fact(db: SupportsDB, user_id: int, key: str) -> None:
    """Mark a fact as deferred — keep it missing but remember the user chose
    to complete it later (REC-ONBOARD-02-04)."""
    await set_fact(
        db, user_id, key, None,
        kind=KIND_GAP,
        source=SOURCE_USER,
        confidence=0.0,
        confirmed=False,
        affects=FACT_REGISTRY[key].affects if key in FACT_REGISTRY else (),
    )
    # Store deferred status in a separate metadata field
    await db.execute(
        """
        UPDATE user_facts SET
            kind=?,
            updated_at=?
        WHERE user_id=? AND key=?
        """,
        (KIND_GAP, _now(), user_id, key),
    )


async def mark_not_applicable(db: SupportsDB, user_id: int, key: str) -> None:
    """Mark a fact as intentionally not applicable — stops it from being
    shown repeatedly as missing (REC-ONBOARD-02-04)."""
    await set_fact(
        db, user_id, key, "__not_applicable__",
        kind=KIND_FACT,
        source=SOURCE_USER,
        confidence=1.0,
        confirmed=True,
        affects=FACT_REGISTRY[key].affects if key in FACT_REGISTRY else (),
    )


def fact_confirmation_status(fact: dict[str, Any] | None, key: str) -> str:
    """Return the confirmation status of a fact (REC-ONBOARD-02-04)."""
    if fact is None or fact.get("kind") == KIND_GAP:
        return CONFIRM_DEFERRED if fact and fact.get("source") == SOURCE_USER else "missing"
    if not fact.get("valid", True):
        return CONFIRM_INVALID
    if fact.get("value") == "__not_applicable__":
        return CONFIRM_NOT_APPLICABLE
    if not fact_is_fresh(key, fact):
        return CONFIRM_STALE
    if fact.get("confirmed"):
        if fact.get("source") == SOURCE_USER and fact.get("kind") == KIND_FACT:
            return CONFIRM_CORRECTED
        return CONFIRM_CONFIRMED
    if fact.get("kind") == KIND_ESTIMATE:
        return CONFIRM_INFERRED
    return CONFIRM_INFERRED


async def get_fact(db: SupportsDB, user_id: int, key: str) -> dict[str, Any] | None:
    row = await db.fetch_one(
        "SELECT * FROM user_facts WHERE user_id=? AND key=? AND valid=1",
        (user_id, key),
    )
    if not row:
        return None
    return _hydrate(row)


def _limitation_text(value: Any) -> str:
    if value in (None, "", "none", "None", "__not_applicable__"):
        return ""
    if isinstance(value, dict):
        if value.get("missing") or value.get("skipped"):
            return ""
        parts = [
            str(value.get(name)).strip()
            for name in ("location", "note", "details", "avoid")
            if value.get(name)
        ]
        return "; ".join(dict.fromkeys(parts))
    if isinstance(value, (list, tuple, set)):
        parts = [_limitation_text(item) for item in value]
        return "; ".join(dict.fromkeys(part for part in parts if part))
    return str(value).strip()


async def get_training_limitations_fact(
    db: SupportsDB, user_id: int
) -> dict[str, Any] | None:
    """Canonical training limitation fact, synthesized from legacy fields.

    ``training_limitations`` is the planning source of truth. Older installs may
    still have ``active_pain`` and/or ``medical_avoidance`` rows; synthesize a
    canonical fact so existing injury and medical-avoidance data is preserved
    without asking the user twice.
    """
    canonical = await get_fact(db, user_id, "training_limitations")
    if canonical is not None:
        return canonical

    legacy: list[dict[str, Any]] = []
    for key in ("active_pain", "medical_avoidance"):
        fact = await get_fact(db, user_id, key)
        if fact is not None:
            legacy.append(fact)
    if not legacy:
        return None

    if any(fact.get("kind") == KIND_GAP for fact in legacy):
        return {
            "user_id": user_id,
            "key": "training_limitations",
            "value": {"missing": True},
            "kind": KIND_GAP,
            "source": SOURCE_USER,
            "confidence": 0.0,
            "confirmed": False,
            "valid": True,
            "affects": list(FACT_REGISTRY["training_limitations"].affects),
            "updated_at": max((str(fact.get("updated_at") or "") for fact in legacy), default="") or _now(),
            "created_at": max((str(fact.get("created_at") or "") for fact in legacy), default="") or _now(),
        }

    texts = [_limitation_text(fact.get("value")) for fact in legacy]
    texts = [text for text in texts if text]
    if not texts:
        if all(fact.get("confirmed") for fact in legacy):
            value: Any = "none"
        else:
            return None
    else:
        value = "; ".join(dict.fromkeys(texts))

    return {
        "user_id": user_id,
        "key": "training_limitations",
        "value": value,
        "kind": KIND_FACT,
        "source": SOURCE_USER,
        "confidence": max(float(fact.get("confidence") or 0.85) for fact in legacy),
        "confirmed": any(bool(fact.get("confirmed")) for fact in legacy),
        "valid": True,
        "affects": list(FACT_REGISTRY["training_limitations"].affects),
        "updated_at": max((str(fact.get("updated_at") or "") for fact in legacy), default="") or _now(),
        "created_at": max((str(fact.get("created_at") or "") for fact in legacy), default="") or _now(),
    }


async def ensure_training_limitations_fact(db: SupportsDB, user_id: int) -> dict[str, Any] | None:
    """Persist the synthesized canonical limitation fact when legacy data exists."""
    existing = await get_fact(db, user_id, "training_limitations")
    if existing is not None:
        return existing
    synthesized = await get_training_limitations_fact(db, user_id)
    if synthesized is None or synthesized.get("kind") == KIND_GAP:
        return synthesized
    await set_fact(
        db,
        user_id,
        "training_limitations",
        synthesized.get("value"),
        kind=KIND_FACT,
        source=SOURCE_USER,
        confidence=float(synthesized.get("confidence") or 0.85),
        confirmed=bool(synthesized.get("confirmed")),
        affects=FACT_REGISTRY["training_limitations"].affects,
    )
    return await get_fact(db, user_id, "training_limitations")


async def get_value(db: SupportsDB, user_id: int, key: str, default: Any = None) -> Any:
    """Raw/draft read — returns the stored value regardless of confirmation
    or freshness. B11/ARCH-15 read policy: decision-grade consumers (plan,
    goal targets, safety gates, menu-style decisions) must use
    ``get_decision_value``; display flows use ``get_display_value``; raw
    reads remain only where the semantics demand them (e.g. restrictions
    that must fail closed, audit trails via ``get_raw_fact``)."""
    fact = (
        await get_training_limitations_fact(db, user_id)
        if key == "training_limitations"
        else await get_fact(db, user_id, key)
    )
    return fact["value"] if fact else default


async def get_decision_value(
    db: SupportsDB, user_id: int, key: str, default: Any = None,
) -> Any:
    """Read a fact's value only if it passes ``fact_is_usable_for_decision``
    (FIX 47): fresh, and not an unconfirmed estimate/derived fact.

    Use this instead of ``get_value`` for anything that drives a plan,
    safety check, restriction filter, or other live decision. A stale or
    unconfirmed fact returns ``default`` here even though ``get_value``
    would still happily return its (not-yet-trustworthy) value -- that gap
    is exactly what let onboarding correctly show a fact as "needs
    confirmation" while a planning/safety reader used it as authoritative
    anyway.
    """
    fact = (
        await get_training_limitations_fact(db, user_id)
        if key == "training_limitations"
        else await get_fact(db, user_id, key)
    )
    if fact is None or not fact_is_usable_for_decision(key, fact):
        return default
    return fact["value"]


async def get_display_value(db: SupportsDB, user_id: int, key: str, default: Any = None) -> Any:
    """Read a fact's value for display/prompt purposes regardless of
    confirmation/freshness policy. Equivalent to ``get_value`` -- kept as a
    distinctly-named alias so call sites can declare intent (FIX 47:
    "all fact reads must declare their intended policy: decision-grade,
    draft, display, or raw audit").
    """
    return await get_value(db, user_id, key, default)


async def get_raw_fact(db: SupportsDB, user_id: int, key: str) -> dict[str, Any] | None:
    """Return the complete raw fact row (kind/source/confidence/confirmed/
    valid), for audit trails and debugging -- never for a decision path."""
    return (
        await get_training_limitations_fact(db, user_id)
        if key == "training_limitations"
        else await get_fact(db, user_id, key)
    )


def is_fact_fresh(fact: dict[str, Any] | None, key: str) -> bool:
    """Check if a fact is still within its expiry window (if one is defined)."""
    return fact_is_fresh(key, fact)


async def stale_facts(db: SupportsDB, user_id: int) -> list[str]:
    """Return keys of facts that have expired and need refreshing."""
    stale: list[str] = []
    for key, spec in FACT_REGISTRY.items():
        if spec.expires_after_days is None:
            continue
        fact = await get_fact(db, user_id, key)
        if fact and fact["kind"] != KIND_GAP and not is_fact_fresh(fact, key):
            stale.append(key)
    return stale


async def record_gap(
    db: SupportsDB,
    user_id: int,
    key: str,
    *,
    why_matters: str,
    affects: tuple[str, ...] | None = None,
) -> None:
    """Record meaningful missing information (only if not already known).

    If the fact already exists with a real value (non-gap), it is left alone.
    If the fact is already a gap, or doesn't exist, we (re-)write the gap.
    """
    existing = await get_fact(db, user_id, key)
    if existing is not None and existing["kind"] != KIND_GAP:
        return
    spec = FACT_REGISTRY.get(key)
    await set_fact(
        db,
        user_id,
        key,
        {"missing": True, "why_matters": why_matters},
        kind=KIND_GAP,
        source=SOURCE_SYSTEM,
        confidence=0.0,
        affects=affects if affects is not None else (spec.affects if spec else ()),
    )


async def record_skip(db: SupportsDB, user_id: int, key: str) -> None:
    """Mark a question as explicitly skipped by the user (TASK-02).

    A plain gap (never asked, or deferred during onboarding) stays eligible
    to be asked again; a skip is a deliberate "not now" and must not be
    re-surfaced by the normal question loop. is_skipped_gap() below is the
    one place that distinguishes the two.
    """
    await set_fact(
        db,
        user_id,
        key,
        {"missing": True, "skipped": True},
        kind=KIND_GAP,
        source=SOURCE_USER,
        confidence=0.0,
        affects=(FACT_REGISTRY[key].affects if key in FACT_REGISTRY else ()),
    )


def is_skipped_gap(fact: dict[str, Any] | None) -> bool:
    """True when a gap fact was a deliberate user skip, not a plain unknown."""
    if fact is None or fact.get("kind") != KIND_GAP:
        return False
    value = fact.get("value")
    return isinstance(value, dict) and bool(value.get("skipped"))


def _hydrate(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["value"] = json.loads(row["value"]) if row.get("value") else None
    out["affects"] = json.loads(row["affects"]) if row.get("affects") else []
    out["confirmed"] = bool(row.get("confirmed"))
    out["valid"] = bool(row.get("valid"))
    return out


async def get_profile_view(db: SupportsDB, user_id: int) -> dict[str, list[dict[str, Any]]]:
    """Group valid facts into measured / inferred / reported / gaps.

    Implements the chapter-3 separation: נמצא / הוסק / דווח / לא ידוע.
    """
    rows = await db.fetch_all(
        "SELECT * FROM user_facts WHERE user_id=? AND valid=1 ORDER BY key",
        (user_id,),
    )
    view: dict[str, list[dict[str, Any]]] = {
        "measured": [],
        "inferred": [],
        "reported": [],
        "gaps": [],
    }
    for row in rows:
        fact = _hydrate(row)
        spec = FACT_REGISTRY.get(fact["key"])
        # Skip unregistered keys and non-user-visible facts.
        if spec is None or spec.visibility != "user":
            continue
        if fact["kind"] == KIND_GAP:
            view["gaps"].append(fact)
            continue
        view.get(spec.category, view["reported"]).append(fact)
    return view


def _audit_action_required(key: str, fact: dict[str, Any] | None) -> str:
    status = fact_confirmation_status(fact, key)
    if status == "missing":
        return "ask_user"
    if status == CONFIRM_DEFERRED:
        return "ask_user"
    if status == CONFIRM_STALE:
        return "refresh_health" if fact and fact.get("source") == SOURCE_APPLE_HEALTH else "confirm"
    if status == CONFIRM_INVALID:
        return "correct"
    if status in {CONFIRM_INFERRED, CONFIRM_NOT_APPLICABLE}:
        return "confirm"
    return "none"


def _audit_freshness(key: str, fact: dict[str, Any] | None) -> str:
    if fact is None or fact.get("kind") == KIND_GAP:
        return "unknown"
    spec = FACT_REGISTRY.get(key)
    if spec is None or spec.expires_after_days is None:
        return "fresh"
    return "fresh" if fact_is_fresh(key, fact) else "stale"


def _audit_source_kind(fact: dict[str, Any] | None) -> str:
    if fact is None:
        return "unknown"
    source = fact.get("source")
    kind = fact.get("kind")
    if source == SOURCE_APPLE_HEALTH:
        return "apple_health"
    if source == SOURCE_USER:
        return "user"
    if source == SOURCE_DERIVED or kind == KIND_ESTIMATE:
        return "inferred"
    if source == SOURCE_SYSTEM:
        return "default"
    return "unknown"


async def build_profile_audit(
    db: SupportsDB,
    user_id: int,
    keys: list[str] | tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    """Return an auditable profile row per fact.

    This is the machine-readable basis for the user-facing "profile audit"
    screen: each row separates source, confidence, approval, freshness and the
    next action instead of rendering a flat profile blob.
    """
    selected = list(keys) if keys is not None else [
        key for key, spec in FACT_REGISTRY.items() if spec.visibility == "user"
    ]
    rows: list[dict[str, Any]] = []
    for key in selected:
        fact = await get_fact(db, user_id, key)
        spec = FACT_REGISTRY.get(key)
        value = fact.get("value") if fact else None
        confirmation_status = fact_confirmation_status(fact, key)
        rows.append(
            {
                "field_name": key,
                "label": spec.label if spec else key,
                "value": value,
                "display_value": display_value(key, value) if fact else None,
                "source": fact.get("source", "unknown") if fact else "unknown",
                "source_kind": _audit_source_kind(fact),
                "confidence": float(fact.get("confidence", 0.0)) if fact else 0.0,
                "confidence_label": confidence_label(float(fact.get("confidence", 0.0))) if fact else "נמוכה",
                "approved": bool(fact.get("confirmed")) if fact else False,
                "approved_status": confirmation_status,
                "freshness": _audit_freshness(key, fact),
                "last_updated": fact.get("updated_at") if fact else None,
                "kind": fact.get("kind", KIND_GAP) if fact else KIND_GAP,
                "action_required": _audit_action_required(key, fact),
            }
        )
    return rows


SOURCE_LABELS = {
    SOURCE_APPLE_HEALTH: "Apple Health",
    SOURCE_DERIVED: "הוסק מהנתונים",
    SOURCE_USER: "דיווח שלך",
    SOURCE_SYSTEM: "המערכת",
}

CONFIDENCE_LABELS = [
    (0.85, "גבוהה"),
    (0.6, "בינונית"),
    (0.0, "נמוכה"),
]


def confidence_label(confidence: float) -> str:
    for threshold, label in CONFIDENCE_LABELS:
        if confidence >= threshold:
            return label
    return "נמוכה"


async def explain_fact(db: SupportsDB, user_id: int, key: str) -> str:
    """Human-readable provenance line for a fact (chapter 14 / 20)."""
    fact = await get_fact(db, user_id, key)
    spec = FACT_REGISTRY.get(key)
    label = spec.label if spec else key
    if not fact:
        return f"{label}: לא ידוע"
    src = SOURCE_LABELS.get(fact["source"], fact["source"])
    conf = confidence_label(float(fact["confidence"]))
    when = (fact.get("updated_at") or "")[:10]
    confirmed = "אושר על ידך" if fact["confirmed"] else "טרם אושר"
    affects = "، ".join(fact["affects"]) if fact["affects"] else "—"
    return (
        f"{label}: {fact['value']}\n"
        f"מקור: {src} | עודכן: {when} | ודאות: {conf} | {confirmed}\n"
        f"משפיע על: {affects}"
    )
