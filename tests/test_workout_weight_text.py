"""TASK-WORKOUT-WEIGHT-TEXT — the lifted weight is TYPED, not picked.

During an active workout the bot asks what weight was lifted and the user
answers with a text message. The predefined weight-SELECTION buttons are gone.

What this suite pins:

* **Grammar** — integer, decimal point, decimal comma, Hebrew kg notation
  (קג / ק״ג), per-hand phrasing, bodyweight, and "אותו משקל" both with and
  without a previous value; plus the safe boundaries.
* **No guessing** — ambiguous or unrelated text is refused, and a refusal
  writes NOTHING and does NOT advance the session.
* **Isolation** — the grammar runs only while an active workout explicitly
  awaits a weight, so meal logging, onboarding and general chat are untouched.
* **Identity & idempotency** — the recorded value lands on the right
  workout/exercise/set, a duplicate Telegram update cannot record or advance
  twice, and a restart mid-question still expects a weight.
* **Keyboards** — no weight-selection buttons at this step; navigation and
  safety controls survive.

Driven through the REAL callback and text entry points, mirroring
tests/test_workout_param_edit_v2.py.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import coach_bot
import conversation
from db import Database
from helpers import utc_now
from noam_coach.bot import callback_session as callback_session_bot
from noam_coach.bot import meal_text as meal_text_bot
from noam_coach.bot import ui as ui_bot
from noam_coach.bot import workout as workout_bot
from noam_coach.services import weight_text

# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class FakeMessage:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.markups: list[Any] = []

    async def reply_text(
        self, text: str, reply_markup: Any = None, parse_mode: str | None = None
    ) -> None:
        del parse_mode
        self.texts.append(text)
        self.markups.append(reply_markup)


class FakeQuery:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.reply_markups: list[Any] = []
        self.answers: list[str | None] = []
        self.message = FakeMessage()

    async def edit_message_text(
        self, text: str, reply_markup: Any = None, parse_mode: str | None = None
    ) -> None:
        del parse_mode
        self.messages.append(text)
        self.reply_markups.append(reply_markup)

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        del show_alert
        self.answers.append(text)


class FakeUpdate:
    """Minimal Update carrying one text message."""

    def __init__(self, text: str) -> None:
        self.effective_message = FakeMessage()
        self.effective_message.text = text  # type: ignore[attr-defined]


def _ctx() -> Any:
    return type("Ctx", (), {"bot": object(), "job_queue": None})()


def _callbacks(markup: Any) -> list[str]:
    if markup is None:
        return []
    return [btn.callback_data for row in markup.inline_keyboard for btn in row]


def _labels(markup: Any) -> list[str]:
    if markup is None:
        return []
    return [btn.text for row in markup.inline_keyboard for btn in row]


async def _make_db(tmp_path: Path, name: str) -> Database:
    db = Database(str(tmp_path / f"{name}.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1, 'Test', NULL, ?)",
        (utc_now(),),
    )
    return db


def _bind(monkeypatch: pytest.MonkeyPatch, db: Database) -> None:
    """Bind the test database everywhere the code can see it.

    ``runtime_bound`` refreshes globals from the ``coach_bot`` facade before
    each call, so the facade binding is the one that matters; the module-level
    ones cover the direct (undecorated) call paths.
    """
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(ui_bot, "DB", db)
    monkeypatch.setattr(workout_bot, "DB", db)
    monkeypatch.setattr(meal_text_bot, "DB", db)
    monkeypatch.setattr(callback_session_bot, "DB", db)


def _ex(exercise_id: str = "db_bench", name: str = "לחיצת חזה") -> dict[str, Any]:
    return {
        "id": exercise_id,
        "name": name,
        "sets": 3,
        "rmin": 8,
        "rmax": 12,
        "rest": 90,
        "weight": 50.0,
        "inc": 2.5,
        "cues": ["גב צמוד"],
        "alts": [],
        "muscle": "חזה",
    }


async def _start_session(
    db: Database, *, exercises: list[dict[str, Any]] | None = None
) -> int:
    plan = {"exercises": exercises or [_ex()]}
    return int(
        await db.execute(
            """
            INSERT INTO sessions(
                user_id, code, name, plan, status, exercise_index, set_number,
                pending_weight, pending_reps, started_at
            ) VALUES(1, 'A', 'אימון A', ?, 'active', 0, 1, NULL, NULL, ?)
            """,
            (json.dumps(plan, ensure_ascii=False), utc_now()),
        )
    )


async def _session_row(db: Database, session_id: int) -> dict[str, Any]:
    return dict(await db.fetch_one("SELECT * FROM sessions WHERE id=?", (session_id,)))


async def _sets(db: Database, session_id: int) -> list[dict[str, Any]]:
    rows = await db.fetch_all(
        "SELECT * FROM sets WHERE session_id=? ORDER BY id", (session_id,)
    )
    return [dict(r) for r in rows]


async def _ask_for_weight(db: Database, session_id: int) -> FakeQuery:
    """Drive the REAL 'ביצעתי אחרת' callback that asks for the weight."""
    session = await _session_row(db, session_id)
    query = FakeQuery()
    await coach_bot.handle_session_action_callback(
        query,
        _ctx(),
        1,
        f"different:{session_id}:{session['exercise_index']}:{session['set_number']}",
    )
    return query


async def _send_text(text: str) -> FakeUpdate:
    """Drive the REAL text entry point."""
    update = FakeUpdate(text)
    await coach_bot.handle_text_message(update, _ctx())
    return update


@pytest.fixture(autouse=True)
def _allow_everyone(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _is_allowed(update: Any) -> bool:
        del update
        return True

    async def _ensure_user(update: Any) -> int:
        del update
        return 1

    async def _track_event(*args: Any, **kwargs: Any) -> None:
        del args, kwargs

    # These are resolved through the coach_bot facade by runtime_bound, so
    # patching the facade is what the extracted modules actually see.
    monkeypatch.setattr(coach_bot, "is_allowed", _is_allowed)
    monkeypatch.setattr(coach_bot, "ensure_user", _ensure_user)
    monkeypatch.setattr(coach_bot, "track_event", _track_event)


# ---------------------------------------------------------------------------
# 1. Parser grammar (pure, deterministic — no AI, no DB).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("80", 80.0),
        ("  80  ", 80.0),
        ("17.5", 17.5),
        ("17,5", 17.5),          # decimal COMMA must work
        ("0", 0.0),
        ("80 קג", 80.0),
        ("80 ק״ג", 80.0),        # gershayim
        ('80 ק"ג', 80.0),        # ASCII double quote
        ("80 קילו", 80.0),
        ("80 קילוגרם", 80.0),
        ("80kg", 80.0),
        ("17,5 ק״ג", 17.5),
        ("הרמתי 80", 80.0),
    ],
)
def test_total_weight_grammar(text: str, expected: float) -> None:
    report = weight_text.parse_weight_text(text)
    assert report is not None, text
    assert report.weight == expected
    assert report.load_type == "total"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("12 בכל יד", 12.0),
        ("12 ק״ג בכל יד", 12.0),
        ("17.5 בכל יד", 17.5),
        ("17,5 בכל יד", 17.5),
        ("12 לכל יד", 12.0),
        ("12 בכל צד", 12.0),
    ],
)
def test_per_hand_grammar(text: str, expected: float) -> None:
    report = weight_text.parse_weight_text(text)
    assert report is not None, text
    assert report.weight == expected
    assert report.load_type == "per_hand"
    assert report.is_per_hand


@pytest.mark.parametrize(
    "text", ["משקל גוף", "משקל הגוף", "רק הגוף", "בלי משקל", "bodyweight"]
)
def test_bodyweight_grammar_stores_zero(text: str) -> None:
    report = weight_text.parse_weight_text(text)
    assert report is not None, text
    # RECORDED DECISION: no load-type column exists; bodyweight -> 0.
    assert report.weight == 0.0
    assert report.load_type == "bodyweight"
    assert report.is_bodyweight


def test_same_weight_requires_a_previous_value() -> None:
    """"אותו משקל" resolves ONLY when a previous valid weight exists."""
    with_previous = weight_text.parse_weight_text("אותו משקל", previous_weight=70.0)
    assert with_previous is not None
    assert with_previous.weight == 70.0
    assert with_previous.same_as_previous is True

    # Without a previous value there is nothing to repeat — refuse, don't guess.
    assert weight_text.parse_weight_text("אותו משקל", previous_weight=None) is None


def test_same_weight_carries_the_previous_load_type() -> None:
    report = weight_text.parse_weight_text(
        "אותו משקל", previous_weight=12.0, previous_load_type="per_hand"
    )
    assert report is not None
    assert report.weight == 12.0
    assert report.load_type == "per_hand"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "בערך",
        "לא זוכר",
        "כבד",
        "המון",
        "12 חזרות",          # reps, NOT a weight
        "3 סטים",
        "90 שניות",
        "rir 2",
        "80 90",             # ambiguous: two numbers
        "17.5.5",
        "מה השעה",
    ],
)
def test_ambiguous_and_unrelated_text_is_refused(text: str) -> None:
    assert weight_text.parse_weight_text(text, previous_weight=70.0) is None


@pytest.mark.parametrize("text", ["9000", "501", "-5", "1000 ק״ג"])
def test_safe_boundary_validation(text: str) -> None:
    """Loads outside the safe range are refused rather than clamped."""
    assert weight_text.parse_weight_text(text) is None
    assert weight_text.parse_weight_text(str(weight_text.MAX_WEIGHT_KG)) is not None


def test_confirmation_reflects_what_was_persisted() -> None:
    total = weight_text.parse_weight_text("70")
    per_hand = weight_text.parse_weight_text("12 בכל יד")
    bodyweight = weight_text.parse_weight_text("משקל גוף")
    assert weight_text.format_weight_confirmation(total) == "נרשם: 70 ק״ג ✅"
    assert weight_text.format_weight_confirmation(per_hand) == "נרשם: 12 ק״ג בכל יד ✅"
    assert weight_text.format_weight_confirmation(bodyweight) == "נרשם: משקל גוף ✅"


# ---------------------------------------------------------------------------
# 2. The weight step asks for TEXT — no weight-selection buttons.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_weight_step_asks_for_text_and_mints_no_weight_buttons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path, "prompt")
    _bind(monkeypatch, db)
    session_id = await _start_session(db)

    query = await _ask_for_weight(db, session_id)

    assert query.messages, "the weight question must be shown"
    prompt = query.messages[-1]
    assert "איזה משקל הרמת בסט הזה?" in prompt
    assert "אפשר לכתוב למשל" in prompt

    # THE requirement: no predefined weight-SELECTION buttons at this step.
    callbacks = _callbacks(query.reply_markups[-1])
    assert not any(cb.startswith("weight:") for cb in callbacks), callbacks
    labels = _labels(query.reply_markups[-1])
    assert not any(label.strip().startswith(("+", "−", "-")) for label in labels), labels


@pytest.mark.asyncio
async def test_navigation_and_safety_controls_are_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only weight-SELECTION buttons were removed."""
    db = await _make_db(tmp_path, "nav")
    _bind(monkeypatch, db)
    session_id = await _start_session(db)

    query = await _ask_for_weight(db, session_id)
    callbacks = _callbacks(query.reply_markups[-1])

    assert any(cb.startswith("ready:") for cb in callbacks), callbacks   # back
    assert any(cb.startswith("finish:") for cb in callbacks), callbacks  # stop

    # The main workout card still carries the full control set.
    card = FakeQuery()
    await coach_bot.show_session(card, 1, session_id)
    card_callbacks = _callbacks(card.reply_markups[-1])
    for prefix in ("setok:", "split:", "occupied:", "pain:", "finish:"):
        assert any(cb.startswith(prefix) for cb in card_callbacks), (prefix, card_callbacks)


@pytest.mark.asyncio
async def test_prompt_shows_previous_weight_as_context_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path, "prevctx")
    _bind(monkeypatch, db)
    session_id = await _start_session(db)
    await db.execute(
        """
        INSERT INTO sets(session_id, exercise_id, exercise_name, set_number,
                         weight, reps, rir, source, created_at)
        VALUES(?, 'db_bench', 'לחיצת חזה', 1, 70.0, 10, 2, 'telegram_one_tap', ?)
        """,
        (session_id, utc_now()),
    )

    query = await _ask_for_weight(db, session_id)
    prompt = query.messages[-1]
    assert "בסט הקודם: 70 ק״ג." in prompt
    # Context only — still no button to pick it with.
    assert not any(cb.startswith("weight:") for cb in _callbacks(query.reply_markups[-1]))


# ---------------------------------------------------------------------------
# 3. Happy path: typed weight is recorded against the right set.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("typed", "expected", "confirmation"),
    [
        ("80", 80.0, "נרשם: 80 ק״ג ✅"),
        ("17.5", 17.5, "נרשם: 17.5 ק״ג ✅"),
        ("17,5", 17.5, "נרשם: 17.5 ק״ג ✅"),
        ("80 ק״ג", 80.0, "נרשם: 80 ק״ג ✅"),
        ("80 קג", 80.0, "נרשם: 80 ק״ג ✅"),
        ("12 בכל יד", 12.0, "נרשם: 12 ק״ג בכל יד ✅"),
        ("משקל גוף", 0.0, "נרשם: משקל גוף ✅"),
    ],
)
async def test_typed_weight_is_recorded_and_confirmed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    typed: str,
    expected: float,
    confirmation: str,
) -> None:
    db = await _make_db(tmp_path, "record")
    _bind(monkeypatch, db)
    session_id = await _start_session(db)
    await _ask_for_weight(db, session_id)

    update = await _send_text(typed)

    row = await _session_row(db, session_id)
    assert row["pending_weight"] == expected
    # Identity preserved: same session, same exercise, same set.
    assert row["exercise_index"] == 0
    assert row["set_number"] == 1
    assert row["status"] == "active"

    assert update.effective_message.texts, "a confirmation must be sent"
    reply = update.effective_message.texts[-1]
    assert confirmation in reply
    # Continues to the EXISTING next step (reps).
    assert "כמה חזרות" in reply
    assert any(
        cb.startswith("reps:") for cb in _callbacks(update.effective_message.markups[-1])
    )


@pytest.mark.asyncio
async def test_same_weight_repeats_the_previous_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path, "same")
    _bind(monkeypatch, db)
    session_id = await _start_session(db)
    await db.execute(
        """
        INSERT INTO sets(session_id, exercise_id, exercise_name, set_number,
                         weight, reps, rir, source, created_at)
        VALUES(?, 'db_bench', 'לחיצת חזה', 1, 70.0, 10, 2, 'telegram_one_tap', ?)
        """,
        (session_id, utc_now()),
    )
    await _ask_for_weight(db, session_id)

    await _send_text("אותו משקל")

    assert (await _session_row(db, session_id))["pending_weight"] == 70.0


@pytest.mark.asyncio
async def test_same_weight_without_a_previous_value_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No previous set -> nothing to repeat -> re-ask, write nothing."""
    db = await _make_db(tmp_path, "samenone")
    _bind(monkeypatch, db)
    session_id = await _start_session(db)
    await _ask_for_weight(db, session_id)

    update = await _send_text("אותו משקל")

    assert (await _session_row(db, session_id))["pending_weight"] is None
    assert "לא הצלחתי לזהות את המשקל" in update.effective_message.texts[-1]


@pytest.mark.asyncio
async def test_per_hand_load_type_hint_rides_in_the_plan_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RECORDED DECISION: no migration. sets.weight keeps the canonical number
    the buttons produced; the per-hand meaning is carried in the schemaless
    sessions.plan JSON."""
    db = await _make_db(tmp_path, "hint")
    _bind(monkeypatch, db)
    session_id = await _start_session(db)
    await _ask_for_weight(db, session_id)

    await _send_text("12 בכל יד")

    row = await _session_row(db, session_id)
    assert row["pending_weight"] == 12.0
    plan = json.loads(row["plan"])
    assert plan["load_type_hints"]["0:1"] == "per_hand"
    # The sets schema itself is untouched — no new column.
    columns = {
        r["name"] for r in await db.fetch_all("PRAGMA table_info(sets)", ())
    }
    assert "load_type" not in columns
    assert "unit" not in columns


# ---------------------------------------------------------------------------
# 4. Invalid input: no write, no advance, same state.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("typed", ["בערך", "לא זוכר", "12 חזרות", "9000", "כבד מאוד"])
async def test_invalid_input_writes_nothing_and_does_not_advance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, typed: str
) -> None:
    db = await _make_db(tmp_path, "invalid")
    _bind(monkeypatch, db)
    session_id = await _start_session(db)
    await _ask_for_weight(db, session_id)
    before = await _session_row(db, session_id)

    update = await _send_text(typed)

    after = await _session_row(db, session_id)
    assert after["pending_weight"] is None
    assert after["exercise_index"] == before["exercise_index"]
    assert after["set_number"] == before["set_number"]
    assert after["status"] == "active"
    assert await _sets(db, session_id) == []

    # Short Hebrew clarification…
    assert "לא הצלחתי לזהות את המשקל" in update.effective_message.texts[-1]
    # …and we REMAIN in the same weight-entry state.
    flow = await conversation.get_active_flow(db, 1)
    assert flow.name == conversation.FlowName.workout_session
    assert flow.step == workout_bot.WEIGHT_TEXT_STEP


@pytest.mark.asyncio
async def test_retry_after_invalid_input_still_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The state survives a bad answer — the next good one is accepted."""
    db = await _make_db(tmp_path, "retry")
    _bind(monkeypatch, db)
    session_id = await _start_session(db)
    await _ask_for_weight(db, session_id)

    await _send_text("לא זוכר")
    assert (await _session_row(db, session_id))["pending_weight"] is None

    await _send_text("62.5")
    assert (await _session_row(db, session_id))["pending_weight"] == 62.5


# ---------------------------------------------------------------------------
# 5. Duplicate updates and idempotency.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_update_does_not_record_or_advance_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A redelivered Telegram update must be a no-op."""
    db = await _make_db(tmp_path, "dup")
    _bind(monkeypatch, db)
    session_id = await _start_session(db)
    await _ask_for_weight(db, session_id)

    await _send_text("80")
    after_first = await _session_row(db, session_id)

    # Exact same update delivered again.
    await _send_text("80")
    after_second = await _session_row(db, session_id)

    assert after_second["pending_weight"] == after_first["pending_weight"] == 80.0
    assert after_second["exercise_index"] == after_first["exercise_index"] == 0
    assert after_second["set_number"] == after_first["set_number"] == 1
    # And no set was logged twice by the weight step (weight != a full set).
    assert await _sets(db, session_id) == []


@pytest.mark.asyncio
async def test_weight_text_cannot_write_to_a_different_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A message typed against a stale prompt (the session advanced meanwhile)
    must not land on the new set."""
    db = await _make_db(tmp_path, "stale")
    _bind(monkeypatch, db)
    session_id = await _start_session(db)
    await _ask_for_weight(db, session_id)

    # The session advances behind the prompt's back.
    await db.execute(
        "UPDATE sessions SET set_number=2 WHERE id=?", (session_id,)
    )

    await _send_text("80")

    row = await _session_row(db, session_id)
    assert row["pending_weight"] is None  # nothing written to set 2
    assert row["set_number"] == 2
    # The stale step was released rather than trapping the user.
    flow = await conversation.get_active_flow(db, 1)
    assert flow.name != conversation.FlowName.workout_session


# ---------------------------------------------------------------------------
# 6. Restart / resume.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_after_restart_still_expects_a_weight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The step is a DB row, not process memory: a restart mid-question keeps
    expecting the weight, and the answer is still accepted."""
    db = await _make_db(tmp_path, "restart")
    _bind(monkeypatch, db)
    session_id = await _start_session(db)
    await _ask_for_weight(db, session_id)

    # Simulate a process restart: drop every in-memory binding and rebind to
    # the same on-disk database.
    reopened = Database(db.path if hasattr(db, "path") else str(tmp_path / "restart.db"))
    await reopened.init()
    _bind(monkeypatch, reopened)

    flow = await conversation.get_active_flow(reopened, 1)
    assert flow.name == conversation.FlowName.workout_session
    assert flow.step == workout_bot.WEIGHT_TEXT_STEP
    assert flow.payload["session_id"] == session_id
    assert flow.payload["exercise_index"] == 0
    assert flow.payload["set_number"] == 1

    await _send_text("95")
    assert (await _session_row(reopened, session_id))["pending_weight"] == 95.0


# ---------------------------------------------------------------------------
# 7. Isolation from every other flow.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_weight_grammar_is_inert_without_an_armed_weight_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An active workout that is NOT awaiting a weight must not capture text.

    "80" typed while the workout card is showing is general text — it must fall
    through to normal routing and write no load.
    """
    db = await _make_db(tmp_path, "inert")
    _bind(monkeypatch, db)
    session_id = await _start_session(db)  # active, but weight NOT requested

    routed: list[str] = []

    async def _route_free_text(update: Any, user_id: int) -> None:
        del update, user_id
        routed.append("free_text")

    monkeypatch.setattr(coach_bot, "route_free_text", _route_free_text, raising=False)
    monkeypatch.setattr(meal_text_bot, "route_free_text", _route_free_text, raising=False)

    flow = await conversation.get_active_flow(db, 1)
    assert flow.is_idle, "no weight step armed"

    decision = await conversation.ConversationRouter.route(db, 1, "text")
    assert decision.handler != "workout_flow"
    assert (await _session_row(db, session_id))["pending_weight"] is None


@pytest.mark.asyncio
async def test_meal_and_question_flows_outrank_or_bypass_the_weight_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Router order: meal flows are resolved BEFORE the workout step, so meal
    logging text can never be read as a weight."""
    db = await _make_db(tmp_path, "isolation")
    _bind(monkeypatch, db)

    await conversation.set_active_flow(
        db, 1, conversation.FlowName.meal_logging, step="await", payload={}
    )
    decision = await conversation.ConversationRouter.route(db, 1, "text")
    assert decision.handler == "meal_flow"

    await conversation.set_active_flow(
        db, 1, conversation.FlowName.onboarding_question, step="q", payload={}
    )
    decision = await conversation.ConversationRouter.route(db, 1, "text")
    assert decision.handler == "question_flow"


@pytest.mark.asyncio
async def test_meal_text_is_not_captured_while_awaiting_a_weight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even with the weight step armed, a meal sentence is refused by the
    grammar rather than mined for a number — nothing is written."""
    db = await _make_db(tmp_path, "mealtext")
    _bind(monkeypatch, db)
    session_id = await _start_session(db)
    await _ask_for_weight(db, session_id)

    update = await _send_text("אכלתי 200 גרם חזה עוף עם אורז")

    assert (await _session_row(db, session_id))["pending_weight"] is None
    assert "לא הצלחתי לזהות את המשקל" in update.effective_message.texts[-1]


@pytest.mark.asyncio
async def test_the_weight_step_is_cleared_after_a_successful_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Clearing on save is what stops the grammar from swallowing the NEXT
    message (a meal, a question, general chat)."""
    db = await _make_db(tmp_path, "clear")
    _bind(monkeypatch, db)
    session_id = await _start_session(db)
    await _ask_for_weight(db, session_id)

    await _send_text("80")

    flow = await conversation.get_active_flow(db, 1)
    assert flow.name != conversation.FlowName.workout_session

    decision = await conversation.ConversationRouter.route(db, 1, "text")
    assert decision.handler != "workout_flow"
