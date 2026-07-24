"""R-2b — coaching-day resolver must honor BOTH sleep_schedule fact shapes.

Verified live bug (before this fix): the resolver reads ``sleep_schedule.bedtime``
(``coaching_day._parse_bedtime``), but the onboarding text-edit writer stores
``typical_bedtime``/``typical_wake_time`` (``onboarding._parse_sleep_window_text``),
while the Health-import writer stores ``bedtime``/``wake_time``. A user who set
their sleep window via onboarding therefore got a silent calendar-midnight
fallback — the coaching-day feature was inert for them.

This module has two halves:

1. Characterization (CURRENT behavior, xfail until the fix lands): asserts that
   the onboarding shape resolves to the SAME coaching day as the health-import
   shape. These are marked ``xfail(strict=True)`` so they flip to XPASS→fail-if-
   left the moment the fix makes them pass, and are then promoted to plain
   asserts in the same commit.
2. Regression (post-fix, always-on): both shapes, explicit precedence when both
   fields exist, after-midnight bedtime, and a non-Israel timezone.
"""

from __future__ import annotations

from datetime import datetime
from datetime import time as dtime

import pytest

from config import TZ
from noam_coach.services import coaching_day as cd

# 23:00 bedtime + 4h grace → rollover at 03:00 local. At 00:30 local the coaching
# day is still the PREVIOUS calendar date.
NIGHT = datetime(2026, 6, 29, 0, 30, tzinfo=TZ)
COACHING_DAY = "2026-06-28"
CALENDAR_DAY_AT_NIGHT = "2026-06-29"

HEALTH_SHAPE = {"bedtime": "23:00", "wake_time": "07:00"}
ONBOARDING_SHAPE = {"typical_bedtime": "23:00", "typical_wake_time": "07:00"}


# --------------------------------------------------------------------------
# Unit-level parser probes (no DB) — the smallest possible characterization.
# --------------------------------------------------------------------------

def test_parse_bedtime_reads_health_import_shape() -> None:
    assert cd._parse_bedtime(HEALTH_SHAPE) == dtime(23, 0)


def test_parse_bedtime_reads_onboarding_shape() -> None:
    # The bug: this returned None before the fix, so the resolver fell back to
    # calendar midnight for onboarding-configured users.
    assert cd._parse_bedtime(ONBOARDING_SHAPE) == dtime(23, 0)


def test_parse_bedtime_precedence_when_both_present() -> None:
    # Explicit precedence: the canonical ``bedtime`` key wins over the legacy
    # ``typical_bedtime`` when both exist (a fact mid-migration).
    both = {"bedtime": "23:30", "typical_bedtime": "21:00"}
    assert cd._parse_bedtime(both) == dtime(23, 30)


def test_parse_bedtime_none_when_neither_present() -> None:
    assert cd._parse_bedtime({"wake_time": "07:00"}) is None
    assert cd._parse_bedtime({}) is None
    assert cd._parse_bedtime("not-a-dict") is None


# --------------------------------------------------------------------------
# Resolver-level parity — the two shapes must yield identical coaching-day
# semantics. Uses a tiny fake fact store so no real DB is needed.
# --------------------------------------------------------------------------

@pytest.fixture
def _patch_fact(monkeypatch):
    """Patch user_model.get_fact to return a chosen confirmed sleep_schedule."""

    def _install(value: dict | None):
        async def _fake_get_fact(db, user_id, key):
            if key == "sleep_schedule" and value is not None:
                return {"value": value, "confirmed": True}
            return None

        import user_model

        monkeypatch.setattr(user_model, "get_fact", _fake_get_fact)

    return _install


async def _resolve(shape: dict | None) -> cd.CoachingDay:
    return await cd.resolve_coaching_day(object(), 1, local_now=NIGHT)


async def test_health_shape_anchors_coaching_day(_patch_fact) -> None:
    _patch_fact(HEALTH_SHAPE)
    day = await _resolve(HEALTH_SHAPE)
    assert day.day_key == COACHING_DAY
    assert day.rollover_reason == "sleep_schedule_bedtime"
    assert day.anchor_hour == 3


async def test_onboarding_shape_gives_same_semantics_as_health(_patch_fact) -> None:
    """The core R-2b guarantee: onboarding-written == health-imported."""
    _patch_fact(HEALTH_SHAPE)
    health = await cd.resolve_coaching_day(object(), 1, local_now=NIGHT)
    _patch_fact(ONBOARDING_SHAPE)
    onboarding = await cd.resolve_coaching_day(object(), 1, local_now=NIGHT)

    assert onboarding.day_key == health.day_key == COACHING_DAY
    assert onboarding.rollover_reason == health.rollover_reason == "sleep_schedule_bedtime"
    assert onboarding.anchor_hour == health.anchor_hour == 3
    # Before the fix, the onboarding shape produced calendar midnight:
    assert onboarding.day_key != CALENDAR_DAY_AT_NIGHT


async def test_after_midnight_bedtime_both_shapes(_patch_fact) -> None:
    """After-midnight bedtime (01:00) + 4h grace → rollover 05:00; at 02:00 the
    coaching day is still the previous date. Both shapes must agree."""
    at_0200 = datetime(2026, 6, 29, 2, 0, tzinfo=TZ)
    for shape in ({"bedtime": "01:00"}, {"typical_bedtime": "01:00"}):
        _patch_fact(shape)
        day = await cd.resolve_coaching_day(object(), 1, local_now=at_0200)
        assert day.anchor_hour == 5, shape
        assert day.rollover_reason == "sleep_schedule_bedtime", shape
        assert day.day_key == "2026-06-28", shape  # still previous coaching day


async def test_non_israel_timezone_both_shapes(_patch_fact) -> None:
    """The resolver normalizes local_now to TZ; a UTC-provided instant that maps
    to 00:30 Asia/Jerusalem must resolve to the previous coaching day for both
    shapes (timezone regression)."""
    from datetime import timezone as _tz

    # 2026-06-28 21:30 UTC == 2026-06-29 00:30 Asia/Jerusalem (UTC+3 DST).
    utc_instant = datetime(2026, 6, 28, 21, 30, tzinfo=_tz.utc)
    for shape in (HEALTH_SHAPE, ONBOARDING_SHAPE):
        _patch_fact(shape)
        day = await cd.resolve_coaching_day(object(), 1, local_now=utc_instant)
        assert day.day_key == COACHING_DAY, shape
        assert day.rollover_reason == "sleep_schedule_bedtime", shape
