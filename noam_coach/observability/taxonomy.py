"""Canonical event taxonomy and event-version registry.

One name per semantic event, dot-namespaced by family, always lower-case
past tense. New instrumentation MUST use these constants — string literals
drift into near-duplicates (``message_sent`` vs ``delivery.succeeded``).

Event versions: bump ``EVENT_VERSIONS[name]`` when an event's payload
contract changes incompatibly; readers can then branch on
``ProductEvent.event_version``. Unlisted events are version 1.
"""

from __future__ import annotations

# interaction.*
INTERACTION_RECEIVED = "interaction.received"

# routing.*
ROUTING_DECIDED = "routing.decided"

# media.*
MEDIA_RECEIVED = "media.received"

# context.*
CONTEXT_BUILT = "context.built"

# ai.*
AI_CALL_STARTED = "ai.call.started"
AI_CALL_COMPLETED = "ai.call.completed"
AI_CALL_FAILED = "ai.call.failed"

# validation.*
VALIDATION_COMPLETED = "validation.completed"
VALIDATION_FAILED = "validation.failed"

# decision.*
DECISION_REPAIRED = "decision.repaired"
DECISION_FALLBACK_SELECTED = "decision.fallback_selected"
DECISION_FINALIZED = "decision.finalized"

# state.*
STATE_MUTATED = "state.mutated"

# flow.*
FLOW_STARTED = "flow.started"
FLOW_UPDATED = "flow.updated"
FLOW_SUSPENDED = "flow.suspended"
FLOW_RESUMED = "flow.resumed"
FLOW_EXPIRED = "flow.expired"
FLOW_COMPLETED = "flow.completed"

# ui.*
UI_RENDER_PREPARED = "ui.render.prepared"
UI_CONTROLS_PRESENTED = "ui.controls.presented"
UI_CONTROL_ACTIVATED = "ui.control.activated"
UI_VIEW_RENDERED = "ui.view.rendered"
UI_ACTION_ACTIVATED = "ui.action.activated"

# delivery.*
DELIVERY_ATTEMPTED = "delivery.attempted"
DELIVERY_SUCCEEDED = "delivery.succeeded"
DELIVERY_FAILED = "delivery.failed"

# error.* / observability.*
ERROR_CAPTURED = "error.captured"
OBSERVABILITY_WRITE_FAILED = "observability.write_failed"

# meal.* — the meal correction/quantity lifecycle (Batch 7).
#
# These make the identity → count → conversion → plausibility → clarification
# chain reconstructable. Join on ``entity_id`` (the approval id) plus the
# ambient trace/interaction correlation; ``revision`` orders them within one
# approval.
MEAL_QUANTITY_CONVERSION_EVALUATED = "meal.quantity.conversion_evaluated"
MEAL_PLAUSIBILITY_EVALUATED = "meal.plausibility.evaluated"
MEAL_CLARIFICATION_RAISED = "meal.clarification.raised"
MEAL_RENDER_QUANTITY_MODE = "meal.render.quantity_mode"

# Pre-existing meal lifecycle names, kept VERBATIM.
#
# They predate the dot-namespaced convention and are read by existing tests
# and operator docs, so Batch 7 moved them to the canonical emit boundary
# (for redaction + mode policy) WITHOUT renaming them. Registering them here
# keeps every meal call site importing from the taxonomy rather than passing
# string literals, which is what the module docstring asks for.
MEAL_CORRECTION_APPLIED = "meal_correction_applied"
MEAL_CORRECTION_ERROR = "meal_correction_error"
MEAL_CLARIFICATION_RESOLVED = "meal_clarification_resolved"

# Envelope version for events emitted by the canonical emit boundary.
EVENT_VERSIONS: dict[str, int] = {}


def event_version(event: str) -> int:
    return EVENT_VERSIONS.get(event, 1)
