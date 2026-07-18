"""Regression coverage for the 2026-07-18 production audit findings.

Each test cites the finding (F-A1..F-A9) from
docs/session_audits/2026-07-18/SANITIZED_SESSION_AUDIT.md that it locks in.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import coach_bot
import conversation
import event_log
from db import Database
from helpers import utc_now
from models import FoodItem, MealAnalysis
from noam_coach.bot import callback_meals as callback_meals_bot
from noam_coach.bot import meals as meals_bot
from noam_coach.observability import ObservabilityMode, interaction_scope, set_mode
from noam_coach.observability.modes import reset_mode
from noam_coach.observability.taxonomy import DECISION_FINALIZED, STATE_MUTATED
from noam_coach.services import core as core_services
from noam_coach.services.core import create_approval, fetch_approval_any, set_meal_fix
from noam_coach.services.meal_approval_lifecycle import (
    bump_revision,
    is_stale_revision,
    parse_decision_token,
)

USER_ID = 1


class FakeMessage:
    def __init__(self) -> None:
        self.replies: list[str] = []
        self.reply_markups: list[Any] = []

    async def reply_text(self, text: str, reply_markup: Any = None, parse_mode: Any = None) -> "FakeMessage":
        self.replies.append(text)
        self.reply_markups.append(reply_markup)
        return self


class FakeQuery:
    def __init__(self, data: str = "") -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=USER_ID)
        self.message = FakeMessage()
        self.edits: list[str] = []
        self.edit_markups: list[Any] = []

    async def edit_message_text(self, text: str, reply_markup: Any = None, parse_mode: Any = None) -> None:
        self.edits.append(text)
        self.edit_markups.append(reply_markup)

    async def answer(self, text: Any = None, show_alert: bool = False) -> None:
        return None


def _markup_callbacks(markup: Any) -> list[str]:
    if markup is None:
        return []
    return [b.callback_data for row in markup.inline_keyboard for b in row]


@pytest.fixture
async def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Database:
    database = Database(str(tmp_path / "audit.db"))
    await database.init()
    await database.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(?, 'Test', NULL, ?)",
        (USER_ID, utc_now()),
    )
    monkeypatch.setattr(coach_bot, "DB", database)
    monkeypatch.setattr(core_services, "DB", database)
    monkeypatch.setattr(callback_meals_bot, "DB", database)
    monkeypatch.setattr(meals_bot, "DB", database)
    monkeypatch.setattr(conversation, "DB", database, raising=False)
    from config import SETTINGS

    monkeypatch.setattr(SETTINGS, "telegram_allowed_user_id", USER_ID, raising=False)
    set_mode(ObservabilityMode.CONTENT)
    yield database
    reset_mode()


def _analysis(name: str = "שניצל") -> MealAnalysis:
    # The card title is composed from ITEM names — the first item carries
    # the distinctive name so per-meal assertions can address the card.
    return MealAnalysis(
        meal_name=name,
        items=[
            FoodItem(name=name, grams=180.0, calories=430.0, protein=32.0,
                     carbs=14.0, fat=26.0, confidence=0.9),
            FoodItem(name="קוסקוס", grams=150.0, calories=180.0, protein=6.0,
                     carbs=36.0, fat=1.0, confidence=0.9),
        ],
        confidence=0.9,
    )


async def _create_meal_approval(
    db: Database, *, name: str = "שניצל וקוסקוס", revision: int = 0,
    image: str | None = None,
) -> str:
    payload: dict[str, Any] = {
        "analysis": _analysis(name).model_dump(),
        "eaten_at": utc_now(),
        "revision": revision,
    }
    if image:
        payload["image"] = image
    return await create_approval(USER_ID, "meal", payload)


async def _decision_events(db: Database, outcome: str) -> list[Any]:
    events = await event_log.list_events(db, USER_ID, event=DECISION_FINALIZED)
    return [e for e in events if e.outcome == outcome]


async def _press(data: str) -> FakeQuery:
    query = FakeQuery(data)
    with interaction_scope(user_id=USER_ID):
        handled = await callback_meals_bot.handle_meal_callback(query, USER_ID, data)
    assert handled is True
    return query


# ---------------------------------------------------------------------------
# F-A1 — approval lifecycle: revisions, truthful terminals, restoration
# ---------------------------------------------------------------------------


def test_fa1_decision_token_roundtrip() -> None:
    assert parse_decision_token("approve_meal:abc-123") == ("abc-123", None)
    assert parse_decision_token("approve_meal:abc-123:r0") == ("abc-123", 0)
    assert parse_decision_token("reject_meal:a_b:r17") == ("a_b", 17)
    row = {"data": {"revision": 2}}
    assert is_stale_revision(row, 1) is True
    assert is_stale_revision(row, 2) is False
    assert is_stale_revision(row, None) is False  # legacy unversioned control
    payload: dict[str, Any] = {}
    assert bump_revision(payload) == 1 and payload["revision"] == 1


async def test_fa1_stale_reject_refused_and_current_card_shown(db: Database) -> None:
    """THE incident shape: a reject pressed on a pre-correction card must
    not consume the corrected approval — it re-renders the current card."""
    approval_id = await _create_meal_approval(db, revision=1)  # corrected once
    query = await _press(f"reject_meal:{approval_id}:r0")  # stale card press

    row = await fetch_approval_any(USER_ID, approval_id)
    assert row["status"] == "pending"  # NOT consumed
    refusals = [
        e for e in await event_log.list_events(db, USER_ID, event=DECISION_FINALIZED)
        if e.outcome == "refused" and e.properties.get("reason") == "stale_approval_revision"
    ]
    assert len(refusals) == 1
    assert any("שניצל" in text for text in query.edits)  # current card re-rendered


async def test_fa1_stale_approve_refused_current_card_shown(db: Database) -> None:
    approval_id = await _create_meal_approval(db, revision=3)
    query = await _press(f"approve_meal:{approval_id}:r1")
    row = await fetch_approval_any(USER_ID, approval_id)
    assert row["status"] == "pending"
    meals = await db.fetch_all("SELECT COUNT(*) AS c FROM meals", ())
    assert meals[0]["c"] == 0  # nothing persisted from a stale press
    assert any("שניצל" in text for text in query.edits)


async def test_fa1_reject_emits_canonical_events_and_deletion_evidence(
    db: Database, tmp_path: Path
) -> None:
    image = tmp_path / "meal.jpg"
    image.write_bytes(b"fake-jpeg-bytes")
    approval_id = await _create_meal_approval(db, image=str(image))

    query = await _press(f"reject_meal:{approval_id}:r0")

    assert not image.exists()  # deletion still happens (privacy by design)
    deleted = [
        e for e in await event_log.list_events(db, USER_ID, event=STATE_MUTATED)
        if e.properties.get("action") == "deleted" and e.properties.get("domain") == "media"
    ]
    assert len(deleted) == 1  # ...but it is trace evidence now (F-A9)
    assert deleted[0].properties["reason"] == "meal_rejected"
    rejected = await _decision_events(db, "rejected")
    assert len(rejected) == 1 and rejected[0].entity == "meal_approval"
    # The reject confirmation offers explicit restoration.
    assert any("restore_meal:" in cb for cb in _markup_callbacks(query.edit_markups[-1]))


async def test_fa1_press_after_reject_gets_restore_and_restore_works(db: Database) -> None:
    """Eleven presses were swallowed in production; now every one gets the
    truthful terminal with a working restore path — no meal can be lost."""
    approval_id = await _create_meal_approval(db)
    await _press(f"reject_meal:{approval_id}:r0")

    query = await _press(f"approve_meal:{approval_id}:r0")
    assert any("נדחתה קודם" in text for text in query.edits)
    restore_cbs = [cb for cb in _markup_callbacks(query.edit_markups[-1]) if cb.startswith("restore_meal:")]
    assert restore_cbs

    restore_query = await _press(restore_cbs[0])
    # A fresh PENDING approval exists and its card is rendered.
    pending = await db.fetch_all(
        "SELECT id FROM approvals WHERE user_id=? AND status='pending'", (USER_ID,))
    assert len(pending) == 1
    new_id = pending[0]["id"]
    assert new_id != approval_id
    assert any("שניצל" in text for text in restore_query.edits)
    flow = await conversation.get_active_flow(db, USER_ID)
    assert flow.name == conversation.FlowName.meal_correction
    assert flow.step == new_id
    restored_events = [
        e for e in await event_log.list_events(db, USER_ID, event=STATE_MUTATED)
        if e.properties.get("action") == "restored"
    ]
    assert len(restored_events) == 1


async def test_fa1_reject_after_approve_is_truthful(db: Database) -> None:
    approval_id = await _create_meal_approval(db)
    await _press(f"approve_meal:{approval_id}:r0")  # persists the meal
    meals = await db.fetch_all("SELECT COUNT(*) AS c FROM meals", ())
    assert meals[0]["c"] == 1

    query = await _press(f"reject_meal:{approval_id}:r0")
    assert any("כבר נשמרה" in text for text in query.edits)
    assert not any("נדחתה ולא נשמרה" in text for text in query.edits)  # no false claim
    row = await fetch_approval_any(USER_ID, approval_id)
    assert row["status"] == "approved"  # untouched
    meals = await db.fetch_all("SELECT COUNT(*) AS c FROM meals", ())
    assert meals[0]["c"] == 1  # idempotent


# ---------------------------------------------------------------------------
# F-A5 — suspended meal flows are re-presented; decisions are entity-addressed
# ---------------------------------------------------------------------------


async def test_fa5_resumed_meal_card_is_represented(db: Database) -> None:
    """Production: rejecting meal B resumed meal A's flow invisibly and the
    protein drink was lost. The resumed card must be re-presented."""
    approval_a = await _create_meal_approval(db, name="שייק חלבון")
    await set_meal_fix(USER_ID, approval_a)
    approval_b = await _create_meal_approval(db, name="קציצות דג")
    await set_meal_fix(USER_ID, approval_b)  # suspends A

    query = await _press(f"reject_meal:{approval_b}:r0")

    flow = await conversation.get_active_flow(db, USER_ID)
    assert flow.name == conversation.FlowName.meal_correction
    assert flow.step == approval_a  # A resumed...
    replies = " ".join(query.message.replies)
    assert "ממשיכים בארוחה" in replies
    assert any("שייק" in reply for reply in query.message.replies)  # ...and visible


async def test_fa5_decision_addressed_to_other_meal_leaves_active_flow(db: Database) -> None:
    approval_a = await _create_meal_approval(db, name="שייק חלבון")
    await set_meal_fix(USER_ID, approval_a)
    approval_b = await _create_meal_approval(db, name="קציצות דג")
    # B is pending but NOT the active flow (no set_meal_fix for B).

    await _press(f"reject_meal:{approval_b}:r0")

    row_b = await fetch_approval_any(USER_ID, approval_b)
    assert row_b["status"] == "rejected"
    flow = await conversation.get_active_flow(db, USER_ID)
    assert flow.name == conversation.FlowName.meal_correction
    assert flow.step == approval_a  # A's flow untouched by B's decision


async def test_fa1_rendered_card_carries_revision_tokens(db: Database) -> None:
    approval_id = await _create_meal_approval(db, revision=2)
    query = FakeQuery()
    await meals_bot.render_meal(query, USER_ID, approval_id)
    callbacks = _markup_callbacks(query.edit_markups[-1])
    assert f"approve_meal:{approval_id}:r2" in callbacks
    assert f"reject_meal:{approval_id}:r2" in callbacks
