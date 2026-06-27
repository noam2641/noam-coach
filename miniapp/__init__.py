"""Mini App presentation layer.

The HTML/CSS/JS for the Telegram Mini App lives in real files under this
package (``templates/index.html``, ``static/app.js``, ``static/styles.css``)
instead of as a giant f-string inside ``coach_bot.py``. The server still owns
every calculation; this module only assembles the shell and injects two
server-rendered, already-escaped status blocks.
"""

from __future__ import annotations

from pathlib import Path

_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = _DIR / "templates"
STATIC_DIR = _DIR / "static"

_INDEX_TEMPLATE = TEMPLATES_DIR / "index.html"


def render_index(status_html: str, weekly_html: str) -> str:
    """Render the Mini App shell with two server-escaped status blocks.

    ``status_html`` and ``weekly_html`` must already be safe HTML (the caller
    runs them through ``helpers._safe_html_block``). Substitution is literal —
    no template engine, no ``eval`` — so untrusted content cannot inject markup
    beyond what the escaper already permits.
    """
    template = _INDEX_TEMPLATE.read_text(encoding="utf-8")
    return template.replace("{{STATUS}}", status_html).replace("{{WEEKLY}}", weekly_html)
