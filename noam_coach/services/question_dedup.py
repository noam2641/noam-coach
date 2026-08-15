"""Plan-completion question deduplication + classification continuation (TASK-63).

Incident: answering the allergies/sensitivities question with "אגוזים"
re-rendered the same question. Root causes in the protected answer path:

1. The free-text dietary branch asks the "איך להתייחס ל..." classification
   WITHOUT persisting the answer first and WITHOUT any pending state: the
   classification card is a bare inline keyboard. A typed reply to it goes
   to the generic assistant (lost), a restart forgets it entirely, and a
   non-allergy classification outcome loses the item (it was never stored).
   In all of those shapes the target fact stays missing — so the wizard's
   next render is the SAME top-level question.
2. "אין אלרגיות" and similar no-answers parse as a food ITEM, producing an
   absurd classification question about "אין אלרגיות".

The single invariant this module enforces (installed as wraps; the
protected onboarding module is untouched):

    after a successful answer save, the next rendered question must not
    have the same fact_key unless a structured clarification for that same
    answer is still unresolved — and that clarification itself must be a
    real, resumable pending question.

Mechanics:
- no-answers ("אין", "אין אלרגיות", "שום דבר", …) resolve the fact to
  "none" immediately and advance — no classification detour.
- when the dietary parse yields items (classification will be asked), the
  answer is persisted FIRST (item appended provisionally to
  diet_restrictions — the conservative, fail-closed store — and the origin
  allergies fact resolved to "none" pending classification, exactly the
  state the classification callback expects and adjusts), and the
  classification becomes a canonical pending question
  (``__diet_classify__:<item>``) on the active flow — restart-safe,
  text-answerable.
- a typed classification answer ("אלרגיה" / "רגישות" / "העדפה" / …) is
  mapped to the SAME ``qa:diet_type:*`` callback the buttons dispatch, so
  there is exactly one classification handler; unrecognized text re-prompts
  instead of silently dropping the flow.

Traces: decision.finalized(entity=question_dedup) with outcome
none_answer / classification_pending / classification_text_resolved, so
the journey "question A answered → classification → question B" is
machine-readable.
"""

from __future__ import annotations

from typing import Any

CLASSIFY_PENDING_PREFIX = "__diet_classify__:"

# W1-23: the payload after the prefix is an ORDERED QUEUE of the items the
# user named in one answer, not a single item. ``queue[0]`` is the item
# currently on the classification keyboard; ``queue[1:]`` are still to come.
# The same pending row (``active_flow.step``) carries it, so the queue is
# restart-safe for free -- no new mechanism, table or state model.
#
# Delimiter: U+001F INFORMATION SEPARATOR ONE (ASCII Unit Separator), the
# character whose *only* defined purpose is exactly this. It cannot collide
# with an item produced by ``_parse_dietary_answer``: that parser splits on
# ``[,\n]+`` / the Hebrew vav connector and then strips whitespace and
# ``.,;:!?"'`` -- it never emits a C0 control character on its own, and
# ``encode_classify_pending`` additionally strips every C0 control character
# from each item before joining, so a hostile paste containing a raw \x1f
# cannot forge a queue boundary either.
CLASSIFY_QUEUE_DELIM = "\x1f"

_C0_CONTROL = {chr(code) for code in range(0x00, 0x20)} | {"\x7f"}


def _strip_control_chars(value: str) -> str:
    return "".join(ch for ch in value if ch not in _C0_CONTROL).strip()


def encode_classify_pending(items: Any) -> str | None:
    """Build the pending step for an ordered classification queue.

    Returns ``None`` when nothing is left to classify, which callers treat
    as "clear the pending question and continue the normal flow".
    """
    cleaned: list[str] = []
    for raw in items or ():
        item = _strip_control_chars(str(raw))
        if item and item not in cleaned:
            cleaned.append(item)
    if not cleaned:
        return None
    return CLASSIFY_PENDING_PREFIX + CLASSIFY_QUEUE_DELIM.join(cleaned)


def decode_classify_pending(pending: Any) -> list[str]:
    """Ordered items still awaiting classification for this pending step.

    Accepts the historical single-item payload unchanged (a payload with no
    delimiter decodes to a one-element queue), so rows written by an older
    build resume correctly.
    """
    if not isinstance(pending, str) or not pending.startswith(CLASSIFY_PENDING_PREFIX):
        return []
    payload = pending[len(CLASSIFY_PENDING_PREFIX):]
    return [part.strip() for part in payload.split(CLASSIFY_QUEUE_DELIM) if part.strip()]

_NONE_ANSWERS = (
    "אין", "אין לי", "אין אלרגיות", "אין לי אלרגיות", "אין רגישויות",
    "אין לי רגישויות", "אין הגבלות", "שום דבר", "כלום", "אין כלום",
    "לא", "אין משהו", "הכל בסדר", "אוכל הכל", "אני אוכל הכל", "none",
)

_TYPE_KEYWORDS = (
    ("allergy", ("אלרגיה", "אלרגי", "אלרגית", "מאובחנת", "אנפילקסיס")),
    ("sensitivity", ("רגישות", "רגיש", "רגישה")),
    ("intolerance", ("אי נוחות", "אי-נוחות", "נפיחות", "לא מתעכל")),
    ("preference", ("העדפה", "מעדיף", "מעדיפה", "לא אוהב", "לא אוהבת", "פשוט להימנע")),
    ("cancel", ("בטל", "ביטול", "טעות", "לא התכוונתי", "תשכח")),
)

_DIETARY_FACT_KEYS = ("allergies", "diet_restrictions")


def is_none_answer(text: str) -> bool:
    cleaned = (text or "").strip().rstrip("!.").strip()
    return cleaned in _NONE_ANSWERS


def classify_type_from_text(text: str) -> str | None:
    cleaned = (text or "").strip()
    for type_key, keywords in _TYPE_KEYWORDS:
        if any(keyword in cleaned for keyword in keywords):
            return type_key
    return None


async def _emit(db: Any, user_id: int, outcome: str, **props: Any) -> None:
    try:
        from noam_coach.observability import taxonomy
        from noam_coach.observability.emit import emit_event

        await emit_event(
            db, user_id, taxonomy.DECISION_FINALIZED,
            entity="question_dedup", source="question_dedup",
            status="finalized", outcome=outcome, properties=props or {},
        )
    except Exception:  # noqa: BLE001
        pass


class _CallbackShim:
    """Message-backed query shim so a typed classification answer flows
    through the SAME qa:diet_type callback handler as a button tap."""

    def __init__(self, message: Any) -> None:
        self._message = message
        self.message = message
        self.from_user = None

    async def edit_message_text(self, text: str, reply_markup: Any = None, parse_mode: Any = None) -> None:
        await self._message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode or "HTML")

    async def edit_message_reply_markup(self, reply_markup: Any = None) -> None:
        return None

    async def answer(self, *args: Any, **kwargs: Any) -> None:
        return None


_original_text_handler: Any = None
_original_callback_handler: Any = None


def install_plan_question_dedup() -> None:
    """Idempotent install of the answer-invariant wraps."""
    global _original_text_handler, _original_callback_handler
    if _original_text_handler is not None:
        return
    import coach_bot

    _original_text_handler = coach_bot.handle_onboarding_text
    original_text = _original_text_handler

    async def dedup_handle_onboarding_text(update: Any, user_id: int) -> bool:
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

        # --- The classification sub-question (restart-safe pending). -------
        if pending and pending.startswith(CLASSIFY_PENDING_PREFIX) and text:
            from noam_coach.bot import onboarding as onboarding_bot

            queue = decode_classify_pending(pending)
            item = queue[0]
            if text in onboarding_bot._CANCEL_WORDS:
                # W1-23: a universal cancel word abandons the whole answer,
                # exactly as before. Cancelling ONE item is the
                # "לא התכוונתי להימנע" button / "טעות" text, which resolves
                # through qa:diet_type:cancel and keeps the queue moving.
                return await original_text(update, user_id)
            resolved = classify_type_from_text(text)
            if resolved is None:
                await message.reply_text(
                    f"לא זיהיתי את הסיווג. איך להתייחס ל{item}? "
                    "אפשר ללחוץ על כפתור או לכתוב: אלרגיה / רגישות / אי נוחות / העדפה / ביטול.",
                    reply_markup=onboarding_bot._diet_type_keyboard(item),
                    parse_mode="HTML",
                )
                return True
            await _emit(
                db, user_id, "classification_text_resolved",
                item=item, restriction_type=resolved, text=text[:80],
                queue_remaining=len(queue) - 1,
            )
            # W1-23: the pending queue is left in place on purpose — the
            # canonical qa:diet_type handler is the single place that pops
            # the head and either re-arms the remainder (rendering the next
            # keyboard) or clears the pending and continues the wizard. The
            # button path goes through exactly the same code.
            await facade.handle_onboarding_callback(
                _CallbackShim(message), user_id, f"qa:diet_type:{resolved}:{item}"
            )
            return True

        # --- Dietary top-level questions: none-answers resolve directly. ---
        question = None
        if pending:
            import questions as questions_module

            question = questions_module.question_by_id(pending)
        fact_key = getattr(question, "fact_key", None)
        # W1-23: ALL named items, in order — not only the first.
        will_classify_items: list[str] = []
        if question is not None and fact_key in _DIETARY_FACT_KEYS and text:
            if is_none_answer(text):
                import user_model

                await user_model.set_fact(
                    db, user_id, fact_key, "none",
                    kind=user_model.KIND_FACT, source=user_model.SOURCE_USER,
                    confirmed=True,
                )
                if fact_key == "diet_restrictions":
                    # A "nothing" answer to the combined sensitivities question
                    # answers the allergy gap too (and vice-versa is handled by
                    # the dedicated allergies question).
                    existing = await user_model.get_fact(db, user_id, "allergies")
                    if existing is None or existing.get("kind") == user_model.KIND_GAP:
                        await user_model.set_fact(
                            db, user_id, "allergies", "none",
                            kind=user_model.KIND_FACT, source=user_model.SOURCE_USER,
                            confirmed=True,
                        )
                await facade.clear_pending(user_id)
                await message.reply_text("מעולה — אין רגישויות או הגבלות. ממשיכים 👍")
                await _emit(db, user_id, "none_answer", fact_key=fact_key, text=text[:80])
                from noam_coach.bot import onboarding as onboarding_bot

                await onboarding_bot.advance_after_answer(message, user_id)
                return True
            from noam_coach.bot import onboarding as onboarding_bot

            try:
                parsed = onboarding_bot._parse_dietary_answer(text)
            except Exception:  # noqa: BLE001
                parsed = []
            if parsed:
                will_classify_items = [str(entry) for entry in parsed if str(entry).strip()]

        consumed = await original_text(update, user_id)

        # --- Invariant repair: answer persisted BEFORE classification. -----
        if consumed and will_classify_items and fact_key:
            import user_model

            parts_fact = await user_model.get_fact(db, user_id, "diet_restrictions")
            parts_value = (
                str(parts_fact.get("value"))
                if parts_fact and parts_fact.get("kind") != user_model.KIND_GAP
                and parts_fact.get("value") not in (None, "", "none")
                else ""
            )
            parts = [p.strip() for p in parts_value.split(",") if p.strip()]
            appended = False
            for item in will_classify_items:
                if item not in parts:
                    parts.append(item)
                    appended = True
            if appended:
                await user_model.set_fact(
                    db, user_id, "diet_restrictions", ", ".join(parts),
                    kind=user_model.KIND_FACT, source=user_model.SOURCE_USER,
                    confirmed=True,
                )
            if fact_key == "allergies":
                existing = await user_model.get_fact(db, user_id, "allergies")
                if existing is None or existing.get("kind") == user_model.KIND_GAP:
                    # Resolved-pending-classification: the item is held in
                    # diet_restrictions (fail-closed) and moves here if the
                    # user classifies it as a true allergy.
                    await user_model.set_fact(
                        db, user_id, "allergies", "none",
                        kind=user_model.KIND_FACT, source=user_model.SOURCE_USER,
                        confirmed=True,
                    )
            # The classification is now a real, resumable pending question —
            # W1-23: an ORDERED QUEUE of every item the user named, on the
            # same pending row. ``original_text`` (the protected onboarding
            # handler) already armed the identical queue; re-arming only when
            # it did not keeps one writer per state and never rewinds a queue
            # that has already advanced.
            flow_now = await conversation.get_active_flow(db, user_id)
            already = decode_classify_pending(
                flow_now.step if flow_now.is_question else None
            )
            if not already:
                encoded = encode_classify_pending(will_classify_items)
                if encoded:
                    await facade.set_pending(user_id, encoded)
            await _emit(
                db, user_id, "classification_pending",
                fact_key=fact_key, item=will_classify_items[0],
                items=list(will_classify_items),
            )
        return consumed

    coach_bot.handle_onboarding_text = dedup_handle_onboarding_text

    _original_callback_handler = coach_bot.handle_onboarding_callback
    original_callback = _original_callback_handler

    async def dedup_handle_onboarding_callback(query: Any, user_id: int, data: str) -> Any:
        import coach_bot as facade
        import conversation

        db = facade.DB
        before: list[str] = []
        if isinstance(data, str) and data.startswith("qa:diet_type:"):
            flow_before = await conversation.get_active_flow(db, user_id)
            before = decode_classify_pending(
                flow_before.step if flow_before.is_question else None
            )
        result = await original_callback(query, user_id, data)
        if isinstance(data, str) and data.startswith("qa:diet_type:"):
            # A tap/typed answer resolved ONE item. The protected handler has
            # already popped the head and re-armed the remainder (W1-23), so
            # only release the pending sub-question when the queue did NOT
            # advance — i.e. nothing is left to classify. Clearing an advanced
            # queue here would drop items 2..n all over again.
            flow = await conversation.get_active_flow(db, user_id)
            after = decode_classify_pending(flow.step if flow.is_question else None)
            if after and after != before:
                return result  # queue advanced — keep the new pending head
            if flow.is_question and str(flow.step or "").startswith(CLASSIFY_PENDING_PREFIX):
                await facade.clear_pending(user_id)
        return result

    coach_bot.handle_onboarding_callback = dedup_handle_onboarding_callback


def uninstall_plan_question_dedup() -> None:
    global _original_text_handler, _original_callback_handler
    import coach_bot

    if _original_text_handler is not None:
        coach_bot.handle_onboarding_text = _original_text_handler
        _original_text_handler = None
    if _original_callback_handler is not None:
        coach_bot.handle_onboarding_callback = _original_callback_handler
        _original_callback_handler = None
