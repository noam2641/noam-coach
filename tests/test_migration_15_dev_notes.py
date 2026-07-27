"""Deployment-safety tests for Migration 15 (dev_notes).

Same class of risk as Migration 14's suite: a migration that is fine on a
fresh database and fatal on a real one.

Migration 15 shipped recording the WRONG version -- it wrote both 15 and 14
into schema_migrations, because the `_record_migration` call was inserted
into the wrong function body. On a fresh database every migration runs in one
pass and nothing collides visibly. On a database that already has 14 -- which
is every existing installation -- re-inserting 14 violates the
schema_migrations primary key and **Database.init() raises, so the bot cannot
boot**.

Nothing in the existing suite caught it: the fresh-database tests pass, and
the v13-shaped test upgrades a database that does not have 14 yet. Only
upgrading a genuinely v14-shaped database exposes it, which is precisely what
a deployed installation does.
"""

from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from db import SCHEMA_MIGRATIONS, Database
from helpers import utc_now


async def _seed_v14_shaped_db(path: Path) -> None:
    """Hand-craft a database that predates Migration 15.

    schema_migrations is seeded 1-14 and nothing else is pre-created:
    Database.init()'s own executescript(SCHEMA) builds the rest through
    CREATE TABLE IF NOT EXISTS exactly as it would on a real installation,
    so this is a faithful simulation rather than a stripped-down fixture.
    """
    connection = await aiosqlite.connect(path)
    try:
        rows = ",\n                ".join(
            f"({version}, '{name}', '2026-01-01')"
            for version, name in SCHEMA_MIGRATIONS
            if version <= 14
        )
        await connection.executescript(
            f"""
            CREATE TABLE schema_migrations(
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL
            );
            INSERT INTO schema_migrations(version, name, applied_at)
            VALUES
                {rows};
            """
        )
        await connection.commit()
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_v14_shaped_database_upgrades_without_crashing(tmp_path: Path) -> None:
    """The regression: init() raised IntegrityError on every existing install."""
    path = tmp_path / "v14_upgrade.db"
    await _seed_v14_shaped_db(path)

    db = Database(str(path))
    await db.init()  # must not raise

    rows = await db.fetch_all("SELECT version FROM schema_migrations ORDER BY version")
    assert [row["version"] for row in rows] == [v for v, _ in SCHEMA_MIGRATIONS]


@pytest.mark.asyncio
async def test_v14_upgrade_records_each_version_exactly_once(
    tmp_path: Path,
) -> None:
    """A migration must record its own version and no other."""
    path = tmp_path / "v14_once.db"
    await _seed_v14_shaped_db(path)

    db = Database(str(path))
    await db.init()

    rows = await db.fetch_all("SELECT version, name FROM schema_migrations")
    versions = [row["version"] for row in rows]
    assert len(versions) == len(set(versions)), "a version was recorded twice"

    registry = dict(SCHEMA_MIGRATIONS)
    for row in rows:
        assert row["name"] == registry[row["version"]], (
            f"version {row['version']} recorded under the wrong name "
            f"({row['name']!r}) -- a migration recorded someone else's version"
        )


@pytest.mark.asyncio
async def test_v14_upgrade_creates_dev_notes(tmp_path: Path) -> None:
    path = tmp_path / "v14_table.db"
    await _seed_v14_shaped_db(path)

    db = Database(str(path))
    await db.init()

    rows = await db.fetch_all(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='dev_notes'"
    )
    assert rows, "dev_notes was not created by the upgrade"


@pytest.mark.asyncio
async def test_v14_upgrade_is_idempotent(tmp_path: Path) -> None:
    """A second boot must be a no-op, not a second insert."""
    path = tmp_path / "v14_twice.db"
    await _seed_v14_shaped_db(path)

    db = Database(str(path))
    await db.init()
    await db.init()

    rows = await db.fetch_all("SELECT version FROM schema_migrations ORDER BY version")
    assert [row["version"] for row in rows] == [v for v, _ in SCHEMA_MIGRATIONS]


@pytest.mark.asyncio
async def test_existing_data_survives_the_upgrade(tmp_path: Path) -> None:
    """An upgrade must not disturb rows that were already there."""
    path = tmp_path / "v14_data.db"
    await _seed_v14_shaped_db(path)

    db = Database(str(path))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1, 'Noam', NULL, ?)",
        (utc_now(),),
    )
    await db.execute(
        """
        INSERT INTO product_events(user_id, event, entity, properties, created_at)
        VALUES(1, 'interaction.received', 'system', '{}', ?)
        """,
        (utc_now(),),
    )

    await db.init()  # simulate the next boot

    events = await db.fetch_all("SELECT COUNT(*) AS c FROM product_events")
    users = await db.fetch_all("SELECT COUNT(*) AS c FROM users")
    assert events[0]["c"] == 1
    assert users[0]["c"] == 1


@pytest.mark.asyncio
async def test_every_registered_migration_records_its_own_version(
    tmp_path: Path,
) -> None:
    """Generalised guard: run the whole chain and check the mapping.

    This is the assertion that would have caught the defect regardless of
    which migration made the mistake.
    """
    db = Database(str(tmp_path / "chain.db"))
    await db.init()

    rows = await db.fetch_all("SELECT version, name FROM schema_migrations")
    recorded = {row["version"]: row["name"] for row in rows}

    assert recorded == dict(SCHEMA_MIGRATIONS)
