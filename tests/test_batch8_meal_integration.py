"""Batch 8 — Meal Interaction integration and full regression.

Batches 2-7 each proved one layer. This suite proves they compose: that a
correction travels intact from raw text through parsing, identity
enforcement, count interpretation, conversion-or-refusal, clarification,
validation, rendering, approval, persistence, day-state invalidation and
structured observability — and that every safety property survives the
journey rather than only holding in isolation.

The failure this program exists to prevent is a single incident shape:
``quantity_count=3`` / ``quantity_unit="כדור"`` / ``grams=3`` — a count
written into the weight field, rendered as a 3-gram meal, approvable and
persistable. Several tests here attack that shape from different layers.

Everything drives real handlers, real services and real persistence against
temporary databases. No production database, image store, configuration or
secret is touched.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import coach_bot
import event_log
from db import Database
from helpers import utc_now
from models import FoodItem, MealAnalysis
from noam_coach.observability import taxonomy
from noam_coach.observability.modes import ObservabilityMode, set_mode
from noam_coach.services.meal_quantity_diagnostics import (
    CONVERTED,
    classify_pre_conversion,
)

USER_ID = 1


@pytest.fixture
async def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Database:
    database = Database(str(tmp_path / "batch8.db"))
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
def _content_mode() -> Any:
    previous = set_mode(ObservabilityMode.CONTENT)
    yield
    set_mode(previous)


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


class _FakeQuery:
    def __init__(self) -> None:
        self.edits: list[tuple[str, Any]] = []
        self.answers: list[str] = []
        self.message = _FakeMessage("")
        self.from_user = SimpleNamespace(id=USER_ID)

    async def answer(self, text: str = "", **kwargs: Any) -> None:
        self.answers.append(text)

    async def edit_message_text(self, text: str, **kwargs: Any) -> None:
        self.edits.append((text, kwargs.get("reply_markup")))


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _falafel_analysis() -> MealAnalysis:
    return MealAnalysis(
        meal_name="פלאפל",
        confidence=0.7,
        items=[
            FoodItem(name="פלאפל", grams=150, calories=450, protein=13,
                     carbs=40, fat=25, confidence=0.8),
        ],
    )


def _incident_analysis() -> MealAnalysis:
    """count=3, unit="כדור", grams=3 — the shape the program exists to kill."""
    item = FoodItem(name="פלאפל", grams=3, calories=8, protein=1,
                    carbs=0, fat=0, confidence=0.8)
    item.quantity_count = 3
    item.quantity_unit = "כדור"
    item.quantity_source = "user_count"
    return MealAnalysis(meal_name="פלאפל", confidence=0.7, items=[item])


async def _create_approval(analysis: MealAnalysis, **extra: Any) -> str:
    from noam_coach.services import core as core_services

    payload = {"analysis": analysis.model_dump(), "image": None, "revision": 0}
    payload.update(extra)
    return await core_services.create_approval(USER_ID, "meal", payload)


async def _payload(db: Database, approval_id: str) -> dict[str, Any]:
    row = await db.fetch_one("SELECT payload FROM approvals WHERE id=?", (approval_id,))
    return json.loads(row["payload"])


async def _analysis_of(db: Database, approval_id: str) -> MealAnalysis:
    return MealAnalysis.model_validate((await _payload(db, approval_id))["analysis"])


async def _correct(
    monkeypatch: pytest.MonkeyPatch,
    approval_id: str,
    text: str,
    *,
    ai_result: MealAnalysis | None = None,
) -> None:
    """Drive the REAL correction-text handler.

    When ``ai_result`` is given the AI fallback is stubbed to return it, so
    the identity-enforcement layer can be exercised against a hostile
    re-analysis. Otherwise any AI call is a test failure.
    """
    from noam_coach.bot import meal_text as meal_text_bot

    async def fake_render(target: Any, user_id: int, approval_id_: str, **k: Any) -> None:
        return None

    monkeypatch.setattr(coach_bot, "render_meal", fake_render, raising=False)

    if ai_result is None:
        async def fail_ai(*a: Any, **k: Any) -> None:
            raise AssertionError("expected a deterministic correction")

        monkeypatch.setattr(coach_bot, "reanalyze_meal_with_text_and_image", fail_ai, raising=False)
        monkeypatch.setattr(coach_bot, "analyze_meal_text", fail_ai, raising=False)
    else:
        async def ai(*a: Any, **k: Any) -> MealAnalysis:
            return ai_result.model_copy(deep=True)

        monkeypatch.setattr(coach_bot, "reanalyze_meal_with_text_and_image", ai, raising=False)
        monkeypatch.setattr(coach_bot, "analyze_meal_text", ai, raising=False)

    message = _FakeMessage(text)
    update = SimpleNamespace(
        effective_message=message,
        effective_user=SimpleNamespace(id=USER_ID),
        effective_chat=SimpleNamespace(id=USER_ID),
    )
    await meal_text_bot._handle_meal_correction_text(update, USER_ID, approval_id, 0)


async def _events(db: Database, name: str | None = None) -> list[Any]:
    rows = await event_log.list_events(db, USER_ID, limit=800)
    return [r for r in rows if name is None or r.event == name]


def _names(analysis: MealAnalysis) -> list[str]:
    return [item.name for item in analysis.items]


# ===========================================================================
# PHASE 3 — End-to-end identity matrix
# ===========================================================================


_REPLACEMENT_FORMS = [
    "לא פלאפל, שניצל",
    "לא פלאפל אלא שניצל",
    "זה שניצל, לא פלאפל",
    "תוריד פלאפל ותוסיף שניצל",
    "הסר פלאפל והוסף שניצל",
    "במקום פלאפל יש שניצל",
    "תחליף פלאפל בשניצל",
]


@pytest.mark.parametrize("text", _REPLACEMENT_FORMS)
def test_equivalent_replacement_language_reaches_one_canonical_constraint(
    text: str,
) -> None:
    """Every supported phrasing yields the same identity constraint.

    This is the Batch 2/3 promise: the user should not have to know which
    sentence shape the parser prefers.
    """
    from noam_coach.services.meal_identity import identity_constraints_from_texts

    constraints = identity_constraints_from_texts([text])
    rejected = {c.rejected for c in constraints}
    confirmed = {c.confirmed for c in constraints}

    assert "פלאפל" in rejected, f"{text!r} lost the rejected identity"
    assert "שניצל" in confirmed, f"{text!r} lost the confirmed identity"


@pytest.mark.asyncio
async def test_replacement_reaches_the_rendered_card(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A replacement correction changes the persisted draft identity."""
    approval_id = await _create_approval(_falafel_analysis())
    await _correct(monkeypatch, approval_id, "לא פלאפל, שניצל")

    names = _names(await _analysis_of(db, approval_id))
    assert any("שניצל" in name for name in names), names
    assert not any("פלאפל" in name for name in names), names


@pytest.mark.asyncio
async def test_remove_add_phrasing_is_not_remove_only(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """'תוריד X ותוסיף Y' must not silently drop the add half."""
    approval_id = await _create_approval(_falafel_analysis())
    await _correct(monkeypatch, approval_id, "תוריד פלאפל ותוסיף שניצל")

    names = _names(await _analysis_of(db, approval_id))
    assert names, "the meal was emptied — remove-add read as remove-only"
    assert any("שניצל" in name for name in names), names


@pytest.mark.asyncio
async def test_remove_only_is_not_misread_as_replacement(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    analysis = _falafel_analysis()
    analysis.items.append(
        FoodItem(name="טחינה", grams=30, calories=180, protein=5,
                 carbs=6, fat=16, confidence=0.8)
    )
    approval_id = await _create_approval(analysis)
    await _correct(monkeypatch, approval_id, "בלי טחינה")

    names = _names(await _analysis_of(db, approval_id))
    assert not any("טחינה" in name for name in names), names
    assert any("פלאפל" in name for name in names), "removal ate an unrelated item"


@pytest.mark.asyncio
async def test_replacement_of_replacement_reaches_the_latest_identity(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """פלאפל → שניצל → חזה עוף ends at חזה עוף."""
    approval_id = await _create_approval(_falafel_analysis())
    await _correct(monkeypatch, approval_id, "לא פלאפל, שניצל")
    await _correct(monkeypatch, approval_id, "לא שניצל, חזה עוף")

    names = _names(await _analysis_of(db, approval_id))
    assert any("חזה עוף" in name for name in names), names
    assert not any("פלאפל" in name for name in names), names


@pytest.mark.asyncio
async def test_rejected_identity_cannot_return_through_ai_reanalysis(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The audit's headline failure: AI re-introducing a rejected food.

    The user explicitly said it is not falafel. A later AI reanalysis that
    "helpfully" restores falafel must be overridden by the recorded identity
    constraint.
    """
    approval_id = await _create_approval(_falafel_analysis())
    await _correct(monkeypatch, approval_id, "לא פלאפל, שניצל")

    hostile = MealAnalysis(
        meal_name="פלאפל",
        confidence=0.9,
        items=[
            FoodItem(name="פלאפל", grams=150, calories=450, protein=13,
                     carbs=40, fat=25, confidence=0.9),
        ],
    )
    await _correct(monkeypatch, approval_id, "תוסיף קצת סלט", ai_result=hostile)

    names = _names(await _analysis_of(db, approval_id))
    assert not any("פלאפל" in name for name in names), (
        f"a rejected identity returned through AI reanalysis: {names}"
    )


@pytest.mark.asyncio
async def test_duplicate_identical_correction_does_not_double_apply(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same correction delivered twice must not compound the meal."""
    approval_id = await _create_approval(_falafel_analysis())
    await _correct(monkeypatch, approval_id, "לא פלאפל, שניצל")
    first = _names(await _analysis_of(db, approval_id))

    await _correct(monkeypatch, approval_id, "לא פלאפל, שניצל")
    second = _names(await _analysis_of(db, approval_id))

    assert first == second, f"duplicate correction changed the meal: {first} -> {second}"


# ===========================================================================
# PHASE 4 — Quantity and conversion matrix
# ===========================================================================


@pytest.mark.asyncio
async def test_count_is_never_copied_into_grams(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE root cause. '3 שניצלים' must never yield grams == 3."""
    analysis = MealAnalysis(
        meal_name="שניצל",
        confidence=0.7,
        items=[FoodItem(name="שניצל", grams=200, calories=400, protein=30,
                        carbs=20, fat=20, confidence=0.8)],
    )
    approval_id = await _create_approval(analysis)
    await _correct(monkeypatch, approval_id, "3 שניצלים")

    item = (await _analysis_of(db, approval_id)).items[0]
    assert item.grams != 3, "the count was written into the weight field"
    assert item.quantity_count == 3


@pytest.mark.asyncio
async def test_explicit_user_grams_remain_authoritative(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """'השניצל בערך 180 גרם' locks, and a later count cannot demote it."""
    analysis = MealAnalysis(
        meal_name="שניצל",
        confidence=0.7,
        items=[FoodItem(name="שניצל", grams=200, calories=400, protein=30,
                        carbs=20, fat=20, confidence=0.8)],
    )
    approval_id = await _create_approval(analysis)
    await _correct(monkeypatch, approval_id, "השניצל בערך 180 גרם")

    item = (await _analysis_of(db, approval_id)).items[0]
    assert item.grams == 180
    assert item.quantity_source in {"user", "user_grams"}

    # A count arriving afterwards must not overwrite the pinned grams.
    await _correct(monkeypatch, approval_id, "3 שניצלים")
    item = (await _analysis_of(db, approval_id)).items[0]
    assert item.grams == 180, "an explicit gram lock was demoted by a count"


@pytest.mark.asyncio
async def test_incident_shape_cannot_be_approved_as_a_three_gram_meal(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """count=3 / grams=3 must be refused at the persistence gate.

    Even if such a draft exists, validation must block approval rather than
    writing a 3-gram falafel portion into meal_items.
    """
    from noam_coach.bot.meals import persist_meal

    approval_id = await _create_approval(_incident_analysis())

    with pytest.raises(Exception) as excinfo:
        await persist_meal(USER_ID, approval_id)

    assert "לא הגיונית" in str(excinfo.value) or "כמות" in str(excinfo.value), (
        f"the incident shape was not blocked: {excinfo.value}"
    )

    rows = await db.fetch_all("SELECT * FROM meal_items", ())
    assert not rows, "a 3-gram incident item reached meal_items"


def test_conversion_and_clarification_share_one_authoritative_classifier() -> None:
    """Batch 6.1's contract, re-asserted at integration level."""
    from noam_coach.services.meal_clarification import _needs_clarification

    item = FoodItem(name="שניצל", grams=100, calories=200, protein=10,
                    carbs=10, fat=5, confidence=0.8)
    item.quantity_count = 3
    item.quantity_source = "user_count"

    diagnostic = classify_pre_conversion(item)
    assert diagnostic.outcome == CONVERTED
    assert _needs_clarification(item) == "", (
        "a cleanly convertible count raised an unnecessary clarification"
    )


def test_conflict_precedence_outranks_conversion_outcome() -> None:
    """grams == count is asked about even when conversion would succeed."""
    from noam_coach.services.meal_clarification import (
        REASON_COUNT_GRAMS_CONFLICT,
        _needs_clarification,
    )

    item = FoodItem(name="שניצל", grams=3, calories=8, protein=1,
                    carbs=0, fat=0, confidence=0.8)
    item.quantity_count = 3
    item.quantity_unit = "כדור"
    item.quantity_source = "user_count"

    assert classify_pre_conversion(item).outcome == CONVERTED
    assert _needs_clarification(item) == REASON_COUNT_GRAMS_CONFLICT


@pytest.mark.parametrize(
    "grams,count,source",
    [
        ("bad", 3, "user_count"),
        (100, "three", "user_count"),
        (None, 3, "user_count"),
        (100, 3, None),
        ("", "", ""),
    ],
    ids=["bad-grams", "bad-count", "none-grams", "none-source", "all-empty"],
)
def test_malformed_quantity_values_never_raise(
    grams: Any, count: Any, source: Any
) -> None:
    """Malformed drafts degrade; they never raise through the pipeline."""
    from noam_coach.services.meal_clarification import _needs_clarification
    from noam_coach.services.meal_observability import render_quantity_mode

    class _Item:
        name = "שניצל"

    item = _Item()
    item.grams = grams
    item.quantity_count = count
    item.quantity_source = source

    assert isinstance(classify_pre_conversion(item).outcome, str)
    assert isinstance(_needs_clarification(item), str)
    assert isinstance(render_quantity_mode(item), str)


@pytest.mark.asyncio
async def test_unknown_unit_does_not_fabricate_grams(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unmapped unit must decline rather than borrow a weight."""
    analysis = MealAnalysis(
        meal_name="מרק קובה",
        confidence=0.7,
        items=[FoodItem(name="מרק קובה", grams=250, calories=300, protein=12,
                        carbs=30, fat=14, confidence=0.8)],
    )
    approval_id = await _create_approval(analysis)
    await _correct(monkeypatch, approval_id, "שלוש חתיכות")

    stored = await _analysis_of(db, approval_id)
    item = stored.items[0]
    # Grams were not invented from an unsupported portion model...
    assert item.grams == 250 or item.quantity_source != "count_derived"
    # ...and the user is asked rather than guessed at.
    assert stored.question, "an unconvertible count was silently accepted"


# ===========================================================================
# PHASE 5 — Clarification lifecycle matrix
# ===========================================================================


async def _seed_clarification(db: Database) -> str:
    from models import ClarificationOption
    from noam_coach.services.meal_clarification import (
        build_clarification_options,
        detect_quantity_clarification,
    )

    item = FoodItem(name="שניצל", grams=3, calories=8, protein=1,
                    carbs=0, fat=0, confidence=0.8)
    item.quantity_count = 3
    item.quantity_source = "user_count"
    analysis = MealAnalysis(meal_name="שניצל", confidence=0.7, items=[item])

    pending = detect_quantity_clarification(analysis)
    assert pending is not None
    analysis.question = pending.question
    analysis.options = [
        ClarificationOption(
            label=str(o.get("label") or ""),
            item_index=o.get("item_index"),
            item_name=pending.item_name,
            apply_kind=o.get("apply_kind"),
            set_grams=o.get("set_grams"),
            set_size=o.get("set_size"),
        )
        for o in build_clarification_options(pending)
    ]
    return await _create_approval(analysis, pending_clarification=pending.to_payload())


async def _tap(approval_id: str, index: int) -> _FakeQuery:
    from noam_coach.bot import callback_meals

    query = _FakeQuery()
    assert await callback_meals.handle_meal_callback(
        query, USER_ID, f"clarify:{approval_id}:{index}"
    )
    return query


async def _index_of(db: Database, approval_id: str, kind: str) -> int:
    analysis = await _analysis_of(db, approval_id)
    return next(i for i, o in enumerate(analysis.options) if o.apply_kind == kind)


@pytest.mark.asyncio
async def test_estimate_option_commits_exactly_one_mutation(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    approval_id = await _seed_clarification(db)

    async def fake_render(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr(coach_bot, "render_meal", fake_render, raising=False)
    await _tap(approval_id, await _index_of(db, approval_id, "size"))

    item = (await _analysis_of(db, approval_id)).items[0]
    assert item.grams > 3
    assert item.quantity_source == "user"

    resolved = await _events(db, taxonomy.MEAL_CLARIFICATION_RESOLVED)
    mutations = [e for e in resolved if e.properties.get("mutated") is True]
    assert len(mutations) == 1


@pytest.mark.asyncio
async def test_cancel_mutates_no_quantity(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    approval_id = await _seed_clarification(db)
    before = (await _analysis_of(db, approval_id)).items[0]

    async def fake_render(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr(coach_bot, "render_meal", fake_render, raising=False)
    await _tap(approval_id, await _index_of(db, approval_id, "cancel"))

    after_analysis = await _analysis_of(db, approval_id)
    after = after_analysis.items[0]
    assert after.grams == before.grams
    assert after.calories == before.calories
    assert after.quantity_count == before.quantity_count
    assert not after_analysis.question, "cancel must close the question"

    resolved = await _events(db, taxonomy.MEAL_CLARIFICATION_RESOLVED)
    assert resolved[-1].properties["status"] == "cancelled"
    assert resolved[-1].properties["mutated"] is False


@pytest.mark.asyncio
async def test_stale_callback_mutates_nothing(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    approval_id = await _seed_clarification(db)
    before = (await _analysis_of(db, approval_id)).items[0].grams

    async def fake_render(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr(coach_bot, "render_meal", fake_render, raising=False)
    await _tap(approval_id, 99)

    assert (await _analysis_of(db, approval_id)).items[0].grams == before
    resolved = await _events(db, taxonomy.MEAL_CLARIFICATION_RESOLVED)
    assert resolved[-1].properties["status"] == "stale"
    assert resolved[-1].properties["mutated"] is False


@pytest.mark.asyncio
async def test_replay_after_resolution_applies_no_second_mutation(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    approval_id = await _seed_clarification(db)

    async def fake_render(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr(coach_bot, "render_meal", fake_render, raising=False)
    index = await _index_of(db, approval_id, "size")
    await _tap(approval_id, index)
    grams_after_first = (await _analysis_of(db, approval_id)).items[0].grams

    await _tap(approval_id, index)
    assert (await _analysis_of(db, approval_id)).items[0].grams == grams_after_first

    resolved = await _events(db, taxonomy.MEAL_CLARIFICATION_RESOLVED)
    mutations = [e for e in resolved if e.properties.get("mutated") is True]
    assert len(mutations) == 1, "a replay applied a second quantity mutation"


@pytest.mark.asyncio
async def test_invalid_clarification_payload_is_recoverable(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A corrupt pending record must re-render, not raise."""
    approval_id = await _seed_clarification(db)
    payload = await _payload(db, approval_id)
    payload["pending_clarification"] = {"token": None, "garbage": True}
    await db.execute(
        "UPDATE approvals SET payload=? WHERE id=?",
        (json.dumps(payload, ensure_ascii=False), approval_id),
    )

    rendered: list[str] = []

    async def fake_render(target: Any, user_id: int, approval_id_: str, **k: Any) -> None:
        rendered.append(approval_id_)

    monkeypatch.setattr(coach_bot, "render_meal", fake_render, raising=False)
    await _tap(approval_id, await _index_of(db, approval_id, "size"))

    assert rendered, "a corrupt clarification payload broke the card"


@pytest.mark.asyncio
async def test_observability_failure_does_not_break_clarification(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    from noam_coach.observability import emit as emit_module

    async def boom(*a: Any, **k: Any) -> None:
        raise RuntimeError("observability down")

    monkeypatch.setattr(emit_module, "emit_event", boom)

    approval_id = await _seed_clarification(db)

    async def fake_render(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr(coach_bot, "render_meal", fake_render, raising=False)
    await _tap(approval_id, await _index_of(db, approval_id, "size"))

    item = (await _analysis_of(db, approval_id)).items[0]
    assert item.grams > 3, "telemetry failure cost the user their resolution"


# ===========================================================================
# PHASE 6 — Render, approval and persistence
# ===========================================================================


@pytest.mark.asyncio
async def test_persist_writes_corrected_items_once_and_invalidates_once(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One approval → one meal, one item set, one day-state invalidation."""
    from noam_coach.bot.meals import persist_meal
    from noam_coach.services import day_state_invalidation

    approval_id = await _create_approval(_falafel_analysis())
    await _correct(monkeypatch, approval_id, "לא פלאפל, שניצל")

    calls: list[str] = []
    original = day_state_invalidation.invalidate_day_projections

    async def counting(db_: Any, user_id: int, **kwargs: Any) -> Any:
        calls.append(str(kwargs.get("reason")))
        return await original(db_, user_id, **kwargs)

    monkeypatch.setattr(
        day_state_invalidation, "invalidate_day_projections", counting
    )

    meal_id = await persist_meal(USER_ID, approval_id)
    assert meal_id

    meals = await db.fetch_all("SELECT * FROM meals WHERE user_id=?", (USER_ID,))
    assert len(meals) == 1, "the meal was persisted more than once"

    items = await db.fetch_all("SELECT * FROM meal_items WHERE meal_id=?", (meal_id,))
    assert items, "no meal items persisted"
    names = [row["name"] for row in items]
    assert any("שניצל" in name for name in names), names
    assert not any("פלאפל" in name for name in names), (
        f"a rejected identity reached persistence: {names}"
    )

    assert len(calls) == 1, f"day-state invalidation ran {len(calls)} times"


@pytest.mark.asyncio
async def test_persisted_totals_match_the_corrected_items(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    from noam_coach.bot.meals import persist_meal

    approval_id = await _create_approval(_falafel_analysis())
    meal_id = await persist_meal(USER_ID, approval_id)

    meal = await db.fetch_one("SELECT * FROM meals WHERE id=?", (meal_id,))
    items = await db.fetch_all("SELECT * FROM meal_items WHERE meal_id=?", (meal_id,))
    assert abs(meal["calories"] - sum(r["calories"] for r in items)) < 1.0
    assert abs(meal["protein"] - sum(r["protein"] for r in items)) < 1.0


@pytest.mark.asyncio
async def test_second_approval_of_the_same_draft_is_idempotent(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A duplicate approve must not create a second meal.

    ``persist_meal`` claims the approval with a conditional UPDATE and
    returns ``None`` when the row was already claimed — deliberately a quiet
    no-op rather than an exception, so a double-tapped approve button cannot
    surface a spurious error to a user whose meal is already saved.
    """
    from noam_coach.bot.meals import persist_meal

    approval_id = await _create_approval(_falafel_analysis())
    first = await persist_meal(USER_ID, approval_id)
    assert first, "the first approval did not persist"

    second = await persist_meal(USER_ID, approval_id)
    assert second is None, "a re-approval was treated as a new save"

    meals = await db.fetch_all("SELECT * FROM meals WHERE user_id=?", (USER_ID,))
    assert len(meals) == 1, "a duplicate approval created a second meal"
    items = await db.fetch_all("SELECT * FROM meal_items", ())
    assert len({row["meal_id"] for row in items}) == 1, "items written twice"


@pytest.mark.asyncio
async def test_count_only_quantity_renders_as_a_count(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unconverted count is shown as a count, not as fabricated grams."""
    from noam_coach.bot import meals as meals_bot

    analysis = MealAnalysis(
        meal_name="מרק קובה",
        confidence=0.7,
        items=[FoodItem(name="מרק קובה", grams=250, calories=300, protein=12,
                        carbs=30, fat=14, confidence=0.8)],
    )
    approval_id = await _create_approval(analysis)
    await _correct(monkeypatch, approval_id, "שלוש חתיכות")

    query = _FakeQuery()
    await meals_bot.render_meal(query, USER_ID, approval_id)
    assert query.edits, "the card did not render"
    text = query.edits[-1][0]
    assert "3 גרם" not in text, "a count was rendered as grams"


# ===========================================================================
# PHASE 7 — Observability reconstruction gate
# ===========================================================================


@pytest.mark.asyncio
async def test_one_interaction_is_reconstructable_from_events(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole chain, joined by approval id and correlation."""
    from noam_coach.observability import obs_context

    approval_id = await _create_approval(_incident_analysis())
    with obs_context.interaction_scope(trace_id="tr_b8", interaction_id="ix_b8"):
        await _correct(monkeypatch, approval_id, "3 כדורי פלאפל")

    events = await _events(db)
    meal_events = [e for e in events if e.event.startswith("meal")]
    assert meal_events, "no meal events emitted"

    by_name = {e.event for e in meal_events}
    # Parser decision + conversion + plausibility + render mode.
    assert taxonomy.MEAL_CORRECTION_APPLIED in by_name
    assert taxonomy.MEAL_QUANTITY_CONVERSION_EVALUATED in by_name
    assert taxonomy.MEAL_PLAUSIBILITY_EVALUATED in by_name
    assert taxonomy.MEAL_RENDER_QUANTITY_MODE in by_name

    # Everything joins on the approval id and shares one correlation.
    correlated = [e for e in meal_events if e.trace_id == "tr_b8"]
    assert correlated, "meal events lost the ambient correlation"
    for event in correlated:
        assert event.entity_id == approval_id
        assert event.interaction_id == "ix_b8"

    # The conversion carries an authoritative reason and full provenance.
    conversion = [e for e in meal_events
                  if e.event == taxonomy.MEAL_QUANTITY_CONVERSION_EVALUATED][-1]
    assert conversion.properties["outcome"]
    assert conversion.properties["quantity_count"] == 3
    assert conversion.properties["quantity_unit"] == "כדור"
    assert conversion.properties["grams_before"] is not None

    # The final visible mode is recorded.
    render = [e for e in meal_events
              if e.event == taxonomy.MEAL_RENDER_QUANTITY_MODE][-1]
    assert render.properties["items"]
    assert render.properties["items"][0]["render_mode"]


@pytest.mark.asyncio
async def test_deterministic_and_ai_paths_are_distinguishable(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    approval_id = await _create_approval(_falafel_analysis())
    await _correct(monkeypatch, approval_id, "בלי שמן")

    applied = await _events(db, taxonomy.MEAL_CORRECTION_APPLIED)
    assert applied[-1].properties["resolution_source"] == "deterministic"
    assert applied[-1].properties["deterministic"] is True


@pytest.mark.asyncio
async def test_explicit_grams_are_distinguishable_from_count_derived(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Evidence strength separates a user lock from a derived estimate."""
    from noam_coach.services.meal_observability import evidence_strength

    assert evidence_strength("user") == "explicit_user"
    assert evidence_strength("count_derived") == "count_derived"
    assert evidence_strength("user") != evidence_strength("count_derived")


@pytest.mark.asyncio
async def test_no_duplicate_lifecycle_event_per_action(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One correction emits exactly one meal_correction_applied."""
    approval_id = await _create_approval(_falafel_analysis())
    await _correct(monkeypatch, approval_id, "בלי שמן")

    applied = await _events(db, taxonomy.MEAL_CORRECTION_APPLIED)
    assert len(applied) == 1, "append_event and emit_event both fired"


# ===========================================================================
# PHASE 8 — Privacy and event-schema compatibility
# ===========================================================================


SECRET_TEXT = "אכלתי פלאפל sk-batch8secretkeyvalue1234567890"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", [ObservabilityMode.METADATA, ObservabilityMode.CONTENT, ObservabilityMode.DEBUG]
)
async def test_no_secret_material_in_event_properties(
    db: Database, monkeypatch: pytest.MonkeyPatch, mode: ObservabilityMode
) -> None:
    """No mode — including DEBUG — may bypass redaction."""
    set_mode(mode)
    approval_id = await _create_approval(_falafel_analysis())
    await _correct(monkeypatch, approval_id, SECRET_TEXT)

    for event in await _events(db):
        blob = json.dumps(event.properties or {}, ensure_ascii=False)
        assert "sk-batch8secretkeyvalue1234567890" not in blob, (
            f"{event.event} leaked a secret in {mode}"
        )
        assert "base64" not in blob.lower()


@pytest.mark.asyncio
async def test_metadata_mode_stores_no_raw_correction_text(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_mode(ObservabilityMode.METADATA)
    approval_id = await _create_approval(_falafel_analysis())
    await _correct(monkeypatch, approval_id, "לא פלאפל, שניצל")

    applied = await _events(db, taxonomy.MEAL_CORRECTION_APPLIED)
    props = applied[-1].properties
    assert "content" not in props
    assert props.get("content_digest", {}).get("text", {}).get("sha256")
    assert "לא פלאפל, שניצל" not in json.dumps(props, ensure_ascii=False)


@pytest.mark.asyncio
async def test_off_mode_writes_no_meal_event(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_mode(ObservabilityMode.OFF)
    approval_id = await _create_approval(_falafel_analysis())
    await _correct(monkeypatch, approval_id, "בלי שמן")

    assert not await _events(db, taxonomy.MEAL_CORRECTION_APPLIED)


@pytest.mark.asyncio
async def test_legacy_non_sensitive_properties_remain_available(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The compatibility statement, asserted rather than asserted-about."""
    approval_id = await _create_approval(_falafel_analysis())
    await _correct(monkeypatch, approval_id, "בלי שמן")

    applied = await _events(db, taxonomy.MEAL_CORRECTION_APPLIED)
    props = applied[-1].properties
    assert props["deterministic"] is True
    assert props["revision"] == 1
    # The removed sensitive keys are genuinely gone.
    assert "text" not in props
    assert "correction_text" not in props


@pytest.mark.asyncio
async def test_error_event_carries_no_message_or_user_text(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    from noam_coach.bot import meal_text as meal_text_bot

    approval_id = await _create_approval(_falafel_analysis())

    def boom(*a: Any, **k: Any) -> None:
        raise RuntimeError(f"exploded on {SECRET_TEXT}")

    monkeypatch.setattr(
        meal_text_bot.meal_intelligence, "parse_meal_correction", boom, raising=False
    )

    async def fake_render(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr(coach_bot, "render_meal", fake_render, raising=False)

    message = _FakeMessage(SECRET_TEXT)
    update = SimpleNamespace(
        effective_message=message,
        effective_user=SimpleNamespace(id=USER_ID),
        effective_chat=SimpleNamespace(id=USER_ID),
    )
    await meal_text_bot._handle_meal_correction_text(update, USER_ID, approval_id, 0)

    errors = await _events(db, taxonomy.MEAL_CORRECTION_ERROR)
    assert errors
    blob = json.dumps(errors[-1].properties, ensure_ascii=False)
    assert errors[-1].properties["error_type"] == "RuntimeError"
    assert "sk-batch8secretkeyvalue1234567890" not in blob
    assert "exploded on" not in blob
    assert "error" not in errors[-1].properties, "the raw message key returned"
