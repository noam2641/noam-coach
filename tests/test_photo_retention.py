from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import coach_bot
import db as db_module


@pytest.mark.asyncio
async def test_photo_retention_cleans_meals_pending_and_orphans(
    tmp_path: Path, monkeypatch
) -> None:
    test_db = coach_bot.Database(str(tmp_path / "coach.db"))
    await test_db.init()
    await test_db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (coach_bot.utc_now(),),
    )
    storage = tmp_path / "storage"
    food = storage / "food"
    food.mkdir(parents=True)
    meal_image = food / "meal.jpg"
    pending_image = food / "pending.jpg"
    orphan_image = food / "orphan.jpg"
    for path in (meal_image, pending_image, orphan_image):
        path.write_bytes(b"image")
        old_epoch = (datetime.now(timezone.utc) - timedelta(days=40)).timestamp()
        os.utime(path, (old_epoch, old_epoch))
    old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    await test_db.execute(
        """
        INSERT INTO meals(
            user_id,name,calories,protein,carbs,fat,confidence,image_path,eaten_at,created_at
        ) VALUES(1,'meal',1,1,1,1,1,?,?,?)
        """,
        (str(meal_image), old, old),
    )
    await test_db.execute(
        """
        INSERT INTO approvals(id,user_id,kind,payload,status,created_at)
        VALUES('pending',1,'meal',?,'pending',?)
        """,
        (json.dumps({"image": str(pending_image)}), old),
    )
    monkeypatch.setattr(db_module, "DB", test_db)
    monkeypatch.setattr(coach_bot, "DB", test_db)
    monkeypatch.setattr(coach_bot.SETTINGS, "storage_dir", str(storage))
    monkeypatch.setattr(coach_bot.SETTINGS, "photo_retention_days", 30)

    result = await coach_bot.cleanup_photos_once()
    assert result == {"meal_images": 1, "approvals": 1, "orphans": 1}
    assert not meal_image.exists()
    assert not pending_image.exists()
    assert not orphan_image.exists()
    meal = await test_db.fetch_one("SELECT image_path FROM meals")
    approval = await test_db.fetch_one("SELECT status FROM approvals WHERE id='pending'")
    assert meal["image_path"] is None
    assert approval["status"] == "expired"
