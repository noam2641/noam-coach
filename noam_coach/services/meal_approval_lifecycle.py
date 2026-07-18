"""Canonical meal-approval lifecycle helpers (audit 2026-07-18, F-A1/F-A5/F-A9).

The production incident: the user rejected a meal from a card that
predated a text correction, which consumed the ONLY approval backing the
corrected card — eleven subsequent ✅ presses were then dropped and the
meal was unrecoverable. This module centralizes the lifecycle rules so
every decision path shares them:

- **Revisions** — every payload update bumps ``payload["revision"]``;
  decision controls carry ``:r<rev>``. A press whose revision no longer
  matches decides *content the user is not looking at* and is refused
  with the CURRENT card re-rendered (recoverable, never destructive).
  Legacy unversioned controls (pre-fix messages) keep their historical
  behavior of acting on current content.
- **Truthful terminals** — a press on a decided approval states what
  actually happened (saved / rejected / unavailable), never a generic or
  false message; a rejected approval offers explicit restoration.
- **Restoration** — a rejected approval's payload is preserved in the
  approvals table, so ``restore`` mints a fresh pending approval from it:
  silent meal loss is impossible.
- **One observable outcome per decision** — approve is evidenced by the
  persistence events; reject emits ``decision.finalized``
  (entity=meal_approval, outcome=rejected); every refusal emits the
  canonical control-refusal event; the reject-time image deletion emits
  ``state.mutated`` (domain=media, action=deleted) so a later "missing
  image" is explainable from the trace (F-A9).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from noam_coach.observability import taxonomy
from noam_coach.observability.emit import emit_event


def _db() -> Any:
    import coach_bot

    return coach_bot.DB


def parse_decision_token(data: str) -> tuple[str, int | None]:
    """``approve_meal:<id>[:r<rev>]`` → (approval_id, revision | None)."""
    payload = data.split(":", 1)[1]
    if ":" in payload:
        approval_id, tail = payload.rsplit(":", 1)
        if tail.startswith("r") and tail[1:].isdigit():
            return approval_id, int(tail[1:])
    return payload, None


def is_stale_revision(row: dict[str, Any], revision: int | None) -> bool:
    """True when the pressed control's revision predates the payload's.

    ``None`` (legacy unversioned control) is never considered stale — that
    preserves the pre-fix behavior for messages rendered before this
    change.
    """
    if revision is None:
        return False
    current = int((row.get("data") or {}).get("revision", 0) or 0)
    return revision != current


def bump_revision(payload: dict[str, Any]) -> int:
    """Increment and return the payload revision (call before persisting)."""
    payload["revision"] = int(payload.get("revision", 0) or 0) + 1
    return payload["revision"]


async def emit_approval_rejected(
    user_id: int, approval_id: str, *, had_image: bool
) -> None:
    """Canonical evidence for a rejected approval (one event per reject)."""
    try:
        await emit_event(
            _db(),
            user_id,
            taxonomy.DECISION_FINALIZED,
            entity="meal_approval",
            entity_id=approval_id,
            source="meal_approval",
            status="decided",
            outcome="rejected",
            properties={"had_image": had_image},
        )
    except Exception:  # noqa: BLE001 — evidence must not break the decision.
        pass


async def emit_media_deleted(
    user_id: int, image_path: str, *, reason: str
) -> None:
    """F-A9: the reject-time photo deletion becomes trace evidence — a
    storage file that later cannot be found is explainable, never silent."""
    try:
        path = Path(image_path)
        existed = path.exists()
        sha_prefix = None
        if existed:
            sha_prefix = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
        await emit_event(
            _db(),
            user_id,
            taxonomy.STATE_MUTATED,
            entity="media",
            source="meal_approval",
            status="deleted",
            outcome="deleted" if existed else "already_missing",
            properties={
                "domain": "media",
                "action": "deleted",
                "reason": reason,
                "file_existed": existed,
                "sha256_prefix": sha_prefix,
            },
        )
    except Exception:  # noqa: BLE001
        pass


async def restore_rejected_approval(user_id: int, approval_id: str) -> str | None:
    """Mint a fresh pending approval from a rejected one (F-A1 recovery).

    Returns the NEW approval id, or None when the source is not a
    rejected meal approval. The image reference is dropped when the file
    no longer exists (reject deletes it — the restored card says so).
    """
    from noam_coach.services.core import create_approval, fetch_approval_any

    row = await fetch_approval_any(user_id, approval_id)
    if not row or row.get("status") != "rejected":
        return None
    if row.get("kind") not in ("meal", "meal_edit"):
        return None
    payload = dict(row["data"])
    payload.pop("edit_meal_id", None)
    payload["revision"] = 0
    payload["restored_from"] = approval_id
    image = payload.get("image")
    image_missing = bool(image) and not Path(str(image)).exists()
    if image_missing:
        payload.pop("image", None)
        payload["image_deleted_on_reject"] = True
    new_id = await create_approval(
        user_id,
        "meal",
        payload,
        telegram_file_unique_id=row.get("telegram_file_unique_id"),
    )
    try:
        await emit_event(
            _db(),
            user_id,
            taxonomy.STATE_MUTATED,
            entity="meal_approval",
            entity_id=new_id,
            source="meal_approval",
            status="restored",
            outcome="restored",
            properties={
                "domain": "meal_approval",
                "action": "restored",
                "image_available": not image_missing and bool(image),
            },
        )
    except Exception:  # noqa: BLE001
        pass
    return new_id


async def render_decided_terminal(query: Any, user_id: int, approval_id: str) -> None:
    """Truthful terminal card for a press on a non-pending approval.

    - approved  → the meal was saved; say so.
    - rejected  → it was rejected; offer explicit restoration.
    - unknown   → the card is simply no longer available.
    Every branch also emits the canonical control refusal.
    """
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    import coach_bot
    from noam_coach.services.control_refusal import refuse_control
    from noam_coach.services.core import fetch_approval_any

    row = await fetch_approval_any(user_id, approval_id)
    status = (row or {}).get("status")
    if status == "rejected":
        await refuse_control(
            query, user_id, reason="approval_already_rejected", source="meal_approval",
        )
        await coach_bot.safe_edit(
            query,
            "הארוחה הזו נדחתה קודם ולא נשמרה.\n"
            "אפשר לשחזר אותה ולערוך לפני שמירה:",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("♻️ שחזר את הארוחה", callback_data=f"restore_meal:{approval_id}")],
                [InlineKeyboardButton("⬅️ תפריט", callback_data="menu:home")],
            ]),
        )
        return
    await refuse_control(
        query, user_id, reason="approval_already_handled", source="meal_approval",
    )
    if status == "approved":
        await coach_bot.safe_edit(query, "הארוחה הזו כבר נשמרה ✅", coach_bot.home_keyboard())
    else:
        await coach_bot.safe_edit(query, "הכרטיס הזה כבר לא זמין.", coach_bot.home_keyboard())


async def represent_resumed_meal_card(query: Any, user_id: int) -> bool:
    """F-A5: after a meal decision, a resumed suspended meal flow must
    RE-PRESENT its card — a flow the user cannot see is a lost meal.

    Sends the resumed card as a NEW message (the current message already
    shows the decision outcome). Returns True when a card was re-presented.
    """
    import coach_bot
    from noam_coach.services.core import fetch_approval, get_meal_fix

    approval_id, refine_count = await get_meal_fix(user_id)
    if not approval_id:
        return False
    row = await fetch_approval(user_id, approval_id)
    if not row:
        return False
    message = getattr(query, "message", None)
    if message is None or not hasattr(message, "reply_text"):
        return False

    class _ReplyTarget:
        """render_meal target that always sends a NEW message (its edit_text
        replies), so the decision card the user just acted on is preserved."""

        def __init__(self, source: Any) -> None:
            self._source = source

        async def edit_text(self, text: str, reply_markup: Any = None, parse_mode: Any = None) -> None:
            await self._source.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)

    try:
        await message.reply_text("ממשיכים בארוחה שממתינה לאישור ↩️")
        await coach_bot.render_meal(_ReplyTarget(message), user_id, approval_id, refine_count=refine_count)
        return True
    except Exception:  # noqa: BLE001 — re-presentation must not break the decision.
        return False


def payload_revision_json(row_data: dict[str, Any]) -> str:
    """Serialize a payload after bumping its revision (single write shape)."""
    bump_revision(row_data)
    return json.dumps(row_data, ensure_ascii=False)
