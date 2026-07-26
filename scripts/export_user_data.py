#!/usr/bin/env python3
"""Export one user's database records and referenced meal images to a ZIP."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from noam_coach.observability.redaction import (  # noqa: E402
    REDACTED,
    is_sensitive_key,
    redact,
)

DIRECT_TABLES = (
    "users",
    # LOG-016: DSAR export reads the live goal source. The legacy `goals` table
    # is frozen by derivation and can hold a stale value for active_provisional
    # users, so we export `goal_versions` (the sole authoritative read path).
    "goal_versions",
    "approvals",
    "meals",
    "sessions",
    "health",
    "audit",
    "routine_profile",
    "daily_flags",
    "job_state",
    "user_facts",
    "user_fact_history",
    "medical_constraints",
    "medication_events",
    "conversation_state",
    "analytics_events",
    "exercise_overrides",
)


# --------------------------------------------------------------------------
# LOG-017: `analytics_events.properties` is raw, un-allowlisted JSON written by
# `track_event(**properties)`. The LOG-012 audit allowlist never covered it, so
# the DSAR ZIP shipped whatever a call site happened to pass (raw callback data,
# free-text, and any secret a future dev adds) verbatim.
#
# The owner decision is to PRESERVE the user's own data rather than drop the
# stream (the `product_events` pattern), so redaction happens HERE — at export
# time only. Stored rows are never rewritten and no schema changes.
#
# Strategy: defensive, not allowlist-based, because analytics property names are
# open-ended. We keep bounded scalars belonging to the requester and drop
# anything unbounded or secret-shaped.
# --------------------------------------------------------------------------

# Analytics properties are telemetry, not prose: any string longer than this is
# treated as unbounded free text and dropped. Mirrors the audit trail's
# `_AUDIT_MAX_SCALAR_STR` so both DSAR streams bound text identically.
_ANALYTICS_MAX_STR = 64
_ANALYTICS_MAX_DEPTH = 6
_ANALYTICS_MAX_ITEMS = 50

DROPPED_FREE_TEXT = "[DROPPED:free_text]"
UNPARSEABLE = "[REDACTED:unparseable]"


def _looks_like_json(text: str) -> bool:
    """Does this string CLAIM to be a JSON container?

    Deliberately keyed on the OPENING delimiter alone. Requiring a matching
    closing delimiter would let a truncated payload (``'{"token": "abc'``)
    skip the parse path and be treated as ordinary short text — i.e. malformed
    input would pass through precisely because it was malformed. Anything that
    starts like a container must parse cleanly or be redacted.
    """
    stripped = text.strip()
    return bool(stripped) and stripped[0] in "{["


def _redact_analytics_value(value: Any, depth: int = _ANALYTICS_MAX_DEPTH) -> Any:
    """Recursively redact one analytics property value (see module notes).

    Fails CLOSED: any value we cannot positively inspect and classify as safe
    is replaced with a placeholder rather than passed through.
    """
    if depth <= 0:
        # Too deep to keep inspecting — do not emit the untraversed remainder.
        return UNPARSEABLE
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"[BINARY:{len(value)} bytes]"
    if isinstance(value, str):
        # A JSON document smuggled inside a string field must be parsed and
        # redacted recursively, never passed through as opaque text.
        if _looks_like_json(value):
            try:
                parsed = json.loads(value)
            except (ValueError, TypeError):
                # Claims to be a container but is malformed: we cannot inspect
                # it, so it does not ship. FAIL CLOSED.
                return UNPARSEABLE
            return _redact_analytics_value(parsed, depth - 1)
        # Canonical redactor strips known secret shapes (sk-, Bearer, telegram
        # tokens, data URLs, base64 blobs) before we apply the length bound.
        cleaned = redact(value, max_depth=1, max_string=_ANALYTICS_MAX_STR)
        if not isinstance(cleaned, str):
            return UNPARSEABLE
        if cleaned != value:
            # The redactor changed something: either a secret shape was found
            # or the string exceeded the bound. Either way it is not safe to
            # ship a partial value — drop it entirely.
            return REDACTED if REDACTED in cleaned else DROPPED_FREE_TEXT
        if len(cleaned) > _ANALYTICS_MAX_STR:
            return DROPPED_FREE_TEXT
        return cleaned
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= _ANALYTICS_MAX_ITEMS:
                out["[TRUNCATED]"] = f"{len(value) - _ANALYTICS_MAX_ITEMS} more keys"
                break
            key_text = str(key)
            # Secret-like KEY: value never ships regardless of its type/depth.
            if is_sensitive_key(key_text):
                out[key_text] = REDACTED
            else:
                out[key_text] = _redact_analytics_value(item, depth - 1)
        return out
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        out_list = [
            _redact_analytics_value(item, depth - 1) for item in items[:_ANALYTICS_MAX_ITEMS]
        ]
        if len(items) > _ANALYTICS_MAX_ITEMS:
            out_list.append(f"[TRUNCATED {len(items) - _ANALYTICS_MAX_ITEMS} more items]")
        return out_list
    # Unknown/unexpected object type: not inspectable, so it does not ship.
    return UNPARSEABLE


def redact_analytics_row(row: dict[str, Any]) -> dict[str, Any]:
    """Return a DSAR-safe copy of one `analytics_events` row.

    `id`, `user_id`, `event` and `created_at` are the user's own bounded,
    non-secret metadata and are preserved so the export stays meaningful.
    `properties` is parsed and recursively redacted; malformed JSON fails
    closed to a placeholder instead of shipping the raw stored text.
    """
    safe = dict(row)
    raw = safe.get("properties")
    if raw is None:
        return safe
    if isinstance(raw, (bytes, bytearray, memoryview)):
        safe["properties"] = UNPARSEABLE
        return safe
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            # Stored value is not valid JSON. It may be arbitrary raw text
            # (including a secret), and we cannot inspect its structure, so it
            # must NOT pass through merely because parsing failed.
            safe["properties"] = UNPARSEABLE
            return safe
    else:
        parsed = raw
    safe["properties"] = _redact_analytics_value(parsed)
    # Bounded event name; the redactor also strips secret shapes defensively.
    safe["event"] = redact(safe.get("event"), max_depth=1, max_string=_ANALYTICS_MAX_STR)
    return safe


# Per-table row transforms applied to DSAR output. Absent tables ship as-is.
ROW_REDACTORS = {
    "analytics_events": redact_analytics_row,
}


def rows(connection: sqlite3.Connection, sql: str, params=()):
    return [dict(row) for row in connection.execute(sql, params).fetchall()]


def export_user(db_path: Path, user_id: int, output: Path) -> Path:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        payload: dict[str, object] = {
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "user_id": user_id,
            "tables": {},
        }
        tables = payload["tables"]
        assert isinstance(tables, dict)
        for table in DIRECT_TABLES:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if exists:
                # Cross-user isolation: every table is scoped to the requesting
                # user here, and redaction below never widens that scope.
                table_rows = (
                    rows(connection, f'SELECT * FROM "{table}" WHERE user_id=?', (user_id,))
                    if table != "users"
                    else rows(connection, "SELECT * FROM users WHERE id=?", (user_id,))
                )
                redactor = ROW_REDACTORS.get(table)
                if redactor is not None:
                    table_rows = [redactor(row) for row in table_rows]
                tables[table] = table_rows

        meal_ids = [item["id"] for item in tables.get("meals", [])]
        session_ids = [item["id"] for item in tables.get("sessions", [])]
        tables["meal_items"] = (
            rows(
                connection,
                f"SELECT * FROM meal_items WHERE meal_id IN ({','.join('?' * len(meal_ids))})",
                meal_ids,
            )
            if meal_ids
            else []
        )
        tables["sets"] = (
            rows(
                connection,
                f"SELECT * FROM sets WHERE session_id IN ({','.join('?' * len(session_ids))})",
                session_ids,
            )
            if session_ids
            else []
        )

        output.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("data.json", json.dumps(payload, ensure_ascii=False, indent=2))
            for meal in tables.get("meals", []):
                image = meal.get("image_path")
                if not image:
                    continue
                path = Path(image)
                if path.exists() and path.is_file() and not path.is_symlink():
                    archive.write(path, f"meal_images/{meal['id']}_{path.name}")
        return output
    finally:
        connection.close()


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("user_id", type=int)
    parser.add_argument("--db", default=os.getenv("DATABASE_PATH", "./data/noam_coach.db"))
    parser.add_argument("--output")
    args = parser.parse_args()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = Path(args.output or f"backups/user_{args.user_id}_{stamp}.zip")
    print(export_user(Path(args.db), args.user_id, output.resolve()))


if __name__ == "__main__":
    main()
