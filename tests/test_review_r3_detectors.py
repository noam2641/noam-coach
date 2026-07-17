"""Review batch R3 — deterministic signal detectors (incl. false-positive proofs).

Each detector is exercised over synthetic-but-contract-exact ProductEvent
sequences via ``build_session_trace`` (the same model the live package
builder uses): a positive case proving detection with exact evidence
references, and a negative case proving healthy behavior stays silent.
"""

from __future__ import annotations

import json
from itertools import count
from typing import Any

from event_log import ProductEvent
from noam_coach.observability import taxonomy
from noam_coach.observability.session_review import (
    DETECTOR_SET_VERSION,
    DETECTORS,
    detector_inventory,
    run_detectors,
)
from noam_coach.observability.session_trace import build_session_trace

USER_ID = 1
_IDS = count(1)


def _event(
    event: str,
    *,
    interaction: str = "in_1",
    trace: str | None = "tr_1",
    properties: dict[str, Any] | None = None,
    entity: str = "system",
    outcome: str | None = None,
) -> ProductEvent:
    return ProductEvent(
        id=next(_IDS),
        user_id=USER_ID,
        event=event,
        entity=entity,
        entity_id=None,
        flow_id=None,
        flow_version=None,
        source="bot",
        properties=properties or {},
        before=None,
        after=None,
        created_at="2026-07-10T08:00:00+00:00",
        trace_id=trace,
        interaction_id=interaction,
        outcome=outcome,
    )


def _received(interaction: str, *, text: str | None = "hi", callback: str | None = None,
              source_message_id: int | None = None, trace: str = "tr_1") -> ProductEvent:
    props: dict[str, Any] = {"kind": "callback" if callback else "text"}
    if callback:
        props["callback_data"] = callback
        props["source_message_id"] = source_message_id
    elif text is not None:
        props["content"] = {"text": text}
    return _event(taxonomy.INTERACTION_RECEIVED, interaction=interaction, trace=trace,
                  properties=props)


def _delivered_render(interaction: str, render_id: str = "rn_1", trace: str = "tr_1") -> list[ProductEvent]:
    return [
        _event(taxonomy.UI_RENDER_PREPARED, interaction=interaction, trace=trace,
               properties={"render_id": render_id}),
        _event(taxonomy.DELIVERY_SUCCEEDED, interaction=interaction, trace=trace,
               properties={"render_id": render_id, "operation": "reply"}, outcome="delivered"),
    ]


def _signals(events: list[ProductEvent]) -> dict[str, Any]:
    result = run_detectors(build_session_trace(USER_ID, events))
    return {signal["detector_id"]: signal for signal in result["signals"]}


def test_registry_metadata_is_complete() -> None:
    inventory = detector_inventory()
    assert len(inventory) == len(DETECTORS)
    for meta in inventory:
        assert meta["detector_id"] and isinstance(meta["version"], int)
        assert meta["severity_hint"] in ("critical", "high", "medium", "low")
        assert meta["description"] and meta["supported_contract"] and meta["limitations"]
    ids = [meta["detector_id"] for meta in inventory]
    assert len(ids) == len(set(ids))  # stable unique ids


def test_result_is_json_safe_and_versioned() -> None:
    result = run_detectors(build_session_trace(USER_ID, []))
    assert result["detector_set_version"] == DETECTOR_SET_VERSION
    assert result["signals"] == []
    json.dumps(result)  # committable artifact must serialize


def test_error_captured_detected_with_evidence() -> None:
    crash = _event(taxonomy.ERROR_CAPTURED, properties={
        "boundary": "telegram_handler:text", "error_type": "ValueError",
    })
    events = [_received("in_1"), crash, *_delivered_render("in_1")]
    signal = _signals(events)["error_captured"]
    assert signal["severity_hint"] == "critical"
    assert signal["count"] == 1
    assert signal["occurrences"][0]["event_ids"] == [crash.id]
    assert signal["occurrences"][0]["detail"]["error_type"] == "ValueError"


def test_healthy_interaction_produces_no_signals() -> None:
    events = [
        _received("in_1"),
        _event(taxonomy.ROUTING_DECIDED, properties={"handler": "free_text"}),
        _event(taxonomy.AI_CALL_STARTED, properties={"ai_call_id": "ai_1", "purpose": "intent_classification"}),
        _event(taxonomy.AI_CALL_COMPLETED, properties={"ai_call_id": "ai_1", "purpose": "intent_classification"}),
        _event(taxonomy.VALIDATION_COMPLETED, outcome="ok"),
        _event(taxonomy.DECISION_FINALIZED, properties={"resolution": "accepted"}),
        _event(taxonomy.STATE_MUTATED, properties={"domain": "meals", "action": "saved"}),
        *_delivered_render("in_1"),
    ]
    assert _signals(events) == {}


def test_ai_call_failed_and_unresolved() -> None:
    events = [
        _received("in_1"),
        _event(taxonomy.AI_CALL_STARTED, properties={"ai_call_id": "ai_f", "purpose": "menu"}),
        _event(taxonomy.AI_CALL_FAILED, properties={
            "ai_call_id": "ai_f", "purpose": "menu", "error_type": "APIError", "failure_class": "provider",
        }),
        _event(taxonomy.AI_CALL_STARTED, properties={"ai_call_id": "ai_u", "purpose": "summary"}),
        _event(taxonomy.STATE_MUTATED, properties={"domain": "x", "action": "y"}),
        *_delivered_render("in_1"),
    ]
    signals = _signals(events)
    assert signals["ai_call_failed"]["occurrences"][0]["detail"]["error_type"] == "APIError"
    assert signals["ai_call_unresolved"]["occurrences"][0]["detail"]["ai_call_id"] == "ai_u"


def test_completed_ai_call_is_not_unresolved() -> None:
    events = [
        _received("in_1"),
        _event(taxonomy.AI_CALL_STARTED, properties={"ai_call_id": "ai_1", "purpose": "p"}),
        _event(taxonomy.AI_CALL_COMPLETED, properties={"ai_call_id": "ai_1", "purpose": "p"}),
        *_delivered_render("in_1"),
    ]
    signals = _signals(events)
    assert "ai_call_unresolved" not in signals
    assert "ai_call_failed" not in signals


def test_delivery_failure_terminal_vs_recovered() -> None:
    # Terminal failure.
    failing = [
        _received("in_1"),
        _event(taxonomy.UI_RENDER_PREPARED, properties={"render_id": "rn_f"}),
        _event(taxonomy.DELIVERY_FAILED, properties={"render_id": "rn_f", "operation": "edit"}),
        _event(taxonomy.STATE_MUTATED, properties={"domain": "x", "action": "y"}),
    ]
    signal = _signals(failing)["delivery_failed_terminal"]
    assert signal["count"] == 1
    # Stale-edit fallback chain that ENDS in success is not terminal.
    recovered = [
        _received("in_1"),
        _event(taxonomy.UI_RENDER_PREPARED, properties={"render_id": "rn_r"}),
        _event(taxonomy.DELIVERY_FAILED, properties={"render_id": "rn_r", "operation": "edit"}),
        _event(taxonomy.DELIVERY_SUCCEEDED, properties={"render_id": "rn_r", "operation": "reply"},
               outcome="delivered"),
    ]
    assert "delivery_failed_terminal" not in _signals(recovered)


def test_render_without_delivery_result() -> None:
    events = [
        _received("in_1"),
        _event(taxonomy.UI_RENDER_PREPARED, properties={"render_id": "rn_x"}),
        _event(taxonomy.STATE_MUTATED, properties={"domain": "x", "action": "y"}),
    ]
    assert _signals(events)["render_without_delivery"]["count"] == 1


def test_callback_unresolved_correlation() -> None:
    events = [
        _received("in_1", callback="pick:1", source_message_id=7),
        _event(taxonomy.UI_CONTROL_ACTIVATED, properties={
            "callback_data": "pick:1", "correlation": "unresolved",
        }),
        *_delivered_render("in_1"),
    ]
    signal = _signals(events)["callback_unresolved"]
    # Digest only — raw callback data never enters a committable signal.
    assert "pick:1" not in json.dumps(signal["occurrences"][0]["detail"])


def test_resolved_callback_stays_silent() -> None:
    events = [
        _received("in_1", callback="pick:1", source_message_id=7),
        _event(taxonomy.UI_CONTROL_ACTIVATED, properties={
            "callback_data": "pick:1", "correlation": "resolved", "source_render_id": "rn_0",
        }),
        *_delivered_render("in_1"),
    ]
    assert "callback_unresolved" not in _signals(events)


def test_validation_decision_and_flow_signals() -> None:
    events = [
        _received("in_1"),
        _event(taxonomy.VALIDATION_FAILED, entity="daily_menu", outcome="protein_low"),
        _event(taxonomy.DECISION_REPAIRED, entity="daily_menu", properties={"reason": "menu_repair"}),
        _event(taxonomy.DECISION_FALLBACK_SELECTED, entity="daily_menu",
               properties={"reason": "ai_repair_failed_meal_splice"}),
        _event(taxonomy.FLOW_EXPIRED, properties={"flow_before": {"name": "goal_wizard", "step": 2}}),
        *_delivered_render("in_1"),
    ]
    signals = _signals(events)
    assert signals["validation_failed"]["occurrences"][0]["detail"]["outcome"] == "protein_low"
    assert signals["decision_repaired"]["count"] == 1
    assert signals["decision_fallback"]["occurrences"][0]["detail"]["reason"] == "ai_repair_failed_meal_splice"
    assert signals["flow_expired"]["occurrences"][0]["detail"]["flow"] == "goal_wizard"


def test_user_correction_undo_and_override() -> None:
    events = [
        _received("in_1"),
        _event("MEAL_UNDONE", entity="meal"),
        _event(taxonomy.DECISION_FINALIZED, entity="meal_reanalysis", properties={
            "overrides": [{"ai_grams": 180, "final_grams": 250, "reason": "user_explicit_quantity"}],
        }),
        *_delivered_render("in_1"),
    ]
    signal = _signals(events)["user_correction"]
    assert signal["count"] == 2
    kinds = {occ["detail"]["correction"] for occ in signal["occurrences"]}
    assert kinds == {"meal_undone", "analysis_override"}


def test_finalized_decision_without_overrides_is_not_a_correction() -> None:
    events = [
        _received("in_1"),
        _event(taxonomy.DECISION_FINALIZED, entity="intent", properties={"overrides": []}),
        *_delivered_render("in_1"),
    ]
    assert "user_correction" not in _signals(events)


def test_repeated_identical_text_detected_distinct_text_not() -> None:
    repeated = []
    for index, text in enumerate(["מה לאכול?", "מה לאכול? ", "מה לאכול?"]):
        interaction = f"in_{index}"
        repeated.append(_received(interaction, text=text, trace=f"tr_{index}"))
        repeated.extend(_delivered_render(interaction, render_id=f"rn_{index}", trace=f"tr_{index}"))
    signal = _signals(repeated)["repeated_user_input"]
    assert signal["occurrences"][0]["detail"]["repetitions"] == 3
    assert "מה לאכול" not in json.dumps(signal)  # digests, never text

    distinct = []
    for index, text in enumerate(["בוקר טוב", "מה לאכול?", "תודה"]):
        interaction = f"in_d{index}"
        distinct.append(_received(interaction, text=text, trace=f"tr_d{index}"))
        distinct.extend(_delivered_render(interaction, render_id=f"rn_d{index}", trace=f"tr_d{index}"))
    assert "repeated_user_input" not in _signals(distinct)


def test_repeated_callback_double_tap_detected() -> None:
    events = []
    for index in range(2):
        interaction = f"in_c{index}"
        events.append(_received(interaction, callback="save:9", source_message_id=42, trace="tr_c"))
        events.extend(_delivered_render(interaction, render_id=f"rn_c{index}", trace="tr_c"))
    signal = _signals(events)["repeated_user_input"]
    assert signal["occurrences"][0]["detail"]["kind"] == "callback"


def test_same_callback_on_different_messages_is_not_a_retry() -> None:
    events = []
    for index in range(2):
        interaction = f"in_m{index}"
        events.append(_received(interaction, callback="save:9", source_message_id=100 + index,
                                trace=f"tr_m{index}"))
        events.extend(_delivered_render(interaction, render_id=f"rn_m{index}", trace=f"tr_m{index}"))
    assert "repeated_user_input" not in _signals(events)


def test_interaction_without_output_detected() -> None:
    events = [_received("in_1", text="הבוט מתעלם ממני")]
    signal = _signals(events)["interaction_no_output"]
    assert signal["severity_hint"] == "high"


def test_interaction_with_state_change_or_crash_not_silent() -> None:
    with_state = [
        _received("in_1"),
        _event(taxonomy.STATE_MUTATED, properties={"domain": "meals", "action": "saved"}),
    ]
    assert "interaction_no_output" not in _signals(with_state)
    crashed = [
        _received("in_2", trace="tr_2"),
        _event(taxonomy.ERROR_CAPTURED, interaction="in_2", trace="tr_2",
               properties={"boundary": "telegram_handler:text", "error_type": "ValueError"}),
    ]
    assert "interaction_no_output" not in _signals(crashed)  # error_captured owns it


def test_not_modified_delivery_counts_as_output() -> None:
    events = [
        _received("in_1"),
        _event(taxonomy.UI_RENDER_PREPARED, properties={"render_id": "rn_nm"}),
        _event(taxonomy.DELIVERY_SUCCEEDED, properties={"render_id": "rn_nm", "operation": "edit"},
               outcome="not_modified"),
    ]
    assert "interaction_no_output" not in _signals(events)


def test_observability_degradation_and_unclassified_ai() -> None:
    events = [
        _received("in_1"),
        _event(taxonomy.OBSERVABILITY_WRITE_FAILED, properties={"failed_event": "state.mutated"}),
        _event(taxonomy.AI_CALL_STARTED, properties={"ai_call_id": "ai_x", "purpose": "unclassified"}),
        _event(taxonomy.AI_CALL_COMPLETED, properties={"ai_call_id": "ai_x", "purpose": "unclassified"}),
        *_delivered_render("in_1"),
    ]
    signals = _signals(events)
    assert signals["observability_write_failed"]["count"] == 1
    assert signals["ai_purpose_unclassified"]["count"] == 1
