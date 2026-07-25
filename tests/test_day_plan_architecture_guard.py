"""Architecture guard (P1.1b, scope 3) — no live daily surface may independently
derive the meal count. DayPlan/the shared carrier is the single decision owner.

This is a FOCUSED allowlist guard, not a brittle repository-wide text search:
- the raw legacy decider `_meals_remaining` must be reachable from exactly ONE
  sanctioned call site (the carrier bridge fallback);
- the daily count carrier (`WorkoutNutritionContext.meals_remaining_estimate`)
  must be produced only through the DayPlan bridge;
- `meal_intent._slot_plan` shapes slot roles/timing only — its COUNT is supplied
  by the canonical carrier, not decided locally.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# The ONLY module+function allowed to call the raw legacy decider _meals_remaining.
_MEALS_REMAINING_ALLOWLIST = {
    ("noam_coach/services/next_meal.py", "build_workout_nutrition_context"),
}


def _call_sites(path: Path, func_name: str) -> list[tuple[str, str]]:
    """Return (relpath, enclosing-function) for every call to ``func_name`` in
    the file, using the AST (robust to comments/strings — not a text grep)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    rel = path.relative_to(REPO).as_posix()
    sites: list[tuple[str, str]] = []

    class _V(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: list[str] = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

        def visit_Call(self, node: ast.Call) -> None:
            fn = node.func
            name = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if name == func_name:
                sites.append((rel, self.stack[-1] if self.stack else "<module>"))
            self.generic_visit(node)

    _V().visit(tree)
    return sites


def test_meals_remaining_called_only_from_sanctioned_site() -> None:
    """The raw legacy meal-count decider must not sprout a second caller."""
    found: set[tuple[str, str]] = set()
    for py in (REPO / "noam_coach").rglob("*.py"):
        found.update(_call_sites(py, "_meals_remaining"))
    # ignore the definition itself (it appears as a def, not a call)
    assert found == _MEALS_REMAINING_ALLOWLIST, (
        f"Unexpected _meals_remaining call sites: {found - _MEALS_REMAINING_ALLOWLIST}. "
        "Daily meal count must come from DayPlan/the carrier, not a new legacy caller."
    )


def test_carrier_is_produced_through_the_dayplan_bridge() -> None:
    """meals_remaining_estimate must be set via resolve_remaining_meals_estimate
    (the DayPlan-owned bridge), not by an ad-hoc second computation."""
    src = (REPO / "noam_coach/services/next_meal.py").read_text(encoding="utf-8")
    assert "resolve_remaining_meals_estimate(" in src
    # the bridge lives in day_plan and is the canonical owner
    dp = (REPO / "noam_coach/services/day_plan.py").read_text(encoding="utf-8")
    assert "def resolve_remaining_meals_estimate(" in dp


def test_slot_plan_has_single_role_timing_call_site() -> None:
    """meal_intent._slot_plan is role/timing only and must have one call site;
    its count comes from the carrier (build_remaining_slot_allocations)."""
    sites = _call_sites(REPO / "noam_coach/services/meal_intent.py", "_slot_plan")
    assert len(sites) == 1
    assert sites[0] == ("noam_coach/services/meal_intent.py", "build_meal_intents")


def test_reconcile_slots_to_count_is_carrier_owned() -> None:
    """_slot_plan supplies roles/timing; the COUNT is imposed by the carrier via
    _reconcile_slots_to_count — proving the count is the canonical decision."""
    from noam_coach.services.meal_intent import _reconcile_slots_to_count

    roles = [("breakfast", "בוקר", 8), ("lunch", "צהריים", 13), ("dinner", "ערב", 20)]
    # carrier says fewer -> trim to the earliest N (chronological)
    assert len(_reconcile_slots_to_count(roles, 2)) == 2
    # carrier says exactly -> unchanged
    assert _reconcile_slots_to_count(roles, 3) == sorted(roles, key=lambda s: s[2])
    # carrier says more -> pad with generic slots so the preferred meals appear
    padded = _reconcile_slots_to_count(roles, 6)
    assert len(padded) == 6
    hours = [h for _s, _l, h in padded]
    assert hours == sorted(hours)  # still chronological


async def test_menu_status_dayplan_agree_on_count(tmp_path) -> None:
    """End-to-end count parity: with an explicit 6-meal preference, Today's Menu
    (build_meal_intents), Today's Status carrier, and DayPlan all return 6 — the
    exact divergence P1.1b closes (Menu previously returned 3)."""
    from datetime import datetime

    import user_model
    from config import TZ
    from db import Database
    from helpers import utc_now
    from noam_coach.services import day_plan as dp
    from noam_coach.services.meal_intent import build_meal_intents
    from noam_coach.services.next_meal import build_workout_nutrition_context
    from noam_coach.services.preference_profile import build_preference_profile

    db = Database(str(tmp_path / "parity.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id,first_name,username,updated_at) VALUES(1,'T',NULL,?)",
        (utc_now(),))
    await db.execute(
        "INSERT INTO goal_versions(user_id,calories,protein,steps,phase,status,source,created_at)"
        " VALUES(1,2400,180,8000,'x','active','t',?)", (utc_now(),))
    await user_model.set_fact(
        db, 1, "sleep_schedule", {"bedtime": "23:00"},
        source=user_model.SOURCE_USER, confirmed=True)
    await dp.persist_preferred_meal_count(db, 1, minimum=6, maximum=6)

    now = datetime(2026, 6, 28, 10, 0, tzinfo=TZ)
    status = (await build_workout_nutrition_context(db, 1, now=now)).meals_remaining_estimate
    pref = await build_preference_profile(db, 1, now=now, flags={})
    menu = len(await build_meal_intents(db, 1, pref, calorie_target=2400, protein_target=180, now=now))
    plan = (await dp.build_day_plan(db, 1, as_of=now)).remaining_meals
    assert status == menu == plan == 6
