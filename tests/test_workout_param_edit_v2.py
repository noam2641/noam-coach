"""Batch 6 (workout-selection architecture): identity-safe parameter editing.

Editing now targets the SELECTED session's exercise by stable id, code-scoped
per plan section G, and the selection survives the whole edit loop
(picker -> params -> free-text -> confirm -> back). Driven through the real
callback and persistence paths, not the service layer.

The regressions this suite exists to prevent:

* **W4** -- editing bench on A silently changing bench on F. Overrides are
  code-scoped with no cross-code fallback.
* **Bare-index application** -- an override landing on whatever exercise now
  occupies that position rather than the one the user actually edited.
* **Stale retargeting** -- an edit composed before a regeneration being
  applied to the new plan afterwards.

Batch 7 owns the legacy `editparams:`/`param:` adapter; those prefixes are
deliberately still live and still positional here, and the legacy-payload
tests below pin that they keep working untouched.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

import coach_bot
import conversation
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


def _ex(exercise_id: str, name: str, **over: Any) -> dict[str, Any]:
    base = {
        "id": exercise_id, "name": name, "sets": 3, "rmin": 8, "rmax": 12,
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


async def _overrides(db: Database) -> list[dict[str, Any]]:
    rows = await db.fetch_all(
        "SELECT code, exercise_index, field, value, exercise_id FROM exercise_overrides "
        "WHERE user_id=1 ORDER BY code, exercise_index, field", ()
    )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Callback graph: picker and params carry identity, not a bare code.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_overview_offers_identity_carrying_edit_button(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The v2 overview's edit button carries (plan_id, session_index) -- the
    Batch-4 gap where editing was omitted rather than minted ambiguously."""
    db = await _make_db(tmp_path, "editbtn")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    plan_id = await _seed_tier1(db, now, codes=["A", "B"])

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, f"wk:sel:{plan_id}:1")

    datas = _callbacks(query.reply_markups[-1])
    assert f"wk:exm:{plan_id}:1" in datas
    assert not any(d.startswith("editparams_menu:") for d in datas)


@pytest.mark.asyncio
async def test_picker_lists_the_selected_sessions_own_exercises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The picker shows session B's personalized exercises, not PLANS["B"]'s."""
    db = await _make_db(tmp_path, "picker")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    plan_id = await _seed_tier1(db, now, codes=["A", "B"], exercises_by_code={
        "B": [_ex("custom_row", "חתירה מותאמת"), _ex("custom_curl", "כפיפה מותאמת")],
    })

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, f"wk:exm:{plan_id}:1")

    datas = _callbacks(query.reply_markups[-1])
    assert f"wk:ex:{plan_id}:1:0" in datas
    assert f"wk:ex:{plan_id}:1:1" in datas
    assert f"wk:sel:{plan_id}:1" in datas  # back preserves the selection
    labels = " ".join(b.text for row in query.reply_markups[-1].inline_keyboard for b in row)
    assert "חתירה מותאמת" in labels


@pytest.mark.asyncio
async def test_params_screen_arms_a_v2_flow_payload_with_full_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The conversation flow carries the whole identity, so a free-text edit
    minutes later still re-validates against this exact session."""
    db = await _make_db(tmp_path, "flowpayload")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    plan_id = await _seed_tier1(db, now, codes=["A", "B"], exercises_by_code={
        "B": [_ex("bench", "לחיצת חזה"), _ex("row", "חתירה")],
    })

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, f"wk:ex:{plan_id}:1:1")

    flow = await conversation.get_active_flow(db, 1)
    assert flow.name == conversation.FlowName.workout_parameter_edit
    assert flow.payload["v"] == 2
    assert flow.payload["tier"] == "plan"
    assert flow.payload["plan_id"] == plan_id
    assert flow.payload["session_index"] == 1
    assert flow.payload["exercise_index"] == 1
    assert flow.payload["exercise_id"] == "row"
    assert flow.payload["code"] == "B"


# ---------------------------------------------------------------------------
# Override semantics (plan section G).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stepper_write_persists_code_scoped_row_with_exercise_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path, "stepper")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    plan_id = await _seed_tier1(db, now, codes=["A"], exercises_by_code={
        "A": [_ex("bench", "לחיצת חזה", weight=50.0)],
    })

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(
        query, _ctx(), 1, f"wk:par:{plan_id}:0:0:weight:2.5"
    )

    rows = await _overrides(db)
    assert len(rows) == 1
    assert rows[0]["code"] == "A"
    assert rows[0]["field"] == "weight"
    assert rows[0]["value"] == 52.5
    assert rows[0]["exercise_id"] == "bench"  # stable identity written (G.6)


@pytest.mark.asyncio
async def test_same_exercise_id_in_two_codes_stays_independent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The W4 regression: editing bench on A must leave bench on F alone.
    Overrides are code-scoped with NO cross-code fallback (plan section G.2)."""
    db = await _make_db(tmp_path, "twocodes")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    plan_id = await _seed_tier1(db, now, codes=["A", "F"], exercises_by_code={
        "A": [_ex("bench", "לחיצת חזה", weight=50.0)],
        "F": [_ex("bench", "לחיצת חזה", weight=50.0)],
    })

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(
        query, _ctx(), 1, f"wk:par:{plan_id}:0:0:weight:10"
    )

    rows = await _overrides(db)
    assert [(r["code"], r["value"]) for r in rows] == [("A", 60.0)]

    # And session F still resolves to its untouched 50.0.
    f_ref = workout_catalog.WorkoutSelectionRef(
        tier="plan", plan_id=plan_id, fact_rev=None, session_index=1
    )
    resolved_f = await workout_catalog.resolve_selection(db, 1, f_ref)
    overrides_f = await workout_catalog.collect_overrides(db, 1, resolved_f.session)
    assert overrides_f == {}


@pytest.mark.asyncio
async def test_override_survives_regeneration_when_code_and_id_persist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A regenerated plan keeping the same code+exercise id keeps the user's
    load -- the override is keyed on identity, not on the plan revision."""
    db = await _make_db(tmp_path, "regenkeep")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    plan_id = await _seed_tier1(db, now, codes=["A"], exercises_by_code={
        "A": [_ex("bench", "לחיצת חזה", weight=50.0)],
    })
    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(
        query, _ctx(), 1, f"wk:par:{plan_id}:0:0:weight:10"
    )

    new_plan_id = await _regenerate_tier1(db, now, codes=["A"], exercises_by_code={
        "A": [_ex("bench", "לחיצת חזה", weight=50.0)],
    })
    new_ref = workout_catalog.WorkoutSelectionRef(
        tier="plan", plan_id=new_plan_id, fact_rev=None, session_index=0
    )
    resolved = await workout_catalog.resolve_selection(db, 1, new_ref)
    overrides = await workout_catalog.collect_overrides(db, 1, resolved.session)
    assert overrides == {"bench": {"weight": 60.0}}


@pytest.mark.asyncio
async def test_override_never_lands_on_an_unrelated_exercise_at_the_same_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plan section G.4: never apply an override solely because it occupies
    the same index. After a regeneration that REPLACES the exercise at index
    0, the old row must not leak onto the new exercise."""
    db = await _make_db(tmp_path, "unrelated")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    plan_id = await _seed_tier1(db, now, codes=["A"], exercises_by_code={
        "A": [_ex("bench", "לחיצת חזה", weight=50.0)],
    })
    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(
        query, _ctx(), 1, f"wk:par:{plan_id}:0:0:weight:10"
    )

    new_plan_id = await _regenerate_tier1(db, now, codes=["A"], exercises_by_code={
        "A": [_ex("squat", "סקוואט", weight=70.0)],  # different id, SAME index
    })
    new_ref = workout_catalog.WorkoutSelectionRef(
        tier="plan", plan_id=new_plan_id, fact_rev=None, session_index=0
    )
    resolved = await workout_catalog.resolve_selection(db, 1, new_ref)
    overrides = await workout_catalog.collect_overrides(db, 1, resolved.session)
    assert overrides == {}  # squat did NOT inherit bench's 60kg


@pytest.mark.asyncio
async def test_edited_value_is_reflected_in_overview_and_start_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: what the user edits is what the overview shows and what
    Start snapshots."""
    db = await _make_db(tmp_path, "e2eedit")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    plan_id = await _seed_tier1(db, now, codes=["A"], exercises_by_code={
        "A": [_ex("bench", "לחיצת חזה", weight=50.0)],
    })

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(
        query, _ctx(), 1, f"wk:par:{plan_id}:0:0:weight:2.5"
    )
    assert "52.5" in query.messages[-1]

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, f"wk:start:{plan_id}:0")
    session = await db.fetch_one("SELECT * FROM sessions WHERE user_id=1 AND status='active'", ())
    snapshot = json.loads(session["plan"])
    assert snapshot["exercises"][0]["weight"] == 52.5
    assert snapshot["provenance"]["overrides_applied"] == [
        {"exercise_id": "bench", "field": "weight", "value": 52.5}
    ]


@pytest.mark.asyncio
async def test_repeated_stepper_delivery_accumulates_once_per_tap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each delivered tap is one delta (the upsert is last-write-wins on the
    computed value, not an increment of a counter), so N taps == N deltas and
    a re-render never double-applies."""
    db = await _make_db(tmp_path, "repeat")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    plan_id = await _seed_tier1(db, now, codes=["A"], exercises_by_code={
        "A": [_ex("bench", "לחיצת חזה", weight=50.0)],
    })

    for _ in range(3):
        query = FakeQuery()
        await callback_plans_bot.handle_workout_setup_callback(
            query, _ctx(), 1, f"wk:par:{plan_id}:0:0:weight:2.5"
        )

    rows = await _overrides(db)
    assert len(rows) == 1  # one row, upserted
    assert rows[0]["value"] == 57.5  # 50 + 3x2.5


# ---------------------------------------------------------------------------
# Tier-2 editing.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tier2_edit_uses_fingerprinted_identity_and_writes_by_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path, "t2edit")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    fact = await _seed_tier2(db, now, codes=["F", "F"])
    rev = workout_catalog.compute_fact_rev(fact)

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, f"wk:exm:{rev}:1")
    datas = _callbacks(query.reply_markups[-1])
    assert f"wk:ex:{rev}:1:0" in datas

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(
        query, _ctx(), 1, f"wk:par:{rev}:1:0:weight:5"
    )
    rows = await _overrides(db)
    assert len(rows) == 1
    assert rows[0]["code"] == "F"
    assert rows[0]["exercise_id"] == PLANS["F"]["exercises"][0]["id"]


@pytest.mark.asyncio
async def test_tier2_duplicate_code_sessions_share_code_scoped_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both ["F","F"] sessions ARE the same code, so a code-scoped override
    legitimately applies to both. This pins the approved semantics rather than
    silently broadening or narrowing them: identity distinguishes which
    SESSION you start, while overrides remain per (code, exercise id)."""
    db = await _make_db(tmp_path, "t2dup")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    fact = await _seed_tier2(db, now, codes=["F", "F"])
    rev = workout_catalog.compute_fact_rev(fact)

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(
        query, _ctx(), 1, f"wk:par:{rev}:0:0:weight:5"
    )

    for session_index in (0, 1):
        ref = workout_catalog.WorkoutSelectionRef(
            tier="fact", plan_id=None, fact_rev=rev, session_index=session_index
        )
        resolved = await workout_catalog.resolve_selection(db, 1, ref)
        overrides = await workout_catalog.collect_overrides(db, 1, resolved.session)
        assert overrides, f"session {session_index} should see the code-scoped override"


# ---------------------------------------------------------------------------
# Stale references and malformed input.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_edit_callbacks_refuse_and_write_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every edit callback re-validates before mutating: a regenerated plan
    must refuse, not retarget the edit onto the new plan."""
    db = await _make_db(tmp_path, "staleedit")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    old_plan_id = await _seed_tier1(db, now, codes=["A"], exercises_by_code={
        "A": [_ex("bench", "לחיצת חזה", weight=50.0)],
    })
    await _regenerate_tier1(db, now, codes=["A"], exercises_by_code={
        "A": [_ex("bench", "לחיצת חזה", weight=50.0)],
    })

    for data in (
        f"wk:exm:{old_plan_id}:0",
        f"wk:ex:{old_plan_id}:0:0",
        f"wk:par:{old_plan_id}:0:0:weight:10",
    ):
        query = FakeQuery()
        assert await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, data) is True
        assert any(a and "התוכנית התעדכנה" in a for a in query.answers), data

    assert await _overrides(db) == []


@pytest.mark.asyncio
async def test_stale_tier2_edit_after_same_length_replacement_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path, "t2staleedit")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    old_fact = await _seed_tier2(db, now, codes=["F", "F"])
    old_rev = workout_catalog.compute_fact_rev(old_fact)
    await _seed_tier2(db, now, codes=["A", "B"])  # same length, different content

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(
        query, _ctx(), 1, f"wk:par:{old_rev}:0:0:weight:5"
    )
    assert any(a and "התוכנית התעדכנה" in a for a in query.answers)
    assert await _overrides(db) == []


@pytest.mark.asyncio
async def test_malformed_edit_callbacks_refuse_without_raising_or_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path, "malformededit")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    plan_id = await _seed_tier1(db, now, codes=["A"])

    for data in (
        f"wk:par:{plan_id}:0:0:bogusfield:1",   # field outside OVERRIDE_FIELDS
        f"wk:par:{plan_id}:0:0:weight:notanum",
        f"wk:ex:{plan_id}:0:99",               # exercise index out of range
        f"wk:ex:{plan_id}:0:-1",
        f"wk:exm:{plan_id}:0:extra",
        "wk:par::0:0:weight:1",
    ):
        query = FakeQuery()
        assert await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, data) is True
        assert await _overrides(db) == [], data


@pytest.mark.asyncio
async def test_custom_unknown_code_session_is_editable_without_keyerror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Tier-1 session whose code is not a PLANS key (planning.py:851 defaults
    to "custom") must edit safely -- the template lookup is guarded."""
    db = await _make_db(tmp_path, "customedit")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    assert "custom" not in PLANS
    plan_id = await _seed_tier1(db, now, codes=["custom"], exercises_by_code={
        "custom": [_ex("weird_ex", "תרגיל מותאם", weight=40.0)],
    })

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, f"wk:exm:{plan_id}:0")
    assert query.messages

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(
        query, _ctx(), 1, f"wk:par:{plan_id}:0:0:weight:5"
    )
    rows = await _overrides(db)
    assert len(rows) == 1
    assert rows[0]["code"] == "custom"
    assert rows[0]["exercise_id"] == "weird_ex"

    ref = workout_catalog.WorkoutSelectionRef(
        tier="plan", plan_id=plan_id, fact_rev=None, session_index=0
    )
    resolved = await workout_catalog.resolve_selection(db, 1, ref)
    assert await workout_catalog.collect_overrides(db, 1, resolved.session) == {
        "weird_ex": {"weight": 45.0}
    }


# ---------------------------------------------------------------------------
# Free-text edit loop: identity survives compose -> confirm.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_free_text_confirm_applies_against_the_carried_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path, "textapply")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    plan_id = await _seed_tier1(db, now, codes=["A", "B"], exercises_by_code={
        "B": [_ex("bench", "לחיצת חזה", weight=50.0), _ex("row", "חתירה", weight=40.0)],
    })

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, f"wk:ex:{plan_id}:1:1")
    flow = await conversation.get_active_flow(db, 1)
    await conversation.set_active_flow(
        db, 1, conversation.FlowName.workout_parameter_edit, step="preview",
        payload={**flow.payload, "pending_updates": [
            {"scope": "current", "field": "weight", "value": 47.5, "label": "משקל 47.5"}
        ], "scope_label": "לתרגיל חתירה"},
    )

    query = FakeQuery()
    assert await callback_plans_bot.handle_workout_setup_callback(
        query, _ctx(), 1, "wparamtext:apply"
    ) is True

    rows = await _overrides(db)
    assert len(rows) == 1
    assert rows[0]["code"] == "B"
    assert rows[0]["exercise_id"] == "row"
    assert rows[0]["value"] == 47.5
    assert f"wk:sel:{plan_id}:1" in _callbacks(query.reply_markups[-1])


@pytest.mark.asyncio
async def test_free_text_confirm_refuses_after_regeneration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The compose->confirm window is exactly where a regeneration can land.
    The edit must refuse rather than apply to the new plan."""
    db = await _make_db(tmp_path, "textstale")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    plan_id = await _seed_tier1(db, now, codes=["A"], exercises_by_code={
        "A": [_ex("bench", "לחיצת חזה", weight=50.0)],
    })

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, f"wk:ex:{plan_id}:0:0")
    flow = await conversation.get_active_flow(db, 1)
    await conversation.set_active_flow(
        db, 1, conversation.FlowName.workout_parameter_edit, step="preview",
        payload={**flow.payload, "pending_updates": [
            {"scope": "current", "field": "weight", "value": 99.0, "label": "משקל 99"}
        ]},
    )
    await _regenerate_tier1(db, now, codes=["A"], exercises_by_code={
        "A": [_ex("bench", "לחיצת חזה", weight=50.0)],
    })

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, "wparamtext:apply")

    assert await _overrides(db) == []
    assert any(a and "התוכנית התעדכנה" in a for a in query.answers)


@pytest.mark.asyncio
async def test_free_text_program_scope_writes_every_session_code_scoped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Program scope walks the whole plan through the catalog, so each write
    is scoped to a real resolved session and carries that session's ids."""
    db = await _make_db(tmp_path, "programscope")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    plan_id = await _seed_tier1(db, now, codes=["A", "B"], exercises_by_code={
        "A": [_ex("bench", "לחיצת חזה")],
        "B": [_ex("row", "חתירה")],
    })

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, f"wk:ex:{plan_id}:0:0")
    flow = await conversation.get_active_flow(db, 1)
    await conversation.set_active_flow(
        db, 1, conversation.FlowName.workout_parameter_edit, step="preview",
        payload={**flow.payload, "pending_updates": [
            {"scope": "program", "field": "rest", "value": 120, "label": "מנוחה 2:00"}
        ]},
    )

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, "wparamtext:apply")

    rows = await _overrides(db)
    by_code = {r["code"]: r for r in rows}
    assert set(by_code) == {"A", "B"}
    assert by_code["A"]["exercise_id"] == "bench"
    assert by_code["B"]["exercise_id"] == "row"
    assert all(r["field"] == "rest" and r["value"] == 120 for r in rows)


@pytest.mark.asyncio
async def test_cancel_returns_to_the_same_selected_overview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path, "cancel")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    plan_id = await _seed_tier1(db, now, codes=["A", "B"])

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, f"wk:ex:{plan_id}:1:0")

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, "wparamtext:cancel")

    assert f"wk:sel:{plan_id}:1" in _callbacks(query.reply_markups[-1])
    assert await _overrides(db) == []
    assert (await conversation.get_active_flow(db, 1)).name != conversation.FlowName.workout_parameter_edit


# ---------------------------------------------------------------------------
# Legacy compatibility (Batch 7 owns retirement).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_code_only_payload_still_applies_positionally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A v1 payload (no identity keys) keeps the old behavior untouched --
    Batch 7 owns retiring it, not Batch 6."""
    db = await _make_db(tmp_path, "legacypayload")
    _bind(monkeypatch, db)
    await conversation.set_active_flow(
        db, 1, conversation.FlowName.workout_parameter_edit, step="preview",
        payload={
            "code": "A", "exercise_index": 0,
            "pending_updates": [{"scope": "current", "field": "weight", "value": 33.0, "label": "משקל 33"}],
            "scope_label": "לתרגיל",
        },
    )

    query = FakeQuery()
    assert await callback_plans_bot.handle_workout_setup_callback(
        query, _ctx(), 1, "wparamtext:apply"
    ) is True

    rows = await _overrides(db)
    assert len(rows) == 1
    assert rows[0]["code"] == "A"
    assert rows[0]["value"] == 33.0
    # Legacy back-buttons remain the bare-code ones.
    assert "workout:A" in _callbacks(query.reply_markups[-1])


@pytest.mark.asyncio
async def test_legacy_editparams_prefixes_are_untouched_by_batch6(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pin #5's legacy flow shape is Batch 7's to change, not Batch 6's."""
    db = await _make_db(tmp_path, "legacyprefix")
    _bind(monkeypatch, db)

    query = FakeQuery()
    assert await callback_plans_bot.handle_workout_setup_callback(
        query, _ctx(), 1, "editparams:A:2"
    ) is True
    flow = await conversation.get_active_flow(db, 1)
    assert set(flow.payload.keys()) == {"code", "exercise_index"}


# ---------------------------------------------------------------------------
# Callback budget and router policy.
# ---------------------------------------------------------------------------


def test_edit_callbacks_stay_within_the_64_byte_budget() -> None:
    """`wk:par` is the longest callback this architecture mints.

    The plan's section-N budget table estimated this worst case at 38 bytes;
    the actual minted string is 35 (the estimate appears to have counted a
    slightly longer form). Asserting the MEASURED value rather than the
    planned one -- the budget conclusion ("nowhere near 64") is unchanged and
    the real number is what protects us.
    """
    from noam_coach.bot.ui import wk_exercise_callback, wk_exercise_menu_callback, wk_param_callback

    tier1 = workout_catalog.WorkoutSelectionRef(
        tier="plan", plan_id=9999999999, fact_rev=None, session_index=12
    )
    tier2 = workout_catalog.WorkoutSelectionRef(
        tier="fact", plan_id=None, fact_rev="1a2b3c4d", session_index=12
    )
    worst = wk_param_callback(tier1, 12, "weight", -2.5)
    assert worst == "wk:par:9999999999:12:12:weight:-2.5"
    assert len(worst.encode("utf-8")) == 35
    for ref in (tier1, tier2):
        for data in (
            wk_exercise_menu_callback(ref),
            wk_exercise_callback(ref, 12),
            wk_param_callback(ref, 12, "weight", -2.5),
            wk_param_callback(ref, 12, "rest", 15),
        ):
            assert len(data.encode("utf-8")) <= 64, (data, len(data.encode("utf-8")))


def test_param_writes_are_debounced_and_navigation_is_not() -> None:
    from noam_coach.bot.callback_router import _DEBOUNCE_PREFIXES

    assert "wk:par:12:0:0:weight:2.5".startswith(_DEBOUNCE_PREFIXES)
    assert not "wk:exm:12:0".startswith(_DEBOUNCE_PREFIXES)
    assert not "wk:ex:12:0:1".startswith(_DEBOUNCE_PREFIXES)


def test_edit_callbacks_do_not_collide_with_the_terminal_grammar_tokens() -> None:
    from noam_coach.services.callback_grammar import strict_extract_flow_id, strict_extract_version

    for data in (
        "wk:exm:12:0", "wk:ex:12:0:1", "wk:par:9999999999:12:12:weight:-2.5",
        "wk:par:1a2b3c4d:0:0:rest:15",
    ):
        assert strict_extract_version(data) is None, data
        assert strict_extract_flow_id(data) is None, data
