"""Every button the bot renders must reach a handler.

`menu:goals` was emitted by the next-best-action engine
(`coach_intelligence.py:215`) and by the assistant's "write a different goal"
button, but the handler was registered for `menu:goal` -- singular. The
plural matched nothing, fell through the entire dispatch chain to
`handle_session_action_callback`, and hit a bare `return` behind a
`parts[1].isdigit()` guard.

The failure was completely silent: no reply, no rendered message, no event.
In the 2026-07-27 session the user tapped the bot's own top-priority CTA four
times and the Telegram message did not even change. And because a
`goal_versions` row can only be created from the approval that screen mints,
no goal could ever exist -- which left every nutrition feature blocked behind
"missing: approved daily target".

A one-character typo, invisible to tests and to telemetry, disabled a
headline feature. These tests make both halves impossible: the callback must
be routable, and an unroutable one must announce itself.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: Modules that own callback dispatch.
_HANDLER_SOURCES = (
    "noam_coach/bot/callback_menu.py",
    "noam_coach/bot/callback_plans.py",
    "noam_coach/bot/callback_meals.py",
    "noam_coach/bot/callback_session.py",
    "noam_coach/bot/callback_router.py",
)

#: `button("label", "menu:x")` and `InlineKeyboardButton("label", callback_data="menu:x")`.
_EMIT_PATTERNS = (
    re.compile(r"""button\(\s*[^,]+,\s*["'](menu:[a-z_]+)["']""", re.S),
    re.compile(r"""callback_data\s*=\s*["'](menu:[a-z_]+)["']"""),
    re.compile(r"""^\s*["'](menu:[a-z_]+)["'],\s*$""", re.M),
)


def _production_files() -> list[Path]:
    out = []
    for path in ROOT.rglob("*.py"):
        parts = path.parts
        if any(p in {".venv", "tests", "__pycache__", "build", "dist"} for p in parts):
            continue
        out.append(path)
    return out


def _emitted_menu_callbacks() -> dict[str, set[str]]:
    """Every `menu:*` literal the product can put on a button → files."""
    found: dict[str, set[str]] = {}
    for path in _production_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pattern in _EMIT_PATTERNS:
            for match in pattern.finditer(text):
                found.setdefault(match.group(1), set()).add(
                    str(path.relative_to(ROOT)).replace("\\", "/")
                )
    return found


def _handled_menu_callbacks() -> set[str]:
    """Every `menu:*` literal a dispatch module compares against.

    Parsed from the AST rather than by regex so `data == "menu:x"`,
    `data in {...}` and `data.startswith("menu:x")` are all captured, and a
    literal appearing only in a comment is not.
    """
    handled: set[str] = set()

    def collect(node: ast.AST) -> None:
        for child in ast.walk(node):
            if isinstance(child, ast.Constant) and isinstance(child.value, str):
                if child.value.startswith("menu:"):
                    handled.add(child.value)

    for rel in _HANDLER_SOURCES:
        path = ROOT / rel
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        for node in ast.walk(tree):
            # Only comparisons/membership tests count as "handled".
            if isinstance(node, (ast.Compare, ast.If, ast.Match)):
                collect(node)
    return handled


def test_every_emitted_menu_callback_has_a_handler() -> None:
    """The regression: a rendered button that no branch can match."""
    emitted = _emitted_menu_callbacks()
    handled = _handled_menu_callbacks()

    unreachable = {
        name: sorted(files)
        for name, files in emitted.items()
        if name not in handled
    }

    assert not unreachable, (
        "these callbacks are rendered to users but no handler matches them, "
        "so tapping them does nothing:\n"
        + "\n".join(f"  {name} <- {', '.join(files)}" for name, files in unreachable.items())
    )


def test_menu_goals_specifically_is_routable() -> None:
    """Pin the exact literal that was dead, so the typo cannot return."""
    handled = _handled_menu_callbacks()
    assert "menu:goals" in handled
    assert "menu:goal" in handled, "the singular must keep working too"


def test_emitters_still_exist_for_the_goal_cta() -> None:
    """If the CTA is renamed, this test should be updated deliberately."""
    emitted = _emitted_menu_callbacks()
    assert "menu:goals" in emitted, (
        "menu:goals is no longer emitted -- if that is intentional, drop the "
        "compatibility branch in callback_plans.py too"
    )


def test_unhandled_callbacks_are_announced_and_recorded() -> None:
    """A callback that matches nothing must not fail silently.

    The dispatch chain's terminal function previously returned bare. Both an
    acknowledgement to the user and an event for us are required, because the
    absence of a render is not something anyone can query for.
    """
    source = (ROOT / "noam_coach/bot/callback_session.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    target = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "handle_session_action_callback"
    )
    # The guard is the first statement after the `parts = data.split(":")`.
    guard_src = ast.get_source_segment(source, target) or ""
    head = guard_src.split("action = parts[0]")[0]

    assert "_emit_unhandled_callback" in head, (
        "an unroutable callback must emit an event -- otherwise a dead button "
        "is indistinguishable from a working one in the event stream"
    )
    assert "safe_answer_callback" in head, (
        "an unroutable callback must answer the user -- otherwise Telegram "
        "leaves the button spinning and the app reads as frozen"
    )


def test_emit_helper_records_only_the_callback_prefix() -> None:
    """The tail of a callback can carry ids; only the prefix is safe to store."""
    source = (ROOT / "noam_coach/bot/callback_session.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    helper = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_emit_unhandled_callback"
    )
    body = ast.get_source_segment(source, helper) or ""

    assert 'split(":")[0]' in body, "only the prefix may be recorded"
    # The raw callback string must never be passed through wholesale.
    assert '"callback_data": data' not in body
