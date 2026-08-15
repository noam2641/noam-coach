"""W1-6 — a confirm tap must not launder a derivation into a user statement.

Five sites in the health wizard used to take a value DERIVED from the health
export and rewrite it as source=user_report/confirmed=1 — the exact token
``_confirmed_fact_value`` checks for when it asks "did the user really say
this?".  A value computed from a single workout became indistinguishable from
one the user typed, and because ``set_fact`` writes history only for a key that
already exists, these wizard-created keys lost their origin entirely.

These tests pin the three properties that fix must hold:
  1. a confirmed derived value is distinguishable from a typed one;
  2. ``_confirmed_fact_value`` still accepts the wizard's write (the gate must
     not silently break);
  3. the original derivation is recoverable afterwards.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import coach_bot
import routine
import user_model
from db import Database
from helpers import utc_now
from noam_coach.bot import onboarding as onboarding_bot
from noam_coach.services import core as core_services
from noam_coach.services import health_jobs


async def _db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Database:
    db = Database(str(tmp_path / "w6.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'Test',NULL,?)",
        (utc_now(),),
    )
    for mod in (coach_bot, health_jobs, onboarding_bot, core_services):
        monkeypatch.setattr(mod, "DB", db, raising=False)
    return db


async def _seed_pattern(db: Database, **overrides: Any) -> None:
    """A workout_pattern derived from a thin sample — the real-world case."""
    value: dict[str, Any] = {
        "weekly_frequency": 3,
        "typical_hour": "19:00",
        "common_weekdays": [0, 2, 4],
        "avg_duration_minutes": 37.6,
        "sessions_sampled": 1,
    }
    value.update(overrides)
    await user_model.set_fact(
        db, 1, "workout_pattern", value,
        kind=user_model.KIND_ESTIMATE,
        source=user_model.SOURCE_DERIVED,
        confirmed=False,
    )


# --- 1. derived-and-confirmed is distinguishable from typed ----------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "step_id,key",
    [
        (health_jobs.WIZARD_STEP_WORKOUT_DURATION, "session_minutes"),
        (health_jobs.WIZARD_STEP_WORKOUT_HOUR, "workout_window"),
        (health_jobs.WIZARD_STEP_WORKOUT_FREQUENCY, "training_days_per_week"),
        (health_jobs.WIZARD_STEP_WORKOUT_DAYS, "weekly_availability"),
    ],
)
async def test_confirmed_derived_value_is_not_recorded_as_user_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, step_id: str, key: str
) -> None:
    """The confirm tap must never claim the user authored the number."""
    db = await _db(tmp_path, monkeypatch)
    await _seed_pattern(db)

    await health_jobs.confirm_health_wizard_step(1, step_id)

    fact = await user_model.get_fact(db, 1, key)
    assert fact is not None, f"{key} should have been written"
    assert fact["source"] != user_model.SOURCE_USER, (
        f"{key} was stored as a user statement; it was derived from the "
        "health export and only confirmed by a tap"
    )
    assert fact["source"] == user_model.SOURCE_DERIVED
    assert fact["confirmed"], "the tap is real evidence and must be recorded"


@pytest.mark.asyncio
async def test_typed_answer_still_records_as_user_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The contrast case: a value the user actually types stays SOURCE_USER.

    Without this, "don't launder derivations" could be trivially satisfied by
    downgrading everything, destroying the distinction from the other side.
    """
    db = await _db(tmp_path, monkeypatch)
    await _seed_pattern(db)

    ok, _msg = await health_jobs.apply_health_wizard_text_edit(
        1, health_jobs.WIZARD_STEP_WORKOUT_DURATION, "55"
    )
    assert ok

    fact = await user_model.get_fact(db, 1, "session_minutes")
    assert fact is not None
    assert fact["source"] == user_model.SOURCE_USER
    assert fact["value"] == 55


@pytest.mark.asyncio
async def test_tapped_and_typed_values_are_distinguishable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The core property, stated directly: the two acts differ in the store."""
    db = await _db(tmp_path, monkeypatch)

    await _seed_pattern(db)
    await health_jobs.confirm_health_wizard_step(
        1, health_jobs.WIZARD_STEP_WORKOUT_HOUR
    )
    tapped = await user_model.get_fact(db, 1, "workout_window")

    ok, _ = await health_jobs.apply_health_wizard_text_edit(
        1, health_jobs.WIZARD_STEP_WORKOUT_HOUR, "07:30"
    )
    assert ok
    typed = await user_model.get_fact(db, 1, "workout_window")

    assert tapped["source"] != typed["source"], (
        "a tapped machine guess and a typed answer must not be stored "
        "identically"
    )
    assert user_model._audit_source_kind(tapped) == "inferred"
    assert user_model._audit_source_kind(typed) == "user"


# --- 2. the _confirmed_fact_value gate still works for the wizard ----------


@pytest.mark.asyncio
async def test_gate_accepts_wizard_confirmed_derived_fact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Changing the representation must not silently break the wizard.

    ``_confirmed_fact_value`` gates the "already answered, stop asking" checks.
    If the new representation stopped matching it, the wizard would re-ask
    forever.
    """
    db = await _db(tmp_path, monkeypatch)
    await _seed_pattern(db)

    await health_jobs.confirm_health_wizard_step(
        1, health_jobs.WIZARD_STEP_WORKOUT_HOUR
    )
    fact = await user_model.get_fact(db, 1, "workout_window")
    assert health_jobs._confirmed_fact_value(fact) == "19:00"
    assert await health_jobs._has_manual_training_hour(1) is True


@pytest.mark.asyncio
async def test_gate_accepts_confirmed_frequency_and_days(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _db(tmp_path, monkeypatch)
    await _seed_pattern(db)

    await health_jobs.confirm_health_wizard_step(
        1, health_jobs.WIZARD_STEP_WORKOUT_FREQUENCY
    )
    await health_jobs.confirm_health_wizard_step(
        1, health_jobs.WIZARD_STEP_WORKOUT_DAYS
    )

    assert await health_jobs._has_manual_training_frequency(1) is True
    assert await health_jobs._has_manual_training_days(1) is True
    assert await health_jobs._desired_weekly_frequency(1) == 3


@pytest.mark.asyncio
async def test_gate_still_rejects_unconfirmed_derivation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The case the gate was originally written for must keep working.

    An estimate nobody confirmed is still not a manual override.
    """
    db = await _db(tmp_path, monkeypatch)
    await user_model.set_fact(
        db, 1, "workout_window", "19:00",
        kind=user_model.KIND_ESTIMATE,
        source=user_model.SOURCE_DERIVED,
        confirmed=False,
    )
    fact = await user_model.get_fact(db, 1, "workout_window")
    assert health_jobs._confirmed_fact_value(fact) is None
    assert await health_jobs._has_manual_training_hour(1) is False


@pytest.mark.asyncio
async def test_gate_rejects_confirmed_but_unpromoted_estimate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Broadening the gate must not let *any* hardened estimate through.

    Only the wizard's deliberate promotion to KIND_FACT counts; a
    KIND_ESTIMATE that merely got confirmed=1 elsewhere still does not qualify
    as a manual override.
    """
    db = await _db(tmp_path, monkeypatch)
    await user_model.set_fact(
        db, 1, "workout_window", "19:00",
        kind=user_model.KIND_ESTIMATE,
        source=user_model.SOURCE_DERIVED,
        confirmed=True,
    )
    fact = await user_model.get_fact(db, 1, "workout_window")
    assert health_jobs._confirmed_fact_value(fact) is None


@pytest.mark.asyncio
async def test_gate_still_accepts_typed_user_fact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _db(tmp_path, monkeypatch)
    await user_model.set_fact(
        db, 1, "workout_window", "07:30",
        kind=user_model.KIND_FACT,
        source=user_model.SOURCE_USER,
        confirmed=True,
    )
    fact = await user_model.get_fact(db, 1, "workout_window")
    assert health_jobs._confirmed_fact_value(fact) == "07:30"


# --- 3. the original derivation stays recoverable --------------------------


@pytest.mark.asyncio
async def test_derivation_is_recoverable_after_confirm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """session_minutes=38 must still be traceable to avg_duration_minutes=37.6.

    This is the case that motivated the task: the stored value is a rounded
    integer, the key is created by the tap (so set_fact writes no history row),
    and without an explicit record the 37.6 is gone forever.
    """
    db = await _db(tmp_path, monkeypatch)
    await _seed_pattern(db)

    await health_jobs.confirm_health_wizard_step(
        1, health_jobs.WIZARD_STEP_WORKOUT_DURATION
    )

    note = await health_jobs.read_wizard_provenance(1, "session_minutes")
    assert note is not None, "the derivation behind the confirmed value is lost"
    assert note["source_fact"] == "workout_pattern"
    assert note["source_field"] == "avg_duration_minutes"
    assert note["raw_value"] == pytest.approx(37.6)
    assert note["confirmed_by_user"] is True


@pytest.mark.asyncio
async def test_provenance_records_thin_evidence_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A value backed by one workout must stay legible as such."""
    db = await _db(tmp_path, monkeypatch)
    await _seed_pattern(db, sessions_sampled=1)

    await health_jobs.confirm_health_wizard_step(
        1, health_jobs.WIZARD_STEP_WORKOUT_HOUR
    )

    note = await health_jobs.read_wizard_provenance(1, "workout_window")
    assert note is not None
    assert note["sessions_sampled"] == 1
    assert note["raw_value"] == "19:00"


@pytest.mark.asyncio
async def test_provenance_recorded_for_trend_button(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The trend-proposal button is also a machine guess the user accepted."""
    db = await _db(tmp_path, monkeypatch)
    await _seed_pattern(db)

    await health_jobs.apply_health_wizard_trend_choice(1, "workout_pattern", 4.0)

    fact = await user_model.get_fact(db, 1, "training_days_per_week")
    assert fact["source"] == user_model.SOURCE_DERIVED
    assert fact["confirmed"]

    note = await health_jobs.read_wizard_provenance(1, "training_days_per_week")
    assert note is not None
    assert note["source_field"] == "trend_proposal"
    assert note["raw_value"] == 4.0


@pytest.mark.asyncio
async def test_provenance_absent_for_typed_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A typed answer has no derivation to recover — nothing is fabricated."""
    db = await _db(tmp_path, monkeypatch)
    await _seed_pattern(db)

    ok, _ = await health_jobs.apply_health_wizard_text_edit(
        1, health_jobs.WIZARD_STEP_WORKOUT_DURATION, "55"
    )
    assert ok
    assert await health_jobs.read_wizard_provenance(1, "session_minutes") is None


# ---------------------------------------------------------------------------
# W1-20 — the pending-fact display must not render a zero-width eating window
# as "אכילה בין X ל-X". Drives the real production formatter used by the
# wizard/confirm prompts (_format_pending_fact_value).
# ---------------------------------------------------------------------------


def _eating_display(value: dict[str, Any]) -> str:
    return health_jobs._format_pending_fact_value("eating_windows", value)


def test_degenerate_eating_window_is_not_rendered_as_a_range() -> None:
    """The live defect: one logged day rendered as "אכילה בין 08:00 ל-08:00"."""
    display = _eating_display(
        {"first_meal_time": "08:00", "last_meal_time": "08:00", "meals_sampled": 1}
    )
    assert "08:00 ל-08:00" not in display
    assert "בין" not in display


def test_thinly_sampled_eating_window_is_not_rendered_as_a_range() -> None:
    display = _eating_display(
        {"first_meal_time": "08:00", "last_meal_time": "21:00", "meals_sampled": 2}
    )
    assert "בין" not in display


def test_well_evidenced_eating_window_is_still_rendered_as_a_range() -> None:
    display = _eating_display(
        {"first_meal_time": "08:00", "last_meal_time": "21:00", "meals_sampled": 120}
    )
    assert display == "אכילה בין 08:00 ל-21:00"


def test_legitimately_narrow_eating_window_is_still_rendered_as_a_range() -> None:
    """ANTI-OVER-CORRECTION: a real 12:00-17:00 fasting routine is a range."""
    display = _eating_display(
        {"first_meal_time": "12:00", "last_meal_time": "17:00", "meals_sampled": 90}
    )
    assert display == "אכילה בין 12:00 ל-17:00"


def test_typical_meal_hours_still_take_precedence() -> None:
    """Unchanged pre-existing behaviour: observed meal hours are a list of real
    observations, not a window interpretation, so they still render first."""
    display = _eating_display(
        {
            "typical_meal_hours": ["08:00", "13:00"],
            "first_meal_time": "08:00",
            "last_meal_time": "08:00",
            "meals_sampled": 1,
        }
    )
    assert display == "ארוחות בדרך כלל סביב 08:00, 13:00"


def test_absent_and_degenerate_eating_windows_are_both_withheld() -> None:
    """States 1 and 2 give the same *display* verdict but keep distinct data."""
    absent = _eating_display({})
    degenerate = _eating_display(
        {"first_meal_time": "08:00", "last_meal_time": "08:00", "meals_sampled": 1}
    )
    assert absent == degenerate == "חלונות אכילה"
    # The evidence itself is untouched — nothing was cleared to produce this.
    assert routine.eating_window_is_trustworthy(
        {"first_meal_time": "08:00", "last_meal_time": "08:00", "meals_sampled": 1}
    ) is False
