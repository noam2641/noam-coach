"""Architecture guardrails for the structured rebuild."""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import coach_bot

ROOT = Path(__file__).resolve().parents[1]


def _line_count(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


def test_coach_bot_is_a_thin_compatibility_facade() -> None:
    path = ROOT / "coach_bot.py"
    assert _line_count(path) < 750
    tree = ast.parse(path.read_text(encoding="utf-8"))
    definitions = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    assert definitions == []


def test_callback_dispatchers_remain_small() -> None:
    callback_lines = len(inspect.getsource(coach_bot.handle_callback).splitlines())
    session_lines = len(inspect.getsource(coach_bot.handle_session_action_callback).splitlines())
    assert callback_lines <= 110
    assert session_lines <= 100


def test_critical_routes_are_registered_once() -> None:
    paths = [getattr(route, "path", None) for route in coach_bot.api.routes]
    for expected in (
        "/healthz",
        "/readyz",
        "/api/healthkit/samples",
        "/api/shortcut/health",
        "/api/watch/current/{user_id}",
        "/api/watch/set",
    ):
        assert paths.count(expected) == 1


def test_structured_package_contains_expected_boundaries() -> None:
    expected = (
        "noam_coach/services/core.py",
        "noam_coach/services/training.py",
        "noam_coach/bot/callback_router.py",
        "noam_coach/bot/callback_session.py",
        "noam_coach/api/security.py",
        "noam_coach/api/health_routes.py",
        "noam_coach/api/watch_routes.py",
        "noam_coach/jobs/proactive.py",
        "noam_coach/app/runtime.py",
    )
    for relative in expected:
        assert (ROOT / relative).is_file(), relative
