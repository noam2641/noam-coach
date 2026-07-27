"""Four independent infrastructure defects found in the 2026-07-26 session.

1. **Proactive messages were permanently blocked.** active_goal_quality
   queried ``status='active'``, but a goal the user accepts before final
   approval is stored as ``active_provisional``. With no row matching, the
   score was 0.0 ("no active goal") rather than 0.55, and can_send_proactive
   refused everything with ``goal_quality_insufficient`` -- silently
   disabling morning_menu, evening, weekly_summary, overpace_alert and
   intraday_nudge. planning.active_goal and the db.py migrations already
   treated both statuses as active; this one query did not.

2. **product_events had no retention.** The canonical interaction trace grows
   at ~1,150 rows per hour of active use and was the only high-volume table
   with no cutoff at all.

3. **The AI failure event never recorded why.** Only error_type was kept, so
   months of BadRequestError said "it failed" and never "a free-form dict is
   illegal under strict mode".

4. **The Mini App button could never work.** Telegram validates a web-app URL
   server-side and rejects non-HTTPS, so the dev localhost exemption produced
   a button whose every tap failed with BadRequest.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import data_quality
from config import SETTINGS
from db import Database
from helpers import utc_now


async def _make_db(tmp_path: Path, name: str) -> Database:
    db = Database(str(tmp_path / f"{name}.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1, 'Test', NULL, ?)",
        (utc_now(),),
    )
    return db


async def _goal(db: Database, status: str, *, source: str = "user_approved") -> None:
    await db.execute(
        """
        INSERT INTO goal_versions(
            user_id, calories, protein, steps, phase, status, source, created_at
        ) VALUES(1, 2080, 170, 8000, 'fat_loss_muscle_retention', ?, ?, ?)
        """,
        (status, source, utc_now()),
    )


# ---------------------------------------------------------------------------
# 1. goal quality / proactive gating
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_provisional_goal_counts_as_an_active_goal(
    tmp_path: Path,
) -> None:
    """The regression: the only goal is provisional, so nothing could send."""
    db = await _make_db(tmp_path, "provisional")
    await _goal(db, "active_provisional", source="user_approved_provisional")

    report = await data_quality.active_goal_quality(db, 1)

    assert report.score > 0.0, "a provisional goal is still an active goal"
    assert not any(i.code == "no_active_goal" for i in report.issues)


@pytest.mark.asyncio
async def test_provisional_goal_clears_the_proactive_threshold(
    tmp_path: Path,
) -> None:
    """0.55 vs 0.9 matters: can_send_proactive requires >= 0.6."""
    db = await _make_db(tmp_path, "threshold")
    await _goal(db, "active_provisional", source="user_approved_provisional")

    report = await data_quality.active_goal_quality(db, 1)

    assert report.score >= 0.6, (
        f"score {report.score} still blocks every gated proactive message"
    )


@pytest.mark.asyncio
async def test_a_genuinely_provisional_goal_is_still_flagged(
    tmp_path: Path,
) -> None:
    """Widening the status filter must not stop flagging real provisionals."""
    db = await _make_db(tmp_path, "flagged")
    await _goal(db, "active_provisional", source="computed_provisional")

    report = await data_quality.active_goal_quality(db, 1)

    assert any(i.code == "provisional_goal" for i in report.issues)
    assert report.score == pytest.approx(0.55)


@pytest.mark.asyncio
async def test_no_goal_at_all_is_still_reported(tmp_path: Path) -> None:
    db = await _make_db(tmp_path, "none")

    report = await data_quality.active_goal_quality(db, 1)

    assert report.score == 0.0
    assert any(i.code == "no_active_goal" for i in report.issues)


@pytest.mark.asyncio
async def test_a_fully_active_goal_scores_highest(tmp_path: Path) -> None:
    db = await _make_db(tmp_path, "active")
    await _goal(db, "active")

    report = await data_quality.active_goal_quality(db, 1)

    assert report.score == pytest.approx(0.9)


@pytest.mark.asyncio
async def test_superseded_goals_do_not_count(tmp_path: Path) -> None:
    """Only active statuses -- a retired goal must not revive the gate."""
    db = await _make_db(tmp_path, "superseded")
    await _goal(db, "superseded")

    report = await data_quality.active_goal_quality(db, 1)

    assert report.score == 0.0


# ---------------------------------------------------------------------------
# 2. product_events retention
# ---------------------------------------------------------------------------
def test_product_events_has_a_retention_setting() -> None:
    assert SETTINGS.product_events_retention_days > 0


def test_product_events_retention_is_generous() -> None:
    """It is the trace historical reconstruction reads -- do not purge it hard."""
    assert SETTINGS.product_events_retention_days >= 180


@pytest.mark.asyncio
async def test_old_product_events_are_purged_and_recent_ones_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import datetime, timedelta, timezone

    import retention

    db = await _make_db(tmp_path, "retention")
    monkeypatch.setattr(retention._db, "DB", db)

    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=SETTINGS.product_events_retention_days + 5)).isoformat()
    recent = (now - timedelta(days=1)).isoformat()
    for created_at, event in ((old, "stale.event"), (recent, "fresh.event")):
        await db.execute(
            """
            INSERT INTO product_events(user_id, event, entity, properties, created_at)
            VALUES(1, ?, 'system', '{}', ?)
            """,
            (event, created_at),
        )

    deleted = await retention.cleanup_operational_data_once()

    assert deleted["product_events"] == 1
    rows = await db.fetch_all("SELECT event FROM product_events")
    assert [r["event"] for r in rows] == ["fresh.event"]


# ---------------------------------------------------------------------------
# 3. AI failure message
# ---------------------------------------------------------------------------
def test_ai_error_message_is_captured() -> None:
    from noam_coach.observability.ai_invocation import _error_message

    exc = ValueError("Invalid schema for response_format: additionalProperties")
    assert "additionalProperties" in _error_message(exc)


def test_ai_error_message_is_bounded() -> None:
    from noam_coach.observability.ai_invocation import (
        _MAX_ERROR_MESSAGE_CHARS,
        _error_message,
    )

    message = _error_message(RuntimeError("x" * 5000))
    assert len(message) <= _MAX_ERROR_MESSAGE_CHARS + 1  # + the ellipsis


def test_ai_error_message_survives_a_broken_exception() -> None:
    """A __str__ that raises must not mask the failure it describes."""
    from noam_coach.observability.ai_invocation import _error_message

    class Hostile(Exception):
        def __str__(self) -> str:
            raise RuntimeError("nope")

    assert _error_message(Hostile()) == ""


# ---------------------------------------------------------------------------
# 4. Mini App web-app button
# ---------------------------------------------------------------------------
def _set_env(monkeypatch: pytest.MonkeyPatch, env: str, url: str) -> None:
    monkeypatch.setattr(SETTINGS, "app_env", env)
    monkeypatch.setattr(SETTINGS, "public_base_url", url)


def test_localhost_never_yields_a_web_app_button(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The live failure: Telegram rejected http://127.0.0.1:8000 outright."""
    from noam_coach.api.mini_auth import _is_valid_public_url

    _set_env(monkeypatch, "dev", "http://127.0.0.1:8000")
    assert _is_valid_public_url(
        "http://127.0.0.1:8000", for_web_app_button=True
    ) is False


def test_localhost_still_works_for_non_button_use_in_dev(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local development of the Mini App itself must keep working."""
    from noam_coach.api.mini_auth import _is_valid_public_url

    _set_env(monkeypatch, "dev", "http://127.0.0.1:8000")
    assert _is_valid_public_url("http://127.0.0.1:8000") is True


def test_a_public_https_url_yields_a_button(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from noam_coach.api.mini_auth import _is_valid_public_url

    _set_env(monkeypatch, "dev", "https://coach.example.org")
    assert _is_valid_public_url(
        "https://coach.example.org", for_web_app_button=True
    ) is True


def test_mini_app_url_returns_none_for_a_button_on_localhost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from noam_coach.api import mini_auth

    _set_env(monkeypatch, "dev", "http://127.0.0.1:8000")
    assert mini_auth.mini_app_url(1, for_web_app_button=True) is None


def test_plain_http_is_refused_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from noam_coach.api.mini_auth import _is_valid_public_url

    _set_env(monkeypatch, "production", "http://coach.example.org")
    assert _is_valid_public_url("http://coach.example.org") is False
