"""The effort CTA: ask for RIR *after* the set is saved, never before.

One-tap logging (`setok`) writes `RIR_UNKNOWN` and never asks how hard the set
was. That is the right default -- interrupting a working set to collect a number
is exactly the friction this bot exists to avoid -- but it leaves progression
starved. `recommend_load_decision` can only raise load when every set in the
latest session reports a *known* RIR of 2 or more, so a user who always taps
"done" never progresses on evidence, only on the plan's prescription.

The fix is to ask at the one moment the answer costs nothing: the rest screen,
after the row is already committed and the pointer has already advanced.
Ignoring the question leaves the set exactly as one-tap wrote it. That makes
"non-blocking" a property of *where* the question lives rather than a promise
about how it behaves.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import coach_bot
from noam_coach.bot import callback_session as callback_session_bot
from noam_coach.bot import workout as workout_bot
from noam_coach.bot import workout_runtime as workout_runtime_bot

_PLAN = {
    "name": "Test",
    "exercises": [
        {"id": "squat", "name": "סקוואט", "sets": 3, "rmin": 5, "rmax": 8,
         "inc": 2.5, "rest": 120, "weight": 60, "cues": []},
    ],
}


class _FakeQuery:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.answers: list[str] = []

    async def edit_message_text(
        self, text: str, reply_markup: Any = None, parse_mode: str | None = None
    ) -> None:
        self.messages.append(text)

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        if text:
            self.answers.append(text)


async def _session_with_logged_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rir: int
) -> tuple[Any, int, int]:
    """An active session whose first set is already saved at the given RIR."""
    db = coach_bot.Database(str(tmp_path / "effort.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (coach_bot.utc_now(),),
    )
    for module in (coach_bot, workout_bot, workout_runtime_bot, callback_session_bot):
        monkeypatch.setattr(module, "DB", db, raising=False)

    session_id = await db.execute(
        "INSERT INTO sessions(user_id, code, name, plan, status, exercise_index, set_number, started_at) "
        "VALUES(1,'T','Test',?, 'active', 0, 2, ?)",
        (json.dumps(_PLAN, ensure_ascii=False), coach_bot.utc_now()),
    )
    set_id = await db.execute(
        "INSERT INTO sets(session_id, exercise_id, exercise_name, set_number, weight, reps, rir, "
        "source, exercise_index, created_at) "
        "VALUES(?, 'squat', 'סקוואט', 1, 60, 8, ?, 'telegram_one_tap', 0, ?)",
        (session_id, rir, coach_bot.utc_now()),
    )
    return db, int(session_id), int(set_id)


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------
def test_effort_maps_onto_the_existing_rir_scale() -> None:
    """Four labels, reusing the values the explicit RIR keyboard already emits.

    This is a relabelling, not a second scale. If the CTA invented its own
    values, progression would have to reconcile two vocabularies describing the
    same thing.
    """
    assert workout_runtime_bot.effort_to_rir("easy") == 3
    assert workout_runtime_bot.effort_to_rir("ok") == 2
    assert workout_runtime_bot.effort_to_rir("hard") == 1
    assert workout_runtime_bot.effort_to_rir("failure") == 0


def test_unknown_effort_token_is_rejected() -> None:
    """A malformed callback must not be coerced into a plausible RIR."""
    assert workout_runtime_bot.effort_to_rir("wat") is None
    assert workout_runtime_bot.effort_to_rir("") is None


# ---------------------------------------------------------------------------
# The keyboard
# ---------------------------------------------------------------------------
def _callbacks(markup: Any) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def test_effort_row_is_offered_when_rir_is_unknown() -> None:
    step = {"id": 1, "exercise_index": 0, "set_number": 2}
    markup = workout_runtime_bot.rest_keyboard(
        step, last_set_rir=coach_bot.RIR_UNKNOWN, last_set_id=7
    )
    callbacks = _callbacks(markup)
    # All four gradations, each naming the set it describes.
    assert "seteffort:1:0:2:7:easy" in callbacks
    assert "seteffort:1:0:2:7:ok" in callbacks
    assert "seteffort:1:0:2:7:hard" in callbacks
    assert "seteffort:1:0:2:7:failure" in callbacks


def test_effort_row_is_absent_when_rir_is_already_known() -> None:
    """Asking again would be the re-asking the product forbids."""
    step = {"id": 1, "exercise_index": 0, "set_number": 2}
    markup = workout_runtime_bot.rest_keyboard(step, last_set_rir=2, last_set_id=7)
    assert not any(cb.startswith("seteffort:") for cb in _callbacks(markup))


def test_effort_row_is_absent_when_no_set_is_identified() -> None:
    """Without a set id there is nothing to attribute the answer to.

    Resolving "the most recent set" at tap time would race the watch ingest
    path, so the CTA names its target or is not offered at all.
    """
    step = {"id": 1, "exercise_index": 0, "set_number": 2}
    markup = workout_runtime_bot.rest_keyboard(
        step, last_set_rir=coach_bot.RIR_UNKNOWN, last_set_id=None
    )
    assert not any(cb.startswith("seteffort:") for cb in _callbacks(markup))


def test_rest_keyboard_keeps_its_existing_actions() -> None:
    """The CTA is additive: ready / +30s / undo must survive untouched."""
    step = {"id": 1, "exercise_index": 0, "set_number": 2}
    markup = workout_runtime_bot.rest_keyboard(
        step, last_set_rir=coach_bot.RIR_UNKNOWN, last_set_id=7
    )
    callbacks = _callbacks(markup)
    assert "ready:1:0:2" in callbacks
    assert "restadd:1:0:2:30" in callbacks
    assert "undoset:1:0:2" in callbacks


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_effort_updates_the_set_it_names(tmp_path, monkeypatch) -> None:
    db, session_id, set_id = await _session_with_logged_set(
        tmp_path, monkeypatch, coach_bot.RIR_UNKNOWN
    )
    query = _FakeQuery()

    await callback_session_bot.handle_session_action_callback(
        query, context=None, user_id=1,
        data=f"seteffort:{session_id}:0:2:{set_id}:hard",
    )

    row = await db.fetch_one("SELECT rir FROM sets WHERE id=?", (set_id,))
    assert row["rir"] == 1


@pytest.mark.asyncio
async def test_effort_never_overwrites_an_explicit_rir(tmp_path, monkeypatch) -> None:
    """A late tap must not clobber a number the user already gave.

    The CTA and the explicit RIR keyboard can both be live: the set card writes
    a real RIR, and a stale rest-screen callback may arrive afterwards. The
    user's explicit answer wins, and the guard also makes a double-tap
    idempotent.
    """
    db, session_id, set_id = await _session_with_logged_set(tmp_path, monkeypatch, 3)
    query = _FakeQuery()

    await callback_session_bot.handle_session_action_callback(
        query, context=None, user_id=1,
        data=f"seteffort:{session_id}:0:2:{set_id}:failure",
    )

    row = await db.fetch_one("SELECT rir FROM sets WHERE id=?", (set_id,))
    assert row["rir"] == 3, "an explicitly reported RIR must survive a later effort tap"


@pytest.mark.asyncio
async def test_ignoring_the_cta_leaves_the_set_untouched(tmp_path, monkeypatch) -> None:
    """The whole design rests on this: not answering costs nothing."""
    db, _session_id, set_id = await _session_with_logged_set(
        tmp_path, monkeypatch, coach_bot.RIR_UNKNOWN
    )
    row = await db.fetch_one("SELECT rir FROM sets WHERE id=?", (set_id,))
    assert row["rir"] == coach_bot.RIR_UNKNOWN
    assert not coach_bot._rir_known(row["rir"])


@pytest.mark.asyncio
async def test_effort_on_a_foreign_set_is_refused(tmp_path, monkeypatch) -> None:
    """A set id from the callback must never be trusted across users.

    Two independent gates stop this, and the redundancy is deliberate. The
    router resolves the session with `AND user_id=?` before any handler runs,
    so a foreign session id is rejected before reaching this code at all. The
    UPDATE then re-scopes to the same user, so the set id -- which arrives from
    the client and is the only value here not already validated -- cannot be
    used to reach a row in someone else's session.
    """
    db, session_id, set_id = await _session_with_logged_set(
        tmp_path, monkeypatch, coach_bot.RIR_UNKNOWN
    )
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(2,'B',NULL,?)",
        (coach_bot.utc_now(),),
    )
    query = _FakeQuery()

    await callback_session_bot.handle_session_action_callback(
        query, context=None, user_id=2,
        data=f"seteffort:{session_id}:0:2:{set_id}:failure",
    )

    row = await db.fetch_one("SELECT rir FROM sets WHERE id=?", (set_id,))
    assert row["rir"] == coach_bot.RIR_UNKNOWN, "another user must not edit this set"


@pytest.mark.asyncio
async def test_effort_cannot_reach_a_set_outside_the_named_session(
    tmp_path, monkeypatch
) -> None:
    """The set id is the one client-supplied value the router does not check.

    The router validates the *session*; nothing upstream ties the set id to it.
    So the UPDATE scopes on both, and a set belonging to another user's session
    stays untouched even when the caller owns the session they name.
    """
    db, session_id, own_set_id = await _session_with_logged_set(
        tmp_path, monkeypatch, coach_bot.RIR_UNKNOWN
    )
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(2,'B',NULL,?)",
        (coach_bot.utc_now(),),
    )
    foreign_session_id = await db.execute(
        "INSERT INTO sessions(user_id, code, name, plan, status, exercise_index, set_number, started_at) "
        "VALUES(2,'T','Other',?, 'active', 0, 2, ?)",
        (json.dumps(_PLAN, ensure_ascii=False), coach_bot.utc_now()),
    )
    foreign_set_id = await db.execute(
        "INSERT INTO sets(session_id, exercise_id, exercise_name, set_number, weight, reps, rir, "
        "source, exercise_index, created_at) "
        "VALUES(?, 'squat', 'סקוואט', 1, 80, 8, ?, 'telegram_one_tap', 0, ?)",
        (foreign_session_id, coach_bot.RIR_UNKNOWN, coach_bot.utc_now()),
    )
    query = _FakeQuery()

    # User 1 owns the session they name, but points at user 2's set.
    await callback_session_bot.handle_session_action_callback(
        query, context=None, user_id=1,
        data=f"seteffort:{session_id}:0:2:{int(foreign_set_id)}:failure",
    )

    stolen = await db.fetch_one("SELECT rir FROM sets WHERE id=?", (foreign_set_id,))
    assert stolen["rir"] == coach_bot.RIR_UNKNOWN, "a set outside the session must be untouchable"
    mine = await db.fetch_one("SELECT rir FROM sets WHERE id=?", (own_set_id,))
    assert mine["rir"] == coach_bot.RIR_UNKNOWN, "and no collateral write either"
