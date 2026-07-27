"""W1-7 regression — a plan rebuild must not happen silently.

The live defect: at 06:59:45 the user typed "6 ארוחות ביום" (6 MEALS a day, a
nutrition statement). The intent classifier has no meals-per-day label, so it
returned ``build_plan(frequency=6)`` at 0.95 confidence and
``_handle_plan_text_action`` rebuilt the weekly TRAINING plan immediately —
``training_days_per_week`` changed and a new ``active_workout_plan`` was created,
rendering "התוכנית השבועית שלך — 6 אימונים". The user corrected it 47 seconds
later with "4 אימונים בשבוע", forcing a second rebuild.

The fix gates the rebuild behind a confirmation *only when the request
contradicts an existing commitment*. These tests pin both halves of that line:
the first-time path must stay ONE step, and the contradicting path must ask.
"""

from __future__ import annotations

from typing import Any

import pytest

import coach_bot
from noam_coach.bot import assistant as assistant_bot


class FakeMessage:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.markups: list[Any] = []

    async def reply_text(
        self, text: str, reply_markup: Any = None, parse_mode: str | None = None
    ) -> None:
        del parse_mode
        self.texts.append(text)
        self.markups.append(reply_markup)


def _plan_ctx(text: str, frequency: int) -> assistant_bot.FreeTextContext:
    return assistant_bot.FreeTextContext(
        update=None,
        user_id=1,
        message=FakeMessage(),
        text=text,
        intent=None,
        action="build_plan",
        slots={"frequency": frequency},
        follow=None,
    )


def _callbacks(markup: Any) -> set[str]:
    return {btn.callback_data for row in markup.inline_keyboard for btn in row}


@pytest.fixture
def plan_world(monkeypatch: pytest.MonkeyPatch):
    """Stub every dependency of the build_plan path.

    Nothing here touches sqlite — ``user_model.get_fact`` and
    ``planning.get_active_plan`` are replaced outright, so the suite cannot
    create a stray ./noam_coach.db.
    """

    state: dict[str, Any] = {
        "fact": None,  # the stored training_days_per_week fact (or None)
        "plan": None,  # the active workout plan (or None)
        "built": [],  # frequencies actually built
    }

    async def fake_get_fact(_db: Any, _user_id: int, key: str) -> dict[str, Any] | None:
        assert key == "training_days_per_week"
        return state["fact"]

    async def fake_get_active_plan(
        _db: Any, _user_id: int, plan_type: str
    ) -> dict[str, Any] | None:
        assert plan_type == "workout"
        return state["plan"]

    async def no_gaps(_user_id: int) -> list[str]:
        return []

    async def no_deferred(_message: Any, _user_id: int, _frequency: int) -> bool:
        return False

    async def fake_build(_user_id: int, frequency: int) -> dict[str, Any]:
        state["built"].append(frequency)
        return {"frequency": frequency, "sessions": []}

    monkeypatch.setattr(assistant_bot.user_model, "get_fact", fake_get_fact)
    monkeypatch.setattr(assistant_bot.planning, "get_active_plan", fake_get_active_plan)
    monkeypatch.setattr(coach_bot, "check_plan_readiness", no_gaps)
    monkeypatch.setattr(coach_bot, "ask_deferred_for_plan", no_deferred)
    monkeypatch.setattr(coach_bot, "build_weekly_plan", fake_build)
    # **_ absorbs pain_regions so an additive renderer parameter is not a
    # false failure (same rationale as test_abc_plan_request.py).
    monkeypatch.setattr(coach_bot, "format_weekly_plan", lambda _plan, **_: "PLAN-BODY")
    return state


def _confirmed_fact(value: int) -> dict[str, Any]:
    return {"value": value, "confirmed": True, "kind": "fact", "source": "user"}


def _estimate_fact(value: int) -> dict[str, Any]:
    return {"value": value, "confirmed": False, "kind": "estimate", "source": "derived"}


# --------------------------------------------------------------------------
# The normal first-time path must stay ONE step.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_time_plan_build_is_not_interrupted(plan_world) -> None:
    """No confirmed frequency, no active plan → nothing to contradict, so the
    plan is built immediately. Asking here would be a regression."""
    ctx = _plan_ctx("4 אימונים בשבוע", 4)

    handled = await assistant_bot._handle_plan_text_action(ctx)

    assert handled is True
    assert plan_world["built"] == [4]
    assert "PLAN-BODY" in ctx.message.texts[-1]


@pytest.mark.asyncio
async def test_repeat_of_the_same_frequency_is_not_interrupted(plan_world) -> None:
    """Re-requesting the frequency the user already committed to is not
    suspicious — it agrees with the commitment, so it builds straight away."""
    plan_world["fact"] = _confirmed_fact(4)
    plan_world["plan"] = {"payload": {"frequency": 4}}
    ctx = _plan_ctx("4 אימונים בשבוע", 4)

    assert await assistant_bot._handle_plan_text_action(ctx) is True
    assert plan_world["built"] == [4]


@pytest.mark.asyncio
async def test_unconfirmed_estimate_does_not_trigger_confirmation(plan_world) -> None:
    """An unconfirmed estimate is a draft, not a commitment. Contradicting one
    is not evidence of a misclassification, so the build proceeds."""
    plan_world["fact"] = _estimate_fact(3)
    ctx = _plan_ctx("6 אימונים בשבוע", 6)

    assert await assistant_bot._handle_plan_text_action(ctx) is True
    assert plan_world["built"] == [6]


# --------------------------------------------------------------------------
# A contradicting request IS confirmed first.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_contradicting_confirmed_frequency_asks_first(plan_world) -> None:
    """The exact live defect: a confirmed 4 training days a week, and
    "6 ארוחות ביום" arrives misclassified as build_plan(frequency=6)."""
    plan_world["fact"] = _confirmed_fact(4)
    ctx = _plan_ctx("6 ארוחות ביום", 6)

    handled = await assistant_bot._handle_plan_text_action(ctx)

    assert handled is True
    # Nothing was built and nothing was persisted.
    assert plan_world["built"] == []
    reply = ctx.message.texts[-1]
    assert "4" in reply and "6" in reply
    callbacks = _callbacks(ctx.message.markups[-1])
    # Accept and reject are both one tap — the user never retypes.
    assert "plan:set:6" in callbacks
    assert len(callbacks) == 2


@pytest.mark.asyncio
async def test_build_contradicting_active_plan_asks_first(plan_world) -> None:
    """No confirmed fact, but an active plan built for 4 — the plan itself is a
    commitment, matching coach_intelligence's declared-vs-plan conflict rule."""
    plan_world["plan"] = {"payload": {"frequency": 4}}
    ctx = _plan_ctx("6 ארוחות ביום", 6)

    assert await assistant_bot._handle_plan_text_action(ctx) is True
    assert plan_world["built"] == []
    assert "plan:set:6" in _callbacks(ctx.message.markups[-1])


@pytest.mark.asyncio
async def test_rejecting_leaves_plan_and_facts_untouched(plan_world) -> None:
    """The reject button must persist nothing: no build, and its callback is
    plain navigation rather than a write."""
    plan_world["fact"] = _confirmed_fact(4)
    plan_world["plan"] = {"payload": {"frequency": 4}}
    ctx = _plan_ctx("6 ארוחות ביום", 6)

    await assistant_bot._handle_plan_text_action(ctx)

    callbacks = _callbacks(ctx.message.markups[-1])
    reject = callbacks - {"plan:set:6"}
    assert reject == {"menu:plan"}
    # The gate itself never writes, so the confirmed fact and the active plan
    # are exactly as they were.
    assert plan_world["built"] == []
    assert plan_world["fact"] == _confirmed_fact(4)
    assert plan_world["plan"] == {"payload": {"frequency": 4}}


# --------------------------------------------------------------------------
# Confirming proceeds, and confirming twice builds once.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirming_proceeds_to_build(plan_world) -> None:
    """Tapping the confirm button routes to `plan:set:<n>`, which writes the
    confirmed fact and rebuilds — the same thing a confirmed rebuild always
    did. This asserts the gate hands off to a real, routed callback."""
    plan_world["fact"] = _confirmed_fact(4)
    ctx = _plan_ctx("6 ארוחות ביום", 6)
    await assistant_bot._handle_plan_text_action(ctx)

    confirm = next(
        btn
        for row in ctx.message.markups[-1].inline_keyboard
        for btn in row
        if btn.callback_data.startswith("plan:set:")
    )
    assert confirm.callback_data == "plan:set:6"
    # And once the user has committed, the same request no longer contradicts
    # anything, so the build path is one step again.
    plan_world["fact"] = _confirmed_fact(6)
    ctx2 = _plan_ctx("6 אימונים בשבוע", 6)
    assert await assistant_bot._handle_plan_text_action(ctx2) is True
    assert plan_world["built"] == [6]


@pytest.mark.asyncio
async def test_double_confirm_builds_once(plan_world) -> None:
    """Idempotency: `plan:set:` is registered in the callback router's
    debounce prefixes, so a second tap within the window is dropped and the
    plan is not rebuilt twice."""
    from noam_coach.bot import callback_router

    plan_world["fact"] = _confirmed_fact(4)
    ctx = _plan_ctx("6 ארוחות ביום", 6)
    await assistant_bot._handle_plan_text_action(ctx)

    confirm = next(
        btn
        for row in ctx.message.markups[-1].inline_keyboard
        for btn in row
        if btn.callback_data.startswith("plan:set:")
    )
    data = confirm.callback_data
    assert data.startswith(callback_router._DEBOUNCE_PREFIXES)

    callback_router._LAST_CALLBACK.pop((99, data), None)
    first = callback_router._is_duplicate_tap(99, data)
    second = callback_router._is_duplicate_tap(99, data)
    assert first is False, "the first tap must go through"
    assert second is True, "the second tap must be dropped — build once"


@pytest.mark.asyncio
async def test_gate_never_blocks_when_lookups_fail(
    plan_world, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A gate that raises must not become a way to lose plan building."""

    async def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("db down")

    monkeypatch.setattr(assistant_bot.user_model, "get_fact", boom)
    monkeypatch.setattr(assistant_bot.planning, "get_active_plan", boom)
    ctx = _plan_ctx("4 אימונים בשבוע", 4)

    assert await assistant_bot._handle_plan_text_action(ctx) is True
    assert plan_world["built"] == [4]
