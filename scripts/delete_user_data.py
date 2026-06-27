#!/usr/bin/env python3
"""Delete one user and all cascading records after an explicit confirmation."""

from __future__ import annotations

import argparse
import os
import sqlite3
from pathlib import Path

from dotenv import load_dotenv


def delete_user(db_path: Path, user_id: int, confirmation: int) -> int:
    if confirmation != user_id:
        raise RuntimeError("Confirmation user id does not match")
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        images = [
            Path(row[0])
            for row in connection.execute(
                "SELECT image_path FROM meals WHERE user_id=? AND image_path IS NOT NULL",
                (user_id,),
            ).fetchall()
        ]
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute("DELETE FROM users WHERE id=?", (user_id,))
        if cursor.rowcount != 1:
            connection.rollback()
            return 0
        connection.commit()
        for image in images:
            try:
                if image.is_file() and not image.is_symlink():
                    image.unlink()
            except OSError:
                pass
        return 1
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("user_id", type=int)
    parser.add_argument("--confirm-user-id", type=int, required=True)
    parser.add_argument("--db", default=os.getenv("DATABASE_PATH", "./data/noam_coach.db"))
    args = parser.parse_args()
    deleted = delete_user(Path(args.db), args.user_id, args.confirm_user_id)
    print("deleted" if deleted else "user not found")


if __name__ == "__main__":
    main()
