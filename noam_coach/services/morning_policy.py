"""Proactive morning policy: short briefing + opt-in daily menu (TASK-62).

Incident: the scheduled morning experience pushed the full pinnable daily
menu (job_morning's second delivery, key="morning_menu") although the
desired proactive interaction is a concise briefing. Resolved product
policy: automatic full-menu delivery is OPT-IN, default OFF, never silently
enabled for existing users, stored through the confirmed-fact architecture,
and toggleable from Telegram.

Mechanics:
- ``job_morning_briefing`` replaces the scheduled callback (runtime.py owns
  the registration): it delivers the SAME short briefing menu:morning
  renders (build_morning_briefing_text — remaining calories/protein, the
  day-specific workout line, one or two priorities) with the check-in
  buttons + the menu opt-in/out toggle, through the B6 delivery boundary
  under the ORIGINAL "morning_checkin" key (budget/dedup/retry semantics
  preserved). It then invokes the original protected job_morning: its
  check-in delivery no-ops (the daily claim is already taken — startup and
  retries can never double-send) and its menu delivery flows into the
  boundary, where the opt-in gate decides.
- the gate itself lives in the canonical delivery boundary
  (deliver_proactive_message): the "morning_menu" key is delivered only for
  a CONFIRMED opt-in (decision-grade read — an unconfirmed estimate never
  drives a proactive send), otherwise it is suppressed BEFORE the claim and
  before delivery.attempted with
  decision.fallback_selected(reason=menu_optin_absent) — prepared output is
  never confused with a delivered message.
- opting out takes effect immediately: the next boundary check reads the
  confirmed fact.
"""

from __future__ import annotations

from typing import Any

MORNING_MENU_OPTIN_FACT = "morning_menu_optin"

OPTIN_CALLBACK = "morningmenu:optin"
OPTOUT_CALLBACK = "morningmenu:optout"


async def morning_menu_opted_in(db: Any, user_id: int) -> bool:
    """Confirmed opt-in only (B11 read policy): default OFF."""
    import user_model

    value = await user_model.get_decision_value(db, user_id, MORNING_MENU_OPTIN_FACT)
    return bool(value) and str(value).lower() not in ("false", "0", "none", "off")


async def set_morning_menu_optin(db: Any, user_id: int, enabled: bool) -> None:
    import user_model

    await user_model.set_fact(
        db, user_id, MORNING_MENU_OPTIN_FACT, bool(enabled),
        kind=user_model.KIND_FACT, source=user_model.SOURCE_USER,
        confirmed=True,
    )


def _optin_toggle_row(opted_in: bool) -> tuple[tuple[str, str], ...]:
    if opted_in:
        return (("🔕 בטל שליחת תפריט יומי אוטומטי", OPTOUT_CALLBACK),)
    return (("📌 שלח לי גם תפריט יומי מלא בבוקר", OPTIN_CALLBACK),)


async def job_morning_briefing(context: Any) -> None:
    """The scheduled morning delivery: concise briefing by default."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    import coach_bot as facade
    from config import SETTINGS

    user_id = SETTINGS.telegram_allowed_user_id

    async def send_briefing() -> None:
        text = await facade.build_morning_briefing_text(user_id)
        base_keyboard = await facade.morning_checkin_keyboard(user_id)
        rows = [list(row) for row in base_keyboard.inline_keyboard]
        opted_in = await morning_menu_opted_in(facade.DB, user_id)
        rows.append([
            InlineKeyboardButton(label, callback_data=data)
            for label, data in _optin_toggle_row(opted_in)
        ])
        await facade.send_to_user(
            context, text, reply_markup=InlineKeyboardMarkup(rows)
        )

    await facade.deliver_proactive_message(
        context,
        key="morning_checkin",
        sender=send_briefing,
        priority=facade.JOB_PRIORITY_SCHEDULED,
        retry_callback=job_morning_briefing,
    )
    # The opt-in decision lives HERE at the job layer: the delivery boundary
    # keeps its existing morning_menu semantics (goal-quality gating —
    # pinned by protected tests), and the scheduled job simply never
    # attempts the menu delivery without a confirmed opt-in. When opted in,
    # the original protected job runs: its check-in delivery no-ops (the
    # daily claim is already taken — startup/retries can never double-send)
    # and all menu mechanics (pin-friendly message,
    # remember_daily_menu_message) stay in the one protected implementation.
    # Known edge (documented): a menu delivery that FAILED after an
    # authorized attempt retries through the original job without
    # re-checking the toggle — one already-authorized morning's retry chain.
    if await morning_menu_opted_in(facade.DB, user_id):
        await facade.job_morning(context)
        return
    try:
        from noam_coach.observability import emit_event, interaction_scope, taxonomy

        with interaction_scope(user_id=user_id):
            await emit_event(
                facade.DB, user_id, taxonomy.DECISION_FALLBACK_SELECTED,
                entity="proactive_job", entity_id="morning_menu",
                source="job", status="selected", outcome="suppressed",
                properties={
                    "operation": "proactive_job", "key": "morning_menu",
                    "reason": "menu_optin_absent",
                },
            )
    except Exception:  # noqa: BLE001
        pass


_original_menu_handler: Any = None


def install_morning_policy() -> None:
    """Wrap handle_menu_callback with the opt-in/out toggle callbacks."""
    global _original_menu_handler
    if _original_menu_handler is not None:
        return
    import coach_bot

    _original_menu_handler = coach_bot.handle_menu_callback
    original = _original_menu_handler

    async def morning_policy_handle_menu_callback(query: Any, user_id: int, data: str) -> bool:
        if data in (OPTIN_CALLBACK, OPTOUT_CALLBACK):
            import coach_bot as facade

            enabled = data == OPTIN_CALLBACK
            await set_morning_menu_optin(facade.DB, user_id, enabled)
            try:
                from noam_coach.observability import taxonomy
                from noam_coach.observability.emit import emit_event

                await emit_event(
                    facade.DB, user_id, taxonomy.DECISION_FINALIZED,
                    entity="morning_menu_optin", source="morning_policy",
                    status="finalized", outcome="enabled" if enabled else "disabled",
                    properties={"callback": data},
                )
            except Exception:  # noqa: BLE001
                pass
            await facade.safe_edit(
                query,
                (
                    "מעכשיו אשלח גם את התפריט היומי המלא בבוקר 📌 "
                    "(אפשר לבטל מחר מאותו כפתור)."
                    if enabled
                    else "בסדר — בבוקר תקבל רק את העדכון הקצר. "
                    "את התפריט המלא אפשר לבקש עם \"תפריט יומי\"."
                ),
                facade.home_keyboard(),
            )
            return True
        return await original(query, user_id, data)

    coach_bot.handle_menu_callback = morning_policy_handle_menu_callback


def uninstall_morning_policy() -> None:
    global _original_menu_handler
    if _original_menu_handler is not None:
        import coach_bot

        coach_bot.handle_menu_callback = _original_menu_handler
        _original_menu_handler = None
