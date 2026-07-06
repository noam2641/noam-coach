from __future__ import annotations

import targets


def test_explain_targets_warns_for_aggressive_weight_loss_rate() -> None:
    result = targets.compute_targets(
        100, avg_steps=8000, sex="male", height_cm=180, age=35,
        goal_type="fat_loss_muscle_retention",
        goal_weight_kg=88,
        goal_timeframe_weeks=8,
    )
    text = targets.explain_targets(result)
    assert "קצב ירידה אגרסיבי" in text


def test_explain_targets_does_not_warn_for_normal_weight_loss_rate() -> None:
    result = targets.compute_targets(
        100, avg_steps=8000, sex="male", height_cm=180, age=35,
        goal_type="fat_loss_muscle_retention",
        goal_weight_kg=96,
        goal_timeframe_weeks=8,
    )
    text = targets.explain_targets(result)
    assert "קצב ירידה אגרסיבי" not in text


def test_targets_are_bounded_and_explainable() -> None:
    result = targets.compute_targets(
        90,
        avg_steps=8000,
        goal_type="fat_loss_muscle_retention",
        sex="male",
        height_cm=174,
        age=32,
        workouts_per_week=3,
    )
    assert result.calories >= targets.MIN_CALORIES
    assert result.protein >= targets.MIN_PROTEIN
    assert result.maintenance > result.calories
    assert result.basis["weight_kg"] == 90


# ---------------------------------------------------------------------------
# D1 — steps target is a gentle nudge, not a hard 8,000-step floor
# ---------------------------------------------------------------------------


def test_low_step_average_gets_gentle_nudge_not_a_67_percent_jump() -> None:
    """Reproduces the exact screenshot case: 4,789 avg steps must NOT jump to 8,000."""
    result = targets.compute_targets(98.84, avg_steps=4789, sex="male", height_cm=178, age=35)
    assert result.steps <= 6000  # the 6,000 safety floor, not the old 8,000 one
    increase_pct = (result.steps - 4789) / 4789
    assert increase_pct < 0.30  # nowhere near the old +67%


def test_default_step_average_still_yields_8000_target() -> None:
    """No avg_steps known: DEFAULT_STEPS=7000 must still nudge to the familiar 8,000."""
    result = targets.compute_targets(80, sex="male", height_cm=178, age=30)
    assert result.steps == 8000


def test_high_step_average_is_capped_at_12000() -> None:
    result = targets.compute_targets(80, avg_steps=15000, sex="male", height_cm=178, age=30)
    assert result.steps == 12000


def test_step_nudge_never_exceeds_1500_absolute_increase() -> None:
    result = targets.compute_targets(80, avg_steps=9000, sex="male", height_cm=178, age=30)
    assert result.steps - 9000 <= 1500


# ---------------------------------------------------------------------------
# E1 — deficit derived from goal weight + timeframe, clamped to 10-25% of maintenance
# ---------------------------------------------------------------------------


def test_deficit_without_timeframe_uses_flat_default() -> None:
    result = targets.compute_targets(
        90, avg_steps=8000, sex="male", height_cm=174, age=32,
        goal_type="fat_loss_muscle_retention",
    )
    assert result.basis["daily_adjustment"] == -450


def test_deficit_derived_from_rate_for_light_user_is_clamped_to_25_percent() -> None:
    """A light user with an aggressive rate must not get a >25%-of-maintenance deficit."""
    result = targets.compute_targets(
        60, avg_steps=8000, sex="female", height_cm=160, age=28,
        goal_type="fat_loss_muscle_retention",
        goal_weight_kg=50,
        goal_timeframe_weeks=8,  # implies ~1.25 kg/week — aggressive for a light person
    )
    max_allowed = result.maintenance * targets.MAX_DEFICIT_PCT_OF_MAINTENANCE
    assert abs(result.basis["daily_adjustment"]) <= max_allowed + 1  # rounding tolerance


def test_deficit_derived_from_rate_for_heavy_user(tmp_path=None) -> None:
    result = targets.compute_targets(
        100, avg_steps=8000, sex="male", height_cm=180, age=35,
        goal_type="fat_loss_muscle_retention",
        goal_weight_kg=96,
        goal_timeframe_weeks=8,  # implies 0.5 kg/week — a normal, sustainable rate
    )
    assert result.basis["rate_based_kg_per_week"] == 0.5
    min_allowed = result.maintenance * targets.MIN_DEFICIT_PCT_OF_MAINTENANCE
    max_allowed = result.maintenance * targets.MAX_DEFICIT_PCT_OF_MAINTENANCE
    assert min_allowed - 1 <= abs(result.basis["daily_adjustment"]) <= max_allowed + 1


def test_deficit_negligible_gap_does_not_force_artificial_floor() -> None:
    """Goal weight is essentially a rounding difference from current weight:
    the implied deficit is negligible and must not be forced up to the 10%
    safety floor — that floor only applies to a real, stated rate."""
    result = targets.compute_targets(
        70, avg_steps=8000, sex="male", height_cm=175, age=30,
        goal_type="fat_loss_muscle_retention",
        goal_weight_kg=69.98,
        goal_timeframe_weeks=12,
    )
    assert result.basis["daily_adjustment"] == -450  # falls back to the flat default


def test_explain_targets_mentions_rate_when_timeframe_known() -> None:
    result = targets.compute_targets(
        100, avg_steps=8000, sex="male", height_cm=180, age=35,
        goal_type="fat_loss_muscle_retention",
        goal_weight_kg=96,
        goal_timeframe_weeks=8,
    )
    text = targets.explain_targets(result)
    assert "0.5" in text
    assert "בשבוע" in text
