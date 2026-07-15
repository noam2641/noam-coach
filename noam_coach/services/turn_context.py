"""AssistantTurnContext v1 + deterministic reference resolution (B9 / ARCH-08+16).

Canonical pipeline for a free-text user turn::

    USER TURN
    → build_turn_context            (bounded, decision-grade sections only)
    → resolve_reference             (deterministic, high-confidence only)
    → dispatch through the CANONICAL handler for the resolved entity
      (the same gated callback paths a button press takes, so the B2
      confirmation gate and the B5 recommendation-identity gate validate
      the resolution for free)
    → otherwise: unresolved candidates are handed to the LLM classifier
      as structured context (never forcing a guess), via the contextvar
      the editable root ``assistant.classify_intent`` reads.

The context is deliberately NOT a universal state dump: five bounded
sections, each the minimum a reference resolver / coaching decision needs.

Emits ``context.built`` per turn and
``decision.finalized(entity=reference_resolution)`` for every deterministic
resolution, carrying the resolved entity identity (fingerprint / goal kind /
flow_id) so the final domain mutation is traceable to the same id.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

# --------------------------------------------------------------------------
# Bounded context
# --------------------------------------------------------------------------


@dataclass
class AssistantTurnContext:
    user_id: int
    text: str
    # canonical flow lifecycle (bounded snapshots, ActiveFlow.snapshot())
    active_flow: dict[str, Any] = field(default_factory=dict)
    suspended_flow: dict[str, Any] | None = None
    # pending confirmation entity (confirm_number payload: kind/value)
    pending_confirmation: dict[str, Any] | None = None
    # displayed recommendation identities (number/title/fingerprint) + card
    recommendation_options: list[dict[str, Any]] = field(default_factory=list)
    recommendation_message_id: int | None = None
    selected_option: int | None = None
    # resumable continuation stores present for this user
    resumable_stores: list[str] = field(default_factory=list)
    # decision-grade coaching memory (B10/ARCH-12 bounded snapshot)
    coaching_memory: dict[str, Any] = field(default_factory=dict)

    def bounded(self) -> dict[str, Any]:
        """The trace/prompt-safe projection (identities, never full payloads)."""
        return {
            "active_flow": {
                k: self.active_flow.get(k) for k in ("name", "step", "flow_id", "version")
            },
            "suspended_flow": (
                {k: self.suspended_flow.get(k) for k in ("name", "step", "flow_id")}
                if self.suspended_flow
                else None
            ),
            "pending_confirmation": (
                {
                    "kind": self.pending_confirmation.get("kind"),
                    "value": self.pending_confirmation.get("value"),
                    "control": self.pending_confirmation.get("control"),
                }
                if self.pending_confirmation
                else None
            ),
            "recommendation_options": [
                {
                    "number": option.get("number"),
                    "title": option.get("title"),
                    "fingerprint": option.get("fingerprint"),
                }
                for option in self.recommendation_options[:5]
            ],
            "selected_option": self.selected_option,
            "resumable_stores": list(self.resumable_stores),
            "coaching_memory": self.coaching_memory,
        }


async def _pending_confirm_control(db: Any, user_id: int, value: Any) -> str | None:
    """The confirm:<kind>:<value> control of the newest delivered render
    matching the pending confirmation value."""
    if value is None:
        return None
    import event_log
    from noam_coach.observability import taxonomy

    try:
        expected = float(value)
    except (TypeError, ValueError):
        return None
    prepared = await event_log.list_events(
        db, user_id, event=taxonomy.UI_RENDER_PREPARED, limit=100
    )
    for item in reversed(prepared):  # newest first
        for control in item.properties.get("controls") or []:
            data = str(control.get("callback_data") or "")
            if not data.startswith("confirm:") or data.startswith("confirm:cancel"):
                continue
            parts = data.split(":")
            if len(parts) != 3:
                continue
            try:
                if float(parts[2]) == expected:
                    return data
            except ValueError:
                continue
    return None


async def build_turn_context(db: Any, user_id: int, text: str) -> AssistantTurnContext:
    import conversation
    from noam_coach.services.flow_resume import RESUMABLE_CONVERSATION_FLOWS

    ctx = AssistantTurnContext(user_id=user_id, text=(text or "").strip())

    flow = await conversation.expire_if_needed(db, user_id)
    ctx.active_flow = flow.snapshot()
    ctx.suspended_flow = flow.suspended or None

    if flow.name == conversation.FlowName.confirm_number and flow.payload:
        # The confirm_number payload stores only value+text — the entity KIND
        # lives in the DISPLAYED confirm control. Recover it from the render
        # evidence (O3 registry): the newest delivered render whose confirm
        # control carries the pending value. No render evidence → the
        # confirmation stays an unresolved candidate (never guessed).
        pending = dict(flow.payload)
        control = await _pending_confirm_control(db, user_id, pending.get("value"))
        if control:
            pending["control"] = control
            pending["kind"] = control.split(":")[1]
        ctx.pending_confirmation = pending

    from noam_coach.services.next_meal import (
        get_active_recommendation_state,
        option_fingerprint,  # noqa: F401  (fingerprints already stored)
    )

    state = await get_active_recommendation_state(db, user_id)
    if state:
        titles = state.get("option_titles") or []
        fingerprints = state.get("options") or []
        ctx.recommendation_options = [
            {
                "number": index + 1,
                "title": str(title),
                "fingerprint": str(fingerprints[index]) if index < len(fingerprints) else None,
            }
            for index, title in enumerate(titles)
        ]
        ctx.recommendation_message_id = state.get("message_id")
        selected = state.get("selected_option")
        ctx.selected_option = int(selected) if selected else None

    rows = await db.fetch_all(
        "SELECT flow FROM conversation_state WHERE user_id=? AND flow IN (?, ?, ?)",
        (user_id, *RESUMABLE_CONVERSATION_FLOWS),
    )
    ctx.resumable_stores = [str(row["flow"]) for row in rows]

    from noam_coach.services.coaching_memory import coaching_memory_snapshot

    ctx.coaching_memory = await coaching_memory_snapshot(db, user_id)

    from noam_coach.observability import taxonomy
    from noam_coach.observability.emit import emit_event

    await emit_event(
        db, user_id, taxonomy.CONTEXT_BUILT,
        entity="assistant_turn", source="turn_context", status="built",
        properties={"sections": ctx.bounded(), "text_chars": len(ctx.text)},
    )
    return ctx


# --------------------------------------------------------------------------
# Deterministic reference resolution
# --------------------------------------------------------------------------


@dataclass
class ResolvedReference:
    kind: str            # confirmation | option_ordinal | save_selected | return_to_suspended
    entity: str          # goal | recommendation_option | flow
    entity_id: str       # kind/value, fingerprint, flow_id
    dispatch: str        # the canonical callback payload to dispatch
    decision: str = ""   # yes/no for confirmations


_YES_WORDS = ("כן", "בטח", "אישור", "מאשר", "סבבה", "אוקיי", "אוקי", "יאללה כן")
_NO_WORDS = ("לא", "בטל", "ביטול", "לא רוצה", "עזוב")

_ORDINALS = {
    "הראשון": 1, "הראשונה": 1, "אחד": 1, "1": 1,
    "השני": 2, "השנייה": 2, "השניה": 2, "שתיים": 2, "2": 2,
    "השלישי": 3, "השלישית": 3, "שלוש": 3, "3": 3,
}

_SAVE_PATTERNS = ("תשמור את זה", "שמור את זה", "תשמור לי את זה", "זה מתאים לי, תשמור")

# B10: the explicit food-identity confirmation phrase offered after a
# repeated-correction proposal ("קבע קוטג' 250 גרם").
_CONFIRM_IDENTITY_RE = re.compile(
    r"^(?:קבע|תקבע)\s+(?P<food>.+?)\s+(?P<grams>\d+(?:\.\d+)?)\s*(?:גרם|גר)'?$"
)
_RETURN_PATTERNS = ("תחזור", "חזור למה שהיינו", "תחזיר אותי", "נמשיך מאיפה שהיינו")


def _normalized(text: str) -> str:
    return re.sub(r"[!?.,]+$", "", (text or "").strip())


def resolve_reference(ctx: AssistantTurnContext) -> ResolvedReference | None:
    """High-confidence deterministic resolutions only — anything ambiguous
    returns None and flows to the LLM with the bounded candidates."""
    text = _normalized(ctx.text)

    if ctx.pending_confirmation:
        kind = str(ctx.pending_confirmation.get("kind") or "")
        control = str(ctx.pending_confirmation.get("control") or "")
        value = ctx.pending_confirmation.get("value")
        if value is not None:
            # "לא" is safe even without render evidence (cancel is always
            # admitted); "כן" requires the PROVEN displayed control.
            if text in _YES_WORDS and kind and control:
                return ResolvedReference(
                    kind="confirmation", entity=kind, entity_id=f"{kind}:{value}",
                    dispatch=control, decision="yes",
                )
            if text in _NO_WORDS:
                return ResolvedReference(
                    kind="confirmation", entity=kind or "confirmation",
                    entity_id=f"{kind or 'pending'}:{value}",
                    dispatch="confirm:cancel:0", decision="no",
                )

    if ctx.recommendation_options:
        # "תשמור את זה" → the selected (or single displayed) option.
        if any(pattern in text for pattern in _SAVE_PATTERNS):
            number = ctx.selected_option or (
                1 if len(ctx.recommendation_options) == 1 else None
            )
            if number and number <= len(ctx.recommendation_options):
                option = ctx.recommendation_options[number - 1]
                return ResolvedReference(
                    kind="save_selected", entity="recommendation_option",
                    entity_id=str(option.get("fingerprint") or option.get("title")),
                    dispatch=f"nextmeal:save:{number}",
                )
        # bare ordinal/deixis → the N-th DISPLAYED option (never regenerated).
        ordinal = _ORDINALS.get(text)
        if ordinal and ordinal <= len(ctx.recommendation_options):
            option = ctx.recommendation_options[ordinal - 1]
            return ResolvedReference(
                kind="option_ordinal", entity="recommendation_option",
                entity_id=str(option.get("fingerprint") or option.get("title")),
                dispatch=f"nextmeal:choose:{ordinal}",
            )

    identity_match = _CONFIRM_IDENTITY_RE.match(text)
    if identity_match:
        return ResolvedReference(
            kind="confirm_food_identity", entity="coaching_memory",
            entity_id=identity_match.group("food").strip(),
            dispatch="memory_confirm:" + identity_match.group("grams"),
        )

    if ctx.suspended_flow and any(pattern in text for pattern in _RETURN_PATTERNS):
        return ResolvedReference(
            kind="return_to_suspended", entity="flow",
            entity_id=str(ctx.suspended_flow.get("flow_id") or ""),
            dispatch="resume:continue",
        )

    return None


class _CardQuery(SimpleNamespace):
    """Query shim: dispatching a resolved reference through the callback
    layer 'presses' the control on the card the reference points at — the
    stored active-card message_id — so the render-level identity gates
    validate the resolution instead of being bypassed. Renders go out as
    replies to the user's message."""

    def __init__(self, message: Any, card_message_id: int | None) -> None:
        super().__init__()
        self._reply_target = message
        self.message = SimpleNamespace(
            message_id=card_message_id
            if card_message_id is not None
            else getattr(message, "message_id", None),
            chat=getattr(message, "chat", None),
        )

    async def edit_message_text(
        self, text: str, reply_markup: Any = None, parse_mode: str | None = None
    ) -> None:
        await self._reply_target.reply_text(
            text, reply_markup=reply_markup, parse_mode=parse_mode or "HTML"
        )

    async def edit_message_reply_markup(self, reply_markup: Any = None) -> None:
        return None

    async def answer(self, *args: Any, **kwargs: Any) -> None:
        return None


async def dispatch_resolved_reference(
    update: Any, user_id: int, ctx: AssistantTurnContext, ref: ResolvedReference
) -> bool:
    """Route the resolved reference through its canonical handler."""
    import coach_bot as facade

    db = facade.DB
    from noam_coach.observability import taxonomy
    from noam_coach.observability.emit import emit_event

    await emit_event(
        db, user_id, taxonomy.DECISION_FINALIZED,
        entity="reference_resolution", entity_id=ref.entity_id,
        source="turn_context", status="finalized", outcome="deterministic",
        properties={
            "kind": ref.kind, "entity": ref.entity,
            "dispatch": ref.dispatch, "decision": ref.decision or None,
            "text": ctx.text[:64],
        },
    )
    message = update.effective_message

    if ref.kind == "confirm_food_identity":
        from noam_coach.services.coaching_memory import confirm_food_identity

        grams = float(ref.dispatch.split(":", 1)[1])
        confirmed = await confirm_food_identity(
            db, user_id, ref.entity_id, grams=grams,
            provenance="explicit_confirmation",
        )
        if confirmed is None:
            return False
        await message.reply_text(
            f"נרשם ✅ מעכשיו {ref.entity_id} אצלך זה {grams:g} גרם כברירת מחדל. "
            "אפשר לשנות בכל רגע באותה צורה."
        )
        return True

    if ref.kind == "return_to_suspended":
        import conversation

        restored = await conversation.resume_suspended(db, user_id)
        if restored is None:
            return False
        from noam_coach.bot import onboarding as onboarding_bot

        await onboarding_bot.advance_after_answer(message, user_id)
        return True

    query = _CardQuery(message, ctx.recommendation_message_id)
    handled = await facade.handle_menu_callback(query, user_id, ref.dispatch)
    return bool(handled)


# --------------------------------------------------------------------------
# Install: the turn pipeline in front of route_free_text
# --------------------------------------------------------------------------

_original_route_free_text: Any = None


def install_turn_context() -> None:
    """Wrap coach_bot.route_free_text with the canonical turn pipeline."""
    global _original_route_free_text
    if _original_route_free_text is not None:
        return
    import coach_bot

    _original_route_free_text = coach_bot.route_free_text
    original = _original_route_free_text

    async def turn_context_route_free_text(update: Any, user_id: int) -> None:
        import assistant as assistant_root
        import coach_bot as facade

        text = (getattr(update.effective_message, "text", None) or "").strip()
        if not text:
            return await original(update, user_id)
        db = facade.DB
        try:
            ctx = await build_turn_context(db, user_id, text)
        except Exception:  # noqa: BLE001 — never block the turn on context
            return await original(update, user_id)
        ref = resolve_reference(ctx)
        if ref is not None:
            if await dispatch_resolved_reference(update, user_id, ctx, ref):
                return None
        # Unresolved: hand the bounded candidates to the classifier instead
        # of letting it guess without them.
        token = assistant_root.REFERENCE_CANDIDATES.set(ctx.bounded())
        try:
            return await original(update, user_id)
        finally:
            assistant_root.REFERENCE_CANDIDATES.reset(token)

    coach_bot.route_free_text = turn_context_route_free_text


def uninstall_turn_context() -> None:
    global _original_route_free_text
    if _original_route_free_text is not None:
        import coach_bot

        coach_bot.route_free_text = _original_route_free_text
        _original_route_free_text = None
