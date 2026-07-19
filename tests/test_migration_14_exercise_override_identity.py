"""Batch 2 (workout-selection architecture, review/workout-selection-architecture):
deployment-safety tests for Migration 14 (exercise_override_identity) and the
new optional exercise_id write path on set_exercise_override.

Storage/write plumbing only -- no production read path applies overrides by
exercise_id yet (that starts in a later batch). These tests exist to prove
the W3 boot-order risk documented in the architecture plan
(C:\\Users\\user\\.claude\\plans\\sprightly-sauteeing-parrot.md, section I)
cannot recur: the new idx_exercise_overrides_identity index must never live
in base SCHEMA (which runs via executescript() BEFORE run_migrations()), or
every existing v13-shaped production database would fail to boot with
"no such column" the moment it upgrades.
"""
from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

import coach_bot
from db import Database
from helpers import utc_now
from noam_coach.services import profile as profile_services


async def _seed_v13_shaped_db(path: Path) -> None:
    """Hand-craft a database that predates Migration 14: schema_migrations
    seeded 1-13 only, and exercise_overrides in its PRE-migration-14 shape
    (no exercise_id column). Every other table is intentionally NOT
    pre-created here -- Database.init()'s own executescript(SCHEMA) creates
    them via CREATE TABLE IF NOT EXISTS exactly as it would on any real
    v13 database being upgraded, so this is a faithful simulation, not a
    stripped-down fixture.
    """
    connection = await aiosqlite.connect(path)
    try:
        await connection.executescript(
            """
            CREATE TABLE schema_migrations(
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL
            );
            INSERT INTO schema_migrations(version, name, applied_at)
            VALUES
                (1, 'single_active_session', '2026-01-01'),
                (2, 'job_delivery_state', '2026-01-01'),
                (3, 'foreign_keys_and_health_identity', '2026-01-01'),
                (4, 'active_flow_table', '2026-01-01'),
                (5, 'goal_versions_table', '2026-01-01'),
                (6, 'planning_and_event_tables', '2026-01-01'),
                (7, 'active_flow_expiry', '2026-01-01'),
                (8, 'meal_origin', '2026-01-01'),
                (9, 'single_active_goal_version', '2026-01-01'),
                (10, 'clean_polluted_gap_values', '2026-01-01'),
                (11, 'meal_status_column', '2026-01-01'),
                (12, 'daily_flags_revision', '2026-01-01'),
                (13, 'observability_correlation', '2026-01-01');

            CREATE TABLE users(
                id INTEGER PRIMARY KEY,
                first_name TEXT,
                username TEXT,
                updated_at TEXT NOT NULL
            );
            INSERT INTO users(id, first_name, username, updated_at)
            VALUES(1, 'A', NULL, '2026-01-01');

            CREATE TABLE exercise_overrides(
                user_id INTEGER NOT NULL,
                code TEXT NOT NULL,
                exercise_index INTEGER NOT NULL,
                field TEXT NOT NULL,
                value REAL NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(user_id, code, exercise_index, field),
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            INSERT INTO exercise_overrides(user_id, code, exercise_index, field, value, updated_at)
            VALUES(1, 'A', 0, 'weight', 55.0, '2026-01-01T00:00:00+00:00');
            """
        )
        await connection.commit()
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_fresh_database_has_exercise_id_column_and_identity_index(
    tmp_path: Path,
) -> None:
    """A brand-new database (base SCHEMA, no upgrade path) already has the
    nullable exercise_id column (added to the CREATE TABLE IF NOT EXISTS
    definition for fresh DBs) and migration 14 has run and created the
    index.
    """
    db = Database(str(tmp_path / "fresh.db"))
    await db.init()

    cursor_columns = await db.fetch_all("PRAGMA table_info(exercise_overrides)")
    column_names = {row["name"] for row in cursor_columns}
    assert "exercise_id" in column_names

    indexes = await db.fetch_all(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='exercise_overrides'"
    )
    index_names = {row["name"] for row in indexes}
    assert "idx_exercise_overrides_identity" in index_names

    migrations = await db.fetch_all("SELECT version FROM schema_migrations ORDER BY version")
    assert 14 in {row["version"] for row in migrations}

    # The primary key is untouched (no PK rebuild in this batch, per plan §I).
    pk_columns = [row["name"] for row in cursor_columns if row["pk"]]
    assert pk_columns == ["user_id", "code", "exercise_index", "field"]


@pytest.mark.asyncio
async def test_v13_shaped_database_upgrades_successfully(tmp_path: Path) -> None:
    """The exact upgrade scenario Migration 14 exists for: a database that
    only has migrations 1-13 applied and exercise_overrides in its old
    shape (no exercise_id column) must gain the column and the index, and
    must NOT lose its existing override row.
    """
    path = tmp_path / "v13_upgrade.db"
    await _seed_v13_shaped_db(path)

    db = Database(str(path))
    await db.init()

    cursor_columns = await db.fetch_all("PRAGMA table_info(exercise_overrides)")
    column_names = {row["name"] for row in cursor_columns}
    assert "exercise_id" in column_names

    indexes = await db.fetch_all(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='exercise_overrides'"
    )
    assert "idx_exercise_overrides_identity" in {row["name"] for row in indexes}

    # The pre-existing override row survived the upgrade, with a NULL
    # exercise_id (never speculatively backfilled, per plan §I).
    row = await db.fetch_one(
        "SELECT * FROM exercise_overrides WHERE user_id=1 AND code='A' AND exercise_index=0 AND field='weight'"
    )
    assert row is not None
    assert row["value"] == 55.0
    assert row["exercise_id"] is None

    migrations = await db.fetch_all("SELECT version FROM schema_migrations ORDER BY version")
    assert [row["version"] for row in migrations] == list(range(1, 15))


@pytest.mark.asyncio
async def test_migration_14_is_idempotent_when_run_twice(tmp_path: Path) -> None:
    """Running Migration 14 twice -- once as part of the real upgrade path,
    once more via a second full init() on the now-current database -- must
    not raise and must not duplicate the column or the index.

    This follows the repo's own established idempotency-testing convention
    (tests/test_database.py::test_database_initialization_is_idempotent):
    idempotency is proven through two Database.init() calls, which is the
    only sanctioned entry point. run_migrations() tracks applied versions
    in schema_migrations and skips any version already recorded there
    before ever invoking its migration function again -- _record_migration
    itself is a plain INSERT with no ON CONFLICT guard (matching every
    other migration in this file), because it is only ever called from
    inside that already-applied check, never called twice for the same
    version by design.
    """
    path = tmp_path / "double_migrate.db"
    await _seed_v13_shaped_db(path)

    db = Database(str(path))
    await db.init()
    # Second full init() on an already-migrated database: run_migrations()
    # must see migration 14 as already applied (via schema_migrations) and
    # skip it entirely -- proving the upgrade path is safe to run twice.
    await db.init()

    cursor_columns = await db.fetch_all("PRAGMA table_info(exercise_overrides)")
    exercise_id_columns = [row for row in cursor_columns if row["name"] == "exercise_id"]
    assert len(exercise_id_columns) == 1

    indexes = await db.fetch_all(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='exercise_overrides' "
        "AND name='idx_exercise_overrides_identity'"
    )
    assert len(indexes) == 1

    migration_rows = await db.fetch_all(
        "SELECT version FROM schema_migrations WHERE version=14"
    )
    assert len(migration_rows) == 1

    # The pre-existing override row from the v13-shaped seed is still
    # exactly one row, undisturbed by the double init().
    override_rows = await db.fetch_all(
        "SELECT * FROM exercise_overrides WHERE user_id=1 AND code='A' AND exercise_index=0 AND field='weight'"
    )
    assert len(override_rows) == 1
    assert override_rows[0]["value"] == 55.0


@pytest.mark.asyncio
async def test_v13_database_completes_full_init_without_no_such_column_failure(
    tmp_path: Path,
) -> None:
    """The W3 regression, verbatim: boot a v13-shaped database all the way
    through Database.init() (executescript(SCHEMA) then run_migrations())
    and confirm it completes cleanly -- proving the identity index was
    never placed in base SCHEMA (which would abort startup with
    "no such column" before migration 14 gets the chance to add it, since
    executescript(SCHEMA) always runs first).
    """
    path = tmp_path / "v13_full_init.db"
    await _seed_v13_shaped_db(path)

    db = Database(str(path))
    # No exception -- this is the assertion. A regression (index in base
    # SCHEMA) would raise aiosqlite.OperationalError: no such column:
    # exercise_id, raised from inside executescript(SCHEMA) before
    # run_migrations() ever runs.
    await db.init()

    # And the database is fully usable afterwards.
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(2,'B',NULL,?)",
        (utc_now(),),
    )
    row = await db.fetch_one("SELECT id FROM users WHERE id=2")
    assert row is not None


# ---------------------------------------------------------------------------
# set_exercise_override: backward-compatible optional exercise_id kwarg.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_exercise_override_positional_call_still_works_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every existing caller invokes set_exercise_override positionally
    with exactly 5 args (user_id, code, exercise_index, field, value) --
    this must keep working byte-for-byte, writing a NULL exercise_id."""
    db = Database(str(tmp_path / "positional.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (utc_now(),),
    )
    monkeypatch.setattr(coach_bot, "DB", db)

    await profile_services.set_exercise_override(1, "A", 0, "weight", 100.0)

    row = await db.fetch_one(
        "SELECT * FROM exercise_overrides WHERE user_id=1 AND code='A' AND exercise_index=0 AND field='weight'"
    )
    assert row is not None
    assert row["value"] == 100.0
    assert row["exercise_id"] is None


@pytest.mark.asyncio
async def test_set_exercise_override_accepts_optional_exercise_id_kwarg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """New writes MAY populate exercise_id via the optional keyword-only
    parameter. This batch only proves the plumbing stores it -- no
    production read path applies overrides by exercise_id yet."""
    db = Database(str(tmp_path / "with_id.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (utc_now(),),
    )
    monkeypatch.setattr(coach_bot, "DB", db)

    await profile_services.set_exercise_override(1, "A", 0, "weight", 100.0, exercise_id="bench")

    row = await db.fetch_one(
        "SELECT * FROM exercise_overrides WHERE user_id=1 AND code='A' AND exercise_index=0 AND field='weight'"
    )
    assert row is not None
    assert row["exercise_id"] == "bench"


@pytest.mark.asyncio
async def test_set_exercise_override_upsert_does_not_erase_existing_exercise_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A later positional-only call (no exercise_id passed) updating the
    SAME override must not null out an exercise_id recorded by an earlier
    call -- COALESCE keeps the previously-recorded identity on conflict."""
    db = Database(str(tmp_path / "coalesce.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (utc_now(),),
    )
    monkeypatch.setattr(coach_bot, "DB", db)

    await profile_services.set_exercise_override(1, "A", 0, "weight", 100.0, exercise_id="bench")
    # Legacy-style call: no exercise_id kwarg.
    await profile_services.set_exercise_override(1, "A", 0, "weight", 105.0)

    row = await db.fetch_one(
        "SELECT * FROM exercise_overrides WHERE user_id=1 AND code='A' AND exercise_index=0 AND field='weight'"
    )
    assert row is not None
    assert row["value"] == 105.0
    assert row["exercise_id"] == "bench"


@pytest.mark.asyncio
async def test_get_user_plan_behavior_unchanged_by_migration_14(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """get_user_plan's SELECT/apply logic is untouched by this batch (it
    still reads only exercise_index/field/value) -- overrides, including
    ones written with an exercise_id, are applied exactly as before, by
    position only."""
    db = Database(str(tmp_path / "get_user_plan.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (utc_now(),),
    )
    monkeypatch.setattr(coach_bot, "DB", db)

    await profile_services.set_exercise_override(1, "A", 0, "weight", 100.0, exercise_id="bench")
    plan = await profile_services.get_user_plan(1, "A")
    assert plan["exercises"][0]["weight"] == 100.0
    # Untouched exercises are unaffected.
    from exercise_plans import PLANS

    assert plan["exercises"][1]["weight"] == PLANS["A"]["exercises"][1]["weight"]
