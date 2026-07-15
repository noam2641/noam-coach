"""Batch G (FIX 44, partial): the Mini App must refresh its read-only
panels when the page regains focus/visibility, not stay a static snapshot
while Telegram/jobs continue changing the same user state.

Root cause (verified before this fix): app.js had no visibilitychange or
focus listener at all -- loadDashboard/loadNextMeal/loadTodayMeals only
ran on initial page load or an explicit manual refresh button tap.
"""
from __future__ import annotations

import miniapp


def test_app_js_registers_visibilitychange_listener() -> None:
    js = (miniapp.STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "visibilitychange" in js
    assert "addEventListener('visibilitychange'" in js


def test_app_js_registers_window_focus_listener() -> None:
    js = (miniapp.STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "window.addEventListener('focus'" in js


def test_app_js_focus_refresh_calls_the_read_only_loaders() -> None:
    """The refresh must re-run the actual data loaders, not just a stub.

    Observability O8: the loaders take a trigger argument so a focus refresh
    is distinguishable from a user action in the semantic view telemetry —
    the assertion accepts the call with its trigger."""
    js = (miniapp.STATIC_DIR / "app.js").read_text(encoding="utf-8")
    refresh_fn_start = js.index("function refreshOnRegainedFocus")
    refresh_fn_body = js[refresh_fn_start:refresh_fn_start + 400]
    assert "loadDashboard('focus_refresh')" in refresh_fn_body
    assert "loadNextMeal('focus_refresh')" in refresh_fn_body
    assert "loadTodayMeals('focus_refresh')" in refresh_fn_body


def test_app_js_focus_refresh_is_debounced() -> None:
    """A rapid focus/blur toggle must not spam the API -- there must be a
    minimum-interval guard."""
    js = (miniapp.STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "FOCUS_REFRESH_MIN_INTERVAL_MS" in js
    assert "lastFocusRefresh" in js
