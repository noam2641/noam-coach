"""Regression tests for REC-ONBOARD-02-01 through REC-ONBOARD-02-14."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import coach_bot
import health_import
import health_service
import onboarding
import questions
import user_model
from conversation import extract_flow_id, extract_version
from db import Database
from helpers import utc_now

# ── Helpers ──────────────────────────────────────────────────────────────

async def _make_db(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "test.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'Test',NULL,?)",
        (utc_now(),),
    )
    return db


def test_medication_pending_reply_does_not_overpromise_learning() -> None:
    from noam_coach.bot import onboarding as onboarding_bot

    src = Path(onboarding_bot.__file__).read_text(encoding="utf-8")
    assert "ואלמד את ההשפעה" not in src
    assert "זה יישמר ביומן שלך" in src
    assert "נוכל להשוות מול תיאבון ואימונים" in src


def test_pain_pending_reply_does_not_overpromise_replacement_and_has_safety_boundary() -> None:
    from noam_coach.bot import onboarding as onboarding_bot

    src = Path(onboarding_bot.__file__).read_text(encoding="utf-8")
    assert "אסיר או אחליף תרגילים" not in src
    assert "אנסה להסיר או להחליף תרגילים" in src
    assert "כדאי בדיקה מקצועית" in src


class FakeQuery:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.reply_markups: list[Any] = []
        self.answers: list[str | None] = []

    async def edit_message_text(
        self,
        text: str,
        reply_markup: Any = None,
        parse_mode: str | None = None,
    ) -> None:
        del parse_mode
        self.messages.append(text)
        self.reply_markups.append(reply_markup)

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        del show_alert
        self.answers.append(text)


# ── REC-ONBOARD-02-01: Structured import result ─────────────────────────

class TestImportSummaryFields:
    def test_summary_has_weight_and_activity_fields(self) -> None:
        s = health_import.ImportSummary()
        assert s.weight_records == 0
        assert s.activity_records == 0
        assert s.invalid_records == 0

    def test_summary_note_increments_rows(self) -> None:
        import datetime as dt
        s = health_import.ImportSummary()
        s.note("steps", dt.date(2025, 1, 1))
        assert s.rows == 1
        assert s.by_type["steps"] == 1
        assert s.min_date == "2025-01-01"
        assert s.max_date == "2025-01-01"


class TestHealthImportOutcomeFields:
    def test_outcome_dataclass_has_new_fields(self) -> None:
        from noam_coach.services.health_jobs import HealthImportOutcome

        outcome = HealthImportOutcome(
            inserted=10,
            duplicates=2,
            updated=3,
            invalid=1,
            summary=None,
            profile={},
            source_file="export.zip",
            total_stored=100,
            import_started="2025-01-01T00:00:00Z",
            import_completed="2025-01-01T00:01:00Z",
        )
        assert outcome.updated == 3
        assert outcome.invalid == 1
        assert outcome.source_file == "export.zip"
        assert outcome.total_stored == 100
        assert outcome.import_started == "2025-01-01T00:00:00Z"
        assert outcome.import_completed == "2025-01-01T00:01:00Z"

    def test_old_health_import_success_text_warns_without_error(self) -> None:
        import datetime as dt
        from noam_coach.services import health_jobs
        from noam_coach.services.health_jobs import HealthImportOutcome

        s = health_import.ImportSummary()
        s.note("steps", dt.date(2026, 6, 16))
        outcome = HealthImportOutcome(
            inserted=1,
            duplicates=0,
            updated=0,
            invalid=0,
            summary=s,
            profile={},
            source_file="export.zip",
            total_stored=1,
            import_started="2026-07-06T00:00:00Z",
            import_completed="2026-07-06T00:01:00Z",
        )

        warning = health_jobs._health_import_staleness_warning(
            "2026-06-16",
            today=dt.datetime(2026, 7, 6, tzinfo=dt.timezone.utc),
        )
        text = health_jobs._health_import_success_text(outcome)

        assert "יובא בהצלחה" in warning
        assert "2026-06-16" in warning
        assert "אינם טריים" in warning
        assert "2026-06-16" in text
        assert "אינם טריים" in text
        assert "שגיאה" not in text

    @pytest.mark.asyncio
    async def test_followup_summary_lists_missing_without_raw_keys(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        """RE10-4 (D15): the "דורש אישור" list moved into the per-fact wizard
        (ask_next_health_confirm_step), shown BEFORE this summary now — so
        _health_import_followup_text only lists what is still genuinely
        missing, and must still never leak raw internal keys."""
        from noam_coach.services import health_jobs

        db = await _make_db(tmp_path)
        monkeypatch.setattr(health_jobs, "DB", db)
        await user_model.set_fact(
            db,
            1,
            "sleep_schedule",
            {"typical_bedtime": "23:00", "typical_wake_time": "07:00"},
            kind=user_model.KIND_ESTIMATE,
            source=user_model.SOURCE_DERIVED,
            confirmed=False,
        )

        text = await health_jobs._health_import_followup_text(1)

        assert "עדיין חסר" in text
        assert "sleep_schedule" not in text
        assert "active_pain" not in text
        assert "{" not in text


# ── REC-ONBOARD-02-02: Health export freshness ──────────────────────────

class TestFreshnessConstants:
    def test_freshness_constants_defined(self) -> None:
        assert health_service.FRESHNESS_CURRENT == "current"
        assert health_service.FRESHNESS_RECENT == "recent"
        assert health_service.FRESHNESS_STALE == "stale"
        assert health_service.FRESHNESS_VERY_STALE == "very_stale"
        assert health_service.FRESHNESS_UNAVAILABLE == "unavailable"

    def test_freshness_warning_empty_for_current(self) -> None:
        info = {"freshness": "current", "newest_date": "2025-01-01", "age_days": 0}
        assert health_service.freshness_warning_text(info) == ""

    def test_freshness_warning_empty_for_recent(self) -> None:
        info = {"freshness": "recent", "newest_date": "2025-01-01", "age_days": 1}
        assert health_service.freshness_warning_text(info) == ""

    def test_freshness_warning_for_stale(self) -> None:
        info = {"freshness": "stale", "newest_date": "2025-01-01", "age_days": 5}
        warning = health_service.freshness_warning_text(info)
        assert "2025-01-01" in warning
        assert "5" in warning

    def test_freshness_warning_for_unavailable(self) -> None:
        info = {"freshness": "unavailable"}
        warning = health_service.freshness_warning_text(info)
        assert "/import" in warning


# ── REC-ONBOARD-02-03: Display labels ───────────────────────────────────

class TestDisplayLabels:
    def test_display_label_returns_hebrew_for_known_keys(self) -> None:
        assert user_model.display_label("workout_pattern") == "דפוס האימונים שלך"
        assert user_model.display_label("active_pain") == "כאב/פציעה פעילה"
        assert user_model.display_label("primary_goal") == "מטרה ראשית"

    def test_display_label_falls_back_to_registry(self) -> None:
        # Keys in FACT_REGISTRY but not in FACT_DISPLAY_LABELS should
        # use the registry label
        for key, spec in user_model.FACT_REGISTRY.items():
            label = user_model.display_label(key)
            assert label != "" and label is not None

    def test_display_label_falls_back_to_key(self) -> None:
        assert user_model.display_label("nonexistent_key_xyz") == "nonexistent_key_xyz"

    def test_display_value_formats_weight(self) -> None:
        result = user_model.display_value("weight_kg", 85.3)
        assert 'ק"ג' in result
        assert "85.3" in result

    def test_display_value_formats_height(self) -> None:
        result = user_model.display_value("height_cm", 178)
        assert 'ס"מ' in result

    def test_display_value_none_returns_placeholder(self) -> None:
        assert user_model.display_value("anything", None) == "לא צוין"

    def test_display_value_enum_returns_hebrew(self) -> None:
        assert user_model.display_value("primary_goal", "fat_loss_muscle_retention") == "ירידה בשומן תוך שמירה על מסת שריר"

    def test_patterns_text_items_have_display_label(self) -> None:
        profile = {
            "workout": {"typical_hour": "18:00", "weekly_frequency": 3},
            "sleep": {"typical_bedtime": "23:00", "typical_wake_time": "07:00"},
        }
        text, items = onboarding.patterns_text(profile)
        assert len(items) >= 1
        for item in items:
            assert "display_label" in item
            assert item["display_label"] != item["id"]  # label should differ from raw ID


# ── REC-ONBOARD-02-04: Fact confirmation statuses ───────────────────────

class TestFactConfirmation:
    def test_confirm_constants_exist(self) -> None:
        assert user_model.CONFIRM_INFERRED == "inferred"
        assert user_model.CONFIRM_CONFIRMED == "confirmed"
        assert user_model.CONFIRM_CORRECTED == "corrected"
        assert user_model.CONFIRM_NOT_APPLICABLE == "not_applicable"
        assert user_model.CONFIRM_DEFERRED == "deferred"
        assert user_model.CONFIRM_STALE == "stale"
        assert user_model.CONFIRM_INVALID == "invalid"

    def test_confirmation_status_none_returns_missing(self) -> None:
        assert user_model.fact_confirmation_status(None, "weight_kg") == "missing"

    def test_confirmation_status_gap_user_returns_deferred(self) -> None:
        fact = {"kind": user_model.KIND_GAP, "source": user_model.SOURCE_USER}
        assert user_model.fact_confirmation_status(fact, "x") == user_model.CONFIRM_DEFERRED

    def test_confirmation_status_not_applicable(self) -> None:
        fact = {
            "kind": user_model.KIND_FACT,
            "value": "__not_applicable__",
            "confirmed": True,
            "valid": True,
            "source": user_model.SOURCE_USER,
            "updated_at": utc_now(),
        }
        assert user_model.fact_confirmation_status(fact, "x") == user_model.CONFIRM_NOT_APPLICABLE

    def test_confirmation_status_confirmed_user_fact(self) -> None:
        fact = {
            "kind": user_model.KIND_FACT,
            "value": "some_value",
            "confirmed": True,
            "valid": True,
            "source": user_model.SOURCE_USER,
            "updated_at": utc_now(),
        }
        assert user_model.fact_confirmation_status(fact, "x") == user_model.CONFIRM_CORRECTED

    @pytest.mark.asyncio
    async def test_defer_fact_creates_gap(self, tmp_path: Path) -> None:
        db = await _make_db(tmp_path)
        await user_model.defer_fact(db, 1, "session_minutes")
        fact = await user_model.get_fact(db, 1, "session_minutes")
        assert fact is not None
        assert fact["kind"] == user_model.KIND_GAP

    @pytest.mark.asyncio
    async def test_mark_not_applicable(self, tmp_path: Path) -> None:
        db = await _make_db(tmp_path)
        await user_model.mark_not_applicable(db, 1, "diet_restrictions")
        fact = await user_model.get_fact(db, 1, "diet_restrictions")
        assert fact is not None
        assert fact["value"] == "__not_applicable__"
        assert fact["confirmed"] is True

    @pytest.mark.asyncio
    async def test_pattern_confirmation_rerenders_visible_status(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from noam_coach.bot import onboarding as onboarding_bot

        db = await _make_db(tmp_path)
        profile = {
            "workout": {"typical_hour": "18:47", "weekly_frequency": 0.9, "sessions_sampled": 3},
            "sleep": {"typical_bedtime": "00:19", "typical_wake_time": "06:14", "nights_sampled": 4},
        }
        await user_model.set_fact(
            db,
            1,
            "workout_pattern",
            profile["workout"],
            kind=user_model.KIND_ESTIMATE,
            source=user_model.SOURCE_DERIVED,
            confirmed=False,
        )
        await user_model.set_fact(
            db,
            1,
            "sleep_schedule",
            profile["sleep"],
            kind=user_model.KIND_ESTIMATE,
            source=user_model.SOURCE_DERIVED,
            confirmed=False,
        )

        async def _load_profile(_user_id: int) -> dict[str, Any]:
            return profile

        async def _track_event(*_args: Any, **_kwargs: Any) -> None:
            return None

        monkeypatch.setattr(coach_bot, "DB", db)
        monkeypatch.setattr(coach_bot, "load_routine_profile", _load_profile)
        monkeypatch.setattr(coach_bot, "track_event", _track_event)

        query = FakeQuery()
        await onboarding_bot.handle_onboarding_callback(query, 1, "onb:pat_ok:workout_pattern")

        fact = await user_model.get_fact(db, 1, "workout_pattern")
        assert fact is not None
        assert fact["confirmed"] is True
        labels = [
            button.text
            for row in query.reply_markups[-1].inline_keyboard
            for button in row
        ]
        assert any("מאושר" in label and "דפוס האימונים" in label for label in labels)
        assert "מאושר ✅" in query.answers

    @pytest.mark.asyncio
    async def test_basics_ok_confirms_visible_routine_facts(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from noam_coach.bot import onboarding as onboarding_bot

        db = await _make_db(tmp_path)
        profile = {
            "workout": {"typical_hour": "19:00", "weekly_frequency": 3, "sessions_sampled": 3},
            "sleep": {"typical_bedtime": "23:30", "typical_wake_time": "06:30", "nights_sampled": 4},
        }
        await user_model.set_fact(
            db,
            1,
            "workout_pattern",
            profile["workout"],
            kind=user_model.KIND_ESTIMATE,
            source=user_model.SOURCE_DERIVED,
            confirmed=False,
        )
        await user_model.set_fact(
            db,
            1,
            "sleep_schedule",
            profile["sleep"],
            kind=user_model.KIND_ESTIMATE,
            source=user_model.SOURCE_DERIVED,
            confirmed=False,
        )

        async def _load_profile(_user_id: int) -> dict[str, Any]:
            return profile

        async def _track_event(*_args: Any, **_kwargs: Any) -> None:
            return None

        monkeypatch.setattr(coach_bot, "DB", db)
        monkeypatch.setattr(coach_bot, "load_routine_profile", _load_profile)
        monkeypatch.setattr(coach_bot, "track_event", _track_event)

        query = FakeQuery()
        await onboarding_bot.handle_onboarding_callback(query, 1, "onb:basics_ok")

        workout = await user_model.get_fact(db, 1, "workout_pattern")
        sleep = await user_model.get_fact(db, 1, "sleep_schedule")
        assert workout is not None and workout["confirmed"] is True
        assert sleep is not None and sleep["confirmed"] is True
        labels = [
            button.text
            for row in query.reply_markups[-1].inline_keyboard
            for button in row
        ]
        assert any("מאושר" in label and "דפוס האימונים" in label for label in labels)
        assert any("מאושר" in label and "שעות השינה" in label for label in labels)


# ── REC-ONBOARD-02-05: Persist onboarding across restart ────────────────

class TestOnboardingPersistence:
    @pytest.mark.asyncio
    async def test_stage_survives_restart(self, tmp_path: Path) -> None:
        db = await _make_db(tmp_path)
        await onboarding.set_stage(db, 1, onboarding.S_CONFIRM_BASICS)
        # Simulate "restart" — re-read from DB
        stage = await onboarding.get_stage(db, 1)
        assert stage == onboarding.S_CONFIRM_BASICS
        assert await onboarding.is_onboarding(db, 1)

    @pytest.mark.asyncio
    async def test_all_stages_persist(self, tmp_path: Path) -> None:
        db = await _make_db(tmp_path)
        stages = [
            onboarding.S_OPEN,
            onboarding.S_EXPORT_HELP,
            onboarding.S_CONFIRM_BASICS,
            onboarding.S_CONFIRM_PATTERNS,
            onboarding.S_SAFETY,
            onboarding.S_PLAN_QUESTIONS,
            onboarding.S_DONE,
        ]
        for stage in stages:
            await onboarding.set_stage(db, 1, stage)
            assert await onboarding.get_stage(db, 1) == stage


# ── REC-ONBOARD-02-06: Stale callback recovery ─────────────────────────

class TestStaleCallbackRecovery:
    def test_extract_flow_id_from_callback(self) -> None:
        # Flow ID is encoded as "f" + flow_id in callback data
        assert extract_flow_id("some:action:ff-123") == "f-123"

    def test_extract_flow_id_none_for_no_flow(self) -> None:
        assert extract_flow_id("simple_callback") is None

    def test_extract_version_from_callback(self) -> None:
        result = extract_version("some:action:v5")
        assert result == 5


# ── REC-ONBOARD-02-07: question_by_fact_key ─────────────────────────────

class TestQuestionByFactKey:
    def test_finds_existing_question(self) -> None:
        q = questions.question_by_fact_key("primary_goal")
        assert q is not None
        assert q.id == "q_primary_goal"

    def test_returns_none_for_unknown(self) -> None:
        assert questions.question_by_fact_key("nonexistent_fact") is None

    def test_all_safety_questions_findable(self) -> None:
        for sq in questions.SAFETY_QUESTIONS:
            found = questions.question_by_fact_key(sq.fact_key)
            assert found is not None
            assert found.id == sq.id


# ── REC-ONBOARD-02-09: Enhanced compute_readiness ──────────────────────

class TestEnhancedReadiness:
    @pytest.mark.asyncio
    async def test_readiness_returns_new_fields(self, tmp_path: Path) -> None:
        db = await _make_db(tmp_path)
        result = await user_model.compute_readiness(db, 1, "safety")
        assert "deferred" in result
        assert "stale" in result
        assert "not_applicable" in result
        assert "missing_labels" in result
        assert "label" in result
        assert isinstance(result["deferred"], list)
        assert isinstance(result["missing_labels"], list)

    @pytest.mark.asyncio
    async def test_readiness_missing_labels_are_hebrew(self, tmp_path: Path) -> None:
        db = await _make_db(tmp_path)
        result = await user_model.compute_readiness(db, 1, "safety")
        # Missing labels should be friendly Hebrew, not raw keys
        for label in result["missing_labels"]:
            # Hebrew chars are in range U+0590-U+05FF
            assert any("\u0590" <= ch <= "\u05ff" for ch in label), f"Label not Hebrew: {label}"

    @pytest.mark.asyncio
    async def test_deferred_fact_appears_in_readiness_deferred(self, tmp_path: Path) -> None:
        db = await _make_db(tmp_path)
        await user_model.defer_fact(db, 1, "active_pain")
        result = await user_model.compute_readiness(db, 1, "safety")
        assert "active_pain" in result["deferred"]
        assert "active_pain" in result["missing"]

    @pytest.mark.asyncio
    async def test_not_applicable_satisfies_readiness(self, tmp_path: Path) -> None:
        db = await _make_db(tmp_path)
        # Mark both safety facts
        await user_model.mark_not_applicable(db, 1, "active_pain")
        await user_model.set_fact(
            db, 1, "medical_avoidance", "none",
            kind=user_model.KIND_FACT,
            source=user_model.SOURCE_USER,
            confirmed=True,
        )
        result = await user_model.compute_readiness(db, 1, "safety")
        assert "active_pain" in result["not_applicable"]
        assert "active_pain" in result["present"]
        assert result["ready"] is True

    @pytest.mark.asyncio
    async def test_readiness_unknown_profile(self, tmp_path: Path) -> None:
        db = await _make_db(tmp_path)
        result = await user_model.compute_readiness(db, 1, "nonexistent_profile")
        assert result["ready"] is False
        assert result["score"] == 0.0


# ── REC-ONBOARD-02-10: Safety profile label ─────────────────────────────

class TestSafetyProfileLabel:
    def test_safety_profile_label_is_hebrew(self) -> None:
        profile = user_model.READINESS_PROFILES.get("safety")
        assert profile is not None
        # Should be "שאלון בטיחות" not a raw English label
        assert any("\u0590" <= ch <= "\u05ff" for ch in profile.label)


# ── REC-ONBOARD-02-11: Targets uses display_label ──────────────────────

class TestTargetsDisplayLabel:
    def test_targets_imports_user_model(self) -> None:
        import targets
        # Verify user_model.display_label is reachable from targets
        assert hasattr(targets, "user_model") or "user_model" in dir(targets)


# ── Cross-cutting: No internal IDs in button-facing strings ─────────────

class TestNoInternalIdsInLabels:
    def test_fact_display_labels_cover_all_onboarding_questions(self) -> None:
        for q in questions.ONBOARDING_QUESTIONS:
            label = user_model.display_label(q.fact_key)
            # Should not be the raw key
            if q.fact_key in user_model.FACT_DISPLAY_LABELS or q.fact_key in user_model.FACT_REGISTRY:
                assert label != q.fact_key, f"No friendly label for onboarding question fact: {q.fact_key}"

    def test_deferred_gap_keys_have_labels(self) -> None:
        for key in questions.DEFERRED_GAP_KEYS:
            label = user_model.display_label(key)
            assert label != key, f"No friendly label for deferred gap key: {key}"

    def test_safety_fact_keys_have_labels(self) -> None:
        for q in questions.SAFETY_QUESTIONS:
            label = user_model.display_label(q.fact_key)
            assert label != q.fact_key, f"No friendly label for safety key: {q.fact_key}"
