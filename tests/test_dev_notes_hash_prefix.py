"""A "##"-prefixed message is developer commentary, never user data.

Evidence this locks down (live session, 2026-07-26): a comment about the
bot's meal suggestions was classified as a dietary preference and persisted
into ``diet_restrictions``, then echoed back on the profile screen as if the
user had declared it. Another was stored as a meal.

The hard requirement beyond "don't store it as data" is that a note typed
*during* an active flow must not disturb that flow -- the bot re-asks
whatever it was asking. That is what the mid-flow tests below verify, since
``ConversationRouter.route`` can expire and clear a flow as a side effect and
is therefore the boundary the guard must sit above.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import coach_bot
import conversation
import user_model
from db import Database
from helpers import utc_now
from noam_coach.bot import assistant as assistant_bot
from noam_coach.bot import meal_text as meal_text_bot
from noam_coach.services import core as core_services
from noam_coach.services import dev_notes as dev_notes_service


class FakeMessage:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.replies: list[str] = []
        self.message_id = 555
        self.chat = self

    async def reply_text(
        self,
        body: str,
        reply_markup: Any = None,
        parse_mode: str | None = None,
    ) -> "FakeMessage":
        del reply_markup, parse_mode
        self.replies.append(body)
        return self

    async def send_action(self, *_a: Any, **_k: Any) -> None:
        return None


class FakeUpdate:
    def __init__(self, text: str, user_id: int = 1) -> None:
        self.effective_message = FakeMessage(text)
        self.effective_user = type("U", (), {"id": user_id})()
        self.effective_chat = type("C", (), {"id": user_id})()


async def _make_db(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "dev_notes.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1, 'Noam', NULL, ?)",
        (utc_now(),),
    )
    return db


def _bind(monkeypatch: pytest.MonkeyPatch, db: Database) -> None:
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(assistant_bot, "DB", db)
    monkeypatch.setattr(meal_text_bot, "DB", db)
    monkeypatch.setattr(core_services, "DB", db)


def _allow_text_message(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _is_allowed(_update: Any) -> bool:
        return True

    async def _ensure_user(_update: Any) -> int:
        return 1

    async def _track_event(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(coach_bot, "is_allowed", _is_allowed)
    monkeypatch.setattr(coach_bot, "ensure_user", _ensure_user)
    monkeypatch.setattr(coach_bot, "track_event", _track_event)


def _forbid_routing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly if a dev note ever reaches the router.

    ConversationRouter.route is the exact boundary the guard must precede --
    it calls expire_if_needed, which can clear an active flow. If routing is
    reached, the note has already been treated as a conversational turn.
    """

    async def _explode(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("dev note reached ConversationRouter.route")

    monkeypatch.setattr(conversation.ConversationRouter, "route", _explode)


async def _notes(db: Database) -> list[dict[str, Any]]:
    return await db.fetch_all("SELECT * FROM dev_notes ORDER BY id")


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------
def test_leading_hash_hash_is_a_dev_note() -> None:
    assert dev_notes_service.is_dev_note("##לצמצם את הכתב")
    assert dev_notes_service.is_dev_note("  ##עם רווח מוביל")


def test_wrapped_single_hash_is_a_dev_note() -> None:
    """The form that is actually typed.

    The guard originally accepted only '##'. In the 2026-07-27 session all
    five developer notes used '#...#' and every one fell through to intent
    classification -- one was stored as a confirmed nutrition fact, another
    regenerated the weekly workout plan mid-set.
    """
    assert dev_notes_service.is_dev_note("#לצמצם את הכתב#")
    assert dev_notes_service.is_dev_note(
        "#לדעתי אמרתי לו שאני מתאמן בשישי לא בשבת#"
    )


def test_bare_leading_single_hash_is_not_a_dev_note() -> None:
    """Without a closing marker a single '#' is ordinary text.

    "#3 בבוקר" is a plausible thing to type at a coach; requiring the closing
    hash is what makes the single-hash form safe to accept at all.
    """
    assert not dev_notes_service.is_dev_note("#לצמצם את הכתב")
    assert not dev_notes_service.is_dev_note("#3 בבוקר")


def test_lone_hash_is_not_a_dev_note() -> None:
    assert not dev_notes_service.is_dev_note("#")
    assert not dev_notes_service.is_dev_note("")


def test_wrapped_note_strips_both_markers() -> None:
    assert dev_notes_service.strip_prefix("#לצמצם את הכתב#") == "לצמצם את הכתב"


def test_hash_inside_text_is_not_a_dev_note() -> None:
    """A '#' mid-message is ordinary user text and must stay routable."""
    assert not dev_notes_service.is_dev_note("כמה קלוריות יש ב#1?")
    assert not dev_notes_service.is_dev_note("אכלתי סטייק ## טעים")


@pytest.mark.parametrize(
    "note",
    [
        "#הייתי רוצה שישאל אותי על כולם איך היתי רוצה שאתייחס הוא שאל רק על חציל#",
        "#לדעתי אמרתי לו שאני מתאמן בשישי לא בשבת#",
        "#זה ארוחה שלא אישרתי לכן לא אמור להופיע לי ההודעה הזאת#",
        "#אני רוצה שנחשוב איך בסט מפוצל נוכל לעשות את זה בלי לחצנים לשקל#",
        "#הוא לא זיהה שיום שבת השעת אימון שלי שונה אני מתאמן בבוקר#",
    ],
)
def test_every_real_note_from_the_live_session_is_caught(note: str) -> None:
    """The exact five messages the guard failed to catch in production.

    Each of these was classified as user speech instead. Their real
    consequences: one became `user_facts.food_environment_context` with
    confirmed=1, one was classified `build_plan` and rebuilt the training
    week mid-set, three were answered with a help menu.
    """
    assert dev_notes_service.is_dev_note(note)
    assert "#" not in dev_notes_service.strip_prefix(note)


def test_strip_prefix_removes_marker_only() -> None:
    assert dev_notes_service.strip_prefix("##  לצמצם את הכתב  ") == "לצמצם את הכתב"


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_dev_note_is_stored_and_acknowledged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path)
    _bind(monkeypatch, db)
    _allow_text_message(monkeypatch)
    _forbid_routing(monkeypatch)

    update = FakeUpdate("##לצמצם את הכתב")
    await meal_text_bot.handle_text_message(update, None)

    rows = await _notes(db)
    assert len(rows) == 1
    assert rows[0]["text"] == "לצמצם את הכתב"
    assert update.effective_message.replies == [dev_notes_service.ACK_TEXT]


@pytest.mark.asyncio
async def test_dev_note_never_becomes_a_dietary_restriction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact live-session corruption: a comment stored as a user fact."""
    db = await _make_db(tmp_path)
    _bind(monkeypatch, db)
    _allow_text_message(monkeypatch)
    _forbid_routing(monkeypatch)

    comment = (
        "##ההצעות שלו לא מותאמות לנורמות בחברה הוא מציע לי חזה עוף ב8 בבוקר "
        "ואני לא מתחיל לאכול ב8"
    )
    await meal_text_bot.handle_text_message(FakeUpdate(comment), None)

    fact = await user_model.get_fact(db, 1, "diet_restrictions")
    assert fact is None or "##" not in str(fact.get("value", ""))

    meals = await db.fetch_all("SELECT * FROM meals WHERE user_id=1")
    assert meals == []


@pytest.mark.asyncio
async def test_dev_note_is_length_capped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path)
    _bind(monkeypatch, db)
    _allow_text_message(monkeypatch)
    _forbid_routing(monkeypatch)

    await meal_text_bot.handle_text_message(
        FakeUpdate("##" + "א" * (dev_notes_service.MAX_NOTE_LENGTH + 500)), None
    )

    rows = await _notes(db)
    assert len(rows[0]["text"]) == dev_notes_service.MAX_NOTE_LENGTH


# ---------------------------------------------------------------------------
# Mid-flow preservation -- the load-bearing requirement
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_dev_note_mid_flow_leaves_the_flow_armed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A note typed while the bot awaits a weight must not advance or clear it."""
    db = await _make_db(tmp_path)
    _bind(monkeypatch, db)
    _allow_text_message(monkeypatch)
    _forbid_routing(monkeypatch)

    await conversation.set_active_flow(
        db,
        1,
        conversation.FlowName.workout_session,
        step="await_weight",
        payload={"exercise_index": 2},
    )
    before = await conversation.get_active_flow(db, 1)

    await meal_text_bot.handle_text_message(FakeUpdate("##תוריד את הRIR"), None)

    after = await conversation.get_active_flow(db, 1)
    assert after.name == before.name == "workout_session"
    assert after.step == before.step == "await_weight"
    assert after.payload == before.payload
    assert after.version == before.version, "the flow must not advance"


@pytest.mark.asyncio
async def test_dev_note_records_the_active_flow_as_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path)
    _bind(monkeypatch, db)
    _allow_text_message(monkeypatch)
    _forbid_routing(monkeypatch)

    await conversation.set_active_flow(
        db,
        1,
        conversation.FlowName.workout_session,
        step="await_weight",
        payload={"exercise_index": 2},
    )
    await meal_text_bot.handle_text_message(FakeUpdate("##הערה באמצע אימון"), None)

    rows = await _notes(db)
    context = json.loads(rows[0]["context"])
    assert context["flow"] == "workout_session"
    assert context["step"] == "await_weight"


def test_flow_context_never_captures_payload_content() -> None:
    """Payload can hold user meal text; context must stay structural."""

    class _Flow:
        name = "meal_flow"
        step = "await_correction"
        flow_id = "abc123"
        payload = {"raw_text": "אכלתי 200 גרם סלמון"}

    context = dev_notes_service.flow_context(_Flow())

    assert context == {"flow": "meal_flow", "step": "await_correction", "flow_id": "abc123"}
    assert "סלמון" not in json.dumps(context, ensure_ascii=False)


@pytest.mark.asyncio
async def test_ordinary_text_still_reaches_the_router(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard must not swallow normal messages."""
    db = await _make_db(tmp_path)
    _bind(monkeypatch, db)
    _allow_text_message(monkeypatch)

    routed: list[str] = []

    async def _route(*_a: Any, **_k: Any) -> Any:
        routed.append("routed")
        raise RuntimeError("stop after routing")

    monkeypatch.setattr(conversation.ConversationRouter, "route", _route)

    with pytest.raises(RuntimeError):
        await meal_text_bot.handle_text_message(FakeUpdate("אכלתי סלמון"), None)

    assert routed == ["routed"]
    assert await _notes(db) == []
