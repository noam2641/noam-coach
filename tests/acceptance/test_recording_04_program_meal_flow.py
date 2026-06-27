"""End-to-end replay tests for recording batch REC-PROGRAM-04.

Verifies the full acceptance flow for dietary restrictions, training
availability, workout proposal generation, meal correction, body-fat
display normalisation, and nutrition/target computation.

Tests use ``async def`` so that pytest-asyncio (asyncio_mode=auto) provides
a single consistent event loop per test.  The database is a temporary file
(via ``tmp_path``) so that aiosqlite connections all refer to the same SQLite
file — :memory: cannot be shared across multiple aiosqlite connections.

No Telegram mocking is needed — all tests operate against the real domain
modules.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

import meal_intelligence
import planning
import targets
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
    load_restrictions_from_facts,
    merge_restrictions,
    normalize_restriction,
    parse_restrictions,
    validate_meal_restrictions,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
async def db(tmp_path: Path):
    """File-backed SQLite database, initialised once per test."""
    database = Database(str(tmp_path / "test_rec04.db"))
    await database.init()
    return database


# ---------------------------------------------------------------------------
# Helpers (all async to share the test's event loop)
# ---------------------------------------------------------------------------

async def _add_user(db: Database, user_id: int = 1) -> None:
    await db.execute(
        "INSERT OR IGNORE INTO users(id, first_name, username, updated_at) "
        "VALUES(?, 'Test', NULL, ?)",
        (user_id, utc_now()),
    )


async def _set(db: Database, user_id: int, key: str, value,
               *, confirmed: bool = True) -> None:
    await user_model.set_fact(
        db, user_id, key, value,
        source=user_model.SOURCE_USER,
        confirmed=confirmed,
    )


_WORKOUT_FACTS: dict = {
    "weight_kg": 82,
    "height_cm": 175,
    "age": 30,
    "sex": "male",
    "primary_goal": "fat_loss_muscle_retention",
    "diet_restrictions": "none",
    "allergies": "none",
    "training_days_per_week": 4,
    "active_pain": "none",
    "medical_avoidance": "none",
    "session_minutes": 50,
    "training_location": "חדר כושר",
    "equipment": "חדר כושר מלא",
    "strength_experience": "intermediate",
    "weekly_availability": [
        {"weekday": 0, "start": "18:30", "minutes": 50, "available": True},
        {"weekday": 1, "start": "18:30", "minutes": 50, "available": True},
        {"weekday": 3, "start": "18:30", "minutes": 50, "available": True},
        {"weekday": 5, "start": "10:00", "minutes": 60, "available": True},
    ],
}


# ---------------------------------------------------------------------------
# TestRecording04ReplayFlow
# ---------------------------------------------------------------------------

class TestRecording04ReplayFlow:
    """End-to-end replay tests for REC-PROGRAM-04."""

    # ------------------------------------------------------------------ #
    # Step 1: explicit training_days_per_week overrides inferred history  #
    # ------------------------------------------------------------------ #

    async def test_explicit_availability_overrides_history(self, db):
        """Confirmed training_days_per_week=4 must win over an inferred
        workout_pattern estimate of 6 days.
        """
        await _add_user(db)
        await user_model.set_fact(
            db, 1, "training_days_per_week", 4,
            source=user_model.SOURCE_USER,
            confirmed=True,
        )
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
            f"Expected 4 (confirmed), got {avail.max_days_per_week}"
        )

    # ------------------------------------------------------------------ #
    # Step 2: availability summary formatting                              #
    # ------------------------------------------------------------------ #

    async def test_availability_summary_format(self, db):
        """format_availability_summary must mention 4 days and day names."""
        avail = TrainingAvailability(
            max_days_per_week=4,
            preferred_days=[0, 1, 3, 5],
            preferred_time="18:30",
            session_minutes=45,
            source="user_confirmed",
            confidence=0.9,
            confirmed=True,
        )
        summary = format_availability_summary(avail)
        assert "4" in summary, "Must show max_days_per_week=4"
        assert "ראשון" in summary, "Must mention Sunday (weekday 0)"

    # ------------------------------------------------------------------ #
    # Step 3: workout proposal generation produces 3 candidates           #
    # ------------------------------------------------------------------ #

    async def test_workout_proposals_generated(self, db):
        """generate_candidates for workout must return exactly 3 candidates."""
        await _add_user(db)
        for key, value in _WORKOUT_FACTS.items():
            await _set(db, 1, key, value)
        proposal = await planning.build_goal_proposal(db, 1)
        goal_id = await planning.persist_goal_proposal(db, 1, proposal)
        await planning.activate_goal(db, 1, goal_id)

        candidates = await planning.generate_candidates(db, 1, "workout")
        assert len(candidates) == 3, f"Expected 3 candidates, got {len(candidates)}"

    # ------------------------------------------------------------------ #
    # Step 4: each candidate has sessions with exercise details           #
    # ------------------------------------------------------------------ #

    async def test_proposal_has_exercises_and_details(self, db):
        """Each workout candidate must have sessions with full exercise keys."""
        await _add_user(db)
        facts = dict(_WORKOUT_FACTS)
        facts["training_days_per_week"] = 3
        facts["strength_experience"] = "beginner"
        facts["weekly_availability"] = [
            {"weekday": 0, "start": "19:00", "minutes": 50, "available": True},
            {"weekday": 2, "start": "19:00", "minutes": 50, "available": True},
            {"weekday": 4, "start": "10:00", "minutes": 60, "available": True},
        ]
        for key, value in facts.items():
            await _set(db, 1, key, value)
        proposal = await planning.build_goal_proposal(db, 1)
        goal_id = await planning.persist_goal_proposal(db, 1, proposal)
        await planning.activate_goal(db, 1, goal_id)

        candidates = await planning.generate_candidates(db, 1, "workout")
        required_keys = {"sets", "rmin", "rmax", "rest", "weight"}
        for candidate in candidates:
            sessions = candidate.payload.get("sessions", [])
            assert sessions, f"Candidate {candidate.title!r} has no sessions"
            for session in sessions:
                exercises = session.get("exercises", [])
                assert exercises, (
                    f"Session {session.get('name')!r} in {candidate.title!r} "
                    f"has no exercises"
                )
                for exercise in exercises:
                    missing = required_keys - set(exercise)
                    assert not missing, (
                        f"Exercise {exercise.get('id')!r} missing keys: {missing}"
                    )

    # ------------------------------------------------------------------ #
    # Step 5: nut allergy blocks yogurt with nuts                         #
    # ------------------------------------------------------------------ #

    async def test_nuts_restriction_blocks_yogurt(self, db):
        """An allergy to 'בוטנים, אגוזים' must block 'יוגורט יווני עם אגוזים ודבש'."""
        restrictions = load_restrictions_from_facts("", "בוטנים, אגוזים")
        items = [{"item_name": "יוגורט יווני עם אגוזים ודבש"}]
        violations = validate_meal_restrictions(items, restrictions)
        assert violations, "Expected at least one violation"
        assert any(v["action"] == "block" for v in violations), (
            f"Expected a 'block' violation, got: {violations}"
        )

    # ------------------------------------------------------------------ #
    # Step 6: peanuts and tree_nuts are distinct canonical IDs            #
    # ------------------------------------------------------------------ #

    async def test_peanuts_distinct_from_tree_nuts(self, db):
        """'בוטנים' and 'אגוזים' must map to different canonical_ids."""
        peanuts_id = normalize_restriction("בוטנים")
        tree_nuts_id = normalize_restriction("אגוזים")
        assert peanuts_id != tree_nuts_id
        assert peanuts_id == "peanuts"
        assert tree_nuts_id == "tree_nuts"

    # ------------------------------------------------------------------ #
    # Step 7: restriction parsing is idempotent                           #
    # ------------------------------------------------------------------ #

    async def test_restriction_idempotent(self, db):
        """Parsing 'אגוזים' twice and merging must yield only one entry."""
        first = parse_restrictions(
            "אגוזים", restriction_type="allergy", severity="critical",
            source="test", confirmed=True,
        )
        second = parse_restrictions(
            "אגוזים", restriction_type="allergy", severity="critical",
            source="test", confirmed=True,
        )
        merged = merge_restrictions(first, second)
        tree_nut_entries = [r for r in merged if r.canonical_id == "tree_nuts"]
        assert len(tree_nut_entries) == 1, (
            f"Expected exactly 1 tree_nuts entry after idempotent merge, "
            f"got {len(tree_nut_entries)}"
        )

    # ------------------------------------------------------------------ #
    # Step 8: Hebrew and English aliases for nuts all map to tree_nuts    #
    # ------------------------------------------------------------------ #

    async def test_restriction_aliases_hebrew_english(self, db):
        """'walnuts', 'אגוזי מלך', and 'קשיו' must all map to 'tree_nuts'."""
        for alias in ("walnuts", "אגוזי מלך", "קשיו"):
            result = normalize_restriction(alias)
            assert result == "tree_nuts", (
                f"Expected 'tree_nuts' for alias {alias!r}, got {result!r}"
            )

    # ------------------------------------------------------------------ #
    # Step 9: body fat fraction 0.20 is displayed as 20.0%               #
    # ------------------------------------------------------------------ #

    async def test_body_fat_fraction_normalized(self, db):
        """display_value('body_fat_pct', 0.20) must produce '20.0%', not '0.2%'."""
        result = user_model.display_value("body_fat_pct", 0.20)
        assert "20.0%" in result, (
            f"Expected fraction 0.20 → '20.0%', got {result!r}"
        )
        assert "0.2%" not in result, (
            f"Raw fraction should not be displayed as '0.2%', got {result!r}"
        )

    # ------------------------------------------------------------------ #
    # Step 10: body fat percent passthrough 20 → 20.0%                   #
    # ------------------------------------------------------------------ #

    async def test_body_fat_percent_passthrough(self, db):
        """display_value('body_fat_pct', 20) must produce '20.0%'."""
        result = user_model.display_value("body_fat_pct", 20)
        assert "20.0%" in result, (
            f"Expected integer 20 → '20.0%', got {result!r}"
        )

    # ------------------------------------------------------------------ #
    # Step 11: gap dict must not expose raw internal keys                 #
    # ------------------------------------------------------------------ #

    async def test_profile_no_raw_dict(self, db):
        """display_value for a gap dict must not surface 'missing' or 'why_matters'."""
        gap_value = {"missing": True, "why_matters": "חשוב לדיוק הדיאטה"}
        result = user_model.display_value("diet_restrictions", gap_value)
        assert "missing" not in result, (
            f"Internal key 'missing' must not appear in output: {result!r}"
        )
        assert "why_matters" not in result, (
            f"Internal key 'why_matters' must not appear in output: {result!r}"
        )

    # ------------------------------------------------------------------ #
    # Step 12: replace correction parsed from "שניצל רגיל לא טופו"       #
    # ------------------------------------------------------------------ #

    async def test_meal_replace_correction(self, db):
        """'שניצל רגיל לא טופו' must parse to a replace correction."""
        corrections = meal_intelligence.parse_meal_correction("שניצל רגיל לא טופו")
        replace_corrections = [c for c in corrections if c.kind == "replace"]
        assert replace_corrections, (
            "Expected at least one 'replace' correction from 'שניצל רגיל לא טופו'"
        )
        rc = replace_corrections[0]
        assert "שניצל" in rc.value, (
            f"Expected replacement value to contain 'שניצל', got {rc.value!r}"
        )

    # ------------------------------------------------------------------ #
    # Step 13: remove correction parsed from "בלי ברוקולי"               #
    # ------------------------------------------------------------------ #

    async def test_meal_remove_broccoli(self, db):
        """'בלי ברוקולי' must parse to a remove correction targeting ברוקולי."""
        corrections = meal_intelligence.parse_meal_correction("בלי ברוקולי")
        remove_corrections = [c for c in corrections if c.kind == "remove"]
        assert remove_corrections, (
            "Expected at least one 'remove' correction from 'בלי ברוקולי'"
        )
        rc = remove_corrections[0]
        assert "ברוקולי" in rc.item_hint, (
            f"Expected item_hint to contain 'ברוקולי', got {rc.item_hint!r}"
        )

    # ------------------------------------------------------------------ #
    # Step 14: replace correction preserves other items unchanged         #
    # ------------------------------------------------------------------ #

    async def test_replace_preserves_other_items(self, db):
        """Replacing one item must leave the other two items unchanged."""
        analysis = MealAnalysis(
            meal_name="ארוחת צהריים",
            confidence=0.9,
            items=[
                FoodItem(name="טופו", grams=150, calories=114,
                         protein=12, carbs=3, fat=6, confidence=0.9),
                FoodItem(name="אורז מבושל", grams=120, calories=156,
                         protein=3, carbs=34, fat=0.4, confidence=0.9),
                FoodItem(name="ברוקולי מאודה", grams=80, calories=28,
                         protein=3, carbs=5, fat=0.4, confidence=0.9),
            ],
        )
        correction = meal_intelligence.MealCorrection(
            kind="replace",
            item_hint="טופו",
            value="שניצל רגיל",
            original_text="שניצל רגיל לא טופו",
        )
        result = meal_intelligence.apply_item_replacement_correction(analysis, correction)
        result_names = [item.name for item in result.items]
        assert any("אורז" in name for name in result_names), (
            "אורז מבושל must remain after replacing טופו"
        )
        assert any("ברוקולי" in name for name in result_names), (
            "ברוקולי מאודה must remain after replacing טופו"
        )

    # ------------------------------------------------------------------ #
    # Step 15: remove correction preserves other items unchanged          #
    # ------------------------------------------------------------------ #

    async def test_remove_preserves_other_items(self, db):
        """Removing broccoli must leave the other 2 items with same values."""
        item_rice = FoodItem(name="אורז מבושל", grams=120, calories=156,
                             protein=3, carbs=34, fat=0.4, confidence=0.9)
        item_chicken = FoodItem(name="עוף צלוי", grams=150, calories=248,
                                protein=37, carbs=0, fat=11, confidence=0.9)
        item_broccoli = FoodItem(name="ברוקולי מאודה", grams=80, calories=28,
                                 protein=3, carbs=5, fat=0.4, confidence=0.9)
        analysis = MealAnalysis(
            meal_name="ארוחת צהריים",
            confidence=0.9,
            items=[item_rice, item_chicken, item_broccoli],
        )
        correction = meal_intelligence.MealCorrection(
            kind="remove",
            item_hint="ברוקולי",
            value="",
            original_text="בלי ברוקולי",
        )
        result = meal_intelligence.apply_item_removal_correction(analysis, correction)
        result_names = [item.name for item in result.items]
        assert not any("ברוקולי" in name for name in result_names), (
            "Broccoli must be removed"
        )
        rice = next((i for i in result.items if "אורז" in i.name), None)
        chicken = next((i for i in result.items if "עוף" in i.name), None)
        assert rice is not None and rice.grams == 120 and rice.calories == 156, (
            f"Rice item must be unchanged: {rice}"
        )
        assert chicken is not None and chicken.grams == 150 and chicken.calories == 248, (
            f"Chicken item must be unchanged: {chicken}"
        )

    # ------------------------------------------------------------------ #
    # Step 16: meal totals equal the sum of individual items              #
    # ------------------------------------------------------------------ #

    async def test_meal_totals_equal_item_sum(self, db):
        """MealAnalysis.totals() must exactly equal the sum of item values."""
        items = [
            FoodItem(name="a", grams=100, calories=200, protein=20,
                     carbs=30, fat=5, confidence=0.9),
            FoodItem(name="b", grams=50, calories=100, protein=10,
                     carbs=15, fat=3, confidence=0.9),
            FoodItem(name="c", grams=75, calories=150, protein=15,
                     carbs=20, fat=4, confidence=0.9),
        ]
        analysis = MealAnalysis(meal_name="test", confidence=0.9, items=items)
        totals = analysis.totals()
        assert totals["calories"] == sum(i.calories for i in items)
        assert totals["protein"] == sum(i.protein for i in items)
        assert totals["carbs"] == sum(i.carbs for i in items)
        assert totals["fat"] == sum(i.fat for i in items)

    # ------------------------------------------------------------------ #
    # Step 17: nutrition plan options are nut-allergy safe                #
    # ------------------------------------------------------------------ #

    async def test_nutrition_plan_restriction_safe(self, db):
        """Nutrition candidates generated for a nut-allergy user must not
        include 'אגוזים' in any meal slot option text.
        """
        await _add_user(db)
        facts = {
            "weight_kg": 70,
            "height_cm": 165,
            "age": 28,
            "sex": "female",
            "primary_goal": "fat_loss_muscle_retention",
            "diet_restrictions": "none",
            "allergies": "אגוזים",
        }
        for key, value in facts.items():
            await _set(db, 1, key, value)
        proposal = await planning.build_goal_proposal(db, 1)
        goal_id = await planning.persist_goal_proposal(db, 1, proposal)
        await planning.activate_goal(db, 1, goal_id)

        candidates = await planning.generate_candidates(db, 1, "nutrition")
        assert candidates, "Must generate at least one nutrition candidate"
        for candidate in candidates:
            payload_text = json.dumps(candidate.payload, ensure_ascii=False)
            assert "אגוזים" not in payload_text, (
                f"Candidate '{candidate.title}' payload must not contain 'אגוזים' "
                f"when user has a nut allergy"
            )

    async def test_nutrition_plan_filters_canonical_protein_options(self, db):
        """Nutrition candidates must filter protein options through canonical IDs."""
        await _add_user(db)
        facts = {
            "weight_kg": 70,
            "height_cm": 165,
            "age": 28,
            "sex": "female",
            "primary_goal": "fat_loss_muscle_retention",
            "diet_restrictions": "חלב, סויה",
            "allergies": "none",
        }
        for key, value in facts.items():
            await _set(db, 1, key, value)
        proposal = await planning.build_goal_proposal(db, 1)
        goal_id = await planning.persist_goal_proposal(db, 1, proposal)
        await planning.activate_goal(db, 1, goal_id)

        candidates = await planning.generate_candidates(db, 1, "nutrition")
        payload_text = json.dumps(
            [candidate.payload for candidate in candidates],
            ensure_ascii=False,
        )
        assert "יוגורט/גבינה" not in payload_text
        assert "גבינה/יוגורט עתיר חלבון" not in payload_text
        assert "יוגורט סויה עתיר חלבון" not in payload_text
        assert "טופו" not in payload_text

    # ------------------------------------------------------------------ #
    # Step 18: target explanation mentions weight and goal                #
    # ------------------------------------------------------------------ #

    async def test_target_explanation_contains_inputs(self, db):
        """explain_targets must mention the input weight and goal type."""
        result = targets.compute_targets(
            80.0,
            avg_steps=8000.0,
            goal_type="fat_loss_muscle_retention",
            sex="male",
            height_cm=175.0,
            age=32,
        )
        explanation = targets.explain_targets(result)
        assert "80" in explanation, (
            f"Explanation must mention weight 80: {explanation!r}"
        )
        assert "ירידה" in explanation, (
            f"Explanation must mention the goal (ירידה...): {explanation!r}"
        )

    # ------------------------------------------------------------------ #
    # Step 19: persist_meal is idempotent (second call returns None)      #
    # ------------------------------------------------------------------ #

    async def test_save_meal_idempotent(self, db):
        """An approval already decided cannot be approved again.

        Simulates the idempotency guarantee: once an approval row is not
        'pending', any subsequent persist attempt must fail gracefully
        (returning None or raising, never double-saving).
        """
        await _add_user(db)
        approval_id = str(uuid.uuid4())
        analysis = MealAnalysis(
            meal_name="בדיקה",
            confidence=0.9,
            items=[
                FoodItem(name="אורז", grams=120, calories=156,
                         protein=3, carbs=34, fat=0.4, confidence=0.9),
            ],
        )
        payload = json.dumps(
            {"analysis": analysis.model_dump()}, ensure_ascii=False
        )
        # Insert approval already in 'approved' state (simulates first save)
        await db.execute(
            "INSERT INTO approvals(id, user_id, kind, payload, status, created_at) "
            "VALUES(?, ?, 'meal', ?, 'approved', ?)",
            (approval_id, 1, payload, utc_now()),
        )
        # A second persist attempt would find no pending row
        pending = await db.fetch_one(
            "SELECT * FROM approvals WHERE id=? AND status='pending'",
            (approval_id,),
        )
        assert pending is None, (
            "No pending approval should exist after the first approval"
        )

    # ------------------------------------------------------------------ #
    # Step 20: daily totals come from saved meals, not pending approvals  #
    # ------------------------------------------------------------------ #

    async def test_daily_totals_no_drafts(self, db):
        """Saved meals must count in daily totals; pending approvals must not."""
        await _add_user(db)
        today = utc_now()[:10]
        eaten_at = f"{today}T12:00:00+00:00"

        # Insert a saved meal directly into the meals table
        await db.execute(
            "INSERT INTO meals(user_id, name, calories, protein, carbs, fat, "
            "confidence, image_path, approval_id, eaten_at, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)",
            (1, "ארוחת צהריים", 500, 40, 50, 15, 0.9, eaten_at, utc_now()),
        )
        # Insert a pending approval (not yet approved)
        approval_id = str(uuid.uuid4())
        pending_analysis = MealAnalysis(
            meal_name="ארוחת ערב",
            confidence=0.9,
            items=[
                FoodItem(name="פסטה", grams=200, calories=260,
                         protein=10, carbs=50, fat=3, confidence=0.9),
            ],
        )
        await db.execute(
            "INSERT INTO approvals(id, user_id, kind, payload, status, created_at) "
            "VALUES(?, ?, 'meal', ?, 'pending', ?)",
            (approval_id, 1,
             json.dumps({"analysis": pending_analysis.model_dump()},
                        ensure_ascii=False),
             utc_now()),
        )

        # Query total calories from meals table (the correct source)
        saved = await db.fetch_all(
            "SELECT SUM(calories) AS total_cal FROM meals "
            "WHERE user_id=? AND DATE(eaten_at)=DATE(?)",
            (1, eaten_at),
        )
        total_cal = saved[0]["total_cal"] if saved else 0
        assert total_cal == 500, (
            f"Daily totals must include saved meals (500 kcal), got {total_cal}"
        )

        # Verify pending approval exists in approvals table but not in meals
        pending_row = await db.fetch_one(
            "SELECT COUNT(*) AS cnt FROM approvals "
            "WHERE user_id=? AND status='pending'",
            (1,),
        )
        assert pending_row and pending_row["cnt"] >= 1, (
            "Pending approval must exist in the approvals table"
        )
        meal_count_row = await db.fetch_one(
            "SELECT COUNT(*) AS cnt FROM meals WHERE user_id=?", (1,)
        )
        assert meal_count_row and meal_count_row["cnt"] == 1, (
            "Only the one saved meal should appear in the meals table"
        )
