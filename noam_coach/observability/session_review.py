"""Deterministic signal detectors over the machine trace (Review batch R3).

Detectors are the mechanical floor of a session review: they walk a
:class:`SessionTrace` and surface occurrences that are RELIABLY provable
from the current event contracts — crashes, failed/unterminated AI calls,
terminal delivery failures, validation failures, fallbacks, expired flows,
explicit user corrections, repeated identical attempts, silent no-output
interactions, degraded observability. They produce **signals**, not
findings: no qualitative root cause, no systemic verdict — a repeated
signal is summarized by count and affected traces, and root-cause
judgment stays with the versioned review protocol.

Privacy: signal payloads are committable by construction — event ids,
interaction/trace ids, event names, enum-ish outcomes, counts and content
DIGESTS only. No conversation text, no AI output, no free-form content
ever enters a signal (the gitignored evidence artifacts carry those).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Callable

from noam_coach.observability import taxonomy
from noam_coach.observability.session_trace import InteractionTrace, SessionTrace

DETECTOR_SET_VERSION = "1.0"

SEVERITY_HINTS = ("critical", "high", "medium", "low")


@dataclass(frozen=True)
class SignalOccurrence:
    """One concrete, evidence-addressed occurrence of a signal."""

    event_ids: tuple[int, ...]
    trace_id: str | None = None
    interaction_id: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_ids": list(self.event_ids),
            "trace_id": self.trace_id,
            "interaction_id": self.interaction_id,
            "detail": dict(self.detail),
        }


@dataclass(frozen=True)
class Signal:
    detector_id: str
    detector_version: int
    severity_hint: str
    summary: str
    count: int
    occurrences: tuple[SignalOccurrence, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "detector_id": self.detector_id,
            "detector_version": self.detector_version,
            "severity_hint": self.severity_hint,
            "summary": self.summary,
            "count": self.count,
            "occurrences": [item.to_dict() for item in self.occurrences],
            "affected_traces": sorted(
                {item.trace_id for item in self.occurrences if item.trace_id}
            ),
        }


@dataclass(frozen=True)
class Detector:
    detector_id: str
    version: int
    severity_hint: str
    description: str
    supported_contract: str
    limitations: str
    run: Callable[[SessionTrace], list[SignalOccurrence]]

    def metadata(self) -> dict[str, Any]:
        return {
            "detector_id": self.detector_id,
            "version": self.version,
            "severity_hint": self.severity_hint,
            "description": self.description,
            "supported_contract": self.supported_contract,
            "limitations": self.limitations,
        }


def _digest8(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:8]


def _events_named(trace: SessionTrace, name: str) -> list[Any]:
    return [e for e in trace.events if e.event == name]


def _occurrence_for_event(event: Any, **detail: Any) -> SignalOccurrence:
    return SignalOccurrence(
        event_ids=(event.id,),
        trace_id=event.trace_id,
        interaction_id=event.interaction_id,
        detail={k: v for k, v in detail.items() if v is not None},
    )


# ---------------------------------------------------------------------------
# Detector implementations
# ---------------------------------------------------------------------------


def _detect_error_captured(trace: SessionTrace) -> list[SignalOccurrence]:
    return [
        _occurrence_for_event(
            event,
            boundary=event.properties.get("boundary"),
            error_type=event.properties.get("error_type"),
            entity=event.entity,
        )
        for event in _events_named(trace, taxonomy.ERROR_CAPTURED)
    ]


def _detect_ai_call_failed(trace: SessionTrace) -> list[SignalOccurrence]:
    found: list[SignalOccurrence] = []
    for interaction in trace.interactions:
        for ai in interaction.ai_calls:
            if ai.failed is not None:
                found.append(SignalOccurrence(
                    event_ids=(ai.failed.id,),
                    trace_id=interaction.trace_id,
                    interaction_id=interaction.interaction_id,
                    detail={
                        "purpose": ai.purpose,
                        "error_type": ai.failed.properties.get("error_type"),
                        "failure_class": ai.failed.properties.get("failure_class"),
                    },
                ))
    return found


def _detect_ai_call_unresolved(trace: SessionTrace) -> list[SignalOccurrence]:
    found: list[SignalOccurrence] = []
    for interaction in trace.interactions:
        for ai in interaction.ai_calls:
            if ai.completed is None and ai.failed is None and ai.started is not None:
                found.append(SignalOccurrence(
                    event_ids=(ai.started.id,),
                    trace_id=interaction.trace_id,
                    interaction_id=interaction.interaction_id,
                    detail={"purpose": ai.purpose, "ai_call_id": ai.ai_call_id},
                ))
    return found


def _detect_delivery_failed_terminal(trace: SessionTrace) -> list[SignalOccurrence]:
    found: list[SignalOccurrence] = []
    for interaction in trace.interactions:
        for render in interaction.renders:
            if render.delivery_result == "failed":
                failure_ids = tuple(
                    e.id for e in render.deliveries if e.event == taxonomy.DELIVERY_FAILED
                )
                found.append(SignalOccurrence(
                    event_ids=(render.prepared.id, *failure_ids),
                    trace_id=interaction.trace_id,
                    interaction_id=interaction.interaction_id,
                    detail={"render_id": render.render_id},
                ))
    return found


def _detect_render_without_delivery(trace: SessionTrace) -> list[SignalOccurrence]:
    found: list[SignalOccurrence] = []
    for interaction in trace.interactions:
        for render in interaction.renders:
            if render.delivery_result == "unknown":
                found.append(SignalOccurrence(
                    event_ids=(render.prepared.id,),
                    trace_id=interaction.trace_id,
                    interaction_id=interaction.interaction_id,
                    detail={"render_id": render.render_id},
                ))
    return found


def _detect_callback_unresolved(trace: SessionTrace) -> list[SignalOccurrence]:
    found: list[SignalOccurrence] = []
    for interaction in trace.interactions:
        activation = interaction.control_activation
        if activation is not None and activation.properties.get("correlation") == "unresolved":
            found.append(_occurrence_for_event(
                activation, callback_digest=_digest8(str(activation.properties.get("callback_data"))),
            ))
    return found


def _detect_validation_failed(trace: SessionTrace) -> list[SignalOccurrence]:
    return [
        _occurrence_for_event(event, entity=event.entity, outcome=event.outcome)
        for event in _events_named(trace, taxonomy.VALIDATION_FAILED)
    ]


def _detect_decision_repaired(trace: SessionTrace) -> list[SignalOccurrence]:
    return [
        _occurrence_for_event(event, entity=event.entity, reason=event.properties.get("reason"))
        for event in _events_named(trace, taxonomy.DECISION_REPAIRED)
    ]


def _detect_decision_fallback(trace: SessionTrace) -> list[SignalOccurrence]:
    return [
        _occurrence_for_event(event, entity=event.entity, reason=event.properties.get("reason"))
        for event in _events_named(trace, taxonomy.DECISION_FALLBACK_SELECTED)
    ]


def _detect_flow_expired(trace: SessionTrace) -> list[SignalOccurrence]:
    occurrences = []
    for event in _events_named(trace, taxonomy.FLOW_EXPIRED):
        flow_before = event.properties.get("flow_before") or {}
        occurrences.append(_occurrence_for_event(
            event, flow=flow_before.get("name"), step=flow_before.get("step"),
        ))
    return occurrences


def _detect_user_correction(trace: SessionTrace) -> list[SignalOccurrence]:
    found: list[SignalOccurrence] = []
    for event in trace.events:
        if event.event == "MEAL_UNDONE":
            found.append(_occurrence_for_event(event, correction="meal_undone"))
        elif event.event == taxonomy.DECISION_FINALIZED:
            overrides = event.properties.get("overrides")
            if overrides:
                found.append(_occurrence_for_event(
                    event,
                    correction="analysis_override",
                    entity=event.entity,
                    override_count=len(overrides),
                ))
    return found


def _normalized_input(interaction: InteractionTrace) -> tuple[str, str] | None:
    """(kind, comparable-key) for repetition matching — digests, not text."""
    received = interaction.received
    if received is None:
        return None
    callback = received.properties.get("callback_data")
    if callback:
        source = received.properties.get("source_message_id")
        return ("callback", f"{callback}@{source}")
    text = interaction.input_text
    if text and text.strip():
        return ("text", _digest8(text.strip().casefold()))
    return None


def _detect_repeated_user_input(trace: SessionTrace) -> list[SignalOccurrence]:
    """Consecutive interactions carrying the SAME input — retry/frustration
    evidence (double-taps on one control, repeating an ignored message)."""
    found: list[SignalOccurrence] = []
    run: list[InteractionTrace] = []
    run_key: tuple[str, str] | None = None

    def flush() -> None:
        if run_key is None or len(run) < 2:
            return
        found.append(SignalOccurrence(
            event_ids=tuple(i.received.id for i in run if i.received is not None),
            trace_id=run[0].trace_id,
            interaction_id=run[0].interaction_id,
            detail={
                "kind": run_key[0],
                "input_digest": run_key[1],
                "repetitions": len(run),
                "interaction_ids": [i.interaction_id for i in run],
            },
        ))

    for interaction in trace.interactions:
        key = _normalized_input(interaction)
        if key is not None and key == run_key:
            run.append(interaction)
            continue
        flush()
        run_key = key
        run = [interaction] if key is not None else []
    flush()
    return found


def _detect_interaction_no_output(trace: SessionTrace) -> list[SignalOccurrence]:
    """User input that produced NO delivered render, NO state change and NO
    Mini App view — the silent "bot ignored me" class. Interactions that
    crashed are excluded (error_captured already reports them)."""
    found: list[SignalOccurrence] = []
    for interaction in trace.interactions:
        received = interaction.received
        if received is None:
            continue
        if any(e.event == taxonomy.ERROR_CAPTURED for e in interaction.events):
            continue
        delivered = any(
            render.delivery_result in ("delivered", "not_modified")
            for render in interaction.renders
        )
        if delivered or interaction.state_mutations or interaction.view_renders:
            continue
        found.append(_occurrence_for_event(received, kind=interaction.input_kind))
    return found


def _detect_observability_write_failed(trace: SessionTrace) -> list[SignalOccurrence]:
    return [
        _occurrence_for_event(event, failed_event=event.properties.get("failed_event"))
        for event in _events_named(trace, taxonomy.OBSERVABILITY_WRITE_FAILED)
    ]


def _detect_ai_purpose_unclassified(trace: SessionTrace) -> list[SignalOccurrence]:
    found: list[SignalOccurrence] = []
    for interaction in trace.interactions:
        for ai in interaction.ai_calls:
            if ai.purpose == "unclassified":
                anchor = ai.started or ai.completed or ai.failed
                if anchor is not None:
                    found.append(SignalOccurrence(
                        event_ids=(anchor.id,),
                        trace_id=interaction.trace_id,
                        interaction_id=interaction.interaction_id,
                        detail={"ai_call_id": ai.ai_call_id},
                    ))
    return found


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

DETECTORS: tuple[Detector, ...] = (
    Detector(
        "error_captured", 1, "critical",
        "Unhandled exception recorded at an execution boundary.",
        "error.captured (R1 boundaries + daily-flags CAS)",
        "Only exceptions that reached an instrumented boundary appear.",
        _detect_error_captured,
    ),
    Detector(
        "ai_call_failed", 1, "high",
        "AI call terminated with a failure.",
        "ai.call.failed grouped per interaction",
        "Product-level fallbacks may have masked the user impact (see decision_fallback).",
        _detect_ai_call_failed,
    ),
    Detector(
        "ai_call_unresolved", 1, "medium",
        "AI call started but no completion/failure was recorded.",
        "ai.call.started without terminal event in the same interaction",
        "A window boundary can split start/terminal events; verify against the full stream.",
        _detect_ai_call_unresolved,
    ),
    Detector(
        "delivery_failed_terminal", 1, "high",
        "Prepared message whose delivery terminally failed (no later success).",
        "ui.render.prepared + delivery.* chain per render",
        "Retries outside the recorded chain are not visible.",
        _detect_delivery_failed_terminal,
    ),
    Detector(
        "render_without_delivery", 1, "medium",
        "Prepared render with no recorded delivery result at all.",
        "RenderTrace.delivery_result == unknown",
        "Can be caused by process death between prepare and send.",
        _detect_render_without_delivery,
    ),
    Detector(
        "callback_unresolved", 1, "low",
        "Pressed control whose source render could not be proven.",
        "ui.control.activated correlation=unresolved",
        "Expected for controls older than the render-registry scan window.",
        _detect_callback_unresolved,
    ),
    Detector(
        "validation_failed", 1, "medium",
        "Deterministic validation rejected produced output.",
        "validation.failed",
        "Repair chains may have recovered; correlate with decision_* signals.",
        _detect_validation_failed,
    ),
    Detector(
        "decision_repaired", 1, "low",
        "AI output required deterministic repair.",
        "decision.repaired",
        "Repair is working as designed; frequency is the signal.",
        _detect_decision_repaired,
    ),
    Detector(
        "decision_fallback", 1, "medium",
        "Deterministic fallback replaced the intended path.",
        "decision.fallback_selected",
        "The user saw degraded output silently; reason field says why.",
        _detect_decision_fallback,
    ),
    Detector(
        "flow_expired", 1, "medium",
        "An active flow expired before completion.",
        "flow.expired",
        "Expiry may be legitimate abandonment; repetition on one flow is the signal.",
        _detect_flow_expired,
    ),
    Detector(
        "user_correction", 1, "medium",
        "Explicit user correction of produced output (undo / analysis override).",
        "MEAL_UNDONE + decision.finalized with non-empty overrides",
        "Only correction paths with structured evidence are counted.",
        _detect_user_correction,
    ),
    Detector(
        "repeated_user_input", 1, "medium",
        "Consecutive identical user inputs (retry/frustration evidence).",
        "interaction.received text digest / callback_data+source message",
        "Identical input does not prove one shared cause; digests only, no text.",
        _detect_repeated_user_input,
    ),
    Detector(
        "interaction_no_output", 1, "high",
        "User input with no delivered output, state change, or view render.",
        "InteractionTrace renders/state_mutations/view_renders",
        "Excludes crashed interactions (error_captured covers those); an "
        "intentionally silent handler would also match.",
        _detect_interaction_no_output,
    ),
    Detector(
        "observability_write_failed", 1, "high",
        "The trace itself degraded during the window.",
        "observability.write_failed",
        "The failed event's own payload is by definition incomplete.",
        _detect_observability_write_failed,
    ),
    Detector(
        "ai_purpose_unclassified", 1, "low",
        "An AI call site without a purpose wrap appeared (coverage regression).",
        "ai.call.* purpose=unclassified",
        "Identifies the call, not the missing wrap location.",
        _detect_ai_purpose_unclassified,
    ),
)


def detector_inventory() -> list[dict[str, Any]]:
    return [detector.metadata() for detector in DETECTORS]


def run_detectors(trace: SessionTrace) -> dict[str, Any]:
    """Run every detector; return the deterministic, committable result."""
    signals: list[dict[str, Any]] = []
    for detector in DETECTORS:
        occurrences = detector.run(trace)
        if not occurrences:
            continue
        signals.append(Signal(
            detector_id=detector.detector_id,
            detector_version=detector.version,
            severity_hint=detector.severity_hint,
            summary=detector.description,
            count=len(occurrences),
            occurrences=tuple(occurrences),
        ).to_dict())
    return {
        "detector_set_version": DETECTOR_SET_VERSION,
        "detectors": detector_inventory(),
        "signals": signals,
    }
