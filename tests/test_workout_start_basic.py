"""Batch 4 (workout-selection architecture): the `wk:start` / `wk:fstart`
path, driven through the REAL callback entry point.

Batch 4 delivers a WORKING Start in the same batch that mints the Start
buttons (plan section M.3) -- this suite is what proves it. Batch 5 hardens
the same path (full provenance, `:again` repeat confirmation); the provenance
assertions here are deliberately limited to what Batch 4 guarantees:
the session is created from the SELECTED identity's content.

Covers: both tiers create the exact selected session; the active-session
guard; duplicate/double delivery; IntegrityError recovery; and stale refs
(Tier-1 plan_id mismatch, Tier-2 fact_rev mismatch incl. a same-length
different-content replacement) refusing WITHOUT creating a session.
"""
from __future__ import annotations

import json
from datetime import datetime
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


_PERSONALIZED_A = [
    {
        "id": "leg_press", "name": "לחיצת רגליים", "sets": 3, "rmin": 10, "rmax": 12,
        "rest": 90, "weight": 80.0, "inc": 5.0, "cues": [], "muscle": "רגליים", "alts": [],
    }
]


async def _seed_tier1(
    db: Database, now: datetime, *, codes: list[str], session_a_exercises: list[dict[str, Any]] | None = None
) -> int:
    weekday = local_weekday(now)
    sessions = []
    for i, code in enumerate(codes):
        exercises = (
            session_a_exercises
            if (code == "A" and session_a_exercises is not None)
            else [{"id": f"{code.lower()}_ex", "name": f"Exercise {code}", "sets": 3, "muscle": "test"}]
        )
        sessions.append({
            "index": i, "weekday": (weekday + i) % 7, "weekday_schema": WEEKDAY_SCHEMA_VERSION,
            "weekday_name": "test", "time": "18:00", "minutes": 60, "code": code,
            "name": f"אימון {code} מותאם אישית", "exercises": exercises,
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


async def _regenerate_tier1(db: Database, now: datetime, *, codes: list[str]) -> int:
    await db.execute("UPDATE plan_versions SET status='superseded' WHERE user_id=1 AND status='active'")
    await db.execute("DELETE FROM active_plans WHERE user_id=1 AND plan_type='workout'")
    return await _seed_tier1(db, now, codes=codes)


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


# ---------------------------------------------------------------------------
# Tier-1 start.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tier1_start_creates_session_from_the_selected_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Start creates a session whose snapshot holds the SELECTED session's
    personalized exercises -- not PLANS[code]'s. This is the core defect the
    redesign exists to fix: PLANS["A"] starts with "bench", the personalized
    payload with "leg_press"."""
    db = await _make_db(tmp_path, "t1start")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    plan_id = await _seed_tier1(db, now, codes=["A", "B"], session_a_exercises=_PERSONALIZED_A)

    query = FakeQuery()
    assert await callback_plans_bot.handle_workout_setup_callback(
        query, _ctx(), 1, f"wk:start:{plan_id}:0"
    ) is True

    rows = await _sessions(db)
    assert len(rows) == 1
    assert rows[0]["status"] == "active"
    assert rows[0]["code"] == "A"
    snapshot = json.loads(rows[0]["plan"])
    assert [e["id"] for e in snapshot["exercises"]] == ["leg_press"]
    assert snapshot["provenance"]["source"] == "active_plan"
    assert snapshot["provenance"]["plan_id"] == plan_id
    assert snapshot["provenance"]["session_index"] == 0


@pytest.mark.asyncio
async def test_tier1_start_of_a_non_recommended_session_starts_that_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Starting session B creates B -- the recommendation never overrides an
    explicit selection."""
    db = await _make_db(tmp_path, "t1startb")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    plan_id = await _seed_tier1(db, now, codes=["A", "B", "C"])

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, f"wk:start:{plan_id}:1")

    rows = await _sessions(db)
    assert len(rows) == 1 and rows[0]["code"] == "B"
    snapshot = json.loads(rows[0]["plan"])
    assert [e["id"] for e in snapshot["exercises"]] == ["b_ex"]
    assert snapshot["provenance"]["session_index"] == 1


# ---------------------------------------------------------------------------
# Tier-2 start.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tier2_start_creates_session_from_template_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fact-only user starts a template-content workout exactly as before,
    now with provenance and a normalized snapshot."""
    db = await _make_db(tmp_path, "t2start")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    fact = await _seed_tier2(db, now, codes=["F", "F"])
    fact_rev = workout_catalog.compute_fact_rev(fact)

    query = FakeQuery()
    assert await callback_plans_bot.handle_workout_setup_callback(
        query, _ctx(), 1, f"wk:fstart:{fact_rev}:1"
    ) is True

    rows = await _sessions(db)
    assert len(rows) == 1 and rows[0]["code"] == "F"
    snapshot = json.loads(rows[0]["plan"])
    assert [e["id"] for e in snapshot["exercises"]] == [e["id"] for e in PLANS["F"]["exercises"]]
    assert snapshot["provenance"]["source"] == "weekly_fact_template"
    assert snapshot["provenance"]["plan_id"] is None
    assert snapshot["provenance"]["session_index"] == 1
    # Every field the in-session runtime hard-reads is present (normalization).
    for exercise in snapshot["exercises"]:
        for key in ("sets", "rmin", "rmax", "rest", "weight", "inc", "cues", "alts", "muscle", "name"):
            assert key in exercise, key


# ---------------------------------------------------------------------------
# Concurrency / idempotency.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_double_start_delivery_creates_only_one_active_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A duplicate callback delivery must not create two active sessions. The
    second call hits the active-session guard and reopens the first."""
    db = await _make_db(tmp_path, "double")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    plan_id = await _seed_tier1(db, now, codes=["A", "B"], session_a_exercises=_PERSONALIZED_A)

    for _ in range(2):
        query = FakeQuery()
        await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, f"wk:start:{plan_id}:0")

    rows = await _sessions(db)
    assert len(rows) == 1
    active = [r for r in rows if r["status"] == "active"]
    assert len(active) == 1


@pytest.mark.asyncio
async def test_start_with_an_active_session_reopens_it_and_starts_nothing_new(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even a start for a DIFFERENT session reopens the active one rather than
    opening a second (one active session per user, unchanged)."""
    db = await _make_db(tmp_path, "activestart")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    plan_id = await _seed_tier1(db, now, codes=["A", "B"], session_a_exercises=_PERSONALIZED_A)

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, f"wk:start:{plan_id}:0")
    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, f"wk:start:{plan_id}:1")

    rows = await _sessions(db)
    assert len(rows) == 1
    assert rows[0]["code"] == "A"  # the second start did NOT create B


@pytest.mark.asyncio
async def test_integrity_error_recovery_shows_the_existing_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unique partial index is the real concurrency guarantee. Simulate the
    race the active-session guard cannot catch (a row appearing between the
    guard and the INSERT) and assert we recover by showing the existing
    session instead of surfacing an error."""
    db = await _make_db(tmp_path, "integrity")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    plan_id = await _seed_tier1(db, now, codes=["A", "B"], session_a_exercises=_PERSONALIZED_A)

    real_execute = db.execute
    state = {"raced": False}

    async def racing_execute(sql: str, params: Any = (), *args: Any, **kwargs: Any) -> Any:
        # On the Start INSERT, first sneak in a competing active session (as a
        # concurrent delivery would), so the real unique index rejects ours.
        if not state["raced"] and sql.strip().startswith("INSERT INTO sessions("):
            state["raced"] = True
            await real_execute(
                "INSERT INTO sessions(user_id, code, name, plan, status, exercise_index, set_number, started_at) "
                "VALUES(1, 'B', 'B', ?, 'active', 0, 1, ?)",
                (json.dumps({"name": "B", "exercises": [{"id": "b_ex", "name": "Exercise B", "sets": 3,
                                                         "rmin": 8, "rmax": 12, "rest": 90, "weight": 0.0,
                                                         "inc": 2.5, "cues": [], "alts": [], "muscle": "t"}]}),
                 utc_now()),
            )
        return await real_execute(sql, params, *args, **kwargs)

    monkeypatch.setattr(db, "execute", racing_execute)

    query = FakeQuery()
    handled = await callback_plans_bot.handle_workout_setup_callback(
        query, _ctx(), 1, f"wk:start:{plan_id}:0"
    )

    assert handled is True
    monkeypatch.undo()
    rows = await _sessions(db)
    active = [r for r in rows if r["status"] == "active"]
    assert len(active) == 1, [dict(r) for r in rows]
    assert active[0]["code"] == "B"  # the winner of the race, shown to the user
    assert state["raced"] is True


# ---------------------------------------------------------------------------
# Stale references must refuse WITHOUT creating a session.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_tier1_start_after_regeneration_creates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The overview->start TOCTOU window: the plan regenerated while the
    overview sat on screen. The stale plan_id must refuse -- never start a
    workout from the superseded plan, and never silently start the new plan's
    session at the same index."""
    db = await _make_db(tmp_path, "stale1start")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    old_plan_id = await _seed_tier1(db, now, codes=["A", "B"], session_a_exercises=_PERSONALIZED_A)
    new_plan_id = await _regenerate_tier1(db, now, codes=["A", "B"])

    query = FakeQuery()
    assert await callback_plans_bot.handle_workout_setup_callback(
        query, _ctx(), 1, f"wk:start:{old_plan_id}:0"
    ) is True

    assert await _sessions(db) == []
    assert any(a and "התוכנית התעדכנה" in a for a in query.answers)
    # And the user is handed the CURRENT plan's selector.
    datas = [b.callback_data for row in query.reply_markups[-1].inline_keyboard for b in row]
    assert any(d.startswith(f"wk:sel:{new_plan_id}:") for d in datas)


@pytest.mark.asyncio
async def test_stale_tier2_start_same_length_replacement_creates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Blocker-1 regression on the START path: a fact replaced by a different
    plan of the SAME length must not start the wrong workout. Without the
    content fingerprint the in-range index would have resolved silently."""
    db = await _make_db(tmp_path, "stale2start")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    old_fact = await _seed_tier2(db, now, codes=["F", "F"])
    old_rev = workout_catalog.compute_fact_rev(old_fact)

    new_fact = await _seed_tier2(db, now, codes=["A", "B"])
    assert len(new_fact["sessions"]) == len(old_fact["sessions"])
    assert workout_catalog.compute_fact_rev(new_fact) != old_rev

    query = FakeQuery()
    assert await callback_plans_bot.handle_workout_setup_callback(
        query, _ctx(), 1, f"wk:fstart:{old_rev}:0"
    ) is True

    assert await _sessions(db) == []
    assert any(a and "התוכנית התעדכנה" in a for a in query.answers)


@pytest.mark.asyncio
async def test_stale_tier2_start_after_fact_removal_creates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path, "stale2gone")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    fact = await _seed_tier2(db, now, codes=["F", "F"])
    fact_rev = workout_catalog.compute_fact_rev(fact)
    await db.execute("DELETE FROM user_facts WHERE user_id=1 AND key='active_workout_plan'")

    query = FakeQuery()
    assert await callback_plans_bot.handle_workout_setup_callback(
        query, _ctx(), 1, f"wk:fstart:{fact_rev}:0"
    ) is True
    assert await _sessions(db) == []


@pytest.mark.asyncio
async def test_out_of_range_and_malformed_start_refs_create_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hand-crafted start callback must refuse, never raise, never insert."""
    db = await _make_db(tmp_path, "badstart")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    plan_id = await _seed_tier1(db, now, codes=["A", "B"])

    for data in (
        f"wk:start:{plan_id}:99", "wk:start:nope:0", "wk:start:1",
        "wk:fstart::0", "wk:fstart:deadbeef:0",
    ):
        query = FakeQuery()
        assert await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, data) is True
        assert await _sessions(db) == [], data


# ---------------------------------------------------------------------------
# Callback budget.
# ---------------------------------------------------------------------------


def test_every_wk_callback_stays_within_telegram_64_byte_limit() -> None:
    """Telegram hard-caps callback_data at 64 BYTES. Worst realistic case: a
    10-digit plan_id and a 2-digit session index."""
    from noam_coach.bot.ui import wk_select_callback, wk_start_callback

    tier1 = workout_catalog.WorkoutSelectionRef(
        tier="plan", plan_id=9999999999, fact_rev=None, session_index=99
    )
    tier2 = workout_catalog.WorkoutSelectionRef(
        tier="fact", plan_id=None, fact_rev="1a2b3c4d", session_index=99
    )
    for ref in (tier1, tier2):
        for data in (wk_select_callback(ref), wk_start_callback(ref)):
            assert len(data.encode("utf-8")) <= 64, (data, len(data.encode("utf-8")))
    # And the concrete worst case is comfortably short.
    assert len(wk_start_callback(tier1).encode("utf-8")) == len("wk:start:9999999999:99")
