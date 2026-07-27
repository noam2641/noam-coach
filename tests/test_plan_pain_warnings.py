"""The weekly plan must warn about exercises that load an injured joint.

In the 2026-07-27 session the user reported "טניס אלבו וברך ימין" (tennis
elbow and right knee) during onboarding. It was stored correctly, both
regions were detected correctly, and the live set card warned correctly.

The weekly plan -- the screen the user actually reads *before* deciding what
to train -- listed `סקוואט עם מוט` (barbell squat) and `Leg Press` three
separate times with **no knee warning at all**. The safety information
existed and simply never reached the screen where it matters; the set card
only appears once the exercise is already underway.

These tests pin the warning to the same CATALOG joint map the set card uses,
so the two screens cannot disagree about which exercises are affected.
"""

from __future__ import annotations

import pytest

import coach_bot
import training_intelligence


def _pain_row(location: str, severity: int | None = None) -> dict:
    return {
        "kind": "pain",
        "status": "active",
        "location": location,
        "note": "",
        "severity": severity,
        "created_at": "2026-07-27T06:58:17+00:00",
    }


def _leg_plan() -> dict:
    return {
        "frequency": 4,
        "structure": "A/B/C + Full Body",
        "sessions": [
            {"weekday": 0, "time": "19:00", "code": "C", "name": "אימון C — רגליים"}
        ],
    }


def test_both_regions_are_detected_from_one_free_text_constraint() -> None:
    """The stored value names two joints in one Hebrew string."""
    regions = training_intelligence.active_pain_regions(
        [_pain_row("טניס אלבו וברך ימין")]
    )
    assert set(regions) == {"elbow", "knee"}


def test_squat_declares_knee_load() -> None:
    """The joint map is the premise of the whole fix -- pin it."""
    profile = training_intelligence.CATALOG.get("squat")
    assert profile is not None
    assert "knee" in profile.joint_load

    leg_press = training_intelligence.CATALOG.get("leg_press")
    assert leg_press is not None
    assert "knee" in leg_press.joint_load


def test_knee_constraint_marks_the_squat_in_the_weekly_plan() -> None:
    """The regression: this screen showed squats with no knee warning."""
    regions = training_intelligence.active_pain_regions([_pain_row("כאב בברך ימין")])

    rendered = coach_bot.format_weekly_plan(_leg_plan(), regions)

    squat_line = next(
        line for line in rendered.splitlines() if "סקוואט" in line
    )
    assert "⚠️" in squat_line, "a knee-loading exercise must be marked"
    assert "ברך" in squat_line


def test_plan_carries_a_header_naming_the_active_constraint() -> None:
    regions = training_intelligence.active_pain_regions(
        [_pain_row("טניס אלבו וברך ימין")]
    )

    rendered = coach_bot.format_weekly_plan(_leg_plan(), regions)

    assert "מגבלה פעילה" in rendered
    assert "ברך" in rendered
    assert "מרפק" in rendered


def test_unaffected_exercises_are_not_marked() -> None:
    """A warning on everything is a warning on nothing."""
    regions = training_intelligence.active_pain_regions([_pain_row("כאב בברך")])

    rendered = coach_bot.format_weekly_plan(_leg_plan(), regions)

    lateral = next(
        line for line in rendered.splitlines() if "הרחקת כתפיים" in line
    )
    assert "⚠️" not in lateral, (
        "a shoulder isolation exercise does not load the knee and must stay clean"
    )


def test_no_constraint_renders_exactly_as_before() -> None:
    """The warning must be purely additive for users with no pain reported."""
    plan = _leg_plan()

    without = coach_bot.format_weekly_plan(plan)
    with_empty = coach_bot.format_weekly_plan(plan, {})

    assert without == with_empty
    assert "מגבלה פעילה" not in without
    assert "⚠️" not in without


def test_the_plan_and_the_set_card_agree_on_which_joints_are_loaded() -> None:
    """Both screens must read the same map, or they will contradict each other.

    The set card resolves its warning through CATALOG[...].joint_load
    (workout.py::_exercise_pain_warning_line). The plan now does the same, so
    an exercise cannot be flagged on one screen and clean on the other.
    """
    regions = training_intelligence.active_pain_regions([_pain_row("כאב בברך")])
    rendered = coach_bot.format_weekly_plan(_leg_plan(), regions)

    from exercise_plans import PLANS

    for exercise in PLANS["C"]["exercises"][:5]:
        profile = training_intelligence.CATALOG.get(str(exercise.get("id")))
        loads_knee = bool(profile and "knee" in (profile.joint_load or ()))
        line = next(
            (line for line in rendered.splitlines() if exercise["name"] in line),
            None,
        )
        if line is None:
            continue
        assert ("⚠️" in line) is loads_knee, (
            f"{exercise['name']}: plan marking disagrees with CATALOG.joint_load"
        )
