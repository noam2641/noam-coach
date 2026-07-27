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

# A single leading "#" only counts as a note when the message also CLOSES with
# one. That wrapped form is unambiguous, while a bare leading "#" is not:
# "#3 בבוקר" is a plausible thing to type at a coach. Requiring the closing
# marker keeps the single-hash form safe to accept.
_MIN_WRAPPED_BODY = 1


def _wrapped_single_hash(text: str) -> bool:
    stripped = text.strip()
    if not stripped.startswith("#") or stripped.startswith(DEV_NOTE_PREFIX):
        return False
    if not stripped.endswith("#"):
        return False
    return len(stripped) >= 2 + _MIN_WRAPPED_BODY


def is_dev_note(text: str) -> bool:
    """True when ``text`` is a developer comment.

    Two forms are accepted:

    * a leading ``##`` -- the documented protocol;
    * a message wrapped in single hashes, ``#...#``.

    The wrapped form is here because it is what actually gets typed. The
    guard originally required ``##`` only, and in the 2026-07-27 session all
    five developer notes used ``#...#``: every one fell through to intent
    classification. One was stored as ``user_facts.food_environment_context``
    with ``confirmed=1`` -- a complaint about the bot's questioning, filed as
    the user's dietary profile -- and another was classified ``build_plan``
    and regenerated the weekly workout plan mid-set.

    A ``#`` appearing anywhere else is ordinary user text: "כמה קלוריות ב#1?"
    must stay a real question, and so must a bare leading "#3 בבוקר".
    """
    if not text:
        return False
    return text.lstrip().startswith(DEV_NOTE_PREFIX) or _wrapped_single_hash(text)


def strip_prefix(text: str) -> str:
    """Return the note body without its markers.

    Handles both accepted forms, including a wrapped ``#...#`` whose trailing
    marker must come off too.
    """
    stripped = text.strip()
    if stripped.startswith(DEV_NOTE_PREFIX):
        return stripped[len(DEV_NOTE_PREFIX) :].strip()
    if _wrapped_single_hash(stripped):
        return stripped[1:-1].strip()
    return stripped


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
