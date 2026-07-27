"""Dietary restrictions module for the coach bot.

Handles parsing, normalizing, merging, and enforcing dietary restrictions
and food allergies. Supports Hebrew and English food aliases.

This module is pure Python (stdlib + dataclasses only) with no async,
no Telegram imports, and no external dependencies.

Canonical IDs
-------------
tree_nuts   – almonds, walnuts, cashews, pistachios, etc.
peanuts     – peanuts (legume, distinct from tree nuts medically)
dairy       – milk, cheese, yogurt, butter, cream
gluten      – wheat, barley, rye, spelt, etc.
eggs        – chicken eggs and egg products
soy         – soy, tofu, edamame, miso
fish        – all finfish (salmon, tuna, etc.)

Groups
------
nuts        – covers both tree_nuts and peanuts
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

# ---------------------------------------------------------------------------
# Safe-compound allowlist: multi-word phrases that must NOT trigger a
# restriction even though a sub-token would match.  Maps a lowercased
# phrase to the set of canonical IDs it should suppress.
#
# Example: "אגוז מוסקט" (nutmeg) contains "אגוז" which maps to tree_nuts,
# but nutmeg is a seed, not a tree nut.  The allowlist suppresses tree_nuts
# for any item containing "אגוז מוסקט".
# ---------------------------------------------------------------------------

SAFE_COMPOUNDS: dict[str, set[str]] = {
    # Nutmeg (seed, not a tree nut)
    "אגוז מוסקט": {"tree_nuts"},
    "nutmeg": {"tree_nuts"},
    # Coconut (drupe, not a tree nut; coconut milk is not dairy)
    "חלב קוקוס": {"dairy"},
    "coconut milk": {"dairy"},
    "coconut cream": {"dairy"},
    "שמנת קוקוס": {"dairy"},
    # Soy/oat/almond milk — suppress dairy but NOT tree_nuts for almond
    "חלב סויה": {"dairy"},
    "soy milk": {"dairy"},
    "חלב שיבולת שועל": {"dairy"},
    "oat milk": {"dairy"},
    # Butternut squash is not dairy/butter
    "butternut": {"dairy"},
    "דלעת חמאה": {"dairy"},
}


# ---------------------------------------------------------------------------
# Alias table: Hebrew + English surface forms → canonical_id
# ---------------------------------------------------------------------------

#: Maps every known surface form (lowercased) to a canonical restriction id.
RESTRICTION_ALIASES: dict[str, str] = {
    # tree nuts – generic
    "אגוזים": "tree_nuts",
    "אגוז": "tree_nuts",
    "nuts": "tree_nuts",
    "nut": "tree_nuts",
    "tree nuts": "tree_nuts",
    "tree_nuts": "tree_nuts",

    # tree nuts – specific subtypes (still map to tree_nuts)
    "קשיו": "tree_nuts",
    "cashew": "tree_nuts",
    "cashews": "tree_nuts",
    "אגוזי מלך": "tree_nuts",
    "walnuts": "tree_nuts",
    "walnut": "tree_nuts",
    "שקדים": "tree_nuts",
    "almonds": "tree_nuts",
    "almond": "tree_nuts",
    "פיסטוק": "tree_nuts",
    "pistachio": "tree_nuts",
    "pistachios": "tree_nuts",
    "פקאן": "tree_nuts",
    "pecan": "tree_nuts",
    "pecans": "tree_nuts",
    "ברזיל": "tree_nuts",       # אגוזי ברזיל
    "brazil nuts": "tree_nuts",
    "brazil nut": "tree_nuts",
    "מקדמיה": "tree_nuts",
    "macadamia": "tree_nuts",
    "hazelnuts": "tree_nuts",
    "hazelnut": "tree_nuts",
    "אגוזי לוז": "tree_nuts",

    # peanuts – separate canonical category
    "בוטנים": "peanuts",
    "בוטן": "peanuts",
    "peanuts": "peanuts",
    "peanut": "peanuts",
    "groundnuts": "peanuts",
    "groundnut": "peanuts",

    # dairy
    "חלב": "dairy",
    "dairy": "dairy",
    "milk": "dairy",
    "גבינה": "dairy",
    "cheese": "dairy",
    "יוגורט": "dairy",
    "yogurt": "dairy",
    "yoghurt": "dairy",
    "חמאה": "dairy",
    "butter": "dairy",
    "שמנת": "dairy",
    "cream": "dairy",
    "לקטוז": "dairy",
    "lactose": "dairy",

    # gluten
    "גלוטן": "gluten",
    "gluten": "gluten",
    "חיטה": "gluten",
    "wheat": "gluten",
    "שעורה": "gluten",
    "barley": "gluten",
    "שיפון": "gluten",
    "rye": "gluten",
    "כוסמין": "gluten",
    "spelt": "gluten",

    # eggs
    "ביצים": "eggs",
    "ביצה": "eggs",
    "eggs": "eggs",
    "egg": "eggs",

    # soy
    "סויה": "soy",
    "soy": "soy",
    "soya": "soy",
    "tofu": "soy",
    "טופו": "soy",
    "edamame": "soy",
    "אדממה": "soy",
    "miso": "soy",
    "מיסו": "soy",

    # fish
    "דגים": "fish",
    "דג": "fish",
    "fish": "fish",
    "salmon": "fish",
    "סלמון": "fish",
    "tuna": "fish",
    "טונה": "fish",
    "cod": "fish",
    "tilapia": "fish",
    "trout": "fish",
    "halibut": "fish",
    "anchovy": "fish",
    "anchovies": "fish",
}

# ---------------------------------------------------------------------------
# Group table: group name → set of canonical_ids it covers
# ---------------------------------------------------------------------------

#: Maps a high-level group name to all canonical ids it encompasses.
RESTRICTION_GROUPS: dict[str, set[str]] = {
    "nuts": {"tree_nuts", "peanuts"},
    "tree_nuts": {"tree_nuts"},
    "peanuts": {"peanuts"},
    "dairy": {"dairy"},
    "gluten": {"gluten"},
    "eggs": {"eggs"},
    "soy": {"soy"},
    "fish": {"fish"},
}

# ---------------------------------------------------------------------------
# Valid enum values
# ---------------------------------------------------------------------------

RESTRICTION_TYPES = frozenset({
    "preference",
    "unavailable",
    "avoidance",
    "intolerance",
    "sensitivity",
    "allergy",
    "unknown",
})

SEVERITY_LEVELS = frozenset({"low", "medium", "high", "critical"})

# Default severity per restriction type
_TYPE_DEFAULT_SEVERITY: dict[str, str] = {
    "allergy": "critical",
    "intolerance": "high",
    "sensitivity": "medium",
    "avoidance": "medium",
    "preference": "low",
    "unavailable": "low",
    "unknown": "low",
}


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------

@dataclass
class DietaryRestriction:
    """Represents a single dietary restriction or food allergy for a user.

    Attributes
    ----------
    canonical_id:
        Normalized restriction identifier (e.g. "tree_nuts", "dairy").
    user_label:
        The Hebrew (or other) text the user originally provided
        (e.g. "אגוזים").
    original_input:
        The raw, unparsed input string from the user.
    restriction_type:
        One of "preference", "unavailable", "avoidance", "intolerance",
        "sensitivity", "allergy", "unknown".
    severity:
        One of "low", "medium", "high", "critical".
    confirmed:
        Whether the restriction has been explicitly confirmed by the user
        (as opposed to inferred).
    source:
        Free-form string identifying where the restriction came from
        (e.g. "onboarding", "conversation", "profile_update").
    created_at:
        ISO-8601 UTC timestamp of first creation.
    updated_at:
        ISO-8601 UTC timestamp of last update.
    revision:
        Integer counter incremented on each merge update.
    """

    canonical_id: str
    user_label: str
    original_input: str
    restriction_type: str = "unknown"
    severity: str = "low"
    confirmed: bool = False
    source: str = ""
    created_at: str = field(default_factory=lambda: _utcnow())
    updated_at: str = field(default_factory=lambda: _utcnow())
    revision: int = 0

    def __post_init__(self) -> None:
        if self.restriction_type not in RESTRICTION_TYPES:
            raise ValueError(
                f"Invalid restriction_type {self.restriction_type!r}. "
                f"Must be one of: {sorted(RESTRICTION_TYPES)}"
            )
        if self.severity not in SEVERITY_LEVELS:
            raise ValueError(
                f"Invalid severity {self.severity!r}. "
                f"Must be one of: {sorted(SEVERITY_LEVELS)}"
            )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _utcnow() -> str:
    """Return current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _tokenize(text: str) -> list[str]:
    """Split *text* into lower-cased tokens on whitespace and punctuation.

    Keeps multi-word Hebrew constructs intact where possible by also
    returning the full lowercased text as one candidate.
    """
    lowered = text.strip().lower()
    # Produce individual words as well as the whole phrase
    tokens = re.split(r"[\s,،؛;/\\|]+", lowered)
    tokens = [t for t in tokens if t]
    return tokens


def _candidates_from_text(text: str) -> list[str]:
    """Return all sub-phrases and tokens to check against RESTRICTION_ALIASES.

    For a text like "אגוזי מלך" we want to check both "אגוזי מלך" (multi-word)
    and the individual words "אגוזי", "מלך".  This allows the alias table to
    handle both full phrases and single words.
    """
    lowered = text.strip().lower()
    # Always include the full string
    results = [lowered]
    tokens = _tokenize(lowered)
    results.extend(tokens)
    # Also add all consecutive bigrams (covers "tree nuts", "אגוזי מלך", etc.)
    for i in range(len(tokens) - 1):
        results.append(f"{tokens[i]} {tokens[i + 1]}")
    return results


def _canonical_ids_in_text(text: str) -> set[str]:
    """Return the set of canonical IDs found anywhere in *text*.

    Respects SAFE_COMPOUNDS: if a known safe phrase (e.g. "אגוז מוסקט")
    appears in the text, the canonical IDs it suppresses are removed from
    the result.  This prevents nutmeg from triggering tree_nuts, coconut
    milk from triggering dairy, etc.
    """
    found: set[str] = set()
    for candidate in _candidates_from_text(text):
        cid = RESTRICTION_ALIASES.get(candidate)
        if cid:
            found.add(cid)

    # Check if any safe compound phrase appears in the full text and
    # suppress the corresponding canonical IDs.
    lowered = text.strip().lower()
    for phrase, suppressed_ids in SAFE_COMPOUNDS.items():
        if phrase in lowered:
            found -= suppressed_ids

    found -= _negated_canonical_ids(lowered)
    return found


#: Every canonical slug the alias table can produce. Derived rather than
#: hardcoded so it cannot drift as aliases are added.
_CANONICAL_IDS: frozenset[str] = frozenset(RESTRICTION_ALIASES.values())

#: Hebrew single-letter particles that attach directly to a noun
#: ("בטורטייה", "והחציל"). Stripped so a restriction matches its own name
#: when it appears inflected in a meal description.
_HEBREW_PREFIXES = ("ו", "ה", "ב", "ל", "כ", "מ", "ש")

#: Plural/feminine endings worth trimming for a stem comparison, longest
#: first so "יות" is consumed before "ות". Kept short on purpose --
#: aggressive stemming would collapse distinct foods.
_HEBREW_SUFFIXES = ("יות", "ות", "ים", "יה", "ה")


def _hebrew_stem(word: str) -> str:
    """A conservative stem for literal restriction matching.

    Strips at most one leading particle and one ending, and only when a
    usable stem remains. This is deliberately weaker than real morphological
    analysis: over-stemming would make unrelated foods collide, and a false
    block on food is worse than a miss the AI layer still sees.

    The feminine singular/plural pair is the case that matters here --
    "טורטייה" and "טורטיות" must reduce to the same stem, or a restriction
    stated in the singular misses every plural mention of the same food.
    """
    stem = word.strip().lower()
    if len(stem) > 3 and stem[0] in _HEBREW_PREFIXES:
        stem = stem[1:]
    for suffix in _HEBREW_SUFFIXES:
        if len(stem) - len(suffix) >= 3 and stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    # "טורטיי" -> "טורטי": a trailing yod left by the feminine ending is
    # not part of the stem.
    if len(stem) > 3 and stem.endswith("י"):
        stem = stem[:-1]
    return stem


def _literal_restrictions_in_text(
    text: str, blocks: dict[str, "DietaryRestriction"]
) -> set[str]:
    """Match restrictions whose canonical_id is raw user text, not an alias.

    Only consults ids that are NOT known canonical slugs -- an unmapped
    restriction keeps the user's own wording as its id. Matching is on word
    stems rather than substrings, so "אגוז" does not match "אגוזי מוסקט"
    handling that SAFE_COMPOUNDS already governs, and a short id cannot
    match inside an unrelated longer word.
    """
    lowered = (text or "").strip().lower()
    if not lowered:
        return set()

    item_stems = {_hebrew_stem(token) for token in _tokenize(lowered)}
    item_stems.discard("")

    found: set[str] = set()
    for cid in blocks:
        # Recognised slugs are handled by the alias table; only raw text
        # needs this path.
        if cid in _CANONICAL_IDS:
            continue
        for part in _tokenize(cid.lower()):
            stem = _hebrew_stem(part)
            if stem and stem in item_stems:
                found.add(cid)
                break
    return found


def _negated_canonical_ids(lowered_text: str) -> set[str]:
    """Return restriction IDs explicitly stated as absent in an item text.

    This is intentionally conservative: it suppresses only tight phrases such
    as "contains no nuts", "nut-free", "ללא אגוזים", and "בלי חלב". It does not
    suppress uncertain warnings such as "may contain nuts".
    """
    negated: set[str] = set()
    aliases = sorted(RESTRICTION_ALIASES, key=len, reverse=True)
    for alias in aliases:
        canonical_id = RESTRICTION_ALIASES[alias]
        escaped = re.escape(alias)
        english_patterns = [
            rf"\bcontains\s+no\s+{escaped}\b",
            rf"\bwith\s+no\s+{escaped}\b",
            rf"\bwithout\s+{escaped}\b",
            rf"\bno\s+{escaped}\b",
            rf"\b{escaped}\s*-\s*free\b",
            rf"\bfree\s+from\s+{escaped}\b",
        ]
        hebrew_patterns = [
            rf"(?:ללא|בלי|אין|לא\s+מכיל)\s+{escaped}",
        ]
        if any(re.search(pattern, lowered_text, re.IGNORECASE) for pattern in english_patterns):
            negated.add(canonical_id)
        elif any(re.search(pattern, lowered_text) for pattern in hebrew_patterns):
            negated.add(canonical_id)
    return negated


def _group_expands_to(canonical_id: str) -> set[str]:
    """Return all canonical IDs that belong to the same group as *canonical_id*.

    For example, "tree_nuts" is in the "nuts" group, so this returns
    {"tree_nuts", "peanuts"} – but only if the restriction itself is for
    the whole group.  When called with a specific canonical_id we return
    just that id plus any group that *contains* it as a member.
    """
    # Direct membership lookup
    return RESTRICTION_GROUPS.get(canonical_id, {canonical_id})


def _restricted_canonical_ids(restrictions: list[DietaryRestriction]) -> set[str]:
    """Expand a list of restrictions into the full set of canonical IDs blocked.

    Handles group membership: if a user restricts "nuts" (canonical_id maps
    to either "tree_nuts" or "peanuts" individually), both will appear here.
    We also check RESTRICTION_GROUPS to expand group-level restrictions.
    """
    blocked: set[str] = set()
    for r in restrictions:
        # Add the canonical id itself
        blocked.add(r.canonical_id)
        # Expand via groups (e.g. "tree_nuts" group covers {"tree_nuts"})
        blocked.update(RESTRICTION_GROUPS.get(r.canonical_id, set()))
    return blocked


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def normalize_restriction(text: str) -> str:
    """Map a user-provided restriction string to its canonical ID.

    The lookup is case-insensitive and strips surrounding whitespace.
    Multi-word phrases (e.g. "אגוזי מלך", "tree nuts") are tried first;
    if no match is found the first recognised token is used.

    Parameters
    ----------
    text:
        Any Hebrew or English food restriction string.

    Returns
    -------
    str
        A canonical_id string (e.g. "tree_nuts") or the original lowercased
        and stripped text if no alias is found.

    Examples
    --------
    >>> normalize_restriction("אגוזי מלך")
    'tree_nuts'
    >>> normalize_restriction("Cashews")
    'tree_nuts'
    >>> normalize_restriction("dairy")
    'dairy'
    >>> normalize_restriction("unknown_food")
    'unknown_food'
    """
    lowered = text.strip().lower()
    # Try exact match first (handles multi-word aliases like "אגוזי מלך")
    if lowered in RESTRICTION_ALIASES:
        return RESTRICTION_ALIASES[lowered]
    # Try each token
    for candidate in _candidates_from_text(lowered):
        if candidate in RESTRICTION_ALIASES:
            return RESTRICTION_ALIASES[candidate]
    # Fall back to the cleaned input
    return lowered


def parse_restrictions(
    text: str,
    restriction_type: str = "unknown",
    severity: Optional[str] = None,
    source: str = "",
    confirmed: bool = False,
) -> list[DietaryRestriction]:
    """Parse a comma- or and-separated restriction string into DietaryRestriction objects.

    The text is split on commas, Hebrew conjunctions (ו, ועל), the word
    "and", and semicolons.  Each segment is normalised and wrapped in a
    DietaryRestriction.  Segments that look like sensitivity/preference
    qualifiers ("רגישות", "sensitivity", "prefer") without a food noun are
    skipped.

    Parameters
    ----------
    text:
        Free-form restriction string, e.g. "אגוזים, גלוטן וחלב".
    restriction_type:
        Type to assign to all parsed restrictions (default "unknown").
    severity:
        Severity to assign; if None, defaults based on restriction_type.
    source:
        Source tag for created objects.
    confirmed:
        Whether to mark restrictions as confirmed.

    Returns
    -------
    list[DietaryRestriction]
        One object per recognised restriction token.  Empty list if nothing
        is recognised.

    Examples
    --------
    >>> rs = parse_restrictions("אגוזים, גלוטן")
    >>> [r.canonical_id for r in rs]
    ['tree_nuts', 'gluten']
    """
    if not text or not text.strip():
        return []

    # Split on: comma, semicolon, "and", Hebrew "ו" conjunction prefix, "ועל"
    # We split on these patterns while preserving meaningful substrings.
    parts = re.split(r"[,;،؛]|\band\b|\bועל\b|(?<!\w)ו(?=\w)", text, flags=re.IGNORECASE)

    # Qualifiers that alone do not identify a food
    _skip_patterns = re.compile(
        r"^(רגישות|אלרגיה|sensitivity|intolerance|allergy|preference|"
        r"avoid|avoidance|prefer|אין לי|אני לא אוכל|לא אוכל|without)$",
        re.IGNORECASE,
    )

    effective_severity = severity or _TYPE_DEFAULT_SEVERITY.get(restriction_type, "low")

    results: list[DietaryRestriction] = []
    seen_canonical: set[str] = set()

    for part in parts:
        label = part.strip()
        if not label:
            continue
        if _skip_patterns.match(label.lower()):
            continue

        canonical = normalize_restriction(label)
        if not canonical:
            continue
        # Deduplicate within this parse call
        if canonical in seen_canonical:
            continue
        seen_canonical.add(canonical)

        results.append(
            DietaryRestriction(
                canonical_id=canonical,
                user_label=label,
                original_input=text,
                restriction_type=restriction_type,
                severity=effective_severity,
                confirmed=confirmed,
                source=source,
            )
        )

    return results


def merge_restrictions(
    existing: list[DietaryRestriction],
    new: list[DietaryRestriction],
) -> list[DietaryRestriction]:
    """Merge *new* restrictions into *existing*, deduplicating by canonical_id.

    If a canonical_id already exists in *existing*, the entry is updated
    with data from *new* (restriction_type, severity, confirmed, source,
    updated_at) and its revision counter is incremented.  Entirely new
    canonical_ids are appended.

    The operation is idempotent: merging the same list twice yields the
    same result (except revision increments on the first merge only).

    Parameters
    ----------
    existing:
        Current list of DietaryRestriction objects (may be empty).
    new:
        Incoming restrictions to merge in.

    Returns
    -------
    list[DietaryRestriction]
        A new list containing the merged result.  The original lists are
        not mutated.
    """
    # Build a mutable index keyed by canonical_id
    index: dict[str, DietaryRestriction] = {r.canonical_id: r for r in existing}

    for incoming in new:
        cid = incoming.canonical_id
        if cid in index:
            current = index[cid]
            # Only update if something meaningful changed
            changed = (
                current.restriction_type != incoming.restriction_type
                or current.severity != incoming.severity
                or current.confirmed != incoming.confirmed
                or current.source != incoming.source
            )
            if changed:
                index[cid] = DietaryRestriction(
                    canonical_id=cid,
                    user_label=incoming.user_label or current.user_label,
                    original_input=incoming.original_input or current.original_input,
                    restriction_type=incoming.restriction_type,
                    severity=incoming.severity,
                    confirmed=incoming.confirmed,
                    source=incoming.source or current.source,
                    created_at=current.created_at,
                    updated_at=_utcnow(),
                    revision=current.revision + 1,
                )
        else:
            index[cid] = incoming

    return list(index.values())


def validate_meal_restrictions(
    items: list[dict],
    restrictions: list[DietaryRestriction],
) -> list[dict]:
    """Check meal items against active dietary restrictions.

    This is the CRITICAL firewall function.  It examines every item in
    *items* and returns a violation report for any item whose name (or
    description) contains a word or alias that maps to a restricted
    canonical category.

    The check is alias-aware: if "nuts" (tree_nuts) is restricted,
    an item containing "אגוזי מלך" or "קשיו" will be caught.

    The check also expands group membership: restricting "tree_nuts"
    catches items that contain "walnuts", "cashews", "almonds", etc.

    Parameters
    ----------
    items:
        List of meal-item dicts.  Each dict must have an "item_name" key;
        an optional "description" key is also searched.  Example::

            [{"item_name": "יוגורט יווני עם אגוזים ודבש"}]

    restrictions:
        Active DietaryRestriction objects for the user.

    Returns
    -------
    list[dict]
        Zero or more violation dicts, each with keys:

        * ``item_name`` – the item name string
        * ``restriction`` – the matching DietaryRestriction object
        * ``action`` – one of ``"block"``, ``"warn"``, ``"substitute"``

    Action mapping
    --------------
    * allergy → "block"
    * sensitivity, intolerance → "block"
    * avoidance → "warn"
    * preference → "warn"
    * unavailable → "substitute"
    * unknown → "warn"

    Examples
    --------
    >>> nuts = DietaryRestriction(
    ...     canonical_id="tree_nuts", user_label="אגוזים",
    ...     original_input="אגוזים", restriction_type="allergy",
    ...     severity="critical", confirmed=True, source="test",
    ... )
    >>> items = [{"item_name": "יוגורט יווני עם אגוזים ודבש"}]
    >>> violations = validate_meal_restrictions(items, [nuts])
    >>> violations[0]["action"]
    'block'
    """
    _ACTION_MAP: dict[str, str] = {
        "allergy": "block",
        "sensitivity": "block",
        "intolerance": "block",
        "avoidance": "warn",
        "preference": "warn",
        "unavailable": "substitute",
        "unknown": "warn",
    }

    if not restrictions:
        return []

    # Build a lookup: canonical_id → DietaryRestriction
    restriction_index: dict[str, DietaryRestriction] = {
        r.canonical_id: r for r in restrictions
    }

    # Expand groups: also index every canonical_id that belongs to a group
    # whose group-head is restricted.
    # e.g. if "tree_nuts" is restricted, we also want to match items that
    # mention "peanuts" if the user restricted "nuts" (which maps to both).
    # We need the reverse: given a canonical_id appearing in an item,
    # find any restriction that covers it.
    #
    # Strategy: pre-compute a map: canonical_id_in_item → DietaryRestriction
    effective_blocks: dict[str, DietaryRestriction] = {}
    for cid, restr in restriction_index.items():
        # The restriction itself
        effective_blocks[cid] = restr
        # Any group members it covers
        for member in RESTRICTION_GROUPS.get(cid, set()):
            if member not in effective_blocks:
                effective_blocks[member] = restr
            # Prefer higher-severity if already present
            elif _severity_rank(restr.severity) > _severity_rank(
                effective_blocks[member].severity
            ):
                effective_blocks[member] = restr

    violations: list[dict] = []

    for item in items:
        item_name: str = item.get("item_name", "") or item.get("name", "")
        description: str = item.get("description", "")
        search_text = f"{item_name} {description}".strip()

        # Find all canonical IDs present in the item text
        found_in_item = _canonical_ids_in_text(search_text)

        # A restriction the alias table does not recognise keeps its raw user
        # text as its canonical_id, and that literal is not an alias -- so it
        # could never match its own name and was silently unenforceable.
        #
        # Measured against the live profile ("חציל, טורטייה ואגוזים"): a
        # tortilla wrap and a baked aubergine both returned ZERO violations,
        # while nuts was caught only because it happens to be in the table.
        # The alias table has 85 entries; anything outside it was stored,
        # displayed, counted and sent to the AI, with the deterministic
        # firewall blind to it.
        found_in_item |= _literal_restrictions_in_text(search_text, effective_blocks)

        # Check each found canonical ID against effective blocks
        reported_restrictions: set[str] = set()
        for cid in found_in_item:
            if cid in effective_blocks:
                restr = effective_blocks[cid]
                # Avoid duplicate violations for the same restriction
                if restr.canonical_id in reported_restrictions:
                    continue
                reported_restrictions.add(restr.canonical_id)

                action = _ACTION_MAP.get(restr.restriction_type, "warn")
                violations.append(
                    {
                        "item_name": item_name,
                        "restriction": restr,
                        "action": action,
                    }
                )

    return violations


def _severity_rank(severity: str) -> int:
    """Return a numeric rank for severity comparison (higher = more severe)."""
    return {"low": 0, "medium": 1, "high": 2, "critical": 3}.get(severity, 0)


def load_restrictions_from_facts(
    diet_restrictions_value: Optional[str],
    allergies_value: Optional[str],
) -> list[DietaryRestriction]:
    """Convert legacy fact values to DietaryRestriction objects.

    The coach bot stores restrictions as comma-separated strings in two
    fact keys: ``diet_restrictions`` (avoidances) and ``allergies``
    (allergies).  This function bridges the old format to the new typed
    model.

    Parameters
    ----------
    diet_restrictions_value:
        Comma-separated avoidance string, e.g. "גלוטן,חלב".
        May be None or empty.
    allergies_value:
        Comma-separated allergy string, e.g. "אגוזים,בוטנים".
        May be None or empty.

    Returns
    -------
    list[DietaryRestriction]
        Merged list of restrictions.  Diet restrictions are type="avoidance",
        allergies are type="allergy".
    """
    result: list[DietaryRestriction] = []

    if diet_restrictions_value and diet_restrictions_value.strip():
        avoidances = parse_restrictions(
            diet_restrictions_value,
            restriction_type="avoidance",
            severity="medium",
            source="facts:diet_restrictions",
            confirmed=True,
        )
        result = merge_restrictions(result, avoidances)

    if allergies_value and allergies_value.strip():
        allergies = parse_restrictions(
            allergies_value,
            restriction_type="allergy",
            severity="critical",
            source="facts:allergies",
            confirmed=True,
        )
        result = merge_restrictions(result, allergies)

    return result


def restrictions_to_fact_value(
    restrictions: list[DietaryRestriction],
    restriction_type_filter: Optional[str] = None,
) -> str:
    """Serialise restrictions back to a comma-separated string for fact storage.

    Parameters
    ----------
    restrictions:
        List of DietaryRestriction objects to serialise.
    restriction_type_filter:
        If provided, only restrictions whose ``restriction_type`` equals
        this value are included.  Pass ``"allergy"`` to rebuild the
        ``allergies`` fact, or ``"avoidance"`` for ``diet_restrictions``.

    Returns
    -------
    str
        Comma-separated string of ``user_label`` values (falling back to
        ``canonical_id`` when ``user_label`` is empty).

    Examples
    --------
    >>> r = DietaryRestriction(
    ...     canonical_id="tree_nuts", user_label="אגוזים",
    ...     original_input="אגוזים", restriction_type="allergy",
    ...     severity="critical", confirmed=True, source="",
    ... )
    >>> restrictions_to_fact_value([r], restriction_type_filter="allergy")
    'אגוזים'
    """
    filtered = (
        restrictions
        if restriction_type_filter is None
        else [r for r in restrictions if r.restriction_type == restriction_type_filter]
    )
    labels = [r.user_label or r.canonical_id for r in filtered]
    return ",".join(labels)
