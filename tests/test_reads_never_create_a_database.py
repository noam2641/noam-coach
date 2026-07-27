"""A read must never create the database it is reading from.

sqlite creates a database on connect, so `fetch_one`/`fetch_all` against a
path that does not exist silently materialises an empty file. `database_path`
defaults to `./noam_coach.db` -- the repo root -- and CI has no `.env`, so any
read reached before `init()` leaves a stray file there and the
forbidden-files guards fail.

This reached CI **twice**, from two unrelated features:
  1. a pain-region lookup while rendering the weekly plan (PR #36)
  2. a plan-rebuild confirmation gate (PR #49)

Each was fixed at its own call site. There are ~67 read sites in the product,
so guarding them one at a time is a losing race -- the third one would arrive
in some future feature written by someone who never saw the first two. The
guard therefore lives in `Database` itself, and these tests pin it there.

Writes are deliberately NOT guarded: they are expected to run against an
initialised database, and silently dropping one would hide a real bug.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from db import Database


@pytest.mark.asyncio
async def test_fetch_one_does_not_create_the_file(tmp_path: Path) -> None:
    missing = tmp_path / "never-created.db"
    db = Database(str(missing))

    assert await db.fetch_one("SELECT 1") is None
    assert not missing.exists()


@pytest.mark.asyncio
async def test_fetch_all_does_not_create_the_file(tmp_path: Path) -> None:
    missing = tmp_path / "never-created.db"
    db = Database(str(missing))

    assert await db.fetch_all("SELECT 1") == []
    assert not missing.exists()


@pytest.mark.asyncio
async def test_reads_work_normally_once_initialised(tmp_path: Path) -> None:
    """The guard must be invisible to every real caller."""
    path = tmp_path / "real.db"
    db = Database(str(path))
    await db.init()

    assert path.exists()
    assert await db.fetch_all("SELECT name FROM sqlite_master LIMIT 1") != []
    assert await db.fetch_one("SELECT 1 AS n") == {"n": 1}


@pytest.mark.asyncio
async def test_an_in_memory_database_is_never_skipped(tmp_path: Path) -> None:
    """`:memory:` has no file to create, so it must not be short-circuited."""
    db = Database(":memory:")
    assert db._is_materialised() is True


@pytest.mark.asyncio
async def test_init_still_creates_the_database(tmp_path: Path) -> None:
    """The guard is on reads only -- init must keep creating."""
    path = tmp_path / "fresh.db"
    db = Database(str(path))
    assert not path.exists()

    await db.init()

    assert path.exists(), "init() is the one place that may create the file"


@pytest.mark.asyncio
async def test_the_repo_root_stays_clean_after_a_speculative_read() -> None:
    """The exact CI failure: a read against the default path.

    `database_path` defaults to `./noam_coach.db`. A read reached before
    init() used to leave that file in the repo root, which is what the
    `stray database artifacts` guards detect.
    """
    repo_root = Path(__file__).resolve().parents[1]
    stray = repo_root / "noam_coach.db"
    assert not stray.exists(), "precondition: the repo root starts clean"

    db = Database("./noam_coach.db")
    await db.fetch_one("SELECT 1")
    await db.fetch_all("SELECT 1")

    assert not stray.exists(), "a read must not create ./noam_coach.db"
