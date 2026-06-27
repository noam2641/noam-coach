from __future__ import annotations

import importlib

MODULES = (
    "assistant",
    "health_import",
    "onboarding",
    "questions",
    "recommendations",
    "reconcile",
    "routine",
    "targets",
    "user_model",
    "coach_bot",
)


def test_all_modules_import() -> None:
    for module in MODULES:
        assert importlib.import_module(module)
