"""LOG-017 — `analytics_events` must never ship raw in the DSAR export.

`track_event(**properties)` writes an open-ended JSON blob with no allowlist
(unlike the LOG-012 audit path), and `analytics_events` IS in
`DIRECT_TABLES`, so the DSAR ZIP shipped whatever a call site happened to
pass: raw callback data, free text, and any secret a future dev adds.

The owner decision is to PRESERVE the user's own data rather than drop the
stream, so redaction happens at EXPORT time only — stored rows are never
rewritten and the schema is unchanged.

These tests pin the contract against the FINAL GENERATED ARTIFACT (the ZIP),
not just the helper:
  - nested sensitive fields (dict-in-dict, list-of-dicts) are redacted;
  - unbounded free text is dropped;
  - secret-like KEYS and secret-like VALUES are redacted;
  - malformed JSON fails CLOSED (never passes through);
  - safe bounded fields survive;
  - cross-user isolation — another user's rows never appear.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import zipfile
from pathlib import Path

import coach_bot
from scripts.export_user_data import (
    DROPPED_FREE_TEXT,
    REDACTED,
    UNPARSEABLE,
    export_user,
    redact_analytics_row,
)

USER_ID = 1
OTHER_USER_ID = 2

# Deliberately identifiable strings asserted never to reach the export.
FREE_TEXT = (
    "I have been feeling anxious about my weight since my doctor mentioned "
    "my blood pressure was elevated at the last appointment in March"
)
OPENAI_KEY = "sk-proj-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
TELEGRAM_TOKEN = "123456789:AAEHVBAqPGgLMHOKfYQNQuXKBRuVwXhzabc"
PASSWORD = "hunter2-correct-horse"
OTHER_USER_SECRET = "other-user-private-note-do-not-leak"


def _seed(db_path: Path) -> None:
    """Create a DB and insert analytics rows directly (raw, as stored today)."""

    async def _init() -> None:
        db = coach_bot.Database(str(db_path))
        await db.init()

    asyncio.run(_init())

    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA foreign_keys=ON")
    now = coach_bot.utc_now()
    for uid in (USER_ID, OTHER_USER_ID):
        connection.execute(
            "INSERT INTO users(id, first_name, username, updated_at) VALUES(?,'N',NULL,?)",
            (uid, now),
        )

    def add(uid: int, event: str, properties: str) -> None:
        connection.execute(
            "INSERT INTO analytics_events(user_id,event,properties,created_at) "
            "VALUES(?,?,?,?)",
            (uid, event, properties, now),
        )

    # 1. Safe bounded telemetry that MUST survive (the user's own data).
    add(USER_ID, "command_weekly", json.dumps({"count": 4, "ok": True, "source": "local_path"}))
    # 2. Secret-like VALUES under innocuous keys.
    add(USER_ID, "user_callback", json.dumps({"data": OPENAI_KEY, "note": TELEGRAM_TOKEN}))
    # 3. Secret-like KEYS.
    add(USER_ID, "login", json.dumps({"password": PASSWORD, "api_key": "abc123", "step": 2}))
    # 4. Unbounded free text.
    add(USER_ID, "USER_MESSAGE", json.dumps({"text": FREE_TEXT, "text_length": 137}))
    # 5. Nested: dict-in-dict and list-of-dicts.
    add(
        USER_ID,
        "nested_event",
        json.dumps(
            {
                "outer": {"inner": {"authorization": "Bearer abcdefghijklmnop", "depth": 3}},
                "items": [{"secret": PASSWORD, "idx": 0}, {"idx": 1, "note": FREE_TEXT}],
            }
        ),
    )
    # 6. SERIALIZED JSON nested inside a string field.
    add(
        USER_ID,
        "serialized_event",
        json.dumps({"payload": json.dumps({"token": TELEGRAM_TOKEN, "kind": "tap"})}),
    )
    # 7. MALFORMED payloads — must fail closed, not pass through.
    add(USER_ID, "malformed_truncated", '{"token": "' + OPENAI_KEY)
    add(USER_ID, "malformed_garbage", "not json at all " + PASSWORD)
    # 8. Another user's row — must never appear in USER_ID's export.
    add(OTHER_USER_ID, "other_event", json.dumps({"note": OTHER_USER_SECRET}))

    connection.commit()
    connection.close()


def _export_payload(tmp_path: Path) -> tuple[dict, str]:
    """Run the real exporter and return (parsed payload, raw data.json text)."""
    db_path = tmp_path / "coach.db"
    _seed(db_path)
    archive = export_user(db_path, USER_ID, tmp_path / "export.zip")
    with zipfile.ZipFile(archive) as exported:
        blob = exported.read("data.json").decode("utf-8")
    return json.loads(blob), blob


def test_dsar_zip_contains_no_raw_secret_or_free_text(tmp_path: Path) -> None:
    """The end-to-end artifact carries none of the seeded sensitive strings."""
    _payload, blob = _export_payload(tmp_path)
    for leak in (
        FREE_TEXT,
        OPENAI_KEY,
        TELEGRAM_TOKEN,
        PASSWORD,
        OTHER_USER_SECRET,
    ):
        assert leak not in blob, f"raw value leaked into DSAR export: {leak[:24]!r}…"


def test_safe_bounded_fields_are_preserved(tmp_path: Path) -> None:
    """This is the user's own data — bounded telemetry must still be there."""
    payload, _blob = _export_payload(tmp_path)
    events = payload["tables"]["analytics_events"]
    by_event = {row["event"]: row["properties"] for row in events}

    assert by_event["command_weekly"] == {"count": 4, "ok": True, "source": "local_path"}
    # Bounded companions of dropped free text survive alongside it.
    assert by_event["USER_MESSAGE"]["text_length"] == 137
    # A bounded scalar sitting next to secret-like keys is kept.
    assert by_event["login"]["step"] == 2
    # Row metadata (the user's own) is preserved.
    assert all(row["user_id"] == USER_ID for row in events)
    assert all(row["created_at"] for row in events)


def test_secret_like_keys_and_values_are_redacted(tmp_path: Path) -> None:
    payload, _blob = _export_payload(tmp_path)
    by_event = {r["event"]: r["properties"] for r in payload["tables"]["analytics_events"]}

    # Secret-like KEYS: value gone regardless of content.
    assert by_event["login"]["password"] == REDACTED
    assert by_event["login"]["api_key"] == REDACTED
    # Secret-like VALUES under innocuous keys.
    assert by_event["user_callback"]["data"] == REDACTED
    assert by_event["user_callback"]["note"] == REDACTED


def test_unbounded_free_text_is_dropped(tmp_path: Path) -> None:
    payload, _blob = _export_payload(tmp_path)
    by_event = {r["event"]: r["properties"] for r in payload["tables"]["analytics_events"]}
    assert by_event["USER_MESSAGE"]["text"] == DROPPED_FREE_TEXT


def test_nested_dicts_and_lists_are_redacted_recursively(tmp_path: Path) -> None:
    payload, _blob = _export_payload(tmp_path)
    by_event = {r["event"]: r["properties"] for r in payload["tables"]["analytics_events"]}
    nested = by_event["nested_event"]

    # dict-in-dict: sensitive key redacted, bounded sibling preserved.
    inner = nested["outer"]["inner"]
    assert inner["authorization"] == REDACTED
    assert inner["depth"] == 3
    # list-of-dicts: each element walked.
    assert nested["items"][0]["secret"] == REDACTED
    assert nested["items"][0]["idx"] == 0
    assert nested["items"][1]["idx"] == 1
    assert nested["items"][1]["note"] == DROPPED_FREE_TEXT


def test_serialized_json_string_is_parsed_and_redacted(tmp_path: Path) -> None:
    """A JSON document inside a string field must not pass through opaque."""
    payload, _blob = _export_payload(tmp_path)
    by_event = {r["event"]: r["properties"] for r in payload["tables"]["analytics_events"]}
    inner = by_event["serialized_event"]["payload"]

    # Parsed into structure, not left as a raw string.
    assert isinstance(inner, dict), f"serialized JSON left unparsed: {inner!r}"
    assert inner["token"] == REDACTED
    assert inner["kind"] == "tap"  # bounded field inside survives


def test_malformed_payloads_fail_closed(tmp_path: Path) -> None:
    """Unsafe content must NOT pass through merely because parsing failed."""
    payload, _blob = _export_payload(tmp_path)
    by_event = {r["event"]: r["properties"] for r in payload["tables"]["analytics_events"]}

    # Truncated JSON that starts like a container: redacted wholesale.
    assert by_event["malformed_truncated"] == UNPARSEABLE
    # Non-JSON garbage stored where JSON was expected: also redacted.
    assert by_event["malformed_garbage"] == UNPARSEABLE


def test_cross_user_isolation(tmp_path: Path) -> None:
    """Only the requesting user's analytics rows appear in their export."""
    payload, blob = _export_payload(tmp_path)
    events = payload["tables"]["analytics_events"]

    assert events, "expected the requesting user's own analytics rows"
    assert {row["user_id"] for row in events} == {USER_ID}
    assert "other_event" not in {row["event"] for row in events}
    assert OTHER_USER_SECRET not in blob


def test_helper_never_mutates_the_stored_row(tmp_path: Path) -> None:
    """Redaction is export-time only: the input row object is not modified."""
    raw = {
        "id": 1,
        "user_id": USER_ID,
        "event": "login",
        "properties": json.dumps({"password": PASSWORD}),
        "created_at": "2026-01-01T00:00:00Z",
    }
    original = dict(raw)
    safe = redact_analytics_row(raw)

    assert raw == original, "redactor mutated the caller's row"
    assert safe["properties"]["password"] == REDACTED


def test_unexpected_property_shapes_fail_closed() -> None:
    """Non-container stored payloads are still bounded and inspected."""
    # A bare JSON scalar string (valid JSON, not a container).
    safe = redact_analytics_row(
        {"user_id": USER_ID, "event": "e", "properties": json.dumps(FREE_TEXT)}
    )
    assert safe["properties"] == DROPPED_FREE_TEXT

    # NULL properties are left alone rather than crashing the export.
    safe_null = redact_analytics_row({"user_id": USER_ID, "event": "e", "properties": None})
    assert safe_null["properties"] is None

    # Raw bytes stored in the column never ship.
    safe_bytes = redact_analytics_row(
        {"user_id": USER_ID, "event": "e", "properties": b"\x00\x01binary"}
    )
    assert safe_bytes["properties"] == UNPARSEABLE
