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

#: Not a member of the vocabulary -- the answer for a state that IS present but
#: is not one of the known values. Deliberately not in `KNOWN_SLOT_STATES`, so
#: it can never be written into a payload or accepted on read; it exists only as
#: the classifier's way of saying "this declared something I do not recognise".
SLOT_STATE_UNKNOWN = "unknown"

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

    Defaults to `mapped` when the key is **absent**, because that is what every
    legacy entry is: an exercise the user is expected to perform. Defaulting to
    `unmapped` would reclassify every existing plan as incomplete.

    An **explicitly present but unrecognised** state is a different situation
    and returns `SLOT_STATE_UNKNOWN` (A11b correction). Before, any unknown
    string silently became `mapped`: a version skew or a typo turned into a
    plausible-looking exercise the user would be told to perform, which is the
    silent-fallback failure this module exists to prevent. Absence is legacy;
    a wrong value is a defect, and the two must not share an answer.
    """
    if not isinstance(entry, dict):
        return SLOT_STATE_MAPPED
    if "slot_state" not in entry:
        return SLOT_STATE_MAPPED
    state = entry.get("slot_state")
    if isinstance(state, str) and state in KNOWN_SLOT_STATES:
        return state
    return SLOT_STATE_UNKNOWN


def classify_entry(entry: Any) -> str:
    """Why an entry can or cannot be treated as a performable exercise.

    Returns one of the bounded ENTRY_* codes. This is the single place that
    decides whether an absence is legitimate or a data defect, so consumers do
    not each invent their own answer.
    """
    if not isinstance(entry, dict):
        return ENTRY_MALFORMED

    state = slot_state_of(entry)
    if state == SLOT_STATE_UNKNOWN:
        # A11b: an explicitly declared state we do not recognise is a defect,
        # not a legacy entry. Classifying it `malformed` keeps it OUT of the
        # performable set and INSIDE the observability counter, so a version
        # skew surfaces as a logged malformed count instead of quietly becoming
        # an exercise the user is told to perform.
        return ENTRY_MALFORMED
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


# ---------------------------------------------------------------------------
# Minting (A11b)
#
# The reconciliation rule, and the evidence for it.
#
# A slot id must survive REAL regeneration -- the user asks for a new plan and
# the pipeline builds one from scratch. A random uuid copied along during a
# mutation would look stable in a substitution test and be worthless here: a
# regenerated plan mints new randomness and every slot identity is lost, which
# is precisely the defect recorded as "No identity survives regeneration".
#
# So the id is DERIVED, not random, from the professional need itself:
#
#     <session_code>:<template_ordinal>
#
# Evidence that this is stable:
#
# * `exercise_plans.PLANS[code]["exercises"]` is a static, literal, ordered list
#   -- measured deterministic across reads. Session code `A` is always "chest +
#   triceps", and its ordinal 0 is always the primary horizontal press.
# * `_schedule_sessions` copies that template verbatim (`copy.deepcopy(
#   PLANS[code]["exercises"])`), so ordinal N of a freshly built session is
#   ordinal N of the template.
# * Regenerating with the same split therefore reproduces the same ids by
#   construction, with no state carried between builds.
#
# Why it is NOT keyed on `exercise_id`: substitution changes the exercise while
# the need persists -- that is the whole point of a slot. Why not on the current
# list position: adaptation removes and backfills entries, so post-adaptation
# position drifts. The ids are therefore minted from the TEMPLATE ordinal,
# BEFORE `adapt_exercises` runs, and travel with the entry dict afterwards.
#
# Known limit, stated rather than hidden: if the split changes (3-day A/B/C to
# 4-day A/B/C/F), a session that did not exist before has no prior identity to
# preserve. That is correct -- a new session is a new professional need, not a
# renamed old one.
# ---------------------------------------------------------------------------
#: Separator between the session code and the ordinal. Chosen because ":" is
#: already the callback field separator, so a slot id must never be embedded
#: raw in callback data -- see `slot_token_of`.
_SLOT_ID_SEP = ":"

#: Max ordinal. A session with more entries than this is not a plan.
_MAX_SLOT_ORDINAL = 99


def mint_slot_id(session_code: Any, template_ordinal: Any) -> str | None:
    """The stable slot id for a template position, or None if unmintable.

    Returns None rather than inventing an id when the inputs cannot express a
    professional need: minting a placeholder would create an identity that
    means nothing and collides with every other unmintable entry.
    """
    code = str(session_code or "").strip()
    if not code or _SLOT_ID_SEP in code:
        return None
    try:
        ordinal = int(template_ordinal)
    except (TypeError, ValueError):
        return None
    if not 0 <= ordinal <= _MAX_SLOT_ORDINAL:
        return None
    return f"{code}{_SLOT_ID_SEP}{ordinal}"


def is_valid_slot_id(value: Any) -> bool:
    """True when `value` is a well-formed slot id.

    Validation is a real check, not a truthiness test: an id that does not
    round-trip through `mint_slot_id` cannot be reconciled against a regenerated
    plan, so accepting it would reintroduce the identity loss silently.
    """
    if not isinstance(value, str) or not value:
        return False
    code, separator, ordinal = value.partition(_SLOT_ID_SEP)
    if not separator:
        return False
    return mint_slot_id(code, ordinal) == value


def slot_token_of(entry: Any) -> str | None:
    """A slot id encoded for use inside callback data.

    `:` is the callback field separator, so a raw slot id would silently add a
    field and shift every later one. The token replaces it with `-`, which is
    absent from session codes and from the ordinal.

    The token is also deliberately unable to match the version grammar
    (`^v\\d{1,9}$`): it always contains a `-`, so a slot token in the terminal
    field can never be misread as a flow version.
    """
    slot_id = slot_id_of(entry)
    if slot_id is None:
        return None
    return slot_id.replace(_SLOT_ID_SEP, "-")


def slot_id_from_token(token: Any) -> str | None:
    """Inverse of `slot_token_of`, rejecting anything that is not a slot id."""
    if not isinstance(token, str) or not token:
        return None
    candidate = token.replace("-", _SLOT_ID_SEP, 1)
    return candidate if is_valid_slot_id(candidate) else None


def assign_slot_ids(exercises: Any, session_code: Any) -> int:
    """Stamp slot ids onto a freshly built session, in place. Returns the count.

    Called on the TEMPLATE copy, before adaptation, so the ordinal is the
    template's. Never overwrites an existing valid id -- a repaired or migrated
    payload keeps the identity it already had.
    """
    if not isinstance(exercises, list):
        return 0
    minted = 0
    for ordinal, entry in enumerate(exercises):
        if not isinstance(entry, dict):
            continue
        if is_valid_slot_id(entry.get("slot_id")):
            continue
        slot_id = mint_slot_id(session_code, ordinal)
        if slot_id is None:
            continue
        entry["slot_id"] = slot_id
        minted += 1
    return minted


def find_by_slot_id(exercises: Any, slot_id: Any) -> tuple[int, dict[str, Any]] | None:
    """Locate an entry by slot identity. Returns (position, entry) or None.

    This is what replaces `list.index(value)`: identity is matched, never
    content and never position, so a reordered or re-ranked list resolves the
    same slot -- or nothing at all, which is a stale callback rather than a
    wrong exercise.
    """
    if not isinstance(exercises, list) or not is_valid_slot_id(slot_id):
        return None
    for position, entry in enumerate(exercises):
        if isinstance(entry, dict) and entry.get("slot_id") == slot_id:
            return position, entry
    return None


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
