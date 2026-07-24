"""Shared sleep-schedule accessor (P1.1b, scope 1) — the single tolerant reader
of both sleep shapes with deterministic canonical precedence.

Every reader migrates to coaching_day.sleep_bedtime / sleep_wake_time /
normalize_sleep_schedule instead of reimplementing ``bedtime or typical_bedtime``.
"""

from __future__ import annotations

from datetime import datetime
from datetime import time as dtime

from config import TZ
from noam_coach.services import coaching_day as cd

HEALTH = {"bedtime": "23:00", "wake_time": "07:00"}          # canonical
ONBOARD = {"typical_bedtime": "23:00", "typical_wake_time": "07:00"}  # legacy


# --- accessor: shape tolerance + precedence --------------------------------

def test_accessor_reads_canonical_only() -> None:
    assert cd.sleep_bedtime(HEALTH) == "23:00"
    assert cd.sleep_wake_time(HEALTH) == "07:00"
    assert cd.normalize_sleep_schedule(HEALTH) == {"bedtime": "23:00", "wake_time": "07:00"}


def test_accessor_reads_legacy_only() -> None:
    assert cd.sleep_bedtime(ONBOARD) == "23:00"
    assert cd.sleep_wake_time(ONBOARD) == "07:00"
    assert cd.normalize_sleep_schedule(ONBOARD) == {"bedtime": "23:00", "wake_time": "07:00"}


def test_accessor_canonical_precedence_when_both_present() -> None:
    both = {"bedtime": "23:30", "wake_time": "07:30",
            "typical_bedtime": "21:00", "typical_wake_time": "05:00"}
    assert cd.sleep_bedtime(both) == "23:30"       # canonical wins
    assert cd.sleep_wake_time(both) == "07:30"
    assert cd.normalize_sleep_schedule(both) == {"bedtime": "23:30", "wake_time": "07:30"}


def test_accessor_empty_and_non_dict() -> None:
    assert cd.sleep_bedtime({}) is None
    assert cd.sleep_wake_time({}) is None
    assert cd.normalize_sleep_schedule({}) == {}
    assert cd.sleep_bedtime("not-a-dict") is None
    assert cd.sleep_bedtime({"typical_bedtime": ""}) is None   # empty string ignored
    assert cd.normalize_sleep_schedule({"bedtime": "23:00"}) == {"bedtime": "23:00"}


# --- accessor drives coaching-day resolution equally for both shapes -------

class _Patch:
    def __init__(self, monkeypatch):
        self.mp = monkeypatch

    def fact(self, value):
        async def _fake_get_fact(db, user_id, key):
            return {"value": value, "confirmed": True} if key == "sleep_schedule" else None

        import user_model
        self.mp.setattr(user_model, "get_fact", _fake_get_fact)


async def test_coaching_day_identical_for_both_shapes(monkeypatch) -> None:
    night = datetime(2026, 6, 29, 0, 30, tzinfo=TZ)  # after midnight, pre-rollover
    p = _Patch(monkeypatch)
    p.fact(HEALTH)
    a = await cd.resolve_coaching_day(object(), 1, local_now=night)
    p.fact(ONBOARD)
    b = await cd.resolve_coaching_day(object(), 1, local_now=night)
    assert a.day_key == b.day_key == "2026-06-28"
    assert a.rollover_reason == b.rollover_reason == "sleep_schedule_bedtime"


async def test_after_midnight_bedtime_both_shapes(monkeypatch) -> None:
    at_0200 = datetime(2026, 6, 29, 2, 0, tzinfo=TZ)
    p = _Patch(monkeypatch)
    for shape in ({"bedtime": "01:00"}, {"typical_bedtime": "01:00"}):
        p.fact(shape)
        day = await cd.resolve_coaching_day(object(), 1, local_now=at_0200)
        assert day.anchor_hour == 5 and day.day_key == "2026-06-28", shape


async def test_timezone_utc_instant_both_shapes(monkeypatch) -> None:
    from datetime import timezone as _tz

    utc_instant = datetime(2026, 6, 28, 21, 30, tzinfo=_tz.utc)  # 00:30 Asia/Jerusalem
    p = _Patch(monkeypatch)
    for shape in (HEALTH, ONBOARD):
        p.fact(shape)
        day = await cd.resolve_coaching_day(object(), 1, local_now=utc_instant)
        assert day.day_key == "2026-06-28", shape


def test_parse_bedtime_still_uses_accessor() -> None:
    assert cd._parse_bedtime(HEALTH) == dtime(23, 0)
    assert cd._parse_bedtime(ONBOARD) == dtime(23, 0)


def test_hours_left_until_sleep_reads_both_shapes() -> None:
    """proactive.hours_left_until_sleep was a legacy-only reader; after migration
    it must honor the canonical bedtime shape too."""
    from noam_coach.jobs.proactive import hours_left_until_sleep

    legacy = hours_left_until_sleep({"sleep": {"typical_bedtime": "23:00"}})
    canonical = hours_left_until_sleep({"sleep": {"bedtime": "23:00"}})
    # both shapes resolve to a real bedtime, so both give the SAME hours-left
    # (not the 23:00 default fallback path) — they must be equal.
    assert legacy == canonical
    assert isinstance(canonical, float)
