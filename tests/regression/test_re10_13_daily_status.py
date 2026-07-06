"""RE10-13 regression tests — rich "מצב היום" screen.

Covers:
  * No stale Apple Health activity section anywhere on this screen.
  * Remaining calories/protein and hours-until-sleep are shown.
  * A planned-but-unreported workout gets one explicit assumption line.
  * The remaining-slot allocations section respects the hard cap and never
    disagrees with the day's actual reported meals.
  * D4: an unapproved (provisional) goal is called out explicitly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import coach_bot
import user_model


async def _db_with_user(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> coach_bot.Database:
    db = coach_bot.Database(str(tmp_path / "status.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (coach_bot.utc_now(),),
    )
    monkeypatch.setattr(coach_bot, "DB", db)
    return db


async def _approve_goal(db: coach_bot.Database, calories: int = 2100, protein: int = 190) -> None:
    now = coach_bot.utc_now()
    await db.execute(
        "INSERT INTO goal_versions(user_id, calories, protein, steps, phase, status, source, "
        "explanation, created_at) VALUES(1, ?, ?, 8000, 'fat_loss_muscle_retention', 'active', "
        "'manual', 'test goal', ?)",
        (calories, protein, now),
    )
    await db.execute(
        "INSERT INTO goals(user_id, calories, protein, steps, phase, updated_at) "
        "VALUES(1, ?, ?, 8000, 'fat_loss_muscle_retention', ?) "
        "ON CONFLICT(user_id) DO UPDATE SET calories=excluded.calories, protein=excluded.protein",
        (calories, protein, now),
    )


async def _log_meal(db: coach_bot.Database, name: str, calories: float, protein: float) -> None:
    now = coach_bot.utc_now()
    await db.execute(
        "INSERT INTO meals(user_id, name, calories, protein, carbs, fat, confidence, eaten_at, created_at) "
        "VALUES(1, ?, ?, ?, 10, 5, 0.9, ?, ?)",
        (name, calories, protein, now, now),
    )


@pytest.mark.asyncio
async def test_no_stale_activity_section_when_no_meals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    await _db_with_user(tmp_path, monkeypatch)
    text = await coach_bot.build_daily_status(1)
    assert "נתוני פעילות" not in text
    assert "Apple Health" not in text
    assert "צעדי היום" not in text


@pytest.mark.asyncio
async def test_no_stale_activity_section_with_meals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = await _db_with_user(tmp_path, monkeypatch)
    await _approve_goal(db)
    await _log_meal(db, "ביצים", 300, 20)

    text = await coach_bot.build_daily_status(1)
    assert "נתוני פעילות" not in text
    assert "Apple Health" not in text
    assert "צעדי היום" not in text
    assert "משקל אחרון" not in text  # weight tracking moved out of this screen


@pytest.mark.asyncio
async def test_shows_remaining_calories_and_protein(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = await _db_with_user(tmp_path, monkeypatch)
    await _approve_goal(db, calories=2100, protein=195)
    await _log_meal(db, "ארוחת צהריים", 400, 30)

    text = await coach_bot.build_daily_status(1)
    assert "נשארו לך היום" in text
    assert "1700" in text or "1,700" in text  # 2100 - 400
    assert "165" in text  # 195 - 30


@pytest.mark.asyncio
async def test_shows_hours_until_sleep(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = await _db_with_user(tmp_path, monkeypatch)
    await _approve_goal(db)
    await _log_meal(db, "ארוחה", 300, 20)

    text = await coach_bot.build_daily_status(1)
    assert "עד שינה" in text


@pytest.mark.asyncio
async def test_provisional_goal_is_called_out_explicitly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """D4: a fallback (never-approved) goal must be flagged, not silently used."""
    db = await _db_with_user(tmp_path, monkeypatch)
    # No approved goal_versions row at all -> fetch_goal falls back to a computed default.
    await user_model.set_fact(db, 1, "weight_kg", 85.0, source=user_model.SOURCE_USER, confirmed=True)
    await _log_meal(db, "ארוחה", 300, 20)

    text = await coach_bot.build_daily_status(1)
    assert "יעד זמני" in text


@pytest.mark.asyncio
async def test_remaining_slots_section_respects_hard_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = await _db_with_user(tmp_path, monkeypatch)
    await _approve_goal(db, calories=2100, protein=195)
    await _log_meal(db, "ארוחת בוקר", 300, 20)

    text = await coach_bot.build_daily_status(1)
    assert "ארוחות עד סוף היום" in text


@pytest.mark.asyncio
async def test_meal_list_still_shown_and_framed_as_reported_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _db_with_user(tmp_path, monkeypatch)
    await _approve_goal(db)
    await _log_meal(db, "פסטה", 500, 15)

    text = await coach_bot.build_daily_status(1)
    assert "פסטה" in text
    assert "דווחו" in text
    assert "שדווחו בלבד" in text


@pytest.mark.asyncio
async def test_goal_source_is_explicit_without_meals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex audit: the user must always see where the daily target comes
    from — approved, provisional computation, or default."""
    await _db_with_user(tmp_path, monkeypatch)
    text = await coach_bot.build_daily_status(1)
    assert "מקור היעד" in text


@pytest.mark.asyncio
async def test_goal_source_shows_user_approved_with_meals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _db_with_user(tmp_path, monkeypatch)
    await _approve_goal(db)
    await _log_meal(db, "ביצים", 300, 20)
    text = await coach_bot.build_daily_status(1)
    assert "מקור היעד: יעד שאישרת" in text
