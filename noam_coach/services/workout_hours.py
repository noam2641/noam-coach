"""Per-weekday workout-time evidence + outlier-day approval (TASK-60).

The Health confirmation wizard's hour step used to collapse all historical
workouts into one pooled typical hour — a Friday-morning routine silently
disappeared into the weekday-evening average. routine.learn_workout_pattern
now carries per-weekday circular means (``weekday_hours`` — a weekday needs
≥2 sessions to earn an average) and derives the DEFAULT hour from the mean
of the per-day means. This module turns that evidence into wizard UX:

- the hour-step prompt shows the per-day breakdown, the derived default,
  and which approved training days look materially different;
- confirming the default hour queues each outlier weekday for its OWN
  entity-addressed approval (callback carries weekday + proposed time),
  persisted in a conversation_state row so a restart resumes the pending
  approvals; approving stores the day-specific ``start`` on the canonical
  weekly_availability slot, declining keeps the default;
- a manual global-hour text correction skips the outlier questions
  entirely — the explicit user choice wins (no queue is created on the
  text-edit path).

Outlier rule (conservative, documented, tested): a weekday is surfaced
only when it has an average at all (≥2 sessions — one unusual workout is
not a pattern), and its circular clock distance from the default is at
least 90 minutes (the spec's materiality example; 23:30 vs 00:30 are 60
minutes apart, not 23 hours).
"""

from __future__ import annotations

from typing import Any

from noam_coach.services.weekdays import weekday_labels_he


def _day_label(weekday: int) -> str:
    labels = weekday_labels_he([weekday])
    return labels[0] if labels else str(weekday)

OUTLIER_THRESHOLD_MINUTES = 90
MIN_WEEKDAY_SAMPLES = 2

OUTLIER_FLOW = "workout_hour_outliers"

_ACCEPT_PREFIX = "hrout:accept:"   # hrout:accept:<weekday>:<HHMM>
_DEFAULT_PREFIX = "hrout:default:"  # hrout:default:<weekday>


def clock_distance_minutes(first_hhmm: str, second_hhmm: str) -> int:
    """Circular clock distance: 23:30 vs 00:30 → 60, not 1380."""
    def minutes(value: str) -> int:
        hour, minute = (int(part) for part in str(value).split(":"))
        return hour * 60 + minute

    delta = abs(minutes(first_hhmm) - minutes(second_hhmm)) % (24 * 60)
    return min(delta, 24 * 60 - delta)


def weekday_hour_evidence(value: dict[str, Any]) -> dict[int, str]:
    """{Mon-first weekday index: HH:MM} from a workout_pattern value."""
    evidence: dict[int, str] = {}
    raw = value.get("weekday_hours") or {}
    samples = value.get("weekday_hour_samples") or {}
    if not isinstance(raw, dict):
        return evidence
    for key, hhmm in raw.items():
        try:
            weekday = int(key)
        except (TypeError, ValueError):
            continue
        if int(samples.get(str(key), MIN_WEEKDAY_SAMPLES) or 0) < MIN_WEEKDAY_SAMPLES:
            continue
        if isinstance(hhmm, str) and ":" in hhmm:
            evidence[weekday] = hhmm
    return evidence


def outlier_days(
    value: dict[str, Any],
    approved_days: list[int] | None = None,
    *,
    threshold_minutes: int = OUTLIER_THRESHOLD_MINUTES,
) -> list[tuple[int, str]]:
    """Approved training weekdays whose average time is materially different
    from the default typical hour."""
    default = value.get("typical_hour")
    if not default:
        return []
    evidence = weekday_hour_evidence(value)
    outliers: list[tuple[int, str]] = []
    for weekday, hhmm in sorted(evidence.items()):
        if approved_days is not None and weekday not in approved_days:
            continue
        if clock_distance_minutes(hhmm, str(default)) >= threshold_minutes:
            outliers.append((weekday, hhmm))
    return outliers


def evidence_prompt_lines(value: dict[str, Any]) -> list[str]:
    """The per-day breakdown for the hour-step confirmation screen."""
    evidence = weekday_hour_evidence(value)
    if not evidence:
        return []
    lines = ["לפי היסטוריית האימונים:"]
    for weekday, hhmm in sorted(evidence.items()):
        lines.append(f"• {_day_label(weekday)}: סביב {hhmm}")
    return lines


async def _approved_training_days(db: Any, user_id: int) -> list[int] | None:
    import user_model

    days = await user_model.get_value(db, user_id, "active_training_days")
    if isinstance(days, list) and days:
        try:
            return [int(day) for day in days]
        except (TypeError, ValueError):
            return None
    return None


async def _queue_outliers(db: Any, user_id: int, outliers: list[tuple[int, str]]) -> None:
    from noam_coach.services import core as core_services

    await core_services.set_flow_state(
        user_id, OUTLIER_FLOW, "pending",
        {"pending": [[weekday, hhmm] for weekday, hhmm in outliers]},
    )


async def _pending_outliers(user_id: int) -> list[tuple[int, str]]:
    from noam_coach.services import core as core_services

    state = await core_services.get_flow_state(user_id, OUTLIER_FLOW)
    if not state:
        return []
    pending = (state.get("payload") or {}).get("pending") or []
    result: list[tuple[int, str]] = []
    for item in pending:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            result.append((int(item[0]), str(item[1])))
    return result


async def _pop_outlier(user_id: int, weekday: int) -> None:
    from noam_coach.services import core as core_services

    remaining = [item for item in await _pending_outliers(user_id) if item[0] != weekday]
    if remaining:
        await _queue_outliers(None, user_id, remaining)
    else:
        await core_services.clear_flow_state(user_id, OUTLIER_FLOW)


async def _apply_day_specific_start(db: Any, user_id: int, weekday: int, hhmm: str) -> None:
    """Persist the approved day-specific time on the canonical availability
    slot (no parallel schedule store)."""
    import user_model

    slots = await user_model.get_value(db, user_id, "weekly_availability") or []
    updated = False
    for slot in slots:
        if isinstance(slot, dict) and int(slot.get("weekday", -1)) == weekday:
            slot["start"] = hhmm
            updated = True
    if not updated:
        slots = [*slots, {"weekday": weekday, "start": hhmm, "available": True}]
    await user_model.set_fact(
        db, user_id, "weekly_availability", slots,
        kind=user_model.KIND_FACT, source=user_model.SOURCE_USER,
        confirmed=True,
    )


async def _emit(db: Any, user_id: int, outcome: str, **props: Any) -> None:
    try:
        from noam_coach.observability import taxonomy
        from noam_coach.observability.emit import emit_event

        await emit_event(
            db, user_id, taxonomy.DECISION_FINALIZED,
            entity="workout_hour_evidence", source="workout_hours",
            status="finalized", outcome=outcome, properties=props or {},
        )
    except Exception:  # noqa: BLE001
        pass


_original_prompt: Any = None
_original_confirm: Any = None
_original_ask: Any = None
_original_menu_handler: Any = None


def install_workout_hour_evidence() -> None:
    """Idempotent install of the four TASK-60 wraps."""
    global _original_prompt, _original_confirm, _original_ask, _original_menu_handler
    if _original_prompt is not None:
        return
    from noam_coach.services import health_jobs

    _original_prompt = health_jobs._wizard_step_prompt
    original_prompt = _original_prompt

    def evidence_wizard_step_prompt(step_id: str, fact: Any, quality: Any = None) -> Any:
        detected, scope, hint = original_prompt(step_id, fact, quality)
        if step_id == health_jobs.WIZARD_STEP_WORKOUT_HOUR:
            value = (fact or {}).get("value")
            if isinstance(value, dict):
                lines = evidence_prompt_lines(value)
                if lines:
                    outliers = outlier_days(value)
                    detected = "\n".join([
                        *lines,
                        f"אשתמש ב-{value.get('typical_hour')} כשעת האימון הרגילה"
                        " (ממוצע של ממוצעי הימים)",
                        *(
                            [
                                "יש ימים שנראים שונים מהשאר — אשאל עליהם בנפרד אחרי האישור: "
                                + ", ".join(
                                    f"{_day_label(weekday)} סביב {hhmm}"
                                    for weekday, hhmm in outliers
                                )
                            ]
                            if outliers
                            else []
                        ),
                    ])
        return detected, scope, hint

    health_jobs._wizard_step_prompt = evidence_wizard_step_prompt

    _original_confirm = health_jobs.confirm_health_wizard_step
    original_confirm = _original_confirm

    async def evidence_confirm_health_wizard_step(user_id: int, step_id: str) -> str:
        import coach_bot as facade

        ack = await original_confirm(user_id, step_id)
        if step_id == health_jobs.WIZARD_STEP_WORKOUT_HOUR:
            import user_model

            db = facade.DB
            fact = await user_model.get_fact(db, user_id, "workout_pattern")
            value = (fact or {}).get("value")
            if isinstance(value, dict):
                approved = await _approved_training_days(db, user_id)
                outliers = outlier_days(value, approved)
                if outliers:
                    await _queue_outliers(db, user_id, outliers)
                    await _emit(
                        db, user_id, "outliers_queued",
                        outliers=[[weekday, hhmm] for weekday, hhmm in outliers],
                        default=value.get("typical_hour"),
                    )
        return ack

    health_jobs.confirm_health_wizard_step = evidence_confirm_health_wizard_step

    _original_ask = health_jobs.ask_next_health_confirm_step
    original_ask = _original_ask

    async def outlier_aware_ask_next_health_confirm_step(
        target: Any, user_id: int, *, ack_text: Any = None
    ) -> bool:
        pending = await _pending_outliers(user_id)
        if pending:
            from telegram import InlineKeyboardMarkup

            import coach_bot as facade
            from noam_coach.bot.ui import button

            weekday, hhmm = pending[0]
            label = _day_label(weekday)
            compact = hhmm.replace(":", "")
            prefix = f"{ack_text}\n\n" if ack_text else ""
            text = (
                f"{prefix}{label} נראה שונה מהשאר לפי ההיסטוריה — "
                f"לקבוע לו אימון סביב {hhmm}?"
            )
            keyboard = InlineKeyboardMarkup([
                [button(f"✅ כן, {label} סביב {hhmm}", f"hrout:accept:{weekday}:{compact}")],
                [button("🕒 להשתמש בשעה הרגילה", f"hrout:default:{weekday}")],
            ])
            if hasattr(target, "edit_message_text"):
                await facade.safe_edit(target, text, keyboard)
            else:
                await target.reply_text(text, reply_markup=keyboard, parse_mode="HTML")
            return True
        return await original_ask(target, user_id, ack_text=ack_text)

    health_jobs.ask_next_health_confirm_step = outlier_aware_ask_next_health_confirm_step

    import coach_bot

    _original_menu_handler = coach_bot.handle_menu_callback
    original_menu = _original_menu_handler

    async def outlier_handle_menu_callback(query: Any, user_id: int, data: str) -> bool:
        if isinstance(data, str) and data.startswith(_ACCEPT_PREFIX):
            import coach_bot as facade

            parts = data.split(":")
            weekday = int(parts[2])
            compact = parts[3]
            hhmm = f"{compact[:2]}:{compact[2:]}"
            await _apply_day_specific_start(facade.DB, user_id, weekday, hhmm)
            await _pop_outlier(user_id, weekday)
            await _emit(
                facade.DB, user_id, "outlier_approved",
                weekday=weekday, start=hhmm,
            )
            ack = f"✅ נקבע: {_day_label(weekday)} סביב {hhmm}"
            if not await health_jobs.ask_next_health_confirm_step(query, user_id, ack_text=ack):
                await health_jobs.finish_health_confirm_wizard(query, user_id, ack_text=ack)
            return True
        if isinstance(data, str) and data.startswith(_DEFAULT_PREFIX):
            import coach_bot as facade

            weekday = int(data.split(":")[2])
            await _pop_outlier(user_id, weekday)
            await _emit(facade.DB, user_id, "outlier_declined", weekday=weekday)
            ack = f"בסדר — {_day_label(weekday)} יישאר בשעה הרגילה"
            if not await health_jobs.ask_next_health_confirm_step(query, user_id, ack_text=ack):
                await health_jobs.finish_health_confirm_wizard(query, user_id, ack_text=ack)
            return True
        return await original_menu(query, user_id, data)

    coach_bot.handle_menu_callback = outlier_handle_menu_callback


def uninstall_workout_hour_evidence() -> None:
    global _original_prompt, _original_confirm, _original_ask, _original_menu_handler
    from noam_coach.services import health_jobs

    if _original_prompt is not None:
        health_jobs._wizard_step_prompt = _original_prompt
        _original_prompt = None
    if _original_confirm is not None:
        health_jobs.confirm_health_wizard_step = _original_confirm
        _original_confirm = None
    if _original_ask is not None:
        health_jobs.ask_next_health_confirm_step = _original_ask
        _original_ask = None
    if _original_menu_handler is not None:
        import coach_bot

        coach_bot.handle_menu_callback = _original_menu_handler
        _original_menu_handler = None
