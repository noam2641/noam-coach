#!/usr/bin/env python3
"""Create a consistent SQLite + storage backup archive with checksums."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_files(root: Path):
    if not root.exists():
        return
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            yield path


def prune_backups(folder: Path, keep: int) -> None:
    archives = sorted(folder.glob("noam_coach_backup_*.tar.gz"), reverse=True)
    for old in archives[max(1, keep) :]:
        old.unlink(missing_ok=True)


def create_backup(db_path: Path, storage_dir: Path, output_dir: Path, keep: int) -> Path:
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive = output_dir / f"noam_coach_backup_{stamp}.tar.gz"

    with tempfile.TemporaryDirectory(prefix="noam_backup_") as temp_name:
        temp = Path(temp_name)
        snapshot = temp / "noam_coach.db"
        source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        target = sqlite3.connect(snapshot)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()

        copied_storage = temp / "storage"
        copied_storage.mkdir()
        storage_manifest: list[dict[str, object]] = []
        if storage_dir.exists():
            for source_file in safe_files(storage_dir):
                relative = source_file.relative_to(storage_dir)
                destination = copied_storage / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_file, destination)
                storage_manifest.append(
                    {
                        "path": relative.as_posix(),
                        "bytes": destination.stat().st_size,
                        "sha256": sha256(destination),
                    }
                )

        manifest = {
            "format_version": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "database": {
                "name": "noam_coach.db",
                "bytes": snapshot.stat().st_size,
                "sha256": sha256(snapshot),
            },
            "storage": storage_manifest,
        }
        (temp / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        with tarfile.open(archive, "w:gz") as tar:
            tar.add(snapshot, arcname="noam_coach.db", recursive=False)
            tar.add(temp / "manifest.json", arcname="manifest.json", recursive=False)
            tar.add(copied_storage, arcname="storage", recursive=True)

    prune_backups(output_dir, keep)
    return archive


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=os.getenv("DATABASE_PATH", "./data/noam_coach.db"))
    parser.add_argument("--storage", default=os.getenv("STORAGE_DIR", "./storage"))
    parser.add_argument("--output", default=os.getenv("BACKUP_DIR", "./backups"))
    parser.add_argument("--keep", type=int, default=14)
    args = parser.parse_args()
    archive = create_backup(
        Path(args.db).expanduser().resolve(),
        Path(args.storage).expanduser().resolve(),
        Path(args.output).expanduser().resolve(),
        args.keep,
    )
    print(archive)


if __name__ == "__main__":
    main()
