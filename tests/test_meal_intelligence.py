from __future__ import annotations

import pytest

import meal_intelligence
from models import FoodItem, MealAnalysis


def _analysis() -> MealAnalysis:
    return MealAnalysis(
        meal_name="צלחת",
        confidence=0.8,
        items=[
            FoodItem(
                name="אורז לבן מבושל",
                grams=120,
                calories=156,
                protein=3,
                carbs=34,
                fat=0.4,
                confidence=0.8,
            )
        ],
    )


def _analysis_with_oil() -> MealAnalysis:
    """Analysis that includes an oil line item alongside rice."""
    return MealAnalysis(
        meal_name="סלט",
        confidence=0.8,
        items=[
            FoodItem(
                name="אורז לבן מבושל",
                grams=120,
                calories=156,
                protein=3,
                carbs=34,
                fat=0.4,
                confidence=0.8,
            ),
            FoodItem(
                name="שמן זית",
                grams=10,
                calories=88,
                protein=0,
                carbs=0,
                fat=10,
                confidence=0.7,
            ),
        ],
    )


def _analysis_with_rice_chicken_oil() -> MealAnalysis:
    return MealAnalysis(
        meal_name="צלחת",
        confidence=0.82,
        items=[
            FoodItem(
                name="אורז לבן מבושל",
                grams=200,
                calories=260,
                protein=5,
                carbs=56,
                fat=1,
                confidence=0.85,
            ),
            FoodItem(
                name="חזה עוף",
                grams=180,
                calories=300,
                protein=55,
                carbs=0,
                fat=7,
                confidence=0.86,
            ),
            FoodItem(
                name="שמן זית",
                grams=10,
                calories=88,
                protein=0,
                carbs=0,
                fat=10,
                confidence=0.75,
            ),
        ],
    )


def test_explicit_quantity_is_a_hard_constraint() -> None:
    constraints = meal_intelligence.parse_locked_quantities("אורז 150 גרם")
    corrected, unmatched = meal_intelligence.apply_locked_quantities(_analysis(), constraints)
    assert not unmatched
    assert corrected.items[0].grams == 150
    assert corrected.items[0].confidence >= 0.95


def test_sensitive_starch_requires_cooked_or_raw_clarification() -> None:
    constraints = meal_intelligence.parse_locked_quantities("150 גרם אורז")
    assert meal_intelligence.requires_cooked_raw_clarification(constraints)
    cooked = meal_intelligence.parse_locked_quantities("150 גרם אורז אחרי בישול")
    assert not meal_intelligence.requires_cooked_raw_clarification(cooked)


def test_hamming_distance_for_identical_hashes_is_zero() -> None:
    assert meal_intelligence.hamming_distance_hex("0f" * 8, "0f" * 8) == 0


# ---------------------------------------------------------------------------
# REC-PLAN-MEAL-03-12: Oil-removal correction tests
# ---------------------------------------------------------------------------

def test_bli_shemen_parsed_as_removal() -> None:
    """'בלי שמן' must produce a 'remove' correction, not a preparation change."""
    corrections = meal_intelligence.parse_meal_correction("בלי שמן")
    assert len(corrections) == 1
    assert corrections[0].kind == "remove"
    assert corrections[0].item_hint == "שמן"


def test_llal_shemen_parsed_as_removal() -> None:
    """'ללא שמן' must produce a 'remove' correction."""
    corrections = meal_intelligence.parse_meal_correction("ללא שמן")
    assert len(corrections) == 1
    assert corrections[0].kind == "remove"
    assert corrections[0].item_hint == "שמן"


def test_bli_shemen_zait_parsed_as_removal() -> None:
    """'בלי שמן זית' must produce a 'remove' correction."""
    corrections = meal_intelligence.parse_meal_correction("בלי שמן זית")
    assert len(corrections) == 1
    assert corrections[0].kind == "remove"
    assert corrections[0].item_hint == "שמן"


def test_apply_item_removal_removes_oil() -> None:
    """Applying the removal correction must drop the oil item from the analysis."""
    analysis = _analysis_with_oil()
    assert len(analysis.items) == 2

    corrections = meal_intelligence.parse_meal_correction("בלי שמן")
    assert corrections[0].kind == "remove"

    result = meal_intelligence.apply_item_removal_correction(analysis, corrections[0])
    assert len(result.items) == 1
    assert result.items[0].name == "אורז לבן מבושל"
    # A note should record the removal
    assert any("שמן" in note for note in result.notes)


def test_apply_item_removal_no_match_leaves_analysis_unchanged() -> None:
    """If the hinted item is not in the analysis, the analysis must not change."""
    analysis = _analysis()  # no oil item
    original_items = list(analysis.items)

    correction = meal_intelligence.MealCorrection(
        kind="remove", item_hint="שמן", value="", original_text="בלי שמן"
    )
    result = meal_intelligence.apply_item_removal_correction(analysis, correction)
    # Items list must be unchanged
    assert result.items == original_items


def test_later_quantity_correction_does_not_restore_removed_oil() -> None:
    analysis = _analysis_with_rice_chicken_oil()

    remove = meal_intelligence.parse_meal_correction("בלי שמן")[0]
    without_oil = meal_intelligence.apply_item_removal_correction(analysis, remove)
    quantity = meal_intelligence.parse_meal_correction("האורז היה 150 גרם")
    corrected, unmatched = meal_intelligence.apply_locked_quantities(
        without_oil,
        meal_intelligence.parse_locked_quantities(quantity[0].original_text),
    )

    assert not unmatched
    assert all("שמן" not in item.name for item in corrected.items)
    rice = next(item for item in corrected.items if "אורז" in item.name)
    assert rice.grams == 150


def test_half_of_rice_scales_only_rice() -> None:
    analysis = _analysis_with_rice_chicken_oil()
    correction = meal_intelligence.parse_meal_correction("חצי מהאורז")[0]

    corrected = meal_intelligence.apply_scale_correction(analysis, correction)

    rice = next(item for item in corrected.items if "אורז" in item.name)
    chicken = next(item for item in corrected.items if "עוף" in item.name)
    assert rice.grams == 100
    assert rice.calories == 130
    assert chicken.grams == 180


def test_whole_meal_double_and_x3_are_parsed_and_scaled() -> None:
    doubled = meal_intelligence.parse_meal_correction("הכל כפול")[0]
    assert doubled.kind == "scale"
    assert doubled.item_hint == ""
    assert doubled.value == "2.0"

    tripled = meal_intelligence.parse_meal_correction("x3")[0]
    corrected = meal_intelligence.apply_scale_correction(_analysis(), tripled)
    assert corrected.items[0].grams == 360
    assert corrected.items[0].calories == 468


def test_removal_takes_priority_over_preparation_in_parser() -> None:
    """parse_meal_correction must return only removal when the text is 'בלי שמן',
    not a preparation correction."""
    corrections = meal_intelligence.parse_meal_correction("בלי שמן")
    kinds = {c.kind for c in corrections}
    assert kinds == {"remove"}
    assert "preparation" not in kinds


# ---------------------------------------------------------------------------
# Batch 1 characterization (FINAL_MEAL_INTERACTION_IMPLEMENTATION_PLAN.md,
# 2026-07-19 falafel/schnitzel root-cause audit).
#
# Two groups:
#   1. FROZEN behavior — replacement forms, explicit gram locking, and the
#      "count phrases never corrupt grams deterministically" safety property.
#      These must keep passing through every later batch.
#   2. KNOWN GAPS — pinned as strict xfail so Batch 2 (remove/add parsing)
#      and Batch 4 (count quantity model) must consciously flip them by
#      removing the marker. An unexpected pass fails the suite (strict).
# ---------------------------------------------------------------------------


def _falafel_analysis() -> MealAnalysis:
    """The incident draft: one falafel item, 120 g / 396 kcal (trace event 1200)."""
    return MealAnalysis(
        meal_name="פלאפל",
        confidence=0.85,
        items=[
            FoodItem(
                name="פלאפל",
                grams=120,
                calories=396,
                protein=13,
                carbs=31,
                fat=24,
                confidence=0.85,
            )
        ],
    )


SUPPORTED_REPLACEMENT_FORMS = [
    "לא פלאפל, שניצל",       # the incident correction (trace event 1204)
    "לא פלאפל אלא שניצל",
    "זה שניצל, לא פלאפל",
    "שניצל במקום פלאפל",
]


@pytest.mark.parametrize("text", SUPPORTED_REPLACEMENT_FORMS)
def test_supported_replacement_forms_parse_as_replace(text: str) -> None:
    """FROZEN: every currently-working replacement form stays a canonical
    replace(rejected=פלאפל, confirmed=שניצל)."""
    corrections = meal_intelligence.parse_meal_correction(text)
    assert [c.kind for c in corrections] == ["replace"]
    assert corrections[0].item_hint == "פלאפל"
    assert corrections[0].value == "שניצל"
    constraints = meal_intelligence.identity_constraints_from_texts([text])
    assert [(c.rejected, c.confirmed) for c in constraints] == [("פלאפל", "שניצל")]


def test_supported_replacement_preserves_grams_and_recalculates_macros() -> None:
    """FROZEN: applying the incident correction renames the item, preserves
    the observed 120 g, and recalculates calories deterministically (the
    historical trace showed exactly 120 g / 336 kcal after event 1210)."""
    correction = meal_intelligence.parse_meal_correction("לא פלאפל, שניצל")[0]
    corrected = meal_intelligence.apply_item_replacement_correction(
        _falafel_analysis(), correction
    )
    names = [item.name for item in corrected.items]
    assert not any("פלאפל" in name for name in names)
    schnitzel = next(item for item in corrected.items if "שניצל" in item.name)
    assert schnitzel.grams == 120  # grams are NEVER invented by a rename
    assert schnitzel.calories != 396  # macros re-derived for the new identity
    assert 150 <= schnitzel.calories <= 500  # plausible schnitzel at 120 g


UNSUPPORTED_REMOVE_ADD_FORMS = [
    # The audit's reproducible gap: none of these currently produce a
    # replace/constraint, so post-AI enforcement has nothing to enforce.
    # Worst of them: "הסר פלאפל והוסף שניצל" mis-parses today as a REMOVAL
    # with the polluted hint "פלאפל והוסף שניצל", which deletes the falafel
    # item and adds nothing — the meal ends up empty.
    "תוריד פלאפל ותוסיף שניצל",
    "הסר פלאפל והוסף שניצל",
    "תוציא פלאפל ותשים שניצל",
    "תחליף פלאפל בשניצל",
    "במקום פלאפל יש שניצל",
    "במקום פלאפל זה שניצל",
]


@pytest.mark.parametrize("text", UNSUPPORTED_REMOVE_ADD_FORMS)
@pytest.mark.xfail(
    strict=True,
    reason="Batch 2 TODO: normalize Hebrew remove-and-add phrasing to a canonical replace",
)
def test_remove_add_forms_should_parse_as_canonical_replace(text: str) -> None:
    corrections = meal_intelligence.parse_meal_correction(text)
    assert [c.kind for c in corrections] == ["replace"]
    assert corrections[0].item_hint == "פלאפל"
    assert corrections[0].value == "שניצל"


@pytest.mark.xfail(
    strict=True,
    reason="Batch 2 TODO: remove/add phrasing must yield an identity constraint",
)
def test_remove_add_form_should_yield_identity_constraint() -> None:
    constraints = meal_intelligence.identity_constraints_from_texts(
        ["תוריד פלאפל ותוסיף שניצל"]
    )
    assert [(c.rejected, c.confirmed) for c in constraints] == [("פלאפל", "שניצל")]


@pytest.mark.xfail(
    strict=True,
    reason="Batch 2 TODO: 'תוריד X' alone should parse as a remove-only correction",
)
def test_toride_alone_should_parse_as_remove_only() -> None:
    corrections = meal_intelligence.parse_meal_correction("תוריד פלאפל")
    assert [c.kind for c in corrections] == ["remove"]
    assert corrections[0].item_hint == "פלאפל"


def test_add_only_command_creates_no_replacement_or_rejection() -> None:
    """FROZEN ambiguity rule: 'תוסיף שניצל' is add-only — it must never
    produce a replace correction or reject any identity."""
    corrections = meal_intelligence.parse_meal_correction("תוסיף שניצל")
    assert not any(c.kind == "replace" for c in corrections)
    assert meal_intelligence.identity_constraints_from_texts(["תוסיף שניצל"]) == []


def test_two_foods_without_connector_do_not_parse_as_replace() -> None:
    """FROZEN ambiguity rule: 'פלאפל שניצל' has no replacement connector —
    it must stay unparsed (AI/clarification fallback), never a guessed swap."""
    corrections = meal_intelligence.parse_meal_correction("פלאפל שניצל")
    assert not any(c.kind == "replace" for c in corrections)


def test_multi_item_remove_add_never_invents_extra_rejections() -> None:
    """FROZEN safety rule (holds today and must survive Batch 2): a
    multi-item correction may only ever reject the explicitly removed food —
    it must not manufacture rejections for the added foods."""
    constraints = meal_intelligence.identity_constraints_from_texts(
        ["תוריד פלאפל ותוסיף שניצל וחציל"]
    )
    assert all(c.rejected == "פלאפל" for c in constraints)


# --- Count/portion quantity characterization (Batch 4 target) --------------

COUNT_PHRASES = [
    "3 שניצלים",
    "3 כדורי פלאפל",
    "שניצל אחד גדול",
    "שלוש חתיכות",
    "יש יותר שניצל",
]


@pytest.mark.parametrize("text", COUNT_PHRASES)
def test_count_phrases_never_produce_gram_locks(text: str) -> None:
    """FROZEN safety property: a count phrase must never be interpreted as
    an explicit gram amount ('3 שניצלים' must not lock grams=3). Today these
    phrases produce no deterministic parse at all; after Batch 4 they must
    parse as counts — but never as gram locks."""
    locked = meal_intelligence.parse_locked_quantities(text)
    assert locked == []
    corrections = meal_intelligence.parse_meal_correction(text)
    assert not any(c.kind == "quantity" for c in corrections)


@pytest.mark.parametrize("text", COUNT_PHRASES[:4])
@pytest.mark.xfail(
    strict=True,
    reason="Batch 4 TODO: count/portion phrases need a deterministic non-gram representation",
)
def test_count_phrases_should_parse_deterministically(text: str) -> None:
    """Desired (Batch 4): count phrases produce SOME deterministic
    correction object (count/portion — exact shape defined in Batch 4),
    instead of falling through to AI with no lock."""
    corrections = meal_intelligence.parse_meal_correction(text)
    assert corrections != []
    assert not any(c.kind == "quantity" for c in corrections)


def test_explicit_gram_lock_still_works_for_schnitzel() -> None:
    """FROZEN: 'השניצל בערך 180 גרם' locks 180 g and scales macros —
    the working path the plan forbids regressing."""
    analysis = MealAnalysis(
        meal_name="שניצל",
        confidence=0.8,
        items=[
            FoodItem(
                name="שניצל",
                grams=100,
                calories=250,
                protein=20,
                carbs=12,
                fat=14,
                confidence=0.8,
            )
        ],
    )
    locked = meal_intelligence.parse_locked_quantities("השניצל בערך 180 גרם")
    assert [q.grams for q in locked] == [180.0]
    corrected, unmatched = meal_intelligence.apply_locked_quantities(analysis, locked)
    assert not unmatched
    assert corrected.items[0].grams == 180
    assert corrected.items[0].calories == 450  # scaled by 1.8, not re-invented
    assert corrected.items[0].confidence >= 0.95
