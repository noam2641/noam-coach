"""Health data services — import, sync, medication, routine profile, flags.

Extracted from coach_bot.py to keep the main module focused on Telegram
handling and routing.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import db as _db
import health_import
import routine
import user_model
from config import SETTINGS, TZ
from helpers import utc_now

HEALTH_BATCH_SIZE = 1000

# Compatibility injection point used by tests and callers.
# Services reference this alias directly so swapping the database is predictable.
_DEFAULT_DB = _db.DB
DB = _DEFAULT_DB


def _current_db():
    """Resolve the active DB while preserving legacy monkeypatch behavior."""
    if DB is not _DEFAULT_DB:
        return DB
    try:
        import coach_bot

        return coach_bot.DB
    except (ImportError, AttributeError):
        return DB


async def upsert_health_rows(
    user_id: int,
    rows: list[health_import.HealthRow],
) -> tuple[int, int]:
    """Insert a complete normalized Health import in one atomic transaction."""
    if not rows:
        return 0, 0
    now = utc_now()
    sql = (
        "INSERT INTO health("
        "user_id, external_id, sample_type, value, unit, "
        "start_time, end_time, source_device, created_at"
        ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(user_id, external_id) DO NOTHING"
    )
    total_inserted = 0
    async with _current_db().transaction() as conn:
        for start in range(0, len(rows), HEALTH_BATCH_SIZE):
            batch = rows[start : start + HEALTH_BATCH_SIZE]
            params = [
                (
                    user_id,
                    row.external_id,
                    row.sample_type,
                    row.value,
                    row.unit,
                    row.start_time,
                    row.end_time,
                    row.source_device,
                    now,
                )
                for row in batch
            ]
            await conn.executemany(sql, params)
            cursor = await conn.execute("SELECT changes()")
            row = await cursor.fetchone()
            total_inserted += row[0]
    return total_inserted, len(rows) - total_inserted


async def save_routine_profile(user_id: int) -> dict[str, Any]:
    profile = await routine.learn_profile(_current_db(), user_id, TZ, SETTINGS.routine_window_days)
    payload = profile.to_dict()
    await _current_db().execute(
        """
        INSERT INTO routine_profile(user_id, profile, updated_at)
        VALUES(?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            profile=excluded.profile,
            updated_at=excluded.updated_at
        """,
        (user_id, json.dumps(payload, ensure_ascii=False), utc_now()),
    )
    await sync_routine_to_facts(user_id, payload)
    return payload


async def sync_routine_to_facts(user_id: int, payload: dict[str, Any]) -> None:
    """Write learned routine into the user model as *estimates* (confirmed=0)."""
    sleep = payload.get("sleep") or {}
    workout = payload.get("workout") or {}
    eating = payload.get("eating") or {}
    if sleep.get("nights_sampled"):
        await user_model.set_fact(
            _current_db(), user_id, "sleep_schedule", sleep,
            kind=user_model.KIND_ESTIMATE, source=user_model.SOURCE_DERIVED,
        )
    if workout.get("sessions_sampled"):
        await user_model.set_fact(
            _current_db(), user_id, "workout_pattern", workout,
            kind=user_model.KIND_ESTIMATE, source=user_model.SOURCE_DERIVED,
        )
    if eating.get("meals_sampled"):
        await user_model.set_fact(
            _current_db(), user_id, "eating_windows", eating,
            kind=user_model.KIND_ESTIMATE, source=user_model.SOURCE_DERIVED,
        )


async def sync_health_measurements_to_facts(user_id: int) -> None:
    """Promote the latest hard measurements from the health table into facts."""

    async def latest(stype: str) -> dict[str, Any] | None:
        return await _current_db().fetch_one(
            "SELECT value, unit, start_time FROM health "
            "WHERE user_id=? AND sample_type=? ORDER BY start_time DESC LIMIT 1",
            (user_id, stype),
        )

    mapping = [
        ("weight", "weight_kg"),
        ("resting_heart_rate", "resting_hr"),
    ]
    for stype, key in mapping:
        row = await latest(stype)
        if row:
            await user_model.set_fact(
                _current_db(), user_id, key,
                round(float(row["value"]), 2),
                kind=user_model.KIND_FACT,
                source=user_model.SOURCE_APPLE_HEALTH,
            )

    # REC-PROGRAM-04-08: Body-fat sync uses the centralized normalizer
    bf_row = await latest("body_fat")
    if bf_row:
        from noam_coach.services.body_fat import normalize_body_fat
        bf_result = normalize_body_fat(
            bf_row["value"],
            source_type="apple_health",
        )
        if bf_result.status in ("valid", "inferred_unit") and bf_result.normalized_pct is not None:
            await user_model.set_fact(
                _current_db(), user_id, "body_fat_pct",
                bf_result.normalized_pct,
                kind=user_model.KIND_FACT,
                source=user_model.SOURCE_APPLE_HEALTH,
            )

    since = (datetime.now(timezone.utc) - timedelta(days=SETTINGS.routine_window_days)).isoformat()
    avg = await _current_db().fetch_one(
        "SELECT AVG(value) AS v FROM health "
        "WHERE user_id=? AND sample_type='steps' AND start_time>=?",
        (user_id, since),
    )
    if avg and avg["v"] is not None:
        await user_model.set_fact(
            _current_db(), user_id, "avg_steps",
            round(float(avg["v"])),
            kind=user_model.KIND_FACT,
            source=user_model.SOURCE_APPLE_HEALTH,
        )


async def load_routine_profile(user_id: int) -> dict[str, Any]:
    row = await _current_db().fetch_one("SELECT profile FROM routine_profile WHERE user_id=?", (user_id,))
    if row:
        return json.loads(row["profile"])
    return await save_routine_profile(user_id)


def local_day_str() -> str:
    return datetime.now(TZ).date().isoformat()


async def get_daily_flags(user_id: int, day: str | None = None) -> dict[str, Any]:
    day = day or local_day_str()
    row = await _current_db().fetch_one(
        "SELECT flags FROM daily_flags WHERE user_id=? AND day=?",
        (user_id, day),
    )
    return json.loads(row["flags"]) if row else {}


async def set_daily_flags(user_id: int, flags: dict[str, Any]) -> None:
    await _current_db().execute(
        """
        INSERT INTO daily_flags(user_id, day, flags, created_at)
        VALUES(?, ?, ?, ?)
        ON CONFLICT(user_id, day) DO UPDATE SET flags=excluded.flags
        """,
        (user_id, local_day_str(), json.dumps(flags, ensure_ascii=False), utc_now()),
    )


async def record_medication(
    user_id: int,
    name: str,
    *,
    source: str = "user_button",
    status: str = "taken",
    dose_text: str | None = None,
    confidence: float = 0.95,
) -> int:
    """Record a medication event (reported by the user — never from live data)."""
    import coach_bot  # deferred to avoid circular import (for write_audit)

    now = utc_now()
    event_id = await _current_db().execute(
        """
        INSERT INTO medication_events(
            user_id, name, dose_text, taken_at, received_at,
            source, status, confidence, created_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (user_id, name, dose_text, now, now, source, status, confidence, now),
    )
    known = await user_model.get_value(_current_db(), user_id, "known_medications", [])
    if name not in known:
        known = known + [name]
        await user_model.set_fact(
            _current_db(), user_id, "known_medications", known,
            kind=user_model.KIND_FACT, source=user_model.SOURCE_USER,
            confirmed=True, affects=("meal_timing", "hydration"),
        )
    flags = await get_daily_flags(user_id)
    if status == "taken":
        meds_today = flags.get("medications", [])
        if name not in meds_today:
            flags["medications"] = meds_today + [name]
        if "ריטלין" in name or name.lower() == "ritalin":
            flags["ritalin"] = True
        await set_daily_flags(user_id, flags)
    await coach_bot.write_audit(user_id, "medication", "med_event", event_id, name=name)
    return event_id


async def known_medications(user_id: int) -> list[str]:
    return await user_model.get_value(_current_db(), user_id, "known_medications", [])


# ---------------------------------------------------------------------------
# Health-export freshness  (REC-ONBOARD-02-02)
# ---------------------------------------------------------------------------

FRESHNESS_CURRENT = "current"       # newest record is today
FRESHNESS_RECENT = "recent"         # 1 day old
FRESHNESS_STALE = "stale"           # 2-7 days
FRESHNESS_VERY_STALE = "very_stale" # >7 days
FRESHNESS_UNAVAILABLE = "unavailable"

_FRESHNESS_LABELS: dict[str, str] = {
    FRESHNESS_CURRENT: "עדכני",
    FRESHNESS_RECENT: "עדכני (אתמול)",
    FRESHNESS_STALE: "לא עדכני",
    FRESHNESS_VERY_STALE: "ישן מאוד",
    FRESHNESS_UNAVAILABLE: "אין נתונים",
}


async def health_export_freshness(user_id: int) -> dict[str, Any]:
    """Return freshness classification and newest record date for a user.

    Uses timezone-aware calculations based on the configured user timezone.
    """
    row = await _current_db().fetch_one(
        "SELECT MAX(start_time) AS newest FROM health WHERE user_id=?",
        (user_id,),
    )
    if not row or not row["newest"]:
        return {
            "freshness": FRESHNESS_UNAVAILABLE,
            "label": _FRESHNESS_LABELS[FRESHNESS_UNAVAILABLE],
            "newest_date": None,
            "age_days": None,
        }
    try:
        newest_dt = datetime.fromisoformat(str(row["newest"]))
        if newest_dt.tzinfo is None:
            newest_dt = newest_dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return {
            "freshness": FRESHNESS_UNAVAILABLE,
            "label": _FRESHNESS_LABELS[FRESHNESS_UNAVAILABLE],
            "newest_date": None,
            "age_days": None,
        }

    now = datetime.now(TZ)
    newest_local = newest_dt.astimezone(TZ)
    # Guard against future-dated records
    if newest_local.date() > now.date():
        age_days = 0
        freshness = FRESHNESS_CURRENT
    else:
        age_days = (now.date() - newest_local.date()).days
        if age_days == 0:
            freshness = FRESHNESS_CURRENT
        elif age_days == 1:
            freshness = FRESHNESS_RECENT
        elif age_days <= 7:
            freshness = FRESHNESS_STALE
        else:
            freshness = FRESHNESS_VERY_STALE

    return {
        "freshness": freshness,
        "label": _FRESHNESS_LABELS[freshness],
        "newest_date": newest_local.date().isoformat(),
        "age_days": age_days,
    }


def freshness_warning_text(info: dict[str, Any]) -> str:
    """Return a user-facing Hebrew warning about stale health data, or empty string."""
    freshness = info.get("freshness", FRESHNESS_UNAVAILABLE)
    if freshness in (FRESHNESS_CURRENT, FRESHNESS_RECENT):
        return ""
    newest = info.get("newest_date", "")
    if freshness == FRESHNESS_UNAVAILABLE:
        return (
            "אין נתוני Apple Health במאגר.\n"
            "אפשר לייבא קובץ ייצוא עם /import."
        )
    age = info.get("age_days", 0)
    return (
        f"נתוני Apple Health האחרונים שלך הם מתאריך {newest}.\n"
        f"הנתונים אינם מתעדכנים אוטומטית "
        f"(לפני {age} ימים), ולכן לא אשתמש בהם כנתוני הפעילות של היום."
    )


async def is_today_activity_available(user_id: int) -> bool:
    """Return True only if health data for today exists in the import."""
    today = datetime.now(TZ).date().isoformat()
    row = await _current_db().fetch_one(
        "SELECT 1 FROM health WHERE user_id=? AND start_time >= ? LIMIT 1",
        (user_id, today),
    )
    return row is not None


_DEFAULT_MED_NAMES = [
    "ריטלין",
    "ritalin",
    "אומפרדקס",
    "אומפרזול",
    "קונצרטה",
    "concerta",
    "ונלפקסין",
    "ציפרלקס",
]


async def all_medication_names(user_id: int) -> list[str]:
    """Return known medication names: built-in defaults + any user-specific ones."""
    user_meds = await user_model.get_value(_current_db(), user_id, "known_medications", [])
    rows = await _current_db().fetch_all(
        "SELECT DISTINCT name FROM medication_events WHERE user_id=? AND name IS NOT NULL",
        (user_id,),
    )
    reported = [r["name"] for r in rows if r["name"]]
    seen: set[str] = set()
    merged: list[str] = []
    for name in _DEFAULT_MED_NAMES + (user_meds if isinstance(user_meds, list) else []) + reported:
        low = name.strip().lower()
        if low and low not in seen:
            seen.add(low)
            merged.append(name.strip())
    return merged


def medication_name_from_text(
    text: str, flag: str | None, known: list[str] | None = None,
) -> str | None:
    """Extract a medication name from a free-text report, or None."""
    if flag == "ritalin":
        return "ריטלין"
    med_list = known if known is not None else _DEFAULT_MED_NAMES
    for med in med_list:
        if med in text or med.lower() in text.lower():
            return "ריטלין" if med.lower() == "ritalin" else med
    if "לקחתי" in text and ("תרופה" in text or "כדור" in text):
        return "תרופה"
    return None
