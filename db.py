"""Database layer — schema, Database class, and migrations."""

from __future__ import annotations

import asyncio
import json
import re
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite

from config import LOGGER, SETTINGS, TZ
from helpers import utc_now

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS schema_migrations(
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY,
    first_name TEXT,
    username TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS goals(
    user_id INTEGER PRIMARY KEY,
    calories INTEGER NOT NULL,
    protein INTEGER NOT NULL,
    steps INTEGER NOT NULL,
    phase TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS approvals(
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    decided_at TEXT,
    telegram_file_unique_id TEXT,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS meals(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    calories REAL NOT NULL,
    protein REAL NOT NULL,
    carbs REAL NOT NULL,
    fat REAL NOT NULL,
    confidence REAL NOT NULL,
    image_path TEXT,
    approval_id TEXT UNIQUE,
    eaten_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'consumed',
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY(approval_id) REFERENCES approvals(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS meal_items(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meal_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    grams REAL NOT NULL,
    calories REAL NOT NULL,
    protein REAL NOT NULL,
    carbs REAL NOT NULL,
    fat REAL NOT NULL,
    confidence REAL NOT NULL,
    FOREIGN KEY(meal_id) REFERENCES meals(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS sessions(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    plan TEXT NOT NULL,
    status TEXT NOT NULL,
    exercise_index INTEGER NOT NULL,
    set_number INTEGER NOT NULL,
    pending_weight REAL,
    pending_reps INTEGER,
    pain_location TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS sets(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    exercise_id TEXT NOT NULL,
    exercise_name TEXT NOT NULL,
    set_number INTEGER NOT NULL,
    weight REAL NOT NULL,
    reps INTEGER NOT NULL,
    rir INTEGER NOT NULL,
    source TEXT NOT NULL,
    client_event_id TEXT UNIQUE,
    created_at TEXT NOT NULL,
    FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS health(
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

CREATE TABLE IF NOT EXISTS audit(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    entity TEXT NOT NULL,
    entity_id TEXT,
    details TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_health_user_type
ON health(user_id, sample_type, start_time DESC);

CREATE TABLE IF NOT EXISTS routine_profile(
    user_id INTEGER PRIMARY KEY,
    profile TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS daily_flags(
    user_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    flags TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(user_id, day),
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS job_state(
    user_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL DEFAULT '1',
    status TEXT NOT NULL DEFAULT 'sent',
    priority INTEGER NOT NULL DEFAULT 10,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    claimed_at TEXT,
    sent_at TEXT,
    last_error TEXT,
    next_retry_at TEXT,
    counts_toward_budget INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(user_id, day, key),
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS user_facts(
    user_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    kind TEXT NOT NULL,
    source TEXT NOT NULL,
    confidence REAL NOT NULL,
    confirmed INTEGER NOT NULL DEFAULT 0,
    valid INTEGER NOT NULL DEFAULT 1,
    affects TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(user_id, key),
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS user_fact_history(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    source TEXT NOT NULL,
    confidence REAL NOT NULL,
    recorded_at TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS medical_constraints(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    location TEXT,
    severity INTEGER,
    status TEXT NOT NULL,
    note TEXT,
    affects TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS medication_events(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    medication_id TEXT,
    name TEXT NOT NULL,
    formulation TEXT,
    dose_text TEXT,
    taken_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    source TEXT NOT NULL,
    status TEXT NOT NULL,
    confidence REAL NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_fact_history
ON user_fact_history(user_id, key, recorded_at DESC);

CREATE INDEX IF NOT EXISTS idx_constraints_active
ON medical_constraints(user_id, status);

CREATE INDEX IF NOT EXISTS idx_medication_events
ON medication_events(user_id, taken_at DESC);

CREATE TABLE IF NOT EXISTS conversation_state(
    user_id INTEGER NOT NULL,
    flow TEXT NOT NULL,
    step TEXT NOT NULL,
    payload TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(user_id, flow),
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS analytics_events(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    event TEXT NOT NULL,
    properties TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_analytics_events
ON analytics_events(user_id, event, created_at DESC);

CREATE TABLE IF NOT EXISTS exercise_overrides(
    user_id INTEGER NOT NULL,
    code TEXT NOT NULL,
    exercise_index INTEGER NOT NULL,
    field TEXT NOT NULL,
    value REAL NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(user_id, code, exercise_index, field),
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS active_flow(
    user_id INTEGER PRIMARY KEY,
    flow TEXT NOT NULL DEFAULT 'idle',
    step TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL DEFAULT '{}',
    version INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS goal_versions(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    calories INTEGER NOT NULL,
    protein INTEGER NOT NULL,
    steps INTEGER NOT NULL,
    phase TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'provisional',
    source TEXT NOT NULL DEFAULT 'computed',
    explanation TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    decided_at TEXT,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_goal_versions_user
ON goal_versions(user_id, status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_meal_items_meal
ON meal_items(meal_id);

CREATE INDEX IF NOT EXISTS idx_sets_session
ON sets(session_id, set_number);

CREATE TABLE IF NOT EXISTS product_events(
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

CREATE INDEX IF NOT EXISTS idx_product_events_user
ON product_events(user_id, id DESC);

CREATE INDEX IF NOT EXISTS idx_product_events_flow
ON product_events(user_id, flow_id, id DESC);

CREATE TABLE IF NOT EXISTS plan_versions(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    plan_type TEXT NOT NULL,
    title TEXT NOT NULL,
    strategy TEXT NOT NULL,
    fit_score REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'candidate',
    payload TEXT NOT NULL,
    rationale TEXT NOT NULL DEFAULT '[]',
    tradeoffs TEXT NOT NULL DEFAULT '[]',
    assumptions TEXT NOT NULL DEFAULT '[]',
    based_on TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    activated_at TEXT,
    superseded_at TEXT,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_plan_versions_user
ON plan_versions(user_id, plan_type, status, created_at DESC);

CREATE TABLE IF NOT EXISTS active_plans(
    user_id INTEGER NOT NULL,
    plan_type TEXT NOT NULL,
    plan_id INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(user_id, plan_type),
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY(plan_id) REFERENCES plan_versions(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS meal_fingerprints(
    meal_id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    telegram_file_unique_id TEXT,
    sha256 TEXT NOT NULL DEFAULT '',
    perceptual_hash TEXT NOT NULL DEFAULT '',
    fingerprint TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY(meal_id) REFERENCES meals(id) ON DELETE CASCADE,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_meal_fingerprints_user
ON meal_fingerprints(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS mini_login_tokens(
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    purpose TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    consumed_at TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS plan_feedback(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    plan_id INTEGER NOT NULL,
    feedback_type TEXT NOT NULL,
    value TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY(plan_id) REFERENCES plan_versions(id) ON DELETE CASCADE
);
"""


DB_BUSY_TIMEOUT_MS = 5000
DB_LOCK_RETRIES = 4


class Database:
    def __init__(self, path: str):
        self.path = path

    def _connect(self) -> Any:
        return aiosqlite.connect(
            self.path,
            timeout=DB_BUSY_TIMEOUT_MS / 1000,
        )

    async def _prepare(self, connection: Any) -> None:
        await connection.execute(f"PRAGMA busy_timeout={DB_BUSY_TIMEOUT_MS}")
        await connection.execute("PRAGMA foreign_keys=ON")

    async def init(self) -> None:
        existing_database = (
            self.path != ":memory:"
            and Path(self.path).exists()
            and Path(self.path).stat().st_size > 0
        )

        async with self._connect() as connection:
            await self._prepare(connection)
            await connection.executescript(SCHEMA)
            await connection.commit()

        await run_migrations(self, backup_existing=existing_database)

        async with self._connect() as connection:
            await self._prepare(connection)
            cursor = await connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='users'"
            )
            row = await cursor.fetchone()
            if not row or row[0] == 0:
                raise RuntimeError(
                    "Database schema failed to initialize — the DB file may be "
                    "corrupt or locked by another running instance."
                )

            cursor = await connection.execute("PRAGMA foreign_key_check")
            violations = await cursor.fetchall()
            if violations:
                raise RuntimeError(
                    f"Foreign-key validation failed after migration: {violations[:5]}"
                )

        LOGGER.info("Database initialized at %s", self.path)

    @asynccontextmanager
    async def transaction(self) -> Any:
        """Open one atomic write transaction.

        Lock retries apply only while acquiring BEGIN IMMEDIATE. Once control is
        yielded, the caller's body is never replayed implicitly; replaying an
        arbitrary transaction body could duplicate external side effects.
        """
        connection: Any | None = None
        try:
            for attempt in range(DB_LOCK_RETRIES):
                candidate = await self._connect()
                try:
                    candidate.row_factory = aiosqlite.Row
                    await self._prepare(candidate)
                    await candidate.execute("BEGIN IMMEDIATE")
                    connection = candidate
                    break
                except aiosqlite.OperationalError as exc:
                    await candidate.close()
                    is_locked = "locked" in str(exc).lower()
                    if not is_locked or attempt >= DB_LOCK_RETRIES - 1:
                        raise
                    await asyncio.sleep(0.1 * (attempt + 1))

            if connection is None:
                raise RuntimeError("Unable to acquire database transaction")

            try:
                yield connection
                await connection.commit()
            except BaseException:
                with suppress(Exception):
                    await connection.rollback()
                raise
        finally:
            if connection is not None:
                await connection.close()

    async def execute(
        self,
        sql: str,
        parameters: tuple[Any, ...] = (),
    ) -> int:
        async with self.transaction() as connection:
            cursor = await connection.execute(sql, parameters)
            return int(cursor.lastrowid or 0)

    async def execute_rowcount(
        self,
        sql: str,
        parameters: tuple[Any, ...] = (),
    ) -> int:
        async with self.transaction() as connection:
            cursor = await connection.execute(sql, parameters)
            return int(cursor.rowcount)

    async def executemany(
        self,
        sql: str,
        seq_of_parameters: list[tuple[Any, ...]],
    ) -> None:
        if not seq_of_parameters:
            return
        async with self.transaction() as connection:
            await connection.executemany(sql, seq_of_parameters)

    async def fetch_one(
        self,
        sql: str,
        parameters: tuple[Any, ...] = (),
    ) -> dict[str, Any] | None:
        async with self._connect() as connection:
            connection.row_factory = aiosqlite.Row
            await self._prepare(connection)
            cursor = await connection.execute(sql, parameters)
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def fetch_all(
        self,
        sql: str,
        parameters: tuple[Any, ...] = (),
    ) -> list[dict[str, Any]]:
        async with self._connect() as connection:
            connection.row_factory = aiosqlite.Row
            await self._prepare(connection)
            cursor = await connection.execute(sql, parameters)
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


SCHEMA_MIGRATIONS: tuple[tuple[int, str], ...] = (
    (1, "single_active_session"),
    (2, "job_delivery_state"),
    (3, "foreign_keys_and_health_identity"),
    (4, "active_flow_table"),
    (5, "goal_versions_table"),
    (6, "planning_and_event_tables"),
    (7, "active_flow_expiry"),
    (8, "meal_origin"),
    (9, "single_active_goal_version"),
    (10, "clean_polluted_gap_values"),
    (11, "meal_status_column"),
)

FK_MIGRATION_TABLES: tuple[str, ...] = (
    "goals",
    "approvals",
    "meals",
    "meal_items",
    "sessions",
    "sets",
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
    "active_flow",
    "product_events",
    "plan_versions",
    "active_plans",
    "meal_fingerprints",
    "mini_login_tokens",
    "plan_feedback",
)


TARGET_FOREIGN_KEYS: dict[str, tuple[str, str, str]] = {
    "goals": ("user_id", "users", "id"),
    "approvals": ("user_id", "users", "id"),
    "meals": ("user_id", "users", "id"),
    "meal_items": ("meal_id", "meals", "id"),
    "sessions": ("user_id", "users", "id"),
    "sets": ("session_id", "sessions", "id"),
    "health": ("user_id", "users", "id"),
    "audit": ("user_id", "users", "id"),
    "routine_profile": ("user_id", "users", "id"),
    "daily_flags": ("user_id", "users", "id"),
    "job_state": ("user_id", "users", "id"),
    "user_facts": ("user_id", "users", "id"),
    "user_fact_history": ("user_id", "users", "id"),
    "medical_constraints": ("user_id", "users", "id"),
    "medication_events": ("user_id", "users", "id"),
    "conversation_state": ("user_id", "users", "id"),
    "analytics_events": ("user_id", "users", "id"),
    "exercise_overrides": ("user_id", "users", "id"),
    "active_flow": ("user_id", "users", "id"),
    "product_events": ("user_id", "users", "id"),
    "plan_versions": ("user_id", "users", "id"),
    "active_plans": ("user_id", "users", "id"),
    "meal_fingerprints": ("user_id", "users", "id"),
    "mini_login_tokens": ("user_id", "users", "id"),
    "plan_feedback": ("user_id", "users", "id"),
}


def _schema_table_ddl(table: str, replacement_name: str) -> str:
    pattern = re.compile(
        rf"CREATE TABLE IF NOT EXISTS {re.escape(table)}\((.*?)\n\);",
        re.DOTALL,
    )
    match = pattern.search(SCHEMA)
    if not match:
        raise RuntimeError(f"Target schema is missing table {table}")
    return match.group(0).replace(
        f"CREATE TABLE IF NOT EXISTS {table}(",
        f"CREATE TABLE {replacement_name}(",
        1,
    )


async def _database_backup(db: Database) -> Path | None:
    if db.path == ":memory:":
        return None
    source_path = Path(db.path)
    if not source_path.exists():
        return None
    stamp = datetime.now(TZ).strftime("%Y%m%d_%H%M%S")
    suffix = source_path.suffix or ".db"
    destination = source_path.with_name(f"{source_path.stem}.pre_migration_{stamp}{suffix}")
    async with db._connect() as source:
        async with aiosqlite.connect(destination) as target:
            await source.backup(target)
    LOGGER.info("Pre-migration backup created at %s", destination)
    return destination


async def _record_migration(
    connection: Any,
    version: int,
    name: str,
) -> None:
    await connection.execute(
        """
        INSERT INTO schema_migrations(version, name, applied_at)
        VALUES(?, ?, ?)
        """,
        (version, name, utc_now()),
    )


async def _migration_single_active_session(db: Database) -> None:
    async with db.transaction() as connection:
        cursor = await connection.execute(
            """
            SELECT id, user_id
            FROM sessions
            WHERE status='active'
              AND id NOT IN (
                  SELECT MAX(id)
                  FROM sessions
                  WHERE status='active'
                  GROUP BY user_id
              )
            ORDER BY user_id, id
            """
        )
        duplicates = await cursor.fetchall()
        ended_at = utc_now()
        for row in duplicates:
            await connection.execute(
                """
                UPDATE sessions
                SET status='cancelled', ended_at=COALESCE(ended_at, ?)
                WHERE id=? AND status='active'
                """,
                (ended_at, row["id"]),
            )
            await connection.execute(
                """
                INSERT INTO audit(
                    user_id, action, entity, entity_id, details, created_at
                ) VALUES(?, 'migration_cancel_duplicate', 'workout', ?, ?, ?)
                """,
                (
                    row["user_id"],
                    str(row["id"]),
                    json.dumps(
                        {"reason": "duplicate_active_session"},
                        ensure_ascii=False,
                    ),
                    ended_at,
                ),
            )

        await connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_one_active_session_per_user
            ON sessions(user_id) WHERE status='active'
            """
        )
        await _record_migration(connection, 1, "single_active_session")


async def _migration_job_delivery_state(db: Database) -> None:
    columns_to_add: tuple[tuple[str, str], ...] = (
        ("status", "TEXT NOT NULL DEFAULT 'sent'"),
        ("priority", "INTEGER NOT NULL DEFAULT 10"),
        ("attempt_count", "INTEGER NOT NULL DEFAULT 0"),
        ("claimed_at", "TEXT"),
        ("sent_at", "TEXT"),
        ("last_error", "TEXT"),
        ("next_retry_at", "TEXT"),
        ("counts_toward_budget", "INTEGER NOT NULL DEFAULT 1"),
        ("updated_at", "TEXT NOT NULL DEFAULT ''"),
    )
    async with db.transaction() as connection:
        cursor = await connection.execute("PRAGMA table_info(job_state)")
        present = {row["name"] for row in await cursor.fetchall()}
        for name, definition in columns_to_add:
            if name not in present:
                await connection.execute(f"ALTER TABLE job_state ADD COLUMN {name} {definition}")

        await connection.execute(
            """
            UPDATE job_state
            SET status=COALESCE(NULLIF(status, ''), 'sent'),
                sent_at=COALESCE(sent_at, created_at),
                updated_at=CASE
                    WHEN updated_at IS NULL OR updated_at='' THEN created_at
                    ELSE updated_at
                END
            """
        )
        await _record_migration(connection, 2, "job_delivery_state")


async def _migration_foreign_keys(db: Database) -> None:
    all_target_fks_present = True
    has_composite_health_identity = False
    async with db._connect() as inspection:
        inspection.row_factory = aiosqlite.Row
        for table, expected in TARGET_FOREIGN_KEYS.items():
            cursor = await inspection.execute(f"PRAGMA foreign_key_list({table})")
            actual = {
                (row["from"], row["table"], row["to"], row["on_delete"].upper())
                for row in await cursor.fetchall()
            }
            source_column, parent_table, parent_column = expected
            if (source_column, parent_table, parent_column, "CASCADE") not in actual:
                all_target_fks_present = False

        cursor = await inspection.execute("PRAGMA index_list(health)")
        health_indexes = await cursor.fetchall()
        for index in health_indexes:
            if not index["unique"]:
                continue
            cursor = await inspection.execute(f"PRAGMA index_info('{index['name']}')")
            columns = [row["name"] for row in await cursor.fetchall()]
            if columns == ["user_id", "external_id"]:
                has_composite_health_identity = True
                break

    if all_target_fks_present and has_composite_health_identity:
        async with db.transaction() as connection:
            await connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS ux_one_active_session_per_user
                ON sessions(user_id) WHERE status='active'
                """
            )
            await _record_migration(connection, 3, "foreign_keys_and_health_identity")
        return

    connection = await db._connect()
    connection.row_factory = aiosqlite.Row
    try:
        await connection.execute(f"PRAGMA busy_timeout={DB_BUSY_TIMEOUT_MS}")
        await connection.execute("PRAGMA foreign_keys=OFF")
        await connection.execute("BEGIN IMMEDIATE")

        for table in FK_MIGRATION_TABLES:
            assert table in FK_MIGRATION_TABLES, f"Unexpected table name: {table!r}"  # noqa: S105 – guard against future refactors that pass untrusted input
            temporary = f"{table}__new"
            await connection.execute(f"DROP TABLE IF EXISTS {temporary}")
            await connection.execute(_schema_table_ddl(table, temporary))

            old_cursor = await connection.execute(f"PRAGMA table_info({table})")
            old_columns = {row["name"] for row in await old_cursor.fetchall()}
            new_cursor = await connection.execute(f"PRAGMA table_info({temporary})")
            new_columns = [row["name"] for row in await new_cursor.fetchall()]
            columns = [name for name in new_columns if name in old_columns]
            if not columns:
                continue

            orphan_query: str | None = None
            if table == "meal_items" and "meal_id" in columns:
                orphan_query = (
                    "SELECT COUNT(*) AS c FROM meal_items "
                    "WHERE meal_id NOT IN (SELECT id FROM meals)"
                )
            elif table == "sets" and "session_id" in columns:
                orphan_query = (
                    "SELECT COUNT(*) AS c FROM sets "
                    "WHERE session_id NOT IN (SELECT id FROM sessions)"
                )
            elif "user_id" in columns:
                orphan_query = (
                    f"SELECT COUNT(*) AS c FROM {table} WHERE user_id NOT IN (SELECT id FROM users)"
                )
            if orphan_query:
                orphan_cursor = await connection.execute(orphan_query)
                orphan_count = int((await orphan_cursor.fetchone())["c"] or 0)
                if orphan_count:
                    raise RuntimeError(
                        f"Migration stopped: {table} contains "
                        f"{orphan_count} orphan row(s); no data was deleted"
                    )

            quoted = ", ".join(f'"{name}"' for name in columns)
            await connection.execute(
                f"INSERT INTO {temporary} ({quoted}) SELECT {quoted} FROM {table}"
            )

        for table in reversed(FK_MIGRATION_TABLES):
            await connection.execute(f"DROP TABLE {table}")

        for table in FK_MIGRATION_TABLES:
            await connection.execute(f"ALTER TABLE {table}__new RENAME TO {table}")

        index_statements = (
            "CREATE INDEX IF NOT EXISTS idx_health_user_type "
            "ON health(user_id, sample_type, start_time DESC)",
            "CREATE INDEX IF NOT EXISTS idx_fact_history "
            "ON user_fact_history(user_id, key, recorded_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_constraints_active "
            "ON medical_constraints(user_id, status)",
            "CREATE INDEX IF NOT EXISTS idx_medication_events "
            "ON medication_events(user_id, taken_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_analytics_events "
            "ON analytics_events(user_id, event, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_meal_items_meal ON meal_items(meal_id)",
            "CREATE INDEX IF NOT EXISTS idx_sets_session ON sets(session_id, set_number)",
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_one_active_session_per_user "
            "ON sessions(user_id) WHERE status='active'",
        )
        for statement in index_statements:
            await connection.execute(statement)
        await connection.execute(
            """
            UPDATE job_state
            SET status=COALESCE(NULLIF(status, ''), 'sent'),
                sent_at=COALESCE(sent_at, created_at),
                updated_at=CASE
                    WHEN updated_at IS NULL OR updated_at='' THEN created_at
                    ELSE updated_at
                END
            """
        )
        await _record_migration(connection, 3, "foreign_keys_and_health_identity")
        cursor = await connection.execute("PRAGMA foreign_key_check")
        violations = await cursor.fetchall()
        if violations:
            raise RuntimeError(
                f"Foreign-key migration produced invalid references: {violations[:5]}"
            )
        await connection.commit()
    except BaseException:
        with suppress(Exception):
            await connection.rollback()
        raise
    finally:
        with suppress(Exception):
            await connection.execute("PRAGMA foreign_keys=ON")
        await connection.close()


async def _migration_active_flow(db: Database) -> None:
    """Migration 4: create active_flow table and populate from conversation_state.

    The old conversation_state table allowed multiple concurrent flows per user
    (PRIMARY KEY(user_id, flow)).  The new active_flow table enforces exactly one
    active flow per user (PRIMARY KEY(user_id)).

    Priority when collapsing multiple rows: meal_fix > pending_prompt >
    deferred_plan > confirm_pending.  The highest-priority row becomes the
    active flow; everything else is discarded (the old state was already
    inconsistent — that's why we're migrating).
    """
    async with db.transaction() as connection:
        # 1. Create the table if it doesn't exist yet (SCHEMA already has it,
        #    but the CREATE TABLE IF NOT EXISTS in SCHEMA might not have run
        #    for existing databases).
        await connection.execute("""
            CREATE TABLE IF NOT EXISTS active_flow(
                user_id INTEGER PRIMARY KEY,
                flow TEXT NOT NULL DEFAULT 'idle',
                step TEXT NOT NULL DEFAULT '',
                payload TEXT NOT NULL DEFAULT '{}',
                version INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)

        # 2. Add telegram_file_unique_id to approvals if missing.
        cursor = await connection.execute("PRAGMA table_info(approvals)")
        approvals_cols = {row["name"] for row in await cursor.fetchall()}
        if "telegram_file_unique_id" not in approvals_cols:
            await connection.execute(
                "ALTER TABLE approvals ADD COLUMN telegram_file_unique_id TEXT"
            )

        # 3. Populate active_flow from conversation_state.
        #    Priority order for picking the "winning" flow per user.
        cursor = await connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='conversation_state'"
        )
        if await cursor.fetchone():
            cursor = await connection.execute(
                "SELECT user_id, flow, step, payload, updated_at "
                "FROM conversation_state ORDER BY user_id"
            )
            rows = await cursor.fetchall()

            # Group by user_id and pick highest-priority flow.
            flow_priority = {
                "meal_fix": 1,
                "pending_prompt": 2,
                "deferred_plan": 3,
                "confirm_pending": 4,
            }
            # Map old conversation_state flow names to new FlowName values.
            flow_name_map = {
                "meal_fix": "meal_correction",
                "pending_prompt": "onboarding_question",
                "deferred_plan": "deferred_question",
                "confirm_pending": "confirm_number",
            }

            users: dict[int, dict] = {}
            for row in rows:
                uid = int(row["user_id"])
                old_flow = row["flow"]
                priority = flow_priority.get(old_flow, 99)
                if uid not in users or priority < users[uid]["priority"]:
                    users[uid] = {
                        "priority": priority,
                        "flow": flow_name_map.get(old_flow, old_flow),
                        "step": row["step"],
                        "payload": row["payload"],
                        "updated_at": row["updated_at"],
                    }

            now = utc_now()
            for uid, data in users.items():
                await connection.execute(
                    """
                    INSERT INTO active_flow(user_id, flow, step, payload, version, updated_at)
                    VALUES(?, ?, ?, ?, 1, ?)
                    ON CONFLICT(user_id) DO NOTHING
                    """,
                    (
                        uid,
                        data["flow"],
                        data["step"],
                        data["payload"] or "{}",
                        data["updated_at"] or now,
                    ),
                )

        await _record_migration(connection, 4, "active_flow_table")


async def _migration_goal_versions(db: Database) -> None:
    """Migration 5: create goal_versions table and seed from existing goals."""
    async with db.transaction() as connection:
        await connection.execute("""
            CREATE TABLE IF NOT EXISTS goal_versions(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                calories INTEGER NOT NULL,
                protein INTEGER NOT NULL,
                steps INTEGER NOT NULL,
                phase TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'provisional',
                source TEXT NOT NULL DEFAULT 'computed',
                explanation TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                decided_at TEXT,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)
        await connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_goal_versions_user
            ON goal_versions(user_id, status, created_at DESC)
        """)
        # Seed from existing goals table.
        now = utc_now()
        await connection.execute("""
            INSERT INTO goal_versions(user_id, calories, protein, steps, phase, status, source, created_at, decided_at)
            SELECT user_id, calories, protein, steps, phase, 'active', 'legacy', ?, ?
            FROM goals
        """, (now, now))
        await _record_migration(connection, 5, "goal_versions_table")


async def _migration_planning_and_events(db: Database) -> None:
    """Migration 6: persistent plans, event replay, fingerprints and Mini tokens."""
    async with db.transaction() as connection:
        statements = [
            """CREATE TABLE IF NOT EXISTS product_events(
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                event TEXT NOT NULL, entity TEXT NOT NULL DEFAULT 'system',
                entity_id TEXT, flow_id TEXT, flow_version INTEGER,
                source TEXT NOT NULL DEFAULT 'bot', properties TEXT NOT NULL DEFAULT '{}',
                before_state TEXT, after_state TEXT, created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE)""",
            """CREATE TABLE IF NOT EXISTS plan_versions(
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                plan_type TEXT NOT NULL, title TEXT NOT NULL, strategy TEXT NOT NULL,
                fit_score REAL NOT NULL, status TEXT NOT NULL DEFAULT 'candidate',
                payload TEXT NOT NULL, rationale TEXT NOT NULL DEFAULT '[]',
                tradeoffs TEXT NOT NULL DEFAULT '[]', assumptions TEXT NOT NULL DEFAULT '[]',
                based_on TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
                activated_at TEXT, superseded_at TEXT,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE)""",
            """CREATE TABLE IF NOT EXISTS active_plans(
                user_id INTEGER NOT NULL, plan_type TEXT NOT NULL, plan_id INTEGER NOT NULL,
                updated_at TEXT NOT NULL, PRIMARY KEY(user_id, plan_type),
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(plan_id) REFERENCES plan_versions(id) ON DELETE CASCADE)""",
            """CREATE TABLE IF NOT EXISTS meal_fingerprints(
                meal_id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,
                telegram_file_unique_id TEXT, sha256 TEXT NOT NULL DEFAULT '',
                perceptual_hash TEXT NOT NULL DEFAULT '', fingerprint TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY(meal_id) REFERENCES meals(id) ON DELETE CASCADE,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE)""",
            """CREATE TABLE IF NOT EXISTS mini_login_tokens(
                token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, purpose TEXT NOT NULL,
                expires_at INTEGER NOT NULL, consumed_at TEXT, created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE)""",
            """CREATE TABLE IF NOT EXISTS plan_feedback(
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                plan_id INTEGER NOT NULL, feedback_type TEXT NOT NULL, value TEXT NOT NULL,
                note TEXT, created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(plan_id) REFERENCES plan_versions(id) ON DELETE CASCADE)""",
        ]
        for statement in statements:
            await connection.execute(statement)
        for statement in (
            "CREATE INDEX IF NOT EXISTS idx_product_events_user ON product_events(user_id, id DESC)",
            "CREATE INDEX IF NOT EXISTS idx_product_events_flow ON product_events(user_id, flow_id, id DESC)",
            "CREATE INDEX IF NOT EXISTS idx_plan_versions_user ON plan_versions(user_id, plan_type, status, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_meal_fingerprints_user ON meal_fingerprints(user_id, created_at DESC)",
        ):
            await connection.execute(statement)
        await _record_migration(connection, 6, "planning_and_event_tables")


async def _migration_active_flow_expiry(db: Database) -> None:
    """Migration 7: explicit expiry/status metadata for restart-safe flows."""
    async with db.transaction() as connection:
        cursor = await connection.execute("PRAGMA table_info(active_flow)")
        present = {row["name"] for row in await cursor.fetchall()}
        for name, definition in (
            ("flow_id", "TEXT NOT NULL DEFAULT ''"),
            ("status", "TEXT NOT NULL DEFAULT 'active'"),
            ("expires_at", "TEXT"),
            ("parent_flow_id", "TEXT"),
        ):
            if name not in present:
                await connection.execute(f"ALTER TABLE active_flow ADD COLUMN {name} {definition}")
        await connection.execute(
            "UPDATE active_flow SET flow_id=CASE WHEN flow_id='' THEN 'legacy-' || user_id ELSE flow_id END"
        )
        await _record_migration(connection, 7, "active_flow_expiry")


async def _migration_meal_origin(db: Database) -> None:
    """Migration 8: connect saved meals to their approval for safe edit/undo."""
    async with db.transaction() as connection:
        cursor = await connection.execute("PRAGMA table_info(meals)")
        present = {row["name"] for row in await cursor.fetchall()}
        if "approval_id" not in present:
            await connection.execute("ALTER TABLE meals ADD COLUMN approval_id TEXT")
        await connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_meals_approval_id "
            "ON meals(approval_id) WHERE approval_id IS NOT NULL"
        )
        await _record_migration(connection, 8, "meal_origin")


async def _migration_single_active_goal_version(db: Database) -> None:
    """Migration 9: enforce one current goal version per user."""
    async with db.transaction() as connection:
        now = utc_now()
        await connection.execute(
            """
            WITH ranked AS (
                SELECT
                    id,
                    ROW_NUMBER() OVER (
                        PARTITION BY user_id
                        ORDER BY
                            CASE status WHEN 'active' THEN 0 ELSE 1 END,
                            COALESCE(decided_at, created_at) DESC,
                            id DESC
                    ) AS rn
                FROM goal_versions
                WHERE status IN ('active', 'active_provisional')
            )
            UPDATE goal_versions
            SET status='superseded',
                decided_at=COALESCE(decided_at, ?)
            WHERE id IN (SELECT id FROM ranked WHERE rn > 1)
            """,
            (now,),
        )
        await connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_goal_versions_single_current
            ON goal_versions(user_id)
            WHERE status IN ('active', 'active_provisional')
            """
        )
        await _record_migration(connection, 9, "single_active_goal_version")


_POLLUTED_GAP_MARKER = "missing"


def _strip_polluted_gap_repr(raw_value: str) -> tuple[str, bool]:
    """Remove a leaked gap-dict repr from a persisted fact string value.

    RE10-2 path B: several onboarding handlers used to read an unanswered
    (gap) fact with plain string interpolation and merge new items into it,
    e.g. ``"{'missing': True, 'why_matters': 'בטיחות תזונתית'}, אגוזים"``.
    That string was then persisted as a real ``KIND_FACT`` value, so the
    corruption survives even after the handlers are fixed. This helper
    extracts any real comma-separated items that follow the leaked dict
    fragment and drops the fragment itself.

    Returns (cleaned_value, changed).
    """
    if "{'missing'" not in raw_value and '{"missing"' not in raw_value:
        return raw_value, False
    # Drop the dict-repr fragment: "{...}" possibly followed by ", " then real items.
    cleaned = re.sub(r"\{[^{}]*['\"]missing['\"][^{}]*\}\s*,?\s*", "", raw_value)
    cleaned = cleaned.strip(" ,")
    return cleaned, True


async def _migration_clean_polluted_gap_values(db: Database) -> None:
    """Migration 10: repair user_facts values that leaked a gap-dict repr.

    Scans every ``KIND_FACT`` string value for the poisoned pattern produced
    by the RE10-2 bug and rewrites it to contain only the real items the user
    actually provided (or reverts the fact to empty/"none" when nothing real
    was left). This does not touch legitimate gap rows (``kind='gap'``) —
    those already display correctly once filtered — only facts that were
    incorrectly promoted out of gap state with corrupted content.
    """
    async with db.transaction() as connection:
        cursor = await connection.execute(
            "SELECT user_id, key, value FROM user_facts "
            "WHERE kind='fact' AND value LIKE '%missing%'"
        )
        rows = await cursor.fetchall()
        now = utc_now()
        fixed = 0
        for row in rows:
            raw = row["value"]
            try:
                decoded = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(decoded, str):
                continue
            cleaned, changed = _strip_polluted_gap_repr(decoded)
            if not changed:
                continue
            new_value = cleaned if cleaned else "none"
            await connection.execute(
                "UPDATE user_facts SET value=?, updated_at=? WHERE user_id=? AND key=?",
                (json.dumps(new_value, ensure_ascii=False), now, row["user_id"], row["key"]),
            )
            fixed += 1
        LOGGER.info("Migration 10: cleaned %d polluted user_facts values", fixed)
        await _record_migration(connection, 10, "clean_polluted_gap_values")


async def _migration_meal_status_column(db: Database) -> None:
    """Migration 11: add meals.status so consumed/planned/recommended stay
    distinct (TASK-04/PATCH-10). Existing rows are real logged meals, so they
    default to 'consumed' — only 'consumed' rows count toward daily totals.
    """
    async with db.transaction() as connection:
        cursor = await connection.execute("PRAGMA table_info(meals)")
        present = {row["name"] for row in await cursor.fetchall()}
        if "status" not in present:
            await connection.execute(
                "ALTER TABLE meals ADD COLUMN status TEXT NOT NULL DEFAULT 'consumed'"
            )
        await _record_migration(connection, 11, "meal_status_column")


async def run_migrations(
    db: Database,
    *,
    backup_existing: bool,
) -> None:
    rows = await db.fetch_all("SELECT version FROM schema_migrations ORDER BY version")
    applied = {int(row["version"]) for row in rows}
    pending = [version for version, _ in SCHEMA_MIGRATIONS if version not in applied]
    if not pending:
        return

    if backup_existing and 3 in pending:
        await _database_backup(db)

    for version, name in SCHEMA_MIGRATIONS:
        if version in applied:
            continue
        LOGGER.info("Applying schema migration %s: %s", version, name)
        if version == 1:
            await _migration_single_active_session(db)
        elif version == 2:
            await _migration_job_delivery_state(db)
        elif version == 3:
            await _migration_foreign_keys(db)
        elif version == 4:
            await _migration_active_flow(db)
        elif version == 5:
            await _migration_goal_versions(db)
        elif version == 6:
            await _migration_planning_and_events(db)
        elif version == 7:
            await _migration_active_flow_expiry(db)
        elif version == 8:
            await _migration_meal_origin(db)
        elif version == 9:
            await _migration_single_active_goal_version(db)
        elif version == 10:
            await _migration_clean_polluted_gap_values(db)
        elif version == 11:
            await _migration_meal_status_column(db)
        else:
            raise RuntimeError(f"Unknown schema migration {version}")
        LOGGER.info("Applied schema migration %s: %s", version, name)


DB = Database(SETTINGS.database_path)
