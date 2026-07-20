"""Visible, trace-evident refusal of control presses (review 2026-07-18_1).

F-02/F-03: two production presses (a duplicate ✅ שמור and a stale wizard
button — events 590/601 and 267/275) produced NOTHING the user or the
trace could see: the refusals were correct (idempotency preserved) but
silent, indistinguishable from a hang.

This module is the single refusal boundary:

- every intentional refusal emits canonical
  ``decision.finalized(entity=ui_control, outcome=refused)`` with a
  machine-readable reason — a refused press can never again read as a
  hang in a session review;
- a short toast acknowledges the press whenever the caller provides one
  (the query must not have been answered yet for Telegram to display it);
- the callback payload appears in properties as a digest, never raw
  (committable-signal policy).

Toast delivery itself is instrumented in ``noam_coach.bot.ui.
safe_answer_callback`` (operation=callback_ack), so acknowledgements are
also distinguishable from delivery failures.
"""

from __future__ import annotations

import hashlib
from typing import Any

from noam_coach.observability import taxonomy
from noam_coach.observability.emit import emit_event

REFUSAL_REASONS = (
    "duplicate_tap",
    "stale_version",
    "approval_already_handled",
    "wizard_already_finished",
    # workout-selection architecture, Batch 4: a `wk:` callback whose carried
    # identity no longer matches the user's live plan/fact -- the plan was
    # regenerated (Tier-1 plan_id mismatch) or the weekly fact was replaced
    # (Tier-2 fact_rev mismatch). Content-addressed, deliberately NOT the
    # router's `:v` version-token mechanism (that one is for conversation
    # flows). See the architecture plan, section F.
    "stale_plan_reference",
)


def _digest8(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:8]


async def refuse_control(
    query: Any,
    user_id: int,
    *,
    reason: str,
    toast: str | None = None,
    show_alert: bool = False,
    source: str = "callback_router",
    extra: dict[str, Any] | None = None,
) -> None:
    """Record one intentional control refusal; optionally acknowledge it.

    Never raises — a refusal path must not replace a correct no-op with a
    crash — and never mutates product state (idempotency is the caller's
    contract; this only makes the refusal visible).
    """
    import coach_bot

    data = str(getattr(query, "data", "") or "")
    properties: dict[str, Any] = {
        "reason": reason,
        "callback_digest": _digest8(data),
        "callback_prefix": data.split(":", 1)[0][:32],
        "acknowledged": bool(toast),
    }
    if extra:
        properties.update(extra)
    try:
        await emit_event(
            coach_bot.DB,
            user_id,
            taxonomy.DECISION_FINALIZED,
            entity="ui_control",
            source=source,
            status="refused",
            outcome="refused",
            properties=properties,
        )
    except Exception:  # noqa: BLE001 — evidence must not break the refusal.
        pass
    if toast:
        try:
            from noam_coach.bot.ui import safe_answer_callback

            await safe_answer_callback(query, toast, show_alert=show_alert)
        except Exception:  # noqa: BLE001
            pass
