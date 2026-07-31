"""Read-only foundation for the workout-slot model (A11a).

A *slot* is the stable professional need a plan expresses — "upper-chest press",
"horizontal pull" — as distinct from the exercise that implements it today. The
slot survives substitution, blocking and reordering; the implementation may
change beneath it.

**Nothing here writes anything.** A11a lands the vocabulary, the reader helpers
and the defensive guards so that when minting arrives (A11b) it lands into a
codebase that already tolerates every state a slot can be in. Every helper is a
pure function over a payload that may or may not carry slot data, and on today's
payloads — which carry none — every one is a no-op returning today's answer.

The distinction this module exists to hold is between three situations that all
look like "no exercise" if you squint, and must never be collapsed:

* **legacy** — a payload written before slots existed. Entirely valid. The
  overwhelmingly common case, and it must render exactly as it does today.
* **unmapped / blocked** — a slot deliberately without an implementation: not
  yet chosen, or withheld because of pain. A first-class state, not an error.
* **malformed** — a payload that violates the shape contract. It may render
  safely, but it must stay *observable*: silently treating it as "no exercise"
  is how a data defect becomes invisible.
"""

from __future__ import annotations

from typing import Any

from config import LOGGER

# ---------------------------------------------------------------------------
# Slot states
#
# Seven are defined by the product decision; three are SHIPPED here. The other
# four (`suggested`, `calibrating`, `temporarily_unavailable`, `retired`) have
# no producer yet, so rendering paths for them would be untested speculation.
# They are declared as accepted values so a payload carrying one is treated as a
# known state rather than malformed data, and so the vocabulary is fixed before
# anything writes it.
# ---------------------------------------------------------------------------
SLOT_STATE_MAPPED = "mapped"
SLOT_STATE_UNMAPPED = "unmapped"
SLOT_STATE_BLOCKED = "blocked"

#: Reserved: accepted on read, not yet produced or rendered.
SLOT_STATE_SUGGESTED = "suggested"
SLOT_STATE_CALIBRATING = "calibrating"
SLOT_STATE_TEMPORARILY_UNAVAILABLE = "temporarily_unavailable"
SLOT_STATE_RETIRED = "retired"

SHIPPED_SLOT_STATES = frozenset(
    {SLOT_STATE_MAPPED, SLOT_STATE_UNMAPPED, SLOT_STATE_BLOCKED}
)
RESERVED_SLOT_STATES = frozenset(
    {
        SLOT_STATE_SUGGESTED,
        SLOT_STATE_CALIBRATING,
        SLOT_STATE_TEMPORARILY_UNAVAILABLE,
        SLOT_STATE_RETIRED,
    }
)
KNOWN_SLOT_STATES = SHIPPED_SLOT_STATES | RESERVED_SLOT_STATES

#: Bounded reason codes for why an entry could not be read as an exercise.
#: Bounded so they can be logged and counted without becoming free text.
ENTRY_OK = "ok"
ENTRY_LEGACY_NO_SLOT = "legacy_no_slot"
ENTRY_UNMAPPED = "unmapped"
ENTRY_BLOCKED = "blocked"
ENTRY_MALFORMED = "malformed"


def slot_id_of(entry: Any) -> str | None:
    """The stable slot id of a plan entry, or None on a legacy entry.

    None is the normal answer today: no payload carries a slot id until A11b
    mints them. It means "this predates slots", never "this is broken".
    """
    if not isinstance(entry, dict):
        return None
    slot_id = entry.get("slot_id")
    return slot_id if isinstance(slot_id, str) and slot_id else None


def slot_state_of(entry: Any) -> str:
    """The declared state of a plan entry.

    Defaults to `mapped` because that is what every legacy entry is: an exercise
    the user is expected to perform. Defaulting to `unmapped` would reclassify
    every existing plan as incomplete.
    """
    if not isinstance(entry, dict):
        return SLOT_STATE_MAPPED
    state = entry.get("slot_state")
    if isinstance(state, str) and state in KNOWN_SLOT_STATES:
        return state
    return SLOT_STATE_MAPPED


def classify_entry(entry: Any) -> str:
    """Why an entry can or cannot be treated as a performable exercise.

    Returns one of the bounded ENTRY_* codes. This is the single place that
    decides whether an absence is legitimate or a data defect, so consumers do
    not each invent their own answer.
    """
    if not isinstance(entry, dict):
        return ENTRY_MALFORMED

    state = slot_state_of(entry)
    if state == SLOT_STATE_BLOCKED:
        return ENTRY_BLOCKED
    if state in (SLOT_STATE_UNMAPPED, SLOT_STATE_SUGGESTED):
        return ENTRY_UNMAPPED

    exercise_id = entry.get("id")
    if not isinstance(exercise_id, str) or not exercise_id:
        # A slot-bearing entry with no id is deliberately unmapped; a legacy
        # entry with no id is a payload defect, because pre-slot payloads have
        # no way to express "no exercise here".
        if slot_id_of(entry) is not None:
            return ENTRY_UNMAPPED
        return ENTRY_MALFORMED

    if slot_id_of(entry) is None:
        return ENTRY_LEGACY_NO_SLOT
    return ENTRY_OK


def is_performable(entry: Any) -> bool:
    """True when the entry names an exercise the user can be asked to perform."""
    return classify_entry(entry) in (ENTRY_OK, ENTRY_LEGACY_NO_SLOT)


def planned_sets_of(entry: Any) -> int:
    """The set count of an entry, or 0 when it contributes none.

    Exists because `sum(int(ex["sets"]) for ex in exercises)` raises on the
    first entry without a `sets` key — in the workout-summary path, i.e. after
    the user has already done the work. An unmapped slot legitimately has no
    sets; a malformed entry contributes none either, and is reported separately
    rather than being allowed to break the summary.
    """
    if not is_performable(entry):
        return 0
    try:
        return max(0, int(entry.get("sets") or 0))
    except (TypeError, ValueError):
        return 0


def observe_plan_entries(
    exercises: Any,
    *,
    user_id: int | None = None,
    session_id: int | None = None,
    context: str = "",
) -> dict[str, int]:
    """Count entries by classification, logging only when something is wrong.

    Returns bounded counts — never payload contents. A malformed entry is
    logged at WARNING with counts and internal ids only, so a data defect is
    visible to an operator instead of silently rendering as an empty slot.

    Legitimate absences (legacy, unmapped, blocked) are counted and NOT logged:
    they are normal, and logging them would bury the one case that matters.
    """
    counts: dict[str, int] = {}
    if not isinstance(exercises, list):
        counts[ENTRY_MALFORMED] = 1
        LOGGER.warning(
            "plan_entries_malformed context=%s user_id=%s session_id=%s "
            "reason=exercises_not_a_list",
            context or "unknown", user_id, session_id,
        )
        return counts

    for entry in exercises:
        code = classify_entry(entry)
        counts[code] = counts.get(code, 0) + 1

    malformed = counts.get(ENTRY_MALFORMED, 0)
    if malformed:
        LOGGER.warning(
            "plan_entries_malformed context=%s user_id=%s session_id=%s "
            "malformed=%d total=%d",
            context or "unknown", user_id, session_id, malformed, len(exercises),
        )
    return counts
