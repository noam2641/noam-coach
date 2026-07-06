"""RE11 regression tests — Israeli (Sunday-first) week display.

Internal storage/comparison stays Monday-first (date.weekday(): 0=Mon..6=Sun,
see noam_coach.services.weekdays) everywhere — only user-facing day sequences
are reordered to start on Sunday, and the Mini App's Sunday-first day picker
is converted at the API boundary so stored data is never corrupted.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import coach_bot
import mini_api
import planning
import user_model
from db import Database
from helpers import utc_now
from models import MiniProfileUpdate
from noam_coach.services.weekdays import (
    monday_first_to_sunday_first,
    sunday_first_order,
    sunday_first_to_monday_first,
    weekday_he,
)


def test_sunday_first_key_orders_sunday_before_monday() -> None:
    # Monday=0 .. Sunday=6 internally; Sunday must sort first for display.
    days = [0, 1, 2, 3, 4, 5, 6]  # Mon..Sun
    ordered = sunday_first_order(days)
    assert ordered == [6, 0, 1, 2, 3, 4, 5]
    assert [weekday_he(d) for d in ordered] == [
        "ראשון", "שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת",
    ]


def test_sunday_first_conversions_are_inverses() -> None:
    for monday_first_idx in range(7):
        sunday_first_idx = monday_first_to_sunday_first(monday_first_idx)
        assert sunday_first_to_monday_first(sunday_first_idx) == monday_first_idx


def test_nutrition_plan_days_are_sunday_first() -> None:
    candidate = planning._nutrition_candidate(
        title="test",
        strategy="balanced",
        score=0.8,
        calories=2000,
        protein=150,
        facts={},
        restrictions=set(),
        canonical_ids=set(),
        rationale=[],
        tradeoffs=[],
        assumptions=[],
    )
    weekdays_in_order = [day["weekday"] for day in candidate.payload["days"]]
    assert weekdays_in_order == [6, 0, 1, 2, 3, 4, 5]  # Sun, Mon, ... Sat
    assert candidate.payload["days"][0]["weekday_name"] == "ראשון"


@pytest.mark.asyncio
async def test_mini_app_weekly_availability_write_converts_sunday_first_to_monday_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Mini App's day picker sends 0=Sunday; the backend must store
    Monday-first (0=Monday) so it's never misread by planning/availability."""
    db = Database(str(tmp_path / "mini_sunday.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (utc_now(),),
    )
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(mini_api, "DB", db)

    payload = MiniProfileUpdate(
        weekly_availability=[
            {"weekday": 0, "available": True, "start": "19:00", "minutes": 50},  # Sunday (UI)
        ],
    )
    await mini_api.mini_update_profile(payload, user_id=1)

    stored = await user_model.get_value(db, 1, "weekly_availability")
    assert stored[0]["weekday"] == 6  # Monday-first internal index for Sunday


@pytest.mark.asyncio
async def test_mini_app_profile_get_converts_monday_first_to_sunday_first_for_display(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = Database(str(tmp_path / "mini_sunday_read.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (utc_now(),),
    )
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(mini_api, "DB", db)

    # Store using the internal Monday-first schema directly (weekday=6 -> Sunday).
    await user_model.set_fact(
        db, 1, "weekly_availability",
        [{"weekday": 6, "available": True, "start": "19:00", "minutes": 50}],
        source=user_model.SOURCE_USER, confirmed=True,
    )

    response = await mini_api.mini_profile(user_id=1)
    data = json.loads(response.body)
    slots = data["snapshot"]["facts"]["weekly_availability"]["value"]
    assert slots[0]["weekday"] == 0  # Sunday in the Mini App's Sunday-first UI


@pytest.mark.asyncio
async def test_mini_app_availability_preferred_days_are_sunday_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = Database(str(tmp_path / "mini_sunday_pref.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (utc_now(),),
    )
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(mini_api, "DB", db)
    await user_model.set_fact(
        db, 1, "weekly_availability",
        [
            {"weekday": 6, "available": True, "start": "19:00", "minutes": 50},  # Sunday
            {"weekday": 0, "available": True, "start": "19:00", "minutes": 50},  # Monday
        ],
        source=user_model.SOURCE_USER, confirmed=True,
    )

    response = await mini_api.mini_profile(user_id=1)
    data = json.loads(response.body)
    assert set(data["availability"]["preferred_days"]) == {0, 1}  # Sun=0, Mon=1 in UI terms
