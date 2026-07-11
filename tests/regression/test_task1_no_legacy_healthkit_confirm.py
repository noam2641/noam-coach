"""TASK-1 — the legacy base-data confirmation sequence must not run after the
new post-import HealthKit summary.

After `finish_health_confirm_wizard` on the onboarding path, the bot shows the
new confirmed-planning summary + reduced "🎯 השלם את התוכנית שלי" menu and must
NOT trigger `show_onboarding_basics` ("אישור נתוני בסיס" / "זיהיתי מהנתונים
שיובאו" / "✅ הכול נכון") or expose "מקור"/"אמינות" metadata.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import coach_bot
import user_model
from db import Database
from helpers import utc_now
from noam_coach.bot import onboarding as onboarding_bot
from noam_coach.bot import ui as ui_bot
from noam_coach.services import core as core_services
from noam_coach.services import health_jobs


class FakeMessage:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.markups: list[Any] = []

    async def reply_text(self, text: str, reply_markup: Any = None, parse_mode: str | None = None) -> None:
        del parse_mode
        self.texts.append(text)
        self.markups.append(reply_markup)


class FakeTarget:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.reply_markups: list[Any] = []
        self.message = FakeMessage()

    async def edit_message_text(self, text: str, reply_markup: Any = None, parse_mode: str | None = None) -> None:
        del parse_mode
        self.messages.append(text)
        self.reply_markups.append(reply_markup)

    async def reply_text(self, text: str, reply_markup: Any = None, parse_mode: str | None = None) -> None:
        del parse_mode
        self.messages.append(text)
        self.reply_markups.append(reply_markup)

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        del text, show_alert


async def _db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Database:
    db = Database(str(tmp_path / "task1.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'Test',NULL,?)",
        (utc_now(),),
    )
    for mod in (coach_bot, health_jobs, onboarding_bot, core_services, ui_bot):
        monkeypatch.setattr(mod, "DB", db, raising=False)
    # Confirmed planning data so the new summary has content.
    await user_model.set_fact(db, 1, "weight_kg", 101.8, kind=user_model.KIND_FACT,
                              source=user_model.SOURCE_USER, confirmed=True)
    await user_model.set_fact(db, 1, "training_days_per_week", 3, kind=user_model.KIND_FACT,
                              source=user_model.SOURCE_USER, confirmed=True)
    return db


@pytest.mark.asyncio
async def test_onboarding_path_shows_new_summary_not_legacy_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _db(tmp_path, monkeypatch)
    # The post-wizard flow remembers we came from onboarding.
    await onboarding_bot.set_flow_state(
        1, health_jobs.HEALTH_POST_WIZARD_FLOW, "onboarding",
        {"summary_text": "<b>סיכום הייבוא</b>"},
    )

    legacy_called = False

    async def _fail_legacy(*_a: Any, **_k: Any) -> None:
        nonlocal legacy_called
        legacy_called = True

    # If the legacy sequence were still wired, this would be invoked.
    monkeypatch.setattr(onboarding_bot, "show_onboarding_basics", _fail_legacy, raising=False)
    monkeypatch.setattr(coach_bot, "show_onboarding_basics", _fail_legacy, raising=False)

    target = FakeTarget()
    await health_jobs.finish_health_confirm_wizard(target, 1)

    all_text = " ".join(target.messages + target.message.texts)
    # New summary rendered.
    assert "סיכום הייבוא" in all_text
    # Legacy base-data confirmation markers absent.
    assert "אישור נתוני בסיס" not in all_text
    assert "זיהיתי מהנתונים שיובאו" not in all_text
    assert "הכול נכון" not in all_text
    assert "מקור:" not in all_text
    assert "אמינות:" not in all_text
    assert legacy_called is False
    # Reduced "Complete my plan" continuation is offered.
    callbacks = [
        btn.callback_data
        for markup in (target.reply_markups + target.message.markups)
        if markup is not None
        for row in markup.inline_keyboard
        for btn in row
    ]
    assert "planv2:complete_missing" in callbacks


def test_profile_audit_line_has_no_source_or_confidence_metadata() -> None:
    rows = [{
        "label": "משקל",
        "display_value": "90 ק\"ג",
        "action_required": "none",
        "source": user_model.SOURCE_APPLE_HEALTH,
        "confidence_label": "גבוהה",
    }]
    lines = onboarding_bot._profile_audit_lines(rows)
    joined = "\n".join(lines)
    assert "משקל" in joined
    assert "מקור" not in joined
    assert "אמינות" not in joined
