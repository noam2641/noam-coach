"""Central day-state invalidation contract (FIX 43, FIX 57 dependency).

FIX 43's required behavior: "Meal create/edit/undo must be treated as one
domain event that increments a day-state revision and invalidates every
dependent projection." This module is that single call site -- every writer
that changes meal/day state should call ``invalidate_day_projections`` instead
of independently deciding which downstream state to touch (or, as today,
touching none of it).

Batch A scope: wire this into meal create/edit/undo only (the FIX 43 minimum).
Later batches (D workout, E planned-meal, F menu/recommendation) extend the
set of projections this function clears as those domains gain their own
invalidatable state.
"""

from __future__ import annotations

from contextlib import suppress
from typing import Any

import health_service
from noam_coach.services import daily_menu_state
from noam_coach.services import next_meal as next_meal_service


async def invalidate_day_projections(
    db: Any,
    user_id: int,
    *,
    reason: str,
    now: Any = None,
) -> dict[str, bool]:
    """Invalidate every day-scoped projection that depends on meal state.

    Covers: active daily menu (marked stale, not deleted), active next-meal
    recommendation (cleared -- it will regenerate fresh on next request),
    and the learned routine profile's eating-window component (FIX 42:
    recomputed eagerly, reusing the same save_routine_profile() path the
    evening job and HealthKit import already call, rather than inventing a
    separate lazy-staleness mechanism for a projection with few, well-known
    readers). Returns a dict of which projections were actually
    present/refreshed, for logging/testing.
    """
    menu_invalidated = await daily_menu_state.mark_daily_menu_stale(
        db, user_id, reason=reason, now=now
    )

    had_recommendation = bool(
        await next_meal_service.get_active_recommendation_state(db, user_id, now=now)
    )
    if had_recommendation:
        await next_meal_service.clear_active_recommendation(db, user_id)

    routine_refreshed = False
    with suppress(Exception):
        await health_service.save_routine_profile(user_id)
        routine_refreshed = True

    return {
        "active_daily_menu": menu_invalidated,
        "active_recommendation": had_recommendation,
        "routine_profile": routine_refreshed,
    }
