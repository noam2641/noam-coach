"""Deterministic meal correction, fingerprinting and duplicate detection."""

from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from models import MealAnalysis

try:
    from PIL import Image
except ImportError:  # pragma: no cover - fallback remains functional
    Image = None  # type: ignore[assignment]


@dataclass(frozen=True)
class LockedQuantity:
    food_text: str
    grams: float
    measurement_state: str | None = None  # cooked/raw/unknown


_QUANTITY_PATTERNS = (
    re.compile(
        r"(?P<grams>\d+(?:[.,]\d+)?)\s*(?:גרם|גר'|ג\b|g\b)\s*(?P<food>[^,.;\n]+)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?P<food>[^,.;\n]+?)\s*[-:]?\s*(?P<grams>\d+(?:[.,]\d+)?)\s*(?:גרם|גר'|ג\b|g\b)",
        re.IGNORECASE,
    ),
)


def parse_locked_quantities(text: str) -> list[LockedQuantity]:
    """Extract explicit user quantities. User numbers are hard constraints."""
    normalized = text.replace("־", "-")
    measurement_state: str | None = None
    if any(token in normalized for token in ("מבושל", "אחרי בישול", "מוכן")):
        measurement_state = "cooked"
    elif any(token in normalized for token in ("יבש", "לפני בישול", "נא")):
        measurement_state = "raw"

    found: list[LockedQuantity] = []
    seen: set[tuple[str, float]] = set()
    for pattern in _QUANTITY_PATTERNS:
        for match in pattern.finditer(normalized):
            grams = float(match.group("grams").replace(",", "."))
            food = match.group("food").strip(" -–—:()")
            food = re.sub(r"\s+", " ", food)
            food = re.sub(r"\b(?:היה|הייתה|היו)\b", " ", food).strip()
            if len(food) > 2 and food.startswith("ה"):
                food = food[1:]
            if not food or grams <= 0 or grams > 5000:
                continue
            key = (food.casefold(), grams)
            if key not in seen:
                found.append(LockedQuantity(food, grams, measurement_state))
                seen.add(key)
    return found


# ---------------------------------------------------------------------------
# Preparation-method correction parser  (REC-MEAL-01 / REC-MEAL-04)
# ---------------------------------------------------------------------------

PREPARATION_METHODS: dict[str, str] = {
    "raw": "נא",
    "boiled": "מבושל",
    "grilled": "צלוי",
    "baked": "אפוי",
    "fried": "מטוגן",
    "deep_fried": "מטוגן בשמן עמוק",
    "air_fried": "מטוגן באוויר",
    "steamed": "מאודה",
    "unknown": "לא ידוע",
}

PREPARATION_ALIASES: dict[str, str] = {
    # Hebrew
    "מטוגן": "fried",
    "טיגון": "fried",
    "בטיגון": "fried",
    "עשוי בטיגון": "fried",
    "מטוגנת": "fried",
    "מטוגנים": "fried",
    "צלוי": "grilled",
    "על הגריל": "grilled",
    "גריל": "grilled",
    "צלויה": "grilled",
    "צלויים": "grilled",
    "אפוי": "baked",
    "בתנור": "baked",
    "אפויה": "baked",
    "אפויים": "baked",
    "מבושל": "boiled",
    "בישול": "boiled",
    "מבושלת": "boiled",
    "מבושלים": "boiled",
    "מאודה": "steamed",
    "נא": "raw",
    "טרי": "raw",
    "חי": "raw",
    "מטוגן בשמן עמוק": "deep_fried",
    "בשמן עמוק": "deep_fried",
    "דיפ פריי": "deep_fried",
    "מטוגן באוויר": "air_fried",
    "אייר פרייר": "air_fried",
    "באוויר חם": "air_fried",
    # English
    "fried": "fried",
    "grilled": "grilled",
    "baked": "baked",
    "boiled": "boiled",
    "steamed": "steamed",
    "raw": "raw",
    "deep fried": "deep_fried",
    "deep-fried": "deep_fried",
    "air fried": "air_fried",
    "air-fried": "air_fried",
}

# Relative calorie multipliers when preparation changes.
# Base = boiled/steamed (no added oil).
# These are conservative estimates; the user can override.
PREPARATION_CALORIE_FACTORS: dict[str, float] = {
    "raw": 1.0,
    "boiled": 1.0,
    "steamed": 1.0,
    "grilled": 1.05,   # minimal oil
    "baked": 1.05,
    "fried": 1.25,      # pan-fried, conservative oil estimate
    "deep_fried": 1.40,
    "air_fried": 1.08,
    "unknown": 1.0,
}


@dataclass(frozen=True)
class MealCorrection:
    """A structured correction parsed from user text."""
    kind: str  # "preparation", "quantity", "remove", "add", "rename", "replace"
    item_hint: str  # food item the correction targets ("" = whole meal)
    value: str  # new value: preparation method key, grams, replacement name, etc.
    original_text: str  # the user's raw text for logging


# ---------------------------------------------------------------------------
# Item-removal patterns  (REC-PLAN-MEAL-03-12)
# ---------------------------------------------------------------------------

# Natural Hebrew removal language (Batch 2, audit Finding B): the negation
# words the parser always knew (בלי/ללא/הוצא/הסר) plus imperative/infinitive
# remove verbs (תוריד/תסיר/תוציא and their inflections).
_REMOVAL_VERBS = r"(?:בלי|ללא|הוצא|תוציא|להוציא|הסר|תסיר|להסיר|תוריד|הורד|להוריד)"

# Each tuple is (compiled_pattern, canonical_item_name).
# The canonical name is a Hebrew term used for similarity matching against
# the analysis items.  Use the most common Hebrew name for the ingredient.
_REMOVAL_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # "בלי שמן" / "ללא שמן" / "הוצא שמן" / "תוריד שמן" / "בלי שמן זית"
    (re.compile(_REMOVAL_VERBS + r"\s+שמן(?:\s+זית)?", re.IGNORECASE), "שמן"),
    # Generic "בלי <item>" / "תוריד <item>" for other items.
    # Captures the item after the keyword so it can be used as item_hint.
    (re.compile(_REMOVAL_VERBS + r"\s+([֐-׿][֐-׿\s]{1,30})", re.IGNORECASE), ""),
]

# Batch 2, audit Finding A: the generic capture above is greedy and used to
# swallow a trailing add clause, producing the destructive polluted hint
# "פלאפל והוסף שניצל" (which removed the falafel and added nothing). The
# captured hint is truncated at the first add/replace connector token.
_REMOVAL_HINT_STOP_TOKENS = frozenset({
    "ותוסיף", "והוסף", "ולהוסיף", "ותשים", "ושים", "ובמקום", "במקום",
    "ותחליף", "והחלף", "אלא", "וגם", "תוסיף", "הוסף", "להוסיף", "תשים", "שים",
})

# Leading filler tokens that may precede the actual food ("תוריד לי את הפלאפל").
_REMOVAL_HINT_FILLER_TOKENS = frozenset({"לי", "את", "בבקשה", "רק", "גם"})

# If the hint STARTS with one of these, the text is a quantity/scale/general
# instruction ("תוריד חצי", "תוריד קצת מהכמות"), not an item removal — the
# parser must fall through to the scale/quantity/AI paths untouched.
_REMOVAL_HINT_NON_ITEM_TOKENS = frozenset({
    "חצי", "רבע", "שליש", "קצת", "מעט", "עוד", "כמות", "הכמות", "מהכמות",
    "מנה", "קלוריות", "גרם", "זה", "זו", "זאת", "אותו", "אותה", "הכל", "הכול",
})


def _clean_removal_hint(raw_hint: str) -> str | None:
    """Reduce a captured removal hint to the removed item only.

    Truncates at the first add/replace connector, skips leading filler
    tokens, and refuses hints that describe amounts rather than items.
    Returns None when no safe item hint remains.
    """
    kept: list[str] = []
    for token in raw_hint.split():
        if token in _REMOVAL_HINT_STOP_TOKENS:
            break
        kept.append(token)
    while kept and kept[0] in _REMOVAL_HINT_FILLER_TOKENS:
        kept.pop(0)
    if not kept or kept[0] in _REMOVAL_HINT_NON_ITEM_TOKENS:
        return None
    return " ".join(kept)


def _parse_removal_corrections(text: str) -> list[MealCorrection]:
    """Extract explicit item-removal instructions from *text*.

    NOTE: remove-and-add phrasing ("תוריד פלאפל ותוסיף שניצל") is a
    REPLACEMENT, parsed by ``_parse_replacement_corrections`` which
    ``parse_meal_correction`` consults first. Even when this function is
    called directly on such text, the hint is cleaned so it can never
    carry a trailing add clause.
    """
    normalized = re.sub(r"\s+", " ", text.strip())
    found: list[MealCorrection] = []

    # First try the oil-specific pattern (highest priority).
    oil_pat, oil_item = _REMOVAL_PATTERNS[0]
    if oil_pat.search(normalized):
        found.append(MealCorrection(
            kind="remove",
            item_hint=oil_item,
            value="",
            original_text=text,
        ))
        return found  # oil removal found; don't also emit a generic removal

    # Generic pattern.
    gen_pat, _ = _REMOVAL_PATTERNS[1]
    m = gen_pat.search(normalized)
    if m:
        item_hint = _clean_removal_hint(m.group(1).strip())
        # Skip if nothing safe remains or the captured text looks like a
        # preparation method (those are handled by _PREP_ITEM_PATTERNS).
        if item_hint and item_hint not in PREPARATION_ALIASES:
            found.append(MealCorrection(
                kind="remove",
                item_hint=item_hint,
                value="",
                original_text=text,
            ))
    return found


# ---------------------------------------------------------------------------
# Item-replacement patterns  (REC-PROGRAM-04-12)
# ---------------------------------------------------------------------------
#
# Supported surface forms (Hebrew):
#   "X לא Y"       → replace Y with X   (e.g. "שניצל רגיל לא טופו")
#   "X ולא Y"      → replace Y with X
#   "X במקום Y"    → replace Y with X
#   "זה X לא Y"    → replace Y with X
#
# Each pattern must expose named groups:
#   replacement – the NEW item (what the user actually ate)
#   target      – the OLD item (what needs to be swapped out)
#
# NOTE: "אין X" is intentionally NOT handled here; it is a removal, already
# covered by _REMOVAL_PATTERNS above.

_HEB = r"[\u0590-\u05FF][\u0590-\u05FF\s'\u05F3]{0,40}"  # Hebrew words (incl. geresh: \u05E7\u05D5\u05D8\u05D2')

# Batch 2: verbs for explicit remove-and-add replacement phrasing. בלי/ללא
# are deliberately EXCLUDED — "בלי שמן ותוסיף מלח" stays a removal, not a
# rename that would inherit the removed item's grams.
_RA_REMOVE_VERBS = r"(?:תוריד|הורד|להוריד|הסר|תסיר|להסיר|תוציא|הוצא|להוציא)"
_RA_ADD_VERBS = r"(?:תוסיף|הוסף|להוסיף|תשים|שים)"

_REPLACEMENT_PATTERNS: list[re.Pattern[str]] = [
    # TASK-58: negation-FIRST identity corrections — the production incident
    # form "לא טחינה חציל במיונז" ("not tahini; eggplant with mayonnaise").
    # These must parse as ITEM_IDENTITY replacements (rejected → confirmed),
    # not fall through to whole-meal AI reinterpretation.
    # Comma/dash-separated: "לא Y, X" / "זה לא Y - X"
    re.compile(
        r"^(?:זה|זו|זאת)?\s*לא\s+(?P<target>" + _HEB + r"?)\s*[,;–—-]\s*"
        r"(?:זה|זו|זאת)?\s*(?P<replacement>" + _HEB + r")$",
        re.IGNORECASE,
    ),
    # "לא Y אלא X"
    re.compile(
        r"^(?:זה|זו|זאת)?\s*לא\s+(?P<target>" + _HEB + r"?)\s+אלא\s+"
        r"(?P<replacement>" + _HEB + r")$",
        re.IGNORECASE,
    ),
    # Space-separated with a single-word rejected identity: "לא טחינה חציל במיונז"
    re.compile(
        r"^(?:זה|זו|זאת)?\s*לא\s+(?P<target>[֐-׿]+)\s+"
        r"(?P<replacement>" + _HEB + r")$",
        re.IGNORECASE,
    ),
    # "זה/זו/זאת X לא Y" – "it's X not Y"
    re.compile(
        r"^(?:זה|זו|זאת)\s+(?P<replacement>" + _HEB + r")\s*,?\s+ולא\s+(?P<target>" + _HEB + r")$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:זה|זו|זאת)\s+(?P<replacement>" + _HEB + r")\s*,?\s+לא\s+(?P<target>" + _HEB + r")$",
        re.IGNORECASE,
    ),
    # "X במקום Y" – "X instead of Y"
    re.compile(
        r"^(?P<replacement>" + _HEB + r")\s+במקום\s+(?P<target>" + _HEB + r")$",
        re.IGNORECASE,
    ),
    # "X ולא Y" – "X and not Y"
    re.compile(
        r"^(?P<replacement>" + _HEB + r")\s+ולא\s+(?P<target>" + _HEB + r")$",
        re.IGNORECASE,
    ),
    # "X לא Y" – "X not Y"  (most general; must come last to avoid false matches)
    re.compile(
        r"^(?P<replacement>" + _HEB + r")\s+לא\s+(?P<target>" + _HEB + r")$",
        re.IGNORECASE,
    ),
    # ------------------------------------------------------------------
    # Batch 2 (audit Finding A) — explicit-verb replacement forms. These
    # normalize natural remove-and-add / swap phrasing into the SAME
    # canonical replace correction as the forms above (one representation,
    # one enforcement path — never a second remove+add mechanism). All are
    # fully anchored, so they can never match mid-sentence commentary, and
    # none of them can collide with the לא-based forms above.
    # ------------------------------------------------------------------
    # "תוריד X ותוסיף Y" / "הסר X, והוסף Y" / "תוציא X - ותשים Y"
    re.compile(
        r"^" + _RA_REMOVE_VERBS + r"\s+(?:את\s+)?"
        r"(?P<target>[֐-׿][֐-׿\s]{0,40}?)"
        r"[\s,;.:–—-]*ו?" + _RA_ADD_VERBS + r"\s+(?:את\s+)?"
        r"(?P<replacement>" + _HEB + r")$",
        re.IGNORECASE,
    ),
    # "תחליף X בY" / "החלף את X ב-Y"
    re.compile(
        r"^(?:תחליף|החלף|להחליף)\s+(?:את\s+)?"
        r"(?P<target>[֐-׿][֐-׿\s]{0,40}?)"
        r"\s+ב-?\s*(?P<replacement>" + _HEB + r")$",
        re.IGNORECASE,
    ),
    # "במקום X יש Y" / "במקום X זה Y"
    re.compile(
        r"^במקום\s+(?:את\s+)?"
        r"(?P<target>[֐-׿][֐-׿\s]{0,40}?)"
        r"\s+(?:יש|זה|זו|זאת|היה|הייתה|תשים|שים)\s+(?:את\s+)?"
        r"(?P<replacement>" + _HEB + r")$",
        re.IGNORECASE,
    ),
]


def _parse_replacement_corrections(text: str) -> list[MealCorrection]:
    """Extract item-replacement instructions from *text*.

    Returns a list with at most one :class:`MealCorrection` of ``kind="replace"``.
    The ``item_hint`` field holds the *target* (old item to swap out) and
    ``value`` holds the *replacement* (new item name).
    """
    # Batch 2: tolerate punctuation/whitespace variants — collapse runs of
    # whitespace and strip trailing sentence punctuation before matching.
    normalized = re.sub(r"\s+", " ", text.strip()).strip(" .,;:!?")
    for pattern in _REPLACEMENT_PATTERNS:
        m = pattern.match(normalized)
        if m is None:
            continue
        target = m.group("target").strip()
        replacement = m.group("replacement").strip()
        # Sanity: both sides must be non-empty and differ.
        if not target or not replacement or target == replacement:
            continue
        # Skip if either side matches a pure preparation alias (those are not
        # item names and should be handled by the preparation-correction path).
        if target in PREPARATION_ALIASES or replacement in PREPARATION_ALIASES:
            continue
        return [
            MealCorrection(
                kind="replace",
                item_hint=target,
                value=replacement,
                original_text=text,
            )
        ]
    return []


# Pattern: "ה<item> <method>" or "<item> <method>"
_SCALE_WHOLE_PATTERNS: list[tuple[re.Pattern[str], float]] = [
    (re.compile(r"(?:^|\s)(?:הכל|כולה|כל\s+הארוחה)\s+כפול(?:ה)?(?:\s|$)", re.IGNORECASE), 2.0),
    (re.compile(r"(?:^|\s)(?:פי\s*)?2x(?:\s|$)", re.IGNORECASE), 2.0),
    (re.compile(r"(?:^|\s)x2(?:\s|$)", re.IGNORECASE), 2.0),
    (re.compile(r"(?:^|\s)פי\s*2(?:\s|$)", re.IGNORECASE), 2.0),
    (re.compile(r"(?:^|\s)(?:פי\s*)?3x(?:\s|$)", re.IGNORECASE), 3.0),
    (re.compile(r"(?:^|\s)x3(?:\s|$)", re.IGNORECASE), 3.0),
    (re.compile(r"(?:^|\s)פי\s*3(?:\s|$)", re.IGNORECASE), 3.0),
    (re.compile(r"(?:^|\s)(?:חצי|חצי\s+מנה)(?:\s|$)", re.IGNORECASE), 0.5),
]

_SCALE_ITEM_PATTERNS: list[tuple[re.Pattern[str], float]] = [
    (re.compile(r"חצי\s+מ(?:ה|ן)(?P<item>[\u0590-\u05FF][\u0590-\u05FF\s]{1,30})", re.IGNORECASE), 0.5),
    (re.compile(r"(?P<item>[\u0590-\u05FF][\u0590-\u05FF\s]{1,30})\s+חצי", re.IGNORECASE), 0.5),
    (re.compile(r"(?P<item>[\u0590-\u05FF][\u0590-\u05FF\s]{1,30})\s+כפול(?:ה)?", re.IGNORECASE), 2.0),
]


def _parse_scale_corrections(text: str) -> list[MealCorrection]:
    """Extract relative portion corrections such as "חצי מהאורז" or "x3"."""
    normalized = text.strip()
    for pattern, factor in _SCALE_ITEM_PATTERNS:
        match = pattern.search(normalized)
        if match is None:
            continue
        item_hint = match.group("item").strip(" -:.,")
        if item_hint in {"הכל", "כולה", "כל הארוחה"}:
            break
        if item_hint:
            return [
                MealCorrection(
                    kind="scale",
                    item_hint=item_hint,
                    value=str(factor),
                    original_text=text,
                )
            ]
    for pattern, factor in _SCALE_WHOLE_PATTERNS:
        if pattern.search(normalized):
            return [
                MealCorrection(
                    kind="scale",
                    item_hint="",
                    value=str(factor),
                    original_text=text,
                )
            ]
    return []


_PREP_ITEM_PATTERNS = [
    re.compile(
        r"(?:ה)?(?P<item>[\u0590-\u05FF]+)\s+(?:עשוי\s+ב?|הוא\s+)?(?P<method>[\u0590-\u05FF\s]+)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:תתקן|תשנה|שנה)\s+(?:ל|ל-)?\s*(?P<method>[\u0590-\u05FF\s]+)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:לא\s+\S+[,،]\s*)?(?P<method>[\u0590-\u05FF]+)$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:זה|הוא|היא)\s+(?P<method>[\u0590-\u05FF]+)$",
        re.IGNORECASE,
    ),
]


def parse_meal_correction(text: str) -> list[MealCorrection]:
    """Parse user correction text into structured :class:`MealCorrection` objects.

    Priority order (Batch 2 — replacement language wins first so
    remove-and-add phrasing is one canonical replace, never a bare removal;
    audit Finding A):
    1. Item-replacement corrections ("לא X, Y", "X במקום Y",
       "תוריד X ותוסיף Y", "תחליף X בY", "במקום X יש Y").
    2. Item-removal corrections ("בלי שמן", "תוריד X", generic "בלי X").
    3. Relative scale corrections ("חצי מהאורז", "x2").
    4. Preparation-method corrections.
    5. Quantity corrections (delegate to parse_locked_quantities).
    """
    normalized = text.strip().lower()
    corrections: list[MealCorrection] = []

    # --- Item-replacement corrections (REC-PROGRAM-04-12 / Batch 2) ---
    # Checked FIRST: explicit replacement language (including remove-and-add
    # phrasing like "תוריד פלאפל ותוסיף שניצל") must win before the generic
    # removal parser can misread its removal half (audit Finding A), and
    # before preparation so "שניצל רגיל לא טופו" is a replace, not a
    # prep-method change. Replacement patterns are fully anchored, so plain
    # removal texts ("בלי שמן") can never match them.
    replacement_corrections = _parse_replacement_corrections(text)
    if replacement_corrections:
        corrections.extend(replacement_corrections)
        return corrections  # replacement is unambiguous; skip further parsing

    # --- Item-removal corrections (REC-PLAN-MEAL-03-12) ---
    # Checked before preparation so "בלי שמן" is an explicit removal, not
    # a prep change.
    removal_corrections = _parse_removal_corrections(text)
    if removal_corrections:
        corrections.extend(removal_corrections)
        return corrections  # removal is unambiguous; skip further parsing

    scale_corrections = _parse_scale_corrections(text)
    if scale_corrections:
        corrections.extend(scale_corrections)
        return corrections

    # --- Preparation corrections ---
    for pattern in _PREP_ITEM_PATTERNS:
        m = pattern.search(normalized)
        if m is None:
            continue
        method_text = m.group("method").strip()
        canonical = PREPARATION_ALIASES.get(method_text)
        if canonical is None:
            continue
        item_hint = m.groupdict().get("item", "")
        if item_hint:
            item_hint = item_hint.strip()
        corrections.append(
            MealCorrection(
                kind="preparation",
                item_hint=item_hint or "",
                value=canonical,
                original_text=text,
            )
        )
        break  # first successful match wins

    # --- Quantity corrections (delegate) ---
    for lq in parse_locked_quantities(text):
        corrections.append(
            MealCorrection(
                kind="quantity",
                item_hint=lq.food_text,
                value=str(lq.grams),
                original_text=text,
            )
        )

    return corrections


def _detect_existing_preparation(item_name: str) -> str | None:
    """Return the canonical preparation key found in *item_name*, if any."""
    for label, key in PREPARATION_ALIASES.items():
        if label in item_name:
            return key
    return None


def apply_preparation_correction(
    analysis: MealAnalysis,
    correction: MealCorrection,
) -> MealAnalysis:
    """Apply a preparation-method correction to *analysis* and return it.

    * Finds the target item via *correction.item_hint* similarity (or picks
      the first protein-heavy item when the hint is empty).
    * Adjusts calories and fat using :data:`PREPARATION_CALORIE_FACTORS`.
    * Updates the item name with the Hebrew preparation label.
    """
    if correction.kind != "preparation":
        return analysis

    new_method = correction.value
    if new_method not in PREPARATION_CALORIE_FACTORS:
        return analysis

    # --- Locate target item ---
    target = None
    if correction.item_hint:
        candidates = sorted(
            analysis.items,
            key=lambda item: _similarity(correction.item_hint, item.name),
            reverse=True,
        )
        if candidates and _similarity(correction.item_hint, candidates[0].name) >= 0.15:
            target = candidates[0]
    if target is None and analysis.items:
        # Default: first protein-heavy item, else first item
        protein_items = [i for i in analysis.items if i.protein > 0]
        target = protein_items[0] if protein_items else analysis.items[0]

    if target is None:
        return analysis

    # --- Determine old method & compute factor ---
    old_method = _detect_existing_preparation(target.name) or "unknown"
    old_factor = PREPARATION_CALORIE_FACTORS.get(old_method, 1.0)
    new_factor = PREPARATION_CALORIE_FACTORS[new_method]

    if old_factor > 0:
        ratio = new_factor / old_factor
    else:
        ratio = 1.0

    # Adjust calories and fat; protein and carbs stay mostly the same
    target.calories = round(target.calories * ratio, 1)
    target.fat = round(target.fat * ratio, 1)

    # --- Update item name with new preparation label ---
    hebrew_label = PREPARATION_METHODS.get(new_method, new_method)
    # Remove any existing preparation annotations "(מבושל)" etc.
    cleaned_name = re.sub(
        r"\s*\((?:" + "|".join(re.escape(v) for v in PREPARATION_METHODS.values()) + r")\)",
        "",
        target.name,
    )
    target.name = f"{cleaned_name} ({hebrew_label})"

    # --- Add explanatory note ---
    old_label = PREPARATION_METHODS.get(old_method, old_method)
    note = f"שיטת הכנה עודכנה: {old_label} → {hebrew_label} (מקדם קלורי ×{ratio:.2f})"
    if note not in analysis.notes:
        analysis.notes.append(note)

    return analysis


def apply_item_removal_correction(
    analysis: MealAnalysis,
    correction: MealCorrection,
) -> MealAnalysis:
    """Remove the item identified by *correction.item_hint* from *analysis*.

    Uses token-based similarity to find the best match.  If no item scores
    above the minimum threshold the analysis is returned unchanged so a
    partial match cannot silently delete the wrong item.

    This implements the deterministic path for REC-PLAN-MEAL-03-12: when the
    user says "בלי שמן" the oil line item is removed and the meal totals are
    recalculated from the remaining items.
    """
    if correction.kind != "remove":
        return analysis

    item_hint = correction.item_hint
    if not item_hint:
        return analysis

    # Find the closest matching item.
    candidates = sorted(
        analysis.items,
        key=lambda item: _similarity(item_hint, item.name),
        reverse=True,
    )
    if not candidates:
        return analysis

    best = candidates[0]
    score = _similarity(item_hint, best.name)

    # Minimum similarity threshold to avoid accidental removal.
    # For "שמן" we also accept items whose name contains the hint token.
    hint_tokens = _tokens(item_hint)
    name_contains_hint = any(
        t in _tokens(best.name) for t in hint_tokens
    )
    if score < 0.15 and not name_contains_hint:
        # No good match — leave the analysis unchanged.
        note = f"פריט לא נמצא להסרה: {item_hint}"
        if note not in analysis.notes:
            analysis.notes.append(note)
        return analysis

    removed_name = best.name
    analysis.items = [item for item in analysis.items if item is not best]

    note = f"הוסר: {removed_name}"
    if note not in analysis.notes:
        analysis.notes.append(note)

    # TASK-14: re-derive the canonical title after removing an item.
    analysis.reconcile_title()
    return analysis


def apply_item_replacement_correction(
    analysis: MealAnalysis,
    correction: MealCorrection,
) -> MealAnalysis:
    """Replace the item identified by *correction.item_hint* with *correction.value*.

    Implements REC-PROGRAM-04-12 (delta-based meal correction, replace type).

    Behaviour:
    * Uses token-based similarity (_similarity) to locate the target item.
    * Only the single best-matching item is renamed; all other items are left
      completely unchanged.
    * The original gram weight is preserved so portion size stays accurate.
    * When the replacement is a nutritionally different food (e.g. tofu →
      schnitzel), macros are adjusted conservatively using a lookup table of
      known food-category calorie densities rather than calling any AI service.
      If neither the old nor the new item appears in the lookup table the macros
      are kept as-is (i.e. the function is always deterministic).
    * Adds a note describing the replacement.
    * If no item matches above the minimum threshold the analysis is returned
      unchanged.
    """
    if correction.kind != "replace":
        return analysis

    item_hint = correction.item_hint
    replacement_name = correction.value
    if not item_hint or not replacement_name:
        return analysis

    # ------------------------------------------------------------------
    # Locate target item via similarity
    # ------------------------------------------------------------------
    candidates = sorted(
        analysis.items,
        key=lambda item: _similarity(item_hint, item.name),
        reverse=True,
    )
    if not candidates:
        return analysis

    best = candidates[0]
    score = _similarity(item_hint, best.name)

    # Also accept if any hint token appears verbatim in the item name.
    hint_tokens = _tokens(item_hint)
    name_contains_hint = any(t in _tokens(best.name) for t in hint_tokens)

    if score < 0.15 and not name_contains_hint:
        note = f"פריט לא נמצא להחלפה: {item_hint}"
        if note not in analysis.notes:
            analysis.notes.append(note)
        return analysis

    old_name = best.name

    # ------------------------------------------------------------------
    # Conservative macro adjustment for known food-category substitutions.
    #
    # Table maps a Hebrew keyword (checked via substring) to approximate
    # kcal per 100 g.  Both the old and new item names are checked; if
    # either is unknown we leave the macros unchanged to avoid inventing
    # nutritional data.
    # ------------------------------------------------------------------
    _KCAL_PER_100G: dict[str, float] = {
        "טופו": 76.0,
        "עוף": 165.0,
        "שניצל": 220.0,      # breaded, pan-fried estimate
        "בשר": 250.0,
        "סלמון": 208.0,
        "טונה": 132.0,
        "ביצה": 155.0,
        "גבינה": 350.0,
        "קוטג": 98.0,
        "יוגורט": 59.0,
        "חלב": 42.0,
        "אורז": 130.0,       # cooked
        "פסטה": 131.0,
        "לחם": 265.0,
        "תפוח אדמה": 77.0,
        "בטטה": 86.0,
        "ברוקולי": 34.0,
        "גזר": 41.0,
        "עגבנייה": 18.0,
        "מלפפון": 16.0,
    }

    def _lookup_kcal(name: str) -> float | None:
        for keyword, kcal in _KCAL_PER_100G.items():
            if keyword in name:
                return kcal
        return None

    # TASK-58: prefer the curated Israeli-foods catalog for the NEW identity —
    # when the confirmed food has trusted per-100g values, recalculate the
    # replaced item's macros from them at the preserved gram weight instead of
    # ratio-scaling the (possibly very wrong) rejected identity's macros.
    curated_applied = False
    try:
        import israeli_foods

        new_match = israeli_foods.lookup(replacement_name)
        if new_match is not None and best.grams > 0:
            scaled = israeli_foods.scaled_macros(new_match, best.grams)
            best.calories = scaled["calories"]
            best.protein = scaled["protein"]
            best.carbs = scaled["carbs"]
            best.fat = scaled["fat"]
            curated_applied = True
    except Exception:  # noqa: BLE001 — deterministic fallback below
        curated_applied = False

    old_kcal = _lookup_kcal(old_name)
    new_kcal = _lookup_kcal(replacement_name)

    if not curated_applied and old_kcal is not None and new_kcal is not None and old_kcal > 0:
        ratio = new_kcal / old_kcal
        best.calories = round(best.calories * ratio, 1)
        best.protein = round(best.protein * ratio, 1)
        best.carbs = round(best.carbs * ratio, 1)
        best.fat = round(best.fat * ratio, 1)

    # ------------------------------------------------------------------
    # Rename item (keep grams unchanged)
    # ------------------------------------------------------------------
    best.name = replacement_name

    note = f"הוחלף: {old_name} → {replacement_name}"
    if note not in analysis.notes:
        analysis.notes.append(note)

    # TASK-14: keep the canonical title consistent with the corrected items.
    analysis.reconcile_title()
    return analysis


def apply_scale_correction(
    analysis: MealAnalysis,
    correction: MealCorrection,
) -> MealAnalysis:
    """Apply a relative quantity correction to one item or the whole meal."""
    if correction.kind != "scale":
        return analysis
    try:
        factor = float(correction.value)
    except (TypeError, ValueError):
        return analysis
    if factor <= 0 or factor > 10:
        return analysis

    targets = analysis.items
    if correction.item_hint:
        candidates = sorted(
            analysis.items,
            key=lambda item: _similarity(correction.item_hint, item.name),
            reverse=True,
        )
        if not candidates:
            return analysis
        best = candidates[0]
        hint_tokens = _tokens(correction.item_hint)
        name_contains_hint = any(t in _tokens(best.name) for t in hint_tokens)
        if _similarity(correction.item_hint, best.name) < 0.15 and not name_contains_hint:
            note = f"פריט לא נמצא לשינוי כמות: {correction.item_hint}"
            if note not in analysis.notes:
                analysis.notes.append(note)
            return analysis
        targets = [best]

    for item in targets:
        item.grams = round(item.grams * factor, 1)
        item.calories = round(item.calories * factor, 1)
        item.protein = round(item.protein * factor, 1)
        item.carbs = round(item.carbs * factor, 1)
        item.fat = round(item.fat * factor, 1)
        item.confidence = max(float(item.confidence), 0.9)

    scope = correction.item_hint or "כל הארוחה"
    note = f"כמות עודכנה: {scope} x{factor:g}"
    if note not in analysis.notes:
        analysis.notes.append(note)
    return analysis


def _tokens(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[\w\u0590-\u05FF]+", text.casefold())
        if len(token) > 1 and token not in {"מבושל", "נא", "גרם"}
    }


def _similarity(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def apply_locked_quantities(
    analysis: MealAnalysis,
    constraints: Iterable[LockedQuantity],
) -> tuple[MealAnalysis, list[LockedQuantity]]:
    """Apply explicit quantities and return unmatched constraints."""
    unmatched: list[LockedQuantity] = []
    for constraint in constraints:
        candidates = sorted(
            analysis.items,
            key=lambda item: _similarity(constraint.food_text, item.name),
            reverse=True,
        )
        if not candidates or _similarity(constraint.food_text, candidates[0].name) < 0.2:
            unmatched.append(constraint)
            continue
        item = candidates[0]
        old_grams = float(item.grams)
        ratio = constraint.grams / old_grams if old_grams > 0 else 1.0
        item.grams = round(constraint.grams, 1)
        item.calories = round(item.calories * ratio, 1)
        item.protein = round(item.protein * ratio, 1)
        item.carbs = round(item.carbs * ratio, 1)
        item.fat = round(item.fat * ratio, 1)
        item.confidence = max(float(item.confidence), 0.95)
        if constraint.measurement_state == "cooked" and "מבושל" not in item.name:
            item.name = f"{item.name} (מבושל)"
        elif constraint.measurement_state == "raw" and "יבש" not in item.name and "נא" not in item.name:
            item.name = f"{item.name} (משקל לפני בישול)"
    return analysis, unmatched


def requires_cooked_raw_clarification(constraints: Iterable[LockedQuantity]) -> bool:
    sensitive = ("אורז", "פסטה", "פתיתים", "קוסקוס", "קטניות", "עדשים", "שעועית")
    return any(
        c.measurement_state is None and any(food in c.food_text for food in sensitive)
        for c in constraints
    )


# ---------------------------------------------------------------------------
# TASK-58: deterministic item-identity constraints
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IdentityConstraint:
    """A user-locked item identity: *rejected* must never return; when
    *confirmed* is set, the item the user was correcting IS that food."""

    rejected: str
    confirmed: str
    source_text: str


def identity_constraints_from_texts(texts: Iterable[str]) -> list[IdentityConstraint]:
    """Parse identity corrections out of the current + locked correction texts.

    This is the deterministic mirror of the prompt rule "the user's text is
    authoritative": every replacement-form correction ("לא טחינה חציל במיונז",
    "זה עוף לא הודו", "X במקום Y") becomes a hard constraint that outlives
    the current AI call — TASK-58's invariant is that a rejected identity
    cannot reappear in ANY later reanalysis of the same meal lifecycle.
    """
    constraints: list[IdentityConstraint] = []
    seen: set[tuple[str, str]] = set()
    for text in texts:
        for correction in _parse_replacement_corrections(str(text or "")):
            key = (correction.item_hint.casefold(), correction.value.casefold())
            if key in seen:
                continue
            seen.add(key)
            constraints.append(
                IdentityConstraint(
                    rejected=correction.item_hint,
                    confirmed=correction.value,
                    source_text=correction.original_text,
                )
            )
    return constraints


def _matches_rejected_identity(item_name: str, rejected: str) -> bool:
    """True when *item_name* is the rejected identity or a canonical alias of
    it (e.g. rejected "טחינה" must also catch "טחינה גולמית" restored by the
    curated-food override)."""
    name_tokens = _tokens(item_name)
    rejected_tokens = _tokens(rejected)
    if not name_tokens or not rejected_tokens:
        return False
    if rejected_tokens <= name_tokens:
        return True
    if _similarity(rejected, item_name) >= 0.5:
        return True
    # Canonical-family check: both names resolve to the same curated food.
    try:
        import israeli_foods

        rejected_match = israeli_foods.lookup(rejected)
        item_match = israeli_foods.lookup(item_name)
        if (
            rejected_match is not None
            and item_match is not None
            and rejected_match.canonical_name == item_match.canonical_name
        ):
            return True
    except Exception:  # noqa: BLE001 — enforcement must never crash analysis
        pass
    return False


def enforce_identity_constraints(
    analysis: MealAnalysis, constraints: Iterable[IdentityConstraint]
) -> tuple[MealAnalysis, list[dict[str, str]]]:
    """Deterministically enforce identity constraints AFTER AI parsing and
    AFTER deterministic food normalization (TASK-58 section A).

    For every item still carrying a rejected identity: rename it to the
    confirmed identity (item-scoped — grams preserved, macros adjusted via
    the same conservative mechanism as apply_item_replacement_correction),
    or drop it when the confirmed food already exists as another item.
    Returns the analysis plus a machine-readable list of enforcement actions
    for tracing. Unrelated items are never touched.
    """
    enforced: list[dict[str, str]] = []
    for constraint in constraints:
        matching = [
            item for item in analysis.items
            if _matches_rejected_identity(item.name, constraint.rejected)
        ]
        if not matching:
            continue
        confirmed_exists = any(
            _similarity(constraint.confirmed, item.name) >= 0.6
            for item in analysis.items
            if item not in matching
        )
        for item in matching:
            old_name = item.name
            if confirmed_exists:
                analysis.items.remove(item)
                action = "removed_rejected_duplicate"
            else:
                analysis = apply_item_replacement_correction(
                    analysis,
                    MealCorrection(
                        kind="replace",
                        item_hint=old_name,
                        value=constraint.confirmed,
                        original_text=constraint.source_text,
                    ),
                )
                action = "renamed_to_confirmed"
                confirmed_exists = True
            enforced.append(
                {
                    "action": action,
                    "rejected": constraint.rejected,
                    "confirmed": constraint.confirmed,
                    "item_was": old_name,
                }
            )
        note = f"אכיפת זהות: {constraint.rejected} → {constraint.confirmed}"
        if enforced and note not in analysis.notes:
            analysis.notes.append(note)
    if enforced:
        analysis.reconcile_title()
    return analysis, enforced


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def perceptual_hash(data: bytes) -> str:
    """Return a compact dHash; SHA fallback if Pillow is unavailable."""
    if Image is None:
        return sha256_bytes(data)[:16]
    try:
        with Image.open(io.BytesIO(data)) as image:
            image = image.convert("L").resize((9, 8))
            pixels = list(image.getdata())
        bits = []
        for row in range(8):
            start = row * 9
            for col in range(8):
                bits.append(pixels[start + col] > pixels[start + col + 1])
        value = 0
        for bit in bits:
            value = (value << 1) | int(bit)
        return f"{value:016x}"
    except Exception:
        return sha256_bytes(data)[:16]


def hamming_distance_hex(a: str, b: str) -> int:
    try:
        return (int(a, 16) ^ int(b, 16)).bit_count()
    except (ValueError, TypeError):
        return 999


async def register_meal_fingerprint(
    db: Any,
    *,
    user_id: int,
    meal_id: int,
    image_path: str | None,
    telegram_file_unique_id: str | None = None,
) -> None:
    raw = b""
    if image_path:
        path = Path(image_path)
        if path.exists() and path.is_file():
            raw = path.read_bytes()
    sha = sha256_bytes(raw) if raw else ""
    phash = perceptual_hash(raw) if raw else ""
    fingerprint = telegram_file_unique_id or phash or sha
    await db.execute(
        """
        INSERT INTO meal_fingerprints(
            meal_id, user_id, telegram_file_unique_id, sha256, perceptual_hash,
            fingerprint, created_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(meal_id) DO UPDATE SET
            telegram_file_unique_id=excluded.telegram_file_unique_id,
            sha256=excluded.sha256,
            perceptual_hash=excluded.perceptual_hash,
            fingerprint=excluded.fingerprint
        """,
        (
            meal_id,
            user_id,
            telegram_file_unique_id,
            sha,
            phash,
            fingerprint,
            datetime.now(timezone.utc).isoformat(),
        ),
    )


async def find_image_duplicate(
    db: Any,
    *,
    user_id: int,
    image_bytes: bytes,
    telegram_file_unique_id: str | None = None,
    window_hours: int = 6,
    max_phash_distance: int = 8,
) -> dict[str, Any] | None:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()
    sha = sha256_bytes(image_bytes)
    phash = perceptual_hash(image_bytes)
    rows = await db.fetch_all(
        """
        SELECT f.*, m.name, m.calories, m.created_at AS meal_created_at
        FROM meal_fingerprints f JOIN meals m ON m.id=f.meal_id
        WHERE f.user_id=? AND f.created_at>=?
        ORDER BY f.created_at DESC
        """,
        (user_id, cutoff),
    )
    for row in rows:
        if telegram_file_unique_id and row.get("telegram_file_unique_id") == telegram_file_unique_id:
            return row
        if row.get("sha256") == sha:
            return row
        existing_phash = row.get("perceptual_hash") or ""
        if existing_phash and hamming_distance_hex(existing_phash, phash) <= max_phash_distance:
            return row
    return None
