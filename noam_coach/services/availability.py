"""Canonical training availability resolver (REC-PROGRAM-04-01).

Resolves the user's training availability from all available sources in
priority order, producing a single authoritative TrainingAvailability value.

Priority (highest → lowest):
  user_corrected  — user explicitly corrected an inferred value
  user_confirmed  — user confirmed an inferred or prompted value
  active_plan     — extracted from the currently active workout plan
  inferred_history — derived from Apple Health workout history (estimate)
  default         — built-in fallback values

Apple Health inferred data never overrides explicit user values.

No Telegram imports — fully testable in isolation.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import user_model
from user_model import (
    CONFIRM_CONFIRMED,
    CONFIRM_CORRECTED,
    KIND_ESTIMATE,
    SOURCE_USER,
    get_fact,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SOURCE_PRIORITY: list[str] = [
    "user_corrected",
    "user_confirmed",
    "active_plan",
    "inferred_history",
    "default",
]

WEEKDAY_NAMES: dict[int, str] = {
    0: "ראשון",
    1: "שני",
    2: "שלישי",
    3: "רביעי",
    4: "חמישי",
    5: "שישי",
    6: "שבת",
}

_DEFAULT_DAYS_PER_WEEK = 3
_DEFAULT_PREFERRED_DAYS: list[int] = [0, 2, 4]
_DEFAULT_SESSION_MINUTES = 45
_DAY_ALIASES: dict[int, tuple[str, ...]] = {
    0: ("ראשון", "יום ראשון", "בראשון", "א׳", "א'"),
    1: ("שני", "יום שני", "בשני", "ב׳", "ב'"),
    2: ("שלישי", "יום שלישי", "בשלישי", "ג׳", "ג'"),
    3: ("רביעי", "יום רביעי", "ברביעי", "ד׳", "ד'"),
    4: ("חמישי", "יום חמישי", "בחמישי", "ה׳", "ה'"),
    5: ("שישי", "יום שישי", "בשישי", "ו׳", "ו'"),
    6: ("שבת", "יום שבת", "בשבת"),
}
_HEBREW_HOURS: dict[str, int] = {
    "אחת": 1,
    "שתיים": 2,
    "שניים": 2,
    "שלוש": 3,
    "ארבע": 4,
    "חמש": 5,
    "שש": 6,
    "שבע": 7,
    "שמונה": 8,
    "תשע": 9,
    "עשר": 10,
}


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------

@dataclass
class TrainingAvailability:
    """Resolved training availability, with full provenance."""

    max_days_per_week: int
    preferred_days: list[int]          # weekday indices, 0=Sunday … 6=Saturday
    preferred_time: str | None         # "HH:MM" or None
    session_minutes: int
    source: str                        # one of SOURCE_PRIORITY
    confidence: float
    confirmed: bool


@dataclass(frozen=True)
class ParsedAvailabilityAnswer:
    weekly_availability: list[dict[str, Any]]
    workout_window: str | None
    session_minutes: int | None

    @property
    def training_days_per_week(self) -> int | None:
        return len(self.weekly_availability) or None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _source_rank(source: str) -> int:
    """Lower rank = higher priority."""
    try:
        return SOURCE_PRIORITY.index(source)
    except ValueError:
        return len(SOURCE_PRIORITY)


def _fact_source_label(fact: dict[str, Any]) -> str:
    """Map a user_facts row to one of our SOURCE_PRIORITY labels."""
    status = user_model.fact_confirmation_status(fact, fact.get("key", ""))
    if status == CONFIRM_CORRECTED:
        return "user_corrected"
    if status == CONFIRM_CONFIRMED:
        return "user_confirmed"
    # Estimates from Apple Health / derived sources land as inferred_history.
    if fact.get("kind") == KIND_ESTIMATE:
        return "inferred_history"
    # A plain user-reported fact that was not yet confirmed stays as confirmed
    # if the source is user_report (they stated it directly).
    if fact.get("source") == SOURCE_USER:
        return "user_confirmed"
    return "inferred_history"


def _parse_weekly_availability_days(value: Any) -> list[int]:
    """Extract weekday indices where available=True from a weekly_availability value."""
    if not value or not isinstance(value, list):
        return []
    days: list[int] = []
    for slot in value:
        if not isinstance(slot, dict):
            continue
        if slot.get("available", True):
            try:
                days.append(int(slot["weekday"]))
            except (KeyError, TypeError, ValueError):
                pass
    return sorted(set(days))


def _parse_workout_window_time(value: Any) -> str | None:
    """Extract an HH:MM string from a workout_window fact value."""
    if value is None:
        return None
    if isinstance(value, str):
        # Accept bare "HH:MM"
        stripped = value.strip()
        if len(stripped) == 5 and stripped[2] == ":":
            return stripped
        return None
    if isinstance(value, dict):
        # Some implementations store {"start": "HH:MM", ...}
        for key in ("start", "time", "typical_hour"):
            candidate = value.get(key)
            if isinstance(candidate, str) and len(candidate) == 5 and candidate[2] == ":":
                return candidate
    return None


def _parse_workout_pattern(value: Any) -> dict[str, Any]:
    """Normalise a workout_pattern fact value into a plain dict."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    # If stored as a JSON string (shouldn't happen after _hydrate, but be safe)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
    return {}


def parse_hebrew_availability_answer(text: str) -> ParsedAvailabilityAnswer:
    """Parse common Hebrew free-text availability into structured facts.

    Each day keeps its OWN time. The input is split into per-day segments so
    "ראשון 19:00, שני 19, שלישי 18:30 45 דקות" stores Sunday 19:00, Monday
    19:00, Tuesday 18:30 (not the first time for everyone). A segment may list
    several days that share one explicit time. A global/representative time is
    used ONLY for a day whose own segment carries no time; the system clock is
    never used.
    """
    normalized = f" {text.strip()} "
    session_minutes = _parse_hebrew_duration_minutes(normalized)
    minutes = session_minutes or _DEFAULT_SESSION_MINUTES

    segments = _segment_availability_by_day(normalized)
    if not segments:
        # No day mentioned: nothing to schedule per-day, but still surface a
        # global time/duration if the user gave one (e.g. "בערב, 45 דקות").
        return ParsedAvailabilityAnswer(
            weekly_availability=[],
            workout_window=_parse_hebrew_time(normalized),
            session_minutes=session_minutes,
        )

    # Representative window: first segment that actually carries a time, else a
    # global parse over the whole text (handles "ראשון ורביעי בערב").
    global_time = next(
        (seg_time for _days, seg_time in segments if seg_time is not None),
        None,
    ) or _parse_hebrew_time(normalized)

    slots: dict[int, dict[str, Any]] = {}
    for seg_days, seg_time in segments:
        start = seg_time if seg_time is not None else global_time
        for day in seg_days:
            slots[day] = {
                "weekday": day,
                "start": start,
                "minutes": minutes,
                "available": True,
            }

    weekly_availability = [slots[day] for day in sorted(slots)]
    return ParsedAvailabilityAnswer(
        weekly_availability=weekly_availability,
        workout_window=global_time,
        session_minutes=session_minutes,
    )


def _segment_availability_by_day(text: str) -> list[tuple[list[int], str | None]]:
    """Split free text into per-day segments anchored on day-name tokens.

    Returns a list of (weekdays, time) where *weekdays* are the day indices that
    appear together before the next day's text, and *time* is the explicit time
    found inside that segment (or None). Consecutive days with no time in
    between are grouped so a single shared time still applies to all of them,
    e.g. "שני וחמישי ב-19:30".
    """
    matches = _find_day_tokens(text)
    if not matches:
        return []

    segments: list[tuple[list[int], str | None]] = []
    pending_days: list[int] = []
    for position, (start_idx, end_idx, day) in enumerate(matches):
        # Text from just after this day token up to the next day token.
        next_start = matches[position + 1][0] if position + 1 < len(matches) else len(text)
        between = text[end_idx:next_start]
        seg_time = _parse_segment_time(between)
        pending_days.append(day)
        if seg_time is not None:
            segments.append((pending_days, seg_time))
            pending_days = []
    if pending_days:
        # Trailing days with no explicit time fall back to the global window.
        segments.append((pending_days, None))
    return segments


def _find_day_tokens(text: str) -> list[tuple[int, int, int]]:
    """Locate day-name tokens with positions, longest-alias-first, no overlaps.

    Returns (start, end, weekday) sorted by position. Longer aliases (e.g.
    "יום ראשון") win over shorter ones ("ראשון") so a day is counted once.
    """
    candidates: list[tuple[int, int, int]] = []
    aliases: list[tuple[str, int]] = [
        (alias, day) for day, names in _DAY_ALIASES.items() for alias in names
    ]
    # Longest aliases first so we prefer the most specific match at a position.
    aliases.sort(key=lambda pair: len(pair[0]), reverse=True)
    occupied: list[tuple[int, int]] = []
    for alias, day in aliases:
        search_from = 0
        while True:
            idx = text.find(alias, search_from)
            if idx == -1:
                break
            end = idx + len(alias)
            if not any(idx < occ_end and end > occ_start for occ_start, occ_end in occupied):
                candidates.append((idx, end, day))
                occupied.append((idx, end))
            search_from = idx + 1
    candidates.sort(key=lambda item: item[0])
    return candidates


def _parse_hebrew_days(text: str) -> list[int]:
    found: list[int] = []
    for day, aliases in _DAY_ALIASES.items():
        if any(alias in text for alias in aliases):
            found.append(day)
    return sorted(set(found))


def _parse_segment_time(segment: str) -> str | None:
    """Parse a time inside a single day's segment, allowing a bare hour.

    Within a day's own text "שני 19" should mean 19:00. A bare hour is accepted
    only when it is NOT immediately followed by a duration word (so "45 דקות"
    is never read as an hour). Falls back to the shared Hebrew time parser for
    everything else (explicit HH:MM, word hours, parts of day).
    """
    padded = f" {segment.strip()} "
    explicit = _parse_hebrew_time(padded)
    if explicit is not None:
        return explicit
    # Bare hour like "19" or "9" not attached to a duration ("45 דקות") and not
    # part of a longer number.
    bare = re.search(r"(?<!\d)([01]?\d|2[0-3])(?!\d)(?!\s*(?:דקות|דקה|דק))", padded)
    if bare:
        hour = int(bare.group(1))
        if ("ערב" in padded or "לילה" in padded) and hour < 12:
            hour += 12
        return f"{hour:02d}:00"
    return None


def _parse_hebrew_time(text: str) -> str | None:
    explicit = re.search(r"(?<!\d)([01]?\d|2[0-3])[:.](\d{2})(?!\d)", text)
    if explicit:
        return f"{int(explicit.group(1)):02d}:{int(explicit.group(2)):02d}"
    word_hour = next((hour for word, hour in _HEBREW_HOURS.items() if word in text), None)
    if word_hour is not None:
        if "ערב" in text or "לילה" in text:
            word_hour = word_hour + 12 if word_hour < 12 else word_hour
        return f"{word_hour:02d}:00"
    hour_match = re.search(r"(?:בשעה|ב־|ב-)\s*([01]?\d|2[0-3])(?!\d)", text)
    if hour_match:
        hour = int(hour_match.group(1))
        if ("ערב" in text or "לילה" in text) and hour < 12:
            hour += 12
        return f"{hour:02d}:00"
    if "בוקר" in text:
        return "07:00"
    if "צהריים" in text or "צהרים" in text:
        return "12:00"
    if "ערב" in text:
        return "18:00"
    return None


def _parse_hebrew_duration_minutes(text: str) -> int | None:
    match = re.search(r"(?<!\d)(\d{2,3})\s*(?:דקות|דקה|דק)", text)
    if match:
        minutes = int(match.group(1))
        if 10 <= minutes <= 300:
            return minutes
    if "שעה וחצי" in text:
        return 90
    if "שעה" in text:
        return 60
    return None


# ---------------------------------------------------------------------------
# Core resolver
# ---------------------------------------------------------------------------

async def resolve_availability(db: Any, user_id: int) -> TrainingAvailability:
    """Resolve training availability from all sources, highest priority wins.

    Each field is resolved independently; the highest-priority non-None value
    for each field is used. Apple Health inferred data (kind=estimate, source
    not user_report) never overrides explicit user values.
    """
    # Resolved field candidates: (source_label, value, confidence, confirmed)
    days_per_week_candidates: list[tuple[str, int, float, bool]] = []
    preferred_days_candidates: list[tuple[str, list[int], float, bool]] = []
    preferred_time_candidates: list[tuple[str, str | None, float, bool]] = []
    session_minutes_candidates: list[tuple[str, int, float, bool]] = []

    # ------------------------------------------------------------------
    # (a) training_days_per_week — explicit user report
    # ------------------------------------------------------------------
    tdpw_fact = await get_fact(db, user_id, "training_days_per_week")
    if tdpw_fact and tdpw_fact.get("kind") != user_model.KIND_GAP:
        raw_val = tdpw_fact.get("value")
        try:
            days_int = int(float(raw_val))
        except (TypeError, ValueError):
            days_int = None
        if days_int is not None and 1 <= days_int <= 7:
            src_label = _fact_source_label(tdpw_fact)
            conf = float(tdpw_fact.get("confidence") or 0.85)
            confirmed = bool(tdpw_fact.get("confirmed"))
            days_per_week_candidates.append((src_label, days_int, conf, confirmed))

    # ------------------------------------------------------------------
    # (b) weekly_availability — extract preferred days
    # ------------------------------------------------------------------
    avail_fact = await get_fact(db, user_id, "weekly_availability")
    if avail_fact and avail_fact.get("kind") != user_model.KIND_GAP:
        parsed_days = _parse_weekly_availability_days(avail_fact.get("value"))
        if parsed_days:
            src_label = _fact_source_label(avail_fact)
            conf = float(avail_fact.get("confidence") or 0.85)
            confirmed = bool(avail_fact.get("confirmed"))
            preferred_days_candidates.append((src_label, parsed_days, conf, confirmed))
            # Also treat count of available days as a days_per_week signal
            # (lower priority than explicit training_days_per_week)
            if not days_per_week_candidates:
                days_per_week_candidates.append(
                    (src_label, len(parsed_days), conf, confirmed)
                )

    # ------------------------------------------------------------------
    # (c) session_minutes — confirmed user report
    # ------------------------------------------------------------------
    sm_fact = await get_fact(db, user_id, "session_minutes")
    if sm_fact and sm_fact.get("kind") != user_model.KIND_GAP:
        raw_val = sm_fact.get("value")
        try:
            sm_int = int(float(raw_val))
        except (TypeError, ValueError):
            sm_int = None
        if sm_int is not None and 10 <= sm_int <= 300:
            src_label = _fact_source_label(sm_fact)
            conf = float(sm_fact.get("confidence") or 0.85)
            confirmed = bool(sm_fact.get("confirmed"))
            session_minutes_candidates.append((src_label, sm_int, conf, confirmed))

    # ------------------------------------------------------------------
    # (d) workout_window — extract preferred time
    # ------------------------------------------------------------------
    ww_fact = await get_fact(db, user_id, "workout_window")
    if ww_fact and ww_fact.get("kind") != user_model.KIND_GAP:
        time_str = _parse_workout_window_time(ww_fact.get("value"))
        if time_str:
            src_label = _fact_source_label(ww_fact)
            conf = float(ww_fact.get("confidence") or 0.85)
            confirmed = bool(ww_fact.get("confirmed"))
            preferred_time_candidates.append((src_label, time_str, conf, confirmed))

    # ------------------------------------------------------------------
    # (e) active_plan "workout" — frequency from plan payload
    # ------------------------------------------------------------------
    active_plan_fact = await get_fact(db, user_id, "active_workout_plan")
    if active_plan_fact and active_plan_fact.get("kind") != user_model.KIND_GAP:
        plan_val = active_plan_fact.get("value") or {}
        if isinstance(plan_val, dict):
            freq = plan_val.get("frequency") or plan_val.get("weekly_frequency")
            try:
                freq_int = int(float(freq))
            except (TypeError, ValueError):
                freq_int = None
            if freq_int is not None and 1 <= freq_int <= 7:
                days_per_week_candidates.append(
                    ("active_plan", freq_int, 0.9, True)
                )
            # Extract preferred days from sessions weekdays if available
            sessions = plan_val.get("sessions") or []
            if sessions and isinstance(sessions, list):
                plan_days = sorted(
                    set(
                        int(s["weekday"])
                        for s in sessions
                        if isinstance(s, dict) and "weekday" in s
                    )
                )
                if plan_days:
                    preferred_days_candidates.append(
                        ("active_plan", plan_days, 0.9, True)
                    )
            # Extract session duration if stored in plan
            session_dur = plan_val.get("session_minutes") or plan_val.get("duration_minutes")
            try:
                dur_int = int(float(session_dur))
            except (TypeError, ValueError):
                dur_int = None
            if dur_int is not None and 10 <= dur_int <= 300:
                session_minutes_candidates.append(("active_plan", dur_int, 0.9, True))

    # ------------------------------------------------------------------
    # (f) workout_pattern — inferred from Apple Health history
    # ------------------------------------------------------------------
    wp_fact = await get_fact(db, user_id, "workout_pattern")
    if (
        wp_fact
        and wp_fact.get("kind") != user_model.KIND_GAP
        and wp_fact.get("kind") == KIND_ESTIMATE
    ):
        pattern = _parse_workout_pattern(wp_fact.get("value"))
        conf = float(wp_fact.get("confidence") or 0.55)
        confirmed = bool(wp_fact.get("confirmed"))

        wf = pattern.get("weekly_frequency")
        try:
            wf_int = round(float(wf))
        except (TypeError, ValueError):
            wf_int = None
        if wf_int is not None and 1 <= wf_int <= 7:
            days_per_week_candidates.append(
                ("inferred_history", wf_int, conf, confirmed)
            )

        common_days = pattern.get("common_weekdays")
        if common_days and isinstance(common_days, list):
            try:
                parsed = [int(d) for d in common_days]
                if parsed:
                    preferred_days_candidates.append(
                        ("inferred_history", sorted(set(parsed)), conf, confirmed)
                    )
            except (TypeError, ValueError):
                pass

        typical_hour = pattern.get("typical_hour")
        if isinstance(typical_hour, str) and len(typical_hour) == 5 and typical_hour[2] == ":":
            preferred_time_candidates.append(
                ("inferred_history", typical_hour, conf, confirmed)
            )

        avg_dur = pattern.get("avg_duration_minutes")
        try:
            dur_int = int(float(avg_dur))
        except (TypeError, ValueError):
            dur_int = None
        if dur_int is not None and 10 <= dur_int <= 300:
            session_minutes_candidates.append(
                ("inferred_history", dur_int, conf, confirmed)
            )

    # ------------------------------------------------------------------
    # Pick best candidate per field (lowest source_rank wins)
    # ------------------------------------------------------------------
    def _best(candidates: list[tuple[str, Any, float, bool]]) -> tuple[str, Any, float, bool] | None:
        if not candidates:
            return None
        return min(candidates, key=lambda c: _source_rank(c[0]))

    best_dpw = _best(days_per_week_candidates)
    best_pdays = _best(preferred_days_candidates)
    best_time = _best(preferred_time_candidates)
    best_sm = _best(session_minutes_candidates)

    # ------------------------------------------------------------------
    # Determine overall source / confidence / confirmed from the most
    # authoritative field (days_per_week drives the headline source).
    # ------------------------------------------------------------------
    overall_src = (best_dpw or best_pdays or best_sm or best_time)
    if overall_src:
        top_source = overall_src[0]
        top_confidence = overall_src[2]
        top_confirmed = overall_src[3]
    else:
        top_source = "default"
        top_confidence = 0.5
        top_confirmed = False

    return TrainingAvailability(
        max_days_per_week=best_dpw[1] if best_dpw else _DEFAULT_DAYS_PER_WEEK,
        preferred_days=best_pdays[1] if best_pdays else list(_DEFAULT_PREFERRED_DAYS),
        preferred_time=best_time[1] if best_time else None,
        session_minutes=best_sm[1] if best_sm else _DEFAULT_SESSION_MINUTES,
        source=top_source,
        confidence=top_confidence,
        confirmed=top_confirmed,
    )


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _format_day_list(days: list[int]) -> str:
    """Format a list of weekday indices as a Hebrew conjunctive list."""
    if not days:
        return ""
    names = [WEEKDAY_NAMES.get(d, str(d)) for d in sorted(days)]
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " ו" + names[-1]


def format_availability_summary(avail: TrainingAvailability) -> str:
    """Return a Hebrew bullet-point summary of the resolved availability."""
    lines = ["לפי מה ששמור אצלי:"]
    lines.append(f"• עד {avail.max_days_per_week} אימונים בשבוע")
    if avail.preferred_days:
        lines.append(f"• ימים מועדפים: {_format_day_list(avail.preferred_days)}")
    lines.append(f"• כ־{avail.session_minutes} דקות לאימון")
    if avail.preferred_time:
        lines.append(f"• שעה מועדפת: {avail.preferred_time}")
    return "\n".join(lines)


def availability_confirmation_text(avail: TrainingAvailability) -> str:
    """Return a Hebrew confirmation prompt for the resolved availability."""
    return f"{format_availability_summary(avail)}\n\nזה עדיין נכון?"
