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

import datetime as dt
import json
from dataclasses import dataclass
from typing import Any, Protocol

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
    "training_days_per_week": FactSpec(
        "training_days_per_week",
        "ימי אימון בשבוע",
        "reported",
        ("workout_schedule",),
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
        "איסורים תזונתיים",
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
        required_for=("safety",),
        expires_after_days=7,
    ),
    "medical_avoidance": FactSpec(
        "medical_avoidance",
        "הימנעות לפי רופא",
        "reported",
        ("exercise_selection", "safety"),
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
    "active_pain": "כאב/פציעה פעילה",
    "medical_avoidance": "הימנעות לפי רופא",
    # Profile fields
    "primary_goal": "מטרה ראשית",
    "strength_experience": "ניסיון באימוני כוח",
    "training_location": "מקום אימון",
    "equipment": "ציוד זמין",
    "diet_restrictions": "איסורים תזונתיים",
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
    """Return a user-friendly Hebrew string for a fact value."""
    if value is None:
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
            return f"{num:.1f}%"
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
            "weight_kg", "primary_goal", "sex", "age",
            "diet_restrictions", "allergies",
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
            "primary_goal", "training_days_per_week", "active_pain",
            "medical_avoidance", "session_minutes", "training_location",
            "equipment", "strength_experience", "weekly_availability",
        ),
        optional=("workout_window", "training_preferences", "performance_goal"),
    ),
    "safety": ReadinessProfile(
        name="safety",
        label="שאלון בטיחות",
        required=("active_pain", "medical_avoidance"),
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
        fact = await get_fact(db, user_id, key)
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
        fact = await get_fact(db, user_id, key)
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
    if confidence is None:
        confidence = SOURCE_CONFIDENCE.get(source, 0.7)
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


async def confirm_fact(db: SupportsDB, user_id: int, key: str) -> None:
    """User confirmed an estimate — harden it (confidence floor + confirmed)."""
    await db.execute(
        """
        UPDATE user_facts
        SET confirmed=1,
            confidence=MAX(confidence, 0.9),
            updated_at=?
        WHERE user_id=? AND key=?
        """,
        (_now(), user_id, key),
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


async def get_value(db: SupportsDB, user_id: int, key: str, default: Any = None) -> Any:
    fact = await get_fact(db, user_id, key)
    return fact["value"] if fact else default


def is_fact_fresh(fact: dict[str, Any] | None, key: str) -> bool:
    """Check if a fact is still within its expiry window (if one is defined)."""
    if fact is None:
        return False
    spec = FACT_REGISTRY.get(key)
    if not spec or spec.expires_after_days is None:
        return True
    updated = fact.get("updated_at", "")
    if not updated:
        return True
    try:
        ts = dt.datetime.fromisoformat(updated)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt.timezone.utc)
        age = dt.datetime.now(dt.timezone.utc) - ts
        return age.days < spec.expires_after_days
    except (ValueError, TypeError):
        return True


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
