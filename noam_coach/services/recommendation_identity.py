"""Render-level identity gate for next-meal option controls (B5 / ARCH-06).

TASK-03 generation exposes exactly ONE top-ranked option and re-ranks on
every regeneration, so a next-meal control that resolves its option index
against a FRESH regeneration can act on a different meal than the one the
user is looking at. The quantity fix (8233abe) established the correct
pattern — resolve against the ACTIVE stored recommendation; this module
extends the policy to the whole control family and adds the render-level
stale rule:

1. NO ACTIVE RECOMMENDATION → the control belongs to a card that expired or
   was superseded: REFUSE with a friendly refresh offer (never silently act
   on a regenerated meal). ``nextmeal:save``/``choose`` keep their own
   existing FIX-51 refusals for this case.
2. ACTIVE CARD MISMATCH → the active state remembers which Telegram message
   carries the live card (``message_id`` recorded by
   ``remember_active_recommendation``). A control pressed on a DIFFERENT
   message is a control from an older card whose displayed option may not
   match the active list: REFUSE instead of applying the action to the
   newer recommendation. When the active state has no message id (older
   rows, test doubles), the gate stays permissive — an unprovable mismatch
   is not treated as proof of staleness.
3. Otherwise delegate to the real handler, whose option resolution is
   anchored to the active options (service-level fix in ``next_meal``).

Refusals emit canonical ``validation.failed``
(entity=recommendation_control, outcome=stale) so the journey is
reconstructable from the trace.
"""

from __future__ import annotations

from typing import Any

_GATED_PREFIXES = (
    "nextmeal:dislike:",
    "nextmeal:dislikeitem:",
    "nextmeal:nostock:",
    "nextmeal:smaller:",
    "nextmeal:bigger:",
    "nextmeal:editqty:",
    "nextmeal:qty:",
    "nextmeal:choose:",
    "nextmeal:save:",
)

# Prefixes whose handlers already refuse the NO-ACTIVE case with dedicated
# UX (FIX 51); the gate only adds the message-mismatch rule for them.
_SELF_GUARDED_NO_ACTIVE = (
    "nextmeal:choose:",
    "nextmeal:save:",
    "nextmeal:qty:",
)


async def _refuse(query: Any, user_id: int, data: str, reason: str) -> None:
    import coach_bot

    try:
        from noam_coach.observability import taxonomy
        from noam_coach.observability.emit import emit_event

        await emit_event(
            coach_bot.DB, user_id, taxonomy.VALIDATION_FAILED,
            entity="recommendation_control",
            source="recommendation_identity", status="failed", outcome="stale",
            properties={"callback_data": data[:64], "reason": reason},
        )
    except Exception:  # noqa: BLE001
        pass
    from telegram import InlineKeyboardMarkup

    from noam_coach.bot.ui import button

    await coach_bot.safe_edit(
        query,
        "הכפתור הזה שייך להמלצה קודמת שכבר לא פעילה — רענן כדי לקבל את ההמלצה הנוכחית.",
        InlineKeyboardMarkup([[button("🍽️ אפשרות חדשה", "nextmeal:refresh"), button("🏠 תפריט", "menu:home")]]),
    )


_original_handler: Any = None


def install_recommendation_identity_gate() -> None:
    """Wrap the menu-callback handler at the coach_bot facade. Idempotent;
    chains cleanly over other installed gates (wraps whatever is current)."""
    global _original_handler
    if _original_handler is not None:
        return
    import coach_bot

    _original_handler = coach_bot.handle_menu_callback
    original = _original_handler

    async def identity_gated_handle_menu_callback(query: Any, user_id: int, data: str) -> bool:
        if isinstance(data, str) and data.startswith(_GATED_PREFIXES):
            import coach_bot as facade
            from noam_coach.services.next_meal import get_active_recommendation_state

            db = facade.DB
            state = None
            try:
                state = await get_active_recommendation_state(db, user_id)
            except Exception:  # noqa: BLE001 — unprovable → permissive.
                state = None
            if state is None and not data.startswith(_SELF_GUARDED_NO_ACTIVE):
                await _refuse(query, user_id, data, reason="no_active_recommendation")
                return True
            if state is not None:
                stored_message_id = state.get("message_id")
                pressed_message_id = getattr(getattr(query, "message", None), "message_id", None)
                if (
                    stored_message_id is not None
                    and pressed_message_id is not None
                    and stored_message_id != pressed_message_id
                ):
                    await _refuse(query, user_id, data, reason="control_from_superseded_card")
                    return True
        return await original(query, user_id, data)

    coach_bot.handle_menu_callback = identity_gated_handle_menu_callback


def uninstall_recommendation_identity_gate() -> None:
    global _original_handler
    if _original_handler is not None:
        import coach_bot

        coach_bot.handle_menu_callback = _original_handler
        _original_handler = None
