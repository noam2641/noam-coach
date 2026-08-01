"""How long a pain report is allowed to constrain training (A7).

Pain lived in two stores with different lifetimes. `medical_constraints` rows
age out after 14 days via `active_pain_regions`; the `training_limitations`
fact that planning reads declared `expires_after_days=30`. That left a **16-day
window** where the workout runtime had stopped treating a region as painful
while planning still asserted an active limitation.

Worse, the mirror that wrote the fact *appended* to a free-text location
string. Report an elbow once and a knee once and the fact said "elbow, knee"
permanently — nothing could ever remove either, because nothing recomputed.

A7 derives instead of accumulating: the fact is rebuilt from the constraint
rows that are currently active, which makes expiry free and repetition
idempotent. Four states stay distinct, and the one that matters most is the
**confirmed limitation** — text the user typed in onboarding. A herniated disc
does not expire because nobody reported elbow pain this fortnight, so a
recompute that erased it would be a safety regression dressed as a cleanup.
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

import pytest

import training_intelligence
import user_model
from db import Database
from helpers import utc_now
from noam_coach.services import pain_persistence


async def _db(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "pain.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (utc_now(),),
    )
    return db


async def _pain(db: Database, location: str, *, age_days: int = 0, severity: int = 3) -> None:
    created = (
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=age_days)
    ).isoformat()
    await db.execute(
        """
        INSERT INTO medical_constraints(
            user_id, kind, location, severity, status, note, affects, created_at
        ) VALUES(1, 'pain', ?, ?, 'active', '', '["exercise_selection"]', ?)
        """,
        (location, severity, created),
    )


# ---------------------------------------------------------------------------
# The lifetimes must agree
# ---------------------------------------------------------------------------
def test_the_two_pain_lifetimes_are_aligned() -> None:
    """The 16-day divergence is the defect; this pins that it stays closed.

    `expires_after_days` is written as a literal because `user_model` imports
    nothing from the project — being a leaf is what lets everything else import
    it without a cycle. So the equality needs a test rather than a shared
    constant.
    """
    spec = user_model.FACT_REGISTRY["training_limitations"]
    assert spec.expires_after_days == training_intelligence.PAIN_CONSTRAINT_TTL_DAYS, (
        "the planning fact and the constraint rows must expire together; a "
        "longer fact TTL leaves planning asserting a limitation the runtime "
        "has already stopped honouring"
    )


def test_the_fact_never_outlives_the_constraint() -> None:
    """Direction matters, not just equality.

    If these ever diverge again, the fact expiring SOONER is the safe side: an
    expired safety fact re-asks the question, while a stale one silently
    constrains exercise selection.
    """
    spec = user_model.FACT_REGISTRY["training_limitations"]
    assert spec.expires_after_days <= training_intelligence.PAIN_CONSTRAINT_TTL_DAYS


# ---------------------------------------------------------------------------
# Recompute, not append
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_second_report_of_the_same_region_is_idempotent(tmp_path) -> None:
    """The append bug: reporting twice must not say it twice."""
    db = await _db(tmp_path)
    await _pain(db, "elbow")

    first = await pain_persistence.sync_training_limitations(db, 1)
    assert first == pain_persistence.SYNC_UPDATED

    await _pain(db, "elbow")
    second = await pain_persistence.sync_training_limitations(db, 1)
    assert second == pain_persistence.SYNC_UNCHANGED

    value = await user_model.get_value(db, 1, "training_limitations")
    assert value["location"].count("מרפק") == 1


@pytest.mark.asyncio
async def test_an_expired_report_stops_constraining_training(tmp_path) -> None:
    """The core of A7. Accumulation had no path back to "no limitation"."""
    db = await _db(tmp_path)
    await _pain(db, "elbow", age_days=1)
    assert await pain_persistence.sync_training_limitations(db, 1) == (
        pain_persistence.SYNC_UPDATED
    )

    # Age the row past the TTL and recompute.
    await db.execute(
        "UPDATE medical_constraints SET created_at=? WHERE user_id=1",
        ((dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=40)).isoformat(),),
    )
    outcome = await pain_persistence.sync_training_limitations(db, 1)

    assert outcome == pain_persistence.SYNC_CLEARED
    fact = await user_model.get_fact(db, 1, "training_limitations")
    assert fact is None or fact.get("valid") in (0, False), (
        "an expired pain report must stop asserting an active limitation"
    )


@pytest.mark.asyncio
async def test_one_region_resolving_does_not_erase_another(tmp_path) -> None:
    """Two regions, one expires. The other must survive.

    Accumulation could not express this at all: with a single free-text string
    and no recompute, removing one region was impossible.
    """
    db = await _db(tmp_path)
    await _pain(db, "elbow", age_days=40)   # expired
    await _pain(db, "knee", age_days=1)     # current

    await pain_persistence.sync_training_limitations(db, 1)

    value = await user_model.get_value(db, 1, "training_limitations")
    assert "ברך" in value["location"]
    assert "מרפק" not in value["location"]


# ---------------------------------------------------------------------------
# A stated limitation is not a pain report
# ---------------------------------------------------------------------------
def test_a_user_stated_limitation_survives_a_recompute() -> None:
    """The safety regression this design must not cause.

    A herniated disc typed in onboarding does not expire because no pain was
    reported recently. A recompute derived purely from constraint rows would
    erase it.
    """
    existing = {
        "location": "פריצת דיסק",
        "status": "active",
        "origin": pain_persistence.SOURCE_USER_STATED,
        "user_stated": "פריצת דיסק",
    }
    stated = pain_persistence._user_stated_part(existing)
    assert stated == "פריצת דיסק"

    value = pain_persistence.compose_limitation_value(stated, [])
    assert value is not None
    assert "פריצת דיסק" in value["location"]


def test_a_legacy_fact_with_no_provenance_is_treated_as_user_stated() -> None:
    """The conservative reading, chosen deliberately.

    Legacy rows record no origin, so stated text and derived text are
    indistinguishable. Losing a real medical limitation is far worse than
    carrying a stale one, so an unmarked fact is preserved.
    """
    legacy = {"location": "כתף", "status": "active"}
    assert pain_persistence._user_stated_part(legacy) == "כתף"

    legacy_string = "כאב גב תחתון"
    assert pain_persistence._user_stated_part(legacy_string) == "כאב גב תחתון"


def test_derived_text_is_not_mistaken_for_a_stated_limitation() -> None:
    """Once provenance exists, derived text must NOT be preserved.

    Otherwise the first recompute would freeze that run's pain regions into the
    "stated" slot and reintroduce accumulation permanently.
    """
    derived = {
        "location": "מרפק",
        "status": "active",
        "origin": pain_persistence.SOURCE_DERIVED_FROM_PAIN,
        "user_stated": "",
    }
    assert pain_persistence._user_stated_part(derived) == ""


def test_a_stated_limitation_and_active_pain_are_both_present() -> None:
    value = pain_persistence.compose_limitation_value("פריצת דיסק", ["מרפק"])
    assert value is not None
    assert "פריצת דיסק" in value["location"]
    assert "מרפק" in value["location"]
    assert value["origin"] == "mixed"


def test_nothing_to_assert_returns_none_rather_than_an_empty_limitation() -> None:
    """An empty string would still read as "a limitation exists"."""
    assert pain_persistence.compose_limitation_value("", []) is None


def test_composition_does_not_duplicate_a_repeated_region() -> None:
    value = pain_persistence.compose_limitation_value("מרפק", ["מרפק"])
    assert value is not None
    assert value["location"].count("מרפק") == 1


# ---------------------------------------------------------------------------
# Observability and privacy
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_sync_failure_is_reported_not_swallowed(tmp_path, caplog) -> None:
    """A projection failure must be visible, and must not raise.

    The constraint row is already durable when this runs, so raising would
    break a pain report that actually succeeded -- the A1 defect.
    """

    class _Broken:
        async def fetch_all(self, *_a, **_k):
            raise RuntimeError("database is locked")

    with caplog.at_level(logging.ERROR):
        outcome = await pain_persistence.sync_training_limitations(_Broken(), 1)

    assert outcome == pain_persistence.SYNC_FAILED
    assert any("pain_limitation_sync_failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_logs_carry_counts_not_medical_text(tmp_path, caplog) -> None:
    """A limitation string can name a body part and a medical condition.

    It is exactly the kind of value that must not reach a log aggregator, and
    the exception path is where that mistake is easiest to make.
    """
    db = await _db(tmp_path)
    await _pain(db, "elbow")

    with caplog.at_level(logging.INFO):
        await pain_persistence.sync_training_limitations(db, 1)

    emitted = "\n".join(r.message for r in caplog.records)
    assert "user_id=1" in emitted
    assert "מרפק" not in emitted, "the limitation text must not be logged"
    assert "elbow" not in emitted


@pytest.mark.asyncio
async def test_no_pain_and_no_stated_limitation_is_unchanged_not_failed(
    tmp_path,
) -> None:
    """A user who never reported anything is a success, not an error."""
    db = await _db(tmp_path)
    assert await pain_persistence.sync_training_limitations(db, 1) == (
        pain_persistence.SYNC_UNCHANGED
    )


# ---------------------------------------------------------------------------
# Scope: A7 does not touch the safety record
# ---------------------------------------------------------------------------
def test_the_module_never_writes_medical_constraints() -> None:
    """`medical_constraints` is authoritative; A7 only derives from it.

    Writing it here would make the projection able to alter the record it is
    derived from -- the circularity that makes such a bug unfixable later.
    """
    import inspect

    source = inspect.getsource(pain_persistence)
    for forbidden in ("INSERT INTO medical_constraints", "UPDATE medical_constraints",
                      "DELETE FROM medical_constraints"):
        assert forbidden not in source, (
            f"{forbidden!r} in the pain projection; the constraint rows are the "
            "safety record and must not be rewritten by what reads them"
        )
