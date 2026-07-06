"""RE9-009 Tranche 2: Apple Health import stays inactive until explicit activation."""

from __future__ import annotations

from pathlib import Path

import pytest

import user_model
from db import Database
from helpers import utc_now
from noam_coach.services import health_jobs


async def _db(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "re9_health.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1, 'Test', NULL, ?)",
        (utc_now(),),
    )
    return db


@pytest.mark.asyncio
async def test_re9_imported_facts_require_explicit_activation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = await _db(tmp_path)
    monkeypatch.setattr(health_jobs, "DB", db)

    # Simulate an import that derived facts but left them unconfirmed.
    await user_model.set_fact(
        db, 1, "typical_bedtime", "23:30",
        source=user_model.SOURCE_APPLE_HEALTH, confirmed=False,
    )
    await user_model.set_fact(
        db, 1, "workout_frequency", 4,
        source=user_model.SOURCE_APPLE_HEALTH, confirmed=False,
    )

    # Baseline: facts are pending and the activation gate is offered.
    pending = await health_jobs.pending_import_facts(1)
    assert len(pending) == 2
    assert health_jobs.health_activation_keyboard(len(pending)) is not None

    # Nothing is silently active before the user acts.
    row = await db.fetch_one(
        "SELECT COUNT(*) AS c FROM user_facts WHERE user_id=1 AND confirmed=1"
    )
    assert int(row["c"]) == 0

    # Explicit activation confirms them.
    activated = await health_jobs.activate_imported_health_facts(1)
    assert activated == 2
    row = await db.fetch_one(
        "SELECT COUNT(*) AS c FROM user_facts WHERE user_id=1 AND confirmed=1"
    )
    assert int(row["c"]) == 2

    # After activation there is nothing left to activate, and no gate.
    assert await health_jobs.pending_import_facts(1) == []
    assert health_jobs.health_activation_keyboard(0) is None
    assert await health_jobs.activate_imported_health_facts(1) == 0
