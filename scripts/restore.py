#!/usr/bin/env python3
"""Restore a backup created by backup.py after checksum verification."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

from dotenv import load_dotenv


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_member(member: tarfile.TarInfo) -> bool:
    path = PurePosixPath(member.name)
    return (
        not path.is_absolute()
        and ".." not in path.parts
        and not member.issym()
        and not member.islnk()
    )


def restore(archive: Path, db_path: Path, storage_dir: Path, force: bool) -> None:
    if not force:
        raise RuntimeError("Restore is destructive; pass --force after stopping the app")
    with tempfile.TemporaryDirectory(prefix="noam_restore_") as temp_name:
        temp = Path(temp_name)
        with tarfile.open(archive, "r:gz") as tar:
            members = tar.getmembers()
            if not all(safe_member(member) for member in members):
                raise ValueError("Backup contains an unsafe path or link")
            tar.extractall(temp, members=members, filter="data")

        manifest = json.loads((temp / "manifest.json").read_text(encoding="utf-8"))
        restored_db = temp / "noam_coach.db"
        if sha256(restored_db) != manifest["database"]["sha256"]:
            raise ValueError("Database checksum mismatch")
        for item in manifest.get("storage", []):
            path = temp / "storage" / item["path"]
            if not path.exists() or sha256(path) != item["sha256"]:
                raise ValueError(f"Storage checksum mismatch: {item['path']}")

        db_path.parent.mkdir(parents=True, exist_ok=True)
        storage_dir.parent.mkdir(parents=True, exist_ok=True)
        if db_path.exists():
            emergency = db_path.with_suffix(db_path.suffix + ".before_restore")
            shutil.copy2(db_path, emergency)
        if storage_dir.exists():
            shutil.rmtree(storage_dir)
        shutil.copy2(restored_db, db_path)
        shutil.copytree(temp / "storage", storage_dir)


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("archive")
    parser.add_argument("--db", default=os.getenv("DATABASE_PATH", "./data/noam_coach.db"))
    parser.add_argument("--storage", default=os.getenv("STORAGE_DIR", "./storage"))
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    restore(
        Path(args.archive).expanduser().resolve(),
        Path(args.db).expanduser().resolve(),
        Path(args.storage).expanduser().resolve(),
        args.force,
    )
    print("Restore completed. Run scripts/preflight.py before starting the app.")


if __name__ == "__main__":
    main()
