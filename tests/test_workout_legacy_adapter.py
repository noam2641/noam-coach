"""Batch 7 (workout-selection architecture): the legacy compatibility adapter.

New screens stopped minting legacy callbacks in Batch 4, but Telegram messages
are permanent -- a card sent months ago still has a live `startworkout:A`
button. This suite drives those already-issued callbacks through the real
router and asserts they now resolve with the SAME catalog identity,
normalization, override and staleness rules as the `wk:` flow.

Four outcomes x the legacy prefixes (plan section K):

  mapped              unique code match -> the real session identity
  ambiguous           2+ sessions share the code -> selector, never a guess
  template_fallback   no plan at all + known PLANS code -> legacy behavior
  stale               code matches nothing current -> refuse + refresh

Also covers the retirement policy (minting stopped / honoring continues),
telemetry, duplicate delivery, and the DEBUG_APPEND_ONLY_MESSAGES condition
where historical keyboards stay clickable.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

import coach_bot
import user_model
from config import TZ
from db import Database
from exercise_plans import PLANS
from helpers import utc_now
from noam_coach.bot import callback_plans as callback_plans_bot
from noam_coach.bot import ui as ui_bot
from noam_coach.bot import workout_compat
from noam_coach.services import workout_catalog
from noam_coach.services.weekdays import WEEKDAY_SCHEMA_VERSION, local_weekday


class FakeMessage:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.markups: list[Any] = []

    async def reply_text(self, text: str, reply_markup: Any = None, parse_mode: str | None = None) -> None:
        del parse_mode
        self.texts.append(text)
        self.markups.append(reply_markup)


class FakeQuery:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.reply_markups: list[Any] = []
        self.answers: list[str | None] = []
        self.message = FakeMessage()

    async def edit_message_text(self, text: str, reply_markup: Any = None, parse_mode: str | None = None) -> None:
        del parse_mode
        self.messages.append(text)
        self.reply_markups.append(reply_markup)

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        del show_alert
        self.answers.append(text)


def _ctx() -> Any:
    return type("Ctx", (), {"bot": object()})()


def _callbacks(markup: Any) -> list[str]:
    return [btn.callback_data for row in markup.inline_keyboard for btn in row]


async def _make_db(tmp_path: Path, name: str) -> Database:
    db = Database(str(tmp_path / f"{name}.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1, 'Test', NULL, ?)",
        (utc_now(),),
    )
    return db


def _bind(monkeypatch: pytest.MonkeyPatch, db: Database) -> None:
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(ui_bot, "DB", db)


def _freeze_clock(monkeypatch: pytest.MonkeyPatch, moment: datetime) -> None:
    """Pin the production session clock to ``moment`` for one test.

    Session start/complete stamp ``started_at``/``ended_at`` with ``utc_now()``
    -- the real wall clock, which takes no injectable argument. Scenarios that
    assert over a fixed fixture window need the written rows to land inside it;
    otherwise the reads come back empty purely because the calendar moved on.

    ``@runtime_bound`` refreshes referenced globals from the ``coach_bot``
    facade before each call, so patching the facade is what actually reaches the
    production write path. ``monkeypatch`` restores it afterwards.
    """
    frozen = moment.astimezone(timezone.utc).isoformat()
    monkeypatch.setattr(coach_bot, "utc_now", lambda: frozen)


def _today_at(moment: datetime) -> str:
    """UTC ISO stamp for *today* at ``moment``'s time of day.

    The repeat-today gate compares against ``datetime.now(TZ)`` (its caller
    passes no ``now``), so a row seeded at the fixture date is not "today" and
    the gate never engages.
    """
    today = datetime.now(TZ).replace(
        hour=moment.hour, minute=moment.minute, second=0, microsecond=0,
    )
    return today.astimezone(timezone.utc).isoformat()


def _ex(exercise_id: str, name: str, **over: Any) -> dict[str, Any]:
    base = {
        "id": exercise_id, "name": name, "sets": 2, "rmin": 8, "rmax": 12,
        "rest": 90, "weight": 50.0, "inc": 2.5, "cues": [], "alts": [], "muscle": "test",
    }
    base.update(over)
    return base


async def _seed_tier1(
    db: Database, now: datetime, *, codes: list[str], exercises_by_code: dict[str, list[dict[str, Any]]] | None = None
) -> int:
    weekday = local_weekday(now)
    exercises_by_code = exercises_by_code or {}
    sessions = []
    for i, code in enumerate(codes):
        sessions.append({
            "index": i, "weekday": (weekday + i) % 7, "weekday_schema": WEEKDAY_SCHEMA_VERSION,
            "weekday_name": "test", "time": "18:00", "minutes": 60, "code": code,
            "name": f"אימון {code}",
            "exercises": exercises_by_code.get(code, [_ex(f"{code.lower()}_ex", f"Exercise {code}")]),
        })
    payload = {"frequency": len(codes), "sessions": sessions}
    plan_id = await db.execute(
        """
        INSERT INTO plan_versions(
            user_id, plan_type, title, strategy, fit_score, status, payload,
            rationale, tradeoffs, assumptions, based_on, created_at, activated_at
        ) VALUES(1, 'workout', 'Test Plan', 'balanced', 1, 'active', ?, '[]', '[]', '[]', '{}', ?, ?)
        """,
        (json.dumps(payload, ensure_ascii=False), utc_now(), utc_now()),
    )
    await db.execute(
        "INSERT INTO active_plans(user_id, plan_type, plan_id, updated_at) VALUES(1, 'workout', ?, ?)",
        (plan_id, utc_now()),
    )
    await user_model.set_fact(
        db, 1, "active_workout_plan", {"plan_id": plan_id, **payload},
        kind=user_model.KIND_FACT, source=user_model.SOURCE_USER, confirmed=True,
    )
    return int(plan_id)


async def _regenerate_tier1(
    db: Database, now: datetime, *, codes: list[str], exercises_by_code: dict[str, list[dict[str, Any]]] | None = None
) -> int:
    await db.execute("UPDATE plan_versions SET status='superseded' WHERE user_id=1 AND status='active'")
    await db.execute("DELETE FROM active_plans WHERE user_id=1 AND plan_type='workout'")
    return await _seed_tier1(db, now, codes=codes, exercises_by_code=exercises_by_code)


async def _seed_tier2(db: Database, now: datetime, *, codes: list[str]) -> dict[str, Any]:
    weekday = local_weekday(now)
    fact = {
        "frequency": len(codes), "method": "moving_weight_double_progression", "structure": "test",
        "sessions": [
            {"weekday": (weekday + i) % 7, "time": "18:00", "code": code, "name": PLANS[code]["name"]}
            for i, code in enumerate(codes)
        ],
        "days_source": "default", "availability_confirmed": False,
    }
    await user_model.set_fact(
        db, 1, "active_workout_plan", fact,
        kind=user_model.KIND_FACT, source=user_model.SOURCE_SYSTEM, confirmed=True,
    )
    return fact


async def _sessions(db: Database) -> list[Any]:
    return await db.fetch_all("SELECT * FROM sessions WHERE user_id=1 ORDER BY id", ())


async def _overrides(db: Database) -> list[dict[str, Any]]:
    rows = await db.fetch_all(
        "SELECT code, exercise_index, field, value, exercise_id FROM exercise_overrides WHERE user_id=1", ()
    )
    return [dict(r) for r in rows]


async def _legacy_events(db: Database) -> list[dict[str, Any]]:
    rows = await db.fetch_all(
        "SELECT * FROM product_events WHERE user_id=1 AND entity='workout_legacy_callback' ORDER BY id", ()
    )
    out = []
    for row in rows:
        props = json.loads(row["properties"]) if row["properties"] else {}
        out.append({**props, "_outcome_col": row["outcome"], "_id": row["id"],
                    "_interaction_id": row["interaction_id"], "_trace_id": row["trace_id"]})
    return out


# ---------------------------------------------------------------------------
# Pure adapter: parsing and code resolution.
# ---------------------------------------------------------------------------


def test_parse_legacy_callback_covers_every_prefix_and_rejects_malformed() -> None:
    assert workout_compat.parse_legacy_callback("startworkout:A") == {
        "action": "start", "code": "A", "prefix": "startworkout"}
    assert workout_compat.parse_legacy_callback("workout:F") == {
        "action": "overview", "code": "F", "prefix": "workout"}
    assert workout_compat.parse_legacy_callback("editparams_menu:B") == {
        "action": "edit_menu", "code": "B", "prefix": "editparams_menu"}
    assert workout_compat.parse_legacy_callback("editparams:A:2")["exercise_index"] == 2
    parsed = workout_compat.parse_legacy_callback("param:A:1:weight:-2.5")
    assert parsed["field"] == "weight" and parsed["delta"] == -2.5

    for bad in ("startworkout:", "workout:", "editparams:A", "editparams:A:x",
                "editparams:A:-1", "param:A:1:weight", "param:A:x:weight:1", "wk:sel:1:0", "menu:home"):
        assert workout_compat.parse_legacy_callback(bad) is None, bad


@pytest.mark.asyncio
async def test_resolve_legacy_code_reports_the_four_outcomes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path, "outcomes")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)

    # No plan at all: known template code -> template_fallback; unknown -> stale.
    assert (await workout_compat.resolve_legacy_code(db, 1, "A"))["outcome"] == "template_fallback"
    assert (await workout_compat.resolve_legacy_code(db, 1, "custom"))["outcome"] == "stale"

    # Unique match -> mapped, with a real identity.
    plan_id = await _seed_tier1(db, now, codes=["A", "B"])
    mapped = await workout_compat.resolve_legacy_code(db, 1, "A")
    assert mapped["outcome"] == "mapped"
    assert mapped["ref"].plan_id == plan_id and mapped["ref"].session_index == 0

    # A code not in the current plan is stale even though PLANS has it.
    assert (await workout_compat.resolve_legacy_code(db, 1, "F"))["outcome"] == "stale"


@pytest.mark.asyncio
async def test_resolve_legacy_code_reports_duplicate_codes_as_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ["F","F"] Tier-2 split cannot be disambiguated by a bare code --
    guessing is the defect this architecture removes."""
    db = await _make_db(tmp_path, "ambiguous")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    await _seed_tier2(db, now, codes=["F", "F"])

    resolution = await workout_compat.resolve_legacy_code(db, 1, "F")
    assert resolution["outcome"] == "ambiguous"
    assert resolution["matches"] == 2
    assert resolution["ref"] is None


# ---------------------------------------------------------------------------
# mapped: legacy start displays and starts the SAME workout.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_overview_and_start_agree_on_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The headline guarantee: an old card cannot display A and start B."""
    db = await _make_db(tmp_path, "agree")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    plan_id = await _seed_tier1(db, now, codes=["A", "B"], exercises_by_code={
        "A": [_ex("leg_press", "לחיצת רגליים")],
    })

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, "workout:A")
    assert "לחיצת רגליים" in query.messages[-1]
    assert f"wk:start:{plan_id}:0" in _callbacks(query.reply_markups[-1])

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, "startworkout:A")
    snapshot = json.loads((await _sessions(db))[0]["plan"])
    assert [e["id"] for e in snapshot["exercises"]] == ["leg_press"]
    assert snapshot["provenance"]["source"] == "active_plan"
    assert snapshot["provenance"]["plan_id"] == plan_id


@pytest.mark.asyncio
async def test_legacy_start_respects_the_active_session_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path, "legacyactive")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    await _seed_tier1(db, now, codes=["A", "B"])

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, "startworkout:A")
    assert len([s for s in await _sessions(db) if s["status"] == "active"]) == 1

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, "startworkout:B")
    active = [s for s in await _sessions(db) if s["status"] == "active"]
    assert len(active) == 1 and active[0]["code"] == "A"


@pytest.mark.asyncio
async def test_duplicate_legacy_start_delivery_creates_one_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path, "legacydup")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    await _seed_tier1(db, now, codes=["A"])

    for _ in range(3):
        query = FakeQuery()
        await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, "startworkout:A")

    assert len([s for s in await _sessions(db) if s["status"] == "active"]) == 1
    assert len(await _sessions(db)) == 1


@pytest.mark.asyncio
async def test_legacy_start_cannot_bypass_again_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A legacy button must not become an escape hatch around the Batch-5
    repeat-today gate."""
    db = await _make_db(tmp_path, "legacyagain")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    plan_id = await _seed_tier1(db, now, codes=["A"])
    iso = _today_at(now)
    await db.execute(
        "INSERT INTO sessions(user_id, code, name, plan, status, exercise_index, set_number, started_at, ended_at) "
        "VALUES(1, 'A', 'A', '{}', 'completed', 0, 1, ?, ?)", (iso, iso),
    )

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, "startworkout:A")

    assert not [s for s in await _sessions(db) if s["status"] == "active"]
    assert f"wk:start:{plan_id}:0:again" in _callbacks(query.reply_markups[-1])


# ---------------------------------------------------------------------------
# ambiguous / template_fallback / stale.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ambiguous_legacy_start_shows_the_selector_and_starts_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path, "ambstart")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    fact = await _seed_tier2(db, now, codes=["F", "F"])
    rev = workout_catalog.compute_fact_rev(fact)

    query = FakeQuery()
    assert await callback_plans_bot.handle_workout_setup_callback(
        query, _ctx(), 1, "startworkout:F") is True

    assert await _sessions(db) == []
    datas = _callbacks(query.reply_markups[-1])
    assert f"wk:fsel:{rev}:0" in datas and f"wk:fsel:{rev}:1" in datas


@pytest.mark.asyncio
async def test_template_fallback_preserved_for_plan_less_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no plan of either tier, a known template code still starts the
    template workout -- the honest legacy behavior, deliberately preserved."""
    db = await _make_db(tmp_path, "fallback")
    _bind(monkeypatch, db)

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, "workout:A")
    assert PLANS["A"]["exercises"][0]["name"] in query.messages[-1]

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, "startworkout:A")
    sessions = await _sessions(db)
    assert len(sessions) == 1 and sessions[0]["code"] == "A"
    snapshot = json.loads(sessions[0]["plan"])
    assert {e["id"] for e in snapshot["exercises"]} == {
        e["id"] for e in PLANS["A"]["exercises"]}


@pytest.mark.asyncio
async def test_stale_legacy_code_refuses_and_mutates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A code that no longer exists in the user's plan must refuse -- never
    silently start the global template with that name."""
    db = await _make_db(tmp_path, "legacystale")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    await _seed_tier1(db, now, codes=["A"])
    await _regenerate_tier1(db, now, codes=["B"])  # "A" is gone

    for data in ("workout:A", "startworkout:A", "editparams_menu:A", "param:A:0:weight:2.5"):
        query = FakeQuery()
        assert await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, data) is True
        assert any(a and "התוכנית התעדכנה" in a for a in query.answers), data

    assert await _sessions(db) == []
    assert await _overrides(db) == []


@pytest.mark.asyncio
async def test_custom_unknown_code_never_raises_on_any_legacy_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W5 on the legacy surface: a `custom` code (planning.py:851) must not
    reach a PLANS[code] hard lookup through ANY legacy prefix."""
    db = await _make_db(tmp_path, "legacycustom")
    _bind(monkeypatch, db)
    assert "custom" not in PLANS

    for data in ("workout:custom", "startworkout:custom", "editparams_menu:custom",
                 "editparams:custom:0", "param:custom:0:weight:2.5"):
        query = FakeQuery()
        assert await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, data) is True
        assert await _sessions(db) == [], data
        assert await _overrides(db) == [], data


# ---------------------------------------------------------------------------
# Legacy editing resolves by verified identity.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_edit_menu_maps_to_the_resolved_sessions_exercises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path, "legacyeditmenu")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    plan_id = await _seed_tier1(db, now, codes=["A"], exercises_by_code={
        "A": [_ex("leg_press", "לחיצת רגליים"), _ex("calf", "עגלים")],
    })

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, "editparams_menu:A")

    datas = _callbacks(query.reply_markups[-1])
    assert f"wk:ex:{plan_id}:0:0" in datas and f"wk:ex:{plan_id}:0:1" in datas
    labels = " ".join(b.text for row in query.reply_markups[-1].inline_keyboard for b in row)
    assert "לחיצת רגליים" in labels


@pytest.mark.asyncio
async def test_legacy_param_write_targets_verified_exercise_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A legacy `param:` tap now writes with the stable exercise_id of the
    resolved session -- not a bare (code, index) pair."""
    db = await _make_db(tmp_path, "legacyparam")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    await _seed_tier1(db, now, codes=["A"], exercises_by_code={
        "A": [_ex("leg_press", "לחיצת רגליים", weight=80.0)],
    })

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, "param:A:0:weight:2.5")

    rows = await _overrides(db)
    assert len(rows) == 1
    assert rows[0]["code"] == "A"
    assert rows[0]["exercise_id"] == "leg_press"
    assert rows[0]["value"] == 82.5


@pytest.mark.asyncio
async def test_legacy_edit_causes_no_cross_code_override_leakage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W4 through the legacy surface: editing bench on A leaves bench on F."""
    db = await _make_db(tmp_path, "legacyleak")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    plan_id = await _seed_tier1(db, now, codes=["A", "F"], exercises_by_code={
        "A": [_ex("bench", "לחיצת חזה", weight=50.0)],
        "F": [_ex("bench", "לחיצת חזה", weight=50.0)],
    })

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, "param:A:0:weight:10")

    rows = await _overrides(db)
    assert [(r["code"], r["value"]) for r in rows] == [("A", 60.0)]

    f_ref = workout_catalog.WorkoutSelectionRef(
        tier="plan", plan_id=plan_id, fact_rev=None, session_index=1)
    resolved_f = await workout_catalog.resolve_selection(db, 1, f_ref)
    assert await workout_catalog.collect_overrides(db, 1, resolved_f.session) == {}


@pytest.mark.asyncio
async def test_legacy_param_never_writes_to_a_same_index_unrelated_exercise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The legacy callback carries index 0 of the OLD template. After the
    adapter resolves it against the current plan, the write must carry the
    CURRENT exercise's id -- and must not later leak onto a replacement."""
    db = await _make_db(tmp_path, "legacyindex")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    await _seed_tier1(db, now, codes=["A"], exercises_by_code={
        "A": [_ex("leg_press", "לחיצת רגליים", weight=80.0)],
    })
    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, "param:A:0:weight:5")
    assert (await _overrides(db))[0]["exercise_id"] == "leg_press"

    new_plan_id = await _regenerate_tier1(db, now, codes=["A"], exercises_by_code={
        "A": [_ex("squat", "סקוואט", weight=70.0)],  # different id, same index
    })
    ref = workout_catalog.WorkoutSelectionRef(
        tier="plan", plan_id=new_plan_id, fact_rev=None, session_index=0)
    resolved = await workout_catalog.resolve_selection(db, 1, ref)
    assert await workout_catalog.collect_overrides(db, 1, resolved.session) == {}


# ---------------------------------------------------------------------------
# Telemetry (plan section K).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_use_emits_exactly_one_event_with_correlation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from noam_coach.observability import obs_context

    db = await _make_db(tmp_path, "telemetry")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    plan_id = await _seed_tier1(db, now, codes=["A"])

    # Open the correlation scope the router opens for a real callback, so this
    # asserts the framework's correlation actually reaches the row rather than
    # asserting against an ambient-context-free call that could never have it.
    query = FakeQuery()
    with obs_context.interaction_scope(user_id=1) as scope:
        await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, "startworkout:A")

    events = await _legacy_events(db)
    assert len(events) == 1
    event = events[0]
    assert event["prefix"] == "startworkout"
    assert event["legacy_action"] == "start"
    assert event["code"] == "A"
    assert event["outcome"] == "mapped"
    assert event["resolved_source"] == "plan_version"
    assert event["tier"] == "plan"
    assert event["plan_id"] == plan_id
    assert event["session_index"] == 0
    # Correlation comes from the framework (emit.py injects obs_context's
    # trace/interaction ids), not hand-rolled in the adapter.
    assert event["_trace_id"] == scope.trace_id
    assert event["_interaction_id"] == scope.interaction_id


@pytest.mark.asyncio
async def test_each_outcome_is_recorded_distinctly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path, "telemetryoutcomes")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)

    query = FakeQuery()  # no plan -> template_fallback
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, "workout:A")

    await _seed_tier2(db, now, codes=["F", "F"])  # -> ambiguous
    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, "workout:F")

    query = FakeQuery()  # code absent from the fact -> stale
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, "workout:C")

    outcomes = [e["outcome"] for e in await _legacy_events(db)]
    assert outcomes == ["template_fallback", "ambiguous", "stale"]


@pytest.mark.asyncio
async def test_duplicate_delivery_does_not_produce_two_mutation_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two legacy taps emit two legacy-callback rows (both taps really
    happened), but only ONE workout_session_started -- the second is absorbed
    by the active-session guard."""
    db = await _make_db(tmp_path, "dupevents")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    await _seed_tier1(db, now, codes=["A"])

    for _ in range(2):
        query = FakeQuery()
        await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, "startworkout:A")

    assert len(await _legacy_events(db)) == 2
    started = await db.fetch_all(
        "SELECT * FROM product_events WHERE user_id=1 AND event LIKE '%workout_session_started%'", ())
    starts_via_track = await db.fetch_all(
        "SELECT * FROM events WHERE user_id=1 AND name='workout_session_started'", ()
    ) if await db.fetch_one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='events'", ()) else []
    assert len(started) + len(starts_via_track) <= 1
    assert len([s for s in await _sessions(db) if s["status"] == "active"]) == 1


@pytest.mark.asyncio
async def test_telemetry_failure_never_breaks_the_user_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An observability outage that swallowed a workout start would be worse
    than a missing row. emit_legacy_event is best-effort by construction."""
    db = await _make_db(tmp_path, "telemetryfail")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    await _seed_tier1(db, now, codes=["A"])

    async def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("observability is down")

    monkeypatch.setattr("noam_coach.observability.emit.emit_event", boom)

    query = FakeQuery()
    assert await callback_plans_bot.handle_workout_setup_callback(
        query, _ctx(), 1, "startworkout:A") is True
    assert len([s for s in await _sessions(db) if s["status"] == "active"]) == 1


# ---------------------------------------------------------------------------
# Retirement policy: minting stopped, honoring continues.
# ---------------------------------------------------------------------------


def test_v2_screens_mint_no_legacy_callbacks() -> None:
    """The retirement policy's first half. Every v2 builder must emit only
    `wk:`/`menu:` callbacks -- if a v2 screen mints `startworkout:` again, the
    legacy population can never reach zero and the adapter can never retire."""
    from noam_coach.bot.ui import (
        exercise_params_keyboard_v2,
        exercise_picker_keyboard_v2,
        workout_overview_keyboard_v2,
        workout_selector_keyboard,
    )

    ref = workout_catalog.WorkoutSelectionRef(
        tier="plan", plan_id=7, fact_rev=None, session_index=1)
    choice = workout_catalog.WorkoutChoice(
        ref=ref, code="A", name="A", weekday=0, done_today=False,
        recommended=True, reason="offer_today")
    resolved = workout_catalog.ResolvedWorkout(
        choice=choice, session={"code": "A", "name": "A", "exercises": [_ex("bench", "B")]})

    keyboards = [
        workout_selector_keyboard([choice]),
        workout_overview_keyboard_v2(ref),
        workout_overview_keyboard_v2(ref, single_choice=True),
        exercise_picker_keyboard_v2(resolved),
        exercise_params_keyboard_v2(ref, 0),
    ]
    for keyboard in keyboards:
        for data in _callbacks(keyboard):
            assert not data.startswith(workout_compat.LEGACY_PREFIXES), data


def test_retirement_criterion_is_documented_and_usage_based() -> None:
    """The criterion must be observable production usage, not 'we shipped the
    new code' -- old buttons live in chat history, not in our deployment."""
    criterion = workout_compat.RETIREMENT_CRITERION
    assert "30 consecutive days" in criterion
    assert "workout_legacy_callback" in criterion
    assert "stale" in criterion  # stale-only taps explicitly do not count as use


@pytest.mark.asyncio
async def test_adapter_still_honors_already_issued_callbacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The retirement policy's second half, stated as a test so a future
    batch cannot quietly delete the adapter: every legacy prefix is still
    routed, not ignored."""
    db = await _make_db(tmp_path, "stillhonored")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    await _seed_tier1(db, now, codes=["A"], exercises_by_code={"A": [_ex("bench", "לחיצה")]})

    for data in ("workout:A", "editparams_menu:A", "editparams:A:0", "param:A:0:weight:2.5"):
        query = FakeQuery()
        assert await callback_plans_bot.handle_workout_setup_callback(
            query, _ctx(), 1, data) is True, data
        assert query.messages, data


# ---------------------------------------------------------------------------
# DEBUG_APPEND_ONLY_MESSAGES: historical keyboards stay clickable.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_controls_under_append_only_mode_mutate_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With DEBUG_APPEND_ONLY_MESSAGES the tapped message is never edited
    away, so EVERY historical keyboard in the chat stays clickable -- stale
    v2 and legacy controls alike. Each must refuse without creating a session
    or writing an override.
    """
    db = await _make_db(tmp_path, "appendonly")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    old_plan_id = await _seed_tier1(db, now, codes=["A"], exercises_by_code={
        "A": [_ex("bench", "לחיצת חזה")]})
    await _regenerate_tier1(db, now, codes=["B"], exercises_by_code={"B": [_ex("row", "חתירה")]})

    stale_controls = [
        # v2 controls from a message rendered before the regeneration
        f"wk:sel:{old_plan_id}:0",
        f"wk:start:{old_plan_id}:0",
        f"wk:start:{old_plan_id}:0:again",
        f"wk:exm:{old_plan_id}:0",
        f"wk:ex:{old_plan_id}:0:0",
        f"wk:par:{old_plan_id}:0:0:weight:2.5",
        # legacy controls for a code that no longer exists
        "workout:A", "startworkout:A", "editparams_menu:A", "param:A:0:weight:2.5",
    ]
    for data in stale_controls:
        query = FakeQuery()
        assert await callback_plans_bot.handle_workout_setup_callback(
            query, _ctx(), 1, data) is True, data
        assert await _sessions(db) == [], data
        assert await _overrides(db) == [], data


@pytest.mark.asyncio
async def test_repeated_stale_taps_stay_side_effect_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path, "repeatstale")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    old_plan_id = await _seed_tier1(db, now, codes=["A"])
    await _regenerate_tier1(db, now, codes=["B"])

    for _ in range(5):
        query = FakeQuery()
        await callback_plans_bot.handle_workout_setup_callback(
            query, _ctx(), 1, f"wk:start:{old_plan_id}:0")
        query = FakeQuery()
        await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, "startworkout:A")

    assert await _sessions(db) == []
    assert await _overrides(db) == []


# ---------------------------------------------------------------------------
# Lifecycle invariants across the B4-B7 unit.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_active_session_stays_bound_to_its_snapshot_during_regeneration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path, "boundsnapshot")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    await _seed_tier1(db, now, codes=["A"], exercises_by_code={
        "A": [_ex("leg_press", "לחיצת רגליים")]})

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, "startworkout:A")
    before = (await _sessions(db))[0]["plan"]

    await _regenerate_tier1(db, now, codes=["A"], exercises_by_code={
        "A": [_ex("totally_different", "אחר")]})

    after = (await _sessions(db))[0]["plan"]
    assert after == before
    assert "leg_press" in after


@pytest.mark.asyncio
async def test_legacy_start_then_regenerate_then_complete_reaches_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E1 over the legacy-adapter path: a session started from an old button
    still completes and lands in history after a mid-session regeneration."""
    from noam_coach.bot import workout as workout_bot
    from noam_coach.services.workout_reconciliation import reconciled_workout_days

    db = await _make_db(tmp_path, "legacye1")
    _bind(monkeypatch, db)
    monkeypatch.setattr(workout_bot, "DB", db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    _freeze_clock(monkeypatch, now)
    await _seed_tier1(db, now, codes=["A"], exercises_by_code={
        "A": [_ex("leg_press", "לחיצת רגליים", sets=1)]})

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, "startworkout:A")
    session = (await _sessions(db))[0]

    await _regenerate_tier1(db, now, codes=["A", "B"])

    current = await db.fetch_one("SELECT * FROM sessions WHERE id=?", (session["id"],))
    completed, _ = await workout_bot.save_set(dict(current), 80.0, 10, 2, "test")
    assert completed is True

    finished = await db.fetch_one("SELECT * FROM sessions WHERE id=?", (session["id"],))
    assert finished["status"] == "completed"

    # E1: the completion is visible to the history/adherence consumers.
    start_utc = (now - timedelta(days=1)).astimezone(timezone.utc).isoformat()
    end_utc = (now + timedelta(days=1)).astimezone(timezone.utc).isoformat()
    days = await reconciled_workout_days(db, 1, start_utc, end_utc)
    assert days, "the completed session must appear in reconciled workout days"
