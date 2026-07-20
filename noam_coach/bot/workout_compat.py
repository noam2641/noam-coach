"""Legacy workout-callback compatibility adapter (workout-selection
architecture, Batch 7).

New screens stopped minting legacy callbacks in Batch 4 -- but Telegram
messages are permanent. A card sent months ago still has a live
``startworkout:A`` button on it, and tapping it must not start a workout the
user was never shown. This module maps those already-issued callbacks onto
the same catalog identity, normalization, override and staleness rules the
``wk:`` flow uses, so old buttons behave like new ones wherever the mapping
is unambiguous -- and refuse safely where it is not.

**Mapping outcomes** (the four the plan's section K telemetry distinguishes):

* ``mapped`` -- exactly one session in the user's CURRENT plan carries this
  code. The legacy callback resolves to that session's real identity and is
  handled by the v2 path: same content, same start guards, same overrides.
* ``ambiguous`` -- two or more sessions share the code (a ``["F","F"]``
  Tier-2 split, or a plan repeating a code). A bare code cannot say WHICH,
  and guessing is exactly the defect this architecture exists to remove, so
  the user gets the selector to choose explicitly.
* ``template_fallback`` -- the user has no active plan of either tier, but
  the code is a real ``PLANS`` template. This is the honest legacy behavior
  (start a template workout) and is preserved deliberately.
* ``stale`` -- the code matches no current session AND no template (a
  regenerated plan that dropped it, or a ``custom`` code from
  ``planning.repair_workout_payload``). Refuses and re-renders.

Nothing here mints a legacy callback. The adapter is a one-way on-ramp from
old buttons into the v2 graph; it is not removed in this batch (see
``RETIREMENT_CRITERION``).
"""
from __future__ import annotations

from typing import Any

LEGACY_PREFIXES: tuple[str, ...] = (
    "workout:",
    "startworkout:",
    "editparams_menu:",
    "editparams:",
    "param:",
)

# Retirement policy (plan section K). Minting STOPPED at Batch 4; honoring
# continues indefinitely until production evidence says it is safe to stop.
# The criterion is deliberately usage-based rather than time-since-release:
# shipping the new code does not retire the old buttons, because the old
# buttons live in the user's chat history, not in our deployment.
RETIREMENT_CRITERION = (
    "Remove this adapter and the hard-coded legacy keyboards only after 30 "
    "consecutive days with zero workout_legacy_callback events whose outcome "
    "is 'mapped', 'ambiguous' or 'template_fallback' in product_events "
    "(outcome='stale' alone does not count as meaningful use -- a stale tap "
    "proves the button still exists but not that anyone depends on it). "
    "Removal is a separate follow-up review, never a side effect of a batch."
)

# Legacy actions, as recorded in telemetry.
ACTION_OVERVIEW = "overview"
ACTION_START = "start"
ACTION_EDIT_MENU = "edit_menu"
ACTION_EDIT_EXERCISE = "edit_exercise"
ACTION_EDIT_PARAM = "edit_param"

OUTCOME_MAPPED = "mapped"
OUTCOME_AMBIGUOUS = "ambiguous"
OUTCOME_TEMPLATE_FALLBACK = "template_fallback"
OUTCOME_STALE = "stale"


def parse_legacy_callback(data: str) -> dict[str, Any] | None:
    """Decompose a legacy workout callback into its parts, or None if `data`
    is not one. Never raises -- a malformed legacy payload must refuse like
    any other, not crash the router."""
    if data.startswith("startworkout:"):
        code = data.split(":", 1)[1]
        return {"action": ACTION_START, "code": code, "prefix": "startworkout"} if code else None
    if data.startswith("editparams_menu:"):
        code = data.split(":", 1)[1]
        return {"action": ACTION_EDIT_MENU, "code": code, "prefix": "editparams_menu"} if code else None
    if data.startswith("editparams:"):
        parts = data.split(":")
        if len(parts) != 3 or not parts[1]:
            return None
        try:
            exercise_index = int(parts[2])
        except (TypeError, ValueError):
            return None
        if exercise_index < 0:
            return None
        return {
            "action": ACTION_EDIT_EXERCISE, "code": parts[1],
            "exercise_index": exercise_index, "prefix": "editparams",
        }
    if data.startswith("param:"):
        parts = data.split(":")
        if len(parts) != 5 or not parts[1]:
            return None
        try:
            exercise_index = int(parts[2])
            delta = float(parts[4])
        except (TypeError, ValueError):
            return None
        if exercise_index < 0:
            return None
        return {
            "action": ACTION_EDIT_PARAM, "code": parts[1], "exercise_index": exercise_index,
            "field": parts[3], "delta": delta, "prefix": "param",
        }
    if data.startswith("workout:"):
        code = data.split(":", 1)[1]
        return {"action": ACTION_OVERVIEW, "code": code, "prefix": "workout"} if code else None
    return None


async def resolve_legacy_code(db: Any, user_id: int, code: str) -> dict[str, Any]:
    """Map a bare legacy workout code onto the user's CURRENT plan.

    Returns ``{"outcome": ..., "ref": WorkoutSelectionRef|None,
    "matches": int, "tier": str|None}``.

    A unique code match yields the real identity; 2+ matches are reported as
    ambiguous rather than resolved to the first (guessing which of two
    same-code sessions the user meant is precisely the bug class this
    architecture removes). No active plan + a known template code is the
    explicit legacy template path; anything else is stale.
    """
    from exercise_plans import PLANS
    from noam_coach.services import workout_catalog

    choices = await workout_catalog.list_selectable_workouts(db, user_id)
    matches = [choice for choice in choices if choice.code == code]

    if len(matches) == 1:
        return {
            "outcome": OUTCOME_MAPPED, "ref": matches[0].ref,
            "matches": 1, "tier": matches[0].ref.tier,
        }
    if len(matches) > 1:
        return {
            "outcome": OUTCOME_AMBIGUOUS, "ref": None,
            "matches": len(matches), "tier": matches[0].ref.tier,
        }
    if not choices and code in PLANS:
        # No plan at all: the honest legacy behavior is the global template.
        return {"outcome": OUTCOME_TEMPLATE_FALLBACK, "ref": None, "matches": 0, "tier": None}
    # The code is not in the current plan. Even if it is a PLANS key, starting
    # it would contradict the plan the user actually has -- refuse and let them
    # re-pick from what is real.
    return {"outcome": OUTCOME_STALE, "ref": None, "matches": 0, "tier": None}


async def emit_legacy_event(
    db: Any,
    user_id: int,
    *,
    prefix: str,
    action: str,
    code: str,
    outcome: str,
    ref: Any = None,
    source: str | None = None,
    detail: str | None = None,
) -> None:
    """Record one compact legacy-usage event (plan section K).

    Uses the existing canonical ``emit_event`` -- no parallel logging
    framework, no migration. ``emit_event`` already injects
    trace/interaction/span correlation (emit.py:110-118), so nothing is
    correlated by hand here.

    Best-effort by construction: telemetry must never break the user's
    action. An observability failure that swallowed a workout start would be
    a far worse defect than a missing row.
    """
    try:
        from noam_coach.observability import taxonomy
        from noam_coach.observability.emit import emit_event

        properties: dict[str, Any] = {
            "domain": "workout_legacy_callback",
            "prefix": prefix,
            "legacy_action": action,
            "code": code,
            "outcome": outcome,
        }
        if source:
            properties["resolved_source"] = source
        if detail:
            properties["detail"] = detail
        if ref is not None:
            properties["tier"] = ref.tier
            properties["session_index"] = ref.session_index
            if ref.plan_id is not None:
                properties["plan_id"] = ref.plan_id
            if ref.fact_rev:
                properties["fact_rev"] = ref.fact_rev

        await emit_event(
            db,
            user_id,
            taxonomy.DECISION_FINALIZED,
            entity="workout_legacy_callback",
            source="bot",
            surface="telegram",
            status="finalized",
            outcome=outcome,
            properties=properties,
        )
    except Exception:  # noqa: BLE001 - telemetry is never allowed to break the action
        return
