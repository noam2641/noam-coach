""""No allergies" must never be inferred from an unanswered question.

In the 2026-07-27 session `user_facts.allergies` was written as `"none"`
with `source=user_report, confirmed=1` -- asserting the user had declared
they have no allergies. No allergy question was ever asked. The write fired
because ONE named food had been classified as a preference.

The user had named three foods, "חציל, טורטייה ואגוזים". Only the first was
ever put through the classification keyboard. The unclassified remainder
included **nuts** -- the most common serious allergen -- while the system
simultaneously recorded that the user has no allergies, `confirmed=1`, ready
to be trusted by menu generation.

Two separate defects, both covered here:
  1. the question was closed while items were still unclassified;
  2. an inference was stored as if the user had stated it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import coach_bot
import user_model
from db import Database
from helpers import utc_now
from noam_coach.bot import onboarding as onboarding_bot


async def _make_db(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "allergy.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1, 'T', NULL, ?)",
        (utc_now(),),
    )
    return db


def _bind(monkeypatch: pytest.MonkeyPatch, db: Database) -> None:
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(onboarding_bot, "DB", db)


async def _restrictions(db: Database, value: str) -> None:
    await user_model.set_fact(
        db, 1, "diet_restrictions", value,
        kind=user_model.KIND_FACT, source=user_model.SOURCE_USER, confirmed=True,
    )


async def _levels(db: Database, mapping: dict[str, str]) -> None:
    await user_model.set_fact(
        db, 1, "diet_restriction_levels", mapping,
        kind=user_model.KIND_FACT, source=user_model.SOURCE_USER, confirmed=True,
    )


@pytest.mark.asyncio
async def test_allergy_question_stays_open_while_items_are_unclassified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact live-session state: 3 foods named, 1 classified."""
    db = await _make_db(tmp_path)
    _bind(monkeypatch, db)
    await _restrictions(db, "חציל, טורטייה ואגוזים")
    await _levels(db, {"חציל": "preference"})

    left = await onboarding_bot._unclassified_restriction_count(1)
    assert left > 0, "unclassified foods must keep the allergy question open"

    wrote = await onboarding_bot._mark_no_allergies_if_missing(1, items_left=left)

    assert wrote is False
    assert await user_model.get_fact(db, 1, "allergies") is None, (
        "'no allergies' must not be recorded while a named food -- here, nuts "
        "-- has never been classified"
    )


@pytest.mark.asyncio
async def test_no_allergies_is_stored_as_derived_not_as_a_user_statement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once everything is classified the inference may be stored -- as an inference."""
    db = await _make_db(tmp_path)
    _bind(monkeypatch, db)
    await _restrictions(db, "חציל")
    await _levels(db, {"חציל": "preference"})

    left = await onboarding_bot._unclassified_restriction_count(1)
    assert left == 0
    assert await onboarding_bot._mark_no_allergies_if_missing(1, items_left=left) is True

    fact = await user_model.get_fact(db, 1, "allergies")
    assert fact is not None
    assert fact["value"] == "none"
    assert fact["source"] == user_model.SOURCE_DERIVED, (
        "the user never said this -- it must not claim user_report"
    )
    assert not fact["confirmed"], "an inference must stay correctable"


@pytest.mark.asyncio
async def test_an_existing_allergy_is_never_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real allergy must survive a later preference classification."""
    db = await _make_db(tmp_path)
    _bind(monkeypatch, db)
    await user_model.set_fact(
        db, 1, "allergies", "בוטנים",
        kind=user_model.KIND_FACT, source=user_model.SOURCE_USER, confirmed=True,
    )
    await _restrictions(db, "חציל")
    await _levels(db, {"חציל": "preference"})

    wrote = await onboarding_bot._mark_no_allergies_if_missing(1, items_left=0)

    assert wrote is False
    fact = await user_model.get_fact(db, 1, "allergies")
    assert fact["value"] == "בוטנים"


@pytest.mark.asyncio
async def test_unclassified_count_is_zero_when_nothing_was_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path)
    _bind(monkeypatch, db)

    assert await onboarding_bot._unclassified_restriction_count(1) == 0

    await _restrictions(db, "none")
    assert await onboarding_bot._unclassified_restriction_count(1) == 0


@pytest.mark.asyncio
async def test_counting_is_conservative_for_unsplit_compound_items(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A compound entry counts as outstanding until it is classified.

    The splitter separates on commas only, so "טורטייה ואגוזים" (two foods
    joined by a prefixed vav) stays a single entry. Over-counting keeps the
    allergy question open; under-counting would close it on a food nobody
    was ever asked about.
    """
    db = await _make_db(tmp_path)
    _bind(monkeypatch, db)
    await _restrictions(db, "חציל, טורטייה ואגוזים")
    await _levels(db, {"חציל": "preference"})

    assert await onboarding_bot._unclassified_restriction_count(1) == 1

    await _levels(db, {"חציל": "preference", "טורטייה ואגוזים": "preference"})
    assert await onboarding_bot._unclassified_restriction_count(1) == 0


@pytest.mark.asyncio
async def test_a_malformed_levels_fact_does_not_close_the_question(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-dict levels value must not be read as 'everything classified'."""
    db = await _make_db(tmp_path)
    _bind(monkeypatch, db)
    await _restrictions(db, "חציל, אגוזים")
    await user_model.set_fact(
        db, 1, "diet_restriction_levels", "unexpected-string",
        kind=user_model.KIND_FACT, source=user_model.SOURCE_USER, confirmed=True,
    )

    assert await onboarding_bot._unclassified_restriction_count(1) == 2
