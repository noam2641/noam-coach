"""Deterministic free-text parsing for the in-workout "what weight did you
lift?" step.

TASK-WORKOUT-WEIGHT-TEXT replaces the predefined weight-SELECTION buttons in
the active-workout set report with a typed answer. This module owns the
grammar for that one question and nothing else.

Design constraints that shape everything below:

* **No AI call.** The step is on the critical path of a live workout; a set
  report must never wait on (or be corrupted by) a model. Every accepted input
  is recognised by an explicit regex.
* **Never guess.** The parser returns ``None`` for anything it does not
  positively recognise. The caller then re-asks and writes nothing — an
  unparsed message must not advance the session or invent a load.
* **Narrow by construction.** This grammar only ever runs while an active
  workout is genuinely awaiting a weight (see ``FlowName.workout_session``).
  Even so it is written to be *unattractive* to unrelated text: it anchors on
  the whole message, so a meal ("אכלתי 200 גרם עוף") or an onboarding answer
  ("אני בן 30 ומתאמן 3 פעמים בשבוע") is rejected rather than mined for a
  number.

Load-type representation (no migration): ``sets.weight`` is a bare REAL with
no unit / per-hand / load-type column, so the parser returns the SAME canonical
number the removed buttons would have produced, plus a ``load_type`` hint the
caller stores in the schemaless ``sessions.plan`` JSON and echoes in the
confirmation copy. Per-hand reports store the per-hand number (that is what the
dumbbell buttons always stored); bodyweight stores 0.0.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

# Safe boundaries. The upper bound matches the existing weight-entry
# conventions elsewhere in the product (a plausible barbell load), and exists
# to catch typos like "8000" rather than to police strong users. The lower
# bound is 0 because bodyweight is a legitimate report.
MIN_WEIGHT_KG = 0.0
MAX_WEIGHT_KG = 500.0

# Existing convention: loads are stored rounded to 2 decimals (see
# callback_session `split` / `splitw` and meal_text._parse_workout_parameter_text).
WEIGHT_PRECISION = 2

LoadType = Literal["total", "per_hand", "bodyweight"]


@dataclass(frozen=True)
class WeightReport:
    """A positively recognised weight report.

    ``weight`` is the canonical number to persist into ``sets.weight`` — the
    exact value the removed selection buttons would have written.
    """

    weight: float
    load_type: LoadType = "total"
    #: True when the user asked to repeat the previous set's load.
    same_as_previous: bool = False

    @property
    def is_bodyweight(self) -> bool:
        return self.load_type == "bodyweight"

    @property
    def is_per_hand(self) -> bool:
        return self.load_type == "per_hand"


# --- Hebrew vocabulary ------------------------------------------------------
#
# Hebrew kilogram notation appears with several quote-like characters that
# users and keyboards produce interchangeably: the ASCII apostrophe, the ASCII
# double quote, the Hebrew punctuation GERESH (U+05F3) and GERSHAYIM (U+05F4),
# and the typographic curly quotes. They are all folded to nothing before
# matching, so ק״ג / ק"ג / קג / ק'ג all reduce to the same token.
_QUOTE_CHARS = "\"'׳״‘’“”′″`´"

_KG_WORDS = (
    "קילוגרם",
    "קילוגרמים",
    "קילו",
    "קג",
    "kilograms",
    "kilogram",
    "kilos",
    "kilo",
    "kgs",
    "kg",
)

# "in each hand" / "per hand" / "each side" phrasings.
_PER_HAND_PATTERNS = (
    r"בכל\s*יד",
    r"לכל\s*יד",
    r"בכל\s*צד",
    r"לכל\s*צד",
    r"בכל\s*אחת",
    r"ביד",
    r"per\s*hand",
    r"each\s*hand",
    r"each\s*side",
    r"per\s*side",
)

_BODYWEIGHT_PATTERNS = (
    r"משקל\s*גוף",
    r"משקל\s*הגוף",
    r"רק\s*הגוף",
    r"בלי\s*משקל",
    r"ללא\s*משקל",
    r"bodyweight",
    r"body\s*weight",
    r"^bw$",
)

_SAME_PATTERNS = (
    r"אותו\s*משקל",
    r"אותו\s*הדבר",
    r"כמו\s*קודם",
    r"כמו\s*בסט\s*הקודם",
    r"כרגיל",
    r"same",
    r"same\s*weight",
)

# Words that make a bare number mean something OTHER than a load. If any of
# these appear we refuse rather than guess — "12 חזרות" is a rep count, not a
# weight, and mis-reading it would silently corrupt the training history.
_NON_WEIGHT_MARKERS = (
    r"חזרות",
    r"חזרה",
    r"reps?\b",
    r"סטים",
    r"\bסט\b",
    r"\bsets?\b",
    r"דקות",
    r"דקה",
    r"שניות",
    r"\brir\b",
    r"מנוחה",
    r"גרם",
    r"קלוריות",
    r"\bkcal\b",
    r"אחוז",
)

_NUMBER = r"\d{1,3}(?:[.,]\d{1,2})?"


def _fold(text: str) -> str:
    """Normalize a raw Telegram message for matching.

    Folds unicode to NFKC, strips quote-like characters (so ק״ג == קג), maps
    all dash/whitespace runs to single spaces, and lowercases. The result is
    what every pattern in this module is written against.
    """
    folded = unicodedata.normalize("NFKC", text or "").strip().lower()
    folded = "".join(ch for ch in folded if ch not in _QUOTE_CHARS)
    folded = re.sub(r"[‎‏‪-‮]", "", folded)  # bidi marks
    folded = re.sub(r"\s+", " ", folded)
    return folded.strip()


def _to_number(raw: str) -> float | None:
    """Parse one numeric token, accepting BOTH decimal comma and point.

    "17,5" and "17.5" are the same number: Hebrew keyboards and European
    habits produce the comma form routinely, and the pre-existing workout
    parameter parser rejected it. A comma is only ever a decimal separator
    here — thousands separators cannot occur inside a plausible load.
    """
    candidate = raw.replace(",", ".")
    if candidate.count(".") > 1:
        return None
    try:
        return float(candidate)
    except ValueError:
        return None


def _validate(value: float) -> float | None:
    """Clamp to the safe boundaries, or reject."""
    if value != value or value in (float("inf"), float("-inf")):  # NaN / inf
        return None
    if value < MIN_WEIGHT_KG or value > MAX_WEIGHT_KG:
        return None
    return round(value, WEIGHT_PRECISION)


def _matches_any(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, text) for pattern in patterns)


def _strip_kg_units(text: str) -> str:
    """Remove a kilogram unit word so the numeric core is left.

    Matched LONGEST-FIRST: "קילוגרם" must be consumed whole, otherwise the
    shorter "קילו" would match and strand a bare "גרם", which the non-weight
    markers would then (correctly, but unhelpfully) read as a food quantity.
    """
    for word in sorted(_KG_WORDS, key=len, reverse=True):
        text = re.sub(rf"(?<=\d)\s*{word}(?![א-ת\w])", "", text)
        text = re.sub(rf"(?<![א-ת\w]){word}(?![א-ת\w])", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_weight_text(
    text: str,
    *,
    previous_weight: float | None = None,
    previous_load_type: LoadType = "total",
) -> WeightReport | None:
    """Parse a free-text weight report.

    Returns ``None`` when the message is not a confident weight report — the
    caller must then re-ask WITHOUT writing anything and WITHOUT advancing.

    ``previous_weight`` enables "אותו משקל"; when no previous value exists that
    phrasing is (correctly) unparseable and the user is asked for a number.

    Accepted, in precedence order:

    1. ``משקל גוף`` / bodyweight        -> 0.0, ``load_type="bodyweight"``
    2. ``אותו משקל`` (needs previous)   -> previous value + its load type
    3. ``<n> בכל יד``                   -> n, ``load_type="per_hand"``
    4. ``<n>`` / ``<n> קג`` / ``<n> ק״ג`` -> n, ``load_type="total"``

    Anything else -> ``None``.
    """
    folded = _fold(text)
    if not folded:
        return None

    # (1) Bodyweight is a positive statement and needs no number.
    if _matches_any(folded, _BODYWEIGHT_PATTERNS):
        return WeightReport(weight=0.0, load_type="bodyweight")

    # (2) "same weight" — only meaningful with a previous value to repeat.
    if _matches_any(folded, _SAME_PATTERNS):
        if previous_weight is None:
            return None
        validated = _validate(float(previous_weight))
        if validated is None:
            return None
        return WeightReport(
            weight=validated,
            load_type=previous_load_type,
            same_as_previous=True,
        )

    # (3) Per-hand. Detected BEFORE the bare-number rule so the per-hand
    #     phrase is not treated as trailing noise.
    per_hand = _matches_any(folded, _PER_HAND_PATTERNS)
    if per_hand:
        remainder = folded
        for pattern in _PER_HAND_PATTERNS:
            remainder = re.sub(pattern, " ", remainder)
        remainder = _strip_kg_units(remainder)
        remainder = re.sub(r"\s+", " ", remainder).strip()
        match = re.fullmatch(rf"({_NUMBER})", remainder)
        if not match:
            return None
        value = _to_number(match.group(1))
        if value is None:
            return None
        validated = _validate(value)
        if validated is None:
            return None
        return WeightReport(weight=validated, load_type="per_hand")

    # (4) Bare number, optionally with a kilogram unit.
    #
    # Units are stripped FIRST so a legitimate "קילוגרם" is consumed whole
    # before the competing-unit check runs — otherwise the "גרם" inside it
    # would be mistaken for a food quantity.
    core = _strip_kg_units(folded)

    # Anything still carrying a competing unit (reps, sets, minutes, grams…)
    # is deliberately NOT a weight. Refuse instead of mining a number out of it.
    if _matches_any(core, _NON_WEIGHT_MARKERS):
        return None

    # FULL-MATCH on what remains: this is what keeps the grammar from stealing
    # meal or onboarding text, which is always a sentence rather than a lone
    # number. "הרמתי 80" is intentionally accepted via the short optional
    # lead-in below, but "אכלתי 200 גרם עוף" is not (it has a competing unit
    # AND trailing words).
    core = re.sub(r"^(?:הרמתי|עשיתי|היה|זה|עם|על)\s+", "", core)
    core = re.sub(r"\s+(?:היום|עכשיו)$", "", core).strip()
    match = re.fullmatch(rf"({_NUMBER})", core)
    if not match:
        return None
    value = _to_number(match.group(1))
    if value is None:
        return None
    validated = _validate(value)
    if validated is None:
        return None
    return WeightReport(weight=validated, load_type="total")


def format_weight_confirmation(report: WeightReport) -> str:
    """Short confirmation that reflects what was ACTUALLY persisted."""
    if report.is_bodyweight:
        return "נרשם: משקל גוף ✅"
    if report.is_per_hand:
        return f"נרשם: {report.weight:g} ק״ג בכל יד ✅"
    return f"נרשם: {report.weight:g} ק״ג ✅"


def weight_prompt_text(
    previous_weight: float | None = None,
    previous_load_type: LoadType = "total",
) -> str:
    """The question that replaces the weight-selection buttons.

    The previous load is shown as CONTEXT only — it is never the required
    input mechanism, and the user can always just type a number.
    """
    lines = ["איזה משקל הרמת בסט הזה?"]
    if previous_weight is not None:
        if previous_load_type == "bodyweight":
            lines.append("בסט הקודם: משקל גוף.")
        elif previous_load_type == "per_hand":
            lines.append(f"בסט הקודם: {previous_weight:g} ק״ג בכל יד.")
        else:
            lines.append(f"בסט הקודם: {previous_weight:g} ק״ג.")
    lines.append("אפשר לכתוב למשל: 80, 17.5 ק״ג, 12 בכל יד, אותו משקל או משקל גוף.")
    return "\n".join(lines)


INVALID_WEIGHT_TEXT = (
    "לא הצלחתי לזהות את המשקל. "
    "אפשר לכתוב למשל: 70, 17.5 ק״ג, 12 בכל יד או משקל גוף."
)
