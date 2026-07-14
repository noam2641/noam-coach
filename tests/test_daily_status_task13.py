"""TASK-13 — "מצב היום" is a concise dashboard without duplicate blocks.

Current state → what happened → rest of day → one recommendation. Remaining
macros appear once, no duplicate workout status, no full next-meal screen
embedded, no internal engine explanations.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import coach_bot
from db import Database
from helpers import utc_now


async def _db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Database:
    db = Database(str(tmp_path / "task13.db"))
    await db.init()
    await db.execute("INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)", (utc_now(),))
    now = utc_now()
    await db.execute(
        "INSERT INTO goal_versions(user_id,calories,protein,steps,phase,status,source,explanation,created_at) "
        "VALUES(1,2100,165,8000,'x','active','user_approved','t',?)",
        (now,),
    )
    await db.execute("INSERT INTO goals(user_id,calories,protein,steps,phase,updated_at) VALUES(1,2100,165,8000,'x',?)", (now,))
    monkeypatch.setattr(coach_bot, "DB", db)
    from noam_coach.bot import workout as workout_bot

    monkeypatch.setattr(workout_bot, "DB", db, raising=False)
    return db


async def _log(db: Database, name: str, cal: float, prot: float) -> None:
    now = utc_now()
    await db.execute(
        "INSERT INTO meals(user_id,name,calories,protein,carbs,fat,confidence,eaten_at,created_at) "
        "VALUES(1,?,?,?,20,10,0.9,?,?)",
        (name, cal, prot, now, now),
    )


@pytest.mark.asyncio
async def test_daily_status_sections_and_no_internal_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = await _db(tmp_path, monkeypatch)
    await _log(db, "קוטג' 5% עם ירקות", 184, 18)
    text = await coach_bot.build_daily_status(1)

    # The four dashboard sections.
    assert "מצב היום" in text
    assert "נאכל היום" in text
    # Internal engine explanations are gone.
    assert "הקלוריות והחלבון לפי היתרה שנותרה" not in text
    assert "מתעדכנים ככל שמדווחים ארוחות" not in text
    assert "הכי קרוב ליעד החלבון שנותר" not in text
    assert "מקור היעד" not in text
    # Not the full next-meal screen embedded (no per-option "למה עכשיו").
    assert "למה עכשיו" not in text
    # A single "נשאר" block.
    assert text.count("<b>נשאר:</b>") <= 1


@pytest.mark.asyncio
async def test_no_raw_iso_in_daily_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = await _db(tmp_path, monkeypatch)
    await _log(db, "ארוחה", 400, 40)
    text = await coach_bot.build_daily_status(1)
    assert "2026-" not in text
    assert "+03:00" not in text and "+02:00" not in text
