from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

import coach_bot


@pytest.mark.asyncio
async def test_fresh_database_has_foreign_keys_and_single_active_session(
    tmp_path: Path,
) -> None:
    db = coach_bot.Database(str(tmp_path / "coach.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (coach_bot.utc_now(),),
    )
    plan = '{"exercises": []}'
    await db.execute(
        """
        INSERT INTO sessions(
            user_id, code, name, plan, status, exercise_index, set_number, started_at
        ) VALUES(1,'A','A',?,'active',0,1,?)
        """,
        (plan, coach_bot.utc_now()),
    )
    with pytest.raises(aiosqlite.IntegrityError):
        await db.execute(
            """
            INSERT INTO sessions(
                user_id, code, name, plan, status, exercise_index, set_number, started_at
            ) VALUES(1,'B','B',?,'active',0,1,?)
            """,
            (plan, coach_bot.utc_now()),
        )

    await db.execute("DELETE FROM users WHERE id=1")
    assert await db.fetch_one("SELECT id FROM sessions LIMIT 1") is None


@pytest.mark.asyncio
async def test_transaction_rolls_back_all_changes(tmp_path: Path) -> None:
    db = coach_bot.Database(str(tmp_path / "coach.db"))
    await db.init()
    with pytest.raises(RuntimeError):
        async with db.transaction() as connection:
            await connection.execute(
                "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
                (coach_bot.utc_now(),),
            )
            raise RuntimeError("boom")
    assert await db.fetch_one("SELECT id FROM users WHERE id=1") is None


@pytest.mark.asyncio
async def test_transaction_body_is_not_replayed(tmp_path: Path) -> None:
    db = coach_bot.Database(str(tmp_path / "coach.db"))
    await db.init()
    runs = 0
    with pytest.raises(RuntimeError):
        async with db.transaction():
            runs += 1
            raise RuntimeError("body failure")
    assert runs == 1


@pytest.mark.asyncio
async def test_legacy_migration_refuses_to_drop_orphans(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    connection = await aiosqlite.connect(path)
    try:
        await connection.executescript(
            """
            CREATE TABLE users(
                id INTEGER PRIMARY KEY,
                first_name TEXT,
                username TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE meals(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                calories REAL NOT NULL,
                protein REAL NOT NULL,
                carbs REAL NOT NULL,
                fat REAL NOT NULL,
                confidence REAL NOT NULL,
                image_path TEXT,
                eaten_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            INSERT INTO meals(
                user_id,name,calories,protein,carbs,fat,confidence,eaten_at,created_at
            ) VALUES(999,'orphan',1,1,1,1,1,'2026-01-01','2026-01-01');
            """
        )
        await connection.commit()
    finally:
        await connection.close()

    db = coach_bot.Database(str(path))
    with pytest.raises(RuntimeError, match="orphan row"):
        await db.init()
    connection = await aiosqlite.connect(path)
    try:
        row = await (await connection.execute("SELECT COUNT(*) FROM meals")).fetchone()
        assert row[0] == 1
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_database_initialization_is_idempotent(tmp_path: Path) -> None:
    db = coach_bot.Database(str(tmp_path / "coach.db"))
    await db.init()
    await db.init()
    rows = await db.fetch_all("SELECT version FROM schema_migrations ORDER BY version")
    assert [row["version"] for row in rows] == list(range(1, 9))


@pytest.mark.asyncio
async def test_partial_foreign_key_schema_is_fully_rebuilt(tmp_path: Path) -> None:
    path = tmp_path / "partial.db"
    connection = await aiosqlite.connect(path)
    try:
        await connection.executescript(
            """
            PRAGMA foreign_keys=ON;
            CREATE TABLE users(
                id INTEGER PRIMARY KEY,
                first_name TEXT,
                username TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE meals(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                calories REAL NOT NULL,
                protein REAL NOT NULL,
                carbs REAL NOT NULL,
                fat REAL NOT NULL,
                confidence REAL NOT NULL,
                image_path TEXT,
                eaten_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE health(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                external_id TEXT NOT NULL,
                sample_type TEXT NOT NULL,
                value REAL NOT NULL,
                unit TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT,
                source_device TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(user_id, external_id),
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE goals(
                user_id INTEGER PRIMARY KEY,
                calories INTEGER NOT NULL,
                protein INTEGER NOT NULL,
                steps INTEGER NOT NULL,
                phase TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        await connection.commit()
    finally:
        await connection.close()

    db = coach_bot.Database(str(path))
    await db.init()
    connection = await aiosqlite.connect(path)
    connection.row_factory = aiosqlite.Row
    try:
        rows = await (await connection.execute("PRAGMA foreign_key_list(goals)")).fetchall()
        assert any(
            row["from"] == "user_id"
            and row["table"] == "users"
            and row["on_delete"].upper() == "CASCADE"
            for row in rows
        )
    finally:
        await connection.close()
