"""Pain state, and how long it is allowed to survive (A7).

Pain lives in two stores with different lifetimes, and the mismatch is the
defect:

* `medical_constraints` rows are the safety record. `active_pain_regions`
  applies a 14-day TTL to them, so a report ages out of the runtime.
* the `training_limitations` fact is what planning reads. Its `FactSpec`
  declares `expires_after_days=30`.

That leaves a **16-day window** where the constraint has expired — the workout
runtime no longer treats the region as painful — while the planning fact still
asserts an active limitation. And the mirror that wrote the fact appended to a
free-text location string, so regions accumulated: report an elbow once and a
knee once, and the fact says "elbow, knee" forever, with nothing that can ever
remove either.

The fix is to derive rather than accumulate. The fact is recomputed from the
constraint rows that are *currently* active, which makes expiry free — a region
that ages out of `active_pain_regions` simply stops appearing — and makes the
operation idempotent, so a repeated report cannot double an entry.

Four states are kept distinct, because collapsing them is what let a one-time
report become permanent:

* **temporary event** — a pain report inside the TTL. Real, and expected to age.
* **active limitation** — currently constraining exercise selection.
* **confirmed limitation** — text the user themselves stated in onboarding
  ("herniated disc"). Not derived from any constraint row, and **must survive a
  recompute** — it is not pain that ages out, it is a standing medical fact.
* **historical** — expired. Absent from the fact, still present in
  `medical_constraints` for audit.

Nothing here writes `medical_constraints`; the safety record is authoritative
and A7 only derives the projection from it.
"""

from __future__ import annotations

from typing import Any

import training_intelligence
import user_model
from config import LOGGER

#: Marker for a limitation the user stated rather than one derived from a pain
#: report. Recompute preserves anything carrying it.
SOURCE_USER_STATED = "user_stated"
SOURCE_DERIVED_FROM_PAIN = "derived_from_pain"

#: Bounded outcome codes, safe to log and count.
SYNC_UPDATED = "updated"
SYNC_UNCHANGED = "unchanged"
SYNC_CLEARED = "cleared"
SYNC_FAILED = "failed"


def _existing_location_text(value: Any) -> str:
    """The free-text location from whatever shape the fact currently holds.

    Legacy installs stored a bare string; later ones a dict under `location`,
    `details` or `note`. All three still exist in the wild.
    """
    if isinstance(value, dict):
        return str(
            value.get("location") or value.get("details") or value.get("note") or ""
        )
    if isinstance(value, str) and value != "none":
        return value
    return ""


def _user_stated_part(value: Any) -> str:
    """The portion of an existing fact the user stated, which must survive.

    A recompute derived purely from pain rows would erase a standing medical
    limitation someone typed during onboarding — a herniated disc does not
    expire because no one reported elbow pain in the last fortnight. When the
    fact records its provenance we trust it; when it does not (every legacy
    row) we cannot tell stated text from derived text, so the conservative
    reading is to treat it as stated and keep it. Losing a real medical
    constraint is far worse than carrying a stale one.
    """
    if isinstance(value, dict):
        origin = value.get("origin")
        if origin == SOURCE_DERIVED_FROM_PAIN:
            return ""
        stated = value.get("user_stated")
        if isinstance(stated, str):
            return stated
    return _existing_location_text(value)


def compose_limitation_value(
    user_stated: str, pain_labels: list[str]
) -> dict[str, Any] | None:
    """The fact value for a given stated text and set of active pain regions.

    Returns None when there is nothing to assert — no stated limitation and no
    active pain — which is what lets an expired report clear the fact instead of
    leaving it asserting something untrue.

    `origin` is recorded so a later recompute can tell which part it owns.
    Without it the next run cannot distinguish text it wrote from text the user
    typed, which is precisely how the append-merge became irreversible.
    """
    parts = [part for part in ([user_stated] + pain_labels) if part]
    if not parts:
        return None

    # Deduplicate while preserving order: the stated limitation reads first.
    seen: set[str] = set()
    ordered: list[str] = []
    for part in parts:
        if part not in seen:
            seen.add(part)
            ordered.append(part)

    origin = (
        SOURCE_USER_STATED
        if user_stated and not pain_labels
        else SOURCE_DERIVED_FROM_PAIN
        if pain_labels and not user_stated
        else "mixed"
    )
    return {
        "location": ", ".join(ordered),
        "status": "active",
        "origin": origin,
        "user_stated": user_stated,
    }


async def sync_training_limitations(db: Any, user_id: int) -> str:
    """Recompute the planning fact from currently-active pain constraints.

    Derivation, not accumulation. A region inside the TTL appears; a region that
    has aged out does not; a limitation the user stated survives regardless.
    Idempotent by construction — running it twice changes nothing the second
    time — which is what makes it safe to call after every pain report.

    Returns a bounded outcome code. Never raises: this is a projection, and the
    `medical_constraints` row it derives from is already durable by the time it
    runs. A failure here must not break a pain report the user just made.
    """
    try:
        rows = await db.fetch_all(
            "SELECT * FROM medical_constraints WHERE user_id=? AND kind='pain'",
            (user_id,),
        )
        regions = training_intelligence.active_pain_regions(rows or [])
        pain_labels = [
            label
            for label in (
                training_intelligence.pain_region_label(region) for region in regions
            )
            if label
        ]

        existing = await user_model.get_value(db, user_id, "training_limitations")
        user_stated = _user_stated_part(existing)
        value = compose_limitation_value(user_stated, pain_labels)

        if value is None:
            # Nothing to assert. Marking the fact invalid rather than deleting
            # it keeps the history readable while stopping planning from acting
            # on an expired report.
            if existing is None:
                return SYNC_UNCHANGED
            await user_model.invalidate_fact(db, user_id, "training_limitations")
            LOGGER.info(
                "pain_limitation_cleared user_id=%s regions=%d", user_id, len(regions)
            )
            return SYNC_CLEARED

        if isinstance(existing, dict) and existing.get("location") == value["location"]:
            return SYNC_UNCHANGED

        await user_model.set_fact(
            db,
            user_id,
            "training_limitations",
            value,
            kind=user_model.KIND_FACT,
            source=user_model.SOURCE_USER,
            confirmed=True,
        )
        LOGGER.info(
            "pain_limitation_synced user_id=%s regions=%d origin=%s",
            user_id, len(regions), value["origin"],
        )
        return SYNC_UPDATED
    except Exception:
        # Bounded fields only -- a limitation string can name a body part and a
        # medical condition, so it never reaches the log.
        LOGGER.exception("pain_limitation_sync_failed user_id=%s", user_id)
        return SYNC_FAILED


__all__ = [
    "SOURCE_DERIVED_FROM_PAIN",
    "SOURCE_USER_STATED",
    "SYNC_CLEARED",
    "SYNC_FAILED",
    "SYNC_UNCHANGED",
    "SYNC_UPDATED",
    "compose_limitation_value",
    "sync_training_limitations",
]
