"""Batch 6.1 — clarification/converter decision parity.

Batch 6 decided what to ASK using its own approximation of the conversion
rules. That approximation disagreed with ``materialize_count_quantity`` in
both directions: it asked about quantities the converter resolves cleanly,
and stayed silent on quantities the converter refuses.

These tests pin the corrected contract:

- the diagnostic classifier's outcome matches the converter's REAL bool for
  every reachable branch (no drift between "why" and "whether");
- the clarification layer asks if and only if the converter could not
  produce a quantity we can stand behind;
- user-pinned grams and idempotent replays are never reported as failures.

Every assertion runs against the real converter — nothing here re-implements
conversion logic, because a parity test that mirrors the code it checks
proves nothing.
"""

from __future__ import annotations

import copy
import itertools
from typing import Any

import pytest

from meal_intelligence import (
    _MAX_DERIVED_GRAMS,
    _MAX_REASONABLE_COUNT,
    materialize_count_quantity,
)
from models import FoodItem
from noam_coach.services.meal_clarification import (
    REASON_COUNT_GRAMS_CONFLICT,
    REASON_IMPLAUSIBLE,
    REASON_NO_PORTION_MODEL,
    _needs_clarification,
    detect_quantity_clarification,
)
from noam_coach.services.meal_quantity_diagnostics import (
    BLOCKED_BY_CEILING,
    CONVERTED,
    IDEMPOTENT_NOOP,
    IMPLAUSIBLE_COUNT,
    IMPLAUSIBLE_RESULT,
    MISSING_OR_INVALID_COUNT,
    NO_PORTION_MODEL,
    NON_FAILURE_OUTCOMES,
    STRONGER_SOURCE_PRESERVED,
    classify_conversion,
    classify_pre_conversion,
)


def _item(
    name: str = "שניצל",
    grams: float = 100.0,
    *,
    count: float | None = None,
    unit: str = "",
    source: str = "",
    calories: float = 200.0,
    protein: float = 10.0,
    carbs: float = 10.0,
    fat: float = 5.0,
) -> FoodItem:
    item = FoodItem(
        name=name, grams=grams, calories=calories, protein=protein,
        carbs=carbs, fat=fat, confidence=0.8,
    )
    item.grams = grams
    if count is not None:
        item.quantity_count = count
    if unit:
        item.quantity_unit = unit
    if source:
        item.quantity_source = source
    return item


# ---------------------------------------------------------------------------
# Parity: the diagnostic reason matches the converter's real bool
# ---------------------------------------------------------------------------

# One representative per reachable converter branch. Each carries the outcome
# the classifier must report AND the bool the converter must actually return.
_BRANCH_CASES: list[tuple[str, FoodItem, str, bool]] = [
    (
        "no count evidence",
        _item(),
        MISSING_OR_INVALID_COUNT,
        False,
    ),
    (
        "user-pinned grams outrank the count",
        _item(count=3, source="user"),
        STRONGER_SOURCE_PRESERVED,
        False,
    ),
    (
        "absurd count",
        _item(count=_MAX_REASONABLE_COUNT + 1, source="user_count"),
        IMPLAUSIBLE_COUNT,
        False,
    ),
    (
        "no supported portion model",
        _item(name="מרק קובה", count=3, source="user_count"),
        NO_PORTION_MODEL,
        False,
    ),
    (
        "result over the total-weight ceiling",
        _item(count=_MAX_REASONABLE_COUNT, source="user_count"),
        BLOCKED_BY_CEILING,
        False,
    ),
    (
        "idempotent replay of an already-derived quantity",
        _item(grams=450, count=3, source="count_derived"),
        IDEMPOTENT_NOOP,
        False,
    ),
    (
        "plausibility trial refuses the candidate",
        # An uncurated food (no israeli_foods entry) keeps its ~zero energy
        # through ratio-scaling, so the derived result trips the
        # zero-energy-solid block rule.
        _item(name="קציצה", grams=10, count=3, source="user_count",
              calories=0.1, protein=0, carbs=0, fat=0),
        IMPLAUSIBLE_RESULT,
        False,
    ),
    (
        "clean conversion",
        _item(count=3, source="user_count"),
        CONVERTED,
        True,
    ),
]


@pytest.mark.parametrize(
    "label,item,expected_outcome,expected_bool",
    _BRANCH_CASES,
    ids=[case[0] for case in _BRANCH_CASES],
)
def test_diagnostic_matches_converter_bool(
    label: str, item: FoodItem, expected_outcome: str, expected_bool: bool
) -> None:
    """For every branch: predicted reason and real converter bool agree."""
    predicted = classify_pre_conversion(item)
    assert predicted.outcome == expected_outcome, label

    probe = copy.deepcopy(item)
    actual = materialize_count_quantity(probe)
    assert actual is expected_bool, f"{label}: converter bool changed"

    final = classify_conversion(predicted, actual, probe)
    assert final.converted is actual, f"{label}: diagnostic/bool parity broken"


def test_ceiling_branch_is_genuinely_reachable() -> None:
    """blocked_by_ceiling is a real converter branch, not a theoretical one —
    and the refused number is preserved for the auditor."""
    item = _item(count=_MAX_REASONABLE_COUNT, source="user_count")
    diagnostic = classify_pre_conversion(item)

    assert diagnostic.outcome == BLOCKED_BY_CEILING
    assert diagnostic.derived_grams is not None
    assert diagnostic.derived_grams > _MAX_DERIVED_GRAMS
    # The count itself was legal; only the product exceeded the ceiling.
    assert diagnostic.count is not None and diagnostic.count <= _MAX_REASONABLE_COUNT


def test_converted_diagnostic_carries_full_provenance() -> None:
    """A conversion records count, unit, grams before/after and source."""
    item = _item(count=3, unit="כדור", source="user_count")
    predicted = classify_pre_conversion(item)
    converted = materialize_count_quantity(item)
    final = classify_conversion(predicted, converted, item)

    assert converted is True
    assert final.outcome == CONVERTED
    assert final.count == 3
    assert final.unit == "כדור"
    assert final.grams_before == 100
    assert final.grams_after == item.grams
    assert final.source_before == "user_count"
    assert final.source_after == "count_derived"
    assert final.per_unit_grams is not None


def test_declined_diagnostic_carries_authoritative_reason() -> None:
    """A decline names the branch that stopped it, not a generic failure."""
    item = _item(name="מרק קובה", count=3, source="user_count")
    predicted = classify_pre_conversion(item)
    converted = materialize_count_quantity(item)
    final = classify_conversion(predicted, converted, item)

    assert converted is False
    assert final.outcome == NO_PORTION_MODEL
    assert final.is_failure is True
    # A decline never invents a quantity.
    assert final.grams_after == final.grams_before


def test_stronger_user_evidence_is_not_a_failed_conversion() -> None:
    """User-pinned grams outranking a count is the hierarchy working."""
    item = _item(count=3, source="user")
    diagnostic = classify_pre_conversion(item)

    assert diagnostic.outcome == STRONGER_SOURCE_PRESERVED
    assert diagnostic.is_failure is False
    assert diagnostic.outcome in NON_FAILURE_OUTCOMES
    assert materialize_count_quantity(item) is False
    assert item.grams == 100, "the user's grams were overwritten"


def test_replay_is_a_noop_not_a_second_success() -> None:
    """Re-running conversion on a derived item reports idempotency."""
    item = _item(count=3, source="user_count")
    assert materialize_count_quantity(item) is True
    first_grams = item.grams

    replay = classify_pre_conversion(item)
    assert replay.outcome == IDEMPOTENT_NOOP
    assert replay.is_failure is False
    assert materialize_count_quantity(item) is False, "converted twice"
    assert item.grams == first_grams


# ---------------------------------------------------------------------------
# The parity contract itself: asking agrees with converting
# ---------------------------------------------------------------------------


def _sweep_items() -> list[FoodItem]:
    names = ["שניצל", "מרק קובה", "קציצה", "פלאפל", "בורקס", "לחם", "כוס שניצל"]
    counts = [0, 1, 2, 3, 5, 31, 99]
    grams_values = [0, 3, 10, 100, 450]
    sources = [
        "", "user_count", "count_derived", "user", "user_grams",
        "package_label", "visual_count", "estimate",
    ]
    items = []
    for name, count, grams, source in itertools.product(
        names, counts, grams_values, sources
    ):
        item = _item(name=name, grams=grams, source=source)
        item.quantity_count = count or None
        items.append(item)
    return items


def test_clarification_asks_if_and_only_if_conversion_fails() -> None:
    """THE Batch 6.1 contract, swept across the whole evidence space.

    We ask exactly when the converter cannot produce a quantity we can stand
    behind — with one deliberate exception: the count/grams conflict
    signature, which is asked even when the converter would re-derive grams,
    because stored grams equal to the count prove the two disagree.
    """
    mismatches: list[str] = []
    for item in _sweep_items():
        reason = _needs_clarification(item)
        probe = copy.deepcopy(item)
        converted = materialize_count_quantity(probe)

        if reason == REASON_COUNT_GRAMS_CONFLICT:
            continue  # the documented exception, pinned separately below

        if converted and reason:
            mismatches.append(
                f"asked {reason!r} for a quantity the converter resolved: "
                f"{item.name} count={item.quantity_count} grams={item.grams} "
                f"src={item.quantity_source}"
            )
        if not converted and not reason:
            diagnostic = classify_pre_conversion(item)
            if diagnostic.is_failure:
                mismatches.append(
                    f"silent on a real conversion failure "
                    f"({diagnostic.outcome}): {item.name} "
                    f"count={item.quantity_count} grams={item.grams} "
                    f"src={item.quantity_source}"
                )

    assert not mismatches, "clarification disagrees with conversion:\n" + "\n".join(
        mismatches[:15]
    )


def test_every_asked_reason_maps_to_a_real_decline_branch() -> None:
    """No question is raised for an outcome the converter treats as fine."""
    for item in _sweep_items():
        reason = _needs_clarification(item)
        if not reason:
            continue
        diagnostic = classify_pre_conversion(item)
        if reason == REASON_COUNT_GRAMS_CONFLICT:
            continue
        assert diagnostic.outcome not in {
            MISSING_OR_INVALID_COUNT,
            STRONGER_SOURCE_PRESERVED,
            IDEMPOTENT_NOOP,
        }, f"asked {reason!r} for benign outcome {diagnostic.outcome}"


def test_reason_taxonomy_is_consistent_with_the_classifier() -> None:
    """no_portion_model and implausible map to their converter branches."""
    no_model = _item(name="מרק קובה", count=3, source="user_count")
    assert _needs_clarification(no_model) == REASON_NO_PORTION_MODEL
    assert classify_pre_conversion(no_model).outcome == NO_PORTION_MODEL

    absurd = _item(count=_MAX_REASONABLE_COUNT + 1, source="user_count")
    assert _needs_clarification(absurd) == REASON_IMPLAUSIBLE
    assert classify_pre_conversion(absurd).outcome == IMPLAUSIBLE_COUNT

    ceiling = _item(count=_MAX_REASONABLE_COUNT, source="user_count")
    assert _needs_clarification(ceiling) == REASON_IMPLAUSIBLE
    assert classify_pre_conversion(ceiling).outcome == BLOCKED_BY_CEILING


# ---------------------------------------------------------------------------
# Regression pins: Batch 6 behaviour that must NOT drift
# ---------------------------------------------------------------------------


def test_incident_shape_is_still_asked_about() -> None:
    """count=3 / unit="כדור" / grams=3 must never pass silently, even though
    the converter would happily re-derive grams from the count."""
    item = _item(name="שניצל", grams=3, count=3, unit="כדור", source="user_count")

    assert _needs_clarification(item) == REASON_COUNT_GRAMS_CONFLICT
    # The converter WOULD convert it — which is exactly why the conflict
    # check must outrank the converter's outcome.
    probe = copy.deepcopy(item)
    assert materialize_count_quantity(probe) is True


def test_clean_count_is_still_not_questioned() -> None:
    """A count with a real portion model converts silently (Batch 6 pin)."""
    item = _item(count=3, source="user_count")
    assert _needs_clarification(item) == ""


def test_user_pinned_grams_are_still_never_questioned() -> None:
    """Batch 6 pin: the strongest evidence is never re-litigated."""
    from models import MealAnalysis

    item = _item(grams=500, count=3, source="user")
    analysis = MealAnalysis(meal_name="שניצל", confidence=0.7, items=[item])
    assert detect_quantity_clarification(analysis) is None


def test_classifier_never_mutates_the_item() -> None:
    """The classifier is pure — a diagnosis must not change the draft."""
    for item in _sweep_items()[:200]:
        before = (
            item.grams, item.calories, item.protein, item.carbs, item.fat,
            getattr(item, "quantity_count", None),
            getattr(item, "quantity_source", ""),
            getattr(item, "quantity_unit", ""),
        )
        classify_pre_conversion(item)
        after = (
            item.grams, item.calories, item.protein, item.carbs, item.fat,
            getattr(item, "quantity_count", None),
            getattr(item, "quantity_source", ""),
            getattr(item, "quantity_unit", ""),
        )
        assert before == after, f"classifier mutated {item.name}"


class _BrokenGrams:
    name = "בעייתי"
    grams = "not-a-number"
    quantity_count = "three"


class _BrokenCountWithModel:
    name = "שניצל"
    grams = "bad"
    quantity_count = 3
    quantity_source = "user_count"


class _BrokenName:
    name = None
    grams = 100
    quantity_count = 3
    quantity_source = "user_count"


class _Empty:
    pass


@pytest.mark.parametrize(
    "broken",
    [_BrokenGrams, _BrokenCountWithModel, _BrokenName, _Empty],
    ids=["bad-grams-and-count", "bad-grams-valid-count", "no-name", "empty"],
)
def test_malformed_items_never_raise(broken: type) -> None:
    """A corrupt draft must degrade, not raise.

    Both entry points run on every meal render, so an exception here would
    surface as a broken card rather than a bad quantity.
    """
    diagnostic = classify_pre_conversion(broken())
    assert isinstance(diagnostic.outcome, str) and diagnostic.outcome

    # The clarification layer calls the classifier on every render.
    reason = _needs_clarification(broken())
    assert isinstance(reason, str)
