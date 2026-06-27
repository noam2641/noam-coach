from exercise_plans import PLANS, SPLIT_BY_FREQUENCY
from training_intelligence import CATALOG


def test_full_body_covers_all_primary_movement_patterns():
    movements = {
        CATALOG[exercise["id"]].movement
        for exercise in PLANS["F"]["exercises"]
        if exercise["id"] in CATALOG
    }
    assert {"squat", "hinge", "horizontal_push", "vertical_pull", "vertical_push"} <= movements


def test_one_and_two_day_plans_use_full_body():
    assert SPLIT_BY_FREQUENCY[1] == ["F"]
    assert SPLIT_BY_FREQUENCY[2] == ["F", "F"]
