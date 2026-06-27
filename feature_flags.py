"""Central feature flags with safe defaults.

Flags make large product changes reversible.  They are intentionally read
from environment variables so the release ships without an ``.env`` file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().casefold() in {"1", "true", "yes", "on", "enabled"}


@dataclass(frozen=True)
class FeatureFlags:
    conversation_router_v2: bool = True
    smart_plans_v2: bool = True
    meal_duplicate_v2: bool = True
    workout_intelligence_v2: bool = True
    next_best_action: bool = True
    mini_app_plans: bool = True

    @classmethod
    def from_environment(cls) -> "FeatureFlags":
        return cls(
            conversation_router_v2=_env_bool("FEATURE_CONVERSATION_ROUTER_V2", True),
            smart_plans_v2=_env_bool("FEATURE_SMART_PLANS_V2", True),
            meal_duplicate_v2=_env_bool("FEATURE_MEAL_DUPLICATE_V2", True),
            workout_intelligence_v2=_env_bool("FEATURE_WORKOUT_INTELLIGENCE_V2", True),
            next_best_action=_env_bool("FEATURE_NEXT_BEST_ACTION", True),
            mini_app_plans=_env_bool("FEATURE_MINI_APP_PLANS", True),
        )


FLAGS = FeatureFlags.from_environment()
