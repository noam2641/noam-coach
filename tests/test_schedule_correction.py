"""W1-8 — a free-text schedule correction must mutate state, not just agree.

Live defect being pinned: at 07:01:03 the user wrote
"לדעתי אמרתי לו שאני מתאמן בשישי לא בשבת". The bot replied
"צודק, אשתמש במידע שכבר יש לי" and emitted no state mutation at all;
weekly_availability still decoded to Mon/Wed/Sat/Sun. The correction was
repeated at 07:15:09 and discarded again.

Every test here seeds the weekday set exactly as it was found in the live
database (monday_first_v1, weekdays [0, 2, 5, 6]).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import user_model
from db import Database
from helpers import utc_now
from noam_coach.services import health_jobs

# The utterance as it was actually typed, at 07:01:03.
LIVE_UTTERANCE = "לדעתי אמרתי לו שאני מתאמן בשישי לא בשבת"

MONDAY, WEDNESDAY, FRIDAY, SATURDAY, SUNDAY = 0, 2, 4, 5, 6

# The weekday set as found in the live database: Mon, Wed, Sat, Sun.
LIVE_WEEKDAYS = [MONDAY, WEDNESDAY, SATURDAY, SUNDAY]


async def _make_db(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "schedule_correction.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'Test',NULL,?)",
        (utc_now(),),
    )
    return db


def _patch_db(monkeypatch: pytest.MonkeyPatch, db: Database) -> None:
    monkeypatch.setattr(health_jobs, "DB", db)


async def _seed_live_schedule(db: Database) -> None:
    """Reproduce the live user_facts rows: Mon/Wed/Sat/Sun, no Friday."""
    slots = [
        {
            "weekday": day,
            "start": "18:00",
            "minutes": 45,
            "available": True,
            "weekday_schema": "monday_first_v1",
        }
        for day in LIVE_WEEKDAYS
    ]
    await user_model.set_fact(
        db, 1, "weekly_availability", slots,
        kind=user_model.KIND_FACT, source=user_model.SOURCE_USER, confirmed=True,
    )
    await user_model.set_fact(
        db, 1, "active_training_days", LIVE_WEEKDAYS,
        kind=user_model.KIND_FACT, source=user_model.SOURCE_USER, confirmed=True,
    )
    await user_model.set_fact(
        db, 1, "detected_training_days", LIVE_WEEKDAYS,
        kind=user_model.KIND_ESTIMATE, source=user_model.SOURCE_DERIVED, confirmed=False,
    )


def _weekdays_of(value: Any) -> list[int]:
    """Weekday indices out of a weekly_availability slot list."""
    return sorted(
        int(slot["weekday"])
        for slot in value
        if isinstance(slot, dict) and slot.get("available", True)
    )


async def _current_weekdays(db: Database) -> list[int]:
    """The stored availability weekdays, read the way the product reads them."""
    return _weekdays_of(await user_model.get_value(db, 1, "weekly_availability"))


@pytest.mark.asyncio
async def test_live_utterance_updates_the_weekday_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact text the user sent must be applied, not agreed with."""
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await _seed_live_schedule(db)

    applied, reply = await health_jobs.apply_schedule_correction(1, LIVE_UTTERANCE)

    assert applied is True, f"correction was discarded again; reply={reply!r}"
    availability = await user_model.get_value(db, 1, "weekly_availability")
    assert _weekdays_of(availability) != LIVE_WEEKDAYS, (
        "weekly_availability is byte-identical to the pre-correction state — "
        "this is exactly the W1-8 defect"
    )


@pytest.mark.asyncio
async def test_friday_present_and_saturday_absent_afterwards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """"בשישי לא בשבת" — Friday must be added AND Saturday removed.

    The underlying availability parser is additive: on this text it returns
    both Friday and Saturday. Adding Friday while keeping Saturday would be a
    different wrong answer, so the negation is asserted here explicitly.
    """
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await _seed_live_schedule(db)

    applied, _reply = await health_jobs.apply_schedule_correction(1, LIVE_UTTERANCE)
    assert applied is True

    days = _weekdays_of(await user_model.get_value(db, 1, "weekly_availability"))
    assert FRIDAY in days, "Friday (4) missing — the correction was not applied"
    assert SATURDAY not in days, "Saturday (5) still present — the negation was ignored"
    # Days the user never disputed must survive a partial correction.
    assert MONDAY in days and WEDNESDAY in days and SUNDAY in days
    assert days == [MONDAY, WEDNESDAY, FRIDAY, SUNDAY]


@pytest.mark.asyncio
async def test_all_weekday_stores_move_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W1-2 divergence guard: no store may keep the stale weekday set.

    Updating one store and leaving another is how a training frequency came to
    read 0.2 in one place and 4.0 in another.
    """
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await _seed_live_schedule(db)

    applied, _reply = await health_jobs.apply_schedule_correction(1, LIVE_UTTERANCE)
    assert applied is True

    expected = [MONDAY, WEDNESDAY, FRIDAY, SUNDAY]
    assert _weekdays_of(await user_model.get_value(db, 1, "weekly_availability")) == expected
    for key in ("active_training_days", "preferred_training_days", "detected_training_days"):
        stored = sorted(int(day) for day in await user_model.get_value(db, 1, key))
        assert stored == expected, f"{key} kept the stale weekday set {stored}"
    assert await user_model.get_value(db, 1, "training_days_per_week") == len(expected)


@pytest.mark.asyncio
async def test_correction_is_recorded_as_user_sourced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stated weekday is user input — not a confirmed derivation (cf. W1-6)."""
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await _seed_live_schedule(db)

    applied, _reply = await health_jobs.apply_schedule_correction(1, LIVE_UTTERANCE)
    assert applied is True

    for key in ("weekly_availability", "active_training_days", "detected_training_days"):
        fact = await user_model.get_fact(db, 1, key)
        assert fact is not None
        assert fact["source"] == user_model.SOURCE_USER, (
            f"{key} was not attributed to the user (source={fact['source']!r})"
        )
        assert fact["kind"] == user_model.KIND_FACT, (
            f"{key} was stored as {fact['kind']!r}, not a hard fact"
        )
        assert fact["confirmed"], f"{key} was left unconfirmed"


@pytest.mark.asyncio
async def test_meal_sentence_mentioning_a_weekday_does_not_trigger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A weekday mentioned in passing must never rewrite the schedule."""
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await _seed_live_schedule(db)

    applied, reply = await health_jobs.apply_schedule_correction(
        1, "אכלתי בשבת סלט עם טונה"
    )

    assert applied is False, "a meal description was treated as a schedule correction"
    assert reply.strip(), "a rejected correction must still say something"
    # And nothing moved.
    days = _weekdays_of(await user_model.get_value(db, 1, "weekly_availability"))
    assert days == LIVE_WEEKDAYS
    assert sorted(await user_model.get_value(db, 1, "active_training_days")) == LIVE_WEEKDAYS


@pytest.mark.asyncio
async def test_meal_sentence_with_a_negation_does_not_trigger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The negation split alone is not a trigger — "ולא" about food is not a
    schedule correction."""
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await _seed_live_schedule(db)

    applied, _reply = await health_jobs.apply_schedule_correction(
        1, "בשבת אכלתי חומוס ולא סלט"
    )

    assert applied is False
    days = _weekdays_of(await user_model.get_value(db, 1, "weekly_availability"))
    assert days == LIVE_WEEKDAYS


def test_parse_gate_rejects_non_corrections() -> None:
    """parse_schedule_correction is the pure gate — it must be inert on text
    that is not a training-day correction (no DB access involved)."""
    assert health_jobs.parse_schedule_correction("מה התפריט להיום?") is None
    assert health_jobs.parse_schedule_correction("אכלתי בשבת סלט עם טונה") is None
    assert health_jobs.parse_schedule_correction("") is None
    # A training sentence with no correction frame is left to the normal flows.
    assert health_jobs.parse_schedule_correction("סיימתי אימון") is None

    parsed = health_jobs.parse_schedule_correction(LIVE_UTTERANCE)
    assert parsed is not None
    assert parsed["asserted"] == [FRIDAY]
    assert parsed["removed"] == [SATURDAY]


@pytest.mark.asyncio
async def test_correction_emptying_the_schedule_is_refused_not_agreed_with(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never claim agreement without mutating: if the result would be no
    training days at all, the reply must say it was not applied."""
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await _seed_live_schedule(db)

    applied, reply = await health_jobs.apply_schedule_correction(
        1, "אמרתי לך שאני לא מתאמן בשני, ברביעי, בשבת ובראשון"
    )

    assert applied is False
    assert "לא עדכנתי" in reply
    days = _weekdays_of(await user_model.get_value(db, 1, "weekly_availability"))
    assert days == LIVE_WEEKDAYS


@pytest.mark.asyncio
async def test_a_stored_plan_is_left_to_the_rebuild_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The correction deliberately does NOT rewrite the stored plan.

    An earlier draft realigned `active_workout_plan` here, and an
    architecture guard rejected it: direct readers of that fact are
    allowlisted and everyone else must route through `workout_catalog`,
    which today exposes readers only -- there is no supported way to write
    a corrected weekday back. The guard's own failure message says a new
    reader is a signal to stop and report, not to broaden the allowlist.

    So the availability stores move and the stored plan does not. This test
    pins that gap deliberately rather than leaving it to be discovered: a
    user who corrects their schedule may still see a Saturday session until
    the plan is rebuilt. Closing it needs a write path in workout_catalog
    (and, for Tier-1, in planning.py) -- a separate task.
    """
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await _seed_live_schedule(db)
    stored_plan = {
        "sessions": [
            {"weekday": MONDAY, "code": "A", "name": "Push"},
            {"weekday": WEDNESDAY, "code": "B", "name": "Pull"},
            {"weekday": SATURDAY, "code": "C", "name": "Legs"},
            {"weekday": SUNDAY, "code": "D", "name": "Full"},
        ]
    }
    await user_model.set_fact(
        db, 1, "active_workout_plan", stored_plan,
        kind=user_model.KIND_FACT, source=user_model.SOURCE_USER, confirmed=True,
    )

    applied, _reply = await health_jobs.apply_schedule_correction(1, LIVE_UTTERANCE)
    assert applied is True, "the availability correction itself must still apply"

    plan = await user_model.get_value(db, 1, "active_workout_plan")
    assert [s["weekday"] for s in plan["sessions"]] == [
        MONDAY, WEDNESDAY, SATURDAY, SUNDAY
    ], "the stored plan is intentionally untouched by this path"


@pytest.mark.asyncio
async def test_correction_survives_a_later_save_routine_profile_recompute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Health-derived workout_pattern estimate arriving afterwards must not
    resurrect Saturday: sync_routine_to_facts writes workout_pattern only, and
    detected_training_days is re-derived solely when active_training_days is
    absent — which the correction guarantees it is not."""
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await _seed_live_schedule(db)

    applied, _reply = await health_jobs.apply_schedule_correction(1, LIVE_UTTERANCE)
    assert applied is True

    # Simulate what a later recompute writes: a fresh derived workout_pattern
    # that still believes Saturday is a training day.
    await user_model.set_fact(
        db, 1, "workout_pattern",
        {
            "weekly_frequency": 4.0,
            "common_weekdays": LIVE_WEEKDAYS,
            "typical_hour": "18:00",
            "weekday_schema": "monday_first_v1",
        },
        kind=user_model.KIND_ESTIMATE, source=user_model.SOURCE_DERIVED, confirmed=False,
    )

    from noam_coach.services.availability import resolve_availability

    resolved = await resolve_availability(db, 1)
    assert SATURDAY not in resolved.preferred_days, (
        "a derived estimate overwrote the user's stated schedule"
    )
    assert FRIDAY in resolved.preferred_days


# ---------------------------------------------------------------------------
# Displacement phrasings — every marker that drops a day must SPLIT, not add.
#
# "לא" was handled from the start. "במקום" ("instead of") was accepted as a
# correction marker but was NOT a split marker, so "מתאמן בשישי במקום בשבת"
# asserted BOTH Friday and Saturday -- the cumulative-parser bug in a second
# costume, silently keeping the day the user was dropping.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "utterance",
    [
        LIVE_UTTERANCE,
        "אני מתאמן בשישי במקום בשבת",
        "מתאמן בשישי ולא בשבת",
    ],
)
def test_every_displacement_phrasing_yields_the_same_delta(utterance: str) -> None:
    parsed = health_jobs.parse_schedule_correction(utterance)
    assert parsed is not None, f"not recognised as a correction: {utterance}"
    assert parsed["asserted"] == [FRIDAY]
    assert parsed["removed"] == [SATURDAY], (
        "the displaced day must be REMOVED, never asserted alongside the new one"
    )


def test_a_removal_only_correction_asserts_nothing() -> None:
    parsed = health_jobs.parse_schedule_correction("אני כבר לא מתאמן בשבת")
    assert parsed is not None
    assert parsed["asserted"] == []
    assert parsed["removed"] == [SATURDAY]


def test_an_addition_only_correction_removes_nothing() -> None:
    parsed = health_jobs.parse_schedule_correction("אמרתי לך שאני מתאמן גם בשישי")
    assert parsed is not None
    assert parsed["asserted"] == [FRIDAY]
    assert parsed["removed"] == []


@pytest.mark.asyncio
async def test_removing_a_day_that_is_already_absent_is_a_no_op(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Idempotency: dropping Thursday when Thursday was never there."""
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await _seed_live_schedule(db)

    applied, _reply = await health_jobs.apply_schedule_correction(
        1, "אני מתאמן בשישי לא בחמישי"
    )

    assert applied is True
    days = await _current_weekdays(db)
    assert SATURDAY in days, "an unrelated day must not be disturbed"
    assert FRIDAY in days


@pytest.mark.asyncio
async def test_asserting_a_day_that_is_already_active_is_a_no_op(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repeating an existing day must not duplicate or reorder it."""
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await _seed_live_schedule(db)
    before = await _current_weekdays(db)

    applied, _reply = await health_jobs.apply_schedule_correction(
        1, "אמרתי לך שאני מתאמן בשני"
    )

    assert applied is True
    after = await _current_weekdays(db)
    assert sorted(after) == sorted(before), "an already-active day changed the set"
    assert len(after) == len(set(after)), "the day was duplicated"
