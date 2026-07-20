"""Batch 7 — meal observability, auditability and privacy.

Two things are under test, and they are equally important:

1. The lifecycle is RECONSTRUCTABLE. Raw correction → parser decision →
   conversion diagnostic → plausibility → clarification → render mode must be
   joinable from structured events, so the historical
   ``quantity_count=3`` / ``quantity_unit="כדור"`` / ``grams=3`` shape can be
   explained from evidence rather than inferred from traces.

2. It is SAFE. Raw user text must never sit in ``properties``; it belongs in
   ``emit_event``'s mode-governed ``content``. An observability failure must
   never break a correction.

Everything here drives the real handlers and the real emit boundary. Tests
that assert on hand-built payloads would prove nothing about production.
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

USER_ID = 1

# The exact incident text and shape from the 2026-07 audit.
INCIDENT_TEXT = "3 כדורי פלאפל"
SECRET_LOOKING_TEXT = "אכלתי פלאפל sk-abcdefghijklmnopqrstuvwxyz123456"


@pytest.fixture
async def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Database:
    database = Database(str(tmp_path / "batch7.db"))
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
    """Default the suite to CONTENT mode; individual tests override."""
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


def _incident_analysis() -> MealAnalysis:
    """The audit shape: a counted food whose grams equal the count."""
    item = FoodItem(
        name="פלאפל", grams=3, calories=8, protein=1, carbs=0, fat=0, confidence=0.8,
    )
    item.quantity_count = 3
    item.quantity_unit = "כדור"
    item.quantity_source = "user_count"
    return MealAnalysis(meal_name="פלאפל", confidence=0.7, items=[item])


def _plain_analysis() -> MealAnalysis:
    return MealAnalysis(
        meal_name="מרק קובה",
        confidence=0.7,
        items=[
            FoodItem(name="מרק קובה", grams=250, calories=300, protein=12,
                     carbs=30, fat=14, confidence=0.8),
        ],
    )


async def _create_approval(analysis: MealAnalysis) -> str:
    from noam_coach.services import core as core_services

    return await core_services.create_approval(
        USER_ID, "meal",
        {"analysis": analysis.model_dump(), "image": None, "revision": 0},
    )


async def _send_correction(
    monkeypatch: pytest.MonkeyPatch, approval_id: str, text: str
) -> None:
    """Run the REAL correction-text handler."""
    from noam_coach.bot import meal_text as meal_text_bot

    async def fake_render(target: Any, user_id: int, approval_id_: str, **k: Any) -> None:
        return None

    monkeypatch.setattr(coach_bot, "render_meal", fake_render, raising=False)

    async def fail_ai(*a: Any, **k: Any) -> None:
        raise AssertionError("this correction must resolve deterministically")

    monkeypatch.setattr(coach_bot, "reanalyze_meal_with_text_and_image", fail_ai, raising=False)
    monkeypatch.setattr(coach_bot, "analyze_meal_text", fail_ai, raising=False)

    message = _FakeMessage(text)
    update = SimpleNamespace(
        effective_message=message,
        effective_user=SimpleNamespace(id=USER_ID),
        effective_chat=SimpleNamespace(id=USER_ID),
    )
    await meal_text_bot._handle_meal_correction_text(update, USER_ID, approval_id, 0)


async def _events(db: Database, name: str | None = None) -> list[Any]:
    rows = await event_log.list_events(db, USER_ID, limit=500)
    if name is None:
        return list(rows)
    return [row for row in rows if row.event == name]


def _all_property_text(event: Any) -> str:
    """Every string in an event's properties, for leak scanning."""
    return json.dumps(event.properties or {}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Compatibility: names and existing properties survive the migration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_event_names_are_unchanged(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The three migrated events keep their original names verbatim."""
    assert taxonomy.MEAL_CORRECTION_APPLIED == "meal_correction_applied"
    assert taxonomy.MEAL_CORRECTION_ERROR == "meal_correction_error"
    assert taxonomy.MEAL_CLARIFICATION_RESOLVED == "meal_clarification_resolved"

    approval_id = await _create_approval(_plain_analysis())
    await _send_correction(monkeypatch, approval_id, "בלי שמן")

    assert await _events(db, "meal_correction_applied"), "renamed or lost"


@pytest.mark.asyncio
async def test_existing_properties_remain_available(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Readers of the pre-Batch-7 property keys keep working."""
    approval_id = await _create_approval(_plain_analysis())
    await _send_correction(monkeypatch, approval_id, "בלי שמן")

    applied = await _events(db, "meal_correction_applied")
    assert applied
    props = applied[-1].properties
    # The keys existing readers depend on (see tests/test_task58_meal_identity).
    assert props["deterministic"] is True
    assert props["revision"] == 1


@pytest.mark.asyncio
async def test_one_user_action_emits_one_lifecycle_event(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No duplicate legacy+canonical pair for the same action."""
    approval_id = await _create_approval(_plain_analysis())
    await _send_correction(monkeypatch, approval_id, "בלי שמן")

    applied = await _events(db, "meal_correction_applied")
    assert len(applied) == 1, "the event was emitted twice (legacy + canonical)"


# ---------------------------------------------------------------------------
# Privacy: raw text never lands in properties
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_raw_correction_text_never_appears_in_properties(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE privacy contract: user text is content, never properties."""
    approval_id = await _create_approval(_plain_analysis())
    await _send_correction(monkeypatch, approval_id, INCIDENT_TEXT)

    for event in await _events(db):
        properties = dict(event.properties or {})
        # `content` is the sanctioned, mode-governed home for user text.
        properties.pop("content", None)
        blob = json.dumps(properties, ensure_ascii=False)
        assert INCIDENT_TEXT not in blob, (
            f"raw correction text leaked into {event.event} properties"
        )


@pytest.mark.asyncio
async def test_metadata_mode_stores_no_raw_text(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """METADATA mode keeps digests only — no user words anywhere."""
    set_mode(ObservabilityMode.METADATA)
    approval_id = await _create_approval(_plain_analysis())
    await _send_correction(monkeypatch, approval_id, INCIDENT_TEXT)

    applied = await _events(db, "meal_correction_applied")
    assert applied
    props = applied[-1].properties
    assert "content" not in props, "METADATA mode stored raw content"
    # The digest still allows correlating identical corrections.
    digest = props.get("content_digest", {}).get("text", {})
    assert digest.get("sha256"), "no digest to correlate on"
    assert digest.get("chars") == len(INCIDENT_TEXT)
    assert INCIDENT_TEXT not in json.dumps(props, ensure_ascii=False)


@pytest.mark.asyncio
async def test_content_mode_keeps_text_under_content(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CONTENT mode follows emit_event's existing policy."""
    set_mode(ObservabilityMode.CONTENT)
    approval_id = await _create_approval(_plain_analysis())
    await _send_correction(monkeypatch, approval_id, INCIDENT_TEXT)

    applied = await _events(db, "meal_correction_applied")
    assert applied
    props = applied[-1].properties
    assert props["content"]["text"] == INCIDENT_TEXT
    assert props["content_digest"]["text"]["chars"] == len(INCIDENT_TEXT)


@pytest.mark.asyncio
async def test_off_mode_emits_nothing(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OFF mode writes no event, per existing emit_event policy."""
    set_mode(ObservabilityMode.OFF)
    approval_id = await _create_approval(_plain_analysis())
    await _send_correction(monkeypatch, approval_id, "בלי שמן")

    assert not await _events(db, "meal_correction_applied")


@pytest.mark.asyncio
async def test_secrets_are_redacted_by_the_existing_boundary(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A key-shaped token in user text is redacted by emit_event."""
    set_mode(ObservabilityMode.CONTENT)
    approval_id = await _create_approval(_plain_analysis())
    await _send_correction(monkeypatch, approval_id, SECRET_LOOKING_TEXT)

    for event in await _events(db):
        blob = json.dumps(event.properties or {}, ensure_ascii=False)
        assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in blob


@pytest.mark.asyncio
async def test_error_event_never_leaks_user_text_or_exception_string(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """meal_correction_error records a class and context, never words.

    The old payload stored ``str(exc)[:200]`` AND ``correction_text[:100]`` —
    a correction failure's message routinely embeds the user's own text.
    """
    from noam_coach.bot import meal_text as meal_text_bot

    approval_id = await _create_approval(_plain_analysis())

    def _boom(*a: Any, **k: Any) -> None:
        raise RuntimeError(f"failed while handling: {INCIDENT_TEXT}")

    monkeypatch.setattr(
        meal_text_bot.meal_intelligence, "parse_meal_correction", _boom, raising=False
    )

    async def fake_render(target: Any, user_id: int, approval_id_: str, **k: Any) -> None:
        return None

    monkeypatch.setattr(coach_bot, "render_meal", fake_render, raising=False)

    message = _FakeMessage(INCIDENT_TEXT)
    update = SimpleNamespace(
        effective_message=message,
        effective_user=SimpleNamespace(id=USER_ID),
        effective_chat=SimpleNamespace(id=USER_ID),
    )
    await meal_text_bot._handle_meal_correction_text(update, USER_ID, approval_id, 0)

    errors = await _events(db, "meal_correction_error")
    assert errors, "the error event must still be recorded"
    props = errors[-1].properties
    assert props["error_type"] == "RuntimeError"
    assert props["context"] == "meal_correction"
    blob = json.dumps(props, ensure_ascii=False)
    assert INCIDENT_TEXT not in blob, "user text leaked through the exception"
    assert "failed while handling" not in blob, "exception string leaked"


# ---------------------------------------------------------------------------
# Reconstructability: the evidence chain
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_incident_shape_is_fully_reconstructable(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """count=3 / unit="כדור" / grams=3 explained entirely from events."""
    approval_id = await _create_approval(_incident_analysis())
    await _send_correction(monkeypatch, approval_id, "3 כדורי פלאפל")

    conversions = await _events(db, taxonomy.MEAL_QUANTITY_CONVERSION_EVALUATED)
    assert conversions, "no conversion evidence"
    props = conversions[-1].properties
    # The authoritative reason, plus the count/unit provenance.
    assert props["outcome"], "no authoritative conversion outcome"
    assert props["quantity_count"] == 3
    assert props["quantity_unit"] == "כדור"
    assert props["grams_before"] is not None
    assert props["conversion_source"]
    assert props["evidence_strength"]

    # The card's final quantity mode is recorded and joinable.
    renders = await _events(db, taxonomy.MEAL_RENDER_QUANTITY_MODE)
    assert renders
    render_props = renders[-1].properties
    assert render_props["items"], "no per-item render evidence"
    assert render_props["items"][0]["render_mode"]
    # Everything joins on the approval id.
    assert renders[-1].entity_id == approval_id
    assert conversions[-1].entity_id == approval_id


@pytest.mark.asyncio
async def test_parser_decision_distinguishes_deterministic_from_ai(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """resolution_source separates a parsed correction from AI reanalysis."""
    approval_id = await _create_approval(_plain_analysis())
    await _send_correction(monkeypatch, approval_id, "בלי שמן")

    applied = await _events(db, "meal_correction_applied")
    props = applied[-1].properties
    assert props["resolution_source"] == "deterministic"
    assert props["deterministic"] is True
    assert "remove" in props["correction_kinds"]
    assert props["text_length"] == len("בלי שמן")


@pytest.mark.asyncio
async def test_plausibility_outcome_is_recorded(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The plausibility verdict and its codes are structured evidence."""
    approval_id = await _create_approval(_incident_analysis())
    await _send_correction(monkeypatch, approval_id, "3 כדורי פלאפל")

    checks = await _events(db, taxonomy.MEAL_PLAUSIBILITY_EVALUATED)
    assert checks
    props = checks[-1].properties
    assert isinstance(props["blocked"], bool)
    assert isinstance(props["blocking_codes"], list)


@pytest.mark.asyncio
async def test_render_mode_matches_the_actual_quantity_mode(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recorded render mode is the one the card really uses."""
    from noam_coach.services.meal_observability import render_quantity_mode

    approval_id = await _create_approval(_incident_analysis())
    await _send_correction(monkeypatch, approval_id, "3 כדורי פלאפל")

    row = await db.fetch_one("SELECT payload FROM approvals WHERE id=?", (approval_id,))
    analysis = MealAnalysis.model_validate(json.loads(row["payload"])["analysis"])
    expected = [render_quantity_mode(item) for item in analysis.items]

    renders = await _events(db, taxonomy.MEAL_RENDER_QUANTITY_MODE)
    recorded = [entry["render_mode"] for entry in renders[-1].properties["items"]]
    assert recorded == expected


# ---------------------------------------------------------------------------
# Clarification lifecycle: resolved / cancelled / duplicate / stale
# ---------------------------------------------------------------------------


async def _seed_clarified_approval() -> str:
    """An approval whose card carries an unresolved quantity question."""
    from models import ClarificationOption
    from noam_coach.services import core as core_services
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
            label=str(opt.get("label") or ""),
            item_index=opt.get("item_index"),
            item_name=pending.item_name,
            apply_kind=opt.get("apply_kind"),
            set_grams=opt.get("set_grams"),
            set_size=opt.get("set_size"),
        )
        for opt in build_clarification_options(pending)
    ]
    return await core_services.create_approval(
        USER_ID, "meal",
        {
            "analysis": analysis.model_dump(),
            "image": None,
            "revision": 0,
            "pending_clarification": pending.to_payload(),
        },
    )


async def _tap(approval_id: str, index: int) -> _FakeQuery:
    from noam_coach.bot import callback_meals

    query = _FakeQuery()
    handled = await callback_meals.handle_meal_callback(
        query, USER_ID, f"clarify:{approval_id}:{index}"
    )
    assert handled
    return query


async def _option_index(db: Database, approval_id: str, kind: str) -> int:
    row = await db.fetch_one("SELECT payload FROM approvals WHERE id=?", (approval_id,))
    analysis = MealAnalysis.model_validate(json.loads(row["payload"])["analysis"])
    return next(i for i, o in enumerate(analysis.options) if o.apply_kind == kind)


@pytest.mark.asyncio
async def test_resolution_records_a_real_mutation(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    approval_id = await _seed_clarified_approval()

    async def fake_render(target: Any, user_id: int, approval_id_: str, **k: Any) -> None:
        return None

    monkeypatch.setattr(coach_bot, "render_meal", fake_render, raising=False)
    await _tap(approval_id, await _option_index(db, approval_id, "size"))

    resolved = await _events(db, "meal_clarification_resolved")
    assert resolved
    props = resolved[-1].properties
    assert props["status"] == "resolved"
    assert props["mutated"] is True
    assert props["duplicate_response"] is False
    assert props["stale_response"] is False
    # Original property keys preserved for existing readers.
    assert props["reason"] and props["item"]
    assert props["grams"] is not None


@pytest.mark.asyncio
async def test_duplicate_delivery_records_no_second_mutation(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two deliveries of one tap produce exactly one mutation.

    Resolving clears the question, so the SECOND delivery of the same button
    finds the option gone and is classified ``stale`` rather than
    ``duplicate``. Both are zero-mutation outcomes and both are recorded —
    what must never happen is a second ``mutated=True``.
    """
    approval_id = await _seed_clarified_approval()

    async def fake_render(target: Any, user_id: int, approval_id_: str, **k: Any) -> None:
        return None

    monkeypatch.setattr(coach_bot, "render_meal", fake_render, raising=False)
    index = await _option_index(db, approval_id, "size")
    await _tap(approval_id, index)
    await _tap(approval_id, index)

    resolved = await _events(db, "meal_clarification_resolved")
    mutations = [e for e in resolved if e.properties.get("mutated") is True]

    assert len(mutations) == 1, "a duplicate delivery recorded two mutations"
    assert len(resolved) == 2, "the second delivery was not recorded at all"
    second = resolved[-1].properties
    assert second["mutated"] is False
    assert second["status"] in {"duplicate", "stale"}
    assert second["duplicate_response"] or second["stale_response"]


@pytest.mark.asyncio
async def test_repeat_tap_on_a_live_option_is_recorded_as_duplicate(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``duplicate`` branch proper: the option still exists, but the
    question behind it was already answered."""
    from noam_coach.bot import callback_meals

    approval_id = await _seed_clarified_approval()

    async def fake_render(target: Any, user_id: int, approval_id_: str, **k: Any) -> None:
        return None

    monkeypatch.setattr(coach_bot, "render_meal", fake_render, raising=False)
    index = await _option_index(db, approval_id, "size")
    await _tap(approval_id, index)

    # Restore the answered question's options WITHOUT clearing its resolved
    # state — exactly the shape an old keyboard replays against.
    row = await db.fetch_one("SELECT payload FROM approvals WHERE id=?", (approval_id,))
    payload = json.loads(row["payload"])
    from models import ClarificationOption
    from noam_coach.services.meal_clarification import (
        PendingClarification,
        build_clarification_options,
    )

    pending = PendingClarification.from_payload(payload["pending_clarification"])
    assert pending is not None and pending.is_resolved
    analysis = MealAnalysis.model_validate(payload["analysis"])
    analysis.question = pending.question
    analysis.options = [
        ClarificationOption(
            label=str(opt.get("label") or ""),
            item_index=opt.get("item_index"),
            item_name=pending.item_name,
            apply_kind=opt.get("apply_kind"),
            set_grams=opt.get("set_grams"),
            set_size=opt.get("set_size"),
        )
        for opt in build_clarification_options(pending)
    ]
    payload["analysis"] = analysis.model_dump()
    await db.execute(
        "UPDATE approvals SET payload=? WHERE id=?",
        (json.dumps(payload, ensure_ascii=False), approval_id),
    )

    grams_before = MealAnalysis.model_validate(payload["analysis"]).items[0].grams
    await _tap(approval_id, index)

    resolved = await _events(db, "meal_clarification_resolved")
    duplicates = [e for e in resolved if e.properties.get("status") == "duplicate"]
    assert duplicates, "a repeat tap on a live option was not recorded"
    assert duplicates[-1].properties["mutated"] is False
    assert duplicates[-1].properties["duplicate_response"] is True

    after = await db.fetch_one("SELECT payload FROM approvals WHERE id=?", (approval_id,))
    grams_after = MealAnalysis.model_validate(
        json.loads(after["payload"])["analysis"]
    ).items[0].grams
    assert grams_after == grams_before, "the duplicate tap mutated the draft"


@pytest.mark.asyncio
async def test_cancellation_is_recorded_and_preserves_the_draft(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancel must not look like a discarded draft."""
    approval_id = await _seed_clarified_approval()
    before = await db.fetch_one(
        "SELECT payload FROM approvals WHERE id=?", (approval_id,)
    )
    before_grams = MealAnalysis.model_validate(
        json.loads(before["payload"])["analysis"]
    ).items[0].grams

    async def fake_render(target: Any, user_id: int, approval_id_: str, **k: Any) -> None:
        return None

    monkeypatch.setattr(coach_bot, "render_meal", fake_render, raising=False)
    await _tap(approval_id, await _option_index(db, approval_id, "cancel"))

    resolved = await _events(db, "meal_clarification_resolved")
    props = resolved[-1].properties
    assert props["status"] == "cancelled"
    assert props["mutated"] is False
    # The evidence claims the draft was preserved — verify it really was.
    after = await db.fetch_one(
        "SELECT payload FROM approvals WHERE id=?", (approval_id,)
    )
    after_grams = MealAnalysis.model_validate(
        json.loads(after["payload"])["analysis"]
    ).items[0].grams
    assert after_grams == before_grams
    assert props["prior_grams"] == before_grams


@pytest.mark.asyncio
async def test_stale_tap_records_zero_mutation(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An old keyboard clicked after the question moved on."""
    approval_id = await _seed_clarified_approval()

    async def fake_render(target: Any, user_id: int, approval_id_: str, **k: Any) -> None:
        return None

    monkeypatch.setattr(coach_bot, "render_meal", fake_render, raising=False)
    await _tap(approval_id, 99)  # an index that no longer exists

    resolved = await _events(db, "meal_clarification_resolved")
    assert resolved
    props = resolved[-1].properties
    assert props["status"] == "stale"
    assert props["stale_response"] is True
    assert props["mutated"] is False


# ---------------------------------------------------------------------------
# Correlation and failure containment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_events_carry_the_ambient_correlation(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Correlation comes from the ambient scope, not a parallel id."""
    from noam_coach.observability import obs_context

    approval_id = await _create_approval(_incident_analysis())
    with obs_context.interaction_scope(trace_id="tr_batch7", interaction_id="ix_batch7"):
        await _send_correction(monkeypatch, approval_id, "3 כדורי פלאפל")

    emitted = [
        event for event in await _events(db)
        if event.event.startswith("meal")
    ]
    assert emitted, "no meal events emitted"
    correlated = [e for e in emitted if e.trace_id == "tr_batch7"]
    assert correlated, "events lost the ambient trace correlation"
    for event in correlated:
        assert event.interaction_id == "ix_batch7"


@pytest.mark.asyncio
async def test_telemetry_failure_does_not_break_the_correction(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The user's correction still applies when event writing explodes."""
    from noam_coach.observability import emit as emit_module

    async def exploding_emit(*a: Any, **k: Any) -> None:
        raise RuntimeError("observability backend down")

    monkeypatch.setattr(emit_module, "emit_event", exploding_emit)

    approval_id = await _create_approval(_plain_analysis())
    await _send_correction(monkeypatch, approval_id, "בלי שמן")

    row = await db.fetch_one("SELECT payload FROM approvals WHERE id=?", (approval_id,))
    payload = json.loads(row["payload"])
    assert payload["revision"] == 1, "the correction was lost to a telemetry failure"
    assert "בלי שמן" in payload["locked_corrections"]


@pytest.mark.asyncio
async def test_evidence_failure_does_not_break_the_correction(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken evidence builder must not cost the user their correction."""
    from noam_coach.services import meal_observability

    def _boom(*a: Any, **k: Any) -> None:
        raise RuntimeError("evidence builder failed")

    monkeypatch.setattr(meal_observability, "conversion_properties", _boom)

    approval_id = await _create_approval(_incident_analysis())
    await _send_correction(monkeypatch, approval_id, "3 כדורי פלאפל")

    row = await db.fetch_one("SELECT payload FROM approvals WHERE id=?", (approval_id,))
    assert json.loads(row["payload"])["revision"] == 1


@pytest.mark.asyncio
async def test_no_binary_or_image_payloads_are_stored(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No base64/image bytes reach properties."""
    approval_id = await _create_approval(_incident_analysis())
    await _send_correction(monkeypatch, approval_id, "3 כדורי פלאפל")

    for event in await _events(db):
        blob = _all_property_text(event)
        assert "base64" not in blob.lower()
        assert "data:image" not in blob.lower()
        # A whole serialized analysis payload is not evidence.
        assert len(blob) < 20000, f"{event.event} stored an oversized payload"


# ---------------------------------------------------------------------------
# The evidence builders are pure and safe
# ---------------------------------------------------------------------------


def test_render_quantity_mode_mirrors_the_card() -> None:
    """The three render modes match noam_coach/bot/meals.py::_quantity_text."""
    from noam_coach.services.meal_observability import (
        RENDER_MODE_COUNT_ONLY,
        RENDER_MODE_COUNT_WITH_ESTIMATE,
        RENDER_MODE_GRAMS,
        render_quantity_mode,
    )

    derived = FoodItem(name="שניצל", grams=450, calories=900, protein=50,
                       carbs=40, fat=30, confidence=0.8)
    derived.quantity_count = 3
    derived.quantity_source = "count_derived"
    assert render_quantity_mode(derived) == RENDER_MODE_COUNT_WITH_ESTIMATE

    counted = FoodItem(name="שניצל", grams=3, calories=8, protein=1,
                       carbs=0, fat=0, confidence=0.8)
    counted.quantity_count = 3
    counted.quantity_source = "user_count"
    assert render_quantity_mode(counted) == RENDER_MODE_COUNT_ONLY

    plain = FoodItem(name="אורז", grams=200, calories=260, protein=5,
                     carbs=56, fat=1, confidence=0.8)
    assert render_quantity_mode(plain) == RENDER_MODE_GRAMS


def test_evidence_strength_ranks_explicit_user_grams_highest() -> None:
    """Explicit user grams must be distinguishable from AI/count evidence."""
    from noam_coach.services.meal_observability import evidence_strength

    assert evidence_strength("user") == "explicit_user"
    assert evidence_strength("user_grams") == "explicit_user"
    assert evidence_strength("count_derived") == "count_derived"
    assert evidence_strength("user_count") == "stated_count"
    assert evidence_strength("visual_count") == "ai_visual_count"
    assert evidence_strength("estimate") == "ai_estimate"


def test_error_properties_never_include_the_message() -> None:
    """error_properties records a class, never str(exc)."""
    from noam_coach.services.meal_observability import error_properties

    exc = ValueError("the user wrote 3 כדורי פלאפל and it broke")
    props = error_properties(exc, context="meal_correction")
    blob = json.dumps(props, ensure_ascii=False)

    assert props["error_type"] == "ValueError"
    assert props["context"] == "meal_correction"
    assert "כדורי פלאפל" not in blob
    assert "broke" not in blob


def test_builders_tolerate_malformed_input() -> None:
    """Evidence builders never raise into a handler."""
    from noam_coach.services import meal_observability as mo

    class _Broken:
        name = None
        grams = "bad"
        quantity_count = "three"

    assert isinstance(mo.render_quantity_mode(_Broken()), str)
    assert isinstance(mo.plausibility_properties(None), dict)
    assert isinstance(mo.clarification_properties(None, status="stale"), dict)
    assert isinstance(mo.conversion_properties(None), dict)
    assert isinstance(
        mo.correction_parse_properties(None, used_deterministic=False), dict
    )
    assert isinstance(
        mo.render_mode_properties(SimpleNamespace(items=[_Broken()])), dict
    )
