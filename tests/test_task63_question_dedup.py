"""TASK-63 — plan-completion question deduplication and continuation.

The invariant: after a successful answer save, the next rendered question
must not have the same fact_key unless a structured clarification for that
same answer is still unresolved — and that clarification is itself a real,
resumable pending question.

Required trace exercised end-to-end with the REAL protected handlers:
question A answered → interrupt (restart) → resume → next render is
question B; question A is never asked again.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import coach_bot
import conversation
import event_log
import user_model
from db import Database
from helpers import utc_now
from noam_coach.observability import ObservabilityMode, set_mode
from noam_coach.observability.emit import reset_observability_health
from noam_coach.observability.modes import reset_mode
from noam_coach.services.question_dedup import (
    CLASSIFY_PENDING_PREFIX,
    CLASSIFY_QUEUE_DELIM,
    classify_type_from_text,
    decode_classify_pending,
    encode_classify_pending,
    install_plan_question_dedup,
    is_none_answer,
    uninstall_plan_question_dedup,
)

USER_ID = 1


class FakeMessage:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.message_id = 42
        self.chat = SimpleNamespace(id=USER_ID)
        self.replies: list[str] = []
        self.markups: list[Any] = []

    async def reply_text(self, text: str, reply_markup: Any = None, parse_mode: Any = None) -> "FakeMessage":
        self.replies.append(text)
        self.markups.append(reply_markup)
        return self


def _update(text: str) -> Any:
    return SimpleNamespace(
        effective_message=FakeMessage(text),
        effective_user=SimpleNamespace(id=USER_ID),
        effective_chat=SimpleNamespace(id=USER_ID),
    )


@pytest.fixture
async def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Database:
    database = Database(str(tmp_path / "task63.db"))
    await database.init()
    await database.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(?, 'Test', NULL, ?)",
        (USER_ID, utc_now()),
    )
    monkeypatch.setattr(coach_bot, "DB", database)
    from config import SETTINGS

    monkeypatch.setattr(SETTINGS, "telegram_allowed_user_id", USER_ID, raising=False)
    return database


@pytest.fixture(autouse=True)
def _isolation():
    set_mode(ObservabilityMode.CONTENT)
    reset_observability_health()
    yield
    uninstall_plan_question_dedup()
    reset_mode()
    reset_observability_health()


async def _open_allergy_question(db: Database) -> None:
    """Arm the exact incident state: plan-completion pending on allergies."""
    from noam_coach.services import core as core_services

    await core_services.set_flow_state(
        USER_ID, "plan_completion", "q_allergies",
        {"return_to": "menu:smartplan", "plan_type": "nutrition"},
    )
    await coach_bot.set_pending(USER_ID, "q_allergies")


async def _allergy_fact(db: Database) -> Any:
    return await user_model.get_fact(db, USER_ID, "allergies")


def test_none_answer_detection() -> None:
    for text in ("אין", "אין אלרגיות", "אין לי רגישויות", "שום דבר", "כלום", "אוכל הכל"):
        assert is_none_answer(text), text
    for text in ("אגוזים", "אין לי מושג", "בוטנים"):
        assert not is_none_answer(text), text


def test_classification_keywords() -> None:
    assert classify_type_from_text("אלרגיה מאובחנת") == "allergy"
    assert classify_type_from_text("רגישות") == "sensitivity"
    assert classify_type_from_text("סתם העדפה") == "preference"
    assert classify_type_from_text("זה עושה לי נפיחות") == "intolerance"
    assert classify_type_from_text("טעות שלי") == "cancel"
    assert classify_type_from_text("אגוזים") is None


@pytest.mark.asyncio
async def test_none_answer_resolves_and_advances(db: Database) -> None:
    """Acceptance 3: 'אין אלרגיות' marks the fact complete and advances —
    never asks how to classify the phrase 'אין אלרגיות'."""
    await _open_allergy_question(db)
    install_plan_question_dedup()

    update = _update("אין אלרגיות")
    consumed = await coach_bot.handle_onboarding_text(update, USER_ID)

    assert consumed is True
    fact = await _allergy_fact(db)
    assert fact["value"] == "none" and fact["confirmed"]
    assert not any("איך להתייחס" in reply for reply in update.effective_message.replies)
    events = await event_log.list_events(db, USER_ID)
    outcomes = [e.outcome for e in events if e.entity == "question_dedup"]
    assert "none_answer" in outcomes


@pytest.mark.asyncio
async def test_answer_is_persisted_before_classification(db: Database) -> None:
    """Acceptance 1: 'אגוזים' records the answer immediately; the top-level
    question can never be re-rendered even if classification is abandoned."""
    await _open_allergy_question(db)
    install_plan_question_dedup()

    update = _update("אגוזים")
    consumed = await coach_bot.handle_onboarding_text(update, USER_ID)

    assert consumed is True
    # Classification was asked ONCE...
    assert any("איך להתייחס לאגוזים" in reply for reply in update.effective_message.replies)
    # ...but the answer is already persisted (held fail-closed) and the
    # allergies gap is resolved, so the wizard cannot re-ask question A.
    restrictions = await user_model.get_value(db, USER_ID, "diet_restrictions")
    assert "אגוזים" in str(restrictions)
    fact = await _allergy_fact(db)
    assert fact is not None and fact.get("kind") != user_model.KIND_GAP
    from noam_coach.bot import onboarding as onboarding_bot

    next_question = await onboarding_bot.first_missing_plan_question(USER_ID, "nutrition")
    assert next_question is None or next_question.fact_key != "allergies"
    # The clarification itself is a real pending question.
    flow = await conversation.get_active_flow(db, USER_ID)
    assert str(flow.step).startswith("__diet_classify__:")


@pytest.mark.asyncio
async def test_typed_classification_resolves_through_the_canonical_handler(
    db: Database,
) -> None:
    """Acceptance 2: answering the classification by TEXT ('אלרגיה') works,
    moves the item to allergies, and the flow continues."""
    await _open_allergy_question(db)
    install_plan_question_dedup()

    await coach_bot.handle_onboarding_text(_update("אגוזים"), USER_ID)
    update = _update("אלרגיה")
    consumed = await coach_bot.handle_onboarding_text(update, USER_ID)

    assert consumed is True
    allergies = await user_model.get_value(db, USER_ID, "allergies")
    assert "אגוזים" in str(allergies)
    restrictions = await user_model.get_value(db, USER_ID, "diet_restrictions")
    assert "אגוזים" not in str(restrictions)  # moved, not duplicated
    flow = await conversation.get_active_flow(db, USER_ID)
    assert not str(flow.step or "").startswith("__diet_classify__:")
    events = await event_log.list_events(db, USER_ID)
    outcomes = [e.outcome for e in events if e.entity == "question_dedup"]
    assert "classification_text_resolved" in outcomes


@pytest.mark.asyncio
async def test_unrecognized_classification_reprompts_instead_of_dropping(
    db: Database,
) -> None:
    await _open_allergy_question(db)
    install_plan_question_dedup()
    await coach_bot.handle_onboarding_text(_update("אגוזים"), USER_ID)

    update = _update("מה זאת אומרת?")
    consumed = await coach_bot.handle_onboarding_text(update, USER_ID)

    assert consumed is True
    assert any("לא זיהיתי את הסיווג" in reply for reply in update.effective_message.replies)
    flow = await conversation.get_active_flow(db, USER_ID)
    assert str(flow.step).startswith("__diet_classify__:")  # still pending


@pytest.mark.asyncio
async def test_required_trace_interrupt_restart_resume_next_question(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """question A answered → restart → resume → next render is question B."""
    await _open_allergy_question(db)
    install_plan_question_dedup()
    await coach_bot.handle_onboarding_text(_update("אגוזים"), USER_ID)

    # Simulated process restart: caches lost, real startup reload runs.
    coach_bot.PENDING_QUESTION.clear()
    await coach_bot.load_pending_state()
    flow = await conversation.get_active_flow(db, USER_ID)
    assert str(flow.step).startswith("__diet_classify__:")  # survived restart

    # Resume by typing the classification.
    update = _update("רגישות")
    await coach_bot.handle_onboarding_text(update, USER_ID)

    # The wizard continued: the next rendered question is NOT allergies.
    rendered = " ".join(update.effective_message.replies)
    assert "איזה מזונות" not in rendered
    from noam_coach.bot import onboarding as onboarding_bot

    next_question = await onboarding_bot.first_missing_plan_question(USER_ID, "nutrition")
    assert next_question is None or next_question.fact_key != "allergies"
    # And the item was preserved (kept fail-closed in diet_restrictions).
    restrictions = await user_model.get_value(db, USER_ID, "diet_restrictions")
    assert "אגוזים" in str(restrictions)


@pytest.mark.asyncio
async def test_button_classification_still_works_and_clears_the_pending(
    db: Database,
) -> None:
    """Acceptance 6: the button path — qa:diet_type tap resolves the pending
    sub-question through the same canonical handler."""
    await _open_allergy_question(db)
    install_plan_question_dedup()
    await coach_bot.handle_onboarding_text(_update("אגוזים"), USER_ID)

    class FakeQuery:
        def __init__(self) -> None:
            self.message = SimpleNamespace(message_id=7, chat=SimpleNamespace(id=USER_ID))
            self.edits: list[str] = []

        async def edit_message_text(self, text: str, reply_markup: Any = None, parse_mode: Any = None) -> None:
            self.edits.append(text)

        async def answer(self, *a: Any, **k: Any) -> None:
            return None

    await coach_bot.handle_onboarding_callback(FakeQuery(), USER_ID, "qa:diet_type:allergy:אגוזים")

    allergies = await user_model.get_value(db, USER_ID, "allergies")
    assert "אגוזים" in str(allergies)
    flow = await conversation.get_active_flow(db, USER_ID)
    assert not str(flow.step or "").startswith("__diet_classify__:")


@pytest.mark.asyncio
async def test_non_dietary_questions_are_untouched(db: Database) -> None:
    """The wrap must not interfere with other question types."""
    from noam_coach.services import core as core_services

    await core_services.set_flow_state(
        USER_ID, "plan_completion", "q_session_minutes",
        {"return_to": "menu:smartplan", "plan_type": "workout"},
    )
    await coach_bot.set_pending(USER_ID, "q_session_minutes")
    install_plan_question_dedup()

    update = _update("45")
    consumed = await coach_bot.handle_onboarding_text(update, USER_ID)
    assert consumed is True
    value = await user_model.get_value(db, USER_ID, "session_minutes")
    assert value is not None


# ---------------------------------------------------------------------------
# W1-23 — a multi-item answer must classify EVERY item, in order.
#
# The defect: an answer naming several restrictions ("גלוטן, חלב, ביצים")
# put only the FIRST through the "how should I treat X?" keyboard. Items
# 2..n landed in diet_restrictions unclassified -- never ruled out as
# allergies. All the tests below drive the REAL handlers
# (coach_bot.handle_onboarding_text / handle_onboarding_callback) through
# the installed dedup wrap; the pure-unit delimiter tests are a supplement.
# ---------------------------------------------------------------------------

THREE_ITEMS = ("גלוטן", "חלב", "ביצים")


class FakeQuery:
    """Button tap. ``message`` is a real FakeMessage so the next
    classification card in the queue has somewhere to render."""

    def __init__(self) -> None:
        self.message = FakeMessage()
        self.edits: list[str] = []

    async def edit_message_text(self, text: str, reply_markup: Any = None, parse_mode: Any = None) -> None:
        self.edits.append(text)

    async def edit_message_reply_markup(self, reply_markup: Any = None) -> None:
        return None

    async def answer(self, *a: Any, **k: Any) -> None:
        return None


async def _open_question(question_id: str) -> None:
    from noam_coach.services import core as core_services

    await core_services.set_flow_state(
        USER_ID, "plan_completion", question_id,
        {"return_to": "menu:smartplan", "plan_type": "nutrition"},
    )
    await coach_bot.set_pending(USER_ID, question_id)


async def _pending_queue(db: Database) -> list[str]:
    flow = await conversation.get_active_flow(db, USER_ID)
    return decode_classify_pending(flow.step if flow.is_question else None)


def _asked(*messages: Any) -> list[str]:
    """Every 'איך להתייחס ל<item>?' card rendered on these messages, in order."""
    out: list[str] = []
    for message in messages:
        for reply in getattr(message, "replies", []):
            if "איך להתייחס ל" in reply:
                out.append(reply.split("איך להתייחס ל", 1)[1].rstrip("?").strip())
    return out


# --- delimiter non-collision (pure unit, supplement) -----------------------


def test_queue_delimiter_cannot_appear_in_a_parsed_item() -> None:
    """The delimiter must be unreachable from _parse_dietary_answer output."""
    from noam_coach.bot import onboarding as onboarding_bot

    answers = [
        "גלוטן, חלב, ביצים",
        "חציל, טורטייה ואגוזים",
        "אני נמנע מקשיו",
        "cashew, peanuts",
        "אגוזים\nחלב",
        'קשיו; חלב: ביצים!',
        "a|b, c-d, e_f, גלוטן:חלב",
        # a hostile paste that literally contains the delimiter
        f"גלוטן{CLASSIFY_QUEUE_DELIM}חלב, ביצים",
    ]
    for answer in answers:
        items = onboarding_bot._parse_dietary_answer(answer)
        encoded = encode_classify_pending(items)
        assert encoded is not None
        # round-trip is lossless in item COUNT and ORDER for everything the
        # parser can legitimately produce
        decoded = decode_classify_pending(encoded)
        assert all(CLASSIFY_QUEUE_DELIM not in item for item in decoded)
        assert decoded == [
            "".join(ch for ch in item if not (ord(ch) < 0x20 or ord(ch) == 0x7F)).strip()
            for item in dict.fromkeys(items)
        ]


def test_legacy_single_item_payload_still_decodes() -> None:
    """Rows written before the queue existed must resume, not crash."""
    assert decode_classify_pending(f"{CLASSIFY_PENDING_PREFIX}אגוזים") == ["אגוזים"]
    assert decode_classify_pending("q_allergies") == []
    assert decode_classify_pending(None) == []
    assert encode_classify_pending([]) is None
    assert encode_classify_pending(["", "  "]) is None


# --- site 1: the allergies branch (q_allergies, free_text_fallback) --------


@pytest.mark.asyncio
async def test_allergies_branch_asks_about_every_named_item_in_order(
    db: Database,
) -> None:
    """Acceptance 1 (site onboarding.py allergies branch): 3 items -> 3 cards."""
    await _open_question("q_allergies")
    install_plan_question_dedup()

    first = _update("גלוטן, חלב, ביצים")
    await coach_bot.handle_onboarding_text(first, USER_ID)
    assert _asked(first.effective_message) == ["גלוטן"]
    assert await _pending_queue(db) == list(THREE_ITEMS)

    second = _update("רגישות")
    await coach_bot.handle_onboarding_text(second, USER_ID)
    assert _asked(second.effective_message) == ["חלב"]
    assert await _pending_queue(db) == ["חלב", "ביצים"]

    third = _update("רגישות")
    await coach_bot.handle_onboarding_text(third, USER_ID)
    assert _asked(third.effective_message) == ["ביצים"]
    assert await _pending_queue(db) == ["ביצים"]

    fourth = _update("העדפה")
    await coach_bot.handle_onboarding_text(fourth, USER_ID)
    # Queue drained -> pending released, wizard continues.
    assert await _pending_queue(db) == []
    levels = await user_model.get_fact(db, USER_ID, "diet_restriction_levels")
    assert set((levels or {}).get("value", {})) == set(THREE_ITEMS)


@pytest.mark.asyncio
async def test_two_item_answer_classifies_both(db: Database) -> None:
    await _open_question("q_allergies")
    install_plan_question_dedup()

    first = _update("קשיו, בוטנים")
    await coach_bot.handle_onboarding_text(first, USER_ID)
    assert _asked(first.effective_message) == ["קשיו"]

    second = _update("אלרגיה")
    await coach_bot.handle_onboarding_text(second, USER_ID)
    assert _asked(second.effective_message) == ["בוטנים"]

    third = _update("אלרגיה")
    await coach_bot.handle_onboarding_text(third, USER_ID)
    assert await _pending_queue(db) == []
    allergies = str(await user_model.get_value(db, USER_ID, "allergies"))
    assert "קשיו" in allergies and "בוטנים" in allergies


# --- site 2: the diet_restrictions branch (q_diet_restrictions) -----------


@pytest.mark.asyncio
async def test_diet_restrictions_branch_asks_about_every_named_item(
    db: Database,
) -> None:
    """Acceptance 1 + 6 (site onboarding.py diet_restrictions branch).

    The storage loop persists ALL items (unchanged behaviour) AND every one
    of them is now put through the classification keyboard.
    """
    await _open_question("q_diet_restrictions")
    install_plan_question_dedup()

    first = _update("גלוטן, חלב, ביצים")
    await coach_bot.handle_onboarding_text(first, USER_ID)

    # Acceptance 6: storage behaviour unchanged -- all three stored up front.
    stored = str(await user_model.get_value(db, USER_ID, "diet_restrictions"))
    for item in THREE_ITEMS:
        assert item in stored, stored

    assert _asked(first.effective_message) == ["גלוטן"]
    assert await _pending_queue(db) == list(THREE_ITEMS)

    second = _update("רגישות")
    await coach_bot.handle_onboarding_text(second, USER_ID)
    assert _asked(second.effective_message) == ["חלב"]

    third = _update("רגישות")
    await coach_bot.handle_onboarding_text(third, USER_ID)
    assert _asked(third.effective_message) == ["ביצים"]
    assert await _pending_queue(db) == ["ביצים"]


# --- Acceptance 2: restart mid-queue resumes at the NEXT item --------------


@pytest.mark.asyncio
async def test_restart_mid_queue_resumes_at_the_next_unclassified_item(
    db: Database,
) -> None:
    await _open_question("q_allergies")
    install_plan_question_dedup()
    await coach_bot.handle_onboarding_text(_update("גלוטן, חלב, ביצים"), USER_ID)
    await coach_bot.handle_onboarding_text(_update("רגישות"), USER_ID)  # גלוטן done

    # Simulated process restart: in-memory caches lost, real reload runs.
    coach_bot.PENDING_QUESTION.clear()
    await coach_bot.load_pending_state()

    # Not the first item, and not none: the queue survived with its head at חלב.
    assert await _pending_queue(db) == ["חלב", "ביצים"]

    resumed = _update("רגישות")
    await coach_bot.handle_onboarding_text(resumed, USER_ID)
    assert _asked(resumed.effective_message) == ["ביצים"]


# --- Acceptance 4: the BUTTON path advances the queue too -----------------


@pytest.mark.asyncio
async def test_button_path_advances_the_queue(db: Database) -> None:
    await _open_question("q_allergies")
    install_plan_question_dedup()
    await coach_bot.handle_onboarding_text(_update("גלוטן, חלב, ביצים"), USER_ID)

    query = FakeQuery()
    await coach_bot.handle_onboarding_callback(query, USER_ID, "qa:diet_type:allergy:גלוטן")
    assert _asked(query.message) == ["חלב"]
    assert await _pending_queue(db) == ["חלב", "ביצים"]

    query2 = FakeQuery()
    await coach_bot.handle_onboarding_callback(query2, USER_ID, "qa:diet_type:sensitivity:חלב")
    assert _asked(query2.message) == ["ביצים"]
    assert await _pending_queue(db) == ["ביצים"]

    query3 = FakeQuery()
    await coach_bot.handle_onboarding_callback(query3, USER_ID, "qa:diet_type:preference:ביצים")
    assert _asked(query3.message) == []          # nothing left to ask
    assert await _pending_queue(db) == []        # pending released
    allergies = str(await user_model.get_value(db, USER_ID, "allergies"))
    assert "גלוטן" in allergies


@pytest.mark.asyncio
async def test_mixed_button_and_text_paths_advance_the_same_queue(
    db: Database,
) -> None:
    """Acceptance 4: the two paths share one queue and one handler."""
    await _open_question("q_allergies")
    install_plan_question_dedup()
    await coach_bot.handle_onboarding_text(_update("גלוטן, חלב, ביצים"), USER_ID)

    query = FakeQuery()
    await coach_bot.handle_onboarding_callback(query, USER_ID, "qa:diet_type:allergy:גלוטן")
    assert _asked(query.message) == ["חלב"]

    typed = _update("רגישות")
    await coach_bot.handle_onboarding_text(typed, USER_ID)
    assert _asked(typed.effective_message) == ["ביצים"]
    assert await _pending_queue(db) == ["ביצים"]


# --- Acceptance 5: a per-item cancel keeps the rest of the queue ----------


@pytest.mark.asyncio
async def test_cancel_on_one_item_does_not_abandon_the_rest(db: Database) -> None:
    await _open_question("q_allergies")
    install_plan_question_dedup()
    await coach_bot.handle_onboarding_text(_update("גלוטן, חלב, ביצים"), USER_ID)

    query = FakeQuery()
    await coach_bot.handle_onboarding_callback(query, USER_ID, "qa:diet_type:cancel:גלוטן")

    # גלוטן removed, but חלב and ביצים are still asked about.
    assert _asked(query.message) == ["חלב"]
    assert await _pending_queue(db) == ["חלב", "ביצים"]

    typed = _update("טעות")  # a typed per-item cancel
    await coach_bot.handle_onboarding_text(typed, USER_ID)
    assert _asked(typed.effective_message) == ["ביצים"]
    assert await _pending_queue(db) == ["ביצים"]


@pytest.mark.asyncio
async def test_universal_cancel_word_still_aborts_the_whole_flow(
    db: Database,
) -> None:
    """A universal cancel word ('ביטול') is not a per-item cancel — it must
    keep abandoning the whole pending flow, exactly as before W1-23."""
    await _open_question("q_allergies")
    install_plan_question_dedup()
    await coach_bot.handle_onboarding_text(_update("גלוטן, חלב, ביצים"), USER_ID)

    update = _update("ביטול")
    await coach_bot.handle_onboarding_text(update, USER_ID)
    assert await _pending_queue(db) == []
    assert _asked(update.effective_message) == []


# --- Acceptance 3: single-item behaviour is unchanged ---------------------


@pytest.mark.asyncio
async def test_single_item_answer_asks_exactly_once_and_then_continues(
    db: Database,
) -> None:
    """No regression: one item -> one card -> pending released."""
    await _open_question("q_allergies")
    install_plan_question_dedup()

    first = _update("אגוזים")
    await coach_bot.handle_onboarding_text(first, USER_ID)
    assert _asked(first.effective_message) == ["אגוזים"]
    assert await _pending_queue(db) == ["אגוזים"]

    second = _update("אלרגיה")
    await coach_bot.handle_onboarding_text(second, USER_ID)
    assert _asked(second.effective_message) == []   # never re-asked
    assert await _pending_queue(db) == []
    assert "אגוזים" in str(await user_model.get_value(db, USER_ID, "allergies"))
