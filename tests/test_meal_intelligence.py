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
#      remove/add parsing group (now regular passing tests); the count
#      quantity group remains xfail until Batch 4. An unexpected pass fails
#      the suite (strict).
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
