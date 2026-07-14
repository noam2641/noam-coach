"""Batch B (FIX 48): pain recovery transition.

The reader side (training_intelligence.active_pain_regions) already skips
any row whose status is not 'active'; the writer side never existed. These
tests prove the writer (resolve_medical_constraints) now exists and that the
pre-existing reader immediately honors it.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import coach_bot
from noam_coach.bot.onboarding import (
    active_constraints,
    resolve_medical_constraints,
    save_medical_constraint,
)
from training_intelligence import active_pain_regions


async def _make_db(tmp_path: Path) -> coach_bot.Database:
    db = coach_bot.Database(str(tmp_path / "coach.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (coach_bot.utc_now(),),
    )
    coach_bot.DB.path = db.path
    return db


@pytest.mark.asyncio
async def test_resolve_medical_constraints_by_location_clears_matching_row(
    tmp_path: Path,
) -> None:
    await _make_db(tmp_path)

    await save_medical_constraint(1, kind="pain", location="ברך ימין", note="test")
    await save_medical_constraint(1, kind="pain", location="כתף שמאל", note="test")

    resolved = await resolve_medical_constraints(1, location="ברך ימין", kind="pain")
    assert resolved == 1

    remaining = await active_constraints(1)
    assert len(remaining) == 1
    assert remaining[0]["location"] == "כתף שמאל"


@pytest.mark.asyncio
async def test_resolve_medical_constraints_without_location_clears_all_of_kind(
    tmp_path: Path,
) -> None:
    await _make_db(tmp_path)

    await save_medical_constraint(1, kind="pain", location="ברך ימין", note="test")
    await save_medical_constraint(1, kind="pain", location="כתף שמאל", note="test")

    resolved = await resolve_medical_constraints(1, kind="pain")
    assert resolved == 2
    assert await active_constraints(1) == []


@pytest.mark.asyncio
async def test_resolved_constraint_is_immediately_invisible_to_active_pain_regions(
    tmp_path: Path,
) -> None:
    """The exact FIX 48 scenario: 'the pain is gone' must resolve the
    correct constraint and every reader must agree immediately -- not after
    a 14-day TTL."""
    await _make_db(tmp_path)

    await save_medical_constraint(1, kind="pain", location="ברך ימין", note="test", severity=3)
    rows_before = await active_constraints(1)
    regions_before = active_pain_regions(rows_before)
    assert regions_before != {}

    await resolve_medical_constraints(1, location="ברך ימין", kind="pain")
    rows_after = await active_constraints(1)
    regions_after = active_pain_regions(rows_after)
    assert regions_after == {}


@pytest.mark.asyncio
async def test_resolve_with_no_matching_active_constraint_returns_zero(tmp_path: Path) -> None:
    await _make_db(tmp_path)
    resolved = await resolve_medical_constraints(1, location="מרפק", kind="pain")
    assert resolved == 0
