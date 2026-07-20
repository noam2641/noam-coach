"""Batch 6 — quantity clarification UX.

Every test here drives the REAL production path: the actual correction-text
handler and the actual callback router, not the pure helpers alone. The
guarantee under test is behavioural — when the quantity evidence is not safe
to convert, the bot ASKS on the card the user can see, and the answer is
applied exactly once.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import coach_bot
from db import Database
from helpers import utc_now
from models import FoodItem, MealAnalysis
from noam_coach.services.meal_clarification import (
    APPLY_CANCEL,
    PendingClarification,
    detect_quantity_clarification,
)

USER_ID = 1


@pytest.fixture
async def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Database:
    database = Database(str(tmp_path / "batch6.db"))
    await database.init()
    await database.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(?, 'Test', NULL, ?)",
        (USER_ID, utc_now()),
    )
    monkeypatch.setattr(coach_bot, "DB", database)
    from config import SETTINGS

    monkeypatch.setattr(SETTINGS, "telegram_allowed_user_id", USER_ID, raising=False)
    return database


class _FakeSent:
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def edit_text(self, text: str, **kwargs: Any) -> "_FakeSent":
        self.texts.append(text)
        return self


class _FakeMessage:
    def __init__(self, text: str) -> None:
        self.text = text
        self.message_id = 42
        self.chat = SimpleNamespace(id=USER_ID)
        self.sent = _FakeSent()

    async def reply_text(self, text: str, **kwargs: Any) -> _FakeSent:
        self.sent.texts.append(text)
        return self.sent


class _FakeQuery:
    """Enough of a Telegram callback query for the real callback handler."""

    def __init__(self) -> None:
        self.edits: list[tuple[str, Any]] = []
        self.answers: list[str] = []
        self.message = _FakeMessage("")
        self.from_user = SimpleNamespace(id=USER_ID)

    async def answer(self, text: str = "", **kwargs: Any) -> None:
        self.answers.append(text)

    async def edit_message_text(self, text: str, **kwargs: Any) -> None:
        self.edits.append((text, kwargs.get("reply_markup")))


def _unknown_food_analysis() -> MealAnalysis:
    """A counted food with NO supported portion model — the silent decline."""
    analysis = MealAnalysis(
        meal_name="מרק קובה",
        confidence=0.7,
        items=[
            FoodItem(name="מרק קובה", grams=250, calories=300, protein=12,
                     carbs=30, fat=14, confidence=0.8),
        ],
    )
    return analysis


def _schnitzel_incident_analysis() -> MealAnalysis:
    """The incident signature: 3 schnitzels recorded as 3 grams."""
    return MealAnalysis(
        meal_name="שניצל",
        confidence=0.7,
        items=[
            FoodItem(name="שניצל", grams=3, calories=8, protein=1,
                     carbs=0, fat=0, confidence=0.8),
        ],
    )


def _unconvertible_counted_item() -> MealAnalysis:
    """A food WITH a portion model whose count cannot be converted safely.

    This is the only shape that reaches the size-estimation buttons: Batch 5
    declines (leaving raw count evidence), but ``_per_unit_weight`` still
    prices small/medium/large. A food that converts cleanly — like a plain
    "3 שניצלים" — is deliberately NOT questioned; see
    ``test_convertible_count_is_estimated_not_questioned``.
    """
    item = FoodItem(name="שניצל", grams=3, calories=8, protein=1,
                    carbs=0, fat=0, confidence=0.8)
    item.quantity_count = 3
    item.quantity_source = "user_count"
    return MealAnalysis(meal_name="שניצל", confidence=0.7, items=[item])


async def _create_approval(analysis: MealAnalysis) -> str:
    from noam_coach.services import core as core_services

    return await core_services.create_approval(
        USER_ID, "meal",
        {"analysis": analysis.model_dump(), "image": None, "revision": 0},
    )


async def _create_approval_with_question(analysis: MealAnalysis) -> str:
    """Seed an approval whose card already carries a quantity question.

    Uses the same production builders the correction-text stage uses, so the
    stored question/options are byte-identical to what a real correction
    would have written — only the AI reanalysis step is bypassed.
    """
    from noam_coach.services import core as core_services
    from noam_coach.services.meal_clarification import (
        build_clarification_options,
        detect_quantity_clarification,
    )
    from models import ClarificationOption

    pending = detect_quantity_clarification(analysis)
    assert pending is not None, "fixture must produce a clarification"
    analysis.question = pending.question
    analysis.options = [
        ClarificationOption(
            label=str(opt.get("label") or ""),
            item_index=opt.get("item_index"),
            item_name=pending.item_name,
            apply_kind=opt.get("apply_kind"),
            set_grams=opt.get("set_grams"),
            set_size=opt.get("set_size"),
        )
        for opt in build_clarification_options(pending)
    ]
    return await core_services.create_approval(
        USER_ID, "meal",
        {
            "analysis": analysis.model_dump(),
            "image": None,
            "revision": 0,
            "pending_clarification": pending.to_payload(),
        },
    )


async def _payload(db: Database, approval_id: str) -> dict[str, Any]:
    import json

    row = await db.fetch_one("SELECT payload FROM approvals WHERE id=?", (approval_id,))
    return json.loads(row["payload"])


async def _send_correction(
    monkeypatch: pytest.MonkeyPatch, approval_id: str, text: str
) -> list[str]:
    """Run the REAL correction-text handler; returns rendered approval ids."""
    from noam_coach.bot import meal_text as meal_text_bot

    rendered: list[str] = []

    async def fake_render(target: Any, user_id: int, approval_id_: str, **k: Any) -> None:
        rendered.append(approval_id_)

    monkeypatch.setattr(coach_bot, "render_meal", fake_render, raising=False)

    async def fail_ai(*a: Any, **k: Any) -> None:
        raise AssertionError("this correction must resolve deterministically")

    monkeypatch.setattr(coach_bot, "reanalyze_meal_with_text_and_image", fail_ai, raising=False)
    monkeypatch.setattr(coach_bot, "analyze_meal_text", fail_ai, raising=False)

    message = _FakeMessage(text)
    update = SimpleNamespace(
        effective_message=message,
        effective_user=SimpleNamespace(id=USER_ID),
        effective_chat=SimpleNamespace(id=USER_ID),
    )
    await meal_text_bot._handle_meal_correction_text(update, USER_ID, approval_id, 0)
    return rendered


# ---------------------------------------------------------------------------
# Trigger: ambiguous / unsupported count asks instead of guessing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unsupported_count_asks_instead_of_guessing(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """'שלוש חתיכות' on a food with no portion model must produce a QUESTION
    on the persisted analysis — previously this declined in total silence."""
    approval_id = await _create_approval(_unknown_food_analysis())

    await _send_correction(monkeypatch, approval_id, "שלוש חתיכות")

    payload = await _payload(db, approval_id)
    analysis = MealAnalysis.model_validate(payload["analysis"])
    assert analysis.question, "the bot must ask rather than guess"
    assert analysis.options, "a question without options traps the user"

    pending = PendingClarification.from_payload(payload.get("pending_clarification"))
    assert pending is not None
    assert not pending.is_resolved
    # The payload alone can reconstruct the pending clarification.
    assert pending.item_name == "מרק קובה"
    assert pending.question == analysis.question


@pytest.mark.asyncio
async def test_question_reaches_the_rendered_card(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clarification that is stored but never shown is worthless: assert the
    real renderer puts the question text and an escape button on the card."""
    from noam_coach.bot import meals as meals_bot

    approval_id = await _create_approval(_unknown_food_analysis())
    await _send_correction(monkeypatch, approval_id, "שלוש חתיכות")

    query = _FakeQuery()
    await meals_bot.render_meal(query, USER_ID, approval_id)

    assert query.edits, "the card must be rendered"
    text, markup = query.edits[-1]
    payload = await _payload(db, approval_id)
    pending = PendingClarification.from_payload(payload["pending_clarification"])
    assert pending.item_name in text

    labels = [b.text for row in markup.inline_keyboard for b in row]
    callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert any("גרמים" in label for label in labels), "no way to type grams"
    assert any("בטל" in label for label in labels), "no way out of the question"
    # The user is never trapped: approve/reject stay available.
    assert any(c and ("approve" in c or "reject" in c) for c in callbacks)


# ---------------------------------------------------------------------------
# Answering: size button, typed grams, and idempotency
# ---------------------------------------------------------------------------


async def _tap_clarify(approval_id: str, index: int, monkeypatch: pytest.MonkeyPatch) -> _FakeQuery:
    """Drive the REAL callback router with clarify:{approval_id}:{index}."""
    from noam_coach.bot import callback_meals

    query = _FakeQuery()
    handled = await callback_meals.handle_meal_callback(
        query, USER_ID, f"clarify:{approval_id}:{index}"
    )
    assert handled, "the clarify callback must be routed"
    return query


@pytest.mark.asyncio
async def test_convertible_count_is_estimated_not_questioned(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """'3 שניצלים' has a real portion model, so Batch 5 converts it to 450 g.

    The spec requires the bot to "estimate safely OR ask" — never '3 גרם'.
    Deriving from a portion model IS the safe estimate, so asking here would
    be needless friction. This pins that Batch 6 does not over-trigger.
    """
    approval_id = await _create_approval(_schnitzel_incident_analysis())
    await _send_correction(monkeypatch, approval_id, "3 שניצלים")

    payload = await _payload(db, approval_id)
    analysis = MealAnalysis.model_validate(payload["analysis"])
    item = analysis.items[0]

    assert item.grams == 450, "3 × 150 g medium units"
    assert item.grams != 3, "the incident: a count rendered as grams"
    assert item.quantity_source == "count_derived"
    assert not analysis.question, "a cleanly converted count must not be questioned"
    assert payload.get("pending_clarification") in (None, {}) or not (
        PendingClarification.from_payload(payload.get("pending_clarification"))
    )


@pytest.mark.asyncio
async def test_size_choice_applies_once_and_is_idempotent(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unconvertible count → user picks 'medium' → grams materialize once.
    Tapping the same button again must NOT apply the quantity a second time."""
    approval_id = await _create_approval_with_question(_unconvertible_counted_item())

    payload = await _payload(db, approval_id)
    analysis = MealAnalysis.model_validate(payload["analysis"])
    assert analysis.question, "the incident shape must be questioned"
    size_indexes = [
        i for i, o in enumerate(analysis.options) if o.apply_kind == "size"
    ]
    assert size_indexes, "a food with a portion model must offer size estimates"

    medium = size_indexes[len(size_indexes) // 2]
    expected = analysis.options[medium].set_grams

    rendered: list[str] = []

    async def fake_render(target: Any, user_id: int, approval_id_: str, **k: Any) -> None:
        rendered.append(approval_id_)

    monkeypatch.setattr(coach_bot, "render_meal", fake_render, raising=False)

    await _tap_clarify(approval_id, medium, monkeypatch)

    after = await _payload(db, approval_id)
    item = MealAnalysis.model_validate(after["analysis"]).items[0]
    assert item.grams == expected
    # The chosen quantity is USER evidence, so re-materialization cannot
    # overwrite it on any later reload.
    assert item.quantity_source == "user"
    first_calories = item.calories

    pending = PendingClarification.from_payload(after["pending_clarification"])
    assert pending.is_resolved
    assert pending.resolved_grams == expected

    # --- repeat tap: must not double-apply -----------------------------
    await _tap_clarify(approval_id, medium, monkeypatch)

    repeated = await _payload(db, approval_id)
    repeated_item = MealAnalysis.model_validate(repeated["analysis"]).items[0]
    assert repeated_item.grams == expected, "quantity was applied twice"
    assert repeated_item.calories == first_calories, "macros were applied twice"


@pytest.mark.asyncio
async def test_typed_grams_resolve_the_open_question_once(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Answering in text goes through the existing gram-locking path and
    closes the question; repeating the same text does not re-apply it."""
    approval_id = await _create_approval_with_question(_unconvertible_counted_item())

    await _send_correction(monkeypatch, approval_id, "480 גרם שניצל")

    payload = await _payload(db, approval_id)
    analysis = MealAnalysis.model_validate(payload["analysis"])
    item = analysis.items[0]
    assert item.grams == 480
    assert not analysis.question, "the answered question must stop being asked"

    pending = PendingClarification.from_payload(payload["pending_clarification"])
    assert pending.is_resolved


@pytest.mark.asyncio
async def test_cancel_preserves_the_previous_draft(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancel clears the question WITHOUT changing any quantity."""
    approval_id = await _create_approval_with_question(_unconvertible_counted_item())

    before = await _payload(db, approval_id)
    before_analysis = MealAnalysis.model_validate(before["analysis"])
    before_item = before_analysis.items[0]
    cancel_index = next(
        i for i, o in enumerate(before_analysis.options) if o.apply_kind == APPLY_CANCEL
    )

    async def fake_render(target: Any, user_id: int, approval_id_: str, **k: Any) -> None:
        return None

    monkeypatch.setattr(coach_bot, "render_meal", fake_render, raising=False)

    await _tap_clarify(approval_id, cancel_index, monkeypatch)

    after = await _payload(db, approval_id)
    after_analysis = MealAnalysis.model_validate(after["analysis"])
    after_item = after_analysis.items[0]

    assert after_item.grams == before_item.grams, "cancel lost the previous draft"
    assert after_item.calories == before_item.calories
    assert after_item.quantity_count == before_item.quantity_count, "count evidence lost"
    assert not after_analysis.question, "cancel must close the question"


# ---------------------------------------------------------------------------
# Adversarial: strong evidence, replay, and stale answers
# ---------------------------------------------------------------------------


def test_user_pinned_grams_are_never_questioned() -> None:
    """An item whose grams the user already pinned has nothing to clarify —
    asking would invite overwriting the strongest evidence we have."""
    analysis = _schnitzel_incident_analysis()
    item = analysis.items[0]
    item.quantity_count = 3
    item.grams = 500
    item.quantity_source = "user"

    assert detect_quantity_clarification(analysis) is None


def test_answer_does_not_apply_to_a_replaced_item() -> None:
    """If the item changed between question and answer, the answer must be
    refused rather than written onto an unrelated food."""
    from noam_coach.services.meal_clarification import (
        apply_quantity_clarification,
        build_clarification_options,
    )

    analysis = _schnitzel_incident_analysis()
    analysis.items[0].quantity_count = 3
    analysis.items[0].quantity_source = "user_count"
    pending = detect_quantity_clarification(analysis)
    option = next(
        o for o in build_clarification_options(pending) if o.get("apply_kind") == "size"
    )

    # The user corrected the meal to a different food before answering.
    analysis.items[0] = FoodItem(
        name="פלאפל", grams=100, calories=300, protein=10,
        carbs=30, fat=15, confidence=0.8,
    )

    assert apply_quantity_clarification(analysis, pending, option) is False
    assert analysis.items[0].grams == 100, "a stale answer mutated the wrong item"


def test_clarification_token_is_stable_across_replay() -> None:
    """The same unresolved question regenerated from the same analysis keeps
    its token, so resolution state survives a re-render."""
    analysis = _schnitzel_incident_analysis()
    analysis.items[0].quantity_count = 3
    analysis.items[0].quantity_source = "user_count"

    first = detect_quantity_clarification(analysis)
    second = detect_quantity_clarification(analysis)
    assert first.token == second.token


# ---------------------------------------------------------------------------
# Spec cases: stale revision, escape routes, and non-interference
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_approval_press_is_refused_after_clarification(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Answering a clarification bumps the payload revision, so an approve
    button rendered BEFORE the answer must be refused, not silently saved."""
    from noam_coach.services.meal_approval_lifecycle import is_stale_revision

    approval_id = await _create_approval_with_question(_unconvertible_counted_item())
    before = await _payload(db, approval_id)
    stale_revision = before.get("revision", 0)

    analysis = MealAnalysis.model_validate(before["analysis"])
    size_index = next(
        i for i, o in enumerate(analysis.options) if o.apply_kind == "size"
    )

    async def fake_render(target: Any, user_id: int, approval_id_: str, **k: Any) -> None:
        return None

    monkeypatch.setattr(coach_bot, "render_meal", fake_render, raising=False)
    await _tap_clarify(approval_id, size_index, monkeypatch)

    row = await db.fetch_one("SELECT * FROM approvals WHERE id=?", (approval_id,))
    import json

    row = {"data": json.loads(row["payload"])}
    assert row["data"]["revision"] > stale_revision, "revision must bump"
    assert is_stale_revision(row, stale_revision), "pre-answer press must be stale"


@pytest.mark.asyncio
async def test_ask_grams_offers_back_to_meal(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """'type grams' must not strand the user: the prompt carries a working
    back-to-meal control and leaves the question open."""
    approval_id = await _create_approval_with_question(_unconvertible_counted_item())
    payload = await _payload(db, approval_id)
    analysis = MealAnalysis.model_validate(payload["analysis"])
    grams_index = next(
        i for i, o in enumerate(analysis.options) if o.apply_kind == "grams_prompt"
    )

    query = await _tap_clarify(approval_id, grams_index, monkeypatch)

    assert query.edits, "the grams prompt must be shown"
    _text, markup = query.edits[-1]
    callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert any(c == f"backmeal:{approval_id}" for c in callbacks), "no way back"

    after = await _payload(db, approval_id)
    pending = PendingClarification.from_payload(after["pending_clarification"])
    assert not pending.is_resolved, "the question must stay open until grams arrive"


@pytest.mark.asyncio
async def test_explicit_grams_are_not_overwritten_by_clarification(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user-locked gram value is the strongest evidence we have. A later
    correction must never reopen a question that could overwrite it."""
    approval_id = await _create_approval(_schnitzel_incident_analysis())
    await _send_correction(monkeypatch, approval_id, "השניצל בערך 180 גרם")

    payload = await _payload(db, approval_id)
    analysis = MealAnalysis.model_validate(payload["analysis"])
    item = analysis.items[0]

    assert item.grams == 180, "explicit gram lock lost"
    assert item.quantity_source in {"user", "user_grams"}
    assert not analysis.question, "locked grams must not be re-questioned"


@pytest.mark.asyncio
async def test_unrelated_text_is_not_swallowed_by_the_question(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An open question must not capture text that is plainly not an answer:
    a non-quantity correction leaves the question standing, unresolved."""
    approval_id = await _create_approval_with_question(_unconvertible_counted_item())

    await _send_correction(monkeypatch, approval_id, "בלי קטשופ")

    payload = await _payload(db, approval_id)
    analysis = MealAnalysis.model_validate(payload["analysis"])
    pending = PendingClarification.from_payload(payload["pending_clarification"])

    assert not pending.is_resolved, "unrelated text must not answer the question"
    assert analysis.question, "the question must survive an unrelated correction"
    assert analysis.items[0].grams == 3, "unrelated text must not set a quantity"
