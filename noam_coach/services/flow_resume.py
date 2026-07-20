"""Restart-safe flow resume for deferred-plan / plan-completion (B6 / ARCH-11).

The canonical mid-flow continuation state for "answer a few more questions,
then build the plan" lives in two persisted stores:

- ``active_flow`` — the authoritative current question flow (survives restart;
  ``load_pending_state`` rebuilds the in-memory caches from it), and
- ``conversation_state`` — the flow-scoped continuation payloads
  (``deferred_plan`` frequency + remaining key, ``plan_completion``,
  ``goal_wizard`` progress).

Two restart gaps, closed here without editing the protected modules:

1. ``load_pending_state`` deletes ``conversation_state`` rows for
   ``deferred_plan`` as if the key were legacy — but it is LIVE
   (``ask_deferred_for_plan`` writes it; ``advance_after_answer`` reads it),
   so every restart silently destroyed the deferred-plan continuation: the
   user's next answer was accepted and then the flow dead-ended instead of
   building the plan. The wrap snapshots those rows before the original
   startup routine runs and restores them afterwards.
2. Nothing OFFERED to continue. Mirroring the existing onboarding restart
   pattern (``resume_onboarding_after_restart``, rendered from the
   stale-callback path), the first menu interaction after a restart now
   renders a resume offer whose controls reference the persisted flow
   identity (``active_flow.flow_id``, carried in the callback per the B2
   grammar) — continue re-renders the pending step through
   ``advance_after_answer``, the single continuation entry point every
   answer path already uses.

Free-text answers never needed the offer: ``active_flow`` survives restart
and routes the next message into the question flow — with the
``deferred_plan`` rows now preserved, that path resumes end-to-end on its
own. The offer covers the interaction that is NOT the pending answer.
"""

from __future__ import annotations

from typing import Any

RESUMABLE_CONVERSATION_FLOWS = ("deferred_plan", "plan_completion", "goal_wizard")

# Users owed a one-time resume offer after the last restart (rebuilt by the
# load_pending_state wrap; process-local on purpose — the offer is about THIS
# process having just started).
_pending_resume_offers: set[int] = set()

_OFFER_TRIGGER_CALLBACKS = ("menu:home", "menu:menu")

_RESUME_FLOW_LABELS = {
    "deferred_plan": "השלמת שאלות לפני בניית תוכנית האימונים",
    "plan_completion": "השלמת הפרטים לתוכנית",
    "goal_wizard": "אשף היעדים",
}


async def _resumable_flows(db: Any, user_id: int) -> list[str]:
    rows = await db.fetch_all(
        "SELECT flow FROM conversation_state WHERE user_id=? AND flow IN (?, ?, ?)",
        (user_id, *RESUMABLE_CONVERSATION_FLOWS),
    )
    return [str(row["flow"]) for row in rows]


async def _render_resume_offer(query: Any, user_id: int, flows: list[str]) -> None:
    from telegram import InlineKeyboardMarkup

    import coach_bot
    import conversation
    import event_log
    from noam_coach.bot.ui import button

    active = await conversation.get_active_flow(coach_bot.DB, user_id)
    flow_token = f":f{active.flow_id}" if active.flow_id else ""
    label = _RESUME_FLOW_LABELS.get(flows[0], "התהליך שהתחלנו")
    await coach_bot.safe_edit(
        query,
        f"לפני האתחול היינו באמצע {label}. להמשיך מאיפה שעצרנו?",
        InlineKeyboardMarkup(
            [
                [button("▶️ המשך מאיפה שעצרנו", f"resume:continue{flow_token}")],
                [button("🏠 לא עכשיו, לתפריט", f"resume:dismiss{flow_token}")],
            ]
        ),
    )
    await event_log.append_event(
        coach_bot.DB, user_id, "restart_resume_offer_rendered",
        entity="flow", source="system",
        properties={"flows": flows, "flow_id": active.flow_id or None},
    )


_original_load_pending_state: Any = None
_original_menu_handler: Any = None


def install_restart_resume() -> None:
    """Idempotent install of both restart-resume wraps on the coach_bot facade."""
    global _original_load_pending_state, _original_menu_handler
    if _original_load_pending_state is not None:
        return
    import coach_bot

    _original_load_pending_state = coach_bot.load_pending_state
    original_load = _original_load_pending_state

    async def restart_safe_load_pending_state() -> None:
        import coach_bot as facade

        db = facade.DB
        # ARCH-11 fix 1: deferred_plan is live continuation state, not legacy —
        # snapshot it around the original's cleanup DELETE.
        preserved = await db.fetch_all(
            "SELECT user_id, flow, step, payload, updated_at FROM conversation_state "
            "WHERE flow='deferred_plan'"
        )
        await original_load()
        for row in preserved:
            await db.execute(
                """
                INSERT INTO conversation_state(user_id, flow, step, payload, updated_at)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(user_id, flow) DO UPDATE SET
                    step=excluded.step, payload=excluded.payload,
                    updated_at=excluded.updated_at
                """,
                (row["user_id"], row["flow"], row["step"], row["payload"], row["updated_at"]),
            )
        _pending_resume_offers.clear()
        rows = await db.fetch_all(
            "SELECT DISTINCT user_id FROM conversation_state WHERE flow IN (?, ?, ?)",
            RESUMABLE_CONVERSATION_FLOWS,
        )
        for row in rows:
            _pending_resume_offers.add(int(row["user_id"]))

    coach_bot.load_pending_state = restart_safe_load_pending_state

    _original_menu_handler = coach_bot.handle_menu_callback
    original_handler = _original_menu_handler

    async def resume_aware_handle_menu_callback(query: Any, user_id: int, data: str) -> bool:
        import coach_bot as facade

        if isinstance(data, str) and data.startswith("resume:"):
            import conversation

            db = facade.DB
            action = data.split(":", 2)[1] if ":" in data else ""
            callback_flow_id = conversation.extract_flow_id(data)
            active = await conversation.get_active_flow(db, user_id)
            _pending_resume_offers.discard(user_id)
            if (
                callback_flow_id is not None
                and active.flow_id
                and callback_flow_id != active.flow_id
            ):
                # A resume control from BEFORE an even newer flow change:
                # refuse instead of continuing the wrong flow.
                from noam_coach.observability import taxonomy
                from noam_coach.observability.emit import emit_event

                await emit_event(
                    db, user_id, taxonomy.VALIDATION_FAILED,
                    entity="flow_resume",
                    source="flow_resume", status="failed", outcome="stale",
                    properties={"callback_data": data[:64], "reason": "flow_id_mismatch"},
                )
                await facade.safe_edit(
                    query,
                    "הכפתור הזה שייך לשלב קודם — נמשיך מהמצב העדכני דרך התפריט.",
                    facade.home_keyboard(),
                )
                return True
            if action == "continue":
                import event_log
                from noam_coach.bot import onboarding as onboarding_bot

                await event_log.append_event(
                    db, user_id, "flow_resumed_after_restart",
                    entity="flow", source="user",
                    properties={"flow_id": active.flow_id or None},
                )
                await facade.safe_edit(query, "ממשיכים מאיפה שעצרנו ▶️", None)
                await onboarding_bot.advance_after_answer(query, user_id)
                return True
            # dismiss (or unknown action): keep the persisted state — answering
            # the pending question still works — and show home.
            return await original_handler(query, user_id, "menu:home")

        if (
            user_id in _pending_resume_offers
            and isinstance(data, str)
            and data in _OFFER_TRIGGER_CALLBACKS
        ):
            _pending_resume_offers.discard(user_id)  # one offer per restart
            flows = await _resumable_flows(facade.DB, user_id)
            if flows:
                await _render_resume_offer(query, user_id, flows)
                return True
        return await original_handler(query, user_id, data)

    coach_bot.handle_menu_callback = resume_aware_handle_menu_callback


def uninstall_restart_resume() -> None:
    global _original_load_pending_state, _original_menu_handler
    import coach_bot

    if _original_load_pending_state is not None:
        coach_bot.load_pending_state = _original_load_pending_state
        _original_load_pending_state = None
    if _original_menu_handler is not None:
        coach_bot.handle_menu_callback = _original_menu_handler
        _original_menu_handler = None
    _pending_resume_offers.clear()
