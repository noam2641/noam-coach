"""Codex-audit regression — an explicit ABC request builds a FULL plan.

Covers (images 11-12 of the audit round):
  * "תוכנית ABC" in free text is detected as a split request — it must route
    to plan building, never to a single-exercise edit screen. Per PATCH-12 /
    TASK-07 an explicit ABC now targets a 4-day "ABC + Full Body" structure
    so a user who declared four training days does not lose a day.
  * When the user's CONFIRMED availability has fewer days than the split
    needs, the bot explains the mismatch and offers a fitting alternative
    instead of silently building an unfollowable plan.
  * When availability fits (or is unknown), a full plan is built and
    presented as such.
"""

from __future__ import annotations

from typing import Any

import pytest

import coach_bot
from noam_coach.bot import assistant as assistant_bot
from noam_coach.services.availability import TrainingAvailability


def test_abc_split_is_detected_in_free_text() -> None:
    # PATCH-12 / TASK-07: explicit ABC targets a 4-day "ABC + Full Body" plan
    # so a user with four declared days keeps all four.
    assert assistant_bot.requested_split_frequency("אני רוצה תוכנית ABC") == 4
    assert assistant_bot.requested_split_frequency("תבנה לי אימון abc") == 4
    assert assistant_bot.requested_split_frequency("תוכנית איי בי סי") == 4
    assert assistant_bot.requested_split_frequency("תוכנית A/B/C") == 4


def test_plain_plan_request_is_not_a_split_request() -> None:
    assert assistant_bot.requested_split_frequency("תבנה לי תוכנית") is None
    assert assistant_bot.requested_split_frequency("3 אימונים בשבוע") is None
    assert assistant_bot.requested_split_frequency("") is None


def _availability(days: int, confirmed: bool) -> TrainingAvailability:
    return TrainingAvailability(
        max_days_per_week=days,
        preferred_days=[],
        preferred_time=None,
        session_minutes=45,
        source="user_confirmed",
        confidence=0.9,
        confirmed=confirmed,
    )


@pytest.mark.asyncio
async def test_gate_offers_alternative_when_confirmed_days_too_few(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_resolve(_db: Any, _user_id: int) -> TrainingAvailability:
        return _availability(days=2, confirmed=True)

    monkeypatch.setattr(
        "noam_coach.services.availability.resolve_availability", fake_resolve
    )
    gate = await assistant_bot._split_availability_gate(1, 3)
    assert gate is not None
    text, keyboard = gate
    assert "2" in text and "ABC" in text
    callbacks = {btn.callback_data for row in keyboard.inline_keyboard for btn in row}
    assert callbacks == {"plan:set:2", "plan:set:3"}


@pytest.mark.asyncio
async def test_gate_passes_when_days_fit_or_unconfirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fits(_db: Any, _user_id: int) -> TrainingAvailability:
        return _availability(days=4, confirmed=True)

    monkeypatch.setattr("noam_coach.services.availability.resolve_availability", fits)
    assert await assistant_bot._split_availability_gate(1, 3) is None

    async def unconfirmed(_db: Any, _user_id: int) -> TrainingAvailability:
        return _availability(days=2, confirmed=False)

    monkeypatch.setattr(
        "noam_coach.services.availability.resolve_availability", unconfirmed
    )
    # Unconfirmed estimates never block an explicit request.
    assert await assistant_bot._split_availability_gate(1, 3) is None


class FakeMessage:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.markups: list[Any] = []

    async def reply_text(self, text: str, reply_markup: Any = None, parse_mode: str | None = None) -> None:
        del parse_mode
        self.texts.append(text)
        self.markups.append(reply_markup)


def _plan_ctx(text: str) -> assistant_bot.FreeTextContext:
    return assistant_bot.FreeTextContext(
        update=None,
        user_id=1,
        message=FakeMessage(),
        text=text,
        intent=None,
        action="build_plan",
        slots={},
        follow=None,
    )


@pytest.mark.asyncio
async def test_abc_request_builds_full_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The full path: ABC in free text → frequency 4 (ABC + Full Body) → a
    complete plan is built and labeled as ABC — not an exercise-parameter
    screen. The user here has four confirmed days, so no day is dropped."""
    built: list[int] = []

    async def fits(_db: Any, _user_id: int) -> TrainingAvailability:
        return _availability(days=4, confirmed=True)

    async def no_gaps(_user_id: int) -> list[str]:
        return []

    async def no_deferred(_message: Any, _user_id: int, _frequency: int) -> bool:
        return False

    async def fake_build(_user_id: int, frequency: int) -> dict[str, Any]:
        built.append(frequency)
        return {"frequency": frequency, "sessions": []}

    monkeypatch.setattr("noam_coach.services.availability.resolve_availability", fits)
    monkeypatch.setattr(coach_bot, "check_plan_readiness", no_gaps)
    monkeypatch.setattr(coach_bot, "ask_deferred_for_plan", no_deferred)
    monkeypatch.setattr(coach_bot, "build_weekly_plan", fake_build)
    # **_ absorbs pain_regions, which the renderer takes so it can mark
    # exercises loading an injured joint. A stub pinned to the exact
    # signature makes every additive parameter a false failure.
    monkeypatch.setattr(
        coach_bot, "format_weekly_plan", lambda _plan, **_: "PLAN-BODY"
    )

    ctx = _plan_ctx("אני רוצה תוכנית ABC")
    handled = await assistant_bot._handle_plan_text_action(ctx)
    assert handled is True
    assert built == [4]
    reply = ctx.message.texts[-1]
    assert "ABC" in reply and "PLAN-BODY" in reply


@pytest.mark.asyncio
async def test_abc_request_blocked_by_confirmed_two_days(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def two_days(_db: Any, _user_id: int) -> TrainingAvailability:
        return _availability(days=2, confirmed=True)

    async def fail_build(_user_id: int, _frequency: int) -> dict[str, Any]:
        raise AssertionError("plan must not be built when the gate blocks")

    monkeypatch.setattr("noam_coach.services.availability.resolve_availability", two_days)
    monkeypatch.setattr(coach_bot, "build_weekly_plan", fail_build)

    ctx = _plan_ctx("תוכנית ABC בבקשה")
    handled = await assistant_bot._handle_plan_text_action(ctx)
    assert handled is True
    reply = ctx.message.texts[-1]
    assert "ABC" in reply
    callbacks = {
        btn.callback_data
        for row in ctx.message.markups[-1].inline_keyboard
        for btn in row
    }
    # The fit (2 confirmed days) and the requested ABC frequency (4) are offered.
    assert "plan:set:2" in callbacks and "plan:set:4" in callbacks
