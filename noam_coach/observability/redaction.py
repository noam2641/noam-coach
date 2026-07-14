"""The canonical recursive redaction/sanitization boundary.

Every payload written through ``noam_coach.observability.emit`` passes
through :func:`redact` — dicts, lists, tuples, sets and nested combinations
are walked recursively.

WHAT THIS REDACTOR GUARANTEES
- Key-based redaction: any mapping key whose normalized name contains a
  sensitive word (token, secret, password, authorization, bearer, cookie,
  credential, api-key, private-key, signature/signing material, or a known
  raw-binary field name such as image_bytes/image_base64) has its value
  replaced with ``[REDACTED]``, regardless of the value's type or depth.
- Value-based redaction for KNOWN secret shapes: OpenAI ``sk-…`` keys,
  ``Bearer …`` headers, Telegram bot tokens (``<digits>:<35 url-safe
  chars>``), ``data:…;base64,…`` URLs, and long base64 blobs are replaced
  wherever they appear inside strings.
- Raw binary rejection: ``bytes``/``bytearray``/``memoryview`` values are
  never stored; they become a ``[BINARY:<n> bytes]`` placeholder.
- Bounded output: strings are truncated at ``max_string`` characters,
  containers at ``max_items`` entries, recursion at ``max_depth`` levels —
  a pathological payload cannot balloon the event store.
- Non-JSON-serializable objects are flattened to a bounded ``repr``.

WHAT THIS REDACTOR DOES **NOT** GUARANTEE
- It cannot recognize an arbitrary opaque secret embedded in free text with
  no known shape (e.g. a password typed by a user inside a sentence).
- It does not anonymize personal content: in ``content``/``debug`` modes
  user-visible text is retained BY DESIGN (that is the replay requirement).
  Access control and retention policy — not this function — govern who may
  read it.
- It does not deduplicate or classify PII beyond the shapes listed above.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"

# Normalized key fragments that always redact the value.
_SENSITIVE_KEY_WORDS = frozenset(
    {
        "token",
        "tokens",
        "secret",
        "secrets",
        "password",
        "passwd",
        "authorization",
        "bearer",
        "cookie",
        "cookies",
        "credential",
        "credentials",
        "apikey",
        "signature",
        "signing",
    }
)

# Compound names that redact even when the parts alone would be too broad.
_SENSITIVE_KEY_EXACT = frozenset(
    {
        "api_key",
        "private_key",
        "public_key",
        "session_key",
        "auth_header",
        "auth_headers",
        "set_cookie",
        "image_bytes",
        "image_base64",
        "photo_bytes",
        "file_bytes",
        "raw_bytes",
        "image_b64",
        "b64_image",
    }
)

_OPENAI_KEY_RE = re.compile(r"sk-[A-Za-z0-9_\-]{16,}")
_BEARER_RE = re.compile(r"[Bb]earer\s+[A-Za-z0-9._\-]{8,}")
_TELEGRAM_TOKEN_RE = re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")
_DATA_URL_RE = re.compile(r"data:[\w/.+-]+;base64,[A-Za-z0-9+/=]+")
_BASE64_BLOB_RE = re.compile(r"^[A-Za-z0-9+/\s]{512,}={0,2}$")


def _normalize_key(key: Any) -> tuple[str, frozenset[str]]:
    text = str(key).strip().lower().replace("-", "_")
    return text, frozenset(part for part in re.split(r"[^a-z0-9]+", text) if part)


def is_sensitive_key(key: Any) -> bool:
    text, parts = _normalize_key(key)
    if text in _SENSITIVE_KEY_EXACT:
        return True
    return bool(parts & _SENSITIVE_KEY_WORDS)


def _redact_string(value: str, max_string: int) -> str:
    if _BASE64_BLOB_RE.match(value):
        return f"[REDACTED:base64 {len(value)} chars]"
    value = _DATA_URL_RE.sub("[REDACTED:data-url]", value)
    value = _OPENAI_KEY_RE.sub(REDACTED, value)
    value = _TELEGRAM_TOKEN_RE.sub(REDACTED, value)
    value = _BEARER_RE.sub(REDACTED, value)
    if len(value) > max_string:
        return value[:max_string] + f"…[TRUNCATED {len(value) - max_string} chars]"
    return value


def redact(
    value: Any,
    *,
    max_depth: int = 8,
    max_string: int = 4000,
    max_items: int = 200,
) -> Any:
    """Return a JSON-safe, redacted copy of ``value`` (see module docstring)."""
    if max_depth <= 0:
        return "[MAX_DEPTH]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _redact_string(value, max_string)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"[BINARY:{len(value)} bytes]"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= max_items:
                out["[TRUNCATED]"] = f"{len(value) - max_items} more keys"
                break
            key_text = str(key)
            if is_sensitive_key(key_text):
                out[key_text] = REDACTED
            else:
                out[key_text] = redact(
                    item, max_depth=max_depth - 1, max_string=max_string, max_items=max_items
                )
        return out
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        out_list = [
            redact(item, max_depth=max_depth - 1, max_string=max_string, max_items=max_items)
            for item in items[:max_items]
        ]
        if len(items) > max_items:
            out_list.append(f"[TRUNCATED {len(items) - max_items} more items]")
        return out_list
    # Unknown object: flatten to a bounded repr so it can never smuggle
    # non-JSON payloads (or exceptions during json.dumps) into the store.
    return _redact_string(repr(value), min(max_string, 300))
