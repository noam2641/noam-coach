"""Canonical flow convergence for the goal wizard (B7 / ARCH-01 phase 1).

Canonical ownership (the phase-1 contract):

- ``active_flow`` owns routing, lifecycle, continuation identity (flow_id,
  version, expiry, suspension snapshots). O7 already traces its transitions
  (flow.started/updated/suspended/resumed/expired/completed) with bounded
  ``ActiveFlow.snapshot()`` payloads.
- Flow-scoped ``conversation_state`` rows (``goal_wizard``,
  ``plan_completion``, ``deferred_plan``) are SCRATCHPAD/domain payload.
  They must never make a flow look alive on their own: their liveness is
  derived from the active_flow lifecycle, which removes dual-write
  synchronization as a correctness requirement — a scratchpad row that
  outlives its flow is cleaned up (expiry) or refused (late answers),
  never silently continued.

Three enforcement points, installed without editing protected modules:

1. ``conversation.expire_if_needed`` wrap: when the active flow actually
   expires, the goal-wizard scratchpad row is deleted in the same step —
   expiry never leaves a wizard row that still appears live. (Answered
   wizard facts are already saved as durable user facts, so nothing
   meaningful is lost; deferred_plan/plan_completion intentionally survive
   — they are the ARCH-11 restart-resume continuations.)
2. ``coach_bot.handle_onboarding_callback`` wrap (``qa:*`` answers — the
   wizard's own buttons carry no version/flow token, so the router's stale
   check does not protect them): a late answer whose goal-wizard row lost
   its owning flow is an ORPHAN — the row is cleared and the tap is
   refused with a fresh-start offer instead of continuing a dead wizard.
   If the wizard was suspended to Home, the tap is an explicit continue:
   the suspended flow is resumed first and the answer proceeds.
3. ``coach_bot.handle_menu_callback`` wrap (Home policy — the resolved
   product decision): Home with a meaningful multi-step goal/plan flow in
   progress no longer destroys it (the protected handler calls
   clear_all_flows). The flow is SUSPENDED into the idle row's snapshot
   (explicitly resumable, no invisible continuation: free text now routes
   as idle), and the home screen renders with a resume control that
   references the persisted flow identity. Trivial single-question flows
   keep the old exit-and-clear behavior — no added friction.

Trace note: the Home suspension is written through
``set_active_flow(idle, suspend_current=True)``, which O7 records as
flow.completed; this module emits the semantically correct flow.suspended
(reason=home_exit) alongside it, so the journey reads
suspended → (transition) rather than a silent completion.
"""

from __future__ import annotations

from typing import Any

from noam_coach.services.flow_resume import RESUMABLE_CONVERSATION_FLOWS

_WIZARD_SCRATCHPAD_FLOW = "goal_wizard"

# B8/ARCH-10 store policies (the explicit per-store contract):
# - FLOW-SCOPED scratchpads live and die with their owning flow: suspended
#   with it (Home on meaningful work), cleared when it expires, invalidated
#   by terminal cancel and by Home when trivial/orphaned.
# - RESUMABLE continuations (ARCH-11) survive Home and restart; only a
#   terminal cancel invalidates them.
# - health_confirm owns its own step lifecycle (done/deferred payload) and
#   is preserved except on terminal cancel.
_FLOW_SCOPED_SCRATCHPADS = ("goal_wizard", "profile_field_edit")
_ALL_CONTINUATION_STORES = (
    "goal_wizard",
    "profile_field_edit",
    "deferred_plan",
    "plan_completion",
    "health_confirm",
)

_HOME_CALLBACKS = ("menu:home", "menu:more")


async def _invalidate_store(db: Any, user_id: int, store: str, reason: str) -> bool:
    """Clear one continuation store row, tracing the invalidation. Returns
    True when a row actually existed."""
    from noam_coach.services import core as core_services

    state = await core_services.get_flow_state(user_id, store)
    if state is None:
        return False
    await core_services.clear_flow_state(user_id, store)
    from noam_coach.observability import taxonomy
    from noam_coach.observability.emit import emit_event

    await emit_event(
        db, user_id, taxonomy.STATE_MUTATED,
        entity="conversation_state", entity_id=store,
        source="flow_convergence", status="mutated", outcome="invalidated",
        properties={"reason": reason, "step": str(state.get("step") or "")},
    )
    return True


async def terminal_cancel_command(update: Any, context: Any) -> None:
    """/cancel — the terminal escape hatch (B8/ARCH-10).

    Runs the protected command_cancel (pending + meal-fix + active_flow)
    and then invalidates EVERY continuation store, so no orphan row remains
    actionable after a terminal cancellation. Registered in runtime.py in
    place of the bare command_cancel.
    """
    import coach_bot as facade

    await facade.command_cancel(update, context)
    user = getattr(update, "effective_user", None)
    user_id = getattr(user, "id", None)
    if user_id is None or not await facade.is_allowed(update):
        return
    db = facade.DB
    for store in _ALL_CONTINUATION_STORES:
        await _invalidate_store(db, user_id, store, "user_cancel")
    from noam_coach.services import flow_resume as flow_resume_module

    flow_resume_module._pending_resume_offers.discard(user_id)


async def _scratchpad_flows(db: Any, user_id: int) -> list[str]:
    rows = await db.fetch_all(
        "SELECT flow FROM conversation_state WHERE user_id=? AND flow IN (?, ?, ?)",
        (user_id, *RESUMABLE_CONVERSATION_FLOWS),
    )
    return [str(row["flow"]) for row in rows]


_original_expire: Any = None
_original_onboarding_handler: Any = None
_original_menu_handler: Any = None


def install_flow_convergence() -> None:
    """Idempotent install of the three convergence wraps."""
    global _original_expire, _original_onboarding_handler, _original_menu_handler
    if _original_expire is not None:
        return
    import conversation

    _original_expire = conversation.expire_if_needed
    original_expire = _original_expire

    async def convergent_expire_if_needed(db: Any, user_id: int) -> Any:
        before = await conversation.get_active_flow(db, user_id)
        result = await original_expire(db, user_id)
        if before.is_expired and not before.is_idle:
            # The flow just expired: flow-scoped scratchpads must not
            # outlive it (goal wizard, single-field profile edit).
            for store in _FLOW_SCOPED_SCRATCHPADS:
                await _invalidate_store(db, user_id, store, "flow_expired")
        return result

    conversation.expire_if_needed = convergent_expire_if_needed

    import coach_bot

    _original_onboarding_handler = coach_bot.handle_onboarding_callback
    original_onboarding = _original_onboarding_handler

    async def convergent_handle_onboarding_callback(
        query: Any, user_id: int, data: str
    ) -> Any:
        if isinstance(data, str) and data.startswith("qa:"):
            import coach_bot as facade
            import conversation as conversation_module

            db = facade.DB
            flow = await conversation_module.expire_if_needed(db, user_id)
            from noam_coach.services import core as core_services

            wizard_row = await core_services.get_flow_state(
                user_id, _WIZARD_SCRATCHPAD_FLOW
            )
            if flow.is_idle and not flow.suspended:
                # An orphaned single-field edit row must not hijack the
                # answer chain: clear it silently (text-driven flow — this
                # qa answer was never its control).
                await _invalidate_store(
                    db, user_id, "profile_field_edit", "orphan_cleared"
                )
            if wizard_row is not None and flow.is_idle:
                if flow.suspended:
                    # Suspended to Home: tapping the wizard's own answer button
                    # is an explicit continue — restore the flow, then answer.
                    await conversation_module.resume_suspended(db, user_id)
                else:
                    # Idle with a leftover wizard row: an orphan (a clear path
                    # that bypassed the expiry cleanup). Refuse the late answer
                    # and clear the row so the wizard never continues from a
                    # dead flow.
                    await core_services.clear_flow_state(
                        user_id, _WIZARD_SCRATCHPAD_FLOW
                    )
                    from noam_coach.observability import taxonomy
                    from noam_coach.observability.emit import emit_event

                    await emit_event(
                        db, user_id, taxonomy.VALIDATION_FAILED,
                        entity="goal_wizard",
                        source="flow_convergence", status="failed",
                        outcome="stale",
                        properties={
                            "callback_data": data[:64],
                            "reason": "orphan_wizard_answer",
                        },
                    )
                    from telegram import InlineKeyboardMarkup

                    from noam_coach.bot.ui import button

                    await facade.safe_edit(
                        query,
                        "השאלה הזו שייכת לתהליך יעדים שכבר הסתיים או פג. "
                        "אפשר להתחיל שוב — התשובות שכבר נתת נשמרו.",
                        InlineKeyboardMarkup(
                            [
                                [button("🎯 המשך בהגדרת היעד", "menu:goal")],
                                [button("🏠 תפריט", "menu:home")],
                            ]
                        ),
                    )
                    return None
        return await original_onboarding(query, user_id, data)

    coach_bot.handle_onboarding_callback = convergent_handle_onboarding_callback

    _original_menu_handler = coach_bot.handle_menu_callback
    original_menu = _original_menu_handler

    async def home_preserving_handle_menu_callback(
        query: Any, user_id: int, data: str
    ) -> bool:
        if isinstance(data, str) and data in _HOME_CALLBACKS:
            import coach_bot as facade
            import conversation as conversation_module

            db = facade.DB
            flow = await conversation_module.expire_if_needed(db, user_id)
            if not flow.is_idle:
                flows = await _scratchpad_flows(db, user_id)
                if flows:
                    # Meaningful multi-step goal/plan work in progress:
                    # suspend (explicitly resumable), never silently delete.
                    from noam_coach.observability import taxonomy
                    from noam_coach.observability.emit import emit_event

                    await emit_event(
                        db, user_id, taxonomy.FLOW_SUSPENDED,
                        entity="flow", entity_id=flow.flow_id or None,
                        flow_id=flow.flow_id or None,
                        flow_version=flow.version,
                        source="flow_convergence", status="suspended",
                        properties={
                            "reason": "home_exit",
                            "flow": flow.snapshot(),
                            "scratchpad_flows": flows,
                        },
                    )
                    resume_flow_id = flow.flow_id
                    await conversation_module.set_active_flow(
                        db, user_id, conversation_module.FlowName.idle,
                        suspend_current=True,
                    )
                    facade.PENDING_QUESTION.pop(user_id, None)
                    facade.CONFIRM_PENDING.pop(user_id, None)
                    from telegram import InlineKeyboardMarkup

                    from noam_coach.bot.ui import button

                    token = f":f{resume_flow_id}" if resume_flow_id else ""
                    base_keyboard = await facade.home_keyboard_for_user(user_id)
                    keyboard = InlineKeyboardMarkup(
                        [
                            [button("▶️ להמשיך מאיפה שעצרנו", f"resume:continue{token}")],
                            *base_keyboard.inline_keyboard,
                        ]
                    )
                    hint = await facade._home_hint(user_id)
                    await facade.safe_edit(
                        query,
                        "<b>המאמן האישי שלך</b>\n\n"
                        "יש תהליך פתוח ששמרתי — אפשר להמשיך אותו מתי שתרצה.\n\n"
                        + hint,
                        keyboard,
                    )
                    return True
        if isinstance(data, str) and data in _HOME_CALLBACKS:
            # Delegating to the protected Home (which clears active_flow):
            # flow-scoped scratchpads must not stay alive behind the cleared
            # flow (trivial single-field edits, orphaned wizard rows).
            # Resumable continuations (deferred_plan/plan_completion) and
            # health_confirm are preserved by policy.
            import coach_bot as facade

            for store in _FLOW_SCOPED_SCRATCHPADS:
                await _invalidate_store(facade.DB, user_id, store, "home_exit")
        return await original_menu(query, user_id, data)

    coach_bot.handle_menu_callback = home_preserving_handle_menu_callback


def uninstall_flow_convergence() -> None:
    global _original_expire, _original_onboarding_handler, _original_menu_handler
    if _original_expire is not None:
        import conversation

        conversation.expire_if_needed = _original_expire
        _original_expire = None
    import coach_bot

    if _original_onboarding_handler is not None:
        coach_bot.handle_onboarding_callback = _original_onboarding_handler
        _original_onboarding_handler = None
    if _original_menu_handler is not None:
        coach_bot.handle_menu_callback = _original_menu_handler
        _original_menu_handler = None
