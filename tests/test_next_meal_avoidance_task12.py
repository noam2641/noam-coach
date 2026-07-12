"""TASK-12 — explicit avoidances are application-level hard constraints in the
real-time "what should I eat now?" flow.

"טורטייה" must block "טורטיית חלבון" (a proposed food), and the avoidance is
enforced at the application level, not only mentioned in a prompt.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from config import TZ
from noam_coach.services.food_preferences import parse_restrictions
from noam_coach.services import next_meal as nm
from noam_coach.services.next_meal import WorkoutPhase
from noam_coach.services.user_state import _phase_from_times


def _avoid(label: str, rtype: str = "preference"):
    return parse_restrictions(label, restriction_type=rtype, severity="low", source="t", confirmed=True)


def test_tortilla_avoidance_blocks_protein_tortilla() -> None:
    r = _avoid("טורטייה")
    assert nm._matches_free_text_preference(["טורטיית חלבון", "ביצה"], r) is True


def test_tortilla_avoidance_blocks_plain_tortilla() -> None:
    r = _avoid("טורטייה")
    assert nm._matches_free_text_preference(["טורטיה"], r) is True


def test_avoidance_type_is_enforced_like_preference() -> None:
    r = _avoid("טורטייה", rtype="avoidance")
    assert nm._matches_free_text_preference(["טורטיית חלבון"], r) is True


def test_unrelated_food_is_not_over_excluded() -> None:
    r = _avoid("טורטייה")
    assert nm._matches_free_text_preference(["חזה עוף", "אורז", "סלט"], r) is False


def test_hebrew_stem_tolerates_construct_ending() -> None:
    assert nm._hebrew_stem("טורטייה") == nm._hebrew_stem("טורטיית") or (
        nm._food_word_matches(nm._free_text_preference_key("טורטייה"),
                              nm._free_text_preference_key("טורטיית חלבון"))
    )


def test_filter_options_drops_avoided_food() -> None:
    from noam_coach.services.next_meal import MealOption, _filter_options

    r = _avoid("טורטייה")
    good = MealOption(title="עוף ואורז", ingredients=["חזה עוף", "אורז"], calories=600, protein=50, rationale="")
    bad = MealOption(title="טורטיית חלבון", ingredients=["טורטיית חלבון", "ביצה"], calories=400, protein=30, rationale="")
    kept = _filter_options([good, bad], r)
    titles = [o.title for o in kept]
    assert "טורטיית חלבון" not in titles
    assert "עוף ואורז" in titles


def test_far_future_workout_is_not_pre_workout_meal_context() -> None:
    now = datetime(2026, 7, 12, 0, 49, tzinfo=TZ)
    start = datetime(2026, 7, 12, 19, 9, tzinfo=TZ)  # ~18h away
    end = start + timedelta(hours=1)
    phase, minutes_until, _ = _phase_from_times(now, start, end)
    assert phase == WorkoutPhase.REST_DAY  # not treated as pre-workout
    assert minutes_until and minutes_until > 300


def test_near_future_workout_is_pre_workout() -> None:
    now = datetime(2026, 7, 12, 17, 30, tzinfo=TZ)
    start = datetime(2026, 7, 12, 19, 9, tzinfo=TZ)  # ~99 min away
    end = start + timedelta(hours=1)
    phase, _minutes, _ = _phase_from_times(now, start, end)
    assert phase in {WorkoutPhase.PRE_WORKOUT_NEAR, WorkoutPhase.PRE_WORKOUT_EARLY}
