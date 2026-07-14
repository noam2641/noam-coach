"""Observability capture modes.

One explicit policy knob controls how much interaction payload the canonical
event stream retains:

OFF       — no interaction payload observability beyond the pre-existing
            direct ``append_event`` domain writes (which predate this system
            and remain the caller's responsibility).  ``emit_event`` becomes
            a no-op.
METADATA  — correlation, event type, purpose, outcome, model metadata,
            media metadata and content DIGESTS (sha256 + length); no full
            conversational content.
CONTENT   — additionally retains user-visible/input content and structured
            AI/product payloads, after canonical redaction.
DEBUG     — maximum safe structured trace detail for tests/local
            diagnostics.  Still never stores secrets or raw binary — the
            redaction boundary applies in every mode.

The mode is read from the ``NOAM_OBSERVABILITY_MODE`` environment variable
once, lazily; tests set it explicitly with :func:`set_mode` (a test must
never depend implicitly on the production default).
"""

from __future__ import annotations

import os
from enum import Enum

_ENV_VAR = "NOAM_OBSERVABILITY_MODE"
_DEFAULT = "content"


class ObservabilityMode(str, Enum):
    OFF = "off"
    METADATA = "metadata"
    CONTENT = "content"
    DEBUG = "debug"


_mode: ObservabilityMode | None = None


def get_mode() -> ObservabilityMode:
    global _mode
    if _mode is None:
        raw = os.environ.get(_ENV_VAR, _DEFAULT).strip().lower()
        try:
            _mode = ObservabilityMode(raw)
        except ValueError:
            _mode = ObservabilityMode(_DEFAULT)
    return _mode


def set_mode(mode: ObservabilityMode | str) -> ObservabilityMode:
    """Set the mode explicitly (tests, admin tooling). Returns the previous mode."""
    global _mode
    previous = get_mode()
    _mode = ObservabilityMode(mode)
    return previous


def reset_mode() -> None:
    """Forget the explicit/lazy mode so the next read re-consults the environment."""
    global _mode
    _mode = None


def observability_enabled() -> bool:
    return get_mode() is not ObservabilityMode.OFF


def captures_content() -> bool:
    return get_mode() in (ObservabilityMode.CONTENT, ObservabilityMode.DEBUG)
