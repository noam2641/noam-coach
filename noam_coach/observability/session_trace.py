"""Deterministic session trace model + human timeline (Observability O9).

Machine-readable trace abstractions over the canonical product_events
stream. Causal order is append order (monotonic row ids under this
single-writer design) — never wall-clock string comparison, and the machine
trace NEVER parses the human timeline (the timeline is rendered FROM the
machine trace, one way).

Unresolved links are explicit: a callback whose source render could not be
proven, or an AI call with no completion, is reported as such — nothing is
guessed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import event_log
from event_log import ProductEvent
from noam_coach.observability import taxonomy


@dataclass(frozen=True)
class AITrace:
    ai_call_id: str
    purpose: str | None
    model: str | None
    operation: str | None
    started: ProductEvent | None
    completed: ProductEvent | None
    failed: ProductEvent | None

    @property
    def succeeded(self) -> bool:
        return self.completed is not None

    @property
    def output(self) -> Any:
        if self.completed is None:
            return None
        return (self.completed.properties.get("content") or {}).get("output")

    @property
    def duration_ms(self) -> int | None:
        final = self.completed or self.failed
        return final.properties.get("duration_ms") if final else None


@dataclass(frozen=True)
class RenderTrace:
    render_id: str
    prepared: ProductEvent
    deliveries: tuple[ProductEvent, ...]

    @property
    def text(self) -> str | None:
        return (self.prepared.properties.get("content") or {}).get("text")

    @property
    def controls(self) -> list[dict[str, Any]]:
        return list(self.prepared.properties.get("controls") or [])

    @property
    def delivery_result(self) -> str:
        """delivered | not_modified | failed | unknown (never guessed)."""
        outcome = "unknown"
        for event in self.deliveries:
            if event.event == taxonomy.DELIVERY_SUCCEEDED:
                outcome = event.outcome or "delivered"
            elif event.event == taxonomy.DELIVERY_FAILED and outcome == "unknown":
                outcome = "failed"
        return outcome

    @property
    def delivered_message_id(self) -> Any:
        for event in reversed(self.deliveries):
            if event.event == taxonomy.DELIVERY_SUCCEEDED:
                return event.properties.get("message_id")
        return None


@dataclass(frozen=True)
class InteractionTrace:
    interaction_id: str | None
    trace_id: str | None
    events: tuple[ProductEvent, ...]

    def _all(self, name: str) -> list[ProductEvent]:
        return [e for e in self.events if e.event == name]

    def _first(self, name: str) -> ProductEvent | None:
        found = self._all(name)
        return found[0] if found else None

    # -- canonical accessors -------------------------------------------------
    @property
    def received(self) -> ProductEvent | None:
        return self._first(taxonomy.INTERACTION_RECEIVED)

    @property
    def input_text(self) -> str | None:
        received = self.received
        if received is None:
            return None
        return (received.properties.get("content") or {}).get("text")

    @property
    def input_kind(self) -> str | None:
        received = self.received
        return received.properties.get("kind") if received else None

    @property
    def routing(self) -> ProductEvent | None:
        return self._first(taxonomy.ROUTING_DECIDED)

    @property
    def media(self) -> list[ProductEvent]:
        return self._all(taxonomy.MEDIA_RECEIVED)

    @property
    def ai_calls(self) -> list[AITrace]:
        by_id: dict[str, dict[str, ProductEvent]] = {}
        order: list[str] = []
        for event in self.events:
            call_id = event.properties.get("ai_call_id")
            if not call_id:
                continue
            if call_id not in by_id:
                by_id[call_id] = {}
                order.append(call_id)
            if event.event == taxonomy.AI_CALL_STARTED:
                by_id[call_id]["started"] = event
            elif event.event == taxonomy.AI_CALL_COMPLETED:
                by_id[call_id]["completed"] = event
            elif event.event == taxonomy.AI_CALL_FAILED:
                by_id[call_id]["failed"] = event
        traces: list[AITrace] = []
        for call_id in order:
            group = by_id[call_id]
            anchor = group.get("started") or group.get("completed") or group.get("failed")
            traces.append(AITrace(
                ai_call_id=call_id,
                purpose=anchor.properties.get("purpose") if anchor else None,
                model=anchor.properties.get("model") if anchor else None,
                operation=anchor.properties.get("operation") if anchor else None,
                started=group.get("started"),
                completed=group.get("completed"),
                failed=group.get("failed"),
            ))
        return traces

    @property
    def validations(self) -> list[ProductEvent]:
        return [
            e for e in self.events
            if e.event in (taxonomy.VALIDATION_COMPLETED, taxonomy.VALIDATION_FAILED)
        ]

    @property
    def decisions(self) -> list[ProductEvent]:
        return [
            e for e in self.events
            if e.event in (
                taxonomy.DECISION_FINALIZED,
                taxonomy.DECISION_REPAIRED,
                taxonomy.DECISION_FALLBACK_SELECTED,
            )
        ]

    @property
    def final_decision(self) -> ProductEvent | None:
        finals = self._all(taxonomy.DECISION_FINALIZED)
        return finals[-1] if finals else None

    @property
    def state_mutations(self) -> list[ProductEvent]:
        flow_events = tuple(
            name for name in vars(taxonomy).values()
            if isinstance(name, str) and name.startswith("flow.")
        )
        return [
            e for e in self.events
            if e.event == taxonomy.STATE_MUTATED or e.event in flow_events
        ]

    @property
    def renders(self) -> list[RenderTrace]:
        prepared = {
            e.properties.get("render_id"): e
            for e in self._all(taxonomy.UI_RENDER_PREPARED)
            if e.properties.get("render_id")
        }
        deliveries: dict[str, list[ProductEvent]] = {rid: [] for rid in prepared}
        for event in self.events:
            if event.event in (
                taxonomy.DELIVERY_ATTEMPTED,
                taxonomy.DELIVERY_SUCCEEDED,
                taxonomy.DELIVERY_FAILED,
            ):
                rid = event.properties.get("render_id")
                if rid in deliveries:
                    deliveries[rid].append(event)
        return [
            RenderTrace(render_id=rid, prepared=prepared[rid], deliveries=tuple(deliveries[rid]))
            for rid in prepared
        ]

    @property
    def visible_outputs(self) -> list[str]:
        """Exact texts that were actually DELIVERED (never merely prepared)."""
        return [
            render.text for render in self.renders
            if render.delivery_result in ("delivered",) and render.text is not None
        ]

    @property
    def control_activation(self) -> ProductEvent | None:
        return self._first(taxonomy.UI_CONTROL_ACTIVATED)

    @property
    def view_renders(self) -> list[ProductEvent]:
        return self._all(taxonomy.UI_VIEW_RENDERED)

    @property
    def unresolved_links(self) -> list[dict[str, Any]]:
        """Explicitly unproven correlations inside this interaction."""
        unresolved: list[dict[str, Any]] = []
        activation = self.control_activation
        if activation is not None and activation.properties.get("correlation") == "unresolved":
            unresolved.append({
                "kind": "control_source_render",
                "callback_data": activation.properties.get("callback_data"),
            })
        for ai in self.ai_calls:
            if ai.completed is None and ai.failed is None:
                unresolved.append({"kind": "ai_call_without_result", "ai_call_id": ai.ai_call_id})
        for render in self.renders:
            if render.delivery_result == "unknown":
                unresolved.append({"kind": "render_without_delivery_result", "render_id": render.render_id})
        return unresolved


@dataclass(frozen=True)
class SessionTrace:
    user_id: int
    events: tuple[ProductEvent, ...]
    interactions: tuple[InteractionTrace, ...] = field(default=())

    @property
    def legacy_events(self) -> list[ProductEvent]:
        return [e for e in self.events if e.is_legacy_uncorrelated]

    def interaction(self, interaction_id: str) -> InteractionTrace | None:
        for item in self.interactions:
            if item.interaction_id == interaction_id:
                return item
        return None

    def by_trace(self, trace_id: str) -> list[InteractionTrace]:
        return [i for i in self.interactions if i.trace_id == trace_id]


def _group_interactions(events: list[ProductEvent]) -> tuple[InteractionTrace, ...]:
    order: list[str | None] = []
    groups: dict[str | None, list[ProductEvent]] = {}
    for event in events:
        key = event.interaction_id
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(event)
    return tuple(
        InteractionTrace(
            interaction_id=key,
            trace_id=next((e.trace_id for e in groups[key] if e.trace_id), None),
            events=tuple(groups[key]),
        )
        for key in order
        if key is not None
    )


async def load_session_trace(db: Any, user_id: int, *, limit: int = 2000) -> SessionTrace:
    events = await event_log.list_events(db, user_id, limit=limit)
    return SessionTrace(
        user_id=user_id,
        events=tuple(events),
        interactions=_group_interactions(events),
    )


async def load_trace(db: Any, user_id: int, trace_id: str, *, limit: int = 2000) -> SessionTrace:
    events = await event_log.list_events(db, user_id, trace_id=trace_id, limit=limit)
    return SessionTrace(
        user_id=user_id,
        events=tuple(events),
        interactions=_group_interactions(events),
    )


async def load_interaction(
    db: Any, user_id: int, interaction_id: str, *, limit: int = 2000
) -> InteractionTrace | None:
    events = await event_log.list_events(db, user_id, interaction_id=interaction_id, limit=limit)
    grouped = _group_interactions(events)
    return grouped[0] if grouped else None


# ---------------------------------------------------------------------------
# Human timeline renderer (one-way: machine trace → text)
# ---------------------------------------------------------------------------


def _short(value: Any, limit: int = 220) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=repr)
    return text if len(text) <= limit else text[:limit] + "…"


def _timeline_for_interaction(interaction: InteractionTrace, lines: list[str]) -> None:
    received = interaction.received
    header = f"── interaction {interaction.interaction_id} (trace {interaction.trace_id})"
    lines.append(header)
    if received is not None:
        kind = received.properties.get("kind")
        text = interaction.input_text
        lines.append(f"USER [{kind}]")
        if text:
            lines.append(f'  "{_short(text)}"')
        callback = received.properties.get("callback_data")
        if callback:
            lines.append(f"  callback: {callback}")
        media = received.properties.get("media")
        if media:
            lines.append(f"  media: {media.get('media_kind')} ({media.get('file_unique_id')})")

    activation = interaction.control_activation
    if activation is not None:
        label = activation.properties.get("label")
        lines.append("USER CONTROL")
        lines.append(
            f"  {activation.properties.get('callback_data')}"
            + (f' — "{label}"' if label else "")
            + f" · source_render={activation.properties.get('source_render_id') or 'unresolved'}"
        )

    routing = interaction.routing
    if routing is not None:
        lines.append("ROUTING")
        lines.append(
            f"  handler={routing.properties.get('handler')}"
            f" action={routing.properties.get('action')}"
            f" reason={routing.properties.get('reason')}"
        )

    for event in interaction.media:
        lines.append("MEDIA")
        lines.append(
            f"  {event.properties.get('media_kind')} sha256={str(event.properties.get('sha256'))[:16]}…"
            f" ({event.properties.get('byte_size')} bytes)"
        )

    for ai in interaction.ai_calls:
        lines.append("AI REQUEST")
        lines.append(f"  purpose={ai.purpose} model={ai.model} op={ai.operation}")
        if ai.failed is not None:
            lines.append("AI RESPONSE")
            lines.append(
                f"  FAILED: {ai.failed.properties.get('error_type')}"
                f" ({ai.failed.properties.get('failure_class')})"
            )
        elif ai.completed is not None:
            lines.append("AI RESPONSE")
            lines.append(f"  {_short(ai.output)}")

    for validation in interaction.validations:
        lines.append("VALIDATION")
        lines.append(f"  {validation.event.rsplit('.', 1)[-1]} outcome={validation.outcome}")

    for decision in interaction.decisions:
        title = {
            taxonomy.DECISION_FINALIZED: "FINAL DECISION",
            taxonomy.DECISION_REPAIRED: "DECISION (repaired)",
            taxonomy.DECISION_FALLBACK_SELECTED: "DECISION (fallback selected)",
        }[decision.event]
        lines.append(title)
        detail = {
            key: value for key, value in decision.properties.items()
            if key in ("resolution", "reason", "overrides", "stage", "ai_action", "final_action")
            and value
        }
        lines.append(f"  entity={decision.entity} outcome={decision.outcome} {_short(detail)}")

    for mutation in interaction.state_mutations:
        lines.append("STATE")
        if mutation.event.startswith("flow."):
            flow_after = (mutation.properties.get("flow_after") or {})
            lines.append(f"  {mutation.event}: {flow_after.get('name')}@{flow_after.get('step')}")
        else:
            lines.append(
                f"  {mutation.properties.get('domain')}.{mutation.properties.get('action')}"
                f" ({mutation.entity_id or mutation.entity})"
            )

    for render in interaction.renders:
        lines.append("TELEGRAM RENDER")
        if render.text:
            lines.append(f'  "{_short(render.text)}"')
        for control in render.controls:
            lines.append(f"  [{control.get('label')}] → {control.get('callback_data')}")
        lines.append("DELIVERY")
        chain = " → ".join(
            f"{e.event.rsplit('.', 1)[-1]}({e.properties.get('operation')}"
            + (f":{e.properties.get('reason')}" if e.event == taxonomy.DELIVERY_FAILED else "")
            + ")"
            for e in render.deliveries
        ) or "no delivery events"
        lines.append(f"  {chain}")
        if render.delivered_message_id is not None:
            lines.append(f"  message_id={render.delivered_message_id}")

    for view in interaction.view_renders:
        lines.append("MINI APP VIEW")
        lines.append(
            f"  {view.properties.get('view')} trigger={view.properties.get('trigger')}"
        )

    for link in interaction.unresolved_links:
        lines.append(f"UNRESOLVED: {_short(link)}")
    lines.append("")


def render_timeline(trace: SessionTrace) -> str:
    """Deterministic human timeline — communicates the journey without
    requiring the reader to open raw event JSON."""
    lines: list[str] = [f"=== session trace · user {trace.user_id} ==="]
    for interaction in trace.interactions:
        _timeline_for_interaction(interaction, lines)
    if trace.legacy_events:
        lines.append(f"({len(trace.legacy_events)} legacy/uncorrelated events not shown inline)")
    return "\n".join(lines)
