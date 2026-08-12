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
from pathlib import Path
from typing import Any

import pytest

from db import Database
from helpers import utc_now
from noam_coach.services import training


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
        1, _decision(), exercise_id="leg_press", session_id=7,
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
            1, _decision(), exercise_id="leg_press", session_id=7,
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
        1, decision, exercise_id="leg_press", session_id=7,
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
    record_at = source.index("record_load_decision(")
    return_at = source.index("return {\n        \"active\": True,\n        \"session_id\"")

    assert record_at < return_at, "recording must be scheduled before the return"
    scheduled = source[record_at - 200:record_at]
    assert "ensure_future" in scheduled or "create_task" in scheduled, (
        "the Watch recording is awaited inline; it will delay every poll"
    )


def test_the_telegram_card_records_after_it_is_presented() -> None:
    """The card is rendered by `safe_edit`; recording follows it.

    Reversed, this would put database latency between the user's tap and the
    card they are waiting for.
    """
    import inspect

    from noam_coach.bot import workout

    source = inspect.getsource(workout.show_session)
    present_at = source.rindex("await safe_edit(query, text, keyboard)")
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
    assert "if recommendation_presented:" in source
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
            1, _decision(), exercise_id="leg_press", session_id=7,
            set_number=1, channel=training.LOAD_CHANNEL_WATCH,
        )
    )
    await task

    rows = await _audit_rows(db)
    assert len(rows) == 1
    assert rows[0]["channel"] == "watch"
