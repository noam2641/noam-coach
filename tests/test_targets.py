from __future__ import annotations

import targets


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
