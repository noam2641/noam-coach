"""Unified meal-photo persistence + missing-image observability (audit F-A9).

Production evidence: an analyzed meal photo (MEDIA_003) could not be
recovered — its bytes were persisted then deleted by a reject, and the
media event carried no provider file id, so nothing in the trace
explained the gap. This module makes photo persistence a single evidenced
operation and gives a deterministic answer to "why is this image
missing?".

Two responsibilities:

1. ``persist_meal_photo`` — the one place raw meal-photo bytes are written
   to storage. It writes the file AND emits ``state.mutated``
   (domain=media, action=persisted) recording the storage path, sha256
   prefix, byte size and the Telegram file_unique_id. Combined with the
   reject-time ``action=deleted`` event (meal_approval_lifecycle) and the
   ``media.received`` analysis event, a photo's whole storage life is
   reconstructable.

2. ``classify_missing_image`` — differentiates WHY an image reference does
   not resolve, so a "missing image" is never a shrug:
     - no_reference       — nothing recorded a path or media id;
     - storage_object_gone — a path was recorded but the file is absent;
     - deleted_on_reject  — a deletion event explains the absence;
     - provider_only      — only a Telegram file id exists (never persisted);
     - present            — the file is on disk.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from noam_coach.observability import taxonomy
from noam_coach.observability.emit import emit_event


def _db() -> Any:
    import coach_bot

    return coach_bot.DB


async def persist_meal_photo(
    user_id: int,
    image_bytes: bytes,
    path: Path,
    *,
    provider_file_unique_id: str | None = None,
) -> str:
    """Write meal-photo bytes to ``path`` and emit persistence evidence.

    Returns the sha256 hex digest of the bytes (the stable content id that
    ties this file to the ``media.received`` analysis event). Persistence
    evidence is best-effort and never blocks the write.
    """
    import asyncio

    await asyncio.to_thread(path.write_bytes, image_bytes)
    sha = hashlib.sha256(image_bytes).hexdigest()
    try:
        await emit_event(
            _db(),
            user_id,
            taxonomy.STATE_MUTATED,
            entity="media",
            entity_id=sha[:16],
            source="meal_pipeline",
            status="persisted",
            outcome="persisted",
            properties={
                "domain": "media",
                "action": "persisted",
                "storage_ref": str(path),
                "sha256_prefix": sha[:16],
                "byte_size": len(image_bytes),
                "provider_file_unique_id": provider_file_unique_id,
            },
        )
    except Exception:  # noqa: BLE001 — evidence must not break persistence.
        pass
    return sha


async def _has_deletion_event(user_id: int, sha_prefix: str | None) -> bool:
    if not sha_prefix:
        # No content id to match; fall back to "any media deletion for this
        # user" is too broad — treat as unknown (caller decides).
        return False
    import event_log

    events = await event_log.list_events(
        _db(), user_id, event=taxonomy.STATE_MUTATED, limit=2000
    )
    for event in events:
        props = event.properties or {}
        if props.get("action") == "deleted" and props.get("domain") == "media":
            if props.get("sha256_prefix") == sha_prefix:
                return True
    return False


async def classify_missing_image(
    user_id: int,
    *,
    image_path: str | None,
    media_sha_prefix: str | None = None,
    provider_file_unique_id: str | None = None,
) -> str:
    """Deterministically classify why an image reference does not resolve.

    Returns one of: present | storage_object_gone | deleted_on_reject |
    provider_only | no_reference.
    """
    if image_path:
        if Path(image_path).exists():
            return "present"
        if await _has_deletion_event(user_id, media_sha_prefix):
            return "deleted_on_reject"
        return "storage_object_gone"
    if provider_file_unique_id:
        return "provider_only"
    if await _has_deletion_event(user_id, media_sha_prefix):
        return "deleted_on_reject"
    return "no_reference"
