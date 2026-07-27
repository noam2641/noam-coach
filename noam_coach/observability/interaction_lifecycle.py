"""Guaranteed termination of the interaction lifecycle (W1-12).

The defect this module closes
-----------------------------
Every interaction emitted ``interaction.received`` and nothing ever closed
it. All 87 interactions in the 2026-07-27 session were formally unterminated:
no latency, no verdict, and — worst — no way to distinguish "handled" from
"silently dropped".

That gap hid a real bug. A ``menu:goals`` button emitted by the bot's own
top-priority CTA was handled by nothing. The user tapped it four times and
got no reply, no render, and no event. In the stream that was
INDISTINGUISHABLE from a working button, because ``routing.decided`` is
written BEFORE dispatch: it proves a handler was *chosen*, never that one
ran or produced output. A full audit passed over the bug for exactly this
reason.

Why termination lives here and not in the handlers
--------------------------------------------------
A terminal event that each handler must remember to emit is the same gap in
a new shape — the one handler that forgets is precisely the broken one. So
the event is emitted from the ``finally`` of the scope that wraps dispatch:
it fires on a clean return, on an exception, and on cancellation. There is
no code path through :func:`observed_interaction` that skips it.

Why not inside ``obs_context.interaction_scope``
------------------------------------------------
``interaction_scope`` is a synchronous contextmanager carrying pure
contextvar state; emitting needs a db handle and an ``await``. Making it
async would force every existing caller to change and would drag a storage
dependency into the correlation primitive. This module composes the two
instead: it owns the scope, so termination still cannot be forgotten.

Outcome determination
---------------------
Decided at scope exit from what actually happened, in priority order:

- ``error``      — an ``Exception`` propagated out of the handler. The
                   exception is always re-raised; the verdict never changes
                   control flow.
- ``cancelled``  — a ``BaseException`` (``asyncio.CancelledError``, shutdown)
                   unwound the scope. Distinct from ``error``: a cancelled
                   interaction is an operational event, not a defect, and
                   collapsing the two would make shutdown noise look like
                   crashes.
- ``no_handler`` — a clean return that produced NO response-bearing event
                   (see ``emit.RESPONSE_EVENTS``). This is the ``menu:goals``
                   signature and the whole reason the event exists.
- ``handled``    — a clean return that produced at least one response.

``no_handler`` is named for the observable fact ("nothing answered the
user"), not an inferred cause. A handler that ran, deliberately chose to
stay silent, and rendered nothing is also reported as ``no_handler`` — that
is correct: from the user's side those two situations are the same, and
representing a silent-by-design path as ``handled`` would re-open the exact
blind spot this event was added to close.

Placement of the duration
-------------------------
``duration_ms`` goes in ``properties``, never ``content``. Per the emit
module contract, ``properties`` is metadata kept in every non-OFF mode,
while ``content`` is conversational payload reduced to a digest under
METADATA mode. Latency is operational metadata that carries no user
utterance; putting it in ``content`` would make it a sha256 digest in the
default production mode and destroy the ability to detect slow or dropped
interactions — which is most of the point.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from noam_coach.observability import obs_context, taxonomy
from noam_coach.observability.emit import emit_event

OUTCOME_HANDLED = "handled"
OUTCOME_NO_HANDLER = "no_handler"
OUTCOME_ERROR = "error"
OUTCOME_CANCELLED = "cancelled"

STATUS_FOR_OUTCOME: dict[str, str] = {
    OUTCOME_HANDLED: "completed",
    OUTCOME_NO_HANDLER: "completed",
    OUTCOME_ERROR: "failed",
    OUTCOME_CANCELLED: "cancelled",
}


def classify_outcome(exc: BaseException | None, responses: int) -> str:
    """The interaction's verdict from how the scope exited.

    Pure and separately testable: the wrapper only supplies the two facts.
    """
    if exc is not None:
        return OUTCOME_ERROR if isinstance(exc, Exception) else OUTCOME_CANCELLED
    return OUTCOME_HANDLED if responses > 0 else OUTCOME_NO_HANDLER


async def emit_interaction_completed(
    db: Any,
    user_id: int,
    *,
    outcome: str,
    duration_ms: int,
    responses: int,
    properties: dict[str, Any] | None = None,
) -> None:
    """Best-effort terminal event. Never raises.

    ``emit_event`` already contains its own failures, but this is called from
    a ``finally`` that may already be unwinding an exception — a raise here
    would REPLACE the handler's exception and lose the original defect. The
    extra guard makes that structurally impossible.
    """
    payload: dict[str, Any] = {
        "outcome": outcome,
        # Kept in properties (metadata), not content: latency must survive
        # METADATA mode, where content becomes a digest. See module docstring.
        "duration_ms": duration_ms,
        "responses": responses,
    }
    if properties:
        payload.update(properties)
    try:
        await emit_event(
            db,
            user_id,
            taxonomy.INTERACTION_COMPLETED,
            entity="interaction",
            source="telegram",
            surface=properties.get("surface") if properties else None,
            status=STATUS_FOR_OUTCOME.get(outcome, "completed"),
            outcome=outcome,
            properties=payload,
        )
    except BaseException:  # noqa: BLE001 — observability never breaks coaching.
        pass


@asynccontextmanager
async def observed_interaction(
    db: Any,
    user_id: int,
    *,
    trace_id: str | None = None,
    interaction_id: str | None = None,
    properties: dict[str, Any] | None = None,
) -> AsyncIterator[obs_context.InteractionScope]:
    """Open a correlated interaction that is GUARANTEED to terminate.

    Wraps :func:`obs_context.interaction_scope` and emits exactly one
    ``interaction.completed`` on every exit path — clean return, exception,
    or cancellation. Exceptions propagate unchanged.
    """
    started = time.monotonic()
    exc: BaseException | None = None
    with obs_context.interaction_scope(
        trace_id=trace_id, interaction_id=interaction_id, user_id=user_id
    ) as scope:
        try:
            yield scope
        except BaseException as raised:
            exc = raised
            raise
        finally:
            # Read INSIDE the scope: interaction_scope resets the response
            # contextvar on exit, so counting after it closes would always
            # report zero and label every interaction no_handler.
            responses = obs_context.response_count()
            duration_ms = int((time.monotonic() - started) * 1000)
            outcome = classify_outcome(exc, responses)
            extra = dict(properties or {})
            if exc is not None:
                extra["error_type"] = type(exc).__name__
            await emit_interaction_completed(
                db,
                user_id,
                outcome=outcome,
                duration_ms=duration_ms,
                responses=responses,
                properties=extra,
            )
