from __future__ import annotations

import pytest
from pydantic import ValidationError

from models import MiniProfileUpdate


def test_mini_profile_rejects_invalid_time_and_duration() -> None:
    with pytest.raises(ValidationError):
        MiniProfileUpdate(work_start="25:90")
    with pytest.raises(ValidationError):
        MiniProfileUpdate(session_minutes=5)


def test_mini_profile_accepts_weekly_availability() -> None:
    update = MiniProfileUpdate(
        work_start="08:00",
        work_end="17:00",
        weekly_availability=[
            {"weekday": 0, "available": True, "start": "19:00", "minutes": 50}
        ],
    )
    assert update.weekly_availability
    assert update.weekly_availability[0].weekday == 0
