"""Targeted unit tests for REC-PROGRAM-04 regression coverage.

Each class targets a specific domain module.  Tests are synchronous
wherever possible; async tests are declared with ``async def`` so that
pytest-asyncio (mode=auto) provides a consistent event loop.

Database-backed tests receive a ``tmp_path`` fixture and create a
file-backed SQLite database — :memory: databases cannot be shared across
multiple aiosqlite connections.

No Telegram or external-service imports are used.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import meal_intelligence
import user_model
from db import Database
from helpers import utc_now
from models import FoodItem, MealAnalysis
from noam_coach.services.availability import (
    TrainingAvailability,
    format_availability_summary,
    resolve_availability,
)
from noam_coach.services.dietary_restrictions import (
    DietaryRestriction,
    merge_restrictions,
    normalize_restriction,
    parse_restrictions,
    validate_meal_restrictions,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _make_db(tmp_path: Path, name: str = "test.db") -> Database:
    """Create and initialise a file-backed test database."""
    db = Database(str(tmp_path / name))
    await db.init()
    return db


async def _add_user(db: Database, user_id: int = 1) -> None:
    await db.execute(
        "INSERT OR IGNORE INTO users(id, first_name, username, updated_at) "
        "VALUES(?, 'Test', NULL, ?)",
        (user_id, utc_now()),
    )


async def _set_fact(db: Database, user_id: int, key: str, value: Any,
                    *, confirmed: bool = True) -> None:
    await user_model.set_fact(
        db, user_id, key, value,
        source=user_model.SOURCE_USER,
        confirmed=confirmed,
    )


def _allergy(canonical_id: str, label: str) -> DietaryRestriction:
    return DietaryRestriction(
        canonical_id=canonical_id,
        user_label=label,
        original_input=label,
        restriction_type="allergy",
        severity="critical",
        confirmed=True,
        source="test",
    )


def _preference(canonical_id: str, label: str) -> DietaryRestriction:
    return DietaryRestriction(
        canonical_id=canonical_id,
        user_label=label,
        original_input=label,
        restriction_type="preference",
        severity="low",
        confirmed=True,
        source="test",
    )


# ===========================================================================
# TestDietaryRestrictionModel
# ===========================================================================

class TestDietaryRestrictionModel:
    """Unit tests for dietary restriction normalisation, parsing, and merging."""

    def test_normalize_nuts_hebrew(self):
        """'אגוזים' must normalise to 'tree_nuts'."""
        assert normalize_restriction("אגוזים") == "tree_nuts"

    def test_normalize_peanuts_english(self):
        """'peanuts' must normalise to 'peanuts' (separate from tree_nuts)."""
        result = normalize_restriction("peanuts")
        assert result == "peanuts"
        assert result != "tree_nuts"

    def test_parse_comma_separated(self):
        """'בוטנים, אגוזים' must produce two separate restrictions."""
        restrictions = parse_restrictions(
            "בוטנים, אגוזים",
            restriction_type="allergy",
            severity="critical",
            confirmed=True,
        )
        canonical_ids = {r.canonical_id for r in restrictions}
        assert "peanuts" in canonical_ids
        assert "tree_nuts" in canonical_ids
        assert len(restrictions) == 2

    def test_merge_no_duplicates(self):
        """Merging the same restriction twice must not produce duplicates."""
        r = parse_restrictions("אגוזים", restriction_type="allergy",
                               severity="critical", confirmed=True)
        merged_once = merge_restrictions([], r)
        merged_twice = merge_restrictions(merged_once, r)
        tree_nut_count = sum(1 for x in merged_twice if x.canonical_id == "tree_nuts")
        assert tree_nut_count == 1, (
            f"Expected exactly one tree_nuts entry, got {tree_nut_count}"
        )

    def test_merge_updates_type(self):
        """Merging a preference over an avoidance must upgrade to the new type."""
        avoidance = DietaryRestriction(
            canonical_id="dairy",
            user_label="חלב",
            original_input="חלב",
            restriction_type="avoidance",
            severity="medium",
            confirmed=True,
            source="test",
        )
        allergy = DietaryRestriction(
            canonical_id="dairy",
            user_label="חלב",
            original_input="חלב",
            restriction_type="allergy",
            severity="critical",
            confirmed=True,
            source="test",
        )
        merged = merge_restrictions([avoidance], [allergy])
        dairy = next(r for r in merged if r.canonical_id == "dairy")
        assert dairy.restriction_type == "allergy"
        assert dairy.severity == "critical"

    def test_firewall_blocks_allergy(self):
        """An allergy restriction must produce a 'block' action."""
        nut_allergy = _allergy("tree_nuts", "אגוזים")
        items = [{"item_name": "גרנולה עם אגוזים ודבש"}]
        violations = validate_meal_restrictions(items, [nut_allergy])
        assert violations, "Expected a violation"
        assert violations[0]["action"] == "block"

    def test_firewall_warns_preference(self):
        """A preference restriction must produce a 'warn' action (not block)."""
        pref = _preference("dairy", "חלב")
        items = [{"item_name": "גבינה לבנה"}]
        violations = validate_meal_restrictions(items, [pref])
        assert violations, "Expected a violation"
        assert violations[0]["action"] == "warn"

    def test_firewall_catches_nut_group(self):
        """A tree_nuts allergy must catch items containing 'אגוזי מלך' (walnuts)."""
        nut_allergy = _allergy("tree_nuts", "אגוזים")
        items = [{"item_name": "עוגת אגוזי מלך"}]
        violations = validate_meal_restrictions(items, [nut_allergy])
        assert violations, (
            "tree_nuts allergy must catch 'אגוזי מלך' — both are tree_nuts"
        )
        assert violations[0]["action"] == "block"


# ===========================================================================
# TestAvailabilityResolver
# ===========================================================================

class TestAvailabilityResolver:
    """Unit tests for training availability resolution and formatting."""

    async def test_default_availability(self, tmp_path: Path):
        """When no facts are set, defaults must be returned."""
        db = await _make_db(tmp_path, "avail_default.db")
        await _add_user(db)
        avail = await resolve_availability(db, 1)
        # The defaults are 3 days, 45 min
        assert avail.max_days_per_week >= 1
        assert avail.session_minutes >= 10
        assert avail.source in (
            "default",
            "legacy",
            "fresh_health_inference",
            "confirmed_health_inference",
            "user_reported",
            "user_confirmed",
            "user_corrected",
        )

    async def test_explicit_overrides_inferred(self, tmp_path: Path):
        """A confirmed training_days_per_week must override an inferred estimate."""
        db = await _make_db(tmp_path, "avail_override.db")
        await _add_user(db)
        # Confirmed user value
        await user_model.set_fact(
            db, 1, "training_days_per_week", 4,
            source=user_model.SOURCE_USER,
            confirmed=True,
        )
        # Inferred Apple Health data
        await user_model.set_fact(
            db, 1, "workout_pattern",
            {"weekly_frequency": 6, "common_weekdays": [0, 1, 2, 3, 4, 5],
             "typical_hour": "07:00", "avg_duration_minutes": 45},
            kind=user_model.KIND_ESTIMATE,
            source=user_model.SOURCE_APPLE_HEALTH,
            confirmed=False,
        )
        avail = await resolve_availability(db, 1)
        assert avail.max_days_per_week == 4, (
            f"Confirmed 4 must override inferred 6, got {avail.max_days_per_week}"
        )

    def test_format_summary(self):
        """format_availability_summary must include days count and at least one day name."""
        avail = TrainingAvailability(
            max_days_per_week=3,
            preferred_days=[0, 2, 4],
            preferred_time="07:00",
            session_minutes=45,
            source="user_confirmed",
            confidence=0.9,
            confirmed=True,
        )
        summary = format_availability_summary(avail)
        assert "3" in summary
        # Monday-first convention: 0=Monday, 2=Wednesday, 4=Friday.
        day_names = ["שני", "רביעי", "שישי"]
        assert any(name in summary for name in day_names), (
            f"Summary must mention at least one day name: {summary!r}"
        )


# ===========================================================================
# TestMealCorrection
# ===========================================================================

class TestMealCorrection:
    """Unit tests for meal correction parsing and application."""

    def test_replace_pattern_parsed(self):
        """'שניצל רגיל לא טופו' must parse to a replace correction."""
        corrections = meal_intelligence.parse_meal_correction("שניצל רגיל לא טופו")
        kinds = [c.kind for c in corrections]
        assert "replace" in kinds, (
            f"Expected 'replace' correction, got: {corrections}"
        )
        rc = next(c for c in corrections if c.kind == "replace")
        assert "שניצל" in rc.value
        assert "טופו" in rc.item_hint

    def test_remove_pattern_parsed(self):
        """'בלי ברוקולי' must parse to a remove correction."""
        corrections = meal_intelligence.parse_meal_correction("בלי ברוקולי")
        remove = [c for c in corrections if c.kind == "remove"]
        assert remove, f"Expected remove correction, got: {corrections}"
        assert "ברוקולי" in remove[0].item_hint

    def test_replace_only_target_changed(self):
        """Replacement must only change the targeted item; others must be intact."""
        rice = FoodItem(name="אורז מבושל", grams=120, calories=156,
                        protein=3, carbs=34, fat=0.4, confidence=0.9)
        tofu = FoodItem(name="טופו", grams=150, calories=114,
                        protein=12, carbs=3, fat=6, confidence=0.9)
        veg = FoodItem(name="ירקות", grams=100, calories=40,
                       protein=2, carbs=8, fat=0.5, confidence=0.9)
        analysis = MealAnalysis(meal_name="test", confidence=0.9,
                                items=[rice, tofu, veg])
        correction = meal_intelligence.MealCorrection(
            kind="replace", item_hint="טופו", value="שניצל",
            original_text="שניצל לא טופו",
        )
        result = meal_intelligence.apply_item_replacement_correction(analysis, correction)
        names = [i.name for i in result.items]
        # Other items must remain
        assert any("אורז" in n for n in names), "Rice must not be removed"
        assert any("ירקות" in n for n in names), "Vegetables must not be removed"
        # The tofu entry must be replaced
        assert not any(n == "טופו" for n in names), "Original 'טופו' must be replaced"

    def test_oil_preserved_after_replace(self):
        """Replacing chicken with salmon must not remove a separate oil item."""
        oil = FoodItem(name="שמן זית", grams=10, calories=88,
                       protein=0, carbs=0, fat=10, confidence=0.9)
        chicken = FoodItem(name="עוף צלוי", grams=150, calories=248,
                           protein=37, carbs=0, fat=11, confidence=0.9)
        analysis = MealAnalysis(meal_name="test", confidence=0.9,
                                items=[chicken, oil])
        correction = meal_intelligence.MealCorrection(
            kind="replace", item_hint="עוף", value="סלמון",
            original_text="סלמון לא עוף",
        )
        result = meal_intelligence.apply_item_replacement_correction(analysis, correction)
        result_names = [i.name for i in result.items]
        assert any("שמן" in n for n in result_names), (
            "Oil item must be preserved when only chicken is replaced"
        )


# ===========================================================================
# TestBodyFatNormalization
# ===========================================================================

class TestBodyFatNormalization:
    """Unit tests for body_fat_pct display normalization."""

    def test_fraction_to_percent(self):
        """A fraction value like 0.20 must be displayed as '20.0%'."""
        result = user_model.display_value("body_fat_pct", 0.20)
        assert "20.0%" in result, (
            f"0.20 (fraction) must display as '20.0%', got {result!r}"
        )

    def test_percent_passthrough(self):
        """An already-percent value like 20 must be displayed as '20.0%'."""
        result = user_model.display_value("body_fat_pct", 20)
        assert "20.0%" in result, (
            f"20 (percent) must display as '20.0%', got {result!r}"
        )

    def test_implausible_value(self):
        """An implausible value like 150 must show 'דורש אימות'."""
        result = user_model.display_value("body_fat_pct", 150)
        assert result == "דורש אימות", f"Implausible 150 must show 'דורש אימות', got {result!r}"

    def test_zero_body_fat_ambiguous(self):
        """Zero body fat is ambiguous — could be missing data."""
        result = user_model.display_value("body_fat_pct", 0)
        assert result == "דורש אימות", f"Zero body fat should be ambiguous, got {result!r}"

    def test_one_body_fat_ambiguous(self):
        """1.0 is ambiguous — could be 1% or 100% fraction."""
        result = user_model.display_value("body_fat_pct", 1.0)
        assert result == "דורש אימות", f"1.0 body fat should be ambiguous, got {result!r}"

    def test_negative_body_fat(self):
        """Negative values must show 'דורש אימות'."""
        result = user_model.display_value("body_fat_pct", -5)
        assert result == "דורש אימות", f"Negative body fat must be implausible, got {result!r}"

    def test_nan_body_fat(self):
        """NaN body fat must show 'דורש אימות'."""
        result = user_model.display_value("body_fat_pct", float("nan"))
        assert result == "דורש אימות", f"NaN body fat must be implausible, got {result!r}"

    def test_inf_body_fat(self):
        """Infinity body fat must show 'דורש אימות'."""
        result = user_model.display_value("body_fat_pct", float("inf"))
        assert result == "דורש אימות", f"Inf body fat must be implausible, got {result!r}"

    def test_fraction_with_explicit_unit(self):
        """Centralized normalizer: 0.20 with fraction unit → 20%."""
        from noam_coach.services.body_fat import display_body_fat, normalize_body_fat
        result = normalize_body_fat(0.20, source_unit="fraction")
        assert result.status == "valid"
        assert result.normalized_pct == 20.0
        assert result.confidence >= 0.9
        assert display_body_fat(result) == "20.0%"

    def test_percent_with_explicit_unit(self):
        """Centralized normalizer: 20 with percent unit → 20%."""
        from noam_coach.services.body_fat import display_body_fat, normalize_body_fat
        result = normalize_body_fat(20, source_unit="percent")
        assert result.status == "valid"
        assert result.normalized_pct == 20.0
        assert display_body_fat(result) == "20.0%"

    def test_fraction_as_percent_unit(self):
        """0.20 with percent unit → 0.2% which is implausible."""
        from noam_coach.services.body_fat import normalize_body_fat
        result = normalize_body_fat(0.20, source_unit="percent")
        assert result.status == "implausible"

    def test_apple_health_source_uses_fraction(self):
        """Apple Health source type defaults to fraction unit."""
        from noam_coach.services.body_fat import display_body_fat, normalize_body_fat
        result = normalize_body_fat(0.22, source_type="apple_health")
        assert result.normalized_pct == 22.0
        assert result.status == "valid"
        assert display_body_fat(result) == "22.0%"

    def test_hebrew_output_not_reversed(self):
        """Hebrew output strings must be normal UTF-8, not reversed."""
        from noam_coach.services.body_fat import display_body_fat, normalize_body_fat
        # Ambiguous case
        result = normalize_body_fat(0)
        text = display_body_fat(result)
        assert text == "דורש אימות"
        # Verify first character is dalet
        assert text[0] == "ד", f"First char must be dalet, got {repr(text[0])}"
        # Missing case
        result2 = normalize_body_fat(None)
        text2 = display_body_fat(result2)
        assert text2 == "לא צוין"
        assert text2[0] == "ל", f"First char must be lamed, got {repr(text2[0])}"


# ===========================================================================
# TestMealTotalConsistency
# ===========================================================================

class TestMealTotalConsistency:
    """Unit tests for MealAnalysis.totals() correctness."""

    def test_totals_match_items(self):
        """totals() must equal the arithmetic sum of item values."""
        items = [
            FoodItem(name="a", grams=100, calories=200, protein=20,
                     carbs=30, fat=5, confidence=0.9),
            FoodItem(name="b", grams=50, calories=100, protein=10,
                     carbs=15, fat=3, confidence=0.9),
        ]
        analysis = MealAnalysis(meal_name="test", confidence=0.9, items=items)
        t = analysis.totals()
        assert t["calories"] == 300
        assert t["protein"] == 30
        assert t["carbs"] == 45
        assert t["fat"] == 8

    def test_no_nan_in_totals(self):
        """totals() must not produce NaN or inf values."""
        import math
        items = [
            FoodItem(name="x", grams=0, calories=0, protein=0,
                     carbs=0, fat=0, confidence=0.9),
        ]
        analysis = MealAnalysis(meal_name="empty", confidence=0.9, items=items)
        t = analysis.totals()
        for key, val in t.items():
            assert not math.isnan(val), f"totals()['{key}'] is NaN"
            assert not math.isinf(val), f"totals()['{key}'] is infinite"

    def test_negative_rejected(self):
        """FoodItem must reject negative calorie values at model construction."""
        import pydantic
        with pytest.raises((pydantic.ValidationError, ValueError)):
            FoodItem(name="negative_cal", grams=50, calories=-50,
                     protein=5, carbs=10, fat=2, confidence=0.5)


# ===========================================================================
# TestProfilePresentation
# ===========================================================================

class TestProfilePresentation:
    """Unit tests for user-facing profile value display."""

    def test_gap_not_displayed_raw(self):
        """A gap dict with 'missing' and 'why_matters' keys must not appear raw."""
        gap_value = {"missing": True, "why_matters": "חשוב לדיוק הדיאטה"}
        result = user_model.display_value("diet_restrictions", gap_value)
        assert "missing" not in result, (
            f"Raw key 'missing' must not appear in profile output: {result!r}"
        )
        assert "why_matters" not in result, (
            f"Raw key 'why_matters' must not appear in profile output: {result!r}"
        )

    def test_enum_displayed_hebrew(self):
        """Known enum values must be shown in Hebrew, not as snake_case strings."""
        from noam_coach.bot.onboarding import _format_fact_value
        result = _format_fact_value("primary_goal", "fat_loss_muscle_retention")
        assert "fat_loss" not in result, (
            f"Snake-case enum value must not appear in output: {result!r}"
        )
        # Must contain a Hebrew word
        has_hebrew = any("\u0590" <= ch <= "\u05FF" for ch in result)
        assert has_hebrew, (
            f"Display value must include Hebrew characters, got: {result!r}"
        )

    def test_missing_value_hebrew_not_reversed(self):
        """'לא צוין' must be normal Hebrew (starts with lamed)."""
        result = user_model.display_value("weight_kg", None)
        assert result == "לא צוין"
        assert result[0] == "ל", f"First char must be lamed, got {repr(result[0])}"

    def test_availability_hebrew_not_reversed(self):
        """Availability summary Hebrew must be normal (not reversed)."""
        from noam_coach.services.availability import (
            TrainingAvailability,
            format_availability_summary,
        )
        a = TrainingAvailability(
            max_days_per_week=3, preferred_days=[0, 2, 4],
            preferred_time=None, session_minutes=45,
            source="default", confidence=0.5, confirmed=False,
        )
        text = format_availability_summary(a)
        # Must contain Hebrew words in normal order
        assert "אימונים" in text, "Missing 'אימונים' in availability summary"
        assert "דקות" in text, "Missing 'דקות' in availability summary"

    def test_restriction_warning_hebrew_not_reversed(self):
        """Dietary restriction canonical IDs contain no reversed Hebrew."""
        from noam_coach.services.dietary_restrictions import normalize_restriction
        # These are canonical IDs (English), so they should not contain Hebrew
        cid = normalize_restriction("אגוזים")
        assert cid == "tree_nuts", f"Expected tree_nuts, got {cid!r}"


# ===========================================================================
# TestDietaryAliasBoundary
# ===========================================================================

class TestDietaryAliasBoundary:
    """Boundary tests for dietary-restriction alias matching in the firewall.

    These tests verify that ``validate_meal_restrictions`` avoids both false
    positives (blocking safe foods) and false negatives (missing dangerous
    foods).

    The ``SAFE_COMPOUNDS`` allowlist in ``dietary_restrictions.py`` prevents
    false positives for known compound phrases:

    * 'אגוז מוסקט' (nutmeg) — suppresses tree_nuts
    * 'חלב קוקוס' (coconut milk) — suppresses dairy
    """

    # ------------------------------------------------------------------
    # False-positive boundary: foods that must NOT be caught
    # ------------------------------------------------------------------

    def test_coconut_not_tree_nuts(self):
        """'קוקוס' (coconut) must NOT be caught by a tree_nuts allergy.

        Coconut is botanically a drupe (fruit), not a tree nut.  'קוקוס'
        is absent from RESTRICTION_ALIASES so no violation must occur.
        """
        tree_nut_allergy = _allergy("tree_nuts", "אגוזים")
        items = [{"item_name": "קוקוס טרי"}]
        violations = validate_meal_restrictions(items, [tree_nut_allergy])
        assert violations == [], (
            "קוקוס (coconut) must not trigger a tree_nuts violation — "
            "coconut is a drupe, not a tree nut"
        )

    def test_nutmeg_not_tree_nuts(self):
        """'אגוז מוסקט' (nutmeg) must NOT be caught by a tree_nuts allergy.

        Nutmeg is a seed, not a tree nut.  The SAFE_COMPOUNDS allowlist
        suppresses tree_nuts for items containing 'אגוז מוסקט'.
        """
        tree_nut_allergy = _allergy("tree_nuts", "אגוזים")
        items = [{"item_name": "אגוז מוסקט טחון"}]
        violations = validate_meal_restrictions(items, [tree_nut_allergy])
        assert violations == [], (
            "אגוז מוסקט (nutmeg) must not trigger a tree_nuts violation — "
            "nutmeg is a seed, not a tree nut"
        )

    # ------------------------------------------------------------------
    # True-positive boundary: foods that MUST be caught
    # ------------------------------------------------------------------

    def test_peanut_butter_caught(self):
        """'חמאת בוטנים' (peanut butter) MUST be caught by a peanuts allergy.

        'בוטנים' is in RESTRICTION_ALIASES as peanuts.  The firewall must
        detect it inside a compound item name and return a 'block' action.
        """
        peanut_allergy = _allergy("peanuts", "בוטנים")
        items = [{"item_name": "חמאת בוטנים טבעית"}]
        violations = validate_meal_restrictions(items, [peanut_allergy])
        assert violations, "חמאת בוטנים must trigger a peanuts violation"
        assert violations[0]["action"] == "block"

    def test_almond_milk_caught(self):
        """'חלב שקדים' (almond milk) MUST be caught by a tree_nuts allergy.

        'שקדים' maps to tree_nuts in RESTRICTION_ALIASES.  Even inside a
        compound item name the firewall must catch it and return 'block'.
        """
        tree_nut_allergy = _allergy("tree_nuts", "אגוזים")
        items = [{"item_name": "חלב שקדים לא ממותק"}]
        violations = validate_meal_restrictions(items, [tree_nut_allergy])
        assert violations, "חלב שקדים must trigger a tree_nuts violation"
        assert violations[0]["action"] == "block"

    # ------------------------------------------------------------------
    # False-positive boundary: plant-based dairy lookalikes
    # ------------------------------------------------------------------

    def test_coconut_milk_not_dairy(self):
        """'חלב קוקוס' (coconut milk) must NOT be caught by a dairy allergy.

        Coconut milk is plant-based and contains no animal dairy.  The
        SAFE_COMPOUNDS allowlist suppresses dairy for 'חלב קוקוס'.
        """
        dairy_allergy = _allergy("dairy", "חלב")
        items = [{"item_name": "חלב קוקוס"}]
        violations = validate_meal_restrictions(items, [dairy_allergy])
        assert violations == [], (
            "חלב קוקוס (coconut milk) is plant-based and must not trigger "
            "a dairy violation"
        )

    # ------------------------------------------------------------------
    # Item dict key fallback: 'name' instead of 'item_name'
    # ------------------------------------------------------------------

    def test_item_name_fallback(self):
        """Items with a 'name' key (not 'item_name') must still be checked.

        The firewall falls back to ``item.get('name', '')`` when
        ``item_name`` is absent.  Items delivered via this path must never
        be silently skipped.
        """
        nut_allergy = _allergy("tree_nuts", "אגוזים")
        # Deliberately use 'name' instead of 'item_name'
        items = [{"name": "גרנולה עם אגוזים"}]
        violations = validate_meal_restrictions(items, [nut_allergy])
        assert violations, (
            "Item with 'name' key containing 'אגוזים' must trigger a "
            "tree_nuts violation — 'name' fallback must be checked"
        )
        assert violations[0]["action"] == "block"

    # ------------------------------------------------------------------
    # Substring / word-boundary: lone tokens must not over-match
    # ------------------------------------------------------------------

    def test_embedded_word_boundary(self):
        """'מוסקט' alone must not be caught by a tree_nuts allergy.

        The bare word 'מוסקט' is not in RESTRICTION_ALIASES.  Only items
        that contain a recognised alias token (e.g. 'אגוז') should be
        flagged.  Pure suffix / substring matches must not occur.
        """
        tree_nut_allergy = _allergy("tree_nuts", "אגוזים")
        items = [{"item_name": "תבלין מוסקט"}]
        violations = validate_meal_restrictions(items, [tree_nut_allergy])
        assert violations == [], (
            "'מוסקט' alone must not trigger a tree_nuts violation — "
            "it is not listed in RESTRICTION_ALIASES"
        )

    # ------------------------------------------------------------------
    # Alias coverage: specific tokens from checkpoint requirements
    # ------------------------------------------------------------------

    def test_cashew_caught(self):
        """'קשיו' (cashew) must be caught by a tree_nuts allergy."""
        tree_nut_allergy = _allergy("tree_nuts", "אגוזים")
        items = [{"item_name": "קשיו קלוי"}]
        violations = validate_meal_restrictions(items, [tree_nut_allergy])
        assert violations, "קשיו (cashew) must trigger a tree_nuts violation"
        assert violations[0]["action"] == "block"

    def test_english_nuts_caught(self):
        """English alias 'nuts' must be caught by a tree_nuts allergy."""
        tree_nut_allergy = _allergy("tree_nuts", "אגוזים")
        items = [{"item_name": "mixed nuts snack"}]
        violations = validate_meal_restrictions(items, [tree_nut_allergy])
        assert violations, "'nuts' (English) must trigger a tree_nuts violation"

    def test_english_peanut_caught(self):
        """English alias 'peanut' must be caught by a peanuts allergy."""
        peanut_allergy = _allergy("peanuts", "בוטנים")
        items = [{"item_name": "peanut butter sandwich"}]
        violations = validate_meal_restrictions(items, [peanut_allergy])
        assert violations, "'peanut' (English) must trigger a peanuts violation"

    def test_english_milk_caught(self):
        """English alias 'milk' must be caught by a dairy allergy."""
        dairy_allergy = _allergy("dairy", "חלב")
        items = [{"item_name": "whole milk"}]
        violations = validate_meal_restrictions(items, [dairy_allergy])
        assert violations, "'milk' (English) must trigger a dairy violation"

    def test_chalavi_not_in_aliases(self):
        """'חלבי' (adjective: milky/dairy) is not a standalone alias.

        The word 'חלבי' is a Hebrew adjective meaning 'dairy-based'.
        It is NOT in RESTRICTION_ALIASES, so an item described only as
        'חלבי' without any specific dairy keyword should not be caught.
        """
        dairy_allergy = _allergy("dairy", "חלב")
        items = [{"item_name": "מאפה חלבי"}]
        # 'חלבי' is not in aliases; only 'חלב' is.
        # The tokenizer splits on whitespace, so 'חלבי' is a separate token.
        violations = validate_meal_restrictions(items, [dairy_allergy])
        # This documents current behavior: 'חלבי' != 'חלב', no match.
        assert violations == [], (
            "'חלבי' is not in RESTRICTION_ALIASES — only 'חלב' is. "
            "No false positive expected."
        )

    # ------------------------------------------------------------------
    # Negation: "contains no X" must NOT trigger a violation
    # ------------------------------------------------------------------

    def test_negated_statement_no_false_positive(self):
        """An item description stating 'nut-free' or 'ללא אגוזים' should
        not be caught.
        """
        tree_nut_allergy = _allergy("tree_nuts", "אגוזים")
        items = [{"item_name": "חטיף בריאות", "description": "ללא אגוזים"}]
        violations = validate_meal_restrictions(items, [tree_nut_allergy])
        assert violations == []

    def test_contains_no_nuts_english(self):
        """English 'contains no nuts' must be treated as explicitly absent."""
        tree_nut_allergy = _allergy("tree_nuts", "אגוזים")
        items = [{"item_name": "granola bar", "description": "contains no nuts"}]
        violations = validate_meal_restrictions(items, [tree_nut_allergy])
        assert violations == []

    def test_nut_free_english(self):
        """English 'nut-free' must be treated as explicitly absent."""
        tree_nut_allergy = _allergy("tree_nuts", "אגוזים")
        items = [{"item_name": "nut-free granola bar"}]
        violations = validate_meal_restrictions(items, [tree_nut_allergy])
        assert violations == []

    def test_english_nut_token_caught(self):
        """English alias 'nut' must be caught as a standalone ingredient token."""
        tree_nut_allergy = _allergy("tree_nuts", "אגוזים")
        items = [{"item_name": "single nut topping"}]
        violations = validate_meal_restrictions(items, [tree_nut_allergy])
        assert violations, "'nut' must trigger a tree_nuts violation"


# ===========================================================================
# TestAvailabilityWiring
# ===========================================================================

class TestAvailabilityWiring:
    """Verify that resolve_availability is called by production flows."""

    async def test_workout_candidates_use_resolved_availability(self, tmp_path: Path):
        """build_workout_candidates must use resolve_availability, not raw facts."""
        db = await _make_db(tmp_path, "avail_wiring.db")
        await _add_user(db)
        # Set up minimum required facts for workout readiness
        for key, value in [
            ("primary_goal", "fat_loss_muscle_retention"),
            ("weight_kg", 85),
            ("sex", "male"),
            ("age", 30),
            ("height_cm", 178),
            ("training_days_per_week", 4),
            ("session_minutes", 60),
            ("training_location", "gym"),
            ("equipment", "full"),
            ("strength_experience", "intermediate"),
            ("weekly_availability", [
                {"weekday": 0, "available": True, "minutes": 60},
                {"weekday": 1, "available": True, "minutes": 60},
                {"weekday": 3, "available": True, "minutes": 60},
                {"weekday": 5, "available": True, "minutes": 60},
            ]),
            ("diet_restrictions", "none"),
            ("allergies", "none"),
        ]:
            await _set_fact(db, 1, key, value)
        # Safety facts
        await _set_fact(db, 1, "active_pain", "none")
        await _set_fact(db, 1, "medical_avoidance", "none")
        # Goal
        import planning
        import targets
        t = targets.compute_targets(85.0, goal_type="fat_loss_muscle_retention",
                                     sex="male", height_cm=178, age=30)
        await db.execute(
            "INSERT INTO goal_versions(user_id,calories,protein,steps,phase,status,source,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (1, t.calories, t.protein, t.steps, "fat_loss_muscle_retention",
             "active", "computed", utc_now()),
        )
        await db.execute(
            "INSERT INTO goals(user_id,calories,protein,steps,phase,updated_at) "
            "VALUES(?,?,?,?,?,?)",
            (1, t.calories, t.protein, t.steps, "fat_loss_muscle_retention", utc_now()),
        )
        candidates = await planning.build_workout_candidates(db, 1)
        assert len(candidates) == 3
        # The balanced candidate should use 4 days (from resolved availability)
        balanced = next(c for c in candidates if c.strategy == "balanced")
        assert balanced.payload["frequency"] == 4

    async def test_body_fat_sync_uses_normalizer(self, tmp_path: Path):
        """sync_health_measurements_to_facts must use normalize_body_fat."""
        db = await _make_db(tmp_path, "bf_sync.db")
        await _add_user(db)
        # Insert a body_fat health record as Apple Health fraction (0.22 = 22%)
        await db.execute(
            "INSERT INTO health(user_id,external_id,sample_type,value,unit,"
            "start_time,end_time,source_device,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (1, "bf_test_1", "body_fat", 0.22, "%",
             utc_now(), utc_now(), "watch", utc_now()),
        )
        import health_service
        old_db = health_service.DB
        health_service.DB = db
        try:
            await health_service.sync_health_measurements_to_facts(1)
        finally:
            health_service.DB = old_db
        # The normalizer should convert 0.22 (Apple Health fraction) to 22.0%
        fact = await user_model.get_fact(db, 1, "body_fat_pct")
        assert fact is not None
        val = fact.get("value")
        assert val is not None, "body_fat_pct must be synced"
        assert abs(float(val) - 22.0) < 0.1, (
            f"Apple Health 0.22 must be normalized to 22.0%, got {val}"
        )

    def test_safe_compounds_suppress_nutmeg(self):
        """SAFE_COMPOUNDS must suppress tree_nuts for nutmeg."""
        from noam_coach.services.dietary_restrictions import SAFE_COMPOUNDS
        assert "אגוז מוסקט" in SAFE_COMPOUNDS
        assert "tree_nuts" in SAFE_COMPOUNDS["אגוז מוסקט"]

    def test_safe_compounds_suppress_coconut_milk_dairy(self):
        """SAFE_COMPOUNDS must suppress dairy for coconut milk."""
        from noam_coach.services.dietary_restrictions import SAFE_COMPOUNDS
        assert "חלב קוקוס" in SAFE_COMPOUNDS
        assert "dairy" in SAFE_COMPOUNDS["חלב קוקוס"]
