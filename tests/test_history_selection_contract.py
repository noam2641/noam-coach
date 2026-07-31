"""One contract for selecting exercise history, and one honest absence.

Four production sites read set history keyed on the canonical exercise id, and
they were written independently: `recommend_load_decision` picks sessions and
then re-queries each one, `show_session` renders a "previous performance" line,
and `previous_weight_context` decides whether "אותו משקל" is even offered. Three
answers to one question means a screen can show one exercise's history while the
recommendation beside it rests on another's.

The sharper problem this file pins is the absence case. `Database.fetch_all`
returns `[]` when the database file does not exist -- a deliberate guard against
sqlite materialising a stray file -- so an unreadable database was
indistinguishable from a user who has never performed the exercise. Both became
`missing_context=["exercise_history"]` at confidence 55: an infrastructure
condition reported as a data condition, with nothing logged.

`select_exercise_history` keeps those two apart and says which identity layer
answered.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from db import Database
from helpers import utc_now
from noam_coach.services import training


async def _db(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "history.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (utc_now(),),
    )
    return db


async def _completed_session_with_sets(
    db: Database, exercise_id: str = "bench", *, source: str = "telegram_one_tap"
) -> int:
    session_id = await db.execute(
        "INSERT INTO sessions(user_id, code, name, plan, status, exercise_index, "
        "set_number, started_at, ended_at) "
        "VALUES(1,'A','A','{}', 'completed', 0, 1, ?, ?)",
        (utc_now(), utc_now()),
    )
    await db.execute(
        "INSERT INTO sets(session_id, exercise_id, exercise_name, set_number, weight, "
        "reps, rir, source, exercise_index, created_at) "
        "VALUES(?, ?, 'Bench', 1, 60, 8, 2, ?, 0, ?)",
        (session_id, exercise_id, source, utc_now()),
    )
    return int(session_id)


# ---------------------------------------------------------------------------
# The two absences are not the same absence
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_user_who_never_trained_reports_no_history(tmp_path) -> None:
    db = await _db(tmp_path)

    selection = await training.select_exercise_history(db, 1, "bench")

    assert not selection.found
    assert selection.layer == training.HISTORY_LAYER_NONE
    assert selection.absent_reason == training.HISTORY_ABSENT_NO_HISTORY
    assert selection.is_unavailable is False, (
        "a genuine absence must never look like a failure -- it is the normal "
        "state of every new exercise"
    )


@pytest.mark.asyncio
async def test_an_unreadable_database_is_not_reported_as_no_history(
    tmp_path, caplog
) -> None:
    """The failure this contract exists to stop.

    Before this change a query error became an empty list, which became
    `missing_context=["exercise_history"]`, which the UI renders as "no history
    yet". The user is told something about their training when the truth is that
    the database could not be read.
    """

    class _Broken:
        async def fetch_all(self, *_a, **_k):
            raise RuntimeError("database is locked")

    with caplog.at_level(logging.ERROR):
        selection = await training.select_exercise_history(_Broken(), 1, "bench")

    assert not selection.found
    assert selection.absent_reason == training.HISTORY_ABSENT_UNAVAILABLE
    assert selection.is_unavailable is True
    assert selection.missing_context() == (training.HISTORY_ABSENT_UNAVAILABLE,)
    assert selection.missing_context() != (training.HISTORY_ABSENT_NO_HISTORY,), (
        "an unreadable database must not be reported as an absence of training"
    )

    # The operator has to be able to see it, or the distinction only exists in
    # a return value nobody reads.
    assert any(
        "history_selection_failed" in record.message for record in caplog.records
    ), "a query failure must be logged, not just returned"


@pytest.mark.asyncio
async def test_the_failure_log_carries_ids_not_training_data(tmp_path, caplog) -> None:
    """Bounded reason code and internal ids only.

    Set rows are the user's training data. A log line that dumps them turns an
    operational signal into a privacy problem, and the exception path is exactly
    where that mistake is easiest to make.
    """
    class _Broken:
        async def fetch_all(self, *_a, **_k):
            raise RuntimeError("weight=225 reps=5 -- payload that must not leak")

    with caplog.at_level(logging.ERROR):
        await training.select_exercise_history(_Broken(), 1, "bench")

    emitted = "\n".join(record.message for record in caplog.records)
    assert "user_id=1" in emitted
    assert "exercise_id=bench" in emitted
    assert "225" not in emitted, (
        "the log formats only ids and a layer name; row contents must never be "
        "interpolated into it"
    )


# ---------------------------------------------------------------------------
# The layer is reported
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_found_history_reports_the_canonical_layer(tmp_path) -> None:
    """Canonical is the only reachable layer until machine identity exists.

    Pinned so the day a stronger layer is added, this test fails and states the
    change rather than letting it pass unnoticed.
    """
    db = await _db(tmp_path)
    await _completed_session_with_sets(db)

    selection = await training.select_exercise_history(db, 1, "bench")

    assert selection.found
    assert selection.layer == training.HISTORY_LAYER_CANONICAL
    assert selection.absent_reason is None
    assert selection.missing_context() == ()


@pytest.mark.asyncio
async def test_history_is_scoped_to_the_exercise_and_the_user(tmp_path) -> None:
    db = await _db(tmp_path)
    await _completed_session_with_sets(db, exercise_id="squat")

    assert (await training.select_exercise_history(db, 1, "squat")).found
    assert not (await training.select_exercise_history(db, 1, "bench")).found
    assert not (await training.select_exercise_history(db, 2, "squat")).found


# ---------------------------------------------------------------------------
# Confidence ceiling
# ---------------------------------------------------------------------------
def test_the_canonical_layer_does_not_lower_existing_confidence() -> None:
    """A5 must not change any load decision today.

    The ceiling exists for the weaker layers a later item introduces. Applying
    it to canonical history would silently de-rate every current recommendation.
    """
    selection = training.HistorySelection(
        rows=({"id": 1},), layer=training.HISTORY_LAYER_CANONICAL
    )
    for base in (55, 68, 78, 82, 86, 88, 90):
        assert selection.confidence_ceiling(base) == base


def test_a_weaker_layer_caps_confidence() -> None:
    """A cross-machine answer is a starting point, not the user's weight."""
    selection = training.HistorySelection(
        rows=({"id": 1},), layer=training.HISTORY_LAYER_EQUIVALENT
    )
    assert selection.confidence_ceiling(90) == 74
    assert selection.confidence_ceiling(60) == 60, "a ceiling, never a floor"


def test_confidence_ceiling_is_a_noop_for_an_unknown_layer() -> None:
    selection = training.HistorySelection(layer=training.HISTORY_LAYER_NONE)
    assert selection.confidence_ceiling(55) == 55


# ---------------------------------------------------------------------------
# All exercise-keyed readers agree on the identity they key on
# ---------------------------------------------------------------------------
def test_every_exercise_keyed_history_reader_keys_on_exercise_id() -> None:
    """The invariant that keeps the screen and the recommendation in agreement.

    Four production sites read set history for a specific exercise. They ask
    different questions -- which sessions inform the load, versus the single
    most recent set -- so they are deliberately separate queries. What they must
    never disagree about is WHICH exercise they mean.

    A new reader that keyed on `exercise_name`, or on a positional index, would
    silently describe a different exercise than the one beside it on screen.
    This scans for that rather than trusting review to catch it.

    Two further readers (`build_fatigue_assessment`, `reconcile
    ._session_perf_by_day`) key on `session_id` only and are intentionally
    exercise-blind -- they aggregate a whole session. They are excluded here
    because giving them an exercise dimension would change what they measure.
    """
    import re
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parents[1]
    # (file, number of exercise-keyed history queries expected)
    expected = {
        "noam_coach/services/training.py": 2,
        "noam_coach/bot/workout.py": 2,
    }

    # A history READ that names an exercise. Anchored to SELECT so the
    # split-secondary DELETE in `undo_last_set` -- which also filters on
    # exercise_id -- is not counted as a reader. It removes rows rather than
    # interpreting them, so it has no bearing on which history a screen shows.
    pattern = re.compile(
        r"SELECT\b[^;]{0,600}?exercise_id\s*=\s*\?",
        re.IGNORECASE | re.DOTALL,
    )

    for relative, count in expected.items():
        source = (root / relative).read_text(encoding="utf-8")
        found = len(pattern.findall(source))
        assert found == count, (
            f"{relative}: expected {count} exercise-keyed history queries, "
            f"found {found}. A new reader must key on exercise_id like the "
            "others, or it will describe a different exercise than the "
            "recommendation shown beside it. If a reader was legitimately "
            "added or removed, update this count deliberately."
        )


def test_no_history_reader_keys_on_exercise_name() -> None:
    """Names are display strings, not identity.

    `sets.exercise_name` is stored for readability. Keying history on it would
    break the moment a name is edited or localised, and the failure would look
    like lost training history rather than a lookup bug.
    """
    import re
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parents[1]
    offenders = []
    for relative in ("noam_coach/services/training.py", "noam_coach/bot/workout.py"):
        source = (root / relative).read_text(encoding="utf-8")
        if re.search(r"exercise_name\s*=\s*\?", source, re.IGNORECASE):
            offenders.append(relative)

    assert not offenders, (
        f"history is being looked up by display name in {offenders}; key on "
        "exercise_id instead -- a name can change without the history moving"
    )
