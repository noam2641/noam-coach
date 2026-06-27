from __future__ import annotations

import sqlite3
from pathlib import Path

from scripts.backup import create_backup
from scripts.restore import restore


def test_backup_and_restore_round_trip(tmp_path: Path) -> None:
    db = tmp_path / "source.db"
    connection = sqlite3.connect(db)
    connection.execute("CREATE TABLE sample(value TEXT)")
    connection.execute("INSERT INTO sample VALUES('kept')")
    connection.commit()
    connection.close()
    storage = tmp_path / "storage"
    storage.mkdir()
    (storage / "photo.txt").write_text("image", encoding="utf-8")

    archive = create_backup(db, storage, tmp_path / "backups", keep=3)
    restored_db = tmp_path / "restored" / "coach.db"
    restored_storage = tmp_path / "restored_storage"
    restore(archive, restored_db, restored_storage, force=True)

    connection = sqlite3.connect(restored_db)
    assert connection.execute("SELECT value FROM sample").fetchone()[0] == "kept"
    connection.close()
    assert (restored_storage / "photo.txt").read_text(encoding="utf-8") == "image"
