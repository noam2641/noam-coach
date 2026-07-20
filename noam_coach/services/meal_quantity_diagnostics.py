"""Batch 6.1 — authoritative diagnostics for count→grams conversion.

``materialize_count_quantity`` answers *whether* it converted (a bool). This
module answers *why*, without changing that contract and without emitting
anything: it is a pure, side-effect-free classifier.

Why it exists: the historical incident shape (``quantity_count=3``,
``quantity_unit="כדור"``, ``grams=3``) was declined in total silence. A bare
``False`` cannot tell an auditor whether the food had no portion model, the
count was absurd, the user's own grams outranked the count, or the derived
result failed the plausibility trial. Those are four different product
problems with four different fixes.

Its first consumer is the clarification layer: Batch 6 decided what to ASK
using a separate approximation of these same rules, which disagreed with the
converter in both directions. Routing both through this one classifier makes
"what we ask" and "what we convert" the same decision by construction.

Design rules:

1. The converter keeps returning ``bool`` — nothing here changes it.
2. This classifier NEVER mutates the item and NEVER emits an event. The
   low-level conversion function does not depend on observability at all;
   callers that want evidence ask this module.
3. Branch precedence is the converter's, in the converter's order. Every
   predicate is IMPORTED from ``meal_intelligence`` rather than restated, so
   a threshold change there cannot silently desynchronize this file. The only
   thing mirrored is the ORDER of the checks, which
   ``tests/test_batch6_1_conversion_parity.py`` pins against the converter's
   real bool for every branch.

The classifier must be given the item as it was BEFORE conversion, because a
successful conversion rewrites ``grams``/``quantity_source`` in place — see
``classify_conversion`` for the pre/post contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Diagnostic outcomes. Each maps to exactly one branch of
# materialize_count_quantity; none is invented.
CONVERTED = "converted"                                   # returned True
MISSING_OR_INVALID_COUNT = "missing_or_invalid_count"     # :1508
STRONGER_SOURCE_PRESERVED = "stronger_source_preserved"   # :1514
IMPLAUSIBLE_COUNT = "implausible_count"                   # :1520
NO_PORTION_MODEL = "no_portion_model"                     # :1524
BLOCKED_BY_CEILING = "blocked_by_ceiling"                 # :1537
IDEMPOTENT_NOOP = "idempotent_noop"                       # :1543
MACROS_INCOHERENT = "macros_incoherent"                   # :1549
IMPLAUSIBLE_RESULT = "implausible_result"                 # :1556

#: Outcomes that mean "the item legitimately kept its existing grams", as
#: opposed to a conversion that failed. Stronger user evidence and a replayed
#: no-op are NOT failures and must never be reported as one — nor is an item
#: that carried no count at all, where there was nothing to convert.
NON_FAILURE_OUTCOMES = frozenset(
    {
        CONVERTED,
        STRONGER_SOURCE_PRESERVED,
        IDEMPOTENT_NOOP,
        MISSING_OR_INVALID_COUNT,
    }
)

#: Outcomes where a portion model exists but the result was refused. These are
#: the "we could have priced it but the number was wrong" family.
REFUSED_RESULT_OUTCOMES = frozenset(
    {BLOCKED_BY_CEILING, MACROS_INCOHERENT, IMPLAUSIBLE_RESULT}
)


@dataclass(frozen=True)
class ConversionDiagnostic:
    """Why the converter did what it did, plus the numbers it used."""

    outcome: str
    count: float | None = None
    unit: str = ""
    grams_before: float | None = None
    grams_after: float | None = None
    source_before: str = ""
    source_after: str = ""
    #: The gram value the converter would have produced, when it got far
    #: enough to compute one. Present even on refusal — that is precisely the
    #: number an auditor needs to see ("it wanted 9000 g, ceiling refused").
    derived_grams: float | None = None
    per_unit_grams: float | None = None
    #: Plausibility codes when the trial refused the candidate result.
    blocking_codes: tuple[str, ...] = ()

    @property
    def converted(self) -> bool:
        """Mirrors the converter's bool for parity assertions."""
        return self.outcome == CONVERTED

    @property
    def is_failure(self) -> bool:
        """True only when conversion was attempted and genuinely refused.

        Stronger user evidence and an idempotent replay are NOT failures —
        reporting them as such is exactly the misclassification this module
        exists to prevent.
        """
        return self.outcome not in NON_FAILURE_OUTCOMES


def classify_pre_conversion(item: Any) -> ConversionDiagnostic:
    """The outcome ``materialize_count_quantity(item)`` WOULD produce.

    Pure: the item is only read. Mirrors the converter branch-for-branch in
    the converter's own order, reusing its predicates and thresholds.

    ``CONVERTED`` here means "would convert" — the caller pairs this with the
    converter's real bool (see :func:`classify_conversion`).
    """
    from meal_intelligence import (
        _MAX_DERIVED_GRAMS,
        _MAX_REASONABLE_COUNT,
        _SIZE_MULTIPLIER,
        _STRONGER_THAN_COUNT,
        QSOURCE_COUNT_DERIVED,
        _per_unit_weight,
        _recompute_macros_for_grams,
        _trial_item,
    )

    unit = str(getattr(item, "quantity_unit", "") or "")
    source = str(getattr(item, "quantity_source", "") or "")

    def _as_float(value: Any) -> float | None:
        """Never raise on a malformed field.

        This classifier runs on every meal render via the clarification
        layer, so a corrupt draft must degrade to "nothing to diagnose"
        rather than raise into the card.
        """
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    grams_before = _as_float(getattr(item, "grams", 0)) or 0.0

    # (0) No usable count evidence — converter :1508. A non-numeric count is
    # treated the same as an absent one: there is nothing to convert.
    count = _as_float(getattr(item, "quantity_count", None))
    if count is None or count <= 0:
        return ConversionDiagnostic(
            outcome=MISSING_OR_INVALID_COUNT,
            count=count,
            unit=unit,
            grams_before=grams_before,
            grams_after=grams_before,
            source_before=source,
            source_after=source,
        )

    def _d(outcome: str, **extra: Any) -> ConversionDiagnostic:
        """A decline: grams and source are unchanged by definition."""
        return ConversionDiagnostic(
            outcome=outcome,
            count=count,
            unit=unit,
            grams_before=grams_before,
            grams_after=grams_before,
            source_before=source,
            source_after=source,
            **extra,
        )

    # (5) Stronger gram evidence wins — converter :1514. NOT a failure: the
    # user's own grams outranking a count is the hierarchy working.
    if source in _STRONGER_THAN_COUNT:
        return _d(STRONGER_SOURCE_PRESERVED)

    # Unreasonable-count guard — converter :1520.
    if count > _MAX_REASONABLE_COUNT:
        return _d(IMPLAUSIBLE_COUNT)

    # (3) No supported portion model — converter :1524.
    weights = _per_unit_weight(item)
    if weights is None:
        return _d(NO_PORTION_MODEL)

    _lo, default, _hi = weights
    per_unit = default * _SIZE_MULTIPLIER.get("", 1.0)
    derived = round(per_unit * count, 1)

    # Total-weight ceiling — converter :1537.
    if derived > _MAX_DERIVED_GRAMS:
        return _d(BLOCKED_BY_CEILING, derived_grams=derived, per_unit_grams=per_unit)

    # (7) Idempotent replay — converter :1543. NOT a second success.
    if source == QSOURCE_COUNT_DERIVED and abs(grams_before - derived) < 0.05:
        return _d(IDEMPOTENT_NOOP, derived_grams=derived, per_unit_grams=per_unit)

    # (4) Macro coherence — converter :1549.
    new_macros = _recompute_macros_for_grams(item, derived, grams_before)
    if new_macros is None:
        return _d(MACROS_INCOHERENT, derived_grams=derived, per_unit_grams=per_unit)

    # (4b) Plausibility trial on the candidate — converter :1556.
    from noam_coach.services.meal_plausibility import check_item

    trial = _trial_item(item, derived, new_macros, count)
    blocking = tuple(
        issue.code for issue in check_item(trial) if issue.severity == "block"
    )
    if blocking:
        return _d(
            IMPLAUSIBLE_RESULT,
            derived_grams=derived,
            per_unit_grams=per_unit,
            blocking_codes=blocking,
        )

    # Everything the converter checks has passed: it would convert. The
    # converter's own try/except around assignment is a schema-rejection
    # backstop the ceilings above already make unreachable; a caller using
    # classify_conversion reconciles that residual case from the real bool.
    return ConversionDiagnostic(
        outcome=CONVERTED,
        count=count,
        unit=unit,
        grams_before=grams_before,
        grams_after=derived,
        source_before=source,
        source_after=QSOURCE_COUNT_DERIVED,
        derived_grams=derived,
        per_unit_grams=per_unit,
    )


def classify_conversion(
    diagnostic: ConversionDiagnostic,
    converted: bool,
    item: Any,
) -> ConversionDiagnostic:
    """Reconcile a pre-conversion prediction with the converter's real bool.

    ``diagnostic`` must come from :func:`classify_pre_conversion` called on
    the item BEFORE ``materialize_count_quantity`` ran; ``converted`` is that
    function's return value and ``item`` is the (possibly mutated) item.

    The prediction is authoritative for the REASON; the converter is
    authoritative for the FACT. When they disagree — only reachable through
    the converter's schema-rejection backstop — the converter wins and the
    outcome is reported as a refusal, never as a success.
    """
    grams_after = float(getattr(item, "grams", 0) or 0)
    source_after = str(getattr(item, "quantity_source", "") or "")

    if converted:
        return ConversionDiagnostic(
            outcome=CONVERTED,
            count=diagnostic.count,
            unit=diagnostic.unit,
            grams_before=diagnostic.grams_before,
            grams_after=grams_after,
            source_before=diagnostic.source_before,
            source_after=source_after,
            derived_grams=diagnostic.derived_grams,
            per_unit_grams=diagnostic.per_unit_grams,
        )

    if diagnostic.outcome == CONVERTED:
        # Predicted success, converter declined: the assignment backstop.
        return ConversionDiagnostic(
            outcome=MACROS_INCOHERENT,
            count=diagnostic.count,
            unit=diagnostic.unit,
            grams_before=diagnostic.grams_before,
            grams_after=grams_after,
            source_before=diagnostic.source_before,
            source_after=source_after,
            derived_grams=diagnostic.derived_grams,
            per_unit_grams=diagnostic.per_unit_grams,
        )

    return diagnostic
