"""Canonical interaction observability for Noam Coach (Observability O1+).

``product_events`` (see ``event_log.py``) is the canonical interaction trace
stream. This package layers policy on top of that store without replacing it:

- ``ids``          — correlation identifier generators (trace/interaction/span/…)
- ``modes``        — off / metadata / content / debug capture policy
- ``redaction``    — the single recursive redaction/sanitization boundary
- ``obs_context``  — contextvar propagation of the active trace/interaction/span
- ``emit``         — the safe event-write boundary (never breaks coaching,
                     degradation stays detectable)
- ``taxonomy``     — canonical event names + event-version registry
- ``interaction_lifecycle`` — the scope that guarantees every interaction
                     terminates with an outcome + duration
- ``trace_reader`` — structured, machine-readable trace grouping primitives

``audit`` (domain/business audit trail) and ``analytics_events``
(compatibility/product metrics) intentionally remain separate stores with
separate purposes.
"""

from noam_coach.observability import taxonomy
from noam_coach.observability.emit import (
    emit_event,
    observability_health,
    reset_observability_health,
)
from noam_coach.observability.ids import (
    new_ai_call_id,
    new_interaction_id,
    new_media_id,
    new_render_id,
    new_span_id,
    new_trace_id,
)
from noam_coach.observability.interaction_lifecycle import (
    OUTCOME_CANCELLED,
    OUTCOME_ERROR,
    OUTCOME_HANDLED,
    OUTCOME_NO_HANDLER,
    observed_interaction,
)
from noam_coach.observability.modes import ObservabilityMode, get_mode, set_mode
from noam_coach.observability.obs_context import (
    correlation_kwargs,
    current_interaction_id,
    current_trace_id,
    current_user_id,
    interaction_scope,
    mark_response,
    response_count,
    span_scope,
)
from noam_coach.observability.redaction import redact

__all__ = [
    "OUTCOME_CANCELLED",
    "OUTCOME_ERROR",
    "OUTCOME_HANDLED",
    "OUTCOME_NO_HANDLER",
    "ObservabilityMode",
    "correlation_kwargs",
    "current_interaction_id",
    "current_trace_id",
    "current_user_id",
    "emit_event",
    "get_mode",
    "interaction_scope",
    "mark_response",
    "new_ai_call_id",
    "new_interaction_id",
    "new_media_id",
    "new_render_id",
    "new_span_id",
    "new_trace_id",
    "observability_health",
    "observed_interaction",
    "redact",
    "reset_observability_health",
    "response_count",
    "set_mode",
    "span_scope",
    "taxonomy",
]
