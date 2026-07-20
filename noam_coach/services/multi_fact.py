"""Multi-fact free-text profile updates during active flows (TASK-64).

Incident: mid profile/Health confirmation the user types a compact
correction carrying several facts — "גובה 174 אימונים מזוהים 4" — and the
single-question routing either answers only the pending question or drops
the message entirely, leaving the user stuck on the confirmation stage.

Architecture: a deterministic label-proximity parser (numbers bind to the
NEAREST fact label, never to position alone; unlabeled numbers stay
ambiguous) feeding the EXISTING canonical mechanisms — the protected
``_parse_health_fact_text_edit`` validation ranges, ``user_model.set_fact``,
``training_days_per_week`` + workout-pattern mirroring (the same writes the
Health wizard's own frequency step performs), sleep windows via the
protected sleep parser shape, and availability via
``parse_hebrew_availability_answer``/``save_user_training_availability``.
No second parser-of-record for any individual fact: each recognized value
is re-validated by the same code path a single-fact answer would take.

Flow continuation contract (installed as a wrap over the protected
``handle_onboarding_text``; runs only when the message carries MULTIPLE
recognizable facts, so single-fact answers keep their existing paths):

- if the active question's own fact is among the recognized ones, the side
  facts are saved first and the message is then delegated to the original
  handler carrying ONLY the active answer — the active question completes
  through its canonical path (acceptance 3);
- otherwise the recognized facts are saved, one concise confirmation lists
  them, ambiguous fragments get a targeted follow-up, and the ACTIVE
  question stays pending (re-stated) — the flow is never lost;
- ``__basics_fix__`` and ``__health_edit_*__`` pendings continue exactly
  the way their single-fact branches do (patterns screen / next wizard
  step).

Traces: decision.finalized(entity=multi_fact_extraction) with the saved
keys + ambiguous fragments, before the individual state.mutated fact
events O7 already emits.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Pendings whose free text is prose, not fact corrections — never hijack.
_EXCLUDED_PENDINGS = (
    "__manual_meal__", "__routine_confirm__", "q_daily_routine",
    "__med_name__", "__diet_classify__",
)

_NUMERIC_LABELS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("height_cm", ("גובה", "הגובה")),
    ("goal_weight_kg", ("משקל יעד", "יעד משקל")),
    ("weight_kg", ("משקל", "המשקל", "שוקל", "שוקלת")),
    ("body_fat_pct", ("אחוז שומן", "אחוזי שומן", "שומן")),
    ("training_days_per_week", ("אימונים מזוהים", "אימונים בשבוע", "אימונים")),
    ("session_minutes", ("דקות אימון", "דקות")),
    ("avg_steps", ("צעדים",)),
    ("resting_hr", ("דופק",)),
)

_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")
_SLEEP_RE = re.compile(r"\b(\d{1,2}):(\d{2})\s*[-–—]\s*(\d{1,2}):(\d{2})\b")

_WEEKDAY_WORDS = ("ראשון", "שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת")

_ALLERGY_RE = re.compile(r"(?:אלרגיה?|אלרגי(?:ת)?|רגישות)\s+ל([֐-׿][֐-׿\s]{1,30})")

_SESSION_MINUTES_RANGE = (10, 240)


@dataclass
class MultiFactResult:
    recognized: dict[str, Any] = field(default_factory=dict)
    ambiguous: list[str] = field(default_factory=list)
    weekday_text: str | None = None
    confidence: float = 1.0

    @property
    def fact_count(self) -> int:
        return len(self.recognized) + (1 if self.weekday_text else 0)


def parse_multi_fact_update(text: str) -> MultiFactResult:
    """Deterministic extraction: numbers bind to the nearest label."""
    result = MultiFactResult()
    working = (text or "").strip()
    if not working:
        return result

    sleep_match = _SLEEP_RE.search(working)
    if sleep_match:
        start_hour, start_minute, end_hour, end_minute = (
            int(part) for part in sleep_match.groups()
        )
        if 0 <= start_hour <= 23 and 0 <= end_hour <= 23:
            result.recognized["sleep_schedule"] = {
                "typical_bedtime": f"{start_hour:02d}:{start_minute:02d}",
                "typical_wake_time": f"{end_hour:02d}:{end_minute:02d}",
            }
        working = working.replace(sleep_match.group(0), " ")

    allergy_match = _ALLERGY_RE.search(working)
    if allergy_match:
        result.recognized["allergies"] = allergy_match.group(1).strip()
        working = working.replace(allergy_match.group(0), " ")

    weekdays = [day for day in _WEEKDAY_WORDS if re.search(rf"(?:^|\s){day}(?:\s|,|$)", working)]
    if len(weekdays) >= 2:
        result.weekday_text = ", ".join(weekdays)

    # Label positions.
    label_hits: list[tuple[int, int, str]] = []  # (start, end, fact_key)
    taken: list[tuple[int, int]] = []
    for fact_key, labels in _NUMERIC_LABELS:
        for label in labels:
            for match in re.finditer(re.escape(label), working):
                span = (match.start(), match.end())
                if any(s < span[1] and span[0] < e for s, e in taken):
                    continue  # a longer label already claimed this text
                label_hits.append((match.start(), match.end(), fact_key))
                taken.append(span)
                break
            else:
                continue
            break

    numbers = [
        (match.start(), match.end(), match.group(0).replace(",", "."))
        for match in _NUMBER_RE.finditer(working)
    ]
    used_numbers: set[int] = set()
    for label_start, label_end, fact_key in sorted(label_hits):
        if fact_key in result.recognized:
            continue
        best_index: int | None = None
        best_distance = 10**9
        for index, (num_start, num_end, _value) in enumerate(numbers):
            if index in used_numbers:
                continue
            # Distance from the label to the number, either side.
            distance = min(abs(num_start - label_end), abs(label_start - num_end))
            if distance < best_distance:
                best_distance = distance
                best_index = index
        if best_index is None or best_distance > 12:
            result.ambiguous.append(working[label_start:label_end])
            continue
        used_numbers.add(best_index)
        result.recognized[fact_key] = float(numbers[best_index][2])

    for index, (num_start, num_end, value) in enumerate(numbers):
        if index not in used_numbers:
            context = working[max(0, num_start - 12):min(len(working), num_end + 12)].strip()
            result.ambiguous.append(context or value)
    return result


async def apply_multi_fact_update(
    db: Any, user_id: int, result: MultiFactResult
) -> tuple[list[str], list[str]]:
    """Persist recognized facts via the canonical validators/writers.

    Returns (saved_summaries, rejected_summaries). Ambiguous fragments are
    not touched here — the caller asks about them.
    """
    import user_model
    from noam_coach.bot import onboarding as onboarding_bot

    saved: list[str] = []
    rejected: list[str] = []
    for fact_key, value in result.recognized.items():
        if fact_key == "sleep_schedule":
            await user_model.set_fact(
                db, user_id, "sleep_schedule", value,
                kind=user_model.KIND_FACT, source=user_model.SOURCE_USER,
                confirmed=True,
            )
            saved.append(
                f"שינה {value['typical_bedtime']}–{value['typical_wake_time']}"
            )
            continue
        if fact_key == "allergies":
            existing = await user_model.get_value(db, user_id, "allergies")
            parts = [
                p.strip() for p in str(existing or "").split(",")
                if p.strip() and p.strip() != "none"
            ]
            if value not in parts:
                parts.append(str(value))
            await user_model.set_fact(
                db, user_id, "allergies", ", ".join(parts),
                kind=user_model.KIND_FACT, source=user_model.SOURCE_USER,
                confirmed=True, affects=("menu_planning", "safety"),
            )
            saved.append(f"אלרגיה: {value}")
            continue
        if fact_key == "training_days_per_week":
            frequency = int(value)
            if not 1 <= frequency <= 7:
                rejected.append("מספר אימונים בשבוע צריך להיות בין 1 ל-7")
                continue
            await user_model.set_fact(
                db, user_id, "training_days_per_week", frequency,
                kind=user_model.KIND_FACT, source=user_model.SOURCE_USER,
                confirmed=True,
            )
            # Mirror the Health wizard's frequency step: keep the learned
            # workout pattern in sync so confirmation screens agree.
            pattern_fact = await user_model.get_fact(db, user_id, "workout_pattern")
            if pattern_fact and isinstance(pattern_fact.get("value"), dict):
                updated = {**pattern_fact["value"], "weekly_frequency": float(frequency)}
                await user_model.set_fact(
                    db, user_id, "workout_pattern", updated,
                    kind=pattern_fact.get("kind") or user_model.KIND_ESTIMATE,
                    source=pattern_fact.get("source") or user_model.SOURCE_DERIVED,
                )
            saved.append(f"{frequency} אימונים בשבוע")
            continue
        if fact_key == "session_minutes":
            minutes = int(value)
            low, high = _SESSION_MINUTES_RANGE
            if not low <= minutes <= high:
                rejected.append(f"משך אימון צריך להיות בין {low} ל-{high} דקות")
                continue
            await user_model.set_fact(
                db, user_id, "session_minutes", minutes,
                kind=user_model.KIND_FACT, source=user_model.SOURCE_USER,
                confirmed=True,
            )
            saved.append(f"משך אימון {minutes} דקות")
            continue
        # Plain numeric facts: reuse the protected validator (same ranges a
        # single-fact edit enforces).
        ok, parsed_value, error = onboarding_bot._parse_health_fact_text_edit(
            fact_key, f"{value:g}"
        )
        if not ok:
            rejected.append(error or f"ערך לא תקין עבור {fact_key}")
            continue
        await user_model.set_fact(
            db, user_id, fact_key, parsed_value,
            kind=user_model.KIND_FACT, source=user_model.SOURCE_USER,
            confirmed=True,
        )
        saved.append(
            f"{user_model.display_label(fact_key)} {parsed_value:g}"
            if isinstance(parsed_value, (int, float))
            else f"{user_model.display_label(fact_key)} {parsed_value}"
        )

    if result.weekday_text:
        from noam_coach.services.availability import (
            parse_hebrew_availability_answer,
            save_user_training_availability,
        )

        parsed = parse_hebrew_availability_answer(result.weekday_text)
        if parsed.weekly_availability:
            await save_user_training_availability(db, user_id, parsed)
            saved.append(f"ימי אימון: {result.weekday_text}")
    return saved, rejected


async def _emit(db: Any, user_id: int, outcome: str, **props: Any) -> None:
    try:
        from noam_coach.observability import taxonomy
        from noam_coach.observability.emit import emit_event

        await emit_event(
            db, user_id, taxonomy.DECISION_FINALIZED,
            entity="multi_fact_extraction", source="multi_fact",
            status="finalized", outcome=outcome, properties=props or {},
        )
    except Exception:  # noqa: BLE001
        pass


_original_text_handler: Any = None


def install_multi_fact_updates() -> None:
    """Wrap handle_onboarding_text with the multi-fact extraction path."""
    global _original_text_handler
    if _original_text_handler is not None:
        return
    import coach_bot

    _original_text_handler = coach_bot.handle_onboarding_text
    original = _original_text_handler

    async def multi_fact_handle_onboarding_text(update: Any, user_id: int) -> bool:
        import coach_bot as facade
        import conversation

        db = facade.DB
        flow = await conversation.expire_if_needed(db, user_id)
        pending = (
            flow.step
            if (flow.is_question or flow.name == conversation.FlowName.routine_confirm)
            else None
        )
        message = update.effective_message
        text = (getattr(message, "text", None) or "").strip()
        if (
            not pending
            or not text
            or any(pending.startswith(prefix) for prefix in _EXCLUDED_PENDINGS)
        ):
            return await original(update, user_id)

        result = parse_multi_fact_update(text)
        if result.fact_count < 2:
            return await original(update, user_id)

        import questions as questions_module

        question = questions_module.question_by_id(pending)
        active_fact_key = getattr(question, "fact_key", None)
        active_answer = (
            result.recognized.pop(active_fact_key)
            if active_fact_key and active_fact_key in result.recognized
            else None
        )

        saved, rejected = await apply_multi_fact_update(db, user_id, result)
        await _emit(
            db, user_id, "applied",
            saved=saved, rejected=rejected,
            ambiguous=result.ambiguous[:5],
            pending=pending, active_answer=active_answer is not None,
        )

        lines: list[str] = []
        if saved:
            lines.append("עדכנתי: " + " · ".join(saved) + " ✅")
        if rejected:
            lines.extend(rejected)
        if result.ambiguous:
            lines.append(
                "לא הייתי בטוח לגבי: "
                + ", ".join(result.ambiguous[:3])
                + ' — אפשר לכתוב למשל "גובה 174".'
            )
        if lines:
            await message.reply_text("\n".join(lines))

        if active_answer is not None:
            # The active question completes through its own canonical path.
            message.text = f"{active_answer:g}" if isinstance(active_answer, float) else str(active_answer)
            return await original(update, user_id)

        if pending == "__basics_fix__":
            from noam_coach.bot import onboarding as onboarding_bot

            await facade.clear_pending(user_id)
            await onboarding_bot.show_onboarding_patterns(message, user_id)
            return True
        if pending.startswith("__health_edit_") and pending.endswith("__"):
            from noam_coach.services.health_jobs import (
                ask_next_health_confirm_step,
                finish_health_confirm_wizard,
            )

            await facade.clear_pending(user_id)
            if not await ask_next_health_confirm_step(message, user_id):
                await finish_health_confirm_wizard(message, user_id)
            return True
        # A question is still open: keep it pending and restate it briefly.
        if question is not None:
            await message.reply_text(f"נשאר רק לענות: {question.text}")
        return True

    coach_bot.handle_onboarding_text = multi_fact_handle_onboarding_text


def uninstall_multi_fact_updates() -> None:
    global _original_text_handler
    if _original_text_handler is not None:
        import coach_bot

        coach_bot.handle_onboarding_text = _original_text_handler
        _original_text_handler = None
