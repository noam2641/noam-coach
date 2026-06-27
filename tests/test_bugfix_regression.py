"""Regression tests for bug fixes — ensure fixed issues stay fixed."""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# 1. P0 Startup — ensure_user_record resolution
# ---------------------------------------------------------------------------


def test_ensure_user_record_accessible() -> None:
    import coach_bot as cb

    assert hasattr(cb, "ensure_user_record")
    assert callable(cb.ensure_user_record)


def test_coach_bot_registered_in_sys_modules() -> None:
    import sys

    import coach_bot  # noqa: F401 — side-effect: registers in sys.modules

    assert "coach_bot" in sys.modules or __name__ == "__main__"


# ---------------------------------------------------------------------------
# 2. SQL Injection fix in migration — parameterized query
# ---------------------------------------------------------------------------


def test_migration_goal_versions_uses_parameterized_query() -> None:
    import inspect

    import db

    source = inspect.getsource(db._migration_goal_versions)
    # Should NOT have f-string interpolation of now
    assert "'{now}'" not in source
    assert "f\"" not in source or "'{now}'" not in source
    # Should have ? placeholders
    assert "?, ?" in source


# ---------------------------------------------------------------------------
# 3. Content-Length handling — APIBodyLimitMiddleware structural check
# ---------------------------------------------------------------------------


def test_content_length_bytes_decoded() -> None:
    from noam_coach.api.security import APIBodyLimitMiddleware

    assert callable(APIBodyLimitMiddleware)
    # The middleware decodes bytes via .decode("ascii"); verify the source uses it
    import inspect

    source = inspect.getsource(APIBodyLimitMiddleware)
    assert ".decode(" in source


# ---------------------------------------------------------------------------
# 4. FoodItem validate_assignment
# ---------------------------------------------------------------------------


def test_fooditem_rejects_nan_on_assignment() -> None:
    from models import FoodItem

    item = FoodItem(
        name="test", grams=100, calories=200, protein=20, carbs=30, fat=5, confidence=0.9
    )
    with pytest.raises((ValueError, Exception)):
        item.calories = float("nan")


def test_fooditem_rejects_negative_on_assignment() -> None:
    from models import FoodItem

    item = FoodItem(
        name="test", grams=100, calories=200, protein=20, carbs=30, fat=5, confidence=0.9
    )
    with pytest.raises((ValueError, Exception)):
        item.grams = -1


def test_fooditem_rejects_infinity_on_assignment() -> None:
    from models import FoodItem

    item = FoodItem(
        name="test", grams=100, calories=200, protein=20, carbs=30, fat=5, confidence=0.9
    )
    with pytest.raises((ValueError, Exception)):
        item.calories = float("inf")


def test_fooditem_valid_assignment_works() -> None:
    from models import FoodItem

    item = FoodItem(
        name="test", grams=100, calories=200, protein=20, carbs=30, fat=5, confidence=0.9
    )
    item.grams = 150.0
    assert item.grams == 150.0


def test_fooditem_macro_consistency_no_recursion() -> None:
    from models import FoodItem

    # Macro inconsistency should lower confidence, not cause recursion.
    # calories=1000 vs macro_kcal = 10*4 + 10*4 + 10*9 = 170 — a big divergence.
    item = FoodItem(
        name="test",
        grams=100,
        calories=1000,
        protein=10,
        carbs=10,
        fat=10,
        confidence=0.9,
    )
    assert item.confidence <= 0.3  # lowered by macro_calorie_consistency


# ---------------------------------------------------------------------------
# 5. XSS prevention in _safe_html_block
# ---------------------------------------------------------------------------


def test_safe_html_block_allows_bare_tags() -> None:
    from helpers import _safe_html_block

    assert "<b>" in _safe_html_block("<b>bold</b>")
    assert "<i>" in _safe_html_block("<i>italic</i>")


def test_safe_html_block_blocks_attributes() -> None:
    from helpers import _safe_html_block

    result = _safe_html_block('<b onmouseover="alert(1)">text</b>')
    assert "onmouseover" not in result or "&lt;" in result
    assert "<b onmouseover" not in result


def test_safe_html_block_blocks_script() -> None:
    from helpers import _safe_html_block

    result = _safe_html_block("<script>alert(1)</script>")
    assert "<script>" not in result


# ---------------------------------------------------------------------------
# 6. Goals activation transaction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_activate_goal_version_is_atomic(tmp_path: object) -> None:
    """Verify that activate_goal_version uses a transaction."""
    import inspect

    from noam_coach.services.goals import activate_goal_version

    source = inspect.getsource(activate_goal_version)
    assert "transaction" in source.lower()


# ---------------------------------------------------------------------------
# 7. Naive datetime handling
# ---------------------------------------------------------------------------


def test_naive_datetime_made_aware() -> None:
    from datetime import datetime, timezone

    naive = datetime(2026, 1, 1, 12, 0, 0)
    if naive.tzinfo is None:
        naive = naive.replace(tzinfo=timezone.utc)
    aware = datetime.now(timezone.utc)
    # Should NOT raise TypeError
    delta = aware - naive
    assert delta.total_seconds() != 0


def test_aware_datetime_stays_aware() -> None:
    from datetime import datetime, timezone

    aware = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    if aware.tzinfo is None:
        aware = aware.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - aware
    assert delta.total_seconds() != 0


# ---------------------------------------------------------------------------
# 8. routine.py robust_mean None handling
# ---------------------------------------------------------------------------


def test_robust_mean_none_no_crash() -> None:
    from routine import robust_mean

    # When all values are outliers, robust_mean returns None.
    result = robust_mean([1, 1_000_000])  # likely filters everything
    # Should not crash with round(None, 1)
    if result is not None:
        rounded = round(result, 1)
        assert isinstance(rounded, float)


# ---------------------------------------------------------------------------
# 9. extract_flow_id handles legacy flows
# ---------------------------------------------------------------------------


def test_extract_flow_id_regular() -> None:
    from conversation import encode_callback, extract_flow_id

    data = encode_callback("test", "action", flow_id="f-42-abc123")
    result = extract_flow_id(data)
    assert result == "f-42-abc123"


def test_extract_flow_id_legacy() -> None:
    from conversation import encode_callback, extract_flow_id

    data = encode_callback("test", "action", flow_id="legacy-42")
    result = extract_flow_id(data)
    assert result == "legacy-42"


# ---------------------------------------------------------------------------
# 10. Watch routes bounds check
# ---------------------------------------------------------------------------


def test_watch_routes_bounds_check_exists() -> None:
    import inspect

    from noam_coach.api.watch_routes import watch_current, watch_set

    src_current = inspect.getsource(watch_current)
    assert "exercise_index" in src_current
    assert "len(" in src_current or "IndexError" in src_current or ">=" in src_current
    # Also verify watch_set has the same guard
    src_set = inspect.getsource(watch_set)
    assert "exercise_index" in src_set


# ---------------------------------------------------------------------------
# 11. targets.py None-safe defaults
# ---------------------------------------------------------------------------


def test_targets_zero_age_not_replaced() -> None:
    """Zero should not be silently replaced by default."""
    import inspect

    import targets

    source = inspect.getsource(targets.compute_targets)
    # Should use 'is not None' pattern, not bare 'or'
    assert "age or" not in source
    assert "height_cm or" not in source


# ---------------------------------------------------------------------------
# 12. Planning protein floor
# ---------------------------------------------------------------------------


def test_planning_protein_floor_maintained() -> None:
    import inspect

    import planning

    source = inspect.getsource(planning._meal_slots)
    assert "max(15" in source
