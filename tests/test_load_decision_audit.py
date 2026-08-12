"""Recording the load decision the athlete was asked to act on (A13).

Two things about this item are easy to get wrong in ways that still look green,
so they are pinned first:

* `_allowlist_audit_details` drops list-valued details through `_scalar_only`
  **silently** -- no error, no log. `signals` as a list would vanish from the
  stored row while a test asserting "a row was written" still passed. The
  encoding is therefore asserted on the STORED row, not on the dict.
* A13 is recording-only. If a load decision differs before and after A13, the
  change is wrong -- so the behaviour-neutrality tests assert the recommendation
  itself, not just that no exception escaped.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest

from db import Database
from helpers import utc_now
from noam_coach.services import training
from noam_coach.services.training import _LOAD_AUDIT_CHAINS


async def _db(tmp_path: Path, name: str = "a13") -> Database:
    db = Database(str(tmp_path / f"{name}.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (utc_now(),),
    )
    return db


def _bind(monkeypatch: pytest.MonkeyPatch, db: Database) -> None:
    import coach_bot
    from noam_coach.services import core as core_services

    monkeypatch.setattr(coach_bot, "DB", db, raising=False)
    monkeypatch.setattr(core_services, "DB", db, raising=False)

    # `_LOAD_AUDIT_CHAINS` and `_LOAD_AUDIT_TASKS` are module globals that
    # outlive a test. Left populated, a COMPLETED task from an earlier test
    # sits at the head of the chain for the same key, so the next schedule
    # awaits something already finished and accidentally serializes -- which
    # masked the concurrency failure this file exists to catch. Each test gets
    # a clean scheduler.
    training._LOAD_AUDIT_CHAINS.clear()
    training._LOAD_AUDIT_TASKS.clear()


def _decision(**overrides: Any) -> training.LoadRecommendation:
    fields: dict[str, Any] = {
        "weight": 60.0,
        "reps": 10,
        "explanation": "עלינו ב-2.5 ק\"ג כי השלמת את כל החזרות בקלות",
        "decision": "progress",
        "signals": ("all_reps_completed", "rir_high"),
        "missing_context": ("sleep_quality",),
        "confidence": 90,
        "data_completeness": 75,
    }
    fields.update(overrides)
    return training.LoadRecommendation(**fields)


async def _audit_rows(db: Database) -> list[dict[str, Any]]:
    rows = await db.fetch_all(
        "SELECT entity_id, details FROM audit WHERE action='recommend_load'", ()
    )
    return [
        {"entity_id": row["entity_id"], **json.loads(row["details"] or "{}")}
        for row in rows or []
    ]


# ---------------------------------------------------------------------------
# The persisted shape
# ---------------------------------------------------------------------------
def test_the_audit_dict_carries_no_prose() -> None:
    """LOG-012 exists because free text reached `audit` and the DSAR export.

    Asserted on the dict itself as well as on the stored row, because this is
    the one place the shape is defined -- a future caller adding `explanation`
    back would be caught here rather than at the database.
    """
    payload = _decision().to_audit_dict()

    assert "explanation" not in payload
    assert not any(
        isinstance(value, str) and "כי" in value for value in payload.values()
    ), f"Hebrew prose reached the persisted shape: {payload}"


def test_list_fields_are_encoded_as_scalars() -> None:
    """The trap: `_scalar_only` drops lists with no error.

    A list here would be missing from the stored row while every "did we write
    a row" assertion still passed, so the encoding happens once, at the source.
    """
    payload = _decision().to_audit_dict()

    assert isinstance(payload["signals"], str), "signals is a list; it will be dropped"
    assert isinstance(payload["missing_context"], str)
    assert payload["signals"] == "all_reps_completed,rir_high"
    assert payload["missing_context"] == "sleep_quality"


def test_token_encoding_is_bounded() -> None:
    """An audit row must never carry an unbounded payload."""
    payload = _decision(
        signals=tuple(f"signal_{index}" for index in range(50)),
        missing_context=("x" * 200,),
    ).to_audit_dict()

    assert payload["signals"].count(",") < training._MAX_AUDIT_TOKENS
    assert len(payload["missing_context"]) <= training._MAX_AUDIT_TOKEN_LEN


def test_empty_signals_do_not_become_the_string_none() -> None:
    payload = _decision(signals=(), missing_context=()).to_audit_dict()
    assert payload["signals"] == ""
    assert payload["missing_context"] == ""


# ---------------------------------------------------------------------------
# The allowlist registration -- proven, not assumed
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_every_field_survives_the_allowlist(tmp_path, monkeypatch) -> None:
    """The pair must be REGISTERED, not merely fall through `_scalar_only`.

    An unregistered pair still stores scalars incidentally, so a test that only
    checks "the row exists" cannot tell the difference. This asserts every
    declared field is present in what was actually stored.
    """
    db = await _db(tmp_path, "allowlist")
    _bind(monkeypatch, db)

    await training.record_load_decision(
        1, _decision(), exercise_id="leg_press", exercise_index=0, session_id=7,
        set_number=2, channel=training.LOAD_CHANNEL_TELEGRAM,
    )

    rows = await _audit_rows(db)
    assert len(rows) == 1
    stored = rows[0]
    for field in (
        "decision", "recommended_weight", "recommended_reps", "signals",
        "missing_context", "confidence", "data_completeness", "channel",
        "session_id", "set_number",
    ):
        assert field in stored, f"{field} was dropped by the allowlist"
    assert stored["entity_id"] == "leg_press"
    assert stored["signals"] == "all_reps_completed,rir_high"
    assert "explanation" not in stored


@pytest.mark.asyncio
async def test_prose_never_reaches_the_stored_row(tmp_path, monkeypatch) -> None:
    """Defence in depth: even if a caller passes it, the allowlist drops it."""
    db = await _db(tmp_path, "no_prose")
    _bind(monkeypatch, db)
    from noam_coach.services.core import write_audit

    await write_audit(
        1, "recommend_load", "exercise", "leg_press",
        decision="progress",
        explanation="טקסט חופשי שאסור שיגיע לאודיט",
    )

    rows = await _audit_rows(db)
    assert len(rows) == 1
    assert "explanation" not in rows[0], "free prose was stored"


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_two_channels_are_distinguishable(tmp_path, monkeypatch) -> None:
    """The Telegram card and the Watch face are two real presentations.

    Both can show the same set. Without `channel` the second reads as a
    duplicate write and would invite someone to "fix" it by suppressing one.
    """
    db = await _db(tmp_path, "channels")
    _bind(monkeypatch, db)

    for channel in (training.LOAD_CHANNEL_TELEGRAM, training.LOAD_CHANNEL_WATCH):
        await training.record_load_decision(
            1, _decision(), exercise_id="leg_press", exercise_index=0, session_id=7,
            set_number=2, channel=channel,
        )

    rows = await _audit_rows(db)
    assert len(rows) == 2
    assert {row["channel"] for row in rows} == {"telegram", "watch"}


# ---------------------------------------------------------------------------
# Behaviour neutrality -- the acceptance criterion that matters most
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_failing_write_neither_raises_nor_changes_the_decision(
    tmp_path, monkeypatch
) -> None:
    """"If a load decision differs before and after A13, the change is wrong."

    Recording is best-effort; the set is not. The failure is contained and
    counted, following the contract stated in `observability/emit.py`.
    """
    db = await _db(tmp_path, "failure")
    _bind(monkeypatch, db)

    async def _explode(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("audit is down")

    monkeypatch.setattr("noam_coach.services.core.write_audit", _explode)
    before = int(training._LOAD_AUDIT_HEALTH["write_failures"])
    decision = _decision()

    # Must not raise.
    await training.record_load_decision(
        1, decision, exercise_id="leg_press", exercise_index=0, session_id=7,
        set_number=2, channel=training.LOAD_CHANNEL_TELEGRAM,
    )

    # The recommendation itself is untouched -- recording cannot alter it.
    assert decision.weight == 60.0
    assert decision.reps == 10
    assert decision.to_tuple() == (60.0, 10, decision.explanation)
    # And the failure left a signal rather than vanishing.
    assert int(training._LOAD_AUDIT_HEALTH["write_failures"]) == before + 1


@pytest.mark.asyncio
async def test_recording_is_not_awaited_before_the_watch_response(
    tmp_path, monkeypatch
) -> None:
    """Ordering, asserted rather than claimed.

    The Watch handler's `return` IS its presentation -- there is no "after"
    inside the function. An awaited write would sit in front of every poll, so
    the recording is scheduled and the response is not held for it.
    """
    import inspect

    from noam_coach.api import watch_routes

    source = inspect.getsource(watch_routes.watch_current)
    record_at = source.index("schedule_load_decision_record(")
    return_at = source.index('return {\n        "active": True,\n        "session_id"')

    assert record_at < return_at, "recording must be scheduled before the return"
    assert "await schedule_load_decision_record" not in source, (
        "the Watch recording is awaited inline; it will delay every poll"
    )
    assert "await record_load_decision" not in source, (
        "the Watch handler awaits the audit write in front of its response"
    )


def test_the_telegram_card_records_after_it_is_presented() -> None:
    """The card is rendered by `safe_edit`; recording follows it.

    Reversed, this would put database latency between the user's tap and the
    card they are waiting for.
    """
    import inspect

    from noam_coach.bot import workout

    source = inspect.getsource(workout.show_session)
    present_at = source.rindex("await safe_edit_delivered(query, text, keyboard)")
    record_at = source.index("await record_load_decision(")

    assert present_at < record_at, (
        "the load decision is recorded before the card is presented"
    )


# ---------------------------------------------------------------------------
# Which sites record, and which deliberately do not
# ---------------------------------------------------------------------------
def test_render_loops_do_not_record() -> None:
    """`ui.py` calls `recommend_load` per exercise in a loop.

    Recording there would emit N rows per screen view. A plan preview is not a
    decision the athlete acts on, so the dedupe is by DEFINITION -- the call
    site is not a recording site -- rather than by suppression machinery that
    could drift.
    """
    import inspect

    from noam_coach.bot import ui

    assert "record_load_decision" not in inspect.getsource(ui), (
        "a preview render loop records load decisions"
    )


def test_recomputation_sites_do_not_record() -> None:
    """`loadwhy` explains a recommendation already presented and recorded.

    Recording it would double-count the same decision under a second event.
    """
    import inspect

    from noam_coach.bot import callback_session

    source = inspect.getsource(callback_session)
    assert "record_load_decision" not in source, (
        "a recomputation site records load decisions"
    )


@pytest.mark.asyncio
async def test_a_typed_weight_is_not_a_recommendation_the_user_acted_on(
    tmp_path, monkeypatch
) -> None:
    """When the user's own number overrides ours, there is nothing to record.

    `pending_weight` means the card shows what the USER typed. Recording our
    recommendation then would claim they acted on advice they never saw.
    """
    import inspect

    from noam_coach.bot import workout

    source = inspect.getsource(workout.show_session)
    assert "recommendation_presented = False" in source
    assert "if recommendation_presented and delivered:" in source
    del tmp_path, monkeypatch


def test_a13_adds_no_raw_audit_insert() -> None:
    """A12 writes a raw INSERT because its row must ride a transaction.

    A13 has no such requirement, so it goes through the canonical allowlisted
    and redacted writer. Copying A12's pattern here would re-implement a
    governed boundary for no reason.
    """
    import inspect

    source = inspect.getsource(training)
    assert "INSERT INTO audit" not in source, (
        "A13 bypasses write_audit with a raw INSERT"
    )
    assert "write_audit" in source


@pytest.mark.asyncio
async def test_the_watch_task_completes_and_stores_its_row(
    tmp_path, monkeypatch
) -> None:
    """The scheduled task must actually land -- fire-and-forget, not fire-and-lose."""
    db = await _db(tmp_path, "watch_task")
    _bind(monkeypatch, db)

    task = asyncio.ensure_future(
        training.record_load_decision(
            1, _decision(), exercise_id="leg_press", exercise_index=0, session_id=7,
            set_number=1, channel=training.LOAD_CHANNEL_WATCH,
        )
    )
    await task

    rows = await _audit_rows(db)
    assert len(rows) == 1
    assert rows[0]["channel"] == "watch"



# ---------------------------------------------------------------------------
# Review blockers on head 7048fc8
#
# All three were "correct on the happy path, wrong at the edge", which is why
# each test below drives the REAL delivery path rather than the recording
# helper: asserting on `record_load_decision` directly cannot see any of them.
# ---------------------------------------------------------------------------
class _StaleQuery:
    """A query whose edit fails as stale, with a controllable fallback."""

    def __init__(self, *, fallback_works: bool) -> None:
        self.fallback_works = fallback_works
        self.fallback_attempted = False
        outer = self

        class _Message:
            async def reply_text(self, *a: Any, **k: Any) -> None:
                del a, k
                outer.fallback_attempted = True
                if not outer.fallback_works:
                    raise RuntimeError("telegram is unreachable")

        self.message = _Message()

    async def edit_message_text(self, *a: Any, **k: Any) -> None:
        del a, k
        from telegram.error import BadRequest

        raise BadRequest("Message to edit not found")

    async def answer(self, *a: Any, **k: Any) -> None:
        del a, k


@pytest.mark.asyncio
async def test_a_screen_that_never_reached_the_user_is_delivered_false() -> None:
    """Blocker 1, at the delivery boundary.

    `safe_edit` returns None whether the edit worked, the fallback rescued it,
    or the fallback ALSO failed inside `suppress(Exception)`. A caller could
    not tell "the user is looking at this" from "nothing arrived".
    """
    from noam_coach.bot.ui import safe_edit_delivered

    rescued = _StaleQuery(fallback_works=True)
    assert await safe_edit_delivered(rescued, "text", None) is True
    assert rescued.fallback_attempted

    lost = _StaleQuery(fallback_works=False)
    assert await safe_edit_delivered(lost, "text", None) is False, (
        "a screen the user never received was reported as delivered"
    )
    assert lost.fallback_attempted


@pytest.mark.asyncio
async def test_no_audit_row_when_the_card_never_reached_the_user(
    tmp_path, monkeypatch
) -> None:
    """Blocker 1, through the real `show_session` path.

    The edit fails as stale AND the fallback reply fails, so the athlete sees
    nothing. Recording "presented" here would put a claim in the audit trail
    that never happened -- and that trail is the evidence a weight dispute
    rests on.
    """
    db = await _db(tmp_path, "undelivered")
    _bind(monkeypatch, db)
    from noam_coach.bot import workout as workout_bot

    monkeypatch.setattr(workout_bot, "DB", db, raising=False)
    plan = {
        "exercises": [{
            "slot_id": "s1:leg_press", "id": "leg_press",
            "name": "leg press", "sets": 3, "reps": 10, "rmin": 8,
            "rmax": 12, "inc": 2.5, "rest": 90, "weight": 60.0,
            "cues": [], "alts": [],
        }]
    }
    session_id = await db.execute(
        "INSERT INTO sessions(user_id, code, name, plan, status, exercise_index, "
        "set_number, started_at) VALUES(1, 'A', 'A', ?, 'active', 0, 1, ?)",
        (json.dumps(plan), utc_now()),
    )

    await workout_bot.show_session(
        _StaleQuery(fallback_works=False), 1, int(session_id)
    )

    rows = await _audit_rows(db)
    assert rows == [], (
        "a load decision was recorded as presented although the card never "
        "reached the user"
    )


@pytest.mark.asyncio
async def test_the_watch_recording_task_is_owned(tmp_path, monkeypatch) -> None:
    """Blocker 2: asyncio holds only a WEAK reference to a running task.

    A bare `ensure_future` whose handle is discarded can be collected
    mid-await, so the write silently never lands. The scheduler keeps a strong
    reference until the task retires, and removes it afterwards so the set
    cannot grow without bound.
    """
    db = await _db(tmp_path, "owned_task")
    _bind(monkeypatch, db)

    assert training._LOAD_AUDIT_TASKS == set(), "the registry starts empty"

    task = training.schedule_load_decision_record(
        1, _decision(), exercise_id="leg_press", exercise_index=0, session_id=7,
        set_number=1, channel=training.LOAD_CHANNEL_WATCH,
    )

    assert task in training._LOAD_AUDIT_TASKS, "the task is not owned"
    await task
    await asyncio.sleep(0)  # let the done-callback run
    assert task not in training._LOAD_AUDIT_TASKS, "the registry leaks tasks"

    rows = await _audit_rows(db)
    assert len(rows) == 1, "the scheduled write did not land"


@pytest.mark.asyncio
async def test_repeated_watch_polling_creates_one_durable_row(
    tmp_path, monkeypatch
) -> None:
    """Blocker 3: a poll is a refresh of one presentation, not a new decision.

    `GET /api/watch/current` is client-driven with no server interval, so a
    Watch resting on one set can call it every few seconds. Without a durable
    key the audit table grows without bound and the question A13 exists to
    answer -- "what did we recommend for this set" -- drowns in its own noise.
    """
    db = await _db(tmp_path, "watch_idem")
    _bind(monkeypatch, db)

    for _ in range(6):
        await training.record_load_decision(
            1, _decision(), exercise_id="leg_press", exercise_index=0, session_id=7,
            set_number=1, channel=training.LOAD_CHANNEL_WATCH,
        )

    rows = await _audit_rows(db)
    assert len(rows) == 1, f"six polls produced {len(rows)} rows"


@pytest.mark.asyncio
async def test_the_durable_key_still_separates_real_presentations(
    tmp_path, monkeypatch
) -> None:
    """Idempotency must not collapse genuinely different presentations.

    A different SET, a different EXERCISE, or a different CHANNEL is a new
    prescription. Deduping those away would lose exactly the history A13 is
    for -- the failure mode opposite to unbounded duplication.
    """
    db = await _db(tmp_path, "durable_key")
    _bind(monkeypatch, db)

    base = dict(
        exercise_id="leg_press", exercise_index=0, session_id=7, set_number=1,
        channel=training.LOAD_CHANNEL_WATCH,
    )
    await training.record_load_decision(1, _decision(), **base)
    await training.record_load_decision(1, _decision(), **{**base, "set_number": 2})
    await training.record_load_decision(
        1, _decision(), **{**base, "exercise_id": "squat"}
    )
    await training.record_load_decision(
        1, _decision(), **{**base, "channel": training.LOAD_CHANNEL_TELEGRAM}
    )
    await training.record_load_decision(1, _decision(), **{**base, "session_id": 8})
    # ...and one exact repeat, which must NOT add a row.
    await training.record_load_decision(1, _decision(), **base)

    rows = await _audit_rows(db)
    assert len(rows) == 5, f"expected 5 distinct presentations, got {len(rows)}"


@pytest.mark.asyncio
async def test_an_unreadable_audit_table_fails_open(tmp_path, monkeypatch) -> None:
    """The dedupe read must never become a reason NOT to record.

    A duplicate row is recoverable; a missing decision record is not. If the
    lookup cannot be answered, recording proceeds.
    """
    db = await _db(tmp_path, "dedupe_open")
    _bind(monkeypatch, db)

    # Record one first. On an EMPTY table "the read failed" and "nothing found"
    # both yield False, so failing closed is invisible -- the mutation survived
    # exactly that gap. With a row already present, failing closed would report
    # "already recorded" and silently drop this second, real recommendation.
    await training.record_load_decision(1, _decision(weight=60.0), **_OCCURRENCE)
    assert len(await _audit_rows(db)) == 1

    # Break BOTH read methods. Patching only the one the dedupe happens to use
    # today made this test silently stop exercising the failure path when the
    # implementation moved from fetch_all to fetch_one -- it kept passing while
    # proving nothing, and the mutation survived.
    real_fetch_all = db.fetch_all
    real_fetch_one = db.fetch_one

    async def _explode(*a: Any, **k: Any) -> None:
        raise RuntimeError("audit unreadable")

    monkeypatch.setattr(db, "fetch_all", _explode)
    monkeypatch.setattr(db, "fetch_one", _explode)
    await training.record_load_decision(1, _decision(weight=57.5), **_OCCURRENCE)

    monkeypatch.setattr(db, "fetch_all", real_fetch_all)
    monkeypatch.setattr(db, "fetch_one", real_fetch_one)
    rows = await _audit_rows(db)
    assert len(rows) == 2, (
        "a failed dedupe read suppressed a real recommendation; recording must "
        "fail OPEN"
    )



@pytest.mark.asyncio
async def test_a_stale_edit_with_no_fallback_target_is_not_delivered() -> None:
    """The branch with no fallback at all -- found by deliberate breakage.

    A stale edit is only recoverable when the query carries a `message` that
    can be replied to. Job-driven and API-driven callers pass query-like
    objects that do not, so the edit fails, NO fallback is attempted, and
    nothing reaches the user. Reporting that as delivered would record a
    presentation that never occurred -- the same lie as blocker 1, through a
    path the earlier tests never touched.
    """
    from noam_coach.bot.ui import safe_edit_delivered

    class _NoMessageQuery:
        async def edit_message_text(self, *a: Any, **k: Any) -> None:
            del a, k
            from telegram.error import BadRequest

            raise BadRequest("Message to edit not found")

    assert await safe_edit_delivered(_NoMessageQuery(), "text", None) is False, (
        "a stale edit with no fallback target was reported as delivered"
    )



# ---------------------------------------------------------------------------
# Occurrence identity (A2) and the real Watch handler path
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_two_occurrences_of_one_movement_are_two_presentations(
    tmp_path, monkeypatch
) -> None:
    """The collision the durable key must NOT make.

    A2 exists because one workout may program the same movement twice, so
    `exercise_id` cannot say which performance is meant -- that is why `sets`
    persists `exercise_index`. And `set_number` RESETS to 1 when the session
    advances, so these two rows agree on user, session, exercise_id,
    set_number and channel while being genuinely different presentations:

        exercise_index=0, leg_press, set 1
        exercise_index=1, leg_press, set 1

    Keyed without the occurrence, A13 suppresses the second and loses exactly
    the history it exists to keep.
    """
    db = await _db(tmp_path, "occurrence")
    _bind(monkeypatch, db)

    base = dict(
        exercise_id="leg_press", session_id=7, set_number=1,
        channel=training.LOAD_CHANNEL_WATCH,
    )
    await training.record_load_decision(1, _decision(), exercise_index=0, **base)
    await training.record_load_decision(1, _decision(), exercise_index=1, **base)

    rows = await _audit_rows(db)
    assert len(rows) == 2, (
        f"two occurrences of one movement collapsed into {len(rows)} row(s)"
    )
    assert {row["exercise_index"] for row in rows} == {0, 1}

    # ...and repeating the exact same occurrences adds nothing.
    await training.record_load_decision(1, _decision(), exercise_index=0, **base)
    await training.record_load_decision(1, _decision(), exercise_index=1, **base)

    rows = await _audit_rows(db)
    assert len(rows) == 2, f"repeating the same occurrences produced {len(rows)} rows"


@pytest.mark.asyncio
async def test_the_slot_id_rides_along_as_supplemental_provenance(
    tmp_path, monkeypatch
) -> None:
    """A11b's canonical slot identity is recorded, but is NOT the key.

    It answers "which slot in the programme"; `exercise_index` answers "which
    performance in this session". A rebuilt plan can move a slot to a new
    index, so the slot cannot replace the occurrence.
    """
    db = await _db(tmp_path, "slot_prov")
    _bind(monkeypatch, db)

    await training.record_load_decision(
        1, _decision(), exercise_id="leg_press", exercise_index=0,
        session_id=7, set_number=1, channel=training.LOAD_CHANNEL_WATCH,
        slot_id="abc1_a:leg_press",
    )

    rows = await _audit_rows(db)
    assert len(rows) == 1
    assert rows[0]["slot_id"] == "abc1_a:leg_press"


class _WatchDB:
    """Routes `active_session` to the test database for the real handler."""

    def __init__(self, db: Any) -> None:
        self._db = db

    def __getattr__(self, name: str) -> Any:
        return getattr(self._db, name)


@pytest.mark.asyncio
async def test_watch_current_schedules_the_record_without_awaiting_it(
    tmp_path, monkeypatch
) -> None:
    """The validation gap: the previous test never called `watch_current`.

    Ownership was asserted against the scheduler directly, which cannot show
    that the REAL handler schedules rather than awaits, nor that the response
    is returned without waiting for the write. This drives the handler.
    """
    from config import SETTINGS
    from noam_coach.api import watch_routes
    from noam_coach.services import training as training_mod

    db = await _db(tmp_path, "watch_handler")
    _bind(monkeypatch, db)
    monkeypatch.setattr(watch_routes, "DB", db, raising=False)
    monkeypatch.setattr(training_mod, "DB", db, raising=False)
    monkeypatch.setattr(SETTINGS, "telegram_allowed_user_id", 1, raising=False)

    plan = {
        "exercises": [{
            "slot_id": "s1:leg_press", "id": "leg_press", "name": "leg press",
            "sets": 3, "reps": 10, "rmin": 8, "rmax": 12, "inc": 2.5,
            "rest": 90, "weight": 60.0, "cues": [], "alts": [],
        }]
    }
    await db.execute(
        "INSERT INTO sessions(id, user_id, code, name, plan, status, "
        "exercise_index, set_number, started_at) "
        "VALUES(500, 1, 'A', 'A', ?, 'active', 0, 1, ?)",
        (json.dumps(plan), utc_now()),
    )

    assert training._LOAD_AUDIT_TASKS == set()
    payload = await watch_routes.watch_current(1)

    # The handler returned a real prescription...
    assert payload["active"] is True
    assert payload["weight"] is not None
    # ...and did NOT wait for the audit write: the task is still owned.
    pending = set(training._LOAD_AUDIT_TASKS)
    assert pending, "the handler awaited the write, or never scheduled it"

    for task in pending:
        await task
    await asyncio.sleep(0)

    assert training._LOAD_AUDIT_TASKS == set(), "the registry leaks tasks"
    rows = await _audit_rows(db)
    assert len(rows) == 1, "the scheduled write never landed"
    assert rows[0]["channel"] == "watch"
    assert rows[0]["exercise_index"] == 0

    # Polling again is the same presentation, not a new one.
    await watch_routes.watch_current(1)
    for task in set(training._LOAD_AUDIT_TASKS):
        await task
    await asyncio.sleep(0)

    rows = await _audit_rows(db)
    assert len(rows) == 1, f"a second poll produced {len(rows)} rows"



# ---------------------------------------------------------------------------
# Recommendation identity: a CHANGED recommendation for a live occurrence
#
# `recommend_load_decision` reads mutable state on every call -- daily flags
# (`sleep_quality`, `energy`) and active pain -- so the recommendation for one
# occurrence can legitimately change before the set is performed. Keyed on the
# occurrence alone, the second real recommendation was suppressed and the audit
# preserved only that SOME recommendation once existed.
# ---------------------------------------------------------------------------
_OCCURRENCE = dict(
    exercise_id="leg_press", exercise_index=0, session_id=7, set_number=1,
    channel=training.LOAD_CHANNEL_WATCH,
)


@pytest.mark.asyncio
async def test_an_identical_recommendation_repeated_is_one_row(
    tmp_path, monkeypatch
) -> None:
    """1. Same occurrence, same channel, identical recommendation => 1 row."""
    db = await _db(tmp_path, "reco_same")
    _bind(monkeypatch, db)

    for _ in range(5):
        await training.record_load_decision(1, _decision(), **_OCCURRENCE)

    rows = await _audit_rows(db)
    assert len(rows) == 1, f"five identical renders produced {len(rows)} rows"


@pytest.mark.asyncio
async def test_a_changed_weight_is_a_new_durable_row(tmp_path, monkeypatch) -> None:
    """2. Same occurrence, changed `recommended_weight` => 2 rows.

    The real case: the athlete reports bad sleep mid-session, `hold_for_recovery`
    trips, and the same set is now prescribed at a lower load. Suppressing that
    would leave the audit asserting 60 kg for a set actually advised at 57.5.
    """
    db = await _db(tmp_path, "reco_weight")
    _bind(monkeypatch, db)

    await training.record_load_decision(1, _decision(weight=60.0), **_OCCURRENCE)
    await training.record_load_decision(1, _decision(weight=57.5), **_OCCURRENCE)

    rows = await _audit_rows(db)
    assert len(rows) == 2, (
        f"a changed recommendation was suppressed; {len(rows)} row(s) stored"
    )
    assert {row["recommended_weight"] for row in rows} == {60.0, 57.5}


@pytest.mark.asyncio
async def test_a_changed_decision_or_reps_is_a_new_durable_row(
    tmp_path, monkeypatch
) -> None:
    """3. A different decision code or rep target is a different prescription."""
    db = await _db(tmp_path, "reco_decision")
    _bind(monkeypatch, db)

    await training.record_load_decision(1, _decision(), **_OCCURRENCE)
    await training.record_load_decision(
        1, _decision(decision="hold_for_recovery"), **_OCCURRENCE
    )
    await training.record_load_decision(1, _decision(reps=8), **_OCCURRENCE)
    # A changed SIGNAL set is also material: it is why the load was chosen.
    await training.record_load_decision(
        1, _decision(signals=("sleep_quality:bad",)), **_OCCURRENCE
    )

    rows = await _audit_rows(db)
    assert len(rows) == 4, f"expected 4 distinct recommendations, got {len(rows)}"


@pytest.mark.asyncio
async def test_repeating_the_changed_recommendation_adds_nothing(
    tmp_path, monkeypatch
) -> None:
    """4. Re-polling the CURRENT recommendation adds nothing.

    Deduped against the LATEST row, not the whole history: consecutive
    repetition is one presentation, so only a change writes.
    """
    db = await _db(tmp_path, "reco_repeat")
    _bind(monkeypatch, db)

    await training.record_load_decision(1, _decision(weight=60.0), **_OCCURRENCE)
    await training.record_load_decision(1, _decision(weight=57.5), **_OCCURRENCE)
    for _ in range(4):
        await training.record_load_decision(1, _decision(weight=57.5), **_OCCURRENCE)

    rows = await _audit_rows(db)
    assert len(rows) == 2, (
        f"repeated polling of the current recommendation produced {len(rows)} rows"
    )


@pytest.mark.asyncio
async def test_a_return_to_an_earlier_recommendation_is_recorded_again(
    tmp_path, monkeypatch
) -> None:
    """The A-B-A case: `audit` is a SEQUENCE, not a set of values seen.

    Measured before the fix: 60 -> 57.5 -> 60 stored only 60 and 57.5, because
    the third recommendation matched a HISTORICAL row. The last row then said
    57.5 while the athlete's final recommendation was 60, so an investigator
    reading the latest row would conclude the opposite of the truth. That is
    worse than a duplicate: the audit tells a false story about what was
    recommended.
    """
    db = await _db(tmp_path, "reco_aba")
    _bind(monkeypatch, db)

    for weight in (60.0, 57.5, 60.0):
        await training.record_load_decision(
            1, _decision(weight=weight), **_OCCURRENCE
        )

    rows = await _audit_rows(db)
    assert len(rows) == 3, (
        f"a return to an earlier recommendation was suppressed; {len(rows)} rows"
    )
    # And the ORDER is the story: the last row must be what was last shown.
    ordered = await db.fetch_all(
        "SELECT details FROM audit WHERE action='recommend_load' "
        "ORDER BY created_at ASC, id ASC",
        (),
    )
    weights = [json.loads(row["details"])["recommended_weight"] for row in ordered]
    assert weights == [60.0, 57.5, 60.0], f"the sequence was not preserved: {weights}"


@pytest.mark.asyncio
async def test_only_the_latest_recommendation_suppresses_a_repeat(
    tmp_path, monkeypatch
) -> None:
    """Dedupe compares the LATEST row, never the whole history.

    A -> B -> B: the second B is consecutive with the first, so it is
    suppressed. That is the half of the contract the A-B-A case must not
    break -- transitions are recorded, repetition is not.
    """
    db = await _db(tmp_path, "reco_latest")
    _bind(monkeypatch, db)

    for weight in (60.0, 57.5, 57.5, 57.5):
        await training.record_load_decision(
            1, _decision(weight=weight), **_OCCURRENCE
        )

    rows = await _audit_rows(db)
    assert len(rows) == 2, f"consecutive repetition was recorded; {len(rows)} rows"


@pytest.mark.asyncio
async def test_a_float_round_trip_is_not_a_change(tmp_path, monkeypatch) -> None:
    """60 and 60.0 must compare equal, or every render looks like a change.

    JSON round-trips numbers, so a naive `==` on the stored value would make
    the dedupe never match and reintroduce unbounded duplication by the back
    door -- passing the "a change is recorded" tests while failing the
    "repetition is not" ones.
    """
    db = await _db(tmp_path, "reco_float")
    _bind(monkeypatch, db)

    # 60 vs 60.0 proves nothing -- Python already compares those equal. The
    # discriminating case is accumulated binary error, which is what a plan
    # built from repeated 2.5 kg increments actually produces: 0.1 + 0.2 != 0.3
    # under `==`, and only the tolerance resolves it.
    drifted = 57.2 + 0.1 + 0.2      # 57.50000000000001
    exact = 57.5
    assert drifted != exact, "precondition: these must differ under `==`"

    await training.record_load_decision(1, _decision(weight=exact), **_OCCURRENCE)
    await training.record_load_decision(1, _decision(weight=drifted), **_OCCURRENCE)

    rows = await _audit_rows(db)
    assert len(rows) == 1, (
        "float drift was treated as a changed weight; every render would look "
        "like a change and duplication returns by the back door"
    )


def test_the_compared_fields_are_exactly_the_persisted_decision_shape() -> None:
    """The comparison must not drift from `to_audit_dict()`.

    If that shape gains a field and this tuple does not, a materially changed
    recommendation silently dedupes as identical -- the same class of silent
    drift as the list-valued detail trap.
    """
    persisted = set(_decision().to_audit_dict())
    compared = set(training._DECISION_IDENTITY_FIELDS)

    assert compared == persisted, (
        "the dedupe comparison and the persisted decision shape have drifted: "
        f"only persisted={persisted - compared}, only compared={compared - persisted}"
    )



# ---------------------------------------------------------------------------
# Concurrency: scheduled recordings must land in PRESENTATION order
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_concurrent_recordings_for_one_key_keep_presentation_order(
    tmp_path, monkeypatch
) -> None:
    """The interleaving that reintroduces false chronology.

    `record_load_decision` reads the latest row and writes in two separately
    awaited steps, so ownership alone -- which keeps tasks ALIVE but says
    nothing about their ORDER -- is not enough:

        latest = A
        B reads latest=A, then suspends before writing
        A is presented again; A reads latest=A and suppresses itself as a repeat
        B resumes and writes B
        stored: A, B   -- while the athlete saw A, B, A

    The last stored row would again disagree with what was last shown, which is
    exactly the defect the transition semantic exists to prevent, reached by a
    different route.

    The block is deterministic: B is held at its read via a real gate, not a
    sleep, so this cannot pass or fail on timing.
    """
    db = await _db(tmp_path, "concurrent_order")
    _bind(monkeypatch, db)

    # 1. Seed A.
    await training.record_load_decision(1, _decision(weight=60.0), **_OCCURRENCE)
    assert len(await _audit_rows(db)) == 1

    # 2. Gate the NEXT dedupe read so B suspends between read and write.
    gate = asyncio.Event()
    released = asyncio.Event()
    real_already = training._already_recorded

    a_read = asyncio.Event()

    async def _gated(*args: Any, **kwargs: Any) -> bool:
        # Gate by WHAT is being recorded, not by call ordinal: counting calls
        # depended on how many reads happened earlier, so the gate could land
        # on the wrong one and the test silently stopped discriminating.
        fields = kwargs.get("decision_fields") or {}
        weight = fields.get("recommended_weight")
        result = await real_already(*args, **kwargs)
        if weight == 57.5 and not released.is_set():
            released.set()
            await gate.wait()
        elif weight == 60.0 and released.is_set():
            # A's read, taken while B is gated. Signalling it is what makes
            # the interleaving deterministic instead of relying on a yield.
            a_read.set()
        return result

    monkeypatch.setattr(training, "_already_recorded", _gated)

    # 3. Schedule B, and wait until it is provably blocked after its read.
    task_b = training.schedule_load_decision_record(
        1, _decision(weight=57.5), **_OCCURRENCE
    )
    # Bounded: if the dedupe read is never reached, this test must FAIL rather
    # than hang. An unbounded wait here turned a broken build into a 15-minute
    # CI stall during deliberate breakage.
    try:
        await asyncio.wait_for(released.wait(), timeout=5)
    except asyncio.TimeoutError:  # pragma: no cover - only on a broken build
        gate.set()
        await asyncio.gather(task_b, return_exceptions=True)
        pytest.fail("the dedupe read was never reached; the gate never opened")

    # 4. Schedule A again WHILE B is blocked.
    task_a = training.schedule_load_decision_record(
        1, _decision(weight=60.0), **_OCCURRENCE
    )
    # Wait for A to REACH its read rather than yielding a fixed number of
    # times. `asyncio.sleep(0)` yields once, which was sometimes enough and
    # sometimes not -- so the test passed or failed on scheduler ordering, the
    # very thing it claims not to depend on. With per-key chaining A never
    # reads while B is gated, so a timeout here is the EXPECTED path.
    with suppress(asyncio.TimeoutError):
        await asyncio.wait_for(a_read.wait(), timeout=1)

    # 5. Release B and let both finish.
    gate.set()
    await asyncio.gather(task_b, task_a)
    await asyncio.sleep(0)

    ordered = await db.fetch_all(
        "SELECT details FROM audit WHERE action='recommend_load' "
        "ORDER BY created_at ASC, id ASC",
        (),
    )
    weights = [json.loads(row["details"])["recommended_weight"] for row in ordered]

    assert weights == [60.0, 57.5, 60.0], (
        f"scheduled recordings landed out of presentation order: {weights}"
    )
    assert weights[-1] == 60.0, (
        "the last stored recommendation is not the one last presented"
    )


@pytest.mark.asyncio
async def test_the_chain_registry_retires_and_does_not_grow(
    tmp_path, monkeypatch
) -> None:
    """A per-key chain must self-retire, or the registry leaks one entry per key."""
    db = await _db(tmp_path, "chain_retire")
    _bind(monkeypatch, db)

    assert training._LOAD_AUDIT_CHAINS == {}

    tasks = [
        training.schedule_load_decision_record(
            1, _decision(weight=60.0 + index), **_OCCURRENCE
        )
        for index in range(3)
    ]
    await asyncio.gather(*tasks)
    await asyncio.sleep(0)

    assert training._LOAD_AUDIT_CHAINS == {}, "the chain registry leaked keys"
    assert training._LOAD_AUDIT_TASKS == set(), "the task registry leaked tasks"


@pytest.mark.asyncio
async def test_channels_are_not_serialized_against_each_other(
    tmp_path, monkeypatch
) -> None:
    """Telegram must never queue behind a Watch poll.

    They are different presentations and already different durable identities,
    so chaining them together would add latency for no correctness gain.
    """
    db = await _db(tmp_path, "chain_channels")
    _bind(monkeypatch, db)

    watch_key = training._chain_key(
        1, exercise_id="leg_press", exercise_index=0, session_id=7,
        set_number=1, channel=training.LOAD_CHANNEL_WATCH,
    )
    telegram_key = training._chain_key(
        1, exercise_id="leg_press", exercise_index=0, session_id=7,
        set_number=1, channel=training.LOAD_CHANNEL_TELEGRAM,
    )

    assert watch_key != telegram_key, "the two channels share one chain"

    # The occurrence is part of the chain key too. Dropping it does not corrupt
    # any stored row -- both occurrences still record correctly -- so this is a
    # CONCURRENCY property, not a correctness one: two occurrences of the same
    # movement would queue behind each other for no reason. Pinned here rather
    # than dressed up as a correctness test.
    first_occurrence = training._chain_key(
        1, exercise_id="leg_press", exercise_index=0, session_id=7,
        set_number=1, channel=training.LOAD_CHANNEL_WATCH,
    )
    second_occurrence = training._chain_key(
        1, exercise_id="leg_press", exercise_index=1, session_id=7,
        set_number=1, channel=training.LOAD_CHANNEL_WATCH,
    )
    assert first_occurrence != second_occurrence, (
        "two occurrences of one movement share a chain and serialize needlessly"
    )



@pytest.mark.asyncio
async def test_cancelling_a_tail_does_not_disconnect_a_running_predecessor(
    tmp_path, monkeypatch
) -> None:
    """The cancellation edge in the per-key chain.

        T1 is running; chain[key] = T1
        T2 is scheduled behind T1; chain[key] = T2
        T2 is CANCELLED while T1 is still pending
        T2 retires and -- unconditionally -- pops the key
        T3 finds no predecessor and runs CONCURRENTLY with T1

    The ordering invariant is lost silently, which is the same interleaving the
    chain exists to prevent, reached through cancellation rather than through
    scheduling. A cancelled tail must hand the key back to the predecessor it
    was waiting on.

    Deterministic: T1 is held on a real Event and T3's arrival at the recorder
    is observed, so nothing here depends on how many times the loop yields.
    """
    db = await _db(tmp_path, "cancel_edge")
    _bind(monkeypatch, db)

    key = training._chain_key(
        1, exercise_id="leg_press", exercise_index=0, session_id=7,
        set_number=1, channel=training.LOAD_CHANNEL_WATCH,
    )

    t1_entered = asyncio.Event()
    release_t1 = asyncio.Event()
    t3_entered = asyncio.Event()
    real_record = training.record_load_decision
    #: Every arrival at the recorder, in order. A cancelled follower must never
    #: appear here -- asserting only on stored rows would miss it entering and
    #: being deduped.
    arrivals: list[Any] = []

    async def _instrumented(*args: Any, **kwargs: Any) -> None:
        recommendation = args[1] if len(args) > 1 else None
        weight = getattr(recommendation, "weight", None)
        arrivals.append(weight)
        if weight == 60.0:                     # T1: hold it here.
            t1_entered.set()
            await release_t1.wait()
        elif weight == 62.5:                   # T3: record its arrival.
            t3_entered.set()
        await real_record(*args, **kwargs)

    monkeypatch.setattr(training, "record_load_decision", _instrumented)

    # 1. T1 running and held.
    task1 = training.schedule_load_decision_record(
        1, _decision(weight=60.0), **_OCCURRENCE
    )
    await asyncio.wait_for(t1_entered.wait(), timeout=5)
    assert not task1.done(), "precondition: T1 must still be pending"

    # 2. T2 chained behind T1.
    task2 = training.schedule_load_decision_record(
        1, _decision(weight=57.5), **_OCCURRENCE
    )
    for _ in range(10):
        await asyncio.sleep(0)
    assert _LOAD_AUDIT_CHAINS.get(key) is task2, "T2 should be the chain tail"
    assert not task2.done(), "T2 should be waiting behind T1"

    # 3. Cancel T2 while T1 is still pending.
    task2.cancel()
    with suppress(asyncio.CancelledError):
        await task2
    for _ in range(5):
        await asyncio.sleep(0)

    # T2 must ACTUALLY be cancelled. Shielding the predecessor while swallowing
    # this task's own CancelledError protects T1 and then ignores what cancel()
    # asked for: measured with a blanket `suppress(BaseException)`, T2 stayed
    # alive and entered the recorder beside a still-running T1.
    assert task2.cancelled(), (
        "the follower's cancellation was swallowed; it did not stay cancelled"
    )
    assert 57.5 not in arrivals, (
        "a cancelled follower entered record_load_decision"
    )

    # T1 must have survived the follower's cancellation.
    assert not task1.done(), "cancelling the follower killed its predecessor"

    # The key must still point at work that is genuinely in flight. Popping it
    # here is what lets T3 start beside T1.
    assert _LOAD_AUDIT_CHAINS.get(key) is task1, (
        "a cancelled tail disconnected the chain from its running predecessor"
    )

    # 4. T3 for the same durable key.
    task3 = training.schedule_load_decision_record(
        1, _decision(weight=62.5), **_OCCURRENCE
    )

    # 5. T3 must not reach the recorder while T1 is held.
    with suppress(asyncio.TimeoutError):
        await asyncio.wait_for(t3_entered.wait(), timeout=0.5)
    assert not t3_entered.is_set(), (
        "T3 ran concurrently with T1: cancelling T2 disconnected the chain"
    )

    # 6. Release T1.
    release_t1.set()

    # 7. T3 proceeds.
    await asyncio.wait_for(t3_entered.wait(), timeout=5)
    await asyncio.gather(task1, task3)
    for _ in range(5):
        await asyncio.sleep(0)

    # 8. The cancelled follower never recorded, and both registries retire.
    assert 57.5 not in arrivals, (
        "the cancelled follower recorded after its predecessor was released"
    )
    assert arrivals == [60.0, 62.5], f"unexpected recorder arrivals: {arrivals}"
    stored = [row["recommended_weight"] for row in await _audit_rows(db)]
    assert 57.5 not in stored, "the cancelled follower reached the audit table"
    assert training._LOAD_AUDIT_CHAINS == {}, "the chain registry leaked a key"
    assert training._LOAD_AUDIT_TASKS == set(), "the task registry leaked a task"
