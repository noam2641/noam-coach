from __future__ import annotations

import json
import sqlite3
import zipfile
from pathlib import Path

import coach_bot
from scripts.delete_user_data import delete_user
from scripts.export_user_data import export_user
from scripts.privacy_audit import scan_privacy_risks


def test_export_and_delete_user_data(tmp_path: Path) -> None:
    db_path = tmp_path / "coach.db"
    import asyncio

    db = coach_bot.Database(str(db_path))
    asyncio.run(db.init())
    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'Noam',NULL,?)",
        (coach_bot.utc_now(),),
    )
    image = tmp_path / "meal.jpg"
    image.write_bytes(b"image")
    connection.execute(
        """
        INSERT INTO meals(
            user_id,name,calories,protein,carbs,fat,confidence,image_path,eaten_at,created_at
        ) VALUES(1,'meal',100,10,10,2,1,?,?,?)
        """,
        (str(image), coach_bot.utc_now(), coach_bot.utc_now()),
    )
    connection.commit()
    connection.close()

    archive = export_user(db_path, 1, tmp_path / "export.zip")
    with zipfile.ZipFile(archive) as exported:
        payload = json.loads(exported.read("data.json"))
        assert payload["user_id"] == 1
        assert len(payload["tables"]["meals"]) == 1
        assert any(name.startswith("meal_images/") for name in exported.namelist())

    assert delete_user(db_path, 1, 1) == 1
    connection = sqlite3.connect(db_path)
    assert connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM meals").fetchone()[0] == 0
    connection.close()
    assert not image.exists()


def test_privacy_audit_flags_sensitive_files_without_touching_them(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("TOKEN=x", encoding="utf-8")
    (tmp_path / "coach.db").write_bytes(b"sqlite")
    (tmp_path / "Recording_20260705_1939.docx").write_bytes(b"doc")
    storage = tmp_path / "storage"
    storage.mkdir()
    (storage / "ignored.db").write_bytes(b"sqlite")

    findings = scan_privacy_risks(tmp_path)

    by_path = {finding.path: finding for finding in findings}
    assert by_path[".env"].severity == "high"
    assert by_path["coach.db"].severity == "high"
    assert by_path["Recording_20260705_1939.docx"].severity == "review"
    assert "storage/ignored.db" not in by_path
