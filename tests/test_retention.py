"""Tests for retention.py — data cleanup functions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import db as db_module
import retention
from db import Database
from helpers import utc_now


@pytest.mark.asyncio
async def test_cleanup_operational_respects_retention_days(tmp_path: Path, monkeypatch) -> None:
    test_db = Database(str(tmp_path / "test.db"))
    await test_db.init()
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=800)).isoformat()
    recent = now.isoformat()
    await test_db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (recent,),
    )
    # Insert old and recent health records
    for ext_id, ts in (("old", old), ("new", recent)):
        await test_db.execute(
            "INSERT INTO health(user_id,external_id,sample_type,value,unit,start_time,created_at) "
            "VALUES(1,?,'steps',1,'count',?,?)",
            (ext_id, ts, ts),
        )
    monkeypatch.setattr(db_module, "DB", test_db)
    result = await retention.cleanup_operational_data_once()
    assert result["health"] == 1
    remaining = await test_db.fetch_one("SELECT COUNT(*) AS c FROM health")
    assert remaining["c"] == 1


@pytest.mark.asyncio
async def test_cleanup_photos_removes_expired_images(tmp_path: Path, monkeypatch) -> None:
    import os

    test_db = Database(str(tmp_path / "test.db"))
    await test_db.init()
    await test_db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (utc_now(),),
    )
    storage = tmp_path / "storage"
    food = storage / "food"
    food.mkdir(parents=True)
    img = food / "test.jpg"
    img.write_bytes(b"data")
    old_epoch = (datetime.now(timezone.utc) - timedelta(days=60)).timestamp()
    os.utime(img, (old_epoch, old_epoch))
    old_ts = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    await test_db.execute(
        "INSERT INTO meals(user_id,name,calories,protein,carbs,fat,confidence,image_path,eaten_at,created_at) "
        "VALUES(1,'x',1,1,1,1,1,?,?,?)",
        (str(img), old_ts, old_ts),
    )
    monkeypatch.setattr(db_module, "DB", test_db)
    monkeypatch.setattr(retention.SETTINGS, "storage_dir", str(storage))
    monkeypatch.setattr(retention.SETTINGS, "photo_retention_days", 30)
    result = await retention.cleanup_photos_once()
    assert result["meal_images"] == 1
    assert not img.exists()
