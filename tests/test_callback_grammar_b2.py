"""B2 — ARCH-04 (callback grammar) + ARCH-05 (entity-addressed confirmations).

Required regressions:
- flag:sleep:bad / chk:state:fasting pressed while a flow is ACTIVE go
  through the REAL router and reach their handlers (previously refused as
  "stale" because greedy extract_flow_id misread ordinary segments)
- grammar matrix over every production callback family: no invented
  flow/version identity; encode_callback round-trips resolve correctly
- goal confirmation: fresh press (pending match / fresh render) works;
  after the goal changes in another interaction, the OLD control is refused
  with validation.failed and no goal mutation
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import coach_bot
import conversation
import event_log
import health_service
import planning
from db import Database
from helpers import utc_now
from noam_coach.observability import ObservabilityMode, interaction_scope, set_mode
from noam_coach.observability.emit import reset_observability_health
from noam_coach.observability.modes import reset_mode
from noam_coach.observability.telegram_egress import (
    install_telegram_egress,
    uninstall_telegram_egress,
)
from noam_coach.services.callback_grammar import (
    install_callback_grammar,
    install_confirmation_gate,
    strict_extract_flow_id,
    strict_extract_version,
    uninstall_callback_grammar,
    uninstall_confirmation_gate,
)

USER_ID = 1


class FakeMessage:
    def __init__(self, message_id: int = 900) -> None:
        self.message_id = message_id
        self.chat = SimpleNamespace(id=USER_ID)
        self.chat_id = USER_ID
        self.replies: list[str] = []
        self._next = message_id + 100

    async def reply_text(self, text: str, reply_markup: Any = None, parse_mode: str | None = None) -> "FakeMessage":
        self.replies.append(text)
        self._next += 1
        return FakeMessage(self._next)


class FakeQuery:
    def __init__(self, data: str = "", message_id: int = 900) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=USER_ID)
        self.message = FakeMessage(message_id)
        self.edits: list[str] = []

    async def edit_message_text(self, text: str, reply_markup: Any = None, parse_mode: str | None = None) -> None:
        self.edits.append(text)

    async def edit_message_reply_markup(self, reply_markup: Any = None) -> None:
        return None

    async def answer(self, *a: Any, **k: Any) -> None:
        return None


def _update(query: FakeQuery) -> SimpleNamespace:
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=USER_ID, first_name="T", username=None),
        effective_chat=SimpleNamespace(id=USER_ID),
        effective_message=query.message,
        callback_query=query,
    )


@pytest.fixture
async def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Database:
    database = Database(str(tmp_path / "b2.db"))
    await database.init()
    await database.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(?, 'Test', NULL, ?)",
        (USER_ID, utc_now()),
    )
    monkeypatch.setattr(coach_bot, "DB", database)
    monkeypatch.setattr(health_service, "DB", database, raising=False)
    from config import SETTINGS

    monkeypatch.setattr(SETTINGS, "telegram_allowed_user_id", USER_ID, raising=False)
    return database


@pytest.fixture(autouse=True)
def _isolation():
    set_mode(ObservabilityMode.CONTENT)
    reset_observability_health()
    install_callback_grammar()
    install_confirmation_gate()
    yield
    uninstall_confirmation_gate()
    uninstall_callback_grammar()
    uninstall_telegram_egress()
    reset_mode()
    reset_observability_health()


# ---------------------------------------------------------------------------
# ARCH-04: grammar
# ---------------------------------------------------------------------------

# Representative sample of EVERY produced callback family (mechanical
# inventory of button()/callback_data literals at current HEAD).
_PRODUCTION_CALLBACKS = [
    "menu:home", "menu:more", "menu:settings", "menu:flags", "menu:nextmeal",
    "menu:daily_menu", "menu:refresh_daily_menu", "menu:now", "menu:about",
    "flag:sleep:good", "flag:sleep:bad", "flag:med", "flag:pain", "flag:done",
    "chk:med:0", "chk:med_other", "chk:sleep:good", "chk:sleep:bad",
    "chk:state:normal", "chk:state:pain", "chk:state:fasting",
    "chk:energy:high", "chk:energy:low",
    "nextmeal:qty:1:0.8", "nextmeal:choose:2", "nextmeal:save:1",
    "nextmeal:dislike:1", "nextmeal:dislikeitem:1:2", "nextmeal:nostock:1",
    "nextmeal:smaller:1", "nextmeal:bigger:1", "nextmeal:refresh",
    "nextmeal:wkt:later", "nextmeal:wkt:done", "nextmeal:editqty:2",
    "dailymenu:save:menu-1-2026-06-28-3:breakfast-0",
    "approve_meal:abc123", "reject_meal:abc123", "fixmeal:abc123",
    "force_approve_meal:abc123", "backmeal:abc123", "editmeal:55",
    "dup:new:abc123", "reject_dup:abc123",
    "approve_goal:abc123", "reject_goal:abc123",
    "confirm:goal_cal:2400", "confirm:weight:98.5", "confirm:cancel:0",
    "plan:set:3", "plan:recommend",
    "planv2:activate:12", "planv2:select:12", "planv2:profile",
    "planv2:complete_missing",
    "workout:A", "workout:F", "startworkout:F",
    "editparams:F:2", "editparams_menu:F",
    # workout-selection architecture, Batch 4. Tier-1 carries the immutable
    # plan_versions id; Tier-2 carries the 8-hex fact fingerprint. Both sit in
    # NON-terminal positions, so neither can be mistaken for the terminal
    # `^v\d{1,9}$` version token or the `^ff-\d+-[0-9a-f]{6,}$` flow token.
    "wk:list", "wk:sel:12:0", "wk:start:12:0",
    "wk:fsel:1a2b3c4d:0", "wk:fstart:1a2b3c4d:2",
    # Batch 5 repeat-confirmation variants: `again` sits in the terminal
    # position the grammar inspects, and is neither `v<digits>` nor a flow id.
    "wk:start:12:0:again", "wk:fstart:1a2b3c4d:2:again",
    "setok:5:0:1", "different:5:0:1", "qtydelta:55:0:+10",
    "wparamtext:F:2", "param:F:2:weight",
    "onb:edit_menu", "onb:edit:field", "routine:confirm", "routine:fix",
    "qa:goal_phase:1", "goal:approve", "health:confirm", "health:skip_item",
    "reconcile_ok:3", "reconcile_no:3", "undo_meal:55", "clarify:abc:1",
    "restadd:5:30", "sub:5:0:1", "wpause:5", "wdone:5", "wcancel:5",
    "cancelfix:abc123", "editqtymenu:abc123:0", "editqty:abc123:0:150",
]


def test_grammar_matrix_no_production_callback_invents_identity() -> None:
    for data in _PRODUCTION_CALLBACKS:
        assert strict_extract_version(data) is None, data
        assert strict_extract_flow_id(data) is None, data


def test_encode_callback_round_trip_resolves_identity() -> None:
    flow_id = "f-1-a1b2c3d4e5f6"
    data = conversation.encode_callback("onb", "confirm", "basics", flow_id=flow_id, version=7)
    assert strict_extract_flow_id(data) == flow_id
    assert strict_extract_version(data) == 7
    # Version-only and flow-only forms too.
    assert strict_extract_version(conversation.encode_callback("onb", "x", version=3)) == 3
    assert strict_extract_flow_id(conversation.encode_callback("onb", "x", flow_id=flow_id)) == flow_id
    # And after install, the conversation module attributes ARE the strict ones.
    assert conversation.extract_version(data) == 7
    assert conversation.extract_flow_id(data) == flow_id


def test_greedy_misparse_cases_are_fixed() -> None:
    # The exact historical false positives.
    assert strict_extract_flow_id("flag:sleep:good") is None
    assert strict_extract_flow_id("chk:state:fasting") is None
    assert strict_extract_flow_id("menu:flags") is None
    # A payload value that merely LOOKS like a version stays payload.
    assert strict_extract_version("editqty:abc:v2something") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("callback_data", ["flag:sleep:bad", "chk:state:fasting"])
async def test_checkin_callbacks_work_through_real_router_with_active_flow(
    db: Database, callback_data: str,
) -> None:
    """THE ARCH-04 user-visible regression: with any active flow, these
    callbacks were refused as stale. Through the REAL handle_callback they
    must now reach their handlers and record the flag."""
    await conversation.set_active_flow(
        db, USER_ID, conversation.FlowName.meal_correction, step="appr-1",
        payload={"refine_count": 0},
    )
    query = FakeQuery(callback_data)
    context = SimpleNamespace(job_queue=None, bot=None)

    await coach_bot.handle_callback(_update(query), context)

    stale = await event_log.list_events(db, USER_ID, event="stale_callback_recovered")
    assert stale == []  # never refused as stale
    day = health_service.local_day_str()
    flags = await health_service.get_daily_flags(USER_ID, day)
    if callback_data == "flag:sleep:bad":
        assert flags.get("sleep_quality") == "bad"
    else:
        assert flags.get("fasting") is True
    # The user got a real response, not the stale-callback apology.
    all_text = " ".join(query.edits + query.message.replies)
    assert "כבר לא פעילים" not in all_text
    assert all_text  # something rendered


@pytest.mark.asyncio
async def test_versioned_onboarding_callback_still_stale_checked(db: Database) -> None:
    """The strict grammar must not break the REAL versioned family: an
    encode_callback-produced control from an outdated flow version is still
    refused by the router's stale gate."""
    await conversation.set_active_flow(
        db, USER_ID, conversation.FlowName.goal_review, step="review",
    )
    flow = await conversation.get_active_flow(db, USER_ID)
    stale_data = conversation.encode_callback(
        "onb", "edit", flow_id=flow.flow_id, version=flow.version + 5,
    )
    query = FakeQuery(stale_data)
    await coach_bot.handle_callback(_update(query), SimpleNamespace(job_queue=None, bot=None))
    stale = await event_log.list_events(db, USER_ID, event="stale_callback_recovered")
    assert len(stale) == 1
    assert stale[0].properties["old_version"] == flow.version + 5


# ---------------------------------------------------------------------------
# ARCH-05: entity-addressed confirmations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fresh_confirmation_with_pending_state_is_allowed(
    db: Database, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from noam_coach.bot import onboarding as onboarding_bot

    monkeypatch.setattr(onboarding_bot, "DB", db, raising=False)
    activated: list[int] = []

    async def fake_activate(db_: Any, user_id: int, goal_id: int) -> None:
        activated.append(goal_id)

    monkeypatch.setattr(planning, "activate_goal", fake_activate)
    await coach_bot.set_confirm_pending(USER_ID, {"value": 2400.0, "text": "2400"})

    query = FakeQuery("confirm:goal_cal:2400")
    handled = await coach_bot.handle_menu_callback(query, USER_ID, "confirm:goal_cal:2400")
    assert handled is True
    assert activated  # the confirmation reached the real handler
    assert any("עודכן יעד" in text for text in query.edits)


@pytest.mark.asyncio
async def test_stale_goal_confirmation_is_refused_after_goal_changed(
    db: Database, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """render delivered → goal changes in another interaction → OLD control
    pressed → refused; no goal mutation; validation.failed emitted."""
    install_telegram_egress()
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    # Seed the goal that existed when the confirmation was rendered.
    await db.execute(
        """
        INSERT INTO goal_versions(user_id, calories, protein, steps, phase, status, source, created_at, decided_at)
        VALUES(?, 2000, 150, 8000, 'fat_loss_muscle_retention', 'active', 'test', ?, ?)
        """,
        (USER_ID, utc_now(), utc_now()),
    )
    # Deliver the confirmation render (through the real instrumented egress).
    render_query = FakeQuery(message_id=700)
    with interaction_scope(user_id=USER_ID):
        await coach_bot.safe_edit(
            render_query, "לוודא: יעד של 2600?",
            InlineKeyboardMarkup([[InlineKeyboardButton("✅ אשר", callback_data="confirm:goal_cal:2600")]]),
        )

    # The goal changes in ANOTHER interaction afterwards (supersede + activate,
    # matching the real activation transaction shape).
    await db.execute(
        "UPDATE goal_versions SET status='superseded', decided_at=? WHERE user_id=? AND status='active'",
        (utc_now(), USER_ID),
    )
    await db.execute(
        """
        INSERT INTO goal_versions(user_id, calories, protein, steps, phase, status, source, created_at, decided_at)
        VALUES(?, 2200, 160, 8000, 'fat_loss_muscle_retention', 'active', 'newer', ?, ?)
        """,
        (USER_ID, utc_now(), utc_now()),
    )

    activated: list[int] = []

    async def fake_activate(db_: Any, user_id: int, goal_id: int) -> None:
        activated.append(goal_id)

    monkeypatch.setattr(planning, "activate_goal", fake_activate)

    # Old control pressed (same message the render was delivered on).
    press_query = FakeQuery("confirm:goal_cal:2600", message_id=700)
    with interaction_scope(user_id=USER_ID):
        handled = await coach_bot.handle_menu_callback(press_query, USER_ID, "confirm:goal_cal:2600")

    assert handled is True
    assert activated == []  # the stale confirmation never mutated the goal
    goals = await db.fetch_all("SELECT calories FROM goal_versions WHERE user_id=?", (USER_ID,))
    assert sorted(g["calories"] for g in goals) == [2000, 2200]  # no 2600 row
    refused = [e for e in await event_log.list_events(db, USER_ID, event="validation.failed")
               if e.entity == "confirmation"]
    assert len(refused) == 1
    assert refused[0].outcome == "stale"
    assert refused[0].properties["kind"] == "goal_cal"
    assert any("כבר לא בתוקף" in text for text in press_query.edits)
    goal_events = [e for e in await event_log.list_events(db, USER_ID)
                   if e.entity == "goal" and e.event in ("GOAL_MANUALLY_CHANGED", "goal_change_confirmed")]
    assert goal_events == []


@pytest.mark.asyncio
async def test_fresh_render_confirmation_without_pending_is_allowed(
    db: Database, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The onboarding goal-question producer shows confirm buttons WITHOUT a
    pending confirmation. When nothing changed since the render was
    delivered, the press must go through (rule 3)."""
    install_telegram_egress()
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    await db.execute(
        """
        INSERT INTO goal_versions(user_id, calories, protein, steps, phase, status, source, created_at, decided_at)
        VALUES(?, 2000, 150, 8000, 'fat_loss_muscle_retention', 'active', 'test', ?, ?)
        """,
        (USER_ID, utc_now(), utc_now()),
    )
    render_query = FakeQuery(message_id=710)
    with interaction_scope(user_id=USER_ID):
        await coach_bot.safe_edit(
            render_query, "לוודא: 2500?",
            InlineKeyboardMarkup([[InlineKeyboardButton("✅ אשר", callback_data="confirm:goal_cal:2500")]]),
        )

    activated: list[int] = []

    async def fake_activate(db_: Any, user_id: int, goal_id: int) -> None:
        activated.append(goal_id)

    monkeypatch.setattr(planning, "activate_goal", fake_activate)
    press_query = FakeQuery("confirm:goal_cal:2500", message_id=710)
    with interaction_scope(user_id=USER_ID):
        handled = await coach_bot.handle_menu_callback(press_query, USER_ID, "confirm:goal_cal:2500")
    assert handled is True
    assert activated  # allowed: nothing changed since the render


@pytest.mark.asyncio
async def test_cancel_confirmation_always_allowed(db: Database) -> None:
    query = FakeQuery("confirm:cancel:0")
    handled = await coach_bot.handle_menu_callback(query, USER_ID, "confirm:cancel:0")
    assert handled is True
    assert any("בוטל" in text for text in query.edits)
