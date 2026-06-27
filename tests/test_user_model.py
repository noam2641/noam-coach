"""Tests for user_model.py — fact lifecycle, provenance, and profile view."""

from __future__ import annotations

from pathlib import Path

import pytest

import coach_bot
import user_model


@pytest.mark.asyncio
async def test_set_and_get_fact(tmp_path: Path) -> None:
    db = coach_bot.Database(str(tmp_path / "coach.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (coach_bot.utc_now(),),
    )
    changed = await user_model.set_fact(
        db, 1, "weight_kg", 85.0,
        kind=user_model.KIND_FACT,
        source=user_model.SOURCE_APPLE_HEALTH,
    )
    assert changed is True

    fact = await user_model.get_fact(db, 1, "weight_kg")
    assert fact is not None
    assert fact["value"] == 85.0
    assert fact["source"] == "apple_health"


@pytest.mark.asyncio
async def test_set_fact_unchanged_returns_false(tmp_path: Path) -> None:
    db = coach_bot.Database(str(tmp_path / "coach.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (coach_bot.utc_now(),),
    )
    await user_model.set_fact(db, 1, "weight_kg", 85.0)
    changed = await user_model.set_fact(db, 1, "weight_kg", 85.0)
    assert changed is False


@pytest.mark.asyncio
async def test_fact_history_on_change(tmp_path: Path) -> None:
    db = coach_bot.Database(str(tmp_path / "coach.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (coach_bot.utc_now(),),
    )
    await user_model.set_fact(db, 1, "weight_kg", 85.0)
    await user_model.set_fact(db, 1, "weight_kg", 84.0)

    history = await db.fetch_all(
        "SELECT * FROM user_fact_history WHERE user_id=1 AND key='weight_kg'"
    )
    assert len(history) == 1
    assert '"85.0"' in history[0]["value"] or "85" in history[0]["value"]


@pytest.mark.asyncio
async def test_get_value_default(tmp_path: Path) -> None:
    db = coach_bot.Database(str(tmp_path / "coach.db"))
    await db.init()
    val = await user_model.get_value(db, 1, "nonexistent", "default_val")
    assert val == "default_val"


@pytest.mark.asyncio
async def test_confirm_fact(tmp_path: Path) -> None:
    db = coach_bot.Database(str(tmp_path / "coach.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (coach_bot.utc_now(),),
    )
    await user_model.set_fact(
        db, 1, "weight_kg", 85.0,
        kind=user_model.KIND_ESTIMATE,
        confidence=0.5,
    )
    await user_model.confirm_fact(db, 1, "weight_kg")

    fact = await user_model.get_fact(db, 1, "weight_kg")
    assert fact is not None
    assert fact["confirmed"] is True
    assert fact["confidence"] >= 0.9


@pytest.mark.asyncio
async def test_invalidate_fact(tmp_path: Path) -> None:
    db = coach_bot.Database(str(tmp_path / "coach.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (coach_bot.utc_now(),),
    )
    await user_model.set_fact(db, 1, "weight_kg", 85.0)
    await user_model.invalidate_fact(db, 1, "weight_kg")

    fact = await user_model.get_fact(db, 1, "weight_kg")
    assert fact is None  # invalid facts are not returned


@pytest.mark.asyncio
async def test_record_gap(tmp_path: Path) -> None:
    db = coach_bot.Database(str(tmp_path / "coach.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (coach_bot.utc_now(),),
    )
    await user_model.record_gap(db, 1, "allergies", why_matters="בטיחות תזונתית")

    fact = await user_model.get_fact(db, 1, "allergies")
    assert fact is not None
    assert fact["kind"] == user_model.KIND_GAP


@pytest.mark.asyncio
async def test_record_gap_skips_if_known(tmp_path: Path) -> None:
    db = coach_bot.Database(str(tmp_path / "coach.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (coach_bot.utc_now(),),
    )
    await user_model.set_fact(db, 1, "allergies", "none")
    await user_model.record_gap(db, 1, "allergies", why_matters="test")

    fact = await user_model.get_fact(db, 1, "allergies")
    assert fact["value"] == "none"  # gap did not overwrite


@pytest.mark.asyncio
async def test_get_profile_view(tmp_path: Path) -> None:
    db = coach_bot.Database(str(tmp_path / "coach.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (coach_bot.utc_now(),),
    )
    await user_model.set_fact(db, 1, "weight_kg", 85.0, source=user_model.SOURCE_APPLE_HEALTH)
    await user_model.set_fact(db, 1, "primary_goal", "fat_loss", source=user_model.SOURCE_USER)
    await user_model.record_gap(db, 1, "allergies", why_matters="test")

    view = await user_model.get_profile_view(db, 1)
    assert len(view["measured"]) >= 1
    assert len(view["reported"]) >= 1
    assert len(view["gaps"]) >= 1


@pytest.mark.asyncio
async def test_explain_fact_known(tmp_path: Path) -> None:
    db = coach_bot.Database(str(tmp_path / "coach.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (coach_bot.utc_now(),),
    )
    await user_model.set_fact(db, 1, "weight_kg", 85.0, source=user_model.SOURCE_APPLE_HEALTH)
    text = await user_model.explain_fact(db, 1, "weight_kg")
    assert "85" in text
    assert "Apple Health" in text


@pytest.mark.asyncio
async def test_explain_fact_unknown(tmp_path: Path) -> None:
    db = coach_bot.Database(str(tmp_path / "coach.db"))
    await db.init()
    text = await user_model.explain_fact(db, 1, "weight_kg")
    assert "לא ידוע" in text


def test_confidence_label() -> None:
    assert user_model.confidence_label(0.9) == "גבוהה"
    assert user_model.confidence_label(0.7) == "בינונית"
    assert user_model.confidence_label(0.3) == "נמוכה"


def test_fact_registry_has_expected_keys() -> None:
    assert "weight_kg" in user_model.FACT_REGISTRY
    assert "primary_goal" in user_model.FACT_REGISTRY
    assert "active_pain" in user_model.FACT_REGISTRY
    for spec in user_model.FACT_REGISTRY.values():
        assert spec.affects, f"{spec.key} has no affects"
