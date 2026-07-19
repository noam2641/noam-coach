"""Regression coverage for the 2026-07-18 production audit findings.

Each test cites the finding (F-A1..F-A9) from
docs/session_audits/2026-07-18/SANITIZED_SESSION_AUDIT.md that it locks in.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import coach_bot
import conversation
import event_log
from db import Database
from helpers import utc_now
from models import FoodItem, MealAnalysis
from noam_coach.bot import callback_meals as callback_meals_bot
from noam_coach.bot import meals as meals_bot
from noam_coach.observability import ObservabilityMode, interaction_scope, set_mode
from noam_coach.observability.modes import reset_mode
from noam_coach.observability.taxonomy import DECISION_FINALIZED, STATE_MUTATED
from noam_coach.services import core as core_services
from noam_coach.services.core import create_approval, fetch_approval_any, set_meal_fix
from noam_coach.services.meal_approval_lifecycle import (
    bump_revision,
    is_stale_revision,
    parse_decision_token,
)

USER_ID = 1


class FakeMessage:
    def __init__(self) -> None:
        self.replies: list[str] = []
        self.reply_markups: list[Any] = []

    async def reply_text(self, text: str, reply_markup: Any = None, parse_mode: Any = None) -> "FakeMessage":
        self.replies.append(text)
        self.reply_markups.append(reply_markup)
        return self


class FakeQuery:
    def __init__(self, data: str = "") -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=USER_ID)
        self.message = FakeMessage()
        self.edits: list[str] = []
        self.edit_markups: list[Any] = []

    async def edit_message_text(self, text: str, reply_markup: Any = None, parse_mode: Any = None) -> None:
        self.edits.append(text)
        self.edit_markups.append(reply_markup)

    async def answer(self, text: Any = None, show_alert: bool = False) -> None:
        return None


def _markup_callbacks(markup: Any) -> list[str]:
    if markup is None:
        return []
    return [b.callback_data for row in markup.inline_keyboard for b in row]


@pytest.fixture
async def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Database:
    database = Database(str(tmp_path / "audit.db"))
    await database.init()
    await database.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(?, 'Test', NULL, ?)",
        (USER_ID, utc_now()),
    )
    from noam_coach.bot import callback_plans as callback_plans_bot
    from noam_coach.bot import callback_session as callback_session_bot
    from noam_coach.bot import ui as ui_bot

    monkeypatch.setattr(coach_bot, "DB", database)
    monkeypatch.setattr(core_services, "DB", database)
    monkeypatch.setattr(callback_meals_bot, "DB", database)
    monkeypatch.setattr(meals_bot, "DB", database)
    monkeypatch.setattr(ui_bot, "DB", database)
    monkeypatch.setattr(callback_plans_bot, "DB", database, raising=False)
    monkeypatch.setattr(callback_session_bot, "DB", database, raising=False)
    import user_model as user_model_mod

    monkeypatch.setattr(user_model_mod, "DB", database, raising=False)
    monkeypatch.setattr(conversation, "DB", database, raising=False)
    from config import SETTINGS

    monkeypatch.setattr(SETTINGS, "telegram_allowed_user_id", USER_ID, raising=False)
    set_mode(ObservabilityMode.CONTENT)
    yield database
    reset_mode()


def _analysis(name: str = "שניצל") -> MealAnalysis:
    # The card title is composed from ITEM names — the first item carries
    # the distinctive name so per-meal assertions can address the card.
    return MealAnalysis(
        meal_name=name,
        items=[
            FoodItem(name=name, grams=180.0, calories=430.0, protein=32.0,
                     carbs=14.0, fat=26.0, confidence=0.9),
            FoodItem(name="קוסקוס", grams=150.0, calories=180.0, protein=6.0,
                     carbs=36.0, fat=1.0, confidence=0.9),
        ],
        confidence=0.9,
    )


async def _create_meal_approval(
    db: Database, *, name: str = "שניצל וקוסקוס", revision: int = 0,
    image: str | None = None,
) -> str:
    payload: dict[str, Any] = {
        "analysis": _analysis(name).model_dump(),
        "eaten_at": utc_now(),
        "revision": revision,
    }
    if image:
        payload["image"] = image
    return await create_approval(USER_ID, "meal", payload)


async def _decision_events(db: Database, outcome: str) -> list[Any]:
    events = await event_log.list_events(db, USER_ID, event=DECISION_FINALIZED)
    return [e for e in events if e.outcome == outcome]


async def _press(data: str) -> FakeQuery:
    query = FakeQuery(data)
    with interaction_scope(user_id=USER_ID):
        handled = await callback_meals_bot.handle_meal_callback(query, USER_ID, data)
    assert handled is True
    return query


# ---------------------------------------------------------------------------
# F-A1 — approval lifecycle: revisions, truthful terminals, restoration
# ---------------------------------------------------------------------------


def test_fa1_decision_token_roundtrip() -> None:
    assert parse_decision_token("approve_meal:abc-123") == ("abc-123", None)
    assert parse_decision_token("approve_meal:abc-123:r0") == ("abc-123", 0)
    assert parse_decision_token("reject_meal:a_b:r17") == ("a_b", 17)
    row = {"data": {"revision": 2}}
    assert is_stale_revision(row, 1) is True
    assert is_stale_revision(row, 2) is False
    assert is_stale_revision(row, None) is False  # legacy unversioned control
    payload: dict[str, Any] = {}
    assert bump_revision(payload) == 1 and payload["revision"] == 1


async def test_fa1_stale_reject_refused_and_current_card_shown(db: Database) -> None:
    """THE incident shape: a reject pressed on a pre-correction card must
    not consume the corrected approval — it re-renders the current card."""
    approval_id = await _create_meal_approval(db, revision=1)  # corrected once
    query = await _press(f"reject_meal:{approval_id}:r0")  # stale card press

    row = await fetch_approval_any(USER_ID, approval_id)
    assert row["status"] == "pending"  # NOT consumed
    refusals = [
        e for e in await event_log.list_events(db, USER_ID, event=DECISION_FINALIZED)
        if e.outcome == "refused" and e.properties.get("reason") == "stale_approval_revision"
    ]
    assert len(refusals) == 1
    assert any("שניצל" in text for text in query.edits)  # current card re-rendered


async def test_fa1_stale_approve_refused_current_card_shown(db: Database) -> None:
    approval_id = await _create_meal_approval(db, revision=3)
    query = await _press(f"approve_meal:{approval_id}:r1")
    row = await fetch_approval_any(USER_ID, approval_id)
    assert row["status"] == "pending"
    meals = await db.fetch_all("SELECT COUNT(*) AS c FROM meals", ())
    assert meals[0]["c"] == 0  # nothing persisted from a stale press
    assert any("שניצל" in text for text in query.edits)


async def test_fa1_reject_emits_canonical_events_and_deletion_evidence(
    db: Database, tmp_path: Path
) -> None:
    image = tmp_path / "meal.jpg"
    image.write_bytes(b"fake-jpeg-bytes")
    approval_id = await _create_meal_approval(db, image=str(image))

    query = await _press(f"reject_meal:{approval_id}:r0")

    assert not image.exists()  # deletion still happens (privacy by design)
    deleted = [
        e for e in await event_log.list_events(db, USER_ID, event=STATE_MUTATED)
        if e.properties.get("action") == "deleted" and e.properties.get("domain") == "media"
    ]
    assert len(deleted) == 1  # ...but it is trace evidence now (F-A9)
    assert deleted[0].properties["reason"] == "meal_rejected"
    rejected = await _decision_events(db, "rejected")
    assert len(rejected) == 1 and rejected[0].entity == "meal_approval"
    # The reject confirmation offers explicit restoration.
    assert any("restore_meal:" in cb for cb in _markup_callbacks(query.edit_markups[-1]))


async def test_fa1_press_after_reject_gets_restore_and_restore_works(db: Database) -> None:
    """Eleven presses were swallowed in production; now every one gets the
    truthful terminal with a working restore path — no meal can be lost."""
    approval_id = await _create_meal_approval(db)
    await _press(f"reject_meal:{approval_id}:r0")

    query = await _press(f"approve_meal:{approval_id}:r0")
    assert any("נדחתה קודם" in text for text in query.edits)
    restore_cbs = [cb for cb in _markup_callbacks(query.edit_markups[-1]) if cb.startswith("restore_meal:")]
    assert restore_cbs

    restore_query = await _press(restore_cbs[0])
    # A fresh PENDING approval exists and its card is rendered.
    pending = await db.fetch_all(
        "SELECT id FROM approvals WHERE user_id=? AND status='pending'", (USER_ID,))
    assert len(pending) == 1
    new_id = pending[0]["id"]
    assert new_id != approval_id
    assert any("שניצל" in text for text in restore_query.edits)
    flow = await conversation.get_active_flow(db, USER_ID)
    assert flow.name == conversation.FlowName.meal_correction
    assert flow.step == new_id
    restored_events = [
        e for e in await event_log.list_events(db, USER_ID, event=STATE_MUTATED)
        if e.properties.get("action") == "restored"
    ]
    assert len(restored_events) == 1


async def test_fa1_reject_after_approve_is_truthful(db: Database) -> None:
    approval_id = await _create_meal_approval(db)
    await _press(f"approve_meal:{approval_id}:r0")  # persists the meal
    meals = await db.fetch_all("SELECT COUNT(*) AS c FROM meals", ())
    assert meals[0]["c"] == 1

    query = await _press(f"reject_meal:{approval_id}:r0")
    assert any("כבר נשמרה" in text for text in query.edits)
    assert not any("נדחתה ולא נשמרה" in text for text in query.edits)  # no false claim
    row = await fetch_approval_any(USER_ID, approval_id)
    assert row["status"] == "approved"  # untouched
    meals = await db.fetch_all("SELECT COUNT(*) AS c FROM meals", ())
    assert meals[0]["c"] == 1  # idempotent


# ---------------------------------------------------------------------------
# F-A5 — suspended meal flows are re-presented; decisions are entity-addressed
# ---------------------------------------------------------------------------


async def test_fa5_resumed_meal_card_is_represented(db: Database) -> None:
    """Production: rejecting meal B resumed meal A's flow invisibly and the
    protein drink was lost. The resumed card must be re-presented."""
    approval_a = await _create_meal_approval(db, name="שייק חלבון")
    await set_meal_fix(USER_ID, approval_a)
    approval_b = await _create_meal_approval(db, name="קציצות דג")
    await set_meal_fix(USER_ID, approval_b)  # suspends A

    query = await _press(f"reject_meal:{approval_b}:r0")

    flow = await conversation.get_active_flow(db, USER_ID)
    assert flow.name == conversation.FlowName.meal_correction
    assert flow.step == approval_a  # A resumed...
    replies = " ".join(query.message.replies)
    assert "ממשיכים בארוחה" in replies
    assert any("שייק" in reply for reply in query.message.replies)  # ...and visible


async def test_fa5_decision_addressed_to_other_meal_leaves_active_flow(db: Database) -> None:
    approval_a = await _create_meal_approval(db, name="שייק חלבון")
    await set_meal_fix(USER_ID, approval_a)
    approval_b = await _create_meal_approval(db, name="קציצות דג")
    # B is pending but NOT the active flow (no set_meal_fix for B).

    await _press(f"reject_meal:{approval_b}:r0")

    row_b = await fetch_approval_any(USER_ID, approval_b)
    assert row_b["status"] == "rejected"
    flow = await conversation.get_active_flow(db, USER_ID)
    assert flow.name == conversation.FlowName.meal_correction
    assert flow.step == approval_a  # A's flow untouched by B's decision


# ---------------------------------------------------------------------------
# F-A2 — canonical quantity interpretation: no impossible meal may persist
# ---------------------------------------------------------------------------


def _incident_schnitzel_item() -> FoodItem:
    """The exact production values: count 3 written into the grams field."""
    return FoodItem(
        name="שניצל", grams=3.0, calories=8.4, protein=0.7, carbs=0.3, fat=0.5,
        confidence=1.0, quantity_count=3.0, quantity_unit="כדור",
        quantity_source="visual_count",
    )


def test_fa2_incident_count_as_grams_is_blocked() -> None:
    from noam_coach.services.meal_plausibility import check_item

    issues = check_item(_incident_schnitzel_item())
    assert [i.code for i in issues] == ["count_written_as_grams"]
    assert issues[0].severity == "block"


def test_fa2_impossible_densities_are_blocked() -> None:
    from noam_coach.services.meal_plausibility import check_item

    protein_heavier_than_food = FoodItem(
        name="חזה עוף", grams=50.0, calories=200.0, protein=80.0,
        carbs=0.0, fat=2.0, confidence=0.9,
    )
    assert any(i.code == "impossible_protein_density" and i.severity == "block"
               for i in check_item(protein_heavier_than_food))

    denser_than_fat = FoodItem(
        name="גבינה", grams=20.0, calories=400.0, protein=5.0,
        carbs=1.0, fat=10.0, confidence=0.9,
    )
    assert any(i.code == "impossible_calorie_density" and i.severity == "block"
               for i in check_item(denser_than_fat))

    zero_energy_solid = FoodItem(
        name="אורז מבושל", grams=200.0, calories=3.0, protein=0.1,
        carbs=0.5, fat=0.0, confidence=0.9,
    )
    assert any(i.code == "implausible_zero_energy_solid" and i.severity == "block"
               for i in check_item(zero_energy_solid))


def test_fa2_legitimate_foods_are_not_flagged() -> None:
    """False-positive proof: small counted units, drinks, tiny genuine
    ingredients and ordinary meals all pass."""
    from noam_coach.services.meal_plausibility import check_analysis, check_item

    olives = FoodItem(name="זיתים", grams=40.0, calories=60.0, protein=0.4,
                      carbs=1.5, fat=6.0, confidence=0.9,
                      quantity_count=10.0, quantity_unit="יחידות")
    assert check_item(olives) == []

    zero_cola = FoodItem(name="משקה קולה ללא סוכר", grams=330.0, calories=0.0,
                         protein=0.0, carbs=0.0, fat=0.0, confidence=0.9)
    assert check_item(zero_cola) == []  # beverages may be zero-energy

    yeast = FoodItem(name="שמרים", grams=3.0, calories=10.0, protein=1.2,
                     carbs=1.0, fat=0.1, confidence=0.9)  # no count → no signature
    assert check_item(yeast) == []

    normal_meal = _analysis("שניצל אמיתי")
    assert check_analysis(normal_meal.items) == []


def test_fa2_incident_meal_cannot_be_persisted(db: Database) -> None:
    from noam_coach.services.meal_validation import validate_meal_analysis

    incident = MealAnalysis(
        meal_name="שניצל + חציל + רוטב",
        items=[
            _incident_schnitzel_item(),
            FoodItem(name="חציל בשרוף מטוגן", grams=100.0, calories=120.0,
                     protein=1.0, carbs=8.0, fat=9.0, confidence=0.8),
        ],
        confidence=0.9,
    )
    result = validate_meal_analysis(incident)
    assert result.blocked  # the render shows "אי אפשר לשמור" and persist raises


async def test_fa2_blocked_card_offers_quantity_fix_not_only_reject(db: Database) -> None:
    payload = {
        "analysis": MealAnalysis(
            meal_name="שניצל", items=[_incident_schnitzel_item()], confidence=0.9,
        ).model_dump(),
        "eaten_at": utc_now(),
        "revision": 0,
    }
    approval_id = await create_approval(USER_ID, "meal", payload)
    query = FakeQuery()
    await meals_bot.render_meal(query, USER_ID, approval_id)
    assert any("אי אפשר לשמור" in text for text in query.edits)
    callbacks = _markup_callbacks(query.edit_markups[-1])
    assert f"editqtymenu:{approval_id}" in callbacks  # the fix path
    assert not any(cb.startswith("approve_meal:") for cb in callbacks)  # no save


async def test_fa2_persist_refuses_impossible_meal(db: Database) -> None:
    payload = {
        "analysis": MealAnalysis(
            meal_name="שניצל", items=[_incident_schnitzel_item()], confidence=0.9,
        ).model_dump(),
        "eaten_at": utc_now(),
        "revision": 0,
    }
    approval_id = await create_approval(USER_ID, "meal", payload)
    with pytest.raises(Exception):
        await meals_bot.persist_meal(USER_ID, approval_id)
    meals = await db.fetch_all("SELECT COUNT(*) AS c FROM meals", ())
    assert meals[0]["c"] == 0
    row = await fetch_approval_any(USER_ID, approval_id)
    assert row["status"] == "pending"  # nothing consumed; the user can fix it


async def test_fa1_rendered_card_carries_revision_tokens(db: Database) -> None:
    approval_id = await _create_meal_approval(db, revision=2)
    query = FakeQuery()
    await meals_bot.render_meal(query, USER_ID, approval_id)
    callbacks = _markup_callbacks(query.edit_markups[-1])
    assert f"approve_meal:{approval_id}:r2" in callbacks
    assert f"reject_meal:{approval_id}:r2" in callbacks


# ---------------------------------------------------------------------------
# F-A3 — workout completion has ONE truth (real sessions), stated with provenance
# ---------------------------------------------------------------------------


async def _seed_plan(db: Database) -> None:
    import user_model

    await user_model.set_fact(
        db, USER_ID, "active_workout_plan",
        {"sessions": [{"code": "A", "weekday": 0}, {"code": "B", "weekday": 2},
                      {"code": "C", "weekday": 4}]},
        kind=user_model.KIND_FACT, source=user_model.SOURCE_USER, confirmed=True,
    )


async def test_fa3_no_plan_and_all_done_are_distinct_states(db: Database) -> None:
    from noam_coach.bot.ui import resolve_todays_workout

    # No plan, zero workouts → NOT "already completed".
    result = await resolve_todays_workout(USER_ID)
    assert result.code is None and result.reason == "no_plan"
    assert result.done_today == ()

    # With a plan and no sessions today → a code is offered.
    await _seed_plan(db)
    offered = await resolve_todays_workout(USER_ID)
    assert offered.code in {"A", "B", "C"}
    assert offered.reason in {"offer_today", "offer_next"}


async def test_fa3_all_done_reflects_real_sessions(db: Database) -> None:
    from noam_coach.bot.ui import resolve_todays_workout

    await _seed_plan(db)
    now = utc_now()
    for code in ("A", "B", "C"):
        await db.execute(
            "INSERT INTO sessions(user_id, code, name, plan, status, exercise_index, "
            "set_number, started_at, ended_at) "
            "VALUES(?, ?, ?, '{}', 'completed', 0, 1, ?, ?)",
            (USER_ID, code, f"אימון {code}", now, now),
        )
    result = await resolve_todays_workout(USER_ID)
    assert result.code is None
    assert result.reason == "all_done_today"
    assert set(result.done_today) == {"A", "B", "C"}


async def test_fa3_menu_message_matches_state(db: Database) -> None:
    from noam_coach.bot import callback_plans as plans_bot

    query = FakeQuery("menu:workout")
    handled = await plans_bot._handle_workout_menu_actions(
        query, SimpleNamespace(bot=None), USER_ID, "menu:workout",
    )
    assert handled is True
    text = " ".join(query.edits)
    assert "כבר הושלם" not in text  # the false claim is gone for a planless user
    assert "אין לך תוכנית אימונים פעילה" in text


# ---------------------------------------------------------------------------
# F-A4 — the finish dialog never offers a "full" it will silently demote
# ---------------------------------------------------------------------------


def test_fa4_incomplete_workout_status_is_partial_even_on_full_choice() -> None:
    import training_intelligence

    # The truth rule the dialog must respect: 1/12 sets + "full" → partial.
    assert training_intelligence.workout_status(
        1, 12, user_choice="full", duration_seconds=60,
    ) == "partial"
    # And a genuinely complete workout honors "full".
    assert training_intelligence.workout_status(
        12, 12, user_choice="full", duration_seconds=600,
    ) == "completed"


# ---------------------------------------------------------------------------
# F-A6 — the morning briefing never renders a Python list literal
# ---------------------------------------------------------------------------


async def test_fa6_morning_briefing_headline_has_no_list_literal(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import datetime, timezone

    from noam_coach.jobs.proactive import DailyContext
    from noam_coach.services import health_jobs

    health_jobs.DB = db

    async def _fake_time(_user_id: int, _now: Any) -> str | None:
        return None

    monkeypatch.setattr(health_jobs, "_todays_workout_time", _fake_time)

    ctx = DailyContext(
        user_id=USER_ID,
        now=datetime(2026, 7, 18, 9, 0, tzinfo=timezone.utc),  # a Saturday
        local_date="2026-07-18",
        calories_consumed=210.0, protein_consumed=3.0,
        calorie_target=2090, protein_target=170,
        calories_remaining=1880.0, protein_remaining=167.0,
        hours_left=14.0, sleep_quality=None, fasting=False,
        medications_today=[], flags={}, active_constraints=[],
        workout_active=False, workout_completed=False,
        usual_workout_time=None, is_usual_workout_day=False,
        latest_health_date=None, goal_computed=True, profile={},
        goal={"calories": 2090, "protein": 170},
    )
    text = await health_jobs.build_morning_briefing_text(USER_ID, ctx)
    assert "['" not in text and "']" not in text  # no Python list literal
    assert "עדכון בוקר —" in text
    headline = text.splitlines()[0]
    assert "[" not in headline and "]" not in headline
    assert "שבת" in headline  # the actual weekday name is rendered


def test_fa6_weekday_label_is_joined_to_string() -> None:
    from noam_coach.services.weekdays import weekday_labels_he

    labels = weekday_labels_he([5])  # Saturday index
    assert isinstance(labels, list)  # the source is a list...
    joined = "".join(labels)
    assert joined and "[" not in joined  # ...and must be joined before display


# ---------------------------------------------------------------------------
# F-A9 — unified photo persistence + differentiated missing-image observability
# ---------------------------------------------------------------------------


async def test_fa9_persist_emits_storage_evidence(db: Database, tmp_path: Path) -> None:
    from noam_coach.services.media_persistence import persist_meal_photo

    path = tmp_path / "food.jpg"
    sha = await persist_meal_photo(
        USER_ID, b"jpeg-bytes-here", path, provider_file_unique_id="AQADabc",
    )
    assert path.exists()
    persisted = [
        e for e in await event_log.list_events(db, USER_ID, event=STATE_MUTATED)
        if e.properties.get("action") == "persisted"
    ]
    assert len(persisted) == 1
    props = persisted[0].properties
    assert props["storage_ref"] == str(path)
    assert props["provider_file_unique_id"] == "AQADabc"  # the metadata the audit found missing
    assert props["sha256_prefix"] == sha[:16]
    assert props["byte_size"] == len(b"jpeg-bytes-here")


async def test_fa9_missing_image_is_classified_not_silent(db: Database, tmp_path: Path) -> None:
    from noam_coach.services.media_persistence import (
        classify_missing_image,
        persist_meal_photo,
    )

    present = tmp_path / "present.jpg"
    await persist_meal_photo(USER_ID, b"bytes", present)
    assert await classify_missing_image(USER_ID, image_path=str(present)) == "present"

    # A path recorded but the file gone, with no deletion event → storage gap.
    gone = tmp_path / "gone.jpg"
    assert await classify_missing_image(
        USER_ID, image_path=str(gone), media_sha_prefix="deadbeefdeadbeef",
    ) == "storage_object_gone"

    # Only a Telegram id, never persisted.
    assert await classify_missing_image(
        USER_ID, image_path=None, provider_file_unique_id="AQADxyz",
    ) == "provider_only"

    # Nothing recorded at all.
    assert await classify_missing_image(USER_ID, image_path=None) == "no_reference"


async def test_fa9_reject_deletion_is_classified_as_deleted_on_reject(
    db: Database, tmp_path: Path
) -> None:
    """The MEDIA_003 shape: persisted, then deleted by reject — the trace
    now explains the absence instead of leaving it a silent mystery."""
    from noam_coach.services.media_persistence import (
        classify_missing_image,
        persist_meal_photo,
    )

    image = tmp_path / "rejected.jpg"
    sha = await persist_meal_photo(USER_ID, b"reject-me", image)
    approval_id = await _create_meal_approval(db, image=str(image))
    await _press(f"reject_meal:{approval_id}:r0")
    assert not image.exists()

    verdict = await classify_missing_image(
        USER_ID, image_path=str(image), media_sha_prefix=sha[:16],
    )
    assert verdict == "deleted_on_reject"  # not "storage_object_gone" — explained


# ---------------------------------------------------------------------------
# F-A8 — health status: two dates measure different things, stated honestly
# ---------------------------------------------------------------------------


async def test_fa8_stale_export_is_labeled_not_contradictory(db: Database) -> None:
    from noam_coach.bot import checkins as checkins_bot

    checkins_bot.DB = db
    # The production shape: imported today, but the newest sample is a month old.
    await db.execute(
        "INSERT INTO audit(user_id, action, entity, entity_id, details, created_at) "
        "VALUES(?, 'health_import', 'health', '0', '{}', ?)",
        (USER_ID, "2026-07-18T06:00:00+00:00"),
    )
    await db.execute(
        "INSERT INTO health(user_id, external_id, sample_type, value, unit, "
        "start_time, end_time, created_at) VALUES(?, 'x1', 'steps', 5000, 'count', ?, ?, ?)",
        (USER_ID, "2026-06-15T00:00:00+00:00", "2026-06-15T23:59:00+00:00", "2026-07-18T06:00:00+00:00"),
    )
    text = await checkins_bot.build_health_status_text(USER_ID)
    # Each date is labeled by what it MEANS, not two bare dates that look contradictory.
    assert "מתי נטען הקובץ" in text
    assert "תאריך המדידה" in text
    assert "2026-07-18" in text and "2026-06-15" in text
    # And the lag is flagged as a stale export, the single honest interpretation.
    assert "אינו עדכני" in text


async def test_fa8_fresh_export_has_no_stale_warning(db: Database) -> None:
    from noam_coach.bot import checkins as checkins_bot

    checkins_bot.DB = db
    await db.execute(
        "INSERT INTO audit(user_id, action, entity, entity_id, details, created_at) "
        "VALUES(?, 'health_import', 'health', '0', '{}', ?)",
        (USER_ID, "2026-07-18T06:00:00+00:00"),
    )
    await db.execute(
        "INSERT INTO health(user_id, external_id, sample_type, value, unit, "
        "start_time, end_time, created_at) VALUES(?, 'x2', 'steps', 5000, 'count', ?, ?, ?)",
        (USER_ID, "2026-07-18T00:00:00+00:00", "2026-07-18T08:00:00+00:00", "2026-07-18T06:00:00+00:00"),
    )
    text = await checkins_bot.build_health_status_text(USER_ID)
    assert "אינו עדכני" not in text  # same-day data → no false stale warning


async def test_fa4_finish_dialog_hides_full_when_incomplete(db: Database) -> None:
    from noam_coach.bot import callback_session as session_bot

    session_bot.DB = db
    plan = {"exercises": [{"name": "x", "sets": 4}, {"name": "y", "sets": 4},
                          {"name": "z", "sets": 4}]}  # 12 planned
    import json as _json

    session_id = await db.execute(
        "INSERT INTO sessions(user_id, code, name, plan, status, exercise_index, "
        "set_number, started_at) VALUES(?, 'B', 'אימון B', ?, 'active', 0, 1, ?)",
        (USER_ID, _json.dumps(plan), utc_now()),
    )
    await db.execute(
        "INSERT INTO sets(session_id, exercise_id, exercise_name, set_number, weight, "
        "reps, rir, source, created_at) VALUES(?, 0, 'x', 1, 54, 10, -1, 'telegram', ?)",
        (session_id, utc_now()),
    )
    session = await db.fetch_one("SELECT * FROM sessions WHERE id=?", (session_id,))

    query = FakeQuery()
    handled = await session_bot._handle_session_lifecycle_actions(
        query, SimpleNamespace(bot=None), USER_ID, "finish",
        ["finish", str(session_id), "0", "1"], session_id, session,
        _json.loads(session["plan"]), {}, 0.0, 0,
    )
    assert handled is True
    callbacks = _markup_callbacks(query.edit_markups[-1])
    # No "full" option is offered for an incomplete workout...
    assert not any(cb.endswith(":full") for cb in callbacks)
    # ...and the dialog says it will be marked partial.
    assert any("ייסמן כ" in text and "חלקי" in text for text in query.edits)


# ---------------------------------------------------------------------------
# 2026-07-19 falafel/schnitzel root-cause audit — the exact reanalysis shape
# from private trace events 1225/1226: the AI turned "3 כדור" into grams=3.0
# with quantity_source="user", and the deterministic override recalculated
# calories 15.0 → 8.4 while preserving the impossible weight. Batch 1 pins
# that this shape is blocked REGARDLESS of quantity_source or calorie stage.
# ---------------------------------------------------------------------------


def _event_1225_item(calories: float, quantity_source: str) -> FoodItem:
    return FoodItem(
        name="שניצל", grams=3.0, calories=calories, protein=0.7, carbs=0.3,
        fat=0.5, confidence=1.0, quantity_count=3.0, quantity_unit="כדור",
        quantity_source=quantity_source,
    )


@pytest.mark.parametrize("calories", [15.0, 8.4])  # raw AI (1225) / post-override (1226)
@pytest.mark.parametrize("quantity_source", ["user", "visual_count", "estimate"])
def test_rc_count_written_as_grams_blocked_for_any_source(
    calories: float, quantity_source: str
) -> None:
    """quantity_count=3 must never imply grams=3 — including when the AI
    mislabels the count as a user-supplied quantity (the incident's exact
    'quantity_source="user"' shape must not bypass the gate)."""
    from noam_coach.services.meal_plausibility import check_item

    issues = check_item(_event_1225_item(calories, quantity_source))
    assert [i.code for i in issues] == ["count_written_as_grams"]
    assert issues[0].severity == "block"


@pytest.mark.parametrize("quantity_source", ["user", "visual_count"])
def test_rc_event_1225_meal_cannot_validate(quantity_source: str) -> None:
    """The full incident meal (schnitzel 3 g + fried eggplant side) is
    blocked at validation for both source labels — the '3 גרם / 8 קל׳'
    render can never be approved again."""
    from noam_coach.services.meal_validation import validate_meal_analysis

    incident = MealAnalysis(
        meal_name="שניצל + חציל מטוגן",
        items=[
            _event_1225_item(8.4, quantity_source),
            FoodItem(name="חציל מטוגן", grams=100.0, calories=120.0,
                     protein=1.0, carbs=8.0, fat=9.0, confidence=0.8),
        ],
        confidence=0.9,
    )
    assert validate_meal_analysis(incident).blocked


def test_rc_plausible_schnitzel_count_with_real_weight_passes() -> None:
    """False-positive guard: three schnitzels at a realistic total weight
    (count=3, grams=540) must pass — the gate blocks the count-as-grams
    signature, not counted foods as such."""
    from noam_coach.services.meal_plausibility import check_item

    plausible = FoodItem(
        name="שניצל", grams=540.0, calories=1188.0, protein=96.0, carbs=42.0,
        fat=70.0, confidence=0.9, quantity_count=3.0, quantity_unit="יחידות",
        quantity_source="visual_count",
    )
    assert check_item(plausible) == []
