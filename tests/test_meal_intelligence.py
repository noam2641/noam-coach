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
#   2. KNOWN GAPS — pinned as strict xfail so the implementing batch must
#      consciously flip them by removing the marker. Batch 2 flipped the
#      remove/add parsing group and Batch 4 flipped the count-quantity group
#      (both now regular passing tests). No strict xfails remain in this file.
#      An unexpected pass fails the suite (strict).
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


REMOVE_ADD_REPLACEMENT_FORMS = [
    # Batch 2 (audit Finding A): natural remove-and-add / swap phrasing now
    # normalizes to the SAME canonical replace as the classic forms. Before
    # Batch 2 the worst of these ("הסר פלאפל והוסף שניצל") mis-parsed as a
    # removal with the polluted hint "פלאפל והוסף שניצל", which deleted the
    # falafel item and added nothing — the meal ended up empty.
    "תוריד פלאפל ותוסיף שניצל",
    "הסר פלאפל והוסף שניצל",
    "תוציא פלאפל ותשים שניצל",
    "תחליף פלאפל בשניצל",
    "במקום פלאפל יש שניצל",
    "במקום פלאפל זה שניצל",
]

PUNCTUATION_AND_SPACING_VARIANTS = [
    "תוריד פלאפל, ותוסיף שניצל",  # comma between clauses
    "תוריד פלאפל ותוסיף שניצל.",  # trailing period
    "הסר פלאפל: והוסף שניצל",  # colon
    "הסר פלאפל - והוסף שניצל",  # dash
    "הסר פלאפל; והוסף שניצל",  # semicolon
    "תוריד  פלאפל   ותוסיף  שניצל",  # extra whitespace
    "לא פלאפל, שניצל.",  # trailing period on a classic form
]


@pytest.mark.parametrize(
    "text", REMOVE_ADD_REPLACEMENT_FORMS + PUNCTUATION_AND_SPACING_VARIANTS
)
def test_remove_add_forms_parse_as_canonical_replace(text: str) -> None:
    corrections = meal_intelligence.parse_meal_correction(text)
    assert [c.kind for c in corrections] == ["replace"]
    assert corrections[0].item_hint == "פלאפל"
    assert corrections[0].value == "שניצל"
    # original_text is preserved verbatim for locked-correction replay.
    assert corrections[0].original_text == text


def test_all_equivalent_replacement_forms_are_structurally_identical() -> None:
    """Every replacement-equivalent surface form — classic or verb-based —
    produces one structurally identical canonical MealCorrection."""
    canonical = ("replace", "פלאפל", "שניצל")
    for text in SUPPORTED_REPLACEMENT_FORMS + REMOVE_ADD_REPLACEMENT_FORMS:
        corrections = meal_intelligence.parse_meal_correction(text)
        assert [(c.kind, c.item_hint, c.value) for c in corrections] == [canonical], text
        constraints = meal_intelligence.identity_constraints_from_texts([text])
        assert [(c.rejected, c.confirmed) for c in constraints] == [
            ("פלאפל", "שניצל")
        ], text


def test_remove_add_form_yields_identity_constraint() -> None:
    constraints = meal_intelligence.identity_constraints_from_texts(
        ["תוריד פלאפל ותוסיף שניצל"]
    )
    assert [(c.rejected, c.confirmed) for c in constraints] == [("פלאפל", "שניצל")]


def test_hasar_form_never_produces_polluted_removal_hint() -> None:
    """Audit Finding A, direct-call safety: even the removal parser alone can
    never emit remove(item_hint='פלאפל והוסף שניצל') — the hint is truncated
    at the add connector."""
    removals = meal_intelligence._parse_removal_corrections("הסר פלאפל והוסף שניצל")
    assert all(c.item_hint == "פלאפל" for c in removals)
    # ...and the full parser routes the text to replacement, never removal.
    kinds = [c.kind for c in meal_intelligence.parse_meal_correction("הסר פלאפל והוסף שניצל")]
    assert kinds == ["replace"]


def test_hasar_form_cannot_empty_the_meal() -> None:
    """The destructive-path regression: applying the parsed correction to a
    falafel meal must produce a schnitzel meal — never an empty one."""
    analysis = _falafel_analysis()
    corrections = meal_intelligence.parse_meal_correction("הסר פלאפל והוסף שניצל")
    assert [c.kind for c in corrections] == ["replace"]
    corrected = meal_intelligence.apply_item_replacement_correction(
        analysis, corrections[0]
    )
    assert corrected.items  # the meal can never end up empty
    names = [item.name for item in corrected.items]
    assert not any("פלאפל" in name for name in names)
    schnitzel = next(item for item in corrected.items if "שניצל" in item.name)
    assert schnitzel.grams == 120  # grams preserved, exactly like classic forms


REMOVE_ONLY_FORMS = [
    "תוריד פלאפל",
    "הסר פלאפל",
    "תוציא פלאפל",
    "בלי פלאפל",
    "ללא פלאפל",
]


@pytest.mark.parametrize("text", REMOVE_ONLY_FORMS)
def test_remove_only_forms_parse_clean(text: str) -> None:
    """Remove-only language (audit Finding B) parses as a clean removal whose
    hint contains only the removed item — never a replacement. Batch 3: it
    also yields a rejected-only identity constraint (confirmed=""), so a
    removed food can no longer return through a later AI reanalysis."""
    corrections = meal_intelligence.parse_meal_correction(text)
    assert [c.kind for c in corrections] == ["remove"]
    assert corrections[0].item_hint == "פלאפל"
    constraints = meal_intelligence.identity_constraints_from_texts([text])
    assert [(c.rejected, c.confirmed) for c in constraints] == [("פלאפל", "")]


def test_removal_guards_protect_scale_and_quantity_language() -> None:
    """'תוריד' as a quantity/scale verb must not become an item removal:
    'תוריד חצי' stays a whole-meal scale and 'תוריד קצת מהאורז' falls
    through (no deterministic removal of a 'קצת' item)."""
    scale = meal_intelligence.parse_meal_correction("תוריד חצי")
    assert [c.kind for c in scale] == ["scale"]
    fallthrough = meal_intelligence.parse_meal_correction("תוריד קצת מהאורז")
    assert not any(c.kind == "remove" for c in fallthrough)


# ---------------------------------------------------------------------------
# Batch 3 — identity-constraint semantics: ולא direction, definite article,
# add-only reversal, removal constraints, supersede, replay equivalence.
# ---------------------------------------------------------------------------


def test_velo_direction_supported_forms() -> None:
    """Correct ולא direction: 'שניצל ולא פלאפל' means the meal IS schnitzel
    and NOT falafel — replace(פלאפל→שניצל). Classic forms keep working."""
    corrections = meal_intelligence.parse_meal_correction("שניצל ולא פלאפל")
    assert [(c.kind, c.item_hint, c.value) for c in corrections] == [
        ("replace", "פלאפל", "שניצל")
    ]
    classic = meal_intelligence.parse_meal_correction("זו קולה זירו ולא קולה רגילה")
    assert [(c.kind, c.item_hint, c.value) for c in classic] == [
        ("replace", "קולה רגילה", "קולה זירו")
    ]


def test_velo_after_remove_verb_is_remove_only() -> None:
    """'תוריד פלאפל ולא שניצל' = "remove the falafel, not the schnitzel" —
    a remove-only correction for פלאפל. Before Batch 3 this matched the
    general 'X ולא Y' pattern REVERSED (rejecting שניצל and confirming the
    verb-polluted phrase 'תוריד פלאפל')."""
    corrections = meal_intelligence.parse_meal_correction("תוריד פלאפל ולא שניצל")
    assert [(c.kind, c.item_hint) for c in corrections] == [("remove", "פלאפל")]
    constraints = meal_intelligence.identity_constraints_from_texts(
        ["תוריד פלאפל ולא שניצל"]
    )
    assert [(c.rejected, c.confirmed) for c in constraints] == [("פלאפל", "")]


def test_hedged_velo_phrasing_creates_no_replacement() -> None:
    """A hedged sentence must not become a hard identity lock — it falls
    through to the AI/clarification path."""
    corrections = meal_intelligence.parse_meal_correction("אני חושב שזה שניצל ולא פלאפל")
    assert not any(c.kind == "replace" for c in corrections)
    assert (
        meal_intelligence.identity_constraints_from_texts(["אולי שניצל ולא פלאפל"]) == []
    )


def test_definite_article_target_matches_bare_item() -> None:
    """'תוריד הפלאפל' / 'תוריד את הפלאפל ותוסיף שניצל' must act on the item
    named 'פלאפל' — comparison-only normalization, names never rewritten."""
    removal = meal_intelligence.parse_meal_correction("תוריד הפלאפל")
    assert [(c.kind, c.item_hint) for c in removal] == [("remove", "הפלאפל")]
    removed = meal_intelligence.apply_item_removal_correction(
        _falafel_analysis(), removal[0]
    )
    assert removed.items == []

    replace = meal_intelligence.parse_meal_correction("תוריד את הפלאפל ותוסיף שניצל")
    assert [c.kind for c in replace] == ["replace"]
    corrected = meal_intelligence.apply_item_replacement_correction(
        _falafel_analysis(), replace[0]
    )
    names = [item.name for item in corrected.items]
    assert names and not any("פלאפל" in name for name in names)
    assert any("שניצל" in name for name in names)
    assert corrected.items[0].grams == 120


def test_definite_article_normalization_does_not_corrupt_names() -> None:
    """Genuine ה-initial food names keep matching themselves ('הודו' is a
    food, not a definite article on 'ודו') and no unrelated match appears."""
    turkey = MealAnalysis(
        meal_name="הודו",
        confidence=0.9,
        items=[
            FoodItem(name="הודו מעושן", grams=100, calories=110, protein=20,
                     carbs=1, fat=3, confidence=0.9)
        ],
    )
    correction = meal_intelligence.parse_meal_correction("לא הודו אלא עוף")[0]
    corrected = meal_intelligence.apply_item_replacement_correction(turkey, correction)
    names = [item.name for item in corrected.items]
    assert any("עוף" in name for name in names)
    assert not any("הודו" in name for name in names)
    # Rejected 'הפלאפל' matches falafel variants, nothing else.
    assert meal_intelligence._matches_rejected_identity("פלאפל כשר (3 כדורים)", "הפלאפל")
    assert not meal_intelligence._matches_rejected_identity("סלט ירקות", "הפלאפל")


def test_add_only_parses_as_add_kind() -> None:
    corrections = meal_intelligence.parse_meal_correction("תוסיף שניצל")
    assert [(c.kind, c.item_hint, c.value) for c in corrections] == [
        ("add", "", "שניצל")
    ]


def test_explicit_add_reverses_earlier_rejection() -> None:
    """TASK-58's escape hatch: an explicit later add of a rejected food
    reverses the rejection — for both replacement- and removal-born
    constraints. Order matters: a removal AFTER the add stands."""
    assert (
        meal_intelligence.identity_constraints_from_texts(
            ["לא פלאפל, שניצל", "תוסיף פלאפל"]
        )
        == []
    )
    assert (
        meal_intelligence.identity_constraints_from_texts(
            ["תוריד פלאפל", "תוסיף פלאפל"]
        )
        == []
    )
    later_removal = meal_intelligence.identity_constraints_from_texts(
        ["תוסיף פלאפל", "תוריד פלאפל"]
    )
    assert [(c.rejected, c.confirmed) for c in later_removal] == [("פלאפל", "")]


def test_removal_constraint_enforcement_deletes_reintroduced_item() -> None:
    """Remove-only history must strip a reintroduced item during enforcement
    (previously removal texts produced no constraint at all), and repeated
    enforcement is idempotent."""
    rice = FoodItem(name="אורז לבן", grams=150, calories=195, protein=4,
                    carbs=42, fat=0.5, confidence=0.9)
    analysis = _falafel_analysis()
    analysis.items.append(rice)
    constraints = meal_intelligence.identity_constraints_from_texts(["תוריד פלאפל"])
    enforced, actions = meal_intelligence.enforce_identity_constraints(
        analysis, constraints
    )
    assert [item.name for item in enforced.items] == ["אורז לבן"]
    assert [a["action"] for a in actions] == ["removed_rejected"]
    again, actions2 = meal_intelligence.enforce_identity_constraints(
        enforced, constraints
    )
    assert [item.name for item in again.items] == ["אורז לבן"]
    assert actions2 == []  # idempotent — nothing left to enforce


def test_same_rejected_food_latest_constraint_wins() -> None:
    """Two corrections about the SAME rejected food: the newest supersedes
    ('לא פלאפל, שניצל' then 'לא פלאפל, חזה עוף' → פלאפל→חזה עוף only)."""
    constraints = meal_intelligence.identity_constraints_from_texts(
        ["לא פלאפל, שניצל", "לא פלאפל, חזה עוף"]
    )
    assert [(c.rejected, c.confirmed) for c in constraints] == [("פלאפל", "חזה עוף")]


def test_correction_replay_matches_sequential_application() -> None:
    """Replaying the locked-correction history against the ORIGINAL analysis
    produces exactly the state sequential application produced — names,
    grams and calories identical (deterministic replay guarantee)."""
    first, second = "לא פלאפל, שניצל", "לא שניצל, חזה עוף"
    sequential = _falafel_analysis()
    for text in (first, second):
        sequential = meal_intelligence.apply_item_replacement_correction(
            sequential, meal_intelligence.parse_meal_correction(text)[0]
        )
    replayed, _ = meal_intelligence.enforce_identity_constraints(
        _falafel_analysis(),
        meal_intelligence.identity_constraints_from_texts([first, second]),
    )
    assert [
        (item.name, item.grams, item.calories) for item in sequential.items
    ] == [(item.name, item.grams, item.calories) for item in replayed.items]
    assert [item.name for item in replayed.items] == ["חזה עוף"]
    assert replayed.items[0].grams == 120


@pytest.mark.parametrize("text", ["תוסיף שניצל", "הוסף שניצל", "שים שניצל"])
def test_add_only_commands_create_no_replacement_or_rejection(text: str) -> None:
    """FROZEN ambiguity rule: add-only commands must never produce a replace
    or remove correction, and must never reject any identity."""
    corrections = meal_intelligence.parse_meal_correction(text)
    assert not any(c.kind in {"replace", "remove"} for c in corrections)
    assert meal_intelligence.identity_constraints_from_texts([text]) == []


@pytest.mark.parametrize(
    "text",
    [
        "פלאפל שניצל",  # no connector
        "יש פלאפל וגם שניצל",  # coexistence, not replacement
        "אולי פלאפל או שניצל",  # uncertainty, not replacement
    ],
)
def test_ambiguous_two_food_mentions_do_not_parse_as_replace(text: str) -> None:
    """FROZEN ambiguity rule: unrelated/uncertain two-food mentions must stay
    unparsed (AI/clarification fallback) — never a guessed swap or a false
    rejected/confirmed pair."""
    corrections = meal_intelligence.parse_meal_correction(text)
    assert not any(c.kind in {"replace", "remove"} for c in corrections)
    assert meal_intelligence.identity_constraints_from_texts([text]) == []


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
    an explicit gram amount ('3 שניצלים' must not lock grams=3). Batch 4
    makes them parse as counts — but never as gram locks."""
    locked = meal_intelligence.parse_locked_quantities(text)
    assert locked == []
    corrections = meal_intelligence.parse_meal_correction(text)
    assert not any(c.kind == "quantity" for c in corrections)


@pytest.mark.parametrize("text", COUNT_PHRASES[:4])
def test_count_phrases_parse_deterministically(text: str) -> None:
    """Batch 4: count/portion phrases produce a deterministic ``count``
    correction (not a gram lock, not an AI fallthrough). "יש יותר שניצל"
    (COUNT_PHRASES[4]) is intentionally excluded — it states no quantity."""
    corrections = meal_intelligence.parse_meal_correction(text)
    assert [c.kind for c in corrections] == ["count"]
    assert corrections[0].quantity is not None
    assert corrections[0].quantity.count > 0
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


# ---------------------------------------------------------------------------
# Batch 4 — count/portion quantity domain (parse_quantity_expression,
# apply_count_correction). Counts are NEVER grams.
# ---------------------------------------------------------------------------


def _one_item(name: str = "שניצל", grams: float = 120) -> MealAnalysis:
    return MealAnalysis(
        meal_name=name,
        confidence=0.85,
        items=[
            FoodItem(name=name, grams=grams, calories=grams * 2, protein=grams * 0.1,
                     carbs=grams * 0.2, fat=grams * 0.05, confidence=0.85)
        ],
    )


@pytest.mark.parametrize(
    ("text", "count", "unit", "size", "food"),
    [
        # Counts
        ("שניצל אחד", 1.0, "", "", "שניצל"),
        ("שני שניצלים", 2.0, "", "", "שניצלים"),
        ("שלושה שניצלים", 3.0, "", "", "שניצלים"),
        ("ארבעה שניצלים", 4.0, "", "", "שניצלים"),
        ("3 שניצלים", 3.0, "", "", "שניצלים"),
        ("חצי שניצל", 0.5, "", "", "שניצל"),
        ("רבע פיתה", 0.25, "", "", "פיתה"),
        ("אחד וחצי", 1.5, "", "", ""),
        ("שתיים וחצי קציצות", 2.5, "קציצה", "", ""),
        ("שניצל וחצי", 1.5, "", "", "שניצל"),
        # Portion units
        ("חצי כוס", 0.5, "כוס", "", ""),
        ("שלוש כפות", 3.0, "כף", "", ""),
        ("שתי פרוסות", 2.0, "פרוסה", "", ""),
        ("3 כדורי פלאפל", 3.0, "כדור", "", "פלאפל"),
        ("שלוש חתיכות", 3.0, "חתיכה", "", ""),
        ("שתי קציצות", 2.0, "קציצה", "", ""),
        # Size modifiers / mixed expressions
        ("שניצל אחד גדול", 1.0, "", "large", "שניצל"),
        ("שני שניצלים גדולים", 2.0, "", "large", "שניצלים"),
        ("שלוש קציצות קטנות", 3.0, "קציצה", "small", ""),
        ("חצי פיתה", 0.5, "", "", "פיתה"),
        # Approximate language normalizes away
        ("בערך שני שניצלים", 2.0, "", "", "שניצלים"),
        ("כ-3 קציצות", 3.0, "קציצה", "", ""),
    ],
)
def test_parse_quantity_expression_matrix(
    text: str, count: float, unit: str, size: str, food: str
) -> None:
    q = meal_intelligence.parse_quantity_expression(text)
    assert q is not None, text
    assert q.count == pytest.approx(count)
    assert q.unit_label == unit
    assert q.size == size
    assert q.food_text == food
    assert q.source == "user_count"


@pytest.mark.parametrize(
    "text",
    [
        "חצי מהאורז",  # of-item SCALE
        "חצי מנה",  # whole-meal SCALE
        "חצי",  # bare fraction = SCALE
        "x2",  # multiplier = SCALE
        "פי 2",  # multiplier = SCALE
        "השניצל בערך 180 גרם",  # gram lock
        "אורז 150 גרם",  # gram lock
        "3 גרם",  # grams
        "יש יותר שניצל",  # vague, no quantity
        "3",  # lone number, no food/unit
        "אחת",  # lone number word, no food/unit
        "בלי שמן",  # removal
        "לא פלאפל, שניצל",  # replacement
    ],
)
def test_parse_quantity_expression_declines_non_counts(text: str) -> None:
    assert meal_intelligence.parse_quantity_expression(text) is None


def test_count_correction_records_count_and_never_copies_it_as_grams() -> None:
    """The core invariant: '3 שניצלים' records count=3 and NEVER writes 3 into
    grams. Batch 5 may derive a plausible weight (schnitzel is a supported
    discrete food), but grams must never equal the raw count."""
    analysis = _one_item("שניצל", grams=120)
    correction = meal_intelligence.parse_meal_correction("3 שניצלים")[0]
    assert correction.kind == "count"
    result = meal_intelligence.apply_count_correction(analysis, correction)
    item = result.items[0]
    assert item.quantity_count == 3.0
    assert item.grams != 3  # never the raw count as grams (the incident shape)
    # Supported discrete food → Batch 5 materializes a plausible weight.
    assert item.grams == 450  # 3 × 150 g/schnitzel
    assert item.quantity_source == "count_derived"


def test_count_correction_on_unsupported_food_leaves_grams_untouched() -> None:
    """A count on a food with NO supported portion model records the count but
    never touches grams — the count is preserved as evidence for later."""
    analysis = _one_item("תבשיל מיוחד", grams=200)
    correction = meal_intelligence.parse_meal_correction("3 תבשיל מיוחד")[0]
    result = meal_intelligence.apply_count_correction(analysis, correction)
    item = result.items[0]
    assert item.grams == 200  # UNTOUCHED — no portion model
    assert item.calories == 400  # UNTOUCHED
    assert item.quantity_count == 3.0
    assert item.quantity_source == "user_count"  # not derived


def test_count_correction_records_portion_unit() -> None:
    analysis = _one_item("פלאפל", grams=100)
    correction = meal_intelligence.parse_meal_correction("3 כדורי פלאפל")[0]
    result = meal_intelligence.apply_count_correction(analysis, correction)
    item = result.items[0]
    assert item.quantity_count == 3.0
    assert item.quantity_unit == "כדור"
    # Falafel is supported → 3 balls × 18 g materializes (never 100 g/ball).
    assert item.grams == 54
    assert item.quantity_source == "count_derived"


def test_count_correction_matches_plural_to_singular_item() -> None:
    """'3 שניצלים' (plural) must find the singular 'שניצל' item."""
    analysis = _one_item("שניצל", grams=120)
    result = meal_intelligence.apply_count_correction(
        analysis, meal_intelligence.parse_meal_correction("3 שניצלים")[0]
    )
    assert result.items[0].quantity_count == 3.0


def test_count_correction_targets_only_the_named_item() -> None:
    analysis = MealAnalysis(
        meal_name="ארוחה",
        confidence=0.85,
        items=[
            FoodItem(name="שניצל", grams=120, calories=240, protein=12, carbs=24,
                     fat=6, confidence=0.85),
            FoodItem(name="אורז לבן", grams=150, calories=300, protein=15, carbs=30,
                     fat=7.5, confidence=0.85),
        ],
    )
    result = meal_intelligence.apply_count_correction(
        analysis, meal_intelligence.parse_meal_correction("3 שניצלים")[0]
    )
    schnitzel = next(i for i in result.items if "שניצל" in i.name)
    rice = next(i for i in result.items if "אורז" in i.name)
    assert schnitzel.quantity_count == 3.0
    assert rice.quantity_count is None  # untouched
    assert rice.grams == 150


def test_count_correction_ambiguous_target_records_note_and_no_mutation() -> None:
    """'שלוש כפות' with no food, on a multi-item meal, cannot pick a target —
    it records a note and mutates nothing (Batch 6 will clarify)."""
    analysis = MealAnalysis(
        meal_name="ארוחה",
        confidence=0.85,
        items=[
            FoodItem(name="טחינה", grams=60, calories=350, protein=10, carbs=12,
                     fat=30, confidence=0.85),
            FoodItem(name="אורז", grams=150, calories=200, protein=4, carbs=44,
                     fat=0.5, confidence=0.85),
        ],
    )
    result = meal_intelligence.apply_count_correction(
        analysis, meal_intelligence.parse_meal_correction("שלוש כפות")[0]
    )
    assert all(i.quantity_count is None for i in result.items)
    assert any("לא שויכה" in note for note in result.notes)


def test_count_correction_single_item_meal_accepts_unitless_count() -> None:
    """'אחד וחצי' with no food resolves against a single-item meal; a
    supported food (בורקס) materializes 1.5 × 100 g."""
    analysis = _one_item("בורקס", grams=90)
    result = meal_intelligence.apply_count_correction(
        analysis, meal_intelligence.parse_meal_correction("אחד וחצי")[0]
    )
    assert result.items[0].quantity_count == pytest.approx(1.5)
    assert result.items[0].grams == 150  # 1.5 × 100 g/בורקס
    assert result.items[0].grams != 1  # never the raw count


def test_count_never_copied_literally_as_grams_end_to_end() -> None:
    """Acceptance criterion: no count of N can surface as 'N גרם'. The parser
    yields kind='count' (never 'quantity'); Batch 5 may derive a plausible
    weight but grams must never equal the raw count."""
    for text in ["3 שניצלים", "שני שניצלים", "חצי שניצל", "שלוש חתיכות"]:
        corrections = meal_intelligence.parse_meal_correction(text)
        assert corrections and corrections[0].kind == "count"
    analysis = _one_item("שניצל", grams=120)
    meal_intelligence.apply_count_correction(
        analysis, meal_intelligence.parse_meal_correction("3 שניצלים")[0]
    )
    # Derived (schnitzel supported), and never the literal count as grams.
    assert analysis.items[0].grams == 450
    assert analysis.items[0].grams != 3


# --- Replay preservation: count survives an identity replacement -----------


def test_count_survives_identity_replacement_for_replay() -> None:
    """3 falafels → replace with schnitzel → still 3 schnitzels (not 1).

    quantity_count lives on the FoodItem, so the count survives the rename.
    Batch 5: the falafel-derived grams are invalidated on replacement (one
    schnitzel ≠ one falafel ball); re-materialization then re-derives from the
    schnitzel model."""
    analysis = _one_item("פלאפל", grams=120)
    # First: user states the count → materializes 3 × 18 g = 54 g (falafel).
    analysis = meal_intelligence.apply_count_correction(
        analysis, meal_intelligence.parse_meal_correction("3 פלאפל")[0]
    )
    assert analysis.items[0].quantity_count == 3.0
    assert analysis.items[0].grams == 54
    assert analysis.items[0].quantity_source == "count_derived"
    # Then: identity replacement (Batch 2/3 path).
    analysis = meal_intelligence.apply_item_replacement_correction(
        analysis, meal_intelligence.parse_meal_correction("לא פלאפל, שניצל")[0]
    )
    item = analysis.items[0]
    assert "שניצל" in item.name
    assert "פלאפל" not in item.name
    assert item.quantity_count == 3.0  # STILL three, not reset to one
    # Derived provenance invalidated so the falafel per-unit weight is dropped.
    assert item.quantity_source == "user_count"
    # Re-materialization now re-derives from the schnitzel model.
    meal_intelligence.materialize_count_quantity(item)
    assert item.grams == 450  # 3 × 150 g/schnitzel, not 3 × 18 g/falafel ball
    assert item.quantity_source == "count_derived"


# ---------------------------------------------------------------------------
# Batch 5 — count → grams materialization (materialize_count_quantity).
# Count is evidence; grams are a derived estimate, produced ONLY with a
# supported portion model, a plausible result, and evidence strong enough to
# override the current grams. Reuses israeli_foods.scaled_macros for macros
# and meal_plausibility as the final gate.
# ---------------------------------------------------------------------------


def _count_item(name: str, grams: float, count: float, unit: str | None = None,
                source: str = "user_count", calories: float | None = None) -> FoodItem:
    calories = calories if calories is not None else grams * 2
    return FoodItem(
        name=name, grams=grams, calories=calories, protein=grams * 0.1,
        carbs=grams * 0.2, fat=grams * 0.05, confidence=0.85,
        quantity_count=count, quantity_unit=unit, quantity_source=source,
    )


@pytest.mark.parametrize(
    ("name", "grams", "count", "unit", "expected_grams"),
    [
        ("שניצל", 120, 3, None, 450),        # 3 × 150
        ("פלאפל", 100, 4, None, 72),         # 4 × 18
        ("קציצה", 90, 2, None, 90),          # 2 × 45
        ("פיתה", 60, 0.5, None, 30),         # half × 60
        ("שניצל", 150, 1.5, None, 225),      # 1.5 × 150
        ("אורז לבן", 150, 0.5, "כוס", 79),   # half × 158 (cooked rice cup)
        ("לחם", 50, 2, "פרוסה", 50),         # 2 × 25 (bread slice)
    ],
)
def test_materialize_supported_conversions(name, grams, count, unit, expected_grams) -> None:
    item = _count_item(name, grams, count, unit)
    assert meal_intelligence.materialize_count_quantity(item) is True
    assert item.grams == expected_grams
    assert item.quantity_source == "count_derived"
    assert item.grams != count  # never the raw count as grams


@pytest.mark.parametrize(
    ("name", "grams", "count", "unit"),
    [
        ("תבשיל מיוחד", 200, 3, None),   # unknown discrete food
        ("אורז לבן", 150, 0.5, None),    # mass food, no unit → unsupported
        ("אורז לבן", 2, 2, "יחידה"),     # rice has no discrete-unit weight
        ("שניצל", 150, 1, "כוס"),        # a "cup of schnitzel" is nonsense
        ("שניצל", 120, 100, None),       # absurd count
    ],
)
def test_materialize_declines_unsupported_or_unsafe(name, grams, count, unit) -> None:
    item = _count_item(name, grams, count, unit)
    before = (item.grams, item.calories)
    assert meal_intelligence.materialize_count_quantity(item) is False
    assert (item.grams, item.calories) == before  # grams UNTOUCHED
    assert item.quantity_count == count  # count evidence preserved


def test_materialize_never_overwrites_explicit_user_grams() -> None:
    item = _count_item("שניצל", 180, 3, source="user")  # user-pinned grams
    assert meal_intelligence.materialize_count_quantity(item) is False
    assert item.grams == 180  # explicit user grams win over a count estimate


def test_materialize_macros_stay_coherent_and_scale() -> None:
    """Curated food: macros recomputed from per-100g at the derived grams."""
    item = _count_item("שניצל", 120, 3, calories=240)
    meal_intelligence.materialize_count_quantity(item)
    # israeli_foods שניצל = 280 kcal/100g → 450 g = 1260 kcal.
    assert item.grams == 450
    assert item.calories == pytest.approx(1260, abs=1)
    # No stale macros: recompute macro-kcal roughly matches calories field.
    macro_kcal = item.protein * 4 + item.carbs * 4 + item.fat * 9
    assert abs(macro_kcal - item.calories) <= max(150, item.calories * 0.35)


def test_materialize_is_idempotent() -> None:
    item = _count_item("שניצל", 120, 3)
    assert meal_intelligence.materialize_count_quantity(item) is True
    snapshot = (item.grams, item.calories, item.protein, item.quantity_source)
    for _ in range(3):
        # Already derived to this count → no-op, no compounding.
        assert meal_intelligence.materialize_count_quantity(item) is False
        assert (item.grams, item.calories, item.protein, item.quantity_source) == snapshot


def test_materialize_recorrection_rederives_from_per_unit_not_result() -> None:
    """Re-stating a new count re-derives from the per-unit weight, never from
    the already-derived grams (no compounding)."""
    item = _count_item("שניצל", 120, 3)
    meal_intelligence.materialize_count_quantity(item)
    assert item.grams == 450
    # User re-corrects to 4 (via apply_count_correction on a fresh analysis).
    analysis = MealAnalysis(meal_name="x", confidence=0.85, items=[item])
    meal_intelligence.apply_count_correction(
        analysis, meal_intelligence.parse_meal_correction("4 שניצלים")[0]
    )
    assert item.grams == 600  # 4 × 150, not 450 × 4/3 or 450 × 4


def test_materialize_declines_on_missing_count() -> None:
    item = _count_item("שניצל", 120, 3)
    item.quantity_count = None  # no count evidence to materialize from
    assert meal_intelligence.materialize_count_quantity(item) is False
    assert item.grams == 120


def test_count_derived_survives_serialization_round_trip() -> None:
    import json

    item = _count_item("שניצל", 120, 3)
    meal_intelligence.materialize_count_quantity(item)
    analysis = MealAnalysis(meal_name="שניצל", confidence=0.85, items=[item])
    reloaded = MealAnalysis.model_validate(json.loads(json.dumps(analysis.model_dump())))
    it = reloaded.items[0]
    assert it.grams == 450
    assert it.quantity_count == 3.0
    assert it.quantity_source == "count_derived"
    # Re-materializing the reloaded item is a no-op (deterministic replay).
    assert meal_intelligence.materialize_count_quantity(it) is False
    assert it.grams == 450


def test_replacement_invalidates_derived_grams_then_rederives() -> None:
    analysis = MealAnalysis(meal_name="פלאפל", confidence=0.85,
                            items=[_count_item("פלאפל", 100, 3)])
    meal_intelligence.materialize_count_quantity(analysis.items[0])
    assert analysis.items[0].grams == 54  # 3 × 18 (falafel ball)
    analysis = meal_intelligence.apply_item_replacement_correction(
        analysis, meal_intelligence.parse_meal_correction("לא פלאפל, שניצל")[0]
    )
    it = analysis.items[0]
    assert it.quantity_source == "user_count"  # derived provenance invalidated
    meal_intelligence.materialize_count_quantity(it)
    assert it.grams == 450  # re-derived from schnitzel, not carried 54 g


def test_legacy_item_without_quantity_fields_is_unaffected() -> None:
    item = FoodItem(name="אורז", grams=150, calories=200, protein=4, carbs=44,
                    fat=1, confidence=0.8)
    assert meal_intelligence.materialize_count_quantity(item) is False
    assert item.grams == 150 and item.quantity_source is None
