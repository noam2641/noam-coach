from __future__ import annotations

from pathlib import Path

import pytest

import user_model
from db import Database
from helpers import utc_now


@pytest.mark.asyncio
async def test_unconfirmed_estimate_does_not_satisfy_required_readiness(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "facts.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (utc_now(),),
    )
    await user_model.set_fact(
        db,
        1,
        "active_pain",
        "none",
        kind=user_model.KIND_ESTIMATE,
        source=user_model.SOURCE_DERIVED,
        confirmed=False,
    )
    await user_model.set_fact(
        db,
        1,
        "medical_avoidance",
        "none",
        source=user_model.SOURCE_USER,
        confirmed=True,
    )
    readiness = await user_model.compute_readiness(db, 1, "safety")
    assert readiness["ready"] is False
    assert "active_pain" in readiness["missing"]

    await user_model.confirm_fact(db, 1, "active_pain")
    readiness = await user_model.compute_readiness(db, 1, "safety")
    assert readiness["ready"] is True
