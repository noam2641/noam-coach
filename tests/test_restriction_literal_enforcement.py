"""A dietary restriction must be enforceable against its own name.

Restriction matching resolved only through `RESTRICTION_ALIASES` -- an
85-entry table. A restriction the table does not recognise keeps the user's
own wording as its `canonical_id`, and that literal is not an alias, so it
could **never match its own name**.

Measured against the live profile from the 2026-07-27 session
("חציל, טורטייה ואגוזים"):

    validate_meal_restrictions('ראפ טורטייה עם עוף') -> []      CLEAN
    validate_meal_restrictions('חציל בתנור')          -> []      CLEAN
    validate_meal_restrictions('סלט עם אגוזי מלך')    -> [warn]  caught

Two of the user's three restrictions were stored, displayed, counted and sent
to the AI while being structurally invisible to the deterministic firewall.
Only nuts was caught, because it happens to be in the table. This is the
mechanism behind the tortilla recommendation.

The matching below is deliberately conservative: stems, not substrings, and
one prefix plus one suffix stripped at most. A false block on food is worse
than a miss the AI layer still sees, so over-stemming is the failure mode to
avoid.
"""

from __future__ import annotations

import pytest

from noam_coach.services import dietary_restrictions as dr

#: The exact value stored in the live database.
LIVE_RESTRICTIONS = "חציל, טורטייה ואגוזים"


@pytest.fixture
def live() -> list:
    return dr.load_restrictions_from_facts(LIVE_RESTRICTIONS, "none")


def _hits(name: str, restrictions: list) -> list[str]:
    violations = dr.validate_meal_restrictions(
        [{"name": name, "grams": 200}], restrictions
    )
    return [v["restriction"].canonical_id for v in violations]


def test_the_three_live_restrictions_all_parse(live: list) -> None:
    ids = {r.canonical_id for r in live}
    assert "חציל" in ids
    assert "טורטייה" in ids
    assert "tree_nuts" in ids


@pytest.mark.parametrize(
    ("meal", "expected"),
    [
        ("ראפ טורטייה עם עוף", "טורטייה"),
        ("חציל בתנור", "חציל"),
        ("סלט עם אגוזי מלך", "tree_nuts"),
        # plural of a feminine noun -- the singular restriction must still match
        ("טורטיות מקמח מלא", "טורטייה"),
        ("חצילים בגריל", "חציל"),
        # a single attached particle
        ("בטורטייה עם טונה", "טורטייה"),
    ],
)
def test_every_stored_restriction_is_enforceable(
    meal: str, expected: str, live: list
) -> None:
    """The regression: unmapped restrictions passed clean."""
    assert expected in _hits(meal, live), f"{meal!r} must violate {expected!r}"


@pytest.mark.parametrize(
    "meal",
    [
        "חזה עוף ואורז",
        "סלמון בתנור",
        "ביצה קשה",
        "יוגורט",
        "טוסט גבינה",
        "טונה",
        "חזה הודו",
    ],
)
def test_unrelated_foods_are_not_blocked(meal: str, live: list) -> None:
    """Over-stemming would be worse than the original bug."""
    assert _hits(meal, live) == [], f"{meal!r} must not violate anything"


def test_safe_compounds_still_suppress(live: list) -> None:
    """Nutmeg must not trigger tree_nuts -- SAFE_COMPOUNDS still governs."""
    assert _hits("אגוז מוסקט בתבשיל", live) == []


def test_canonical_slugs_do_not_take_the_literal_path() -> None:
    """A recognised slug is handled by the alias table, not by text matching.

    Otherwise 'tree_nuts' would be compared as English words against Hebrew
    item names, which is meaningless and could only produce noise.
    """
    assert "tree_nuts" in dr._CANONICAL_IDS
    assert "חציל" not in dr._CANONICAL_IDS


def test_canonical_id_set_is_derived_from_the_alias_table() -> None:
    """Hardcoding it would drift the moment an alias is added."""
    assert dr._CANONICAL_IDS == frozenset(dr.RESTRICTION_ALIASES.values())


@pytest.mark.parametrize(
    ("word", "stem"),
    [
        ("טורטייה", "טורט"),
        ("טורטיות", "טורט"),
        ("בטורטייה", "טורט"),
        ("חציל", "חציל"),
        ("חצילים", "חציל"),
        ("והחציל", "החציל"),  # only ONE prefix is stripped, by design
    ],
)
def test_stemming_is_conservative(word: str, stem: str) -> None:
    assert dr._hebrew_stem(word) == stem


def test_short_words_are_not_stemmed_away() -> None:
    """Stripping must never leave a stem too short to be distinctive."""
    for word in ("ביצה", "מים", "לחם"):
        assert len(dr._hebrew_stem(word)) >= 3


def test_an_empty_or_missing_item_name_is_safe(live: list) -> None:
    assert dr.validate_meal_restrictions([{"name": "", "grams": 0}], live) == []
    assert dr.validate_meal_restrictions([], live) == []
