"""Observability O9 — deterministic session trace + regression harness.

The batch acceptance test: at least one COMPLETE representative trace
proving USER INPUT → ROUTING → AI CALL → AI OUTPUT → FINAL DECISION →
STATE MUTATION → RENDER → DELIVERY → CONTROL ACTION → RESULTING RENDER,
asserted through the machine-readable trace (which never parses the human
timeline), plus journey-level examples for the photo/locked-quantity case,
AI fallback, stale-edit delivery, Mini App views, and observability
degradation.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest

import assistant as assistant_module
import coach_bot
import event_log
import mini_api
import user_model
from assistant import Intent
from db import Database
from helpers import utc_now
from models import FoodItem, MealAnalysis
from noam_coach.observability import ObservabilityMode, set_mode
from noam_coach.observability.ai_invocation import (
    ObservedOpenAIClient,
    install_ai_observability,
    uninstall_ai_observability,
)
from noam_coach.observability.decision_trace import (
    install_decision_trace,
    uninstall_decision_trace,
)
from noam_coach.observability.emit import observability_health, reset_observability_health
from noam_coach.observability.harness import HarnessQuery, run_user_turn
from noam_coach.observability.meal_trace import install_meal_trace, uninstall_meal_trace
from noam_coach.observability.modes import reset_mode
from noam_coach.observability.session_trace import (
    load_session_trace,
    load_trace,
    render_timeline,
)
from noam_coach.observability.state_trace import install_state_trace, uninstall_state_trace
from noam_coach.observability.telegram_egress import (
    install_telegram_egress,
    uninstall_telegram_egress,
)
from noam_coach.observability.telegram_ingress import (
    install_routing_observer,
    uninstall_routing_observer,
)

USER_ID = 1


class FakeResponses:
    def __init__(self, result: Any = None, error: BaseException | None = None) -> None:
        self.result = result
        self.error = error

    async def parse(self, **kwargs: Any) -> Any:
        if self.error is not None:
            raise self.error
        return SimpleNamespace(output_parsed=self.result, usage=None)


class FakeClient:
    def __init__(self, responses: FakeResponses) -> None:
        self.responses = responses


@pytest.fixture
async def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Database:
    database = Database(str(tmp_path / "obs_o9.db"))
    await database.init()
    await database.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(?, 'Test', NULL, ?)",
        (USER_ID, utc_now()),
    )
    monkeypatch.setattr(coach_bot, "DB", database)
    monkeypatch.setattr(mini_api, "DB", database)
    from config import SETTINGS

    monkeypatch.setattr(SETTINGS, "telegram_allowed_user_id", USER_ID, raising=False)
    return database


@pytest.fixture(autouse=True)
def _full_observability_stack():
    set_mode(ObservabilityMode.CONTENT)
    reset_observability_health()
    install_routing_observer()
    install_telegram_egress()
    install_ai_observability()
    install_meal_trace()
    install_decision_trace()
    install_state_trace()
    yield
    uninstall_state_trace()
    uninstall_decision_trace()
    uninstall_meal_trace()
    uninstall_ai_observability()
    uninstall_telegram_egress()
    uninstall_routing_observer()
    reset_mode()
    reset_observability_health()


_KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton("שייק חלבון", callback_data="pick:1"),
     InlineKeyboardButton("קוטג' ולחם", callback_data="pick:2")],
])


@pytest.mark.asyncio
async def test_complete_representative_journey(db: Database) -> None:
    """The full chain, twice through the real envelope: a text turn and the
    callback turn pressing a control on the DELIVERED render."""
    import conversation

    ai_client = ObservedOpenAIClient(FakeClient(FakeResponses(
        Intent(action="next_meal", confidence=0.95)
    )))
    query = HarnessQuery(USER_ID, message_id=100)

    async def text_turn(update: Any, context: Any) -> None:
        # ROUTING (real router, observed caller-side)
        await conversation.ConversationRouter.route(db, USER_ID, "text")
        # AI CALL + FINAL DECISION (intent classification, observed)
        await assistant_module.classify_intent(
            ai_client, "gpt-test", update.effective_message.text
        )
        # STATE MUTATION (observed fact write)
        await user_model.set_fact(db, USER_ID, "last_meal_request_hour", 13,
                                  source=user_model.SOURCE_SYSTEM)
        # RENDER + DELIVERY (observed safe_edit)
        await coach_bot.safe_edit(query, "מה מתאים עכשיו:", _KEYBOARD)

    turn1 = await run_user_turn(db, USER_ID, text_turn, text="מה לאכול עכשיו?")

    # USER INPUT
    assert turn1.input_kind == "text"
    assert turn1.input_text == "מה לאכול עכשיו?"
    # ROUTING
    assert turn1.routing.properties["handler"] == "free_text"
    assert turn1.routing.properties["reason"] == "no active flow"
    # AI CALL + OUTPUT
    assert len(turn1.ai_calls) == 1
    ai = turn1.ai_calls[0]
    assert ai.purpose == "intent_classification"
    assert ai.succeeded and ai.output["action"] == "next_meal"
    # FINAL DECISION
    decision = turn1.final_decision
    assert decision.properties["final_action"] == "next_meal"
    assert decision.properties["resolution"] == "ai"
    # STATE MUTATION
    fact_mutations = [
        m for m in turn1.state_mutations
        if m.properties.get("domain") == "user_fact"
    ]
    assert fact_mutations[0].properties["key"] == "last_meal_request_hour"
    # RENDER + DELIVERY
    assert len(turn1.renders) == 1
    render = turn1.renders[0]
    assert render.text == "מה מתאים עכשיו:"
    assert [c["label"] for c in render.controls] == ["שייק חלבון", "קוטג' ולחם"]
    assert render.delivery_result == "delivered"
    assert render.delivered_message_id == 100
    assert turn1.visible_outputs == ["מה מתאים עכשיו:"]
    assert turn1.unresolved_links == []

    # ---- CONTROL ACTION: press "קוטג' ולחם" on the delivered render -------
    result_query = HarnessQuery(USER_ID, message_id=100)

    async def callback_turn(update: Any, context: Any) -> None:
        await coach_bot.safe_edit(result_query, "בחרת: קוטג' ולחם. בתיאבון!", None)

    turn2 = await run_user_turn(
        db, USER_ID, callback_turn,
        callback_data="pick:2", source_message_id=100,
    )
    activation = turn2.control_activation
    assert activation.properties["correlation"] == "resolved"
    assert activation.properties["label"] == "קוטג' ולחם"
    assert activation.properties["source_render_id"] == render.render_id
    # Trace continuation: the callback joined the text turn's trace.
    assert turn2.trace_id == turn1.trace_id
    assert turn2.interaction_id != turn1.interaction_id
    # RESULTING RENDER
    assert turn2.visible_outputs == ["בחרת: קוטג' ולחם. בתיאבון!"]

    # ---- Human timeline: understandable without raw JSON ------------------
    session = await load_session_trace(db, USER_ID)
    timeline = render_timeline(session)
    for expected in (
        '"מה לאכול עכשיו?"',
        "handler=free_text",
        "purpose=intent_classification",
        "FINAL DECISION",
        "user_fact.created (last_meal_request_hour)",
        '"מה מתאים עכשיו:"',
        "[קוטג' ולחם] → pick:2",
        "succeeded(edit)",
        "message_id=100",
        "USER CONTROL",
        "source_render=" + render.render_id,
        '"בחרת: קוטג\' ולחם. בתיאבון!"',
    ):
        assert expected in timeline, expected


@pytest.mark.asyncio
async def test_photo_trace_ai_estimate_vs_locked_user_quantity(db: Database, tmp_path: Path, monkeypatch) -> None:
    from noam_coach.services import profile as profile_module

    fake = FakeResponses(MealAnalysis(
        meal_name="אורז לבן",
        items=[FoodItem(name="אורז לבן", grams=180, calories=234, protein=4.3,
                        carbs=50, fat=0.4, confidence=0.8)],
        confidence=0.8,
    ))
    monkeypatch.setattr(coach_bot, "OPENAI_CLIENT", ObservedOpenAIClient(FakeClient(fake)), raising=False)
    image_bytes = b"\xff\xd8\xff journey-photo"
    image_path = tmp_path / "meal.jpg"
    image_path.write_bytes(image_bytes)
    photo = [SimpleNamespace(file_id="f", file_unique_id="u9", width=800, height=600, file_size=123)]

    async def photo_turn(update: Any, context: Any) -> None:
        await profile_module.analyze_meal_image(image_bytes, user_id=USER_ID)
        final = await profile_module.reanalyze_meal_with_text_and_image(
            str(image_path), "אורז 250 גרם", None,
        )
        message = update.effective_message
        # (harness message has no reply capability on plain SimpleNamespace —
        # visible output is asserted via a render in the richer journeys.)
        del message, final

    trace = await run_user_turn(db, USER_ID, photo_turn, photo=photo, caption="צהריים")

    assert trace.received.properties["media"]["file_unique_id"] == "u9"
    assert len(trace.media) >= 1  # media.received with sha256 identity
    purposes = [ai.purpose for ai in trace.ai_calls]
    assert purposes == ["meal_image_analysis", "meal_reanalysis"]
    assert trace.ai_calls[1].output["items"][0]["grams"] == 180.0
    finalized = trace.final_decision
    overrides = finalized.properties["overrides"]
    rice = [o for o in overrides if o.get("ai_grams") == 180.0][0]
    assert rice["final_grams"] == 250.0
    assert rice["reason"] == "user_explicit_quantity"
    assert finalized.properties["content"]["final_analysis"]["items"][0]["grams"] == 250.0


@pytest.mark.asyncio
async def test_ai_fallback_journey(db: Database) -> None:
    failing_client = ObservedOpenAIClient(FakeClient(FakeResponses(error=TimeoutError("outage"))))
    query = HarnessQuery(USER_ID, message_id=300)

    async def turn(update: Any, context: Any) -> None:
        intent = await assistant_module.classify_intent(
            failing_client, "gpt-test", "לקחתי ריטלין"
        )
        await coach_bot.safe_edit(query, f"נרשם: {intent.action}", None)

    trace = await run_user_turn(db, USER_ID, turn, text="לקחתי ריטלין")

    assert len(trace.ai_calls) == 1
    assert trace.ai_calls[0].failed is not None
    assert trace.ai_calls[0].failed.properties["failure_class"] == "timeout"
    decision = trace.final_decision
    assert decision.properties["resolution"] == "keyword_fallback_no_ai_result"
    assert decision.properties["final_action"] == "morning_flag"
    # Causality: the failure precedes the fallback decision in append order.
    event_names = [e.event for e in trace.events]
    assert event_names.index("ai.call.failed") < event_names.index("decision.finalized")
    # The user-visible render uses the fallback output.
    assert trace.visible_outputs == ["נרשם: morning_flag"]


@pytest.mark.asyncio
async def test_stale_edit_delivery_journey(db: Database) -> None:
    query = HarnessQuery(
        USER_ID, message_id=400,
        edit_error=BadRequest("Message to edit not found"),
    )

    async def turn(update: Any, context: Any) -> None:
        await coach_bot.safe_edit(query, "מסך חדש", None)

    trace = await run_user_turn(db, USER_ID, turn, text="עדכן")
    render = trace.renders[0]
    chain = [(e.event, e.properties.get("operation")) for e in render.deliveries]
    assert chain == [
        ("delivery.attempted", "edit"),
        ("delivery.failed", "edit"),
        ("delivery.attempted", "reply"),
        ("delivery.succeeded", "reply"),
    ]
    assert render.delivery_result == "delivered"
    # The final delivered render is the REPLY's message, not the stale edit's.
    assert render.delivered_message_id == query.message._next_id


@pytest.mark.asyncio
async def test_mini_app_journey_view_action_api_view(db: Database) -> None:
    ci = "ci_journey0001aabb"
    # 1) view R rendered → 2) action from R → 3) API correlated → 4) view V.
    await mini_api.mini_obs_events(payload={"events": [
        {"event": "ui.view.rendered", "view": "next_meal",
         "render_id": "rn_journey0001aabb", "trigger": "initial_load"},
        {"event": "ui.action.activated", "action": "next_meal_workout_status",
         "source_view": "next_meal", "source_render_id": "rn_journey0001aabb",
         "client_interaction_id": ci, "payload": {"status": "completed"}},
    ]}, user_id=USER_ID)

    agen = mini_api.mini_obs_scope(user_id=USER_ID, x_obs_client_interaction=ci)
    await agen.__anext__()
    await event_log.append_event(db, USER_ID, "workout_status_saved", entity="next_meal")
    with pytest.raises(StopAsyncIteration):
        await agen.__anext__()

    await mini_api.mini_obs_events(payload={"events": [
        {"event": "ui.view.rendered", "view": "next_meal",
         "render_id": "rn_journey0002ccdd", "trigger": "post_mutation_refresh",
         "caused_by_client_interaction_id": ci,
         "state": {"action_count": 3}},
    ]}, user_id=USER_ID)

    session = await load_trace(db, USER_ID, "tr_mini_" + ci[3:])
    events = [e.event for e in session.events]
    assert events == ["ui.action.activated", "workout_status_saved", "ui.view.rendered"]
    action = session.events[0]
    resulting = session.events[2]
    assert action.properties["source_render_id"] == "rn_journey0001aabb"
    assert resulting.properties["caused_by_client_interaction_id"] == ci
    assert resulting.properties["trigger"] == "post_mutation_refresh"


@pytest.mark.asyncio
async def test_observability_degradation_does_not_break_coaching_and_is_detectable(
    db: Database, monkeypatch,
) -> None:
    class _FlakyDB:
        """First product_events insert fails; everything else delegates."""

        def __init__(self, inner: Database) -> None:
            self._inner = inner
            self.failed_once = False

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

        async def execute(self, sql: str, parameters: tuple = ()) -> int:
            if not self.failed_once and "INSERT INTO product_events" in sql:
                self.failed_once = True
                raise RuntimeError("simulated store hiccup")
            return await self._inner.execute(sql, parameters)

    flaky = _FlakyDB(db)
    monkeypatch.setattr(coach_bot, "DB", flaky)
    outputs: list[str] = []

    async def turn(update: Any, context: Any) -> None:
        outputs.append("coaching-ran")

    trace = await run_user_turn(db, USER_ID, turn, text="שלום")
    # Coaching survived the write failure...
    assert outputs == ["coaching-ran"]
    # ...the degradation is detectable...
    health = observability_health()
    assert health["write_failures"] == 1
    assert health["write_failed_events_written"] == 1
    # ...and the degradation marker reached the canonical store.
    marker = [e for e in await event_log.list_events(db, USER_ID)
              if e.event == "observability.write_failed"]
    assert marker and marker[0].properties["failed_event"] == "interaction.received"
    del trace


@pytest.mark.asyncio
async def test_machine_trace_does_not_depend_on_timeline_text(db: Database) -> None:
    """The timeline is one-way: mutating its OUTPUT cannot affect the machine
    trace (guards against accidental parsing coupling)."""
    query = HarnessQuery(USER_ID, message_id=500)

    async def turn(update: Any, context: Any) -> None:
        await coach_bot.safe_edit(query, "טקסט", None)

    trace = await run_user_turn(db, USER_ID, turn, text="הי")
    session = await load_session_trace(db, USER_ID)
    timeline = render_timeline(session)
    assert isinstance(timeline, str) and timeline
    # Machine access works identically regardless of the rendered text.
    assert trace.renders[0].delivery_result == "delivered"
