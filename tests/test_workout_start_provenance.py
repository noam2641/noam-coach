"""Batch 5 (workout-selection architecture): Start hardening on the already-
working Batch-4 path.

Three things are proven here, all through the REAL callback entry point:

1. **Provenance correctness** (plan section H) -- the full immutable record
   inside ``sessions.plan``, both tiers, including ``overrides_applied``,
   ``defaults_filled``, ``app_version`` and ``normalizer_version``.
2. **Normalization end-to-end** -- a deliberately SPARSE plan payload
   (``{id, sets, muscle}`` only, the shape a generator can legitimately emit)
   survives ``show_session`` -> ``save_set`` -> finish without a KeyError.
   This is the real proof that the normalization choke point covers every
   field the in-session runtime hard-reads.
3. **Repeat confirmation** -- starting a session already completed today
   requires the explicit ``:again`` variant, which skips ONLY the done-today
   check and never the staleness/concurrency guards.

Plus snapshot compatibility in both directions (plan section M.2): old code
reads the new snapshot (it touches only ``name``/``exercises``), and a
mid-session regeneration leaves an already-started snapshot byte-unchanged.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

import coach_bot
import user_model
from config import APP_VERSION, TZ
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


def _bind(monkeypatch: pytest.MonkeyPatch, db: Database) -> None:
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(ui_bot, "DB", db)


# A payload exercise carrying ONLY what a sparse generator emits. Every other
# field the runtime hard-reads (rmin/rmax/rest/weight/inc/cues/alts) must be
# supplied by the normalizer, not by this fixture.
_SPARSE_EXERCISES = [{"id": "bench", "sets": 2, "muscle": "חזה"}]

_RICH_EXERCISES = [
    {
        "id": "leg_press", "name": "לחיצת רגליים", "sets": 2, "rmin": 10, "rmax": 12,
        "rest": 90, "weight": 80.0, "inc": 5.0, "cues": ["טווח מלא"], "muscle": "רגליים", "alts": [],
    }
]


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
            "name": f"אימון {code} מותאם אישית",
            "exercises": exercises_by_code.get(
                code, [{"id": f"{code.lower()}_ex", "name": f"Exercise {code}", "sets": 2, "muscle": "test"}]
            ),
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


async def _active(db: Database) -> Any:
    return await db.fetch_one("SELECT * FROM sessions WHERE user_id=1 AND status='active'", ())


async def _all_sessions(db: Database) -> list[Any]:
    return await db.fetch_all("SELECT * FROM sessions WHERE user_id=1 ORDER BY id", ())


async def _mark_completed_today(db: Database, code: str, now: datetime) -> None:
    """Seed a session completed *today*, as the helper's name promises.

    The repeat-today gate compares against ``datetime.now(TZ)`` -- its caller
    passes no ``now`` -- so a row stamped with the fixture date is not "today",
    the gate never engages, and the interstitial these tests assert on never
    appears. Anchoring to the real current day keeps the scenario honest
    without pinning production behaviour; ``now``'s time of day is preserved so
    the seeded session still sits at the fixture's hour.
    """
    today = datetime.now(TZ).replace(
        hour=now.hour, minute=now.minute, second=0, microsecond=0,
    )
    iso = today.astimezone(timezone.utc).isoformat()
    await db.execute(
        "INSERT INTO sessions(user_id, code, name, plan, status, exercise_index, set_number, started_at, ended_at) "
        "VALUES(1, ?, ?, '{}', 'completed', 0, 1, ?, ?)",
        (code, code, iso, iso),
    )


# ---------------------------------------------------------------------------
# 1. Provenance correctness (plan section H).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tier1_provenance_records_the_full_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path, "prov1")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    plan_id = await _seed_tier1(db, now, codes=["A", "B"], exercises_by_code={"A": _RICH_EXERCISES})

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, f"wk:start:{plan_id}:0")

    snapshot = json.loads((await _active(db))["plan"])
    prov = snapshot["provenance"]
    assert prov["schema_version"] == 2
    assert prov["source"] == "active_plan"
    assert prov["plan_id"] == plan_id
    assert prov["session_index"] == 0
    assert prov["session_code"] == "A"
    assert prov["session_name"] == "אימון A מותאם אישית"
    assert prov["app_version"] == APP_VERSION
    assert prov["normalizer_version"] == workout_catalog.NORMALIZER_VERSION
    assert prov["overrides_applied"] == []
    # A rich payload needs no defaults filled.
    assert prov["defaults_filled"] == {}
    datetime.fromisoformat(prov["materialized_at"])  # parses, i.e. real iso8601


@pytest.mark.asyncio
async def test_tier2_provenance_records_template_source_and_null_plan_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path, "prov2")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    fact = await _seed_tier2(db, now, codes=["F", "F"])
    rev = workout_catalog.compute_fact_rev(fact)

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, f"wk:fstart:{rev}:1")

    prov = json.loads((await _active(db))["plan"])["provenance"]
    assert prov["source"] == "weekly_fact_template"
    assert prov["plan_id"] is None
    assert prov["session_index"] == 1
    assert prov["session_code"] == "F"
    assert prov["session_name"] == PLANS["F"]["name"]
    assert prov["app_version"] == APP_VERSION


@pytest.mark.asyncio
async def test_provenance_records_applied_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An override the user set is both APPLIED to the snapshot exercise and
    recorded in provenance, so the snapshot explains itself."""
    db = await _make_db(tmp_path, "provov")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    plan_id = await _seed_tier1(db, now, codes=["A"], exercises_by_code={"A": _RICH_EXERCISES})
    await db.execute(
        "INSERT INTO exercise_overrides(user_id, code, exercise_index, field, value, updated_at, exercise_id) "
        "VALUES(1, 'A', 0, 'weight', 95.0, ?, 'leg_press')",
        (utc_now(),),
    )

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, f"wk:start:{plan_id}:0")

    snapshot = json.loads((await _active(db))["plan"])
    assert snapshot["exercises"][0]["weight"] == 95.0
    assert snapshot["provenance"]["overrides_applied"] == [
        {"exercise_id": "leg_press", "field": "weight", "value": 95.0}
    ]


@pytest.mark.asyncio
async def test_provenance_records_defaults_filled_for_sparse_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`defaults_filled` is the audit trail answering 'which default rules
    invented these numbers' -- non-empty exactly when the payload was sparse."""
    db = await _make_db(tmp_path, "provsparse")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    plan_id = await _seed_tier1(db, now, codes=["A"], exercises_by_code={"A": _SPARSE_EXERCISES})

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, f"wk:start:{plan_id}:0")

    prov = json.loads((await _active(db))["plan"])["provenance"]
    filled = prov["defaults_filled"]["bench"]
    for key in ("rmin", "rmax", "rest", "weight", "inc", "cues", "alts"):
        assert key in filled, (key, filled)
    # "sets" and "muscle" were present in the payload and must NOT be recorded
    # as filled -- the payload's own prescription always wins.
    assert "sets" not in filled


# ---------------------------------------------------------------------------
# 2. Normalization end-to-end: a sparse payload survives the real runtime.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sparse_payload_session_runs_start_to_finish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The normalization proof. A payload exercise carrying only
    {id, sets, muscle} must survive show_session (which hard-reads "cues"),
    every save_set, and completion -- with no KeyError anywhere.
    """
    db = await _make_db(tmp_path, "e2e")
    _bind(monkeypatch, db)
    monkeypatch.setattr(workout_bot, "DB", db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    plan_id = await _seed_tier1(db, now, codes=["A"], exercises_by_code={"A": _SPARSE_EXERCISES})

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, f"wk:start:{plan_id}:0")
    session = await _active(db)
    assert session is not None

    # show_session renders without KeyError on the normalized snapshot.
    await workout_bot.show_session(query, 1, session["id"])
    assert query.messages

    # Log every prescribed set; the last one completes the session.
    snapshot = json.loads(session["plan"])
    total_sets = snapshot["exercises"][0]["sets"]
    assert total_sets == 2  # from the payload, not a default
    for _ in range(total_sets):
        current = await db.fetch_one("SELECT * FROM sessions WHERE id=?", (session["id"],))
        completed, _set_id = await workout_bot.save_set(dict(current), 60.0, 10, 2, "test")

    finished = await db.fetch_one("SELECT * FROM sessions WHERE id=?", (session["id"],))
    assert finished["status"] == "completed"
    assert finished["ended_at"] is not None
    assert completed is True


@pytest.mark.asyncio
async def test_tier2_sparse_template_session_shows_without_keyerror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same guarantee on the Tier-2 template path."""
    db = await _make_db(tmp_path, "e2et2")
    _bind(monkeypatch, db)
    monkeypatch.setattr(workout_bot, "DB", db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    fact = await _seed_tier2(db, now, codes=["F", "F"])
    rev = workout_catalog.compute_fact_rev(fact)

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, f"wk:fstart:{rev}:0")
    session = await _active(db)
    await workout_bot.show_session(query, 1, session["id"])
    assert query.messages


# ---------------------------------------------------------------------------
# 3. Repeat-today confirmation (`:again`).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repeat_today_requires_again_and_starts_nothing_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session already completed today shows the interstitial and creates NO
    new session until the user explicitly confirms."""
    db = await _make_db(tmp_path, "again1")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    plan_id = await _seed_tier1(db, now, codes=["A", "B"], exercises_by_code={"A": _RICH_EXERCISES})
    await _mark_completed_today(db, "A", now)
    before = len(await _all_sessions(db))

    query = FakeQuery()
    assert await callback_plans_bot.handle_workout_setup_callback(
        query, _ctx(), 1, f"wk:start:{plan_id}:0"
    ) is True

    assert len(await _all_sessions(db)) == before  # nothing started
    assert await _active(db) is None
    assert f"wk:start:{plan_id}:0:again" in _callbacks(query.reply_markups[-1])


@pytest.mark.asyncio
async def test_again_variant_starts_the_repeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path, "again2")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    plan_id = await _seed_tier1(db, now, codes=["A", "B"], exercises_by_code={"A": _RICH_EXERCISES})
    await _mark_completed_today(db, "A", now)

    query = FakeQuery()
    assert await callback_plans_bot.handle_workout_setup_callback(
        query, _ctx(), 1, f"wk:start:{plan_id}:0:again"
    ) is True

    session = await _active(db)
    assert session is not None and session["code"] == "A"
    assert json.loads(session["plan"])["provenance"]["session_index"] == 0


@pytest.mark.asyncio
async def test_tier2_repeat_flow_uses_fstart_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path, "again3")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    fact = await _seed_tier2(db, now, codes=["F", "F"])
    rev = workout_catalog.compute_fact_rev(fact)
    await _mark_completed_today(db, "F", now)

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, f"wk:fstart:{rev}:0")
    assert await _active(db) is None
    assert f"wk:fstart:{rev}:0:again" in _callbacks(query.reply_markups[-1])

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, f"wk:fstart:{rev}:0:again")
    assert (await _active(db)) is not None


@pytest.mark.asyncio
async def test_again_does_not_bypass_stale_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`:again` skips ONLY the done-today check. A stale identity carrying the
    repeat marker must still refuse -- confirming a repeat can never be an
    escape hatch around regeneration safety."""
    db = await _make_db(tmp_path, "againstale")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    old_plan_id = await _seed_tier1(db, now, codes=["A", "B"], exercises_by_code={"A": _RICH_EXERCISES})
    await _mark_completed_today(db, "A", now)
    await _regenerate_tier1(db, now, codes=["A", "B"])

    query = FakeQuery()
    assert await callback_plans_bot.handle_workout_setup_callback(
        query, _ctx(), 1, f"wk:start:{old_plan_id}:0:again"
    ) is True

    assert await _active(db) is None
    assert any(a and "התוכנית התעדכנה" in a for a in query.answers)


@pytest.mark.asyncio
async def test_again_does_not_bypass_the_active_session_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path, "againactive")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    plan_id = await _seed_tier1(db, now, codes=["A", "B"], exercises_by_code={"A": _RICH_EXERCISES})

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, f"wk:start:{plan_id}:0")
    assert await _active(db) is not None

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, f"wk:start:{plan_id}:1:again")
    assert len([s for s in await _all_sessions(db) if s["status"] == "active"]) == 1


@pytest.mark.asyncio
async def test_again_marker_is_rejected_on_non_start_callbacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`wk:sel:...:again` is not a callback this codebase mints; honoring it
    would be accepting a shape no screen produces."""
    db = await _make_db(tmp_path, "againsel")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    plan_id = await _seed_tier1(db, now, codes=["A", "B"])

    assert callback_plans_bot._parse_wk_ref(f"wk:sel:{plan_id}:0:again") is None
    assert callback_plans_bot._parse_wk_ref(f"wk:start:{plan_id}:0:bogus") is None
    assert callback_plans_bot._parse_wk_ref(f"wk:start:{plan_id}:0:again") is not None


def test_again_variants_are_debounced_by_the_existing_start_prefixes() -> None:
    """The repeat variants are mutations and must be debounced. They are
    covered by the `wk:start:` / `wk:fstart:` prefixes already registered in
    Batch 4 (marker is a suffix, matching is startswith) -- this test is what
    lets the router keep a single entry per tier instead of four."""
    from noam_coach.bot.callback_router import _DEBOUNCE_PREFIXES

    assert "wk:start:12:0:again".startswith(_DEBOUNCE_PREFIXES)
    assert "wk:fstart:1a2b3c4d:0:again".startswith(_DEBOUNCE_PREFIXES)
    # Navigation stays usable.
    assert not "wk:sel:12:0".startswith(_DEBOUNCE_PREFIXES)
    assert not "wk:list".startswith(_DEBOUNCE_PREFIXES)


def test_again_variants_stay_within_the_callback_budget_and_grammar() -> None:
    """The `:again` marker adds 6 bytes and lands in the TERMINAL position --
    the one the ARCH-04 grammar inspects for `^v\\d{1,9}$` / `^ff-...$`. Assert
    it is not mistakable for either token, and that the worst realistic case
    (10-digit plan_id, 2-digit index) stays far under Telegram's 64 bytes.
    """
    from noam_coach.services.callback_grammar import strict_extract_flow_id, strict_extract_version

    for data in (
        "wk:start:9999999999:99:again",
        "wk:fstart:1a2b3c4d:99:again",
    ):
        assert len(data.encode("utf-8")) <= 64, (data, len(data.encode("utf-8")))
        assert strict_extract_version(data) is None, data
        assert strict_extract_flow_id(data) is None, data


# ---------------------------------------------------------------------------
# 4. Snapshot compatibility (plan section M.2, bidirectional).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_old_code_reads_the_new_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pre-Batch-4 consumers touch only `name` and `exercises`. The added
    `provenance` key must be invisible to them -- this is what makes rolling
    the B4-B7 UX unit back safe without stranding started sessions."""
    db = await _make_db(tmp_path, "oldread")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    plan_id = await _seed_tier1(db, now, codes=["A"], exercises_by_code={"A": _RICH_EXERCISES})

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, f"wk:start:{plan_id}:0")

    snapshot = json.loads((await _active(db))["plan"])
    # Exactly the shape old code expects, plus one additive key.
    assert snapshot["name"]
    assert isinstance(snapshot["exercises"], list) and snapshot["exercises"]
    assert set(snapshot) == {"name", "exercises", "provenance"}
    # An old reader ignoring provenance round-trips the dict unchanged.
    reserialized = json.loads(json.dumps(snapshot, ensure_ascii=False))
    assert reserialized == snapshot


@pytest.mark.asyncio
async def test_mid_session_regeneration_leaves_the_snapshot_byte_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The snapshot decouples a running session from its plan. Regenerating
    mid-session must not retroactively change what the user is doing."""
    db = await _make_db(tmp_path, "midregen")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    plan_id = await _seed_tier1(db, now, codes=["A", "B"], exercises_by_code={"A": _RICH_EXERCISES})

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, f"wk:start:{plan_id}:0")
    before = (await _active(db))["plan"]

    await _regenerate_tier1(db, now, codes=["A", "B"])

    after = (await _active(db))["plan"]
    assert after == before  # byte-identical


@pytest.mark.asyncio
async def test_session_started_pre_batch4_without_provenance_still_shows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No backfill is required: a session row written by the OLD code (no
    provenance key at all) must keep rendering."""
    db = await _make_db(tmp_path, "legacysnap")
    _bind(monkeypatch, db)
    monkeypatch.setattr(workout_bot, "DB", db)
    legacy = {
        "name": "אימון ישן",
        "exercises": [{
            "id": "bench", "name": "לחיצת חזה", "sets": 2, "rmin": 8, "rmax": 12,
            "rest": 90, "weight": 40.0, "inc": 2.5, "cues": [], "alts": [], "muscle": "חזה",
        }],
    }
    session_id = await db.execute(
        "INSERT INTO sessions(user_id, code, name, plan, status, exercise_index, set_number, started_at) "
        "VALUES(1, 'A', 'אימון ישן', ?, 'active', 0, 1, ?)",
        (json.dumps(legacy, ensure_ascii=False), utc_now()),
    )

    query = FakeQuery()
    await workout_bot.show_session(query, 1, session_id)
    assert query.messages
    assert "provenance" not in json.loads((await _active(db))["plan"])
