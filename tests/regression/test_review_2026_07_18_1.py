"""Regression coverage for review 2026-07-18_1 approved findings.

Each test cites the finding it locks in. Evidence lives in
reviews/2026-07-18_1/ (events referenced by id in findings.json).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import coach_bot
import user_model
from db import Database
from helpers import utc_now
from noam_coach.bot import callback_menu as callback_menu_bot
from noam_coach.bot import onboarding as onboarding_bot
from noam_coach.services import core as core_services
from noam_coach.services import health_jobs

USER_ID = 1


class FakeMessage:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.markups: list[Any] = []

    async def reply_text(self, text: str, reply_markup: Any = None, parse_mode: str | None = None) -> None:
        del parse_mode
        self.texts.append(text)
        self.markups.append(reply_markup)


class FakeTarget:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.reply_markups: list[Any] = []
        self.message = FakeMessage()
        self.from_user = type("U", (), {"id": USER_ID})()
        self.data: str | None = None

    async def edit_message_text(self, text: str, reply_markup: Any = None, parse_mode: str | None = None) -> None:
        del parse_mode
        self.messages.append(text)
        self.reply_markups.append(reply_markup)

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        del text, show_alert


async def _make_db(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "review_r1.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'Test',NULL,?)",
        (utc_now(),),
    )
    return db


def _patch_db(monkeypatch: pytest.MonkeyPatch, db: Database) -> None:
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(health_jobs, "DB", db)
    monkeypatch.setattr(onboarding_bot, "DB", db)
    monkeypatch.setattr(core_services, "DB", db)
    monkeypatch.setattr(callback_menu_bot, "DB", db)


async def _seed_single_pending_item(db: Database) -> None:
    """The incident shape: sleep_schedule is the ONLY unconfirmed item."""
    await user_model.set_fact(
        db, USER_ID, "sleep_schedule",
        {"typical_bedtime": "23:15", "typical_wake_time": "06:45"},
        kind=user_model.KIND_ESTIMATE, source=user_model.SOURCE_DERIVED, confirmed=False,
    )


async def _press_skip_item(db: Database) -> FakeTarget:
    target = FakeTarget()
    target.data = "health:skip_item"
    handled = await callback_menu_bot.handle_menu_callback(target, USER_ID, "health:skip_item")
    assert handled is True
    return target


# ---------------------------------------------------------------------------
# F-01 — incident-specific: the exact sleep_schedule loop from events 66/75/84
# ---------------------------------------------------------------------------


async def test_f01_second_skip_on_last_item_finishes_the_wizard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await _seed_single_pending_item(db)

    first = FakeTarget()
    assert await health_jobs.ask_next_health_confirm_step(first, USER_ID) is True
    assert "שינה" in first.messages[-1]

    # First skip: the item is deferred and returns for one explicit final
    # pass — but NEVER as a byte-identical screen (the return is announced).
    second = await _press_skip_item(db)
    assert second.messages, "first skip must re-render, not go silent"
    assert "חוזר לפריט שדחית" in second.messages[-1]
    assert second.messages[-1] != first.messages[-1]

    # Second skip: terminal — the wizard finishes instead of looping.
    third = await _press_skip_item(db)
    assert third.messages
    final_text = third.messages[-1]
    assert "חוזר לפריט שדחית" not in final_text
    assert "לא זיהיתי דפוס שינה" not in final_text  # the question is GONE
    # The fact was neither applied nor invalidated — still pending for later.
    fact = await user_model.get_fact(db, USER_ID, "sleep_schedule")
    assert fact is not None and not fact.get("confirmed")


async def test_f01_skip_never_rerenders_identical_screen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The user-visible invariant behind the incident: pressing 'דלג כרגע'
    must always change the screen."""
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await _seed_single_pending_item(db)

    target = FakeTarget()
    await health_jobs.ask_next_health_confirm_step(target, USER_ID)
    seen = [target.messages[-1]]
    for _ in range(3):
        pressed = await _press_skip_item(db)
        if not pressed.messages:
            break
        text = pressed.messages[-1]
        assert text not in seen, "identical wizard screen re-rendered after skip"
        seen.append(text)


# ---------------------------------------------------------------------------
# F-01 — generic: deferral ordering and terminality across MULTIPLE items
# ---------------------------------------------------------------------------


async def test_f01_generic_deferred_item_returns_after_others_then_terminates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await user_model.set_fact(
        db, USER_ID, "weight_kg", 58.0,
        kind=user_model.KIND_FACT, source=user_model.SOURCE_APPLE_HEALTH, confirmed=False,
    )
    await _seed_single_pending_item(db)

    target = FakeTarget()
    await health_jobs.ask_next_health_confirm_step(target, USER_ID)
    first_question = target.messages[-1]

    # Skip the first item: with another pending item available, the wizard
    # must advance to it (not re-ask, not finish).
    after_skip = await _press_skip_item(db)
    assert after_skip.messages
    assert after_skip.messages[-1] != first_question

    # Deferred bookkeeping is explicit and the skipped-terminal list is empty.
    assert await health_jobs._wizard_deferred_steps(USER_ID) != []
    assert await health_jobs._wizard_skipped_steps(USER_ID) == []


async def test_f01_generic_every_item_skipped_twice_ends_wizard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Terminality holds for the whole wizard, not one item: two skips per
    item always terminate, regardless of item count or kind."""
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await user_model.set_fact(
        db, USER_ID, "weight_kg", 58.0,
        kind=user_model.KIND_FACT, source=user_model.SOURCE_APPLE_HEALTH, confirmed=False,
    )
    await _seed_single_pending_item(db)

    target = FakeTarget()
    assert await health_jobs.ask_next_health_confirm_step(target, USER_ID) is True

    for _ in range(8):  # 2 items × 2 skips each = 4 presses; bound the loop
        await _press_skip_item(db)
        state = await onboarding_bot.get_flow_state(USER_ID, health_jobs.HEALTH_CONFIRM_FLOW)
        if not state:
            break
    state = await onboarding_bot.get_flow_state(USER_ID, health_jobs.HEALTH_CONFIRM_FLOW)
    assert not state, "wizard flow state must be cleared after finite skips"


# ---------------------------------------------------------------------------
# F-02 / F-03 — control refusals are visible and trace-evident, never silent
# (production evidence: events 590/601 duplicate ✅ שמור; 267/275 stale press)
# ---------------------------------------------------------------------------

from types import SimpleNamespace  # noqa: E402

import conversation  # noqa: E402
import event_log  # noqa: E402
from noam_coach.observability import ObservabilityMode, interaction_scope, set_mode  # noqa: E402
from noam_coach.observability.modes import reset_mode  # noqa: E402
from noam_coach.observability.taxonomy import (  # noqa: E402
    DECISION_FINALIZED,
    DELIVERY_SUCCEEDED,
)


class RouterQuery:
    """Query double for the REAL handle_callback (records answers + edits)."""

    def __init__(self, data: str, message_id: int = 900) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=USER_ID)
        self.message = SimpleNamespace(
            message_id=message_id, chat=SimpleNamespace(id=USER_ID), chat_id=USER_ID,
        )
        self.answers: list[str | None] = []
        self.edits: list[str] = []

    async def edit_message_text(self, text: str, reply_markup=None, parse_mode=None) -> None:  # noqa: ANN001
        self.edits.append(text)

    async def edit_message_reply_markup(self, reply_markup=None) -> None:  # noqa: ANN001
        return None

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        self.answers.append(text)


def _router_update(query: RouterQuery) -> SimpleNamespace:
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=USER_ID, first_name="T", username=None),
        effective_chat=SimpleNamespace(id=USER_ID),
        effective_message=query.message,
        callback_query=query,
    )


@pytest.fixture
def _obs_content_mode(monkeypatch: pytest.MonkeyPatch):
    set_mode(ObservabilityMode.CONTENT)
    from config import SETTINGS

    monkeypatch.setattr(SETTINGS, "telegram_allowed_user_id", USER_ID, raising=False)
    # Debounce state is process-global — isolate per test.
    from noam_coach.bot import callback_router

    callback_router._LAST_CALLBACK.clear()
    yield
    callback_router._LAST_CALLBACK.clear()
    reset_mode()


async def _refusals(db: Database) -> list:
    events = await event_log.list_events(db, USER_ID, event=DECISION_FINALIZED)
    return [e for e in events if e.outcome == "refused" and e.entity == "ui_control"]


async def test_f02_consumed_approval_press_gets_terminal_edit_and_refusal_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _obs_content_mode
) -> None:
    """The exact event-601 shape: approve_meal pressed after the approval was
    consumed — must edit the card to a terminal state and record a refusal."""
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)

    query = RouterQuery("approve_meal:consumed123")
    with interaction_scope(user_id=USER_ID):
        await coach_bot.handle_callback(_router_update(query), SimpleNamespace(job_queue=None, bot=None))

    assert any("כבר טופלה" in text for text in query.edits), query.edits
    refusals = await _refusals(db)
    assert len(refusals) == 1
    assert refusals[0].properties["reason"] == "approval_already_handled"
    assert "consumed123" not in str(refusals[0].properties)  # digest, not raw
    meals = await db.fetch_all("SELECT COUNT(*) AS c FROM meals WHERE user_id=?", (USER_ID,))
    assert meals[0]["c"] == 0  # idempotency preserved — refusal changed nothing


async def test_f02_duplicate_tap_gets_visible_toast_and_refusal_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _obs_content_mode
) -> None:
    """A rapid second press of the same debounced control answers with a
    visible toast (first answer of that query) instead of pure silence."""
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)

    first = RouterQuery("approve_meal:dup999")
    with interaction_scope(user_id=USER_ID):
        await coach_bot.handle_callback(_router_update(first), SimpleNamespace(job_queue=None, bot=None))

    second = RouterQuery("approve_meal:dup999")
    with interaction_scope(user_id=USER_ID):
        await coach_bot.handle_callback(_router_update(second), SimpleNamespace(job_queue=None, bot=None))

    # The duplicate's FIRST answer carries the toast (Telegram shows only the
    # first answer to a query, so ordering is the guarantee that it displays).
    assert second.answers and second.answers[0], second.answers
    assert "ללחוץ שוב" in second.answers[0]
    reasons = [r.properties["reason"] for r in await _refusals(db)]
    assert reasons == ["approval_already_handled", "duplicate_tap"]


async def test_f02_normal_callbacks_are_not_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _obs_content_mode
) -> None:
    """False-positive proof: distinct presses in sequence pass through the
    dup gate untouched (the relocated check must not swallow real taps)."""
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)

    for data in ("menu:home", "menu:status", "menu:home"):
        query = RouterQuery(data)
        with interaction_scope(user_id=USER_ID):
            await coach_bot.handle_callback(_router_update(query), SimpleNamespace(job_queue=None, bot=None))
        assert query.edits or query.answers  # something happened, not a refusal
    assert await _refusals(db) == []


async def test_f03_stale_versioned_control_emits_canonical_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _obs_content_mode
) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    monkeypatch.setattr(conversation, "DB", db, raising=False)

    await conversation.set_active_flow(db, USER_ID, conversation.FlowName.goal_review, step="review")
    flow = await conversation.get_active_flow(db, USER_ID)
    stale = conversation.encode_callback(
        "onb", "edit", flow_id=flow.flow_id, version=flow.version + 5
    )
    query = RouterQuery(stale)
    with interaction_scope(user_id=USER_ID):
        await coach_bot.handle_callback(_router_update(query), SimpleNamespace(job_queue=None, bot=None))

    refusals = await _refusals(db)
    assert len(refusals) == 1
    assert refusals[0].properties["reason"] == "stale_version"
    # The pre-existing visible recovery is unchanged.
    legacy = await event_log.list_events(db, USER_ID, event="stale_callback_recovered")
    assert len(legacy) == 1


async def test_f06_stale_wizard_version_press_is_visibly_recovered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _obs_content_mode
) -> None:
    """F-06 investigation record: the suspected 'parallel card / stale
    version' dead press is NOT reproducible — a wizard control carrying an
    outdated version gets the explicit stale-recovery path (visible text +
    legacy event + canonical refusal), not silence. The production silence
    at event 275 is therefore attributed to an unhandled exception, which
    was invisible before R1 error capture and is recorded since."""
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    monkeypatch.setattr(conversation, "DB", db, raising=False)

    await conversation.set_active_flow(
        db, USER_ID, conversation.FlowName.workout_plan_selection, step="choose_structure",
    )
    flow = await conversation.get_active_flow(db, USER_ID)
    stale = conversation.encode_callback(
        "planv2", "wiz_type", "consistency", flow_id=flow.flow_id, version=flow.version + 1,
    )
    query = RouterQuery(stale)
    with interaction_scope(user_id=USER_ID):
        await coach_bot.handle_callback(_router_update(query), SimpleNamespace(job_queue=None, bot=None))

    legacy = await event_log.list_events(db, USER_ID, event="stale_callback_recovered")
    assert len(legacy) == 1
    refusals = await _refusals(db)
    assert [r.properties["reason"] for r in refusals] == ["stale_version"]
    visible = " ".join(query.edits) + " ".join(t for t in query.answers if t)
    assert "כבר לא פעילים" in visible  # never silent


async def test_f03_visible_toast_is_recorded_as_callback_ack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _obs_content_mode
) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    from noam_coach.bot.ui import safe_answer_callback

    query = RouterQuery("whatever")
    with interaction_scope(user_id=USER_ID):
        await safe_answer_callback(query, "הפעולה כבר בביצוע")
        await safe_answer_callback(query)  # empty spinner-stop: NOT evented

    acks = await event_log.list_events(db, USER_ID, event=DELIVERY_SUCCEEDED)
    ack_events = [e for e in acks if e.entity == "callback_ack"]
    assert len(ack_events) == 1
    assert ack_events[0].properties["operation"] == "callback_ack"
    assert ack_events[0].properties["content"]["text"] == "הפעולה כבר בביצוע"
    assert ack_events[0].interaction_id is not None  # correlated, not orphaned
