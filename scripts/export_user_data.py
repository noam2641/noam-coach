#!/usr/bin/env python3
"""Export one user's database records and referenced meal images to a ZIP."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

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
                tables[table] = (
                    rows(connection, f'SELECT * FROM "{table}" WHERE user_id=?', (user_id,))
                    if table != "users"
                    else rows(connection, "SELECT * FROM users WHERE id=?", (user_id,))
                )

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
