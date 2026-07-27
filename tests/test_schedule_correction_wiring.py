"""A challenge that carries a correction must apply it, not agree with it.

The live defect (2026-07-27 07:01:03): the user wrote "I train on Friday, not
Saturday", the bot replied **"צודק, אשתמש במידע שכבר יש לי"** -- *"you're
right, I'll use the information I already have"* -- and emitted no state
mutation at all. `weekly_availability` still said Saturday. The user repeated
the correction at 07:15:09 and it was discarded again.

W1-8 made the correction applicable; this pins that it is actually *reached*.
Without the wiring the function exists, is tested, and is never called -- so
the defect reproduces exactly as before, which is why this file tests the
handler rather than the service.
"""

from __future__ import annotations

from typing import Any

import pytest

import coach_bot
from noam_coach.bot import assistant as assistant_bot


class _Msg:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def reply_text(self, body: str, reply_markup: Any = None, **_: Any) -> Any:
        del reply_markup
        self.sent.append(body)
        return self


class _Ctx:
    """Minimal FreeTextContext stand-in for the challenge handler."""

    def __init__(self, text: str) -> None:
        self.user_id = 1
        self.text = text
        self.message = _Msg()
        self.update = None
        self.action = "redundant_question_challenge"
        self.slots: dict[str, Any] = {}
        self.intent = None
        self.follow = None

    async def send(self, body: str, markup: Any = None) -> None:
        del markup
        self.message.sent.append(body)


LIVE = "לדעתי אמרתי לו שאני מתאמן בשישי לא בשבת"
AGREEMENT = "צודק, אשתמש במידע שכבר יש לי"


@pytest.mark.asyncio
async def test_a_carried_correction_is_applied_not_acknowledged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression: the reply claimed agreement and changed nothing."""
    calls: list[tuple[int, str]] = []

    async def fake_apply(user_id: int, text: str) -> tuple[bool, str]:
        calls.append((user_id, text))
        return True, "עודכן: מתאמן בשישי ולא בשבת ✅"

    from noam_coach.services import health_jobs

    monkeypatch.setattr(health_jobs, "apply_schedule_correction", fake_apply)

    # The real ActiveFlow, not a hand-rolled stub: the handler reads
    # `is_question`, and a stub that omits it would fail for a reason that
    # has nothing to do with what these tests assert.
    from conversation import ActiveFlow

    async def no_flow(*_a: Any, **_k: Any) -> ActiveFlow:
        return ActiveFlow()

    monkeypatch.setattr(assistant_bot.conversation, "get_active_flow", no_flow)

    ctx = _Ctx(LIVE)
    await assistant_bot._handle_redundant_question_challenge(ctx)

    assert calls == [(1, LIVE)], "the correction must be attempted"
    body = "\n".join(ctx.message.sent)
    assert AGREEMENT not in body, (
        "the bot must not claim agreement when it applied a real change"
    )


@pytest.mark.asyncio
async def test_the_generic_acknowledgement_survives_when_nothing_applies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A challenge carrying no correction keeps the old behaviour."""

    async def fake_apply(user_id: int, text: str) -> tuple[bool, str]:
        del user_id, text
        return False, ""

    from noam_coach.services import health_jobs

    monkeypatch.setattr(health_jobs, "apply_schedule_correction", fake_apply)

    # The real ActiveFlow, not a hand-rolled stub: the handler reads
    # `is_question`, and a stub that omits it would fail for a reason that
    # has nothing to do with what these tests assert.
    from conversation import ActiveFlow

    async def no_flow(*_a: Any, **_k: Any) -> ActiveFlow:
        return ActiveFlow()

    monkeypatch.setattr(assistant_bot.conversation, "get_active_flow", no_flow)

    ctx = _Ctx("כבר אמרתי לך את זה")
    await assistant_bot._handle_redundant_question_challenge(ctx)

    assert AGREEMENT in "\n".join(ctx.message.sent)


@pytest.mark.asyncio
async def test_the_correction_is_attempted_before_any_acknowledgement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ordering is the whole point.

    The defect was an acknowledgement emitted *over* a discarded input, so a
    correction attempted after the reply would fix nothing the user can see.
    """
    order: list[str] = []

    async def fake_apply(user_id: int, text: str) -> tuple[bool, str]:
        del user_id, text
        order.append("apply")
        return True, "עודכן ✅"

    from noam_coach.services import health_jobs

    monkeypatch.setattr(health_jobs, "apply_schedule_correction", fake_apply)

    # The real ActiveFlow, not a hand-rolled stub: the handler reads
    # `is_question`, and a stub that omits it would fail for a reason that
    # has nothing to do with what these tests assert.
    from conversation import ActiveFlow

    async def no_flow(*_a: Any, **_k: Any) -> ActiveFlow:
        return ActiveFlow()

    monkeypatch.setattr(assistant_bot.conversation, "get_active_flow", no_flow)

    ctx = _Ctx(LIVE)
    original_send = ctx.send

    async def tracking_send(body: str, markup: Any = None) -> None:
        order.append("send")
        await original_send(body, markup)

    ctx.send = tracking_send  # type: ignore[method-assign]
    await assistant_bot._handle_redundant_question_challenge(ctx)

    assert order[0] == "apply", f"expected apply before any send, got {order}"


def test_the_handler_calls_the_service_and_owns_no_parser() -> None:
    """The wiring must not duplicate the parsing that W1-8 already owns."""
    import inspect

    source = inspect.getsource(assistant_bot._handle_redundant_question_challenge)
    assert "apply_schedule_correction" in source
    assert "weekday" not in source.lower(), (
        "weekday parsing belongs to health_jobs, not to the dispatch site"
    )


# ---------------------------------------------------------------------------
# End-to-end delta and truthfulness, through the real service.
#
# The handler tests above stub the service to prove the WIRING. These drive
# the real one to prove the SEMANTICS survive that wiring -- a correctly
# wired call to a wrong delta would pass every test above.
# ---------------------------------------------------------------------------
import os  # noqa: E402
import tempfile  # noqa: E402

import user_model  # noqa: E402
from db import Database  # noqa: E402
from noam_coach.services import health_jobs  # noqa: E402

LIVE_DAYS = [0, 2, 5, 6]  # Mon, Wed, Sat, Sun -- the live user's stored set
FRIDAY, SATURDAY = 4, 5


async def _seeded_db() -> Database:
    db = Database(os.path.join(tempfile.mkdtemp(), "wiring.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'T',NULL,'x')"
    )
    slots = [
        {
            "weekday": day,
            "start": "18:00",
            "minutes": 45,
            "available": True,
            "weekday_schema": "monday_first_v1",
        }
        for day in LIVE_DAYS
    ]
    await user_model.set_fact(
        db, 1, "weekly_availability", slots,
        kind=user_model.KIND_FACT, source=user_model.SOURCE_USER, confirmed=True,
    )
    await user_model.set_fact(
        db, 1, "active_training_days", LIVE_DAYS,
        kind=user_model.KIND_FACT, source=user_model.SOURCE_USER, confirmed=True,
    )
    return db


def _bind(monkeypatch: pytest.MonkeyPatch, db: Database) -> None:
    for module in (coach_bot, health_jobs, user_model):
        if hasattr(module, "DB"):
            monkeypatch.setattr(module, "DB", db)


async def _days(db: Database) -> set[int]:
    slots = await user_model.get_value(db, 1, "weekly_availability")
    return {int(s["weekday"]) for s in slots if s.get("available", True)}


@pytest.mark.asyncio
async def test_saturday_is_removed_not_merely_friday_added(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """{0,2,5,6} -> {0,2,4,6}. The displaced day must leave the set."""
    db = await _seeded_db()
    _bind(monkeypatch, db)

    applied, _ = await health_jobs.apply_schedule_correction(1, LIVE)

    assert applied is True
    days = await _days(db)
    assert days == {0, 2, FRIDAY, 6}
    assert SATURDAY not in days, "adding Friday while keeping Saturday is not a fix"


@pytest.mark.asyncio
async def test_the_reply_does_not_claim_the_saved_plan_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Truthfulness: only availability moved, so only availability is claimed.

    The saved plan is deliberately untouched (no supported mutation API --
    see W1-44). A reply implying otherwise would be a defect even though the
    availability change itself is correct.
    """
    db = await _seeded_db()
    _bind(monkeypatch, db)
    await user_model.set_fact(
        db, 1, "active_workout_plan",
        {"sessions": [{"weekday": SATURDAY, "code": "C", "name": "Legs"}]},
        kind=user_model.KIND_FACT, source=user_model.SOURCE_USER, confirmed=True,
    )

    applied, reply = await health_jobs.apply_schedule_correction(1, LIVE)

    assert applied is True
    assert "תוכנית" not in reply, "the reply must not claim the plan was updated"

    plan = await user_model.get_value(db, 1, "active_workout_plan")
    assert [s["weekday"] for s in plan["sessions"]] == [SATURDAY], (
        "the saved plan is intentionally unchanged until W1-44 lands"
    )
