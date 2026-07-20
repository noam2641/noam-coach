"""B6 — ARCH-09 (proactive cancellation suppression) + ARCH-11 (restart resume).

ARCH-09 resolved contract: same-day workout CANCELLATION suppresses proactive
prompts that assume the workout is still pending (workout_prompt,
motivation_pre_workout) — without reinterpreting the cancellation as a
completion (distinct suppression reasons preserve the completed / cancelled /
pending three-way), before any delivery.attempted and before the send-budget
claim. Required trace: decision.fallback_selected(reason=workout_cancelled_today),
zero delivery.attempted.

ARCH-11 required harness: flow active → simulated process restart (real
load_pending_state, caches lost) → next interaction → resume offer rendered →
controls reference the persisted flow identity (active_flow.flow_id).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import coach_bot
import conversation
import event_log
from db import Database
from helpers import utc_now
from noam_coach.observability import ObservabilityMode, set_mode
from noam_coach.observability.emit import reset_observability_health
from noam_coach.observability.modes import reset_mode
from noam_coach.services.flow_resume import (
    install_restart_resume,
    uninstall_restart_resume,
)

USER_ID = 1


class FakeQuery:
    def __init__(self, message_id: int = 500) -> None:
        self.message = SimpleNamespace(
            message_id=message_id,
            chat=SimpleNamespace(id=USER_ID),
        )
        self.from_user = SimpleNamespace(id=USER_ID)
        self.edits: list[str] = []
        self.markups: list[Any] = []

    async def edit_message_text(
        self, text: str, reply_markup: Any = None, parse_mode: str | None = None
    ) -> None:
        self.edits.append(text)
        self.markups.append(reply_markup)

    async def answer(self, *a: Any, **k: Any) -> None:
        return None


@pytest.fixture
async def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Database:
    database = Database(str(tmp_path / "b6.db"))
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
    uninstall_restart_resume()
    reset_mode()
    reset_observability_health()


def _events(events: list[Any], name: str) -> list[Any]:
    return [e for e in events if e.event == name]


# ---------------------------------------------------------------------------
# ARCH-09 — suppression at the proactive delivery boundary
# ---------------------------------------------------------------------------


def _patch_delivery_plumbing(monkeypatch: pytest.MonkeyPatch, db: Database) -> None:
    from noam_coach.jobs import proactive as proactive_module

    monkeypatch.setattr(proactive_module, "DB", db, raising=False)

    class _Claim:
        attempt_count = 1

    async def ok_claim(*a: Any, **k: Any) -> Any:
        return _Claim()

    async def noop(*a: Any, **k: Any) -> Any:
        return None

    async def allowed(*a: Any, **k: Any) -> tuple[bool, str]:
        return True, ""

    monkeypatch.setattr(coach_bot, "claim_job_delivery", ok_claim, raising=False)
    monkeypatch.setattr(coach_bot, "complete_job_delivery", noop, raising=False)
    monkeypatch.setattr(coach_bot.data_quality, "can_send_proactive", allowed, raising=False)


async def _set_workout_status(db: Database, status: str) -> None:
    import health_service

    flags = await health_service.get_daily_flags(USER_ID)
    flags["next_meal_workout_status"] = status
    await health_service.set_daily_flags(USER_ID, flags)


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["workout_prompt", "motivation_pre_workout"])
async def test_cancelled_workout_suppresses_pending_assuming_prompts(
    db: Database, monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    """THE required ARCH-09 trace: cancellation → decision reason → zero
    delivery.attempted → sender never runs."""
    from noam_coach.jobs import proactive as proactive_module

    _patch_delivery_plumbing(monkeypatch, db)
    await _set_workout_status(db, "cancelled")

    sends: list[str] = []

    async def sender() -> None:
        sends.append("sent")

    result = await proactive_module.deliver_proactive_message(
        SimpleNamespace(bot=None), key=key, sender=sender, priority=1
    )

    assert result is False
    assert sends == []
    events = await event_log.list_events(db, USER_ID)
    assert _events(events, "delivery.attempted") == []
    fallback = _events(events, "decision.fallback_selected")
    assert len(fallback) == 1
    assert fallback[0].entity_id == key
    assert fallback[0].properties["reason"] == "workout_cancelled_today"


@pytest.mark.asyncio
async def test_completed_workout_suppresses_with_a_distinct_reason(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Completion also suppresses — but with its OWN reason: cancellation is
    never reinterpreted as completion (three-way distinction preserved)."""
    from noam_coach.jobs import proactive as proactive_module

    _patch_delivery_plumbing(monkeypatch, db)
    await _set_workout_status(db, "completed")

    result = await proactive_module.deliver_proactive_message(
        SimpleNamespace(bot=None), key="motivation_pre_workout",
        sender=_never_sender(), priority=1,
    )

    assert result is False
    events = await event_log.list_events(db, USER_ID)
    fallback = _events(events, "decision.fallback_selected")
    assert fallback[0].properties["reason"] == "workout_already_completed"
    assert fallback[0].properties["reason"] != "workout_cancelled_today"


def _never_sender() -> Any:
    async def sender() -> None:
        raise AssertionError("suppressed prompt must never render/send")

    return sender


@pytest.mark.asyncio
async def test_pending_workout_still_delivers(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The legitimate case is untouched: no cancellation, no completion →
    the prompt delivers exactly as before."""
    from noam_coach.jobs import proactive as proactive_module

    _patch_delivery_plumbing(monkeypatch, db)
    sends: list[str] = []

    async def sender() -> None:
        sends.append("sent")

    result = await proactive_module.deliver_proactive_message(
        SimpleNamespace(bot=None), key="workout_prompt", sender=sender, priority=1
    )

    assert result is True
    assert sends == ["sent"]
    events = await event_log.list_events(db, USER_ID)
    assert len(_events(events, "delivery.attempted")) == 1
    assert len(_events(events, "delivery.succeeded")) == 1
    assert _events(events, "decision.fallback_selected") == []


@pytest.mark.asyncio
async def test_cancellation_does_not_suppress_nutrition_prompts(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancelled workout is a claim about the WORKOUT, not about eating:
    nutrition nudges keep flowing."""
    from noam_coach.jobs import proactive as proactive_module

    _patch_delivery_plumbing(monkeypatch, db)
    await _set_workout_status(db, "cancelled")
    sends: list[str] = []

    async def sender() -> None:
        sends.append("sent")

    result = await proactive_module.deliver_proactive_message(
        SimpleNamespace(bot=None), key="intraday_nudge", sender=sender, priority=1
    )

    assert result is True
    assert sends == ["sent"]


@pytest.mark.asyncio
async def test_job_motivation_pre_workout_moment_is_suppressed_end_to_end(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Production path: job_motivation resolves moment=pre_workout from the
    routine profile → the delivery boundary suppresses it after cancellation."""
    from datetime import datetime

    from config import TZ
    from noam_coach.jobs import proactive as proactive_module
    from noam_coach.services import health_jobs as health_jobs_module

    _patch_delivery_plumbing(monkeypatch, db)
    await _set_workout_status(db, "cancelled")

    now = datetime.now(TZ)
    workout_hour = f"{now.hour:02d}:{now.minute:02d}"  # near() is True right now

    async def profile(_user_id: int) -> dict[str, Any]:
        return {"workout": {"typical_hour": workout_hour}, "eating": {}}

    monkeypatch.setattr(coach_bot, "load_routine_profile", profile, raising=False)

    sent: list[str] = []

    async def send_to_user(*a: Any, **k: Any) -> None:
        sent.append("sent")

    monkeypatch.setattr(coach_bot, "send_to_user", send_to_user, raising=False)

    await health_jobs_module.job_motivation(SimpleNamespace(bot=None, job_queue=None))

    assert sent == []
    events = await event_log.list_events(db, USER_ID)
    assert _events(events, "delivery.attempted") == []
    fallback = _events(events, "decision.fallback_selected")
    assert fallback and fallback[0].entity_id == "motivation_pre_workout"
    assert fallback[0].properties["reason"] == "workout_cancelled_today"
    del proactive_module  # imported for parity with siblings


# ---------------------------------------------------------------------------
# ARCH-11 — restart-safe resume
# ---------------------------------------------------------------------------


async def _seed_mid_deferred_plan_flow(db: Database) -> str:
    """User answered 'build me a plan (3x week)' and was asked a deferred
    question: deferred_plan continuation + active question flow persisted."""
    await db.execute(
        """
        INSERT INTO conversation_state(user_id, flow, step, payload, updated_at)
        VALUES(?, 'deferred_plan', '3', '{"remaining_key": "meals_per_day"}', ?)
        """,
        (USER_ID, utc_now()),
    )
    await conversation.set_active_flow(
        db, USER_ID, conversation.FlowName.onboarding_question, step="q_meals_per_day"
    )
    active = await conversation.get_active_flow(db, USER_ID)
    assert active.flow_id
    return active.flow_id


async def _simulate_restart() -> None:
    """The production startup sequence: process-local caches are gone and
    the REAL load_pending_state (wrapped) rebuilds them from the DB."""
    await coach_bot.load_pending_state()


@pytest.mark.asyncio
async def test_restart_preserves_live_deferred_plan_continuation(db: Database) -> None:
    """The destroyer bug: load_pending_state deleted deferred_plan rows as
    legacy although ask_deferred_for_plan/advance_after_answer still use
    them — every restart silently dead-ended the plan flow."""
    await _seed_mid_deferred_plan_flow(db)
    # True-legacy rows ARE still cleaned up.
    await db.execute(
        "INSERT INTO conversation_state(user_id, flow, step, payload, updated_at) "
        "VALUES(?, 'pending_prompt', 'x', '{}', ?)",
        (USER_ID, utc_now()),
    )

    install_restart_resume()
    await _simulate_restart()

    rows = await db.fetch_all(
        "SELECT flow, step FROM conversation_state WHERE user_id=?", (USER_ID,)
    )
    by_flow = {row["flow"]: row["step"] for row in rows}
    assert by_flow.get("deferred_plan") == "3"  # continuation survives
    assert "pending_prompt" not in by_flow  # legacy cleanup preserved


@pytest.mark.asyncio
async def test_resume_offer_rendered_on_first_menu_interaction(db: Database) -> None:
    """The required ARCH-11 harness: flow active -> restart -> next
    interaction -> resume offer rendered -> controls reference the persisted
    flow_id."""
    flow_id = await _seed_mid_deferred_plan_flow(db)

    delegated: list[str] = []

    async def spy(query: Any, user_id: int, data: str) -> bool:
        delegated.append(data)
        return True

    real_handler = coach_bot.handle_menu_callback
    coach_bot.handle_menu_callback = spy
    try:
        install_restart_resume()  # wraps the spy as the delegate
        await _simulate_restart()

        query = FakeQuery()
        handled = await coach_bot.handle_menu_callback(query, USER_ID, "menu:home")
        assert handled is True
        assert delegated == []  # the offer replaced the home render once
        assert any("להמשיך מאיפה שעצרנו" in text for text in query.edits)
        controls = [
            btn.callback_data
            for row in query.markups[-1].inline_keyboard
            for btn in row
        ]
        assert f"resume:continue:f{flow_id}" in controls
        assert f"resume:dismiss:f{flow_id}" in controls
        events = await event_log.list_events(db, USER_ID)
        offer = _events(events, "restart_resume_offer_rendered")
        assert offer and offer[0].properties["flow_id"] == flow_id

        # One offer per restart: the next menu tap goes straight through.
        handled = await coach_bot.handle_menu_callback(FakeQuery(), USER_ID, "menu:home")
        assert handled is True
        assert delegated == ["menu:home"]
    finally:
        uninstall_restart_resume()
        coach_bot.handle_menu_callback = real_handler


@pytest.mark.asyncio
async def test_resume_continue_reenters_the_flow_via_advance_after_answer(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    flow_id = await _seed_mid_deferred_plan_flow(db)
    install_restart_resume()
    await _simulate_restart()

    resumed: list[int] = []

    async def fake_advance(target: Any, user_id: int) -> None:
        resumed.append(user_id)

    from noam_coach.bot import onboarding as onboarding_bot

    monkeypatch.setattr(onboarding_bot, "advance_after_answer", fake_advance)

    query = FakeQuery()
    handled = await coach_bot.handle_menu_callback(
        query, USER_ID, f"resume:continue:f{flow_id}"
    )
    assert handled is True
    assert resumed == [USER_ID]
    assert any("ממשיכים" in text for text in query.edits)
    events = await event_log.list_events(db, USER_ID)
    assert _events(events, "flow_resumed_after_restart")


@pytest.mark.asyncio
async def test_resume_control_with_stale_flow_id_is_refused(db: Database) -> None:
    """A resume button from BEFORE an even newer flow change must not
    continue the wrong flow."""
    await _seed_mid_deferred_plan_flow(db)
    install_restart_resume()
    await _simulate_restart()

    query = FakeQuery()
    handled = await coach_bot.handle_menu_callback(
        query, USER_ID, f"resume:continue:ff-{USER_ID}-deadbeef9999"
    )
    assert handled is True
    assert any("שלב קודם" in text for text in query.edits)
    events = await event_log.list_events(db, USER_ID)
    refused = [
        e for e in _events(events, "validation.failed") if e.entity == "flow_resume"
    ]
    assert refused and refused[0].properties["reason"] == "flow_id_mismatch"
    assert not _events(events, "flow_resumed_after_restart")


@pytest.mark.asyncio
async def test_no_offer_without_resumable_state(db: Database) -> None:
    """Restart with nothing mid-flight: the menu behaves exactly as before."""
    delegated: list[str] = []

    async def spy(query: Any, user_id: int, data: str) -> bool:
        delegated.append(data)
        return True

    real_handler = coach_bot.handle_menu_callback
    coach_bot.handle_menu_callback = spy
    try:
        install_restart_resume()
        await _simulate_restart()
        handled = await coach_bot.handle_menu_callback(FakeQuery(), USER_ID, "menu:home")
        assert handled is True
        assert delegated == ["menu:home"]
    finally:
        uninstall_restart_resume()
        coach_bot.handle_menu_callback = real_handler
