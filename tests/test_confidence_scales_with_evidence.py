"""W1-5 — confidence must reflect how much evidence stands behind a fact.

Before this change ``confidence`` was a pure function of the source string:
a ``workout_pattern`` derived from ONE sampled session and one derived from
120 both scored 0.55, and a single confirmation tap lifted either to 0.90.

These tests pin the four properties the fix has to hold simultaneously:
  1. more evidence  -> higher confidence (same key, same source)
  2. no sample metadata -> byte-identical to the pre-W1-5 behaviour
  3. user confirmation still raises confidence
  4. every existing threshold consumer stays on the same side of its gate
"""

from __future__ import annotations

from pathlib import Path

import pytest

import coach_bot
import user_model


async def _make_db(tmp_path: Path) -> coach_bot.Database:
    db = coach_bot.Database(str(tmp_path / "coach.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (coach_bot.utc_now(),),
    )
    return db


# --- the live-database defect, reproduced -----------------------------------


def _workout_pattern(n: int) -> dict:
    """The shape health_jobs writes, parameterised by sample count."""
    return {
        "weekly_frequency": 4.0,
        "sessions_sampled": n,
        "valid_weeks_sampled": n,
        "weekday_hour_samples": {"6": n},
    }


@pytest.mark.asyncio
async def test_one_sample_scores_lower_than_a_hundred(tmp_path: Path) -> None:
    """The core property: identical key, identical source, different n."""
    db = await _make_db(tmp_path)

    await user_model.set_fact(
        db, 1, "workout_pattern", _workout_pattern(1),
        kind=user_model.KIND_ESTIMATE, source=user_model.SOURCE_DERIVED,
    )
    thin = (await user_model.get_fact(db, 1, "workout_pattern"))["confidence"]

    await user_model.set_fact(
        db, 1, "workout_pattern", _workout_pattern(100),
        kind=user_model.KIND_ESTIMATE, source=user_model.SOURCE_DERIVED,
    )
    thick = (await user_model.get_fact(db, 1, "workout_pattern"))["confidence"]

    assert thin < thick, (
        f"n=1 ({thin}) must not score as high as n=100 ({thick}) -- "
        "confidence is keyed on the source string only"
    )
    # n=100 keeps the full source confidence; nothing is inflated above it.
    assert thick == pytest.approx(user_model.SOURCE_CONFIDENCE[user_model.SOURCE_DERIVED])


@pytest.mark.asyncio
async def test_confidence_is_monotonic_in_sample_size(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    seen: list[float] = []
    for n in (1, 2, 5, 10, 50):
        await user_model.set_fact(
            db, 1, "workout_pattern", _workout_pattern(n),
            kind=user_model.KIND_ESTIMATE, source=user_model.SOURCE_DERIVED,
        )
        fact = await user_model.get_fact(db, 1, "workout_pattern")
        seen.append(float(fact["confidence"]))
    assert seen == sorted(seen), f"confidence must not decrease with evidence: {seen}"
    assert seen[0] < seen[-1]


def test_weakest_dimension_governs() -> None:
    """A fact is only as evidenced as its thinnest supporting counter.

    The live workout_pattern row claimed four training days off
    sessions_sampled=1 / valid_weeks_sampled=0 -- healthy-looking counters
    elsewhere must not paper over that.
    """
    assert user_model.sample_size(
        {"sessions_sampled": 90, "valid_weeks_sampled": 0}
    ) == 0
    assert user_model.sample_size(
        {"sessions_sampled": 1, "weekday_hour_samples": {"6": 1}}
    ) == 1


def test_sample_metadata_detected_by_convention_not_a_key_list() -> None:
    """Any *_sampled / *_samples key counts -- including ones not yet written.

    A lookup table of key names drifts the moment a new derived fact is added;
    the convention does not.
    """
    assert user_model.sample_size({"meals_sampled": 1}) == 1
    assert user_model.sample_size({"nights_sampled": 12}) == 12
    assert user_model.sample_size({"some_future_thing_sampled": 3}) == 3
    assert user_model.sample_size({"weekday_hour_samples": {"6": 2, "7": 3}}) == 5
    # Not sample metadata -- must not be mistaken for a count.
    assert user_model.sample_size({"weekly_frequency": 4.0}) is None
    assert user_model.sample_size({"sampled_at": "2026-07-01"}) is None


# --- backwards compatibility ------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source",
    [
        user_model.SOURCE_APPLE_HEALTH,
        user_model.SOURCE_DERIVED,
        user_model.SOURCE_USER,
        user_model.SOURCE_SYSTEM,
    ],
)
async def test_scalar_fact_confidence_unchanged(tmp_path: Path, source: str) -> None:
    """No sample metadata -> exactly today's number, for every source."""
    db = await _make_db(tmp_path)
    await user_model.set_fact(db, 1, "weight_kg", 85.0, source=source)
    fact = await user_model.get_fact(db, 1, "weight_kg")
    assert fact["confidence"] == pytest.approx(user_model.SOURCE_CONFIDENCE[source])


@pytest.mark.asyncio
async def test_dict_without_sample_metadata_unchanged(tmp_path: Path) -> None:
    """Dict-valued facts that simply don't report samples are untouched."""
    db = await _make_db(tmp_path)
    await user_model.set_fact(
        db, 1, "workout_window", {"start": "18:00", "end": "20:00"},
        source=user_model.SOURCE_USER,
    )
    fact = await user_model.get_fact(db, 1, "workout_window")
    assert fact["confidence"] == pytest.approx(0.85)


@pytest.mark.asyncio
async def test_explicit_confidence_without_samples_is_respected(tmp_path: Path) -> None:
    """Callers passing confidence= for a sample-less fact still get it verbatim."""
    db = await _make_db(tmp_path)
    await user_model.set_fact(db, 1, "sleep_schedule", "23:00-07:00", confidence=0.7)
    fact = await user_model.get_fact(db, 1, "sleep_schedule")
    assert fact["confidence"] == pytest.approx(0.7)


@pytest.mark.asyncio
async def test_explicit_confidence_cannot_launder_a_thin_sample(tmp_path: Path) -> None:
    """A caller rewriting a one-sample derived value as user_report/0.85 must
    not thereby out-rank the same claim held at derived/0.55."""
    db = await _make_db(tmp_path)
    await user_model.set_fact(
        db, 1, "eating_windows",
        {"first_meal_time": "08:00", "last_meal_time": "08:00", "meals_sampled": 1},
        source=user_model.SOURCE_USER, confidence=0.85,
    )
    fact = await user_model.get_fact(db, 1, "eating_windows")
    assert fact["confidence"] < 0.85


def test_evidence_factor_never_inflates() -> None:
    """Guarantees no existing gate can be crossed *upward* by this change."""
    for n in [None, 0, 1, 2, 3, 5, 9, 10, 11, 100, 10_000]:
        assert 0.0 < user_model.evidence_factor(n) <= 1.0
    assert user_model.evidence_factor(None) == 1.0


# --- confirmation still means something -------------------------------------


@pytest.mark.asyncio
async def test_confirmation_still_raises_confidence(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    await user_model.set_fact(
        db, 1, "weight_kg", 85.0, kind=user_model.KIND_ESTIMATE, confidence=0.5,
    )
    before = (await user_model.get_fact(db, 1, "weight_kg"))["confidence"]
    await user_model.confirm_fact(db, 1, "weight_kg")
    after = await user_model.get_fact(db, 1, "weight_kg")

    assert after["confirmed"] is True
    assert after["confidence"] > before
    # Unchanged from pre-W1-5 for a fact with no sample metadata; this is the
    # value tests/test_user_model.py::test_confirm_fact and
    # tests/test_routine_confirm_session_minutes.py both pin at >= 0.9.
    assert after["confidence"] >= 0.9


@pytest.mark.asyncio
async def test_confirmation_of_thin_fact_cannot_reach_full_confidence(
    tmp_path: Path,
) -> None:
    """One tap does not retroactively create observations.

    The live defect: eating_windows built from a single meal sat at 0.90 /
    confirmed=1, ranking above better-evidenced facts.
    """
    db = await _make_db(tmp_path)
    await user_model.set_fact(
        db, 1, "eating_windows",
        {"first_meal_time": "08:00", "last_meal_time": "08:00",
         "avg_daily_calories": 140.0, "meals_sampled": 1},
        kind=user_model.KIND_ESTIMATE, source=user_model.SOURCE_DERIVED,
    )
    before = (await user_model.get_fact(db, 1, "eating_windows"))["confidence"]
    await user_model.confirm_fact(db, 1, "eating_windows")
    after = await user_model.get_fact(db, 1, "eating_windows")

    assert after["confirmed"] is True
    assert after["confidence"] > before, "confirmation must still be worth something"
    assert after["confidence"] < 0.9, (
        f"n=1 fact reached {after['confidence']} on a single tap"
    )


@pytest.mark.asyncio
async def test_confirmation_of_well_evidenced_fact_reaches_full_confidence(
    tmp_path: Path,
) -> None:
    db = await _make_db(tmp_path)
    await user_model.set_fact(
        db, 1, "eating_windows",
        {"first_meal_time": "08:00", "last_meal_time": "21:00", "meals_sampled": 120},
        kind=user_model.KIND_ESTIMATE, source=user_model.SOURCE_DERIVED,
    )
    await user_model.confirm_fact(db, 1, "eating_windows")
    fact = await user_model.get_fact(db, 1, "eating_windows")
    assert fact["confidence"] >= 0.9


@pytest.mark.asyncio
async def test_confirmation_never_lowers_confidence(tmp_path: Path) -> None:
    """MAX() semantics: confirming must not be a downgrade for any fact."""
    db = await _make_db(tmp_path)
    await user_model.set_fact(
        db, 1, "eating_windows", {"meals_sampled": 1},
        source=user_model.SOURCE_DERIVED,
    )
    before = (await user_model.get_fact(db, 1, "eating_windows"))["confidence"]
    await user_model.confirm_fact(db, 1, "eating_windows")
    after = (await user_model.get_fact(db, 1, "eating_windows"))["confidence"]
    assert after >= before


# --- existing threshold consumers -------------------------------------------


def test_confidence_label_gates_unmoved() -> None:
    """confidence_label is the only real threshold gate on fact confidence.

    Its boundaries (0.85 / 0.6) are untouched by W1-5; pinned here so a later
    change to the confidence scale cannot silently reband every profile row.
    """
    assert user_model.confidence_label(0.9) == "גבוהה"
    assert user_model.confidence_label(0.85) == "גבוהה"
    assert user_model.confidence_label(0.7) == "בינונית"
    assert user_model.confidence_label(0.6) == "בינונית"
    assert user_model.confidence_label(0.3) == "נמוכה"


@pytest.mark.asyncio
async def test_sample_less_facts_land_on_the_same_side_of_every_gate(
    tmp_path: Path,
) -> None:
    """Every source's default, for a fact with no sample metadata, must keep
    its pre-W1-5 confidence_label band."""
    db = await _make_db(tmp_path)
    expected = {
        user_model.SOURCE_APPLE_HEALTH: "גבוהה",   # 0.90
        user_model.SOURCE_USER: "גבוהה",           # 0.85
        user_model.SOURCE_SYSTEM: "בינונית",       # 0.70
        user_model.SOURCE_DERIVED: "נמוכה",        # 0.55
    }
    for source, band in expected.items():
        await user_model.set_fact(db, 1, "weight_kg", 85.0, source=source)
        fact = await user_model.get_fact(db, 1, "weight_kg")
        assert user_model.confidence_label(float(fact["confidence"])) == band


@pytest.mark.asyncio
async def test_availability_defaults_still_apply(tmp_path: Path) -> None:
    """noam_coach/services/availability.py reads confidence with `or <default>`
    fallbacks and carries it as metadata; _best() ranks by SOURCE, never by
    confidence. A non-zero confidence must keep flowing through so the `or`
    fallback is not accidentally triggered."""
    db = await _make_db(tmp_path)
    await user_model.set_fact(
        db, 1, "workout_pattern", _workout_pattern(1),
        source=user_model.SOURCE_DERIVED,
    )
    fact = await user_model.get_fact(db, 1, "workout_pattern")
    # Falsy confidence would silently swap in availability.py's 0.55 default.
    assert float(fact["confidence"]) > 0.0


@pytest.mark.asyncio
async def test_decision_gate_untouched(tmp_path: Path) -> None:
    """fact_is_usable_for_decision keys on freshness/kind/source/confirmed and
    never on confidence -- W1-5 must not change what is usable."""
    db = await _make_db(tmp_path)
    await user_model.set_fact(
        db, 1, "workout_pattern", _workout_pattern(1),
        kind=user_model.KIND_ESTIMATE, source=user_model.SOURCE_DERIVED,
    )
    fact = await user_model.get_fact(db, 1, "workout_pattern")
    assert user_model.fact_is_usable_for_decision("workout_pattern", fact) is False

    await user_model.confirm_fact(db, 1, "workout_pattern")
    fact = await user_model.get_fact(db, 1, "workout_pattern")
    # Still usable after confirmation even though confidence stays below 0.9.
    assert user_model.fact_is_usable_for_decision("workout_pattern", fact) is True
    assert float(fact["confidence"]) < 0.9
