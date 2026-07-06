"""Mini App presentation is now real files, not a Python f-string.

These tests cover the extraction without needing a browser: the template
renders with injected status blocks, the static JS/CSS is served with correct
content types, traversal is blocked, and the page itself stays auth-gated.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

import coach_bot
import miniapp


def test_render_index_injects_blocks_and_links_assets() -> None:
    html = miniapp.render_index("<b>סטטוס</b>", "<b>שבועי</b>")
    assert "<b>סטטוס</b>" in html
    assert "<b>שבועי</b>" in html
    assert "{{STATUS}}" not in html and "{{WEEKLY}}" not in html
    # Assets are external files, not inline scripts.
    assert '/mini/static/app.js' in html
    assert '/mini/static/styles.css' in html
    assert "script-src 'self'" in html


def test_static_files_exist_and_have_no_python_braces() -> None:
    js = (miniapp.STATIC_DIR / "app.js").read_text(encoding="utf-8")
    css = (miniapp.STATIC_DIR / "styles.css").read_text(encoding="utf-8")
    # The f-string escaping ({{ }}) must be gone now that JS lives in a file.
    assert "{{" not in js and "}}" not in js
    assert "saveProfile" in js and "loadDashboard" in js
    assert "loadTodayMeals" in js and "mealCard" in js
    assert "renderOperations" in js
    assert "onclick=" not in js
    assert "escapeHtml" in js
    assert ".card" in css
    assert ".ops-item" in css


def test_template_has_no_inline_event_handlers() -> None:
    html = (miniapp.TEMPLATES_DIR / "index.html").read_text(encoding="utf-8")
    assert "onclick=" not in html
    assert 'script-src \'self\'' in html
    assert 'id="todayMealsBox"' in html
    assert 'id="operationsBox"' in html


def test_static_route_serves_js_with_correct_type() -> None:
    client = TestClient(coach_bot.api)
    resp = client.get("/mini/static/app.js")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/javascript")
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert "saveProfile" in resp.text


def test_static_route_serves_css() -> None:
    client = TestClient(coach_bot.api)
    resp = client.get("/mini/static/styles.css")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/css")


def test_static_route_rejects_unknown_and_traversal() -> None:
    client = TestClient(coach_bot.api)
    assert client.get("/mini/static/secret.py").status_code == 404
    # Encoded traversal attempts must not escape the static allowlist.
    assert client.get("/mini/static/..%2F..%2Fcoach_bot.py").status_code == 404


def test_mini_page_requires_session(monkeypatch) -> None:
    monkeypatch.setattr(coach_bot.SETTINGS, "mini_app_secret", "z" * 48)
    client = TestClient(coach_bot.api)
    # No session cookie -> unauthorized, never leaks the page.
    resp = client.get("/mini")
    assert resp.status_code == 401
