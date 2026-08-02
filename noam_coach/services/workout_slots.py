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
    if state in RESERVED_SLOT_STATES:
        # A11b: the remaining reserved states (`calibrating`, `retired`,
        # `temporarily_unavailable`) fell through to the id check and came back
        # `ok` -- so a RETIRED slot was performable and the user would have been
        # told to train it. They have no producer yet, but "accepted on read"
        # must mean "not treated as a normal exercise", not "silently normal".
        # Reported as unmapped: a slot that exists without an implementation the
        # user should perform, which is exactly what each of them describes.
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
# regenerated plan mints new randomness and every identity is lost, which is
# precisely the defect recorded as "No identity survives regeneration".
#
# So the id is DERIVED, from two DECLARED keys:
#
#     <stable_session_key>:<slot_key>
#
# e.g. `abc1_a:bench` and `abc2_a:bench` for the two A-sessions of a 6-day
# split. Both components are declared against a definition, not computed from
# how a list happens to be walked:
#
# * `slot_key` is declared by `exercise_plans.exercise(...)`, defaulting to the
#   seed exercise id. Reordering a template's exercises reassigns nothing.
# * `session_key` is declared by `exercise_plans.SESSION_KEYS_BY_FREQUENCY`
#   (see `session_keys_for_split`). Reordering a split moves a session without
#   renaming it.
#
# Two earlier schemes were tried and measured wrong:
#
# 1. `<session_code>:<template_ordinal>` -- a session code is not unique in a
#    split, so a 6-day plan (`A,B,C,A,B,C`) produced 10 duplicate ids out of 20.
# 2. `<code>#<Nth appearance>:<slot_key>` -- the counter is recomputed while
#    traversing, so two semantically distinct sessions sharing a code SWAPPED
#    identities when the split order changed, reattributing every slot in both
#    to the other session's professional meaning.
#
# The second is the subtler failure, and a test that reverses a list AFTER
# minting cannot see it: that proves an id travels with an object, not that a
# fresh build reproduces the same id for the same need.
#
# It is derived from none of: weekday (a scheduling choice the user changes,
# and one A9 realignment moves), array position, the current exercise id
# (substitution changes it -- the whole point of a slot), or a traversal
# counter.
#
# Minting happens on the TEMPLATE copy, BEFORE `adapt_exercises` runs, because
# adaptation removes and backfills entries; a backfilled replacement then
# INHERITS the identity of the entry it replaces rather than being minted anew.
#
# Known limit, stated rather than hidden: if a split structurally adds or
# removes a session, the added one has no prior identity to preserve. That is
# correct -- a new session is a new professional need, not a renamed old one.
# ---------------------------------------------------------------------------
#: Separator between the two identity components. Chosen because ":" is already
#: the callback field separator, so a slot id must never be embedded raw in
#: callback data -- see `slot_token_of`.
_SLOT_ID_SEP = ":"

#: Separator inside a callback token. A key containing it is refused at
#: minting, which is what makes `slot_token_of` / `slot_id_from_token`
#: unambiguously reversible rather than "reversible unless a key has a dash".
_TOKEN_SEP = "-"

#: Max ordinal. A session with more entries than this is not a plan.
_MAX_SLOT_ORDINAL = 99


def session_keys_for_split(split: Any, declared: Any) -> list[str]:
    """The stable professional key of every session in a split.

    `declared` comes from the split's own definition --
    `exercise_plans.SESSION_KEYS_BY_FREQUENCY` for a default split, or
    `planning._SPLIT_OVERRIDE_SESSION_KEYS` for a strategy override. A key
    names what the session IS, so reordering a split moves a session without
    renaming it.

    **There is deliberately no positional fallback.** An earlier version fell
    back to `<code>_<n>`, counting appearances while iterating, and that
    reintroduced the exact defect the declared keys exist to prevent: two
    semantically distinct sessions sharing a code swap identities when the
    order changes. Measured on the consistency 3-day override (`F/F/F`), where
    three sessions share one code and nothing but position separated them.

    A mismatch returns `[]` rather than inventing keys. The caller then mints
    nothing, the entries stay identity-less -- a visible, classifiable state --
    and `test_every_split_producer_has_declared_session_keys` fails. That is
    the intended behaviour: a configuration defect must surface as a defect,
    not as silently reassigned identities.
    """
    codes = [str(c) for c in (split or [])]
    if not codes:
        return []
    if not isinstance(declared, (list, tuple)) or len(declared) != len(codes):
        LOGGER.error(
            "session_keys_missing_or_mismatched codes=%d declared=%s",
            len(codes),
            len(declared) if isinstance(declared, (list, tuple)) else "none",
        )
        return []

    keys = [str(k).strip() for k in declared]
    if not all(keys) or len(set(keys)) != len(keys):
        LOGGER.error("session_keys_invalid count=%d unique=%d", len(keys), len(set(keys)))
        return []
    return keys


def mint_session_occurrence(session_key: Any) -> str | None:
    """The stable identity of one session within a plan.

    Takes a DECLARED session key (`abc1_a`, `full_2`) -- see
    `session_keys_for_split` -- and not a code plus a traversal counter.

    The counter version of this function was wrong, and measured so. Two
    semantically distinct sessions sharing a code, minted from one split order
    and regenerated from the opposite order, SWAPPED identities:

        order [HEAVY_A, PULL_B, LIGHT_A] -> HEAVY_A=A#0  LIGHT_A=A#1
        order [LIGHT_A, PULL_B, HEAVY_A] -> HEAVY_A=A#1  LIGHT_A=A#0

    Every slot in both sessions was therefore reattributed to the other
    session's professional meaning. A test that reverses a list AFTER minting
    cannot see this: it proves an id travels with an object, not that a fresh
    build reproduces the same id for the same need.
    """
    key = str(session_key or "").strip()
    if not key:
        return None
    # `:` separates the two identity components; `-` is the callback token
    # separator. A key containing either would make the id ambiguous to split
    # or to decode, so it is refused at minting rather than silently mangled.
    if any(ch in key for ch in (_SLOT_ID_SEP, _TOKEN_SEP)):
        return None
    return key


def mint_slot_id(session_key: Any, slot_key: Any) -> str | None:
    """The stable id for one professional need, or None if unmintable.

    `<stable_session_key>:<slot_key>` -- e.g. `abc1_a:bench`, `abc2_a:bench`
    for the two A-sessions of a 6-day split.

    Neither component is positional, and neither is recomputed from traversal:

    * `session_key` is declared against the split definition, so reordering a
      split moves a session without renaming it;
    * `slot_key` is declared on the template by `exercise_plans.exercise(...)`,
      so reordering a session's exercises reassigns nothing.

    It is derived from none of: weekday, array position, the current exercise
    id, or an occurrence number counted while iterating.

    Returns None rather than inventing an id: a placeholder would collide with
    every other unmintable entry, which is worse than having no identity.
    """
    session = mint_session_occurrence(session_key)
    if session is None:
        return None
    key = str(slot_key or "").strip()
    if not key or any(ch in key for ch in (_SLOT_ID_SEP, _TOKEN_SEP)):
        return None
    return f"{session}{_SLOT_ID_SEP}{key}"


def is_valid_slot_id(value: Any) -> bool:
    """True when `value` is a well-formed slot id.

    Validation is a real check, not a truthiness test: an id that does not
    round-trip through `mint_slot_id` cannot be reconciled against a regenerated
    plan, so accepting it would reintroduce the identity loss silently.
    """
    if not isinstance(value, str) or not value:
        return False
    session, separator, key = value.partition(_SLOT_ID_SEP)
    if not separator:
        return False
    return mint_slot_id(session, key) == value


def slot_token_of(entry: Any) -> str | None:
    """A slot id encoded for use inside callback data.

    `:` is the callback field separator, so a raw slot id would silently add a
    field and shift every later one. The token substitutes `-` for it.

    That substitution is **unambiguously reversible** because `mint_slot_id`
    refuses a session key or slot key containing either `:` or `-`. Without
    that refusal the decode would be a guess: `a-b-c` could be `a:b-c` or
    `a-b:c`, and the wrong reading resolves to a different slot or to none. The
    constraint is enforced at minting rather than papered over at decode.

    The token also cannot match the version grammar: it always contains a `-`,
    so a slot token in the terminal callback field can never be misread as a
    flow version.
    """
    slot_id = slot_id_of(entry)
    if slot_id is None:
        return None
    return slot_id.replace(_SLOT_ID_SEP, _TOKEN_SEP)


def slot_id_from_token(token: Any) -> str | None:
    """Inverse of `slot_token_of`, rejecting anything that is not a slot id.

    Exactly one `-` may appear, because neither component may contain the
    character. A token with a different count is not a slot token and is
    rejected rather than partially decoded into a plausible wrong slot.
    """
    if not isinstance(token, str) or not token:
        return None
    # Defence in depth, and measured as such: `is_valid_slot_id` already
    # rejects every ambiguous token, because minting refuses `-` in either
    # component -- so `a-b-c` decodes to `a:b-c`, which fails validation.
    # Deliberate breakage confirmed removing this check changes no observable
    # behaviour today; it stays because it states the invariant at the point of
    # decode rather than relying on a property of a function two calls away.
    if token.count(_TOKEN_SEP) != 1:
        return None
    candidate = token.replace(_TOKEN_SEP, _SLOT_ID_SEP, 1)
    return candidate if is_valid_slot_id(candidate) else None


def assign_slot_ids(exercises: Any, session_occurrence: Any) -> int:
    """Stamp slot ids onto a freshly built session, in place. Returns the count.

    Called on the TEMPLATE copy, before adaptation, so `slot_key` is the one the
    template declared. Never overwrites an existing valid id -- a repaired or
    migrated payload keeps the identity it already had.

    An entry with no declared `slot_key` falls back to its seed exercise id,
    which is what `exercise_plans.exercise(...)` would have declared anyway. It
    is NOT given a positional key: that would make reordering rename slots,
    which is the failure this scheme exists to avoid.
    """
    if not isinstance(exercises, list):
        return 0
    minted = 0
    for entry in exercises:
        if not isinstance(entry, dict):
            continue
        if is_valid_slot_id(entry.get("slot_id")):
            continue
        slot_key = entry.get("slot_key") or entry.get("id")
        slot_id = mint_slot_id(session_occurrence, slot_key)
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
