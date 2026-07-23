"""Batch 8 (workout-selection architecture): the final integration, lifecycle
and regression gate.

Batches 4-7 built the deployable unit; this suite proves it stays coherent
across the things that actually break architectures in production --
regeneration mid-flow, completion after the source plan is superseded,
historical/malformed payloads, and the downstream readers (history,
adherence, Mini App) that must keep reading the immutable session row rather
than re-resolving through whatever plan is active now.

Organised by the Batch-8 scopes:

  1. regeneration lifecycle matrix
  2. done-today after regeneration (E2)
  3. missing/malformed exercise identity
  4. Mini App and operational-snapshot compatibility
  5. history, adherence and statistics
  6. observability assertions
  7. callback and architecture invariants

Everything runs through the real callback, database, catalog, session and
Mini App paths.
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
from noam_coach.bot import workout as workout_bot
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


def _freeze_clock(monkeypatch: pytest.MonkeyPatch, moment: datetime) -> None:
    """Pin the production session clock to ``moment`` for one test.

    The session start/complete paths stamp ``started_at``/``ended_at`` with
    ``utc_now()`` -- the real wall clock, which takes no injectable argument.
    These scenarios build their assertion windows from a fixed fixture date, so
    without pinning the clock the rows land outside the window being queried and
    every "did this happen today / in this range" read comes back empty. That is
    what made this suite start failing once the calendar moved past the fixture
    date, with no code change.

    ``@runtime_bound`` refreshes referenced globals from the ``coach_bot``
    facade immediately before each call, so patching the facade is what actually
    reaches the production write path -- patching a test-local import would not.
    ``monkeypatch`` restores it afterwards.
    """
    frozen = moment.astimezone(timezone.utc).isoformat()
    monkeypatch.setattr(coach_bot, "utc_now", lambda: frozen)


def _bind(monkeypatch: pytest.MonkeyPatch, db: Database) -> None:
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(ui_bot, "DB", db)
    monkeypatch.setattr(workout_bot, "DB", db)


def _ex(exercise_id: str | None, name: str, **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": exercise_id, "name": name, "sets": 1, "rmin": 8, "rmax": 12,
        "rest": 90, "weight": 50.0, "inc": 2.5, "cues": [], "alts": [], "muscle": "test",
    }
    base.update(over)
    if exercise_id is None:
        base.pop("id", None)
    return base


async def _seed_tier1(
    db: Database, now: datetime, *, codes: list[str],
    exercises_by_code: dict[str, list[dict[str, Any]]] | None = None,
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


async def _regenerate(
    db: Database, now: datetime, *, codes: list[str],
    exercises_by_code: dict[str, list[dict[str, Any]]] | None = None,
) -> int:
    """Copy-on-write regeneration: supersede the active row, INSERT a new one."""
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


async def _active(db: Database) -> Any:
    return await db.fetch_one("SELECT * FROM sessions WHERE user_id=1 AND status='active'", ())


async def _overrides(db: Database) -> list[dict[str, Any]]:
    rows = await db.fetch_all(
        "SELECT code, exercise_index, field, value, exercise_id FROM exercise_overrides WHERE user_id=1", ()
    )
    return [dict(r) for r in rows]


async def _complete_active_session(db: Database) -> int:
    """Drive the real save_set path until the session completes."""
    session = await _active(db)
    assert session is not None
    session_id = session["id"]
    for _ in range(40):
        current = await db.fetch_one("SELECT * FROM sessions WHERE id=?", (session_id,))
        if current["status"] != "active":
            break
        completed, _set_id = await workout_bot.save_set(dict(current), 60.0, 10, 2, "test")
        if completed:
            break
    return session_id


# ---------------------------------------------------------------------------
# Scope 1 -- regeneration lifecycle matrix.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_selector_then_regenerate_then_old_tier1_selection_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path, "m1")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    old_plan_id = await _seed_tier1(db, now, codes=["A", "B"])

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, "menu:workout")
    stale_control = f"wk:sel:{old_plan_id}:0"
    assert stale_control in _callbacks(query.reply_markups[-1])

    new_plan_id = await _regenerate(db, now, codes=["A", "B"])

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, stale_control)
    assert any(a and "התוכנית התעדכנה" in a for a in query.answers)
    assert await _sessions(db) == []
    assert any(d.startswith(f"wk:sel:{new_plan_id}:") for d in _callbacks(query.reply_markups[-1]))


@pytest.mark.asyncio
async def test_tier2_same_length_replacement_then_old_selection_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path, "m2")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    old_fact = await _seed_tier2(db, now, codes=["F", "F"])
    old_rev = workout_catalog.compute_fact_rev(old_fact)

    new_fact = await _seed_tier2(db, now, codes=["A", "B"])
    assert len(new_fact["sessions"]) == len(old_fact["sessions"])

    for data in (f"wk:fsel:{old_rev}:1", f"wk:fstart:{old_rev}:1",
                 f"wk:exm:{old_rev}:1", f"wk:par:{old_rev}:1:0:weight:5"):
        query = FakeQuery()
        assert await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, data) is True
        assert any(a and "התוכנית התעדכנה" in a for a in query.answers), data

    assert await _sessions(db) == []
    assert await _overrides(db) == []


@pytest.mark.asyncio
async def test_overview_then_regenerate_then_start_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path, "m3")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    old_plan_id = await _seed_tier1(db, now, codes=["A"])

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, f"wk:sel:{old_plan_id}:0")
    start_control = f"wk:start:{old_plan_id}:0"

    await _regenerate(db, now, codes=["A"])

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, start_control)
    assert await _sessions(db) == []
    assert any(a and "התוכנית התעדכנה" in a for a in query.answers)


@pytest.mark.asyncio
async def test_param_editor_then_regenerate_then_edit_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path, "m4")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    old_plan_id = await _seed_tier1(db, now, codes=["A"])

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, f"wk:ex:{old_plan_id}:0:0")

    await _regenerate(db, now, codes=["A"])

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(
        query, _ctx(), 1, f"wk:par:{old_plan_id}:0:0:weight:5")
    assert await _overrides(db) == []
    assert any(a and "התוכנית התעדכנה" in a for a in query.answers)

    # And the confirmed free-text path refuses too.
    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, "wparamtext:apply")
    assert await _overrides(db) == []


@pytest.mark.asyncio
async def test_active_session_survives_regeneration_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path, "m5")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    plan_id = await _seed_tier1(db, now, codes=["A"], exercises_by_code={
        "A": [_ex("leg_press", "לחיצת רגליים")]})

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, f"wk:start:{plan_id}:0")
    before = (await _active(db))["plan"]

    await _regenerate(db, now, codes=["B", "C"], exercises_by_code={
        "B": [_ex("row", "חתירה")], "C": [_ex("curl", "כפיפה")]})

    after = (await _active(db))["plan"]
    assert after == before
    assert "leg_press" in after


@pytest.mark.asyncio
async def test_started_workout_completes_after_source_plan_superseded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path, "m6")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    plan_id = await _seed_tier1(db, now, codes=["A"], exercises_by_code={
        "A": [_ex("leg_press", "לחיצת רגליים", sets=1)]})

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, f"wk:start:{plan_id}:0")
    await _regenerate(db, now, codes=["B"])

    session_id = await _complete_active_session(db)
    finished = await db.fetch_one("SELECT * FROM sessions WHERE id=?", (session_id,))
    assert finished["status"] == "completed"
    assert finished["ended_at"] is not None
    assert json.loads(finished["plan"])["exercises"][0]["id"] == "leg_press"


@pytest.mark.asyncio
async def test_recommendations_follow_the_new_plan_not_the_started_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path, "m7")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    plan_id = await _seed_tier1(db, now, codes=["A"])
    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, f"wk:start:{plan_id}:0")
    await _complete_active_session(db)

    new_plan_id = await _regenerate(db, now, codes=["X", "Y"], exercises_by_code={
        "X": [_ex("x_ex", "X")], "Y": [_ex("y_ex", "Y")]})

    choices = await workout_catalog.list_selectable_workouts(db, 1)
    assert {c.code for c in choices} == {"X", "Y"}
    assert all(c.ref.plan_id == new_plan_id for c in choices)


@pytest.mark.asyncio
async def test_later_overrides_do_not_mutate_active_or_completed_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path, "m8")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    plan_id = await _seed_tier1(db, now, codes=["A"], exercises_by_code={
        "A": [_ex("bench", "לחיצה", weight=50.0, sets=1)]})

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, f"wk:start:{plan_id}:0")
    active_before = (await _active(db))["plan"]

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(
        query, _ctx(), 1, f"wk:par:{plan_id}:0:0:weight:20")
    assert (await _active(db))["plan"] == active_before

    session_id = await _complete_active_session(db)
    completed_before = (await db.fetch_one("SELECT * FROM sessions WHERE id=?", (session_id,)))["plan"]

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(
        query, _ctx(), 1, f"wk:par:{plan_id}:0:0:weight:30")
    after = (await db.fetch_one("SELECT * FROM sessions WHERE id=?", (session_id,)))["plan"]
    assert after == completed_before


# ---------------------------------------------------------------------------
# Scope 2 -- done-today after regeneration (E2).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_banner_is_truthful_after_regeneration_into_different_codes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE E2 REGRESSION. Before Batch 8 the banner was derived from per-code
    done_today, so regenerating into a plan with DIFFERENT codes made the
    selector silently claim the user had not trained today. The banner now
    asks the completion-evidence source directly, which no plan change can
    invalidate."""
    db = await _make_db(tmp_path, "e2a")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    plan_id = await _seed_tier1(db, now, codes=["A"], exercises_by_code={
        "A": [_ex("a_ex", "A", sets=1)]})

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, f"wk:start:{plan_id}:0")
    await _complete_active_session(db)

    # Regenerate into completely different codes -- no code overlap at all.
    await _regenerate(db, now, codes=["X", "Y"], exercises_by_code={
        "X": [_ex("x_ex", "X")], "Y": [_ex("y_ex", "Y")]})

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, "menu:workout")
    assert "כבר התאמנת היום" in query.messages[-1], query.messages[-1]


@pytest.mark.asyncio
async def test_per_session_check_stays_identity_specific(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The banner is user-level; the ✅ mark is not. Only the session actually
    performed carries it."""
    db = await _make_db(tmp_path, "e2b")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    plan_id = await _seed_tier1(db, now, codes=["A", "B"])

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, f"wk:start:{plan_id}:0")
    await _complete_active_session(db)

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, "menu:workout")
    labels = [b.text for row in query.reply_markups[-1].inline_keyboard for b in row]
    done = [label for label in labels if "✅" in label]
    assert len(done) == 1 and "A" in done[0]


@pytest.mark.asyncio
async def test_alternative_sessions_stay_selectable_after_completing_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Do not silently suppress valid alternative sessions."""
    db = await _make_db(tmp_path, "e2c")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    plan_id = await _seed_tier1(db, now, codes=["A", "B", "C"])

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, f"wk:start:{plan_id}:0")
    await _complete_active_session(db)

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, "menu:workout")
    datas = [d for d in _callbacks(query.reply_markups[-1]) if d.startswith("wk:sel:")]
    assert len(datas) == 3


@pytest.mark.asyncio
async def test_starting_the_completed_session_again_still_requires_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path, "e2d")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    plan_id = await _seed_tier1(db, now, codes=["A", "B"])

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, f"wk:start:{plan_id}:0")
    await _complete_active_session(db)

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, f"wk:start:{plan_id}:0")
    assert await _active(db) is None
    assert f"wk:start:{plan_id}:0:again" in _callbacks(query.reply_markups[-1])


@pytest.mark.asyncio
async def test_starting_a_different_session_after_completing_one_is_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A DIFFERENT session is not "done today", so it starts directly -- the
    confirmation policy is per-identity, not a blanket daily lock."""
    db = await _make_db(tmp_path, "e2e")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    plan_id = await _seed_tier1(db, now, codes=["A", "B"])

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, f"wk:start:{plan_id}:0")
    await _complete_active_session(db)

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, f"wk:start:{plan_id}:1")
    active = await _active(db)
    assert active is not None and active["code"] == "B"


# ---------------------------------------------------------------------------
# Scope 3 -- missing / malformed exercise identity.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_exercise_id_renders_but_refuses_the_override_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Smallest safe behavior: normalization repairs the payload enough to
    RENDER, but a write is refused -- a NULL-id row would later be re-applied
    by template POSITION to whatever sits at that index."""
    db = await _make_db(tmp_path, "s3a")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    plan_id = await _seed_tier1(db, now, codes=["A"], exercises_by_code={
        "A": [_ex(None, "תרגיל ללא מזהה")]})

    query = FakeQuery()
    assert await callback_plans_bot.handle_workout_setup_callback(
        query, _ctx(), 1, f"wk:ex:{plan_id}:0:0") is True
    assert query.messages  # rendered, no exception

    query = FakeQuery()
    assert await callback_plans_bot.handle_workout_setup_callback(
        query, _ctx(), 1, f"wk:par:{plan_id}:0:0:weight:5") is True
    assert await _overrides(db) == []  # refused, nothing written


@pytest.mark.asyncio
async def test_duplicate_exercise_ids_do_not_crash_and_stay_code_scoped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path, "s3b")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    plan_id = await _seed_tier1(db, now, codes=["A"], exercises_by_code={
        "A": [_ex("bench", "לחיצה 1", weight=50.0), _ex("bench", "לחיצה 2", weight=50.0)]})

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(
        query, _ctx(), 1, f"wk:par:{plan_id}:0:0:weight:10")

    rows = await _overrides(db)
    assert len(rows) == 1 and rows[0]["exercise_id"] == "bench"
    # Documented consequence of the approved code+id model: a duplicate id
    # inside one session shares the override. planning.py:806-814 rejects/
    # repairs duplicates at generation time, so this is a defensive path.
    ref = workout_catalog.WorkoutSelectionRef(
        tier="plan", plan_id=plan_id, fact_rev=None, session_index=0)
    resolved = await workout_catalog.resolve_selection(db, 1, ref)
    assert await workout_catalog.collect_overrides(db, 1, resolved.session) == {
        "bench": {"weight": 60.0}}


@pytest.mark.asyncio
async def test_sparse_legacy_payload_renders_and_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path, "s3c")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    plan_id = await _seed_tier1(db, now, codes=["A"], exercises_by_code={
        "A": [{"id": "bench", "sets": 1, "muscle": "חזה"}]})

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, f"wk:start:{plan_id}:0")
    snapshot = json.loads((await _active(db))["plan"])
    for key in ("rmin", "rmax", "rest", "weight", "inc", "cues", "alts", "name"):
        assert key in snapshot["exercises"][0], key
    await workout_bot.show_session(FakeQuery(), 1, (await _active(db))["id"])


@pytest.mark.asyncio
async def test_custom_code_full_lifecycle_never_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path, "s3d")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    assert "custom" not in PLANS
    plan_id = await _seed_tier1(db, now, codes=["custom"], exercises_by_code={
        "custom": [_ex("weird", "מותאם", sets=1)]})

    for data in (f"wk:sel:{plan_id}:0", f"wk:exm:{plan_id}:0",
                 f"wk:par:{plan_id}:0:0:weight:5", f"wk:start:{plan_id}:0"):
        query = FakeQuery()
        assert await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, data) is True

    assert (await _active(db))["code"] == "custom"
    await _complete_active_session(db)


@pytest.mark.asyncio
async def test_exercise_index_beyond_the_displayed_session_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path, "s3e")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    plan_id = await _seed_tier1(db, now, codes=["A"], exercises_by_code={
        "A": [_ex("only", "יחיד")]})

    for data in (f"wk:ex:{plan_id}:0:5", f"wk:par:{plan_id}:0:5:weight:5"):
        query = FakeQuery()
        assert await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, data) is True
        assert await _overrides(db) == [], data


# ---------------------------------------------------------------------------
# Scope 4 + 5 -- Mini App, history, adherence.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_operational_snapshot_reads_the_session_not_the_active_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Mini App must never reinterpret a superseded plan as the source of
    an active snapshot."""
    import mini_api

    db = await _make_db(tmp_path, "mini1")
    _bind(monkeypatch, db)
    monkeypatch.setattr(mini_api, "DB", db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    plan_id = await _seed_tier1(db, now, codes=["A"], exercises_by_code={
        "A": [_ex("leg_press", "לחיצת רגליים")]})

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, f"wk:start:{plan_id}:0")
    await _regenerate(db, now, codes=["Z"], exercises_by_code={"Z": [_ex("z_ex", "Z")]})

    snapshot = await mini_api._operational_snapshot(1)
    assert snapshot["active_session"] is not None
    assert snapshot["latest_session"]["code"] == "A"
    assert snapshot["latest_session"]["status"] == "active"
    assert snapshot.get("current_load") != {"error": "active_session_plan_unreadable"}


@pytest.mark.asyncio
async def test_operational_snapshot_reads_an_old_session_without_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Backward compatibility: a session written by pre-Batch-4 code has no
    provenance key at all and must still be readable."""
    import mini_api

    db = await _make_db(tmp_path, "mini2")
    _bind(monkeypatch, db)
    monkeypatch.setattr(mini_api, "DB", db)
    legacy = {"name": "ישן", "exercises": [_ex("bench", "לחיצה")]}
    await db.execute(
        "INSERT INTO sessions(user_id, code, name, plan, status, exercise_index, set_number, started_at) "
        "VALUES(1, 'A', 'ישן', ?, 'active', 0, 1, ?)",
        (json.dumps(legacy, ensure_ascii=False), utc_now()),
    )

    snapshot = await mini_api._operational_snapshot(1)
    assert snapshot["latest_session"]["code"] == "A"
    assert snapshot["active_session"] is not None


@pytest.mark.asyncio
async def test_provenance_is_not_exposed_by_the_operational_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Provenance stays internal -- it is not part of any approved API shape."""
    import mini_api

    db = await _make_db(tmp_path, "mini3")
    _bind(monkeypatch, db)
    monkeypatch.setattr(mini_api, "DB", db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    plan_id = await _seed_tier1(db, now, codes=["A"])
    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, f"wk:start:{plan_id}:0")

    snapshot = await mini_api._operational_snapshot(1)
    assert "provenance" not in json.dumps(snapshot, ensure_ascii=False, default=str)


@pytest.mark.asyncio
async def test_full_lifecycle_start_regenerate_complete_history_adherence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The end-to-end E1 chain over the new architecture: every downstream
    consumer reads the immutable session row, not the current active plan."""
    import mini_api
    import planning
    from noam_coach.services.workout_reconciliation import reconciled_workout_days

    db = await _make_db(tmp_path, "life")
    _bind(monkeypatch, db)
    monkeypatch.setattr(mini_api, "DB", db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    _freeze_clock(monkeypatch, now)
    plan_id = await _seed_tier1(db, now, codes=["A"], exercises_by_code={
        "A": [_ex("leg_press", "לחיצת רגליים", sets=1)]})

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, f"wk:start:{plan_id}:0")
    await _regenerate(db, now, codes=["Z"], exercises_by_code={"Z": [_ex("z_ex", "Z")]})
    session_id = await _complete_active_session(db)

    finished = await db.fetch_one("SELECT * FROM sessions WHERE id=?", (session_id,))
    assert finished["status"] == "completed"
    assert json.loads(finished["plan"])["exercises"][0]["id"] == "leg_press"

    start_utc = (now - timedelta(days=1)).astimezone(timezone.utc).isoformat()
    end_utc = (now + timedelta(days=1)).astimezone(timezone.utc).isoformat()
    days = await reconciled_workout_days(db, 1, start_utc, end_utc)
    assert days, "completed session must appear in history"

    adherence = await planning.adherence_snapshot(db, 1, start_utc, end_utc)
    assert adherence is not None

    snapshot = await mini_api._operational_snapshot(1)
    assert snapshot["latest_session"]["code"] == "A"
    assert snapshot["latest_session"]["status"] == "completed"


# ---------------------------------------------------------------------------
# Scope 6 -- observability assertions.
# ---------------------------------------------------------------------------


async def _events(db: Database, entity: str) -> list[dict[str, Any]]:
    rows = await db.fetch_all(
        "SELECT * FROM product_events WHERE user_id=1 AND entity=? ORDER BY id", (entity,))
    out = []
    for row in rows:
        props = json.loads(row["properties"]) if row["properties"] else {}
        out.append({**props, "_trace_id": row["trace_id"], "_outcome": row["outcome"]})
    return out


@pytest.mark.asyncio
async def test_stale_action_is_recorded_as_refusal_not_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path, "obs1")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    old_plan_id = await _seed_tier1(db, now, codes=["A"])
    await _regenerate(db, now, codes=["A"])

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(
        query, _ctx(), 1, f"wk:start:{old_plan_id}:0")

    assert await _sessions(db) == []
    refusals = await db.fetch_all(
        "SELECT * FROM product_events WHERE user_id=1 AND properties LIKE '%stale_plan_reference%'", ())
    assert refusals, "a stale start must leave refusal evidence"


@pytest.mark.asyncio
async def test_no_full_workout_payload_is_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Events carry identity and counts, never whole plan bodies."""
    db = await _make_db(tmp_path, "obs2")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    plan_id = await _seed_tier1(db, now, codes=["A"], exercises_by_code={
        "A": [_ex("secret_exercise_marker", "סודי")]})

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, f"wk:start:{plan_id}:0")

    rows = await db.fetch_all("SELECT properties FROM product_events WHERE user_id=1", ())
    blob = " ".join(r["properties"] or "" for r in rows)
    assert "secret_exercise_marker" not in blob


@pytest.mark.asyncio
async def test_duplicate_delivery_does_not_double_count_a_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path, "obs3")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    plan_id = await _seed_tier1(db, now, codes=["A"])

    for _ in range(3):
        query = FakeQuery()
        await callback_plans_bot.handle_workout_setup_callback(
            query, _ctx(), 1, f"wk:start:{plan_id}:0")

    assert len([s for s in await _sessions(db) if s["status"] == "active"]) == 1
    assert len(await _sessions(db)) == 1


# ---------------------------------------------------------------------------
# Scope 7 -- callback and architecture invariants.
# ---------------------------------------------------------------------------


def test_every_minted_workout_callback_is_within_the_64_byte_limit() -> None:
    from noam_coach.bot.ui import (
        wk_exercise_callback,
        wk_exercise_menu_callback,
        wk_param_callback,
        wk_select_callback,
        wk_start_callback,
    )

    refs = [
        workout_catalog.WorkoutSelectionRef(
            tier="plan", plan_id=9999999999, fact_rev=None, session_index=99),
        workout_catalog.WorkoutSelectionRef(
            tier="fact", plan_id=None, fact_rev="1a2b3c4d", session_index=99),
    ]
    for ref in refs:
        minted = [
            wk_select_callback(ref), wk_start_callback(ref), f"{wk_start_callback(ref)}:again",
            wk_exercise_menu_callback(ref), wk_exercise_callback(ref, 99),
            wk_param_callback(ref, 99, "weight", -2.5), wk_param_callback(ref, 99, "rest", 15),
        ]
        for data in minted:
            assert len(data.encode("utf-8")) <= 64, (data, len(data.encode("utf-8")))


def test_terminal_grammar_tokens_stay_unambiguous_for_every_wk_form() -> None:
    from noam_coach.services.callback_grammar import strict_extract_flow_id, strict_extract_version

    for data in (
        "wk:list", "wk:sel:12:0", "wk:fsel:1a2b3c4d:0", "wk:start:12:0",
        "wk:fstart:1a2b3c4d:0", "wk:start:12:0:again", "wk:fstart:1a2b3c4d:0:again",
        "wk:exm:12:0", "wk:ex:12:0:1", "wk:par:9999999999:12:12:weight:-2.5",
    ):
        assert strict_extract_version(data) is None, data
        assert strict_extract_flow_id(data) is None, data


def test_no_production_hard_plans_lookup_without_a_guard() -> None:
    """Every remaining `PLANS[<var>]` in production must be provably guarded.

    The allowlist is explicit: each entry is a site inspected and justified,
    so a NEW unguarded lookup fails this test instead of silently shipping a
    KeyError path for custom codes (W5).
    """
    import re
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parent.parent
    allowed = {
        # onboarding builds the weekly plan FROM SPLIT_BY_FREQUENCY, whose
        # codes are PLANS keys by construction.
        "noam_coach/bot/onboarding.py",
        # planning generates from the same template source.
        "planning.py",
        # the legacy template path, reached only after the adapter has
        # confirmed `code in PLANS` (template_fallback).
        "noam_coach/services/profile.py",
        "noam_coach/bot/ui.py",
        # the catalog guards with an explicit `if code not in PLANS: raise`.
        "noam_coach/services/workout_catalog.py",
    }
    pattern = re.compile(r"PLANS\[")
    offenders = []
    for path in root.rglob("*.py"):
        rel = str(path.relative_to(root)).replace("\\", "/")
        if rel.startswith(("tests/", ".venv/")) or "__pycache__" in rel:
            continue
        if rel in allowed:
            continue
        text = path.read_text(encoding="utf-8")
        for line_number, line_text in enumerate(text.splitlines(), start=1):
            # Comment-only mentions are documentation of the OLD defect (the
            # Batch-4 pin #10/#12 fixes describe `PLANS[code]` in prose), not
            # live lookups. Strip them before matching, or this guard flags
            # its own fix notes.
            code_part = line_text.split("#", 1)[0]
            if pattern.search(code_part):
                offenders.append(f"{rel}:{line_number}")
    assert not offenders, (
        f"Unreviewed hard PLANS[...] lookups found: {offenders}. Guard with "
        "PLANS.get(...) or an explicit `code in PLANS` check, or add the site "
        "to the reviewed allowlist with a justification."
    )


@pytest.mark.asyncio
async def test_legacy_adapter_is_still_wired_for_every_legacy_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guards against a future batch quietly removing or bypassing the
    adapter before the retirement criterion is met."""
    from noam_coach.bot import workout_compat

    db = await _make_db(tmp_path, "inv1")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    await _seed_tier1(db, now, codes=["A"], exercises_by_code={"A": [_ex("bench", "לחיצה")]})

    assert workout_compat.LEGACY_PREFIXES
    for data in ("workout:A", "startworkout:A", "editparams_menu:A", "editparams:A:0",
                 "param:A:0:weight:2.5"):
        query = FakeQuery()
        assert await callback_plans_bot.handle_workout_setup_callback(
            query, _ctx(), 1, data) is True, data
    events = await _events(db, "workout_legacy_callback")
    assert len(events) == 5


@pytest.mark.asyncio
async def test_append_only_mode_stale_controls_across_the_whole_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With DEBUG_APPEND_ONLY_MESSAGES every historical keyboard stays
    clickable. No stale control anywhere in the graph may mutate."""
    db = await _make_db(tmp_path, "append")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    old_plan_id = await _seed_tier1(db, now, codes=["A"])
    old_fact_rev = "deadbeef"
    await _regenerate(db, now, codes=["Q"])

    stale = [
        f"wk:sel:{old_plan_id}:0", f"wk:start:{old_plan_id}:0",
        f"wk:start:{old_plan_id}:0:again", f"wk:exm:{old_plan_id}:0",
        f"wk:ex:{old_plan_id}:0:0", f"wk:par:{old_plan_id}:0:0:weight:5",
        f"wk:fsel:{old_fact_rev}:0", f"wk:fstart:{old_fact_rev}:0",
        f"wk:par:{old_fact_rev}:0:0:rest:15",
        "workout:A", "startworkout:A", "editparams_menu:A", "param:A:0:weight:5",
    ]
    for data in stale:
        query = FakeQuery()
        assert await callback_plans_bot.handle_workout_setup_callback(
            query, _ctx(), 1, data) is True, data
        assert await _sessions(db) == [], data
        assert await _overrides(db) == [], data


@pytest.mark.asyncio
async def test_telemetry_failure_does_not_break_any_workout_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path, "telfail")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    plan_id = await _seed_tier1(db, now, codes=["A"])

    async def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("observability is down")

    monkeypatch.setattr("noam_coach.observability.emit.emit_event", boom)
    monkeypatch.setattr("noam_coach.services.core.track_event", boom, raising=False)

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, f"wk:sel:{plan_id}:0")
    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, f"wk:start:{plan_id}:0")
    assert await _active(db) is not None


@pytest.mark.asyncio
async def test_fact_only_user_full_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path, "t2life")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    fact = await _seed_tier2(db, now, codes=["F", "F"])
    rev = workout_catalog.compute_fact_rev(fact)

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, "menu:workout")
    assert len([d for d in _callbacks(query.reply_markups[-1]) if d.startswith("wk:fsel:")]) == 2

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, f"wk:fstart:{rev}:1")
    active = await _active(db)
    assert active is not None and active["code"] == "F"
    assert json.loads(active["plan"])["provenance"]["source"] == "weekly_fact_template"

    await _complete_active_session(db)
    assert (await _sessions(db))[0]["status"] == "completed"
