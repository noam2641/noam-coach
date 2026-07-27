"""W1-9 — duplicate detection must ignore decided (rejected/approved) approvals.

The production defect: the duplicate lookup in ``handle_photo`` filtered by
user, telegram file id, a 6-hour window and ``kind`` — but never by ``status``.
The row it found was used as a truthy gate, so a *decided* approval still
raised a ``meal_duplicate`` card.

Live evidence: approval ``W-RbRHEKu94`` was rejected at 07:07:57 and its image
deleted; 15 seconds later, at 07:08:12, approval ``Lk2XAFA31bI`` (kind
``meal_duplicate``) was raised against it with
``existing_approval_id='W-RbRHEKu94'`` and ``existing_meal_id=null``. The user
had re-sent the photo *because* they had just rejected it, and was told it
duplicated the thing they had discarded.

Only ``pending`` is a live duplicate. See
``meals_bot.LIVE_DUPLICATE_APPROVAL_STATUSES`` for the per-status reasoning.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import coach_bot
from db import Database
from helpers import utc_now
from noam_coach.bot import meals as meals_bot

FILE_UID = "AgACAgQAAxkBAAI-photo-unique-id"
USER_ID = 1


async def _db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Database:
    db = Database(str(tmp_path / "dup_status.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(?,'A',NULL,?)",
        (USER_ID, utc_now()),
    )
    # runtime_bound refreshes meals_bot.DB from the coach_bot facade on every
    # call, so the facade is the binding that actually matters here.
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(meals_bot, "DB", db, raising=False)
    return db


async def _approval(
    db: Database,
    status: str,
    *,
    kind: str = "meal",
    approval_id: str = "A1",
    file_uid: str | None = FILE_UID,
    created_at: str | None = None,
) -> str:
    now = created_at or utc_now()
    await db.execute(
        """
        INSERT INTO approvals(
            id, user_id, kind, payload, status, created_at, decided_at,
            telegram_file_unique_id
        ) VALUES(?,?,?,?,?,?,?,?)
        """,
        (
            approval_id,
            USER_ID,
            kind,
            json.dumps({"analysis": {}, "image": "/tmp/x.jpg"}, ensure_ascii=False),
            status,
            now,
            None if status == "pending" else now,
            file_uid,
        ),
    )
    return approval_id


@pytest.mark.asyncio
async def test_rejected_approval_does_not_raise_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reported defect: re-sending a photo after rejecting it.

    Rejecting deletes the image and is recoverable only via an explicit
    ``restore_meal``. Re-sending the photo IS the user's retry — it must be
    analyzed, not refused as a duplicate of what they just discarded.
    """
    db = await _db(tmp_path, monkeypatch)
    await _approval(db, "rejected", approval_id="W-RbRHEKu94")

    assert await meals_bot.find_live_duplicate_approval(USER_ID, FILE_UID) is None


@pytest.mark.asyncio
async def test_pending_approval_still_raises_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live, undecided analysis is a genuine duplicate and must be kept."""
    db = await _db(tmp_path, monkeypatch)
    await _approval(db, "pending", approval_id="P1")

    row = await meals_bot.find_live_duplicate_approval(USER_ID, FILE_UID)
    assert row is not None
    assert row["id"] == "P1"
    # The caller only renders "open the existing analysis" for a pending row.
    assert row["status"] == "pending"


@pytest.mark.asyncio
async def test_approved_approval_does_not_raise_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Approved is correctly EXCLUDED from the approvals arm.

    This is not a loss of duplicate protection. ``persist_meal`` writes a real
    meal and calls ``register_meal_fingerprint``, so an approved photo is
    caught by ``meal_intelligence.find_image_duplicate`` (the ``saved_dup``
    arm), which carries a ``meal_id`` and drives the actionable "edit the
    existing meal" button. Matching here as well would only produce a second,
    weaker duplicate card with ``existing_meal_id=None``.
    """
    db = await _db(tmp_path, monkeypatch)
    await _approval(db, "approved", approval_id="OK1")

    assert await meals_bot.find_live_duplicate_approval(USER_ID, FILE_UID) is None


@pytest.mark.asyncio
async def test_superseded_approval_does_not_raise_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Superseded means discarded — never a live duplicate."""
    db = await _db(tmp_path, monkeypatch)
    await _approval(db, "superseded", approval_id="S1")

    assert await meals_bot.find_live_duplicate_approval(USER_ID, FILE_UID) is None


@pytest.mark.asyncio
async def test_pending_wins_over_older_rejected_for_same_photo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rejected row must not mask a later pending one for the same photo.

    ORDER BY created_at DESC previously made the newest row win regardless of
    status; the pending analysis is the one the user can actually open.
    """
    db = await _db(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    await _approval(
        db,
        "pending",
        approval_id="P2",
        created_at=(now - timedelta(minutes=30)).isoformat(),
    )
    # Rejected 15 seconds ago — newer than the pending row, mirroring the
    # live incident's ordering.
    await _approval(
        db,
        "rejected",
        approval_id="R2",
        created_at=(now - timedelta(seconds=15)).isoformat(),
    )

    row = await meals_bot.find_live_duplicate_approval(USER_ID, FILE_UID)
    assert row is not None
    assert row["id"] == "P2"


@pytest.mark.asyncio
async def test_meal_edit_kind_pending_still_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The status filter must not narrow the existing kind coverage."""
    db = await _db(tmp_path, monkeypatch)
    await _approval(db, "pending", kind="meal_edit", approval_id="E1")

    row = await meals_bot.find_live_duplicate_approval(USER_ID, FILE_UID)
    assert row is not None
    assert row["id"] == "E1"


@pytest.mark.asyncio
async def test_pending_outside_window_is_not_a_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 6-hour window is preserved alongside the new status filter."""
    db = await _db(tmp_path, monkeypatch)
    stale = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
    await _approval(db, "pending", approval_id="OLD1", created_at=stale)

    assert await meals_bot.find_live_duplicate_approval(USER_ID, FILE_UID) is None


@pytest.mark.asyncio
async def test_other_users_pending_photo_is_not_a_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """User scoping is preserved alongside the new status filter."""
    db = await _db(tmp_path, monkeypatch)
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(2,'B',NULL,?)",
        (utc_now(),),
    )
    await db.execute(
        """
        INSERT INTO approvals(
            id, user_id, kind, payload, status, created_at, telegram_file_unique_id
        ) VALUES('OTHER',2,'meal','{}','pending',?,?)
        """,
        (utc_now(), FILE_UID),
    )

    assert await meals_bot.find_live_duplicate_approval(USER_ID, FILE_UID) is None


def test_only_pending_counts_as_a_live_duplicate() -> None:
    """Pin the chosen status set so a future widening is a deliberate act."""
    assert meals_bot.LIVE_DUPLICATE_APPROVAL_STATUSES == ("pending",)
