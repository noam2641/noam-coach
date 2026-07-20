"""Batch 6 — quantity clarification.

When count/portion evidence is insufficient or implausible, ASK instead of
guessing. Guessing is precisely the root cause the audit traced: a count that
cannot be safely converted must never become a silent gram estimate.

This module is the pure decision core. It answers two questions and owns no
I/O:

- ``detect_quantity_clarification`` — does this analysis have an item whose
  quantity evidence we must not resolve on our own, and what should we ask?
- ``apply_quantity_clarification`` — the user answered; write the resulting
  quantity onto the item deterministically.

Design constraints honoured here (each was verified against the code before
being relied upon):

1. ``MealAnalysis.question`` is a SINGLE slot that is cleared on answer, so
   two decline reasons cannot be asked at once. ``detect_*`` therefore ranks
   candidates and returns exactly one, and the pending record keeps enough
   state that the next unresolved item can be asked afterwards.
2. A user-chosen size must survive re-materialization. ``materialize_count_
   quantity`` re-derives grams whenever the source is weaker than
   ``_STRONGER_THAN_COUNT``, so an answered clarification writes
   ``quantity_source="user"`` — a member of that frozenset — and the choice is
   never silently overwritten on reload/replay.
3. Resolution is idempotent. The pending record carries a ``token`` derived
   from the question's identity; once ``resolved_token`` matches, re-applying
   the same answer is a no-op rather than a second quantity mutation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Reason codes — stable, logged, and used to pick the question text.
REASON_NO_PORTION_MODEL = "no_portion_model"
REASON_IMPLAUSIBLE = "implausible_result"
REASON_AMBIGUOUS_TARGET = "ambiguous_target"
REASON_COUNT_GRAMS_CONFLICT = "count_grams_conflict"

# Ranked worst-first: when several items are unresolved we ask about the one
# whose silence is most damaging. A conflict/implausible number is actively
# wrong on the card; a missing portion model is merely unconverted.
_REASON_PRIORITY = (
    REASON_COUNT_GRAMS_CONFLICT,
    REASON_IMPLAUSIBLE,
    REASON_AMBIGUOUS_TARGET,
    REASON_NO_PORTION_MODEL,
)

# Apply kinds carried on a ClarificationOption.
APPLY_SIZE = "size"      # estimate by small/medium/large units
APPLY_ASK_GRAMS = "grams_prompt"  # switch to typing grams
APPLY_CANCEL = "cancel"  # keep the draft exactly as it was

# Grams-per-unit fallbacks are NOT invented here: sizing reuses Batch 5's
# portion model so a clarification can never introduce a weight the
# deterministic converter would refuse.
_SIZE_LABELS = {
    "small": "קטן",
    "medium": "בינוני",
    "large": "גדול",
}


@dataclass
class PendingClarification:
    """Everything needed to reconstruct an asked-but-unanswered question.

    Serialized verbatim into the approval payload (free-form JSON, no
    migration), so a reload — or a replay from the payload alone — restores
    both the question and the item it belongs to.
    """

    token: str
    reason: str
    item_index: int
    item_name: str
    count: float
    unit_label: str = ""
    question: str = ""
    # Set once answered, so a repeated callback is detectably a repeat.
    resolved_token: str = ""
    resolution: str = ""
    resolved_grams: float | None = None
    # The pre-clarification quantity, so cancel restores rather than guesses.
    prior_grams: float | None = None
    prior_source: str = ""
    options: list[dict[str, Any]] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "reason": self.reason,
            "item_index": self.item_index,
            "item_name": self.item_name,
            "count": self.count,
            "unit_label": self.unit_label,
            "question": self.question,
            "resolved_token": self.resolved_token,
            "resolution": self.resolution,
            "resolved_grams": self.resolved_grams,
            "prior_grams": self.prior_grams,
            "prior_source": self.prior_source,
            "options": list(self.options),
        }

    @classmethod
    def from_payload(cls, raw: Any) -> "PendingClarification | None":
        if not isinstance(raw, dict) or not raw.get("token"):
            return None
        try:
            return cls(
                token=str(raw.get("token") or ""),
                reason=str(raw.get("reason") or ""),
                item_index=int(raw.get("item_index", -1)),
                item_name=str(raw.get("item_name") or ""),
                count=float(raw.get("count") or 0),
                unit_label=str(raw.get("unit_label") or ""),
                question=str(raw.get("question") or ""),
                resolved_token=str(raw.get("resolved_token") or ""),
                resolution=str(raw.get("resolution") or ""),
                resolved_grams=(
                    float(raw["resolved_grams"])
                    if raw.get("resolved_grams") is not None
                    else None
                ),
                prior_grams=(
                    float(raw["prior_grams"])
                    if raw.get("prior_grams") is not None
                    else None
                ),
                prior_source=str(raw.get("prior_source") or ""),
                options=list(raw.get("options") or []),
            )
        except (TypeError, ValueError):
            # A malformed payload must never break rendering — treat it as
            # "no pending clarification" and let the card show normally.
            return None

    @property
    def is_resolved(self) -> bool:
        return bool(self.resolved_token) and self.resolved_token == self.token


def clarification_token(item_index: int, item_name: str, count: float, reason: str) -> str:
    """Stable identity of one question.

    Derived from WHAT is being asked, not when — so the same unanswered
    question regenerated on reload keeps its token (and its resolved state),
    while a genuinely different count/item/reason produces a new one and is
    asked again.
    """
    return f"{reason}:{item_index}:{str(item_name or '').strip()}:{float(count or 0):g}"


def _needs_clarification(item: Any) -> str:
    """The reason this item's quantity cannot be resolved safely, or ""."""
    from meal_intelligence import (
        _MAX_REASONABLE_COUNT,
        _STRONGER_THAN_COUNT,
        QSOURCE_COUNT_DERIVED,
        _per_unit_weight,
    )

    count = getattr(item, "quantity_count", None)
    if not count or float(count) <= 0:
        return ""
    count = float(count)
    source = str(getattr(item, "quantity_source", "") or "")

    # The user already pinned grams — there is nothing to ask. Mirrors
    # materialize_count_quantity:1515, which declines for the same reason.
    if source in _STRONGER_THAN_COUNT:
        return ""

    # Already converted cleanly by Batch 5 — the card shows real grams.
    if source == QSOURCE_COUNT_DERIVED:
        return ""

    grams = float(getattr(item, "grams", 0) or 0)

    # The incident signature: a multi-unit count sitting next to grams that
    # equal (or nearly equal) the count itself — "3 שניצלים, 3 גרם".
    if count > 1 and grams > 0 and abs(grams - count) < 1.0:
        return REASON_COUNT_GRAMS_CONFLICT

    # An absurd count is not a portion we should price.
    if count > _MAX_REASONABLE_COUNT:
        return REASON_IMPLAUSIBLE

    # No supported portion model — the most common decline, and until now
    # entirely silent (materialize_count_quantity:1525).
    if _per_unit_weight(item) is None:
        return REASON_NO_PORTION_MODEL

    # A portion model exists but conversion still declined and left the item
    # on raw count evidence: the result failed the plausibility trial
    # (materialize_count_quantity:1556).
    return REASON_IMPLAUSIBLE


def _unit_phrase(count: float, item_name: str, unit_label: str) -> str:
    unit = str(unit_label or "").strip()
    if unit:
        return f"{count:g} {unit} {item_name}".strip()
    return f"{count:g} {item_name}".strip()


def build_question(reason: str, count: float, item_name: str, unit_label: str = "") -> str:
    """The short, low-friction question for this decline reason."""
    phrase = _unit_phrase(count, item_name, unit_label)
    if reason == REASON_COUNT_GRAMS_CONFLICT:
        return (
            f"כתבת {phrase}, אבל המשקל שרשום נראה כמו מספר היחידות ולא כמו גרמים. "
            "בערך כמה גרם הכל יחד — או שאעריך לפי יחידות בינוניות?"
        )
    if reason == REASON_IMPLAUSIBLE:
        return (
            f"כתבת {phrase}, אבל ההערכה שיצאה לא נראית סבירה. "
            "בערך כמה גרם הכל יחד — או שאעריך לפי יחידות בינוניות?"
        )
    if reason == REASON_AMBIGUOUS_TARGET:
        return f"כתבת {count:g}{(' ' + unit_label) if unit_label else ''} — לאיזה פריט זה שייך?"
    return (
        f"כתבת {phrase}. כדי לדייק, בערך כמה גרם הכל יחד — "
        "או שתרצה שאעריך לפי יחידות בינוניות?"
    )


def _size_options(item: Any, count: float) -> list[dict[str, Any]]:
    """Size buttons, priced from Batch 5's portion model.

    Returns [] when the food has no portion model — we must not offer an
    estimate we have no basis for. Those items get the grams-only path.
    """
    from meal_intelligence import _MAX_DERIVED_GRAMS, _SIZE_MULTIPLIER, _per_unit_weight

    weights = _per_unit_weight(item)
    if weights is None:
        return []
    _lo, default, _hi = weights
    options: list[dict[str, Any]] = []
    for size in ("small", "medium", "large"):
        grams = round(default * _SIZE_MULTIPLIER.get(size, 1.0) * count, 1)
        if grams <= 0 or grams > _MAX_DERIVED_GRAMS:
            continue
        options.append(
            {
                "label": f"{_SIZE_LABELS[size]} (~{grams:g} ג׳)",
                "apply_kind": APPLY_SIZE,
                "set_size": size,
                "set_grams": grams,
                "item_index": None,  # filled by the caller
            }
        )
    return options


def detect_quantity_clarification(analysis: Any) -> PendingClarification | None:
    """The one quantity question this analysis needs, or None.

    Ranked by reason priority, then by item order, so the choice is
    deterministic across replays of the same analysis.
    """
    items = list(getattr(analysis, "items", None) or [])
    candidates: list[tuple[int, int, Any, str]] = []
    for index, item in enumerate(items):
        reason = _needs_clarification(item)
        if not reason:
            continue
        candidates.append((_REASON_PRIORITY.index(reason), index, item, reason))
    if not candidates:
        return None

    _rank, item_index, item, reason = min(candidates, key=lambda c: (c[0], c[1]))
    count = float(getattr(item, "quantity_count", 0) or 0)
    item_name = str(getattr(item, "name", "") or "")
    unit_label = str(getattr(item, "quantity_unit", "") or "")

    options = _size_options(item, count)
    for option in options:
        option["item_index"] = item_index

    return PendingClarification(
        token=clarification_token(item_index, item_name, count, reason),
        reason=reason,
        item_index=item_index,
        item_name=item_name,
        count=count,
        unit_label=unit_label,
        question=build_question(reason, count, item_name, unit_label),
        prior_grams=float(getattr(item, "grams", 0) or 0),
        prior_source=str(getattr(item, "quantity_source", "") or ""),
        options=options,
    )


def apply_quantity_clarification(
    analysis: Any,
    pending: PendingClarification,
    option: dict[str, Any],
) -> bool:
    """Write the user's answer onto the item. True when the item changed.

    Idempotent: once ``pending`` is resolved, re-applying the SAME answer is a
    no-op. The caller persists ``pending`` afterwards, so the stored payload
    always reflects both the question and its resolution.
    """
    if pending.is_resolved:
        return False

    apply_kind = str(option.get("apply_kind") or "")

    if apply_kind == APPLY_CANCEL:
        # Cancel resolves the question without touching the draft — the
        # previous quantity evidence stays exactly as it was.
        pending.resolved_token = pending.token
        pending.resolution = APPLY_CANCEL
        return False

    if apply_kind == APPLY_ASK_GRAMS:
        # The user will type grams; the existing gram-locking path applies
        # them. The question stays pending until the text arrives, so a lost
        # message cannot strand the card in an unanswerable state.
        pending.resolution = APPLY_ASK_GRAMS
        return False

    items = list(getattr(analysis, "items", None) or [])
    if not (0 <= pending.item_index < len(items)):
        return False
    item = items[pending.item_index]

    # The item must still be the one we asked about. A correction that
    # reordered or replaced items between question and answer invalidates the
    # answer rather than applying it to an unrelated food.
    if str(getattr(item, "name", "") or "") != pending.item_name:
        return False

    grams = option.get("set_grams")
    if grams is None:
        return False
    grams = float(grams)
    if grams <= 0:
        return False

    return _write_user_grams(item, grams, pending, resolution=apply_kind)


def _write_user_grams(
    item: Any,
    grams: float,
    pending: PendingClarification,
    resolution: str,
) -> bool:
    """Set grams + coherent macros with ``quantity_source="user"``.

    ``"user"`` is in ``_STRONGER_THAN_COUNT``, so a later re-materialization
    pass declines rather than overwriting what the user just chose.
    """
    from meal_intelligence import _recompute_macros_for_grams

    old_grams = float(getattr(item, "grams", 0) or 0)
    new_macros = _recompute_macros_for_grams(item, grams, old_grams)
    if new_macros is None:
        return False
    try:
        item.grams = grams
        item.calories = new_macros["calories"]
        item.protein = new_macros["protein"]
        item.carbs = new_macros["carbs"]
        item.fat = new_macros["fat"]
        item.quantity_source = "user"
    except Exception:  # noqa: BLE001 — decline on any schema rejection
        return False

    pending.resolved_token = pending.token
    pending.resolution = resolution
    pending.resolved_grams = grams
    return True


def resolve_typed_grams(analysis: Any, pending: PendingClarification, grams: float) -> bool:
    """Resolve a pending clarification from grams the user typed.

    Called from the correction-text path so a typed answer closes the same
    question a button would have closed — and closes it exactly once.
    """
    if pending.is_resolved or grams <= 0:
        return False
    items = list(getattr(analysis, "items", None) or [])
    if not (0 <= pending.item_index < len(items)):
        return False
    item = items[pending.item_index]
    if str(getattr(item, "name", "") or "") != pending.item_name:
        return False
    return _write_user_grams(item, grams, pending, resolution="typed_grams")


def build_clarification_options(pending: PendingClarification) -> list[dict[str, Any]]:
    """Option dicts for the card: sizes, type-grams, cancel.

    The user is never trapped — approve/reject remain on the card, and cancel
    returns to the untouched draft.
    """
    options = list(pending.options)
    options.append(
        {
            "label": "✍️ אקליד גרמים",
            "apply_kind": APPLY_ASK_GRAMS,
            "item_index": pending.item_index,
        }
    )
    options.append(
        {
            "label": "↩️ בטל והשאר כמו שהיה",
            "apply_kind": APPLY_CANCEL,
            "item_index": pending.item_index,
        }
    )
    return options
