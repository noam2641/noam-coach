"""The read-only slot foundation: three absences that must stay distinct.

A slot is the stable professional need a plan expresses; the exercise
implementing it may change beneath it. A11a lands only the vocabulary, the
reader helpers and the defensive guards — nothing mints a slot yet — so every
assertion here is either about today's payloads (which carry no slot data and
must behave exactly as before) or about a shape that only exists once A11b
starts writing.

The distinction the module exists to hold is between three situations that all
look like "no exercise" from a distance:

* **legacy** — written before slots existed. Valid, and the common case.
* **unmapped / blocked** — deliberately without an implementation. A state, not
  an error.
* **malformed** — violates the shape contract. May render safely, but must stay
  observable: silently treating it as "no exercise" is how a data defect
  becomes invisible.

Collapsing any two of those is the failure this file guards against.
"""

from __future__ import annotations

import logging

import pytest

from noam_coach.services import workout_slots as slots


# ---------------------------------------------------------------------------
# Legacy payloads are untouched
# ---------------------------------------------------------------------------
def test_a_legacy_entry_has_no_slot_id_and_that_is_normal() -> None:
    """None means "predates slots", never "broken".

    Every payload in the database today answers None here. If this ever implied
    a defect, the whole existing corpus would be reported as malformed.
    """
    legacy = {"id": "bench", "name": "Bench", "sets": 3, "rmin": 5, "rmax": 8}

    assert slots.slot_id_of(legacy) is None
    assert slots.classify_entry(legacy) == slots.ENTRY_LEGACY_NO_SLOT
    assert slots.is_performable(legacy) is True


def test_a_legacy_entry_defaults_to_mapped_not_unmapped() -> None:
    """Defaulting the other way would reclassify every existing plan.

    A legacy entry names an exercise the user is expected to perform. Reading
    it as `unmapped` because it lacks a `slot_state` key would mark every plan
    in the database incomplete overnight.
    """
    assert slots.slot_state_of({"id": "bench"}) == slots.SLOT_STATE_MAPPED


def test_legacy_set_counting_is_unchanged() -> None:
    legacy = [{"id": "a", "sets": 3}, {"id": "b", "sets": 4}]
    assert sum(slots.planned_sets_of(e) for e in legacy) == 7


# ---------------------------------------------------------------------------
# Deliberate absence is a state, not an error
# ---------------------------------------------------------------------------
def test_an_unmapped_slot_is_not_performable_and_not_malformed() -> None:
    """The core-model state: a slot exists, its implementation does not yet."""
    unmapped = {"slot_id": "A:2", "slot_state": slots.SLOT_STATE_UNMAPPED}

    assert slots.classify_entry(unmapped) == slots.ENTRY_UNMAPPED
    assert slots.is_performable(unmapped) is False
    assert slots.planned_sets_of(unmapped) == 0


def test_a_blocked_slot_is_reported_as_blocked_not_merely_unmapped() -> None:
    """Blocked carries a reason a user can be told; unmapped does not.

    A slot withheld because of pain and a slot not yet chosen both lack an
    exercise, but only one of them should say "we are protecting you".
    """
    blocked = {
        "slot_id": "A:1",
        "slot_state": slots.SLOT_STATE_BLOCKED,
        "id": "bench",
    }

    assert slots.classify_entry(blocked) == slots.ENTRY_BLOCKED
    assert slots.is_performable(blocked) is False


def test_a_slot_bearing_entry_without_an_id_is_unmapped_not_malformed() -> None:
    """Slot-bearing payloads CAN express "no exercise here"; legacy cannot.

    That asymmetry is the whole reason `classify_entry` looks at `slot_id`
    before deciding whether a missing `id` is a defect.
    """
    assert slots.classify_entry({"slot_id": "A:3"}) == slots.ENTRY_UNMAPPED


def test_reserved_states_are_accepted_not_treated_as_corruption() -> None:
    """Four states are declared but unshipped.

    They have no producer yet, so rendering paths for them would be untested
    speculation. Accepting them on read means a payload written by a later
    version is a known state rather than malformed data.
    """
    for state in sorted(slots.RESERVED_SLOT_STATES):
        entry = {"slot_id": "A:1", "slot_state": state, "id": "bench"}
        assert slots.slot_state_of(entry) == state
        assert slots.classify_entry(entry) != slots.ENTRY_MALFORMED


# ---------------------------------------------------------------------------
# Malformed data renders safely but stays visible
# ---------------------------------------------------------------------------
def test_a_legacy_entry_with_no_id_is_malformed() -> None:
    """Pre-slot payloads have no way to say "no exercise here".

    So an entry with neither a slot id nor an exercise id is a defect, not an
    intentional gap — and must not be quietly counted as one.
    """
    assert slots.classify_entry({"name": "orphan", "sets": 3}) == slots.ENTRY_MALFORMED
    assert slots.is_performable({"name": "orphan"}) is False


def test_a_non_dict_entry_is_malformed_not_a_crash() -> None:
    for junk in (None, "bench", 42, ["bench"]):
        assert slots.classify_entry(junk) == slots.ENTRY_MALFORMED
        assert slots.planned_sets_of(junk) == 0


def test_malformed_entries_are_logged_with_counts_only(caplog) -> None:
    """Observable, but never at the cost of leaking the payload.

    A plan payload is the user's training programme. The log line carries
    bounded counts and internal ids so an operator can see that something is
    wrong without the contents ending up in a log aggregator.
    """
    entries = [
        {"id": "bench", "sets": 3},
        {"name": "orphan"},
        None,
    ]

    with caplog.at_level(logging.WARNING):
        counts = slots.observe_plan_entries(
            entries, user_id=7, session_id=99, context="unit_test"
        )

    assert counts[slots.ENTRY_MALFORMED] == 2
    assert counts[slots.ENTRY_LEGACY_NO_SLOT] == 1

    emitted = "\n".join(record.message for record in caplog.records)
    assert "plan_entries_malformed" in emitted
    assert "user_id=7" in emitted
    assert "session_id=99" in emitted
    assert "bench" not in emitted, "payload contents must never reach the log"
    assert "orphan" not in emitted


def test_legitimate_absences_are_counted_but_not_logged(caplog) -> None:
    """Logging normal states would bury the one case that matters.

    Unmapped and blocked slots are expected. If they warned, the malformed
    warning would be lost in the noise within a day.
    """
    entries = [
        {"slot_id": "A:1", "slot_state": slots.SLOT_STATE_UNMAPPED},
        {"slot_id": "A:2", "slot_state": slots.SLOT_STATE_BLOCKED, "id": "squat"},
        {"id": "bench", "sets": 3},
    ]

    with caplog.at_level(logging.WARNING):
        counts = slots.observe_plan_entries(entries, context="unit_test")

    assert counts[slots.ENTRY_UNMAPPED] == 1
    assert counts[slots.ENTRY_BLOCKED] == 1
    assert slots.ENTRY_MALFORMED not in counts
    assert not caplog.records, "a normal plan must produce no warnings"


def test_a_non_list_exercises_value_is_reported_not_raised(caplog) -> None:
    with caplog.at_level(logging.WARNING):
        counts = slots.observe_plan_entries(None, context="unit_test")

    assert counts == {slots.ENTRY_MALFORMED: 1}
    assert any("exercises_not_a_list" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# The summary path: one bad entry must not zero the whole count
# ---------------------------------------------------------------------------
def test_one_malformed_entry_does_not_discard_every_other_set() -> None:
    """The defect this guard closes.

    `sum(int(ex["sets"]) for ex in exercises)` inside a single try meant one
    entry missing the key raised, the except zeroed the total, and the user was
    told 0 sets were planned — after finishing the workout.
    """
    mixed = [
        {"id": "a", "sets": 3},
        {"name": "malformed-no-id"},
        {"id": "c", "sets": 4},
        {"slot_id": "A:9", "slot_state": slots.SLOT_STATE_UNMAPPED},
    ]

    total = sum(slots.planned_sets_of(e) for e in mixed)

    assert total == 7, (
        "well-formed exercises must still count when a sibling entry is "
        "malformed or deliberately unmapped"
    )


def test_a_non_numeric_sets_value_contributes_zero_not_an_exception() -> None:
    assert slots.planned_sets_of({"id": "a", "sets": "three"}) == 0
    assert slots.planned_sets_of({"id": "a", "sets": None}) == 0
    assert slots.planned_sets_of({"id": "a"}) == 0


def test_a_negative_set_count_is_clamped() -> None:
    assert slots.planned_sets_of({"id": "a", "sets": -5}) == 0


# ---------------------------------------------------------------------------
# Scope: A11a adds no writers
# ---------------------------------------------------------------------------
def test_the_slot_module_contains_no_write_path() -> None:
    """A11a is read-only by contract, not just by intent.

    Minting slots is A11b. A writer appearing here would cross that boundary
    without the review that scope change deserves.
    """
    import inspect

    source = inspect.getsource(slots)
    for forbidden in ("INSERT ", "UPDATE ", "DELETE ", "set_fact", "execute("):
        assert forbidden not in source, (
            f"{forbidden!r} found in the read-only slot foundation; minting and "
            "persistence belong to A11b"
        )


# ---------------------------------------------------------------------------
# The consumer actually uses it
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_workout_summary_survives_a_malformed_plan_entry(
    tmp_path, monkeypatch
) -> None:
    """End-to-end on the path where the old failure was worst.

    `workout_summary` runs after the user has finished. Zeroing the planned-set
    count there tells someone who just trained that nothing was planned, which
    reads as lost data rather than a payload defect.
    """
    import json

    import coach_bot
    from db import Database
    from helpers import utc_now
    from noam_coach.bot import workout as workout_bot

    db = Database(str(tmp_path / "summary.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (utc_now(),),
    )
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(workout_bot, "DB", db)

    plan = {
        "name": "Mixed",
        "exercises": [
            {"id": "a", "name": "A", "sets": 3},
            {"name": "malformed-no-id"},          # defect
            {"id": "c", "name": "C", "sets": 4},
        ],
    }
    session_id = await db.execute(
        "INSERT INTO sessions(user_id, code, name, plan, status, exercise_index, "
        "set_number, started_at, ended_at) VALUES(1,'T','Mixed',?, 'completed', 0, 1, ?, ?)",
        (json.dumps(plan, ensure_ascii=False), utc_now(), utc_now()),
    )

    summary = await workout_bot.workout_summary(1, int(session_id))

    # 7 planned sets survive the malformed sibling. Asserted through the
    # rendered summary because that is what the user actually reads.
    assert "7" in summary, (
        "a malformed entry zeroed the whole planned-set count; well-formed "
        "exercises must still be counted"
    )
