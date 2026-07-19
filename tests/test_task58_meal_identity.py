"""TASK-58 — meal image identity correction and portion estimation.

Pins the exact production incident: "לא טחינה חציל במיונז" must become an
item-scoped identity constraint — tahini can never return for this meal,
unrelated items keep their identities AND quantities, the Israeli-food
override cannot re-canonicalize the generic word, and the final visible
output reflects eggplant-with-mayonnaise. Identity is asserted semantically
(names/constraints), never by list position.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import coach_bot
import event_log
import israeli_foods
import meal_intelligence
from db import Database
from helpers import utc_now
from models import FoodItem, MealAnalysis
from noam_coach.observability import ObservabilityMode, set_mode
from noam_coach.observability.emit import reset_observability_health
from noam_coach.observability.modes import reset_mode
from noam_coach.services.meal_identity import (
    high_impact_uncertainty_question,
    install_meal_identity_enforcement,
    uninstall_meal_identity_enforcement,
)

USER_ID = 1

INCIDENT_CORRECTION = "לא טחינה חציל במיונז"


def _incident_analysis(tahini_name: str = "טחינה גולמית") -> MealAnalysis:
    """The production draft: chicken/beans/rice/salad + the wrong tahini."""
    return MealAnalysis(
        meal_name="עוף צלוי + ירקות",
        confidence=0.7,
        items=[
            FoodItem(name="עוף צלוי", grams=150, calories=330, protein=31, carbs=0, fat=22, confidence=0.8),
            FoodItem(name="שעועית ירוקה", grams=70, calories=20, protein=1, carbs=4, fat=0, confidence=0.9),
            FoodItem(name="אורז לבן (מבושל)", grams=50, calories=70, protein=1.5, carbs=15, fat=0.2, confidence=0.85),
            FoodItem(name="סלט עגבניות ומלפפונים", grams=80, calories=20, protein=1, carbs=4, fat=0, confidence=0.9),
            FoodItem(name=tahini_name, grams=50, calories=298, protein=8.5, carbs=10.5, fat=27, confidence=0.5),
        ],
    )


def _names(analysis: MealAnalysis) -> list[str]:
    return [item.name for item in analysis.items]


@pytest.fixture
async def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Database:
    database = Database(str(tmp_path / "task58.db"))
    await database.init()
    await database.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(?, 'Test', NULL, ?)",
        (USER_ID, utc_now()),
    )
    monkeypatch.setattr(coach_bot, "DB", database)
    from config import SETTINGS

    monkeypatch.setattr(SETTINGS, "telegram_allowed_user_id", USER_ID, raising=False)
    return database


@pytest.fixture(autouse=True)
def _isolation():
    set_mode(ObservabilityMode.CONTENT)
    reset_observability_health()
    yield
    uninstall_meal_identity_enforcement()
    reset_mode()
    reset_observability_health()


# ---------------------------------------------------------------------------
# Structured identity-correction parsing (section A)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "rejected", "confirmed"),
    [
        ("לא טחינה חציל במיונז", "טחינה", "חציל במיונז"),
        ("לא טחינה, חציל במיונז", "טחינה", "חציל במיונז"),
        ("זה עוף, לא הודו", "הודו", "עוף"),
        ("זו קולה זירו ולא קולה רגילה", "קולה רגילה", "קולה זירו"),
        ("זה קוטג' ולא גבינה לבנה", "גבינה לבנה", "קוטג'"),
        ("לא הודו אלא עוף", "הודו", "עוף"),
    ],
)
def test_identity_corrections_parse_structured(text: str, rejected: str, confirmed: str) -> None:
    corrections = meal_intelligence.parse_meal_correction(text)
    assert [c.kind for c in corrections] == ["replace"]
    assert corrections[0].item_hint == rejected
    assert corrections[0].value == confirmed


def test_identity_parsing_does_not_break_other_correction_kinds() -> None:
    assert meal_intelligence.parse_meal_correction("בלי שמן")[0].kind == "remove"
    assert meal_intelligence.parse_meal_correction("חצי מהאורז")[0].kind == "scale"
    assert meal_intelligence.parse_meal_correction("100 גרם אורז")[0].kind == "quantity"


# ---------------------------------------------------------------------------
# Cases 1+2 — deterministic item-scoped replacement
# ---------------------------------------------------------------------------


def test_incident_correction_is_item_scoped(caplog: Any) -> None:
    analysis = _incident_analysis()
    before = {
        item.name: (item.grams, item.calories) for item in analysis.items
        if "טחינה" not in item.name
    }

    correction = meal_intelligence.parse_meal_correction(INCIDENT_CORRECTION)[0]
    corrected = meal_intelligence.apply_item_replacement_correction(analysis, correction)

    names = _names(corrected)
    assert not any("טחינה" in name for name in names)
    assert any("חציל במיונז" in name for name in names)
    # Unrelated items: same identity, same grams, same calories.
    for item in corrected.items:
        if "חציל" in item.name:
            continue
        assert (item.grams, item.calories) == before[item.name]
    # The replaced item keeps its observed grams but gets CURATED
    # eggplant-mayo macros — not raw-tahini macros ratio-scaled.
    eggplant = next(item for item in corrected.items if "חציל" in item.name)
    assert eggplant.grams == 50
    assert eggplant.calories < 150  # 165 kcal/100g * 50g, nothing like 298


# ---------------------------------------------------------------------------
# Cases 4+5 — Israeli-food canonicalization safety (section C)
# ---------------------------------------------------------------------------


def test_generic_tahini_does_not_canonicalize_to_raw() -> None:
    assert israeli_foods.lookup("טחינה") is None


def test_specific_raw_tahini_still_matches() -> None:
    match = israeli_foods.lookup("טחינה גולמית")
    assert match is not None and match.calories_per_100g == 595
    assert israeli_foods.lookup("raw tahini") is not None


def test_prepared_tahini_is_represented_separately() -> None:
    match = israeli_foods.lookup("טחינה מוכנה")
    assert match is not None and match.calories_per_100g < 300


def test_generic_query_never_matches_a_more_specific_alias() -> None:
    # Evidence-aware policy: specificity comes from the item name, never
    # from canonicalization ("לחם" must not receive "לחם אחיד" macros).
    assert israeli_foods.lookup("לחם") is None
    # ...while containment of a full alias still matches.
    assert israeli_foods.lookup("במבה אסם") is not None


# ---------------------------------------------------------------------------
# Cases 1+3 — enforcement after AI + overrides, across the lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rejected_identity_cannot_return_from_reanalysis(db: Database) -> None:
    """Adversarial: the model ignores the instruction and the override
    restores raw tahini — deterministic enforcement still removes it."""

    async def noncompliant_reanalyze(image_path: str, correction_text: str,
                                     locked_corrections: Any = None,
                                     nutrition_context: Any = None) -> MealAnalysis:
        return _incident_analysis("טחינה גולמית")  # tahini came back

    real = coach_bot.reanalyze_meal_with_text_and_image
    coach_bot.reanalyze_meal_with_text_and_image = noncompliant_reanalyze
    try:
        install_meal_identity_enforcement()
        result = await coach_bot.reanalyze_meal_with_text_and_image(
            "unused.jpg", INCIDENT_CORRECTION,
            locked_corrections=[], nutrition_context={"user_id": USER_ID},
        )
        names = _names(result)
        assert not any("טחינה" in name for name in names)
        assert any("חציל במיונז" in name for name in names)
        # Trace chain: the lock + the deterministic enforcement decision.
        events = await event_log.list_events(db, USER_ID)
        locks = [e for e in events if e.entity == "meal_identity_constraint"]
        assert locks and locks[0].properties["memory_class"] == "meal_instance"
        assert locks[0].properties["confirmed"] == "חציל במיונז"
        enforcement = [e for e in events if e.entity == "identity_enforcement"]
        assert enforcement and enforcement[-1].outcome == "enforced"
        assert enforcement[-1].properties["actions"]
    finally:
        uninstall_meal_identity_enforcement()
        coach_bot.reanalyze_meal_with_text_and_image = real


@pytest.mark.asyncio
async def test_correction_survives_a_later_unrelated_correction(db: Database) -> None:
    """Case 3: the identity lock persists via locked_corrections — a LATER
    AI reanalysis for an unrelated correction still cannot restore tahini."""

    async def noncompliant_reanalyze(image_path: str, correction_text: str,
                                     locked_corrections: Any = None,
                                     nutrition_context: Any = None) -> MealAnalysis:
        return _incident_analysis("טחינה")

    real = coach_bot.reanalyze_meal_with_text_and_image
    coach_bot.reanalyze_meal_with_text_and_image = noncompliant_reanalyze
    try:
        install_meal_identity_enforcement()
        result = await coach_bot.reanalyze_meal_with_text_and_image(
            "unused.jpg", "האורז היה עם קצת שמן",  # unrelated follow-up
            locked_corrections=[INCIDENT_CORRECTION],
            nutrition_context={"user_id": USER_ID},
        )
        names = _names(result)
        assert not any("טחינה" in name for name in names)
        assert any("חציל במיונז" in name for name in names)
    finally:
        uninstall_meal_identity_enforcement()
        coach_bot.reanalyze_meal_with_text_and_image = real


@pytest.mark.asyncio
async def test_enforcement_drops_duplicate_when_confirmed_item_exists(db: Database) -> None:
    """When the reanalysis already contains the confirmed food AND the
    rejected one, the rejected duplicate is dropped, not renamed into a dup."""
    analysis = _incident_analysis("טחינה גולמית")
    analysis.items.append(
        FoodItem(name="חציל במיונז", grams=100, calories=165, protein=1.5, carbs=6, fat=15, confidence=0.9)
    )

    async def noncompliant_reanalyze(*a: Any, **k: Any) -> MealAnalysis:
        return analysis

    real = coach_bot.reanalyze_meal_with_text_and_image
    coach_bot.reanalyze_meal_with_text_and_image = noncompliant_reanalyze
    try:
        install_meal_identity_enforcement()
        result = await coach_bot.reanalyze_meal_with_text_and_image(
            "unused.jpg", INCIDENT_CORRECTION, locked_corrections=[],
            nutrition_context={"user_id": USER_ID},
        )
        names = _names(result)
        assert names.count("חציל במיונז") == 1
        assert not any("טחינה" in name for name in names)
    finally:
        uninstall_meal_identity_enforcement()
        coach_bot.reanalyze_meal_with_text_and_image = real


# ---------------------------------------------------------------------------
# Case 6 — high-impact uncertainty gate (section H)
# ---------------------------------------------------------------------------


def test_high_impact_uncertain_item_triggers_targeted_question() -> None:
    analysis = _incident_analysis()  # tahini: conf 0.5, 298/738 kcal ≈ 40%
    question = high_impact_uncertainty_question(analysis)
    assert question is not None
    assert "טחינה" in question  # targeted at the uncertain item, not generic


def test_confident_meals_do_not_get_gate_questions() -> None:
    analysis = _incident_analysis()
    for item in analysis.items:
        item.confidence = 0.9
    assert high_impact_uncertainty_question(analysis) is None


def test_gate_respects_an_existing_model_question() -> None:
    analysis = _incident_analysis()
    analysis.question = "האם הסלט עם שמן זית?"
    assert high_impact_uncertainty_question(analysis) is None


@pytest.mark.asyncio
async def test_analyze_image_wrap_attaches_gate_question(db: Database) -> None:
    async def fake_analyze(image_bytes: bytes, user_id: Any = None,
                           nutrition_context: Any = None, caption: Any = None) -> MealAnalysis:
        return _incident_analysis()

    real = coach_bot.analyze_meal_image
    coach_bot.analyze_meal_image = fake_analyze
    try:
        install_meal_identity_enforcement()
        result = await coach_bot.analyze_meal_image(b"img", USER_ID)
        assert result.question and "טחינה" in result.question
        events = await event_log.list_events(db, USER_ID)
        gate = [e for e in events if e.entity == "meal_uncertainty_gate"]
        assert gate and gate[0].outcome == "clarification_required"
    finally:
        uninstall_meal_identity_enforcement()
        coach_bot.analyze_meal_image = real


# ---------------------------------------------------------------------------
# THE required regression journey — real production correction path
# ---------------------------------------------------------------------------


class _FakeSent:
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def edit_text(self, text: str, **kwargs: Any) -> "_FakeSent":
        self.texts.append(text)
        return self


class _FakeMessage:
    def __init__(self, text: str) -> None:
        self.text = text
        self.message_id = 42
        self.chat = SimpleNamespace(id=USER_ID)
        self.sent = _FakeSent()

    async def reply_text(self, text: str, **kwargs: Any) -> _FakeSent:
        self.sent.texts.append(text)
        return self.sent


@pytest.mark.asyncio
async def test_full_incident_journey_through_the_real_correction_handler(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Telegram draft → user sends the incident correction → the REAL
    _handle_meal_correction_text applies it deterministically (no AI call) →
    persisted approval has eggplant-mayo, no tahini, unrelated items intact."""
    from noam_coach.bot import meal_text as meal_text_bot
    from noam_coach.services import core as core_services

    approval_id = await core_services.create_approval(
        USER_ID, "meal",
        {"analysis": _incident_analysis().model_dump(), "image": None, "revision": 0},
    )

    rendered: list[str] = []

    async def fake_render(target: Any, user_id: int, approval_id_: str, **k: Any) -> None:
        rendered.append(approval_id_)

    monkeypatch.setattr(coach_bot, "render_meal", fake_render, raising=False)

    async def fail_ai(*a: Any, **k: Any) -> None:
        raise AssertionError("the incident correction must resolve deterministically")

    monkeypatch.setattr(coach_bot, "reanalyze_meal_with_text_and_image", fail_ai, raising=False)
    monkeypatch.setattr(coach_bot, "analyze_meal_text", fail_ai, raising=False)

    message = _FakeMessage(INCIDENT_CORRECTION)
    update = SimpleNamespace(
        effective_message=message,
        effective_user=SimpleNamespace(id=USER_ID),
        effective_chat=SimpleNamespace(id=USER_ID),
    )
    await meal_text_bot._handle_meal_correction_text(update, USER_ID, approval_id, 0)

    row = await core_services.fetch_approval(USER_ID, approval_id)
    assert row is not None
    saved = MealAnalysis.model_validate(row["data"]["analysis"])
    names = _names(saved)
    assert not any("טחינה" in name for name in names)
    assert any("חציל במיונז" in name for name in names)
    by_name = {item.name: item for item in saved.items}
    assert by_name["שעועית ירוקה"].grams == 70
    assert by_name["אורז לבן (מבושל)"].grams == 50
    assert by_name["סלט עגבניות ומלפפונים"].grams == 80
    assert by_name["עוף צלוי"].calories == 330
    # The correction is locked on the meal instance (its memory store).
    assert INCIDENT_CORRECTION in row["data"]["locked_corrections"]
    assert rendered == [approval_id]
    events = await event_log.list_events(db, USER_ID)
    applied = [e for e in events if e.event == "meal_correction_applied"]
    assert applied and applied[-1].properties["deterministic"] is True


# ---------------------------------------------------------------------------
# Batch 1 characterization (FINAL_MEAL_INTERACTION_IMPLEMENTATION_PLAN.md,
# 2026-07-19 falafel/schnitzel root-cause audit) — identity lifecycle.
# FROZEN tests pin behavior that already works; the strict xfail pins the
# remove/add phrasing gap that Batch 2/3 must close.
# ---------------------------------------------------------------------------


def _falafel_analysis(falafel_name: str = "פלאפל") -> MealAnalysis:
    return MealAnalysis(
        meal_name="פלאפל",
        confidence=0.85,
        items=[
            FoodItem(
                name=falafel_name, grams=120, calories=396, protein=13,
                carbs=31, fat=24, confidence=0.85,
            )
        ],
    )


def test_sequential_replacement_of_replacement_lands_on_latest_identity() -> None:
    """FROZEN: 'לא פלאפל, שניצל' then 'לא שניצל, חזה עוף' — the final item is
    חזה עוף, grams preserved through both renames, no rejected identity left."""
    constraints = meal_intelligence.identity_constraints_from_texts(
        ["לא פלאפל, שניצל", "לא שניצל, חזה עוף"]
    )
    assert [(c.rejected, c.confirmed) for c in constraints] == [
        ("פלאפל", "שניצל"),
        ("שניצל", "חזה עוף"),
    ]
    enforced, actions = meal_intelligence.enforce_identity_constraints(
        _falafel_analysis(), constraints
    )
    assert [item.name for item in enforced.items] == ["חזה עוף"]
    assert enforced.items[0].grams == 120
    assert [a["action"] for a in actions] == [
        "renamed_to_confirmed",
        "renamed_to_confirmed",
    ]


def test_duplicate_identity_correction_is_idempotent() -> None:
    """FROZEN: repeating the same correction produces one constraint and one
    enforcement action — never duplicate items or duplicate actions."""
    constraints = meal_intelligence.identity_constraints_from_texts(
        ["לא פלאפל, שניצל", "לא פלאפל, שניצל"]
    )
    assert len(constraints) == 1
    enforced, actions = meal_intelligence.enforce_identity_constraints(
        _falafel_analysis(), constraints
    )
    assert [item.name for item in enforced.items] == ["שניצל"]
    assert len(actions) == 1


def test_rejected_identity_matches_parenthetical_descriptor() -> None:
    """FROZEN: rejected 'פלאפל' must catch the descriptor-laden AI name
    'פלאפל כשר (3 כדורים)' (the exact initial-analysis name from the trace)."""
    analysis = _falafel_analysis("פלאפל כשר (3 כדורים)")
    constraints = meal_intelligence.identity_constraints_from_texts(["לא פלאפל, שניצל"])
    enforced, actions = meal_intelligence.enforce_identity_constraints(
        analysis, constraints
    )
    names = _names(enforced)
    assert not any("פלאפל" in name for name in names)
    assert any("שניצל" in name for name in names)
    assert actions


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    reason="Batch 2/3 TODO: remove/add phrasing does not yet create identity constraints, "
    "so post-AI enforcement cannot block the reintroduced rejected food",
)
async def test_remove_add_phrasing_blocks_reintroduced_identity(db: Database) -> None:
    """Desired (Batch 2/3): after 'תוריד פלאפל ותוסיף שניצל', a noncompliant
    reanalysis that returns falafel again must have it enforced away — the
    same guarantee 'לא פלאפל, שניצל' already provides today."""

    async def noncompliant_reanalyze(image_path: str, correction_text: str,
                                     locked_corrections: Any = None,
                                     nutrition_context: Any = None) -> MealAnalysis:
        analysis = _falafel_analysis()
        analysis.items.append(
            FoodItem(name="שניצל", grams=180, calories=430, protein=32,
                     carbs=14, fat=26, confidence=0.9)
        )
        return analysis

    real = coach_bot.reanalyze_meal_with_text_and_image
    coach_bot.reanalyze_meal_with_text_and_image = noncompliant_reanalyze
    try:
        install_meal_identity_enforcement()
        result = await coach_bot.reanalyze_meal_with_text_and_image(
            "unused.jpg", "תוריד פלאפל ותוסיף שניצל",
            locked_corrections=[], nutrition_context={"user_id": USER_ID},
        )
        names = _names(result)
        assert not any("פלאפל" in name for name in names)
        assert any("שניצל" in name for name in names)
    finally:
        uninstall_meal_identity_enforcement()
        coach_bot.reanalyze_meal_with_text_and_image = real
