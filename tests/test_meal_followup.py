from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

from config import TZ
from noam_coach.services.meal_followup import planned_meal_followup


def _ctx(*, planned_at: datetime, reported_name: str | None = None):
    reported = []
    if reported_name:
        reported.append(SimpleNamespace(name=reported_name))
    return SimpleNamespace(
        planned_meals=[
            {
                "fingerprint": "abc123",
                "name": "Chicken bowl",
                "calories": 650,
                "protein": 55,
                "planned_at": planned_at.isoformat(),
                "source": "next_meal_plan",
            }
        ],
        reported_meals=reported,
    )


def test_planned_meal_followup_waits_for_grace_period() -> None:
    now = datetime(2026, 7, 2, 15, 0, tzinfo=TZ)
    ctx = _ctx(planned_at=now - timedelta(minutes=45))

    assert planned_meal_followup(ctx, now=now) is None


def test_planned_meal_followup_skips_if_meal_was_logged() -> None:
    now = datetime(2026, 7, 2, 15, 0, tzinfo=TZ)
    ctx = _ctx(planned_at=now - timedelta(hours=3), reported_name="Chicken bowl")

    assert planned_meal_followup(ctx, now=now) is None


def test_planned_meal_followup_prompts_after_missed_planned_meal() -> None:
    now = datetime(2026, 7, 2, 15, 0, tzinfo=TZ)
    ctx = _ctx(planned_at=now - timedelta(hours=3))

    followup = planned_meal_followup(ctx, now=now)

    assert followup is not None
    assert followup.key == "planned_meal_followup_6367c48dd1"
    assert "Chicken bowl" in followup.text
    assert "אכלת" in followup.text


def test_planned_meal_followup_skips_when_logged_under_a_shorter_title() -> None:
    """FIX 49 (Batch E): a meal planned as 'Chicken bowl' but logged simply
    as 'Chicken' (a very common real-world shortening) must still be
    recognized as consumed -- exact-string equality is not sufficient."""
    now = datetime(2026, 7, 2, 15, 0, tzinfo=TZ)
    ctx = _ctx(planned_at=now - timedelta(hours=3), reported_name="Chicken")

    assert planned_meal_followup(ctx, now=now) is None


def test_planned_meal_followup_skips_when_logged_under_a_longer_title() -> None:
    now = datetime(2026, 7, 2, 15, 0, tzinfo=TZ)
    ctx = _ctx(planned_at=now - timedelta(hours=3), reported_name="Chicken bowl with rice and veggies")

    assert planned_meal_followup(ctx, now=now) is None


def test_planned_meal_followup_still_prompts_for_genuinely_unrelated_meal() -> None:
    """Sanity check: the widened match must not become so loose that an
    unrelated logged meal suppresses a real follow-up."""
    now = datetime(2026, 7, 2, 15, 0, tzinfo=TZ)
    ctx = _ctx(planned_at=now - timedelta(hours=3), reported_name="Salad")

    followup = planned_meal_followup(ctx, now=now)
    assert followup is not None
