"""LOG-012 — the audit trail must never store raw free-text.

`write_audit` is the single choke point for every audit INSERT. Because the
audit table is exported verbatim in the user's DSAR ZIP, `details` is filtered
through a per-(action, entity) ALLOWLIST that keeps only bounded structured
fields (codes, ids, numeric metadata, booleans) and drops everything else,
then passes the survivors through the canonical redactor as a backstop.

These tests pin the new contract:
  - free-text keys (goal explanation, meal correction_text, daily-flag note
    text, medication name) are DROPPED / never stored;
  - bounded fields (calories, revision, counts, kind_code, booleans) survive;
  - a DSAR export contains none of the raw free-text;
  - medication is stored as a coarse CATEGORY code, not the raw drug name.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

import coach_bot
from scripts.export_user_data import export_user

USER_ID = 1
# Deliberately identifiable strings we assert never reach the store.
GOAL_EXPLANATION = "כי המשתמש ביקש דיאטה קפדנית מאוד עם צום לסירוגין"
CORRECTION_TEXT = "actually it was 300g grilled chicken breast not 150g"
FLAG_NOTE = "לקחתי ריטלין הבוקר ואני בצום עד 12"
MEDICATION_NAME = "Ritalin LA 20mg"


async def _fresh_db(tmp_path: Path) -> "coach_bot.Database":
    db = coach_bot.Database(str(tmp_path / "audit.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(?,'Noam',NULL,?)",
        (USER_ID, coach_bot.utc_now()),
    )
    return db


async def _audit_rows(db: "coach_bot.Database") -> list[dict]:
    rows = await db.fetch_all(
        "SELECT action, entity, details FROM audit WHERE user_id=? ORDER BY id",
        (USER_ID,),
    )
    return [dict(row) for row in rows]


def _run(coro):
    import asyncio

    return asyncio.run(coro)


@pytest.mark.asyncio
async def test_goal_explanation_is_dropped(tmp_path: Path) -> None:
    db = await _fresh_db(tmp_path)
    cb = coach_bot
    orig, cb.DB = cb.DB, db
    try:
        await cb.write_audit(
            USER_ID, "approve", "goal", 42,
            calories=2100, protein=150, steps=8000,
            phase="fat_loss", explanation=GOAL_EXPLANATION,
        )
        details = json.loads((await _audit_rows(db))[0]["details"])
    finally:
        cb.DB = orig
    assert details["calories"] == 2100
    assert details["protein"] == 150
    assert details["phase"] == "fat_loss"
    assert "explanation" not in details
    assert GOAL_EXPLANATION not in json.dumps(details, ensure_ascii=False)


@pytest.mark.asyncio
async def test_meal_correction_text_is_dropped(tmp_path: Path) -> None:
    db = await _fresh_db(tmp_path)
    cb = coach_bot
    orig, cb.DB = cb.DB, db
    try:
        await cb.write_audit(
            USER_ID, "meal_text_correction", "approval", 7,
            revision=3, correction_text=CORRECTION_TEXT,
        )
        details = json.loads((await _audit_rows(db))[0]["details"])
    finally:
        cb.DB = orig
    assert details["revision"] == 3
    assert "correction_text" not in details
    assert CORRECTION_TEXT not in json.dumps(details, ensure_ascii=False)


@pytest.mark.asyncio
async def test_daily_flag_note_text_is_dropped(tmp_path: Path) -> None:
    db = await _fresh_db(tmp_path)
    cb = coach_bot
    orig, cb.DB = cb.DB, db
    try:
        # A caller that still (incorrectly) passes raw text must have it
        # dropped by the choke point — only derived booleans may survive.
        await cb.write_audit(
            USER_ID, "daily_flags", "flags", None,
            ritalin=True, fasting=True, has_note=True, text=FLAG_NOTE,
        )
        details = json.loads((await _audit_rows(db))[0]["details"])
    finally:
        cb.DB = orig
    assert details == {"ritalin": True, "fasting": True, "has_note": True}
    assert "text" not in details
    assert FLAG_NOTE not in json.dumps(details, ensure_ascii=False)


@pytest.mark.asyncio
async def test_medication_is_stored_as_code_not_raw_name(tmp_path: Path) -> None:
    db = await _fresh_db(tmp_path)
    cb = coach_bot
    orig, cb.DB = cb.DB, db
    try:
        # Even a caller passing the raw drug name gets it coded + dropped.
        await cb.write_audit(
            USER_ID, "medication", "med_event", 99, name=MEDICATION_NAME,
        )
        details = json.loads((await _audit_rows(db))[0]["details"])
    finally:
        cb.DB = orig
    assert details == {"kind_code": "stimulant"}
    assert "name" not in details
    assert MEDICATION_NAME not in json.dumps(details, ensure_ascii=False)
    assert "Ritalin" not in json.dumps(details, ensure_ascii=False)


@pytest.mark.asyncio
async def test_safety_alert_location_is_coded(tmp_path: Path) -> None:
    db = await _fresh_db(tmp_path)
    cb = coach_bot
    orig, cb.DB = cb.DB, db
    try:
        await cb.write_audit(
            USER_ID, "safety_alert", "constraint", 5,
            kind="pain", location="כאב חד בברך שמאל אחרי ריצה",
        )
        details = json.loads((await _audit_rows(db))[0]["details"])
    finally:
        cb.DB = orig
    assert details["kind_code"] == "pain"
    assert details["location_code"] == "knee"
    assert "location" not in details
    assert "כאב חד" not in json.dumps(details, ensure_ascii=False)


@pytest.mark.asyncio
async def test_unknown_action_drops_free_text_keeps_scalars(tmp_path: Path) -> None:
    db = await _fresh_db(tmp_path)
    cb = coach_bot
    orig, cb.DB = cb.DB, db
    try:
        await cb.write_audit(
            USER_ID, "brand_new_action", "brand_new_entity", None,
            count=4, ok=True,
            free_text="a very long free-text sentence that a future dev might "
            "pass without realizing it leaks into the DSAR export forever",
        )
        details = json.loads((await _audit_rows(db))[0]["details"])
    finally:
        cb.DB = orig
    assert details["count"] == 4
    assert details["ok"] is True
    assert "free_text" not in details


def test_dsar_export_contains_no_raw_free_text(tmp_path: Path) -> None:
    db_path = tmp_path / "audit.db"

    async def _seed():
        db = coach_bot.Database(str(db_path))
        await db.init()
        await db.execute(
            "INSERT INTO users(id, first_name, username, updated_at) VALUES(?,'Noam',NULL,?)",
            (USER_ID, coach_bot.utc_now()),
        )
        cb = coach_bot
        orig, cb.DB = cb.DB, db
        try:
            await cb.write_audit(
                USER_ID, "approve", "goal", 42,
                calories=2100, protein=150, steps=8000,
                phase="fat_loss", explanation=GOAL_EXPLANATION,
            )
            await cb.write_audit(
                USER_ID, "meal_text_correction", "approval", 7,
                revision=1, correction_text=CORRECTION_TEXT,
            )
            await cb.write_audit(
                USER_ID, "medication", "med_event", 99, name=MEDICATION_NAME,
            )
            await cb.write_audit(
                USER_ID, "daily_flags", "flags", None,
                ritalin=True, fasting=True, has_note=True, text=FLAG_NOTE,
            )
        finally:
            cb.DB = orig

    _run(_seed())

    archive = export_user(db_path, USER_ID, tmp_path / "export.zip")
    with zipfile.ZipFile(archive) as exported:
        blob = exported.read("data.json").decode("utf-8")

    # The whole DSAR payload — every audit row included — must be free of the
    # raw medical / correction / goal free-text and raw drug name.
    for leak in (GOAL_EXPLANATION, CORRECTION_TEXT, FLAG_NOTE, MEDICATION_NAME, "Ritalin"):
        assert leak not in blob, f"raw free-text leaked into DSAR export: {leak!r}"

    payload = json.loads(blob)
    audit_rows = payload["tables"]["audit"]
    # Bounded / coded fields must survive so the trail keeps its value.
    all_details = " ".join(row["details"] for row in audit_rows)
    assert "stimulant" in all_details  # medication coded to category
    assert "2100" in all_details  # goal target preserved
