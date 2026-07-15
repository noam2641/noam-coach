"""Observability O1 — canonical event foundation acceptance tests.

Covers the batch acceptance criteria:
- legacy (pre-correlation) product_events rows remain readable and are
  explicitly identifiable as uncorrelated
- new rows carry queryable trace/interaction/span correlation
- the canonical redactor is recursive and rejects binary/base64 payloads
- observability modes (off/metadata/content/debug) behave as specified
- an event-write failure cannot break a representative coaching caller
- write degradation stays detectable (health counters + write_failed event)
- structured trace grouping primitives work
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import aiosqlite
import pytest

import db as db_module
import event_log
from db import Database
from helpers import utc_now
from noam_coach.observability import (
    ObservabilityMode,
    emit_event,
    interaction_scope,
    new_trace_id,
    observability_health,
    redact,
    reset_observability_health,
    set_mode,
    span_scope,
    trace_reader,
)
from noam_coach.observability.modes import reset_mode


@pytest.fixture
async def db(tmp_path: Path) -> Database:
    database = Database(str(tmp_path / "obs_o1.db"))
    await database.init()
    await database.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'Test',NULL,?)",
        (utc_now(),),
    )
    return database


@pytest.fixture(autouse=True)
def _obs_mode_isolation():
    """No test here may implicitly depend on the production default mode."""
    set_mode(ObservabilityMode.CONTENT)
    reset_observability_health()
    yield
    reset_mode()
    reset_observability_health()


# ---------------------------------------------------------------------------
# Backward compatibility: legacy rows and legacy databases
# ---------------------------------------------------------------------------

_LEGACY_PRODUCT_EVENTS_DDL = """
CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL);
CREATE TABLE users(id INTEGER PRIMARY KEY, first_name TEXT, username TEXT, updated_at TEXT);
CREATE TABLE product_events(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    event TEXT NOT NULL,
    entity TEXT NOT NULL DEFAULT 'system',
    entity_id TEXT,
    flow_id TEXT,
    flow_version INTEGER,
    source TEXT NOT NULL DEFAULT 'bot',
    properties TEXT NOT NULL DEFAULT '{}',
    before_state TEXT,
    after_state TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);
"""


@pytest.mark.asyncio
async def test_migration_13_upgrades_legacy_db_and_keeps_old_rows_readable(tmp_path: Path) -> None:
    """A real pre-O1 database (old product_events shape, migrations 1-12
    recorded) must upgrade through the REAL startup path ``Database.init()``
    — not by calling run_migrations() directly.

    Regression (production startup failure): the base SCHEMA briefly
    contained CREATE INDEX statements on migration-13 columns
    (idx_product_events_trace/interaction). ``init()`` runs
    ``executescript(SCHEMA)`` BEFORE ``run_migrations()``, and on an
    existing database ``CREATE TABLE IF NOT EXISTS`` keeps the old table
    shape — so startup aborted with "no such column: trace_id" before the
    migration could ever add the column. Base-SCHEMA indexes must never
    reference migration-added columns; the correlation indexes belong to
    migration 13.
    """
    path = tmp_path / "legacy.db"
    async with aiosqlite.connect(path) as conn:
        await conn.executescript(_LEGACY_PRODUCT_EVENTS_DDL)
        for version in range(1, 13):
            await conn.execute(
                "INSERT INTO schema_migrations(version, name, applied_at) VALUES(?,?,?)",
                (version, f"legacy_{version}", utc_now()),
            )
        await conn.execute(
            "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'Old',NULL,?)",
            (utc_now(),),
        )
        await conn.execute(
            "INSERT INTO product_events(user_id, event, entity, properties, created_at)"
            " VALUES(1,'meal_saved','meal','{\"calories\": 500}',?)",
            (utc_now(),),
        )
        await conn.commit()

    database = Database(str(path))
    # THE regression assertion: real startup on the legacy file succeeds.
    await database.init()

    rows = await database.fetch_all("PRAGMA table_info(product_events)")
    columns = {row["name"] for row in rows}
    assert {"trace_id", "interaction_id", "span_id", "parent_span_id",
            "event_version", "surface", "status", "outcome"} <= columns
    index_rows = await database.fetch_all(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='product_events'"
    )
    index_names = {row["name"] for row in index_rows}
    assert {"idx_product_events_trace", "idx_product_events_interaction"} <= index_names
    # Startup must be idempotent on the upgraded file too.
    await database.init()

    events = await event_log.list_events(database, 1)
    assert len(events) == 1
    legacy = events[0]
    assert legacy.event == "meal_saved"
    assert legacy.properties == {"calories": 500}
    assert legacy.trace_id is None
    assert legacy.is_legacy_uncorrelated is True
    assert legacy.event_version == 1

    # And the upgraded store accepts fully correlated new rows.
    row_id = await event_log.append_event(
        database, 1, "interaction.received",
        trace_id="tr_x", interaction_id="in_x", surface="telegram",
    )
    assert row_id > 0
    correlated = await event_log.list_events(database, 1, trace_id="tr_x")
    assert [e.event for e in correlated] == ["interaction.received"]
    assert correlated[0].is_legacy_uncorrelated is False


@pytest.mark.asyncio
async def test_migration_13_recovers_from_half_applied_state(db: Database) -> None:
    """Crash-recovery contract: if the columns already exist (fresh init
    applied the full new DDL, or a previous attempt crashed after the ALTERs
    but before recording) the guarded migration must complete cleanly."""
    await db.execute("DELETE FROM schema_migrations WHERE version=13")
    await db_module.run_migrations(db, backup_existing=False)
    rows = await db.fetch_all("PRAGMA table_info(product_events)")
    assert {"trace_id", "outcome"} <= {row["name"] for row in rows}
    recorded = await db.fetch_all("SELECT version FROM schema_migrations WHERE version=13")
    assert len(recorded) == 1
    # And a second full run is a pure no-op.
    await db_module.run_migrations(db, backup_existing=False)


@pytest.mark.asyncio
async def test_append_event_original_signature_still_works(db: Database) -> None:
    """The 40+ pre-existing call sites use only the original kwargs."""
    row_id = await event_log.append_event(
        db, 1, "flow_completed", entity="flow", flow_id="f1", flow_version=2,
        properties={"step": "done"},
    )
    assert row_id > 0
    events = await event_log.list_events(db, 1, event="flow_completed")
    assert events[0].flow_id == "f1"
    assert events[0].is_legacy_uncorrelated is True  # explicit, not inferred


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def test_redaction_is_recursive_across_nested_structures() -> None:
    payload = {
        "user_text": "מה לאכול עכשיו?",
        "headers": [("Authorization", "Bearer abc123def456")],
        "nested": {
            "level2": [
                {"api_key": "sk-abcdefghijklmnop1234", "keep": 1},
                ({"bot_token": "123456789:" + "A" * 35},),
            ],
        },
    }
    out = redact(payload)
    assert out["user_text"] == "מה לאכול עכשיו?"
    assert out["nested"]["level2"][0]["api_key"] == "[REDACTED]"
    assert out["nested"]["level2"][0]["keep"] == 1
    assert out["nested"]["level2"][1][0]["bot_token"] == "[REDACTED]"
    # Value-shape redaction inside a tuple->list converted structure.
    assert "Bearer" not in str(out["headers"])


def test_redaction_catches_secret_shapes_inside_free_text() -> None:
    text = "call with sk-ABCDEFGHIJKLMNOPQRST and Bearer eyJhbGciOi and 123456789:" + "B" * 35
    out = redact({"note": text})
    assert "sk-ABCDEFGHIJKLMNOPQRST" not in out["note"]
    assert "eyJhbGciOi" not in out["note"]
    assert "B" * 35 not in out["note"]


def test_redaction_rejects_binary_and_base64_image_payloads() -> None:
    out = redact(
        {
            "photo": b"\x89PNG raw bytes",
            "inline": "data:image/jpeg;base64," + "aGVsbG8=" * 8,
            "blob": "QUJD" * 200,  # 800 chars of pure base64
        }
    )
    assert out["photo"] == "[BINARY:14 bytes]"
    assert "base64," not in out["inline"]
    assert out["blob"].startswith("[REDACTED:base64")


def test_redaction_bounds_strings_depth_and_items() -> None:
    deep: Any = "leaf"
    for _ in range(20):
        deep = {"d": deep}
    out = redact(deep)
    assert "[MAX_DEPTH]" in str(out)
    long_text = redact("א" * 5000)
    assert long_text.endswith("[TRUNCATED 1000 chars]")
    many = redact(list(range(500)))
    assert many[-1] == "[TRUNCATED 300 more items]"


def test_redaction_does_not_over_redact_domain_words() -> None:
    out = redact({"author": "noam", "day_key": "2026-07-14", "protein": 150})
    assert out == {"author": "noam", "day_key": "2026-07-14", "protein": 150}


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mode_off_writes_nothing(db: Database) -> None:
    set_mode(ObservabilityMode.OFF)
    result = await emit_event(db, 1, "interaction.received", content={"text": "hi"})
    assert result is None
    assert await event_log.list_events(db, 1) == []


@pytest.mark.asyncio
async def test_mode_metadata_keeps_digests_but_never_content(db: Database) -> None:
    set_mode(ObservabilityMode.METADATA)
    await emit_event(
        db, 1, "interaction.received",
        properties={"kind": "text"},
        content={"text": "שלום, אכלתי פסטה"},
    )
    event = (await event_log.list_events(db, 1))[0]
    assert event.properties["kind"] == "text"
    assert "content" not in event.properties
    digest = event.properties["content_digest"]["text"]
    assert set(digest) == {"sha256", "chars"}
    assert digest["chars"] == len("שלום, אכלתי פסטה")
    assert "פסטה" not in str(event.properties)


@pytest.mark.asyncio
async def test_mode_content_retains_redacted_content_and_digest(db: Database) -> None:
    set_mode(ObservabilityMode.CONTENT)
    await emit_event(
        db, 1, "interaction.received",
        content={"text": "הסיסמה sk-SHOULDNEVERAPPEAR1234 בפנים"},
    )
    event = (await event_log.list_events(db, 1))[0]
    assert "content" in event.properties
    assert "sk-SHOULDNEVERAPPEAR1234" not in str(event.properties)
    assert "בפנים" in event.properties["content"]["text"]
    assert "content_digest" in event.properties  # metadata queries still work


@pytest.mark.asyncio
async def test_mode_debug_still_redacts_secrets_and_binary(db: Database) -> None:
    set_mode(ObservabilityMode.DEBUG)
    await emit_event(
        db, 1, "ai.call.started",
        content={"request": {"image_bytes": b"12345", "prompt": "analyze"}},
    )
    event = (await event_log.list_events(db, 1))[0]
    request = event.properties["content"]["request"]
    assert request["image_bytes"] == "[REDACTED]"
    assert request["prompt"] == "analyze"


# ---------------------------------------------------------------------------
# Safe write boundary + detectable degradation
# ---------------------------------------------------------------------------


class _FlakyDB:
    """DB double whose first ``fail_times`` executes raise."""

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.calls = 0
        self.written: list[tuple[str, tuple]] = []

    async def execute(self, sql: str, parameters: tuple = ()) -> int:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("simulated disk I/O error")
        self.written.append((sql, parameters))
        return self.calls


@pytest.mark.asyncio
async def test_event_write_failure_does_not_break_a_representative_caller() -> None:
    flaky = _FlakyDB(fail_times=99)

    async def coaching_step() -> str:
        # Representative caller: instrumentation first, then user-facing work.
        await emit_event(flaky, 1, "interaction.received", content={"text": "hi"})
        return "coaching-ok"

    assert await coaching_step() == "coaching-ok"
    health = observability_health()
    assert health["write_failures"] == 1
    assert health["last_failed_event"] == "interaction.received"
    assert "RuntimeError" in health["last_error"]


@pytest.mark.asyncio
async def test_write_degradation_emits_write_failed_event_when_store_recovers() -> None:
    flaky = _FlakyDB(fail_times=1)
    await emit_event(flaky, 1, "routing.decided")
    health = observability_health()
    assert health["write_failures"] == 1
    assert health["write_failed_events_written"] == 1
    # The best-effort degradation marker actually reached the store.
    assert any("observability.write_failed" in str(params) for _sql, params in flaky.written)


@pytest.mark.asyncio
async def test_total_store_failure_cannot_recurse() -> None:
    flaky = _FlakyDB(fail_times=99)
    await emit_event(flaky, 1, "routing.decided")
    health = observability_health()
    assert health["write_failures"] == 1
    assert health["write_failed_event_failures"] == 1
    assert flaky.calls == 2  # one payload attempt + one marker attempt, no loop


# ---------------------------------------------------------------------------
# Correlation context + structured trace grouping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ambient_interaction_scope_correlates_events(db: Database) -> None:
    with interaction_scope() as scope:
        await emit_event(db, 1, "interaction.received", surface="telegram")
        with span_scope() as routing_span:
            await emit_event(db, 1, "routing.decided")
            with span_scope() as ai_span:
                await emit_event(db, 1, "ai.call.started")
    events = await trace_reader.events_for_interaction(db, 1, scope.interaction_id)
    assert [e.event for e in events] == [
        "interaction.received", "routing.decided", "ai.call.started",
    ]
    assert {e.trace_id for e in events} == {scope.trace_id}
    routing_event = events[1]
    ai_event = events[2]
    assert routing_event.span_id == routing_span.span_id
    assert routing_event.parent_span_id is None
    assert ai_event.span_id == ai_span.span_id
    assert ai_event.parent_span_id == routing_span.span_id


@pytest.mark.asyncio
async def test_trace_grouping_separates_traces_and_legacy_rows(db: Database) -> None:
    trace_a = new_trace_id()
    with interaction_scope(trace_id=trace_a):
        await emit_event(db, 1, "interaction.received")
        await emit_event(db, 1, "routing.decided")
    with interaction_scope():
        await emit_event(db, 1, "interaction.received")
    # A legacy-style direct write with no correlation at all.
    await event_log.append_event(db, 1, "meal_saved", entity="meal")

    events = await trace_reader.recent_events(db, 1)
    groups = trace_reader.group_by_trace(events)
    assert len(groups) == 3  # trace A, the fresh trace, and the explicit None group
    assert [e.event for e in groups[trace_a]] == ["interaction.received", "routing.decided"]
    assert [e.event for e in groups[None]] == ["meal_saved"]
    assert [e.event for e in trace_reader.legacy_events(events)] == ["meal_saved"]

    by_interaction = trace_reader.group_by_interaction(events)
    interaction_ids = [key for key in by_interaction if key is not None]
    assert len(interaction_ids) == 2


@pytest.mark.asyncio
async def test_trace_continuation_reuses_provided_trace_id(db: Database) -> None:
    """A callback continuing a journey passes the stored trace id: both
    interactions correlate to one trace while keeping distinct interaction ids."""
    with interaction_scope() as first:
        await emit_event(db, 1, "interaction.received")
    with interaction_scope(trace_id=first.trace_id) as second:
        await emit_event(db, 1, "interaction.received")
    assert first.trace_id == second.trace_id
    assert first.interaction_id != second.interaction_id
    events = await trace_reader.events_for_trace(db, 1, first.trace_id)
    assert len(events) == 2


@pytest.mark.asyncio
async def test_replay_summary_still_renders(db: Database) -> None:
    await emit_event(db, 1, "interaction.received", content={"text": "hello"})
    summary = await event_log.replay_summary(db, 1)
    assert "interaction.received" in summary
