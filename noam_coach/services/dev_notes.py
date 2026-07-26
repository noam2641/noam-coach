"""Developer commentary captured mid-conversation.

A message whose text starts with ``##`` is a note *about the product*, not a
statement about the user. Before this module existed such messages fell
through to intent classification and were persisted as user data: one landed
in ``diet_restrictions`` and was echoed back on the profile screen as a
dietary preference, and another was stored as a meal.

The guard deliberately runs before ``ConversationRouter.route`` -- routing
calls ``expire_if_needed``, which can expire and clear an active flow as a
side effect. Intercepting afterwards would let a comment typed mid-workout
disturb the very flow it was commenting on.

Nothing here writes to ``user_facts``, ``meals`` or ``approvals``; no
coaching read path consults ``dev_notes``.
"""

from __future__ import annotations

import json
from contextlib import suppress
from typing import Any

from helpers import utc_now

DEV_NOTE_PREFIX = "##"

# Kept short on purpose: the reply is an acknowledgement, not a conversation
# turn. A longer message would read as a prompt and invite a follow-up that
# the (still-armed) active flow would then have to compete with.
ACK_TEXT = "נרשם כהערה ✅"

# Guards against a runaway paste becoming an unbounded row.
MAX_NOTE_LENGTH = 4000


def is_dev_note(text: str) -> bool:
    """True when ``text`` is a developer comment.

    Only a leading ``##`` counts. A ``#`` appearing mid-message is ordinary
    user text -- "כמה קלוריות ב#1?" must stay a real question.
    """
    return text.lstrip().startswith(DEV_NOTE_PREFIX)


def strip_prefix(text: str) -> str:
    """Return the note body without its ``##`` marker."""
    return text.lstrip()[len(DEV_NOTE_PREFIX) :].strip()


async def record_dev_note(
    db: Any,
    user_id: int,
    text: str,
    *,
    context: dict[str, Any] | None = None,
) -> None:
    """Persist a developer note.

    Failure is swallowed: losing a note is bad, but breaking the user's
    active conversation because a note could not be written is worse.
    """
    body = strip_prefix(text)[:MAX_NOTE_LENGTH]
    with suppress(Exception):
        await db.execute(
            "INSERT INTO dev_notes(user_id, text, context, created_at) VALUES(?, ?, ?, ?)",
            (
                user_id,
                body,
                json.dumps(context or {}, ensure_ascii=False),
                utc_now(),
            ),
        )


def flow_context(flow: Any) -> dict[str, Any]:
    """Describe the flow a note was written during, for later correlation.

    Only structural fields are captured -- never ``flow.payload``, which can
    carry meal text or other user content.
    """
    if flow is None:
        return {}
    context: dict[str, Any] = {}
    name = getattr(flow, "name", "") or ""
    step = getattr(flow, "step", "") or ""
    if name:
        context["flow"] = name
    if step:
        context["step"] = step
    flow_id = getattr(flow, "flow_id", "") or ""
    if flow_id:
        context["flow_id"] = flow_id
    return context
