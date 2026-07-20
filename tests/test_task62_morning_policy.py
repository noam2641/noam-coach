"""TASK-62 — proactive morning briefing + opt-in daily menu delivery.

Required traces: briefing generated and delivered once; the full menu is
NOT delivered without a confirmed opt-in and IS delivered with one;
retry/restart never duplicates; a failed delivery stays visible as failed.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import coach_bot
import event_log
import user_model
from db import Database
from helpers import utc_now
from noam_coach.observability import ObservabilityMode, set_mode
from noam_coach.observability.emit import reset_observability_health
from noam_coach.observability.modes import reset_mode
from noam_coach.services.morning_policy import (
    MORNING_MENU_OPTIN_FACT,
    install_morning_policy,
    job_morning_briefing,
    morning_menu_opted_in,
    set_morning_menu_optin,
    uninstall_morning_policy,
)

USER_ID = 1


@pytest.fixture
async def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Database:
    database = Database(str(tmp_path / "task62.db"))
    await database.init()
    await database.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(?, 'Test', NULL, ?)",
        (USER_ID, utc_now()),
    )
    monkeypatch.setattr(coach_bot, "DB", database)
    from config import SETTINGS

    monkeypatch.setattr(SETTINGS, "telegram_allowed_user_id", USER_ID, raising=False)
    return database


@pytest.fixture(autouse=True)
def _isolation():
    set_mode(ObservabilityMode.CONTENT)
    reset_observability_health()
    yield
    uninstall_morning_policy()
    reset_mode()
    reset_observability_health()


def _patch_boundary_plumbing(monkeypatch: pytest.MonkeyPatch, db: Database) -> None:
    """Real deliver_proactive_message; permissive quality gate; REAL daily
    claims (the dedup semantics under test) via the test db."""
    from noam_coach.jobs import proactive as proactive_module

    monkeypatch.setattr(proactive_module, "DB", db, raising=False)

    async def allowed(*a: Any, **k: Any) -> tuple[bool, str]:
        return True, ""

    monkeypatch.setattr(coach_bot.data_quality, "can_send_proactive", allowed, raising=False)
    # Deterministic at any wall clock: disable quiet hours (equal start/end).
    from config import SETTINGS

    monkeypatch.setattr(SETTINGS, "proactive_quiet_start", "03:00", raising=False)
    monkeypatch.setattr(SETTINGS, "proactive_quiet_end", "03:00", raising=False)


def _events(events: list[Any], name: str) -> list[Any]:
    return [e for e in events if e.event == name]


# ---------------------------------------------------------------------------
# The opt-in gate at the delivery boundary
# ---------------------------------------------------------------------------


def _fake_briefing_env(monkeypatch: pytest.MonkeyPatch) -> tuple[list[str], list[Any]]:
    sent_texts: list[str] = []
    sent_markups: list[Any] = []

    async def fake_briefing(user_id: int, ctx: Any = None) -> str:
        return "בוקר טוב — נשארו 1800 קל׳ | 140 ג׳ חלבון. אימון ב-18:00."

    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    async def fake_keyboard(user_id: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton("😴 שינה טובה", callback_data="chk:sleep:good")]]
        )

    async def fake_send(context: Any, text: str, reply_markup: Any = None, **k: Any) -> Any:
        sent_texts.append(text)
        sent_markups.append(reply_markup)
        return SimpleNamespace(message_id=101, chat=SimpleNamespace(id=USER_ID))

    monkeypatch.setattr(coach_bot, "build_morning_briefing_text", fake_briefing, raising=False)
    monkeypatch.setattr(coach_bot, "morning_checkin_keyboard", fake_keyboard, raising=False)
    monkeypatch.setattr(coach_bot, "send_to_user", fake_send, raising=False)
    return sent_texts, sent_markups



@pytest.mark.asyncio
async def test_full_menu_not_attempted_without_optin(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    """Default OFF: the scheduled job never attempts the menu delivery —
    only the decision trace records the suppression."""
    _patch_boundary_plumbing(monkeypatch, db)
    _fake_briefing_env(monkeypatch)
    menu_runs: list[str] = []

    async def fake_job_morning(context: Any) -> None:
        menu_runs.append("ran")

    monkeypatch.setattr(coach_bot, "job_morning", fake_job_morning, raising=False)
    await job_morning_briefing(SimpleNamespace(bot=None, job_queue=None))

    assert menu_runs == []
    events = await event_log.list_events(db, USER_ID)
    assert not [e for e in _events(events, "delivery.attempted") if e.properties.get("key") == "morning_menu"]
    fallback = _events(events, "decision.fallback_selected")
    assert any(e.properties.get("reason") == "menu_optin_absent" for e in fallback)


@pytest.mark.asyncio
async def test_full_menu_flows_with_confirmed_optin(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_boundary_plumbing(monkeypatch, db)
    _fake_briefing_env(monkeypatch)
    await set_morning_menu_optin(db, USER_ID, True)
    menu_runs: list[str] = []

    async def fake_job_morning(context: Any) -> None:
        menu_runs.append("ran")

    monkeypatch.setattr(coach_bot, "job_morning", fake_job_morning, raising=False)
    await job_morning_briefing(SimpleNamespace(bot=None, job_queue=None))
    assert menu_runs == ["ran"]


@pytest.mark.asyncio
async def test_unconfirmed_optin_estimate_stays_off(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    """B11 read policy: an unconfirmed derived opt-in must never drive sends."""
    _patch_boundary_plumbing(monkeypatch, db)
    _fake_briefing_env(monkeypatch)
    await user_model.set_fact(
        db, USER_ID, MORNING_MENU_OPTIN_FACT, True,
        kind=user_model.KIND_ESTIMATE, source=user_model.SOURCE_DERIVED,
        confirmed=False,
    )
    assert await morning_menu_opted_in(db, USER_ID) is False
    menu_runs: list[str] = []

    async def fake_job_morning(context: Any) -> None:
        menu_runs.append("ran")

    monkeypatch.setattr(coach_bot, "job_morning", fake_job_morning, raising=False)
    await job_morning_briefing(SimpleNamespace(bot=None, job_queue=None))
    assert menu_runs == []


@pytest.mark.asyncio
async def test_optout_immediately_blocks_the_next_morning(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_boundary_plumbing(monkeypatch, db)
    _fake_briefing_env(monkeypatch)
    await set_morning_menu_optin(db, USER_ID, True)
    await set_morning_menu_optin(db, USER_ID, False)
    menu_runs: list[str] = []

    async def fake_job_morning(context: Any) -> None:
        menu_runs.append("ran")

    monkeypatch.setattr(coach_bot, "job_morning", fake_job_morning, raising=False)
    await job_morning_briefing(SimpleNamespace(bot=None, job_queue=None))
    assert menu_runs == []


# ---------------------------------------------------------------------------
# The scheduled morning job
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scheduled_morning_is_briefing_only_by_default(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Briefing delivered once; NO menu message; retry produces no duplicate."""
    _patch_boundary_plumbing(monkeypatch, db)
    sent_texts, sent_markups = _fake_briefing_env(monkeypatch)
    context = SimpleNamespace(bot=SimpleNamespace(send_message=None), job_queue=None)

    await job_morning_briefing(context)
    # Restart/retry: run again — the daily claim dedups everything.
    await job_morning_briefing(context)

    briefings = [t for t in sent_texts if "בוקר טוב" in t]
    assert len(briefings) == 1  # delivered exactly once
    events = await event_log.list_events(db, USER_ID)
    succeeded = [
        e for e in _events(events, "delivery.succeeded")
        if e.properties.get("key") == "morning_checkin"
    ]
    assert len(succeeded) == 1
    # No menu delivery happened (no opt-in).
    assert not [
        e for e in _events(events, "delivery.attempted")
        if e.properties.get("key") == "morning_menu"
    ]
    fallback = _events(events, "decision.fallback_selected")
    assert any(e.properties.get("reason") == "menu_optin_absent" for e in fallback)
    # The briefing keyboard exposes the opt-in toggle.
    toggle_callbacks = [
        button.callback_data
        for markup in sent_markups if markup is not None
        for row in markup.inline_keyboard for button in row
    ]
    assert "morningmenu:optin" in toggle_callbacks


@pytest.mark.asyncio
async def test_failed_briefing_delivery_is_visible_as_failed(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_boundary_plumbing(monkeypatch, db)
    _fake_briefing_env(monkeypatch)

    async def broken_send(*a: Any, **k: Any) -> None:
        raise RuntimeError("telegram down")

    monkeypatch.setattr(coach_bot, "send_to_user", broken_send, raising=False)

    async def fake_job_morning(context: Any) -> None:
        return None

    monkeypatch.setattr(coach_bot, "job_morning", fake_job_morning, raising=False)
    monkeypatch.setattr(coach_bot, "notify_admin", _async_noop, raising=False)
    context = SimpleNamespace(bot=None, job_queue=None)
    await job_morning_briefing(context)

    events = await event_log.list_events(db, USER_ID)
    failed = [
        e for e in _events(events, "delivery.failed")
        if e.properties.get("key") == "morning_checkin"
    ]
    assert failed  # visible as failed…
    succeeded = [
        e for e in _events(events, "delivery.succeeded")
        if e.properties.get("key") == "morning_checkin"
    ]
    assert not succeeded  # …never as sent


async def _async_noop(*args: Any, **kwargs: Any) -> None:
    return None


# ---------------------------------------------------------------------------
# The Telegram toggle
# ---------------------------------------------------------------------------


class FakeQuery:
    def __init__(self) -> None:
        self.message = SimpleNamespace(message_id=7, chat=SimpleNamespace(id=USER_ID))
        self.edits: list[str] = []

    async def edit_message_text(self, text: str, reply_markup: Any = None, parse_mode: Any = None) -> None:
        self.edits.append(text)

    async def answer(self, *a: Any, **k: Any) -> None:
        return None


@pytest.mark.asyncio
async def test_toggle_callbacks_set_the_confirmed_fact(db: Database) -> None:
    install_morning_policy()

    await coach_bot.handle_menu_callback(FakeQuery(), USER_ID, "morningmenu:optin")
    fact = await user_model.get_fact(db, USER_ID, MORNING_MENU_OPTIN_FACT)
    assert fact["value"] is True and fact["confirmed"]
    assert await morning_menu_opted_in(db, USER_ID) is True

    await coach_bot.handle_menu_callback(FakeQuery(), USER_ID, "morningmenu:optout")
    assert await morning_menu_opted_in(db, USER_ID) is False
