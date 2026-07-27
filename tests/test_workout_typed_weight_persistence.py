"""The weight the user TYPED must be the weight that gets stored.

Evidence from the 2026-07-26 live session: three typed weights, zero stored
correctly. One set persisted 50.0 kg with source=telegram_one_tap -- the
plan's own default -- after the user had typed a different number, and
another persisted 0.0 kg on a barbell bench press.

Root cause: handle_session_action_callback seeded ``weight``/``reps`` from
recommend_load() and handed those values to every handler. The ``rir``
branch re-read the session row and preferred pending_weight, so that path
worked; ``setok`` trusted the seed and wrote the plan default. show_session
performed the same override when rendering, so the card displayed the typed
weight while the sets table stored the recommendation -- the user saw the
right number and the wrong one was persisted.

These tests drive the REAL callback and text entry points.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import coach_bot
from db import Database
from helpers import utc_now
from noam_coach.bot import callback_session as callback_session_bot
from noam_coach.bot import meal_text as meal_text_bot
from noam_coach.bot import ui as ui_bot
from noam_coach.bot import workout as workout_bot
from noam_coach.services import weight_text


class FakeMessage:
    # chat_id/message_id are read by start_rest_timer, which these tests
    # reach because saving a set is what starts the rest.
    chat_id = 1
    message_id = 555

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
    def __init__(self, text: str) -> None:
        self.effective_message = FakeMessage()
        self.effective_message.text = text  # type: ignore[attr-defined]


class FakeJobQueue:
    """Records scheduled rest timers instead of running them.

    Saving a set starts a rest timer, so these tests need a JobQueue to
    exist -- but nothing here depends on it firing.
    """

    def __init__(self) -> None:
        self.scheduled: list[dict[str, Any]] = []

    def run_repeating(self, callback: Any, **kwargs: Any) -> None:
        del callback
        self.scheduled.append(kwargs)

    def get_jobs_by_name(self, name: str) -> tuple[Any, ...]:
        del name
        return ()


def _ctx() -> Any:
    return type("Ctx", (), {"bot": object(), "job_queue": FakeJobQueue()})()


async def _make_db(tmp_path: Path, name: str) -> Database:
    db = Database(str(tmp_path / f"{name}.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1, 'Test', NULL, ?)",
        (utc_now(),),
    )
    return db


def _bind(monkeypatch: pytest.MonkeyPatch, db: Database) -> None:
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(ui_bot, "DB", db)
    monkeypatch.setattr(workout_bot, "DB", db)
    monkeypatch.setattr(meal_text_bot, "DB", db)
    monkeypatch.setattr(callback_session_bot, "DB", db)


def _ex(
    exercise_id: str = "db_bench",
    name: str = "לחיצת חזה",
    weight: float = 50.0,
) -> dict[str, Any]:
    return {
        "id": exercise_id,
        "name": name,
        "sets": 3,
        "rmin": 8,
        "rmax": 12,
        "rest": 90,
        "weight": weight,
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
    update = FakeUpdate(text)
    await coach_bot.handle_text_message(update, _ctx())
    return update


async def _tap(db: Database, session_id: int, action: str) -> FakeQuery:
    session = await _session_row(db, session_id)
    query = FakeQuery()
    await coach_bot.handle_session_action_callback(
        query,
        _ctx(),
        1,
        f"{action}:{session_id}:{session['exercise_index']}:{session['set_number']}",
    )
    return query


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

    monkeypatch.setattr(coach_bot, "is_allowed", _is_allowed)
    monkeypatch.setattr(coach_bot, "ensure_user", _ensure_user)
    monkeypatch.setattr(coach_bot, "track_event", _track_event)


# ---------------------------------------------------------------------------
# The regression: a typed weight reaching the sets table
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_typed_weight_is_persisted_by_one_tap_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact live failure: typed 47.5, stored the plan's 50."""
    db = await _make_db(tmp_path, "typed")
    _bind(monkeypatch, db)
    session_id = await _start_session(db)

    await _ask_for_weight(db, session_id)
    await _send_text("47.5")
    assert (await _session_row(db, session_id))["pending_weight"] == 47.5

    await _tap(db, session_id, "setok")

    rows = await _sets(db, session_id)
    assert len(rows) == 1
    assert rows[0]["weight"] == 47.5, "the plan default overwrote the typed weight"


@pytest.mark.asyncio
async def test_typed_weight_survives_a_different_plan_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A typed value well away from the recommendation must still win."""
    db = await _make_db(tmp_path, "away")
    _bind(monkeypatch, db)
    session_id = await _start_session(db, exercises=[_ex(weight=60.0)])

    await _ask_for_weight(db, session_id)
    await _send_text("32.5")
    await _tap(db, session_id, "setok")

    rows = await _sets(db, session_id)
    assert rows[0]["weight"] == 32.5
    assert rows[0]["weight"] != 60.0


@pytest.mark.asyncio
async def test_untyped_set_still_uses_the_recommendation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The override must not break the ordinary one-tap path."""
    db = await _make_db(tmp_path, "untyped")
    _bind(monkeypatch, db)
    session_id = await _start_session(db, exercises=[_ex(weight=42.5)])

    await _tap(db, session_id, "setok")

    rows = await _sets(db, session_id)
    assert len(rows) == 1
    assert rows[0]["weight"] == 42.5


@pytest.mark.asyncio
async def test_card_and_storage_agree_on_the_typed_weight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """show_session already displayed the typed value; storage must match it."""
    db = await _make_db(tmp_path, "agree")
    _bind(monkeypatch, db)
    session_id = await _start_session(db)

    await _ask_for_weight(db, session_id)
    await _send_text("47.5")

    query = FakeQuery()
    await workout_bot.show_session(query, 1, session_id)
    rendered = "\n".join(query.messages)
    assert "47.5" in rendered

    await _tap(db, session_id, "setok")
    assert (await _sets(db, session_id))[0]["weight"] == 47.5


# ---------------------------------------------------------------------------
# 0 kg on a loaded exercise
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_bodyweight_is_rejected_on_a_loaded_exercise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A barbell press reported as bodyweight stored 0.0 kg as real history."""
    db = await _make_db(tmp_path, "zero")
    _bind(monkeypatch, db)
    session_id = await _start_session(db, exercises=[_ex(weight=60.0)])

    await _ask_for_weight(db, session_id)
    update = await _send_text("משקל גוף")

    assert (await _session_row(db, session_id))["pending_weight"] is None, (
        "a contradictory bodyweight report must not be written"
    )
    assert update.effective_message.texts, "the user must be re-asked"
    assert "ק״ג" in update.effective_message.texts[-1]


@pytest.mark.asyncio
async def test_bodyweight_is_accepted_on_a_bodyweight_exercise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real bodyweight movements are planned at 0 and must keep working."""
    db = await _make_db(tmp_path, "bw")
    _bind(monkeypatch, db)
    session_id = await _start_session(
        db, exercises=[_ex(exercise_id="plank", name="פלאנק", weight=0.0)]
    )

    await _ask_for_weight(db, session_id)
    await _send_text("משקל גוף")

    assert (await _session_row(db, session_id))["pending_weight"] == 0.0


@pytest.mark.asyncio
async def test_rejected_bodyweight_leaves_the_set_unadvanced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path, "noadv")
    _bind(monkeypatch, db)
    session_id = await _start_session(db, exercises=[_ex(weight=60.0)])
    before = await _session_row(db, session_id)

    await _ask_for_weight(db, session_id)
    await _send_text("משקל גוף")

    after = await _session_row(db, session_id)
    assert after["set_number"] == before["set_number"]
    assert after["exercise_index"] == before["exercise_index"]
    assert await _sets(db, session_id) == []


@pytest.mark.asyncio
async def test_a_real_number_still_works_after_a_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rejection re-asks; the retry must be accepted normally."""
    db = await _make_db(tmp_path, "retry")
    _bind(monkeypatch, db)
    session_id = await _start_session(db, exercises=[_ex(weight=60.0)])

    await _ask_for_weight(db, session_id)
    await _send_text("משקל גוף")
    await _ask_for_weight(db, session_id)
    await _send_text("40")

    assert (await _session_row(db, session_id))["pending_weight"] == 40.0


def test_rejection_copy_names_the_exercise_and_asks_for_a_number() -> None:
    text = weight_text.bodyweight_rejected_text("לחיצת חזה")
    assert "לחיצת חזה" in text
    assert "ק״ג" in text


def test_rejection_copy_handles_a_missing_exercise_name() -> None:
    assert weight_text.bodyweight_rejected_text("").strip()
