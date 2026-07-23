"""Batch 4 (workout-selection architecture): the `wk:` selector and overview
graph, driven through the REAL callback entry point
(``handle_workout_setup_callback``) rather than the catalog service directly --
these tests exist to prove the handler wiring, not the already-tested pure
resolution logic.

Covers plan section M.3's Batch-4 read-side list: dynamic names, ⭐
recommended, ✅ done-today + banner, regeneration between render and tap →
refusal + fresh selector, single-session direct overview, Tier-2 selector
preserving the duplicate-code split, and the assistant surface rendering
catalog content with `wk:` buttons.

Conventions follow tests/test_workout_selection_characterization.py: per-test
tmp_path SQLite DB, ``monkeypatch.setattr`` on both the ``coach_bot`` facade
and ``ui_bot`` (ui.py's module-scope DB is not refreshed by the facade sync).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

import coach_bot
import user_model
from config import TZ
from db import Database
from exercise_plans import PLANS
from helpers import utc_now
from noam_coach.bot import assistant as assistant_bot
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


def _labels(markup: Any) -> list[str]:
    return [btn.text for row in markup.inline_keyboard for btn in row]


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
    """Supersede the active plan with a NEW plan_versions row -- exactly what
    planning.activate_plan does (copy-on-write; never an in-place payload
    UPDATE, which the Batch-3 architecture guard forbids)."""
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


async def _mark_completed_today(db: Database, code: str, now: datetime) -> None:
    """Seed a session completed *today*, as the helper's name promises.

    ``resolve_todays_workout`` computes its done-today window from
    ``datetime.now(TZ)`` -- its caller passes no ``now`` -- so a row stamped
    with the fixture date falls outside that window and the ✅ marker never
    appears. Anchoring to the real current day keeps the scenario honest
    without pinning production behaviour: ``now``'s clock time is preserved so
    the seeded session still sits at the fixture's hour of day.
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
# Tier-1 selector.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tier1_selector_lists_every_session_by_real_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A >=2-session plan shows ALL sessions by their personalized names, each
    with a wk:sel callback carrying (plan_id, session_index)."""
    db = await _make_db(tmp_path, "t1sel")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    plan_id = await _seed_tier1(db, now, codes=["A", "B", "C"])

    query = FakeQuery()
    assert await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, "menu:workout") is True

    datas = _callbacks(query.reply_markups[-1])
    assert [d for d in datas if d.startswith("wk:sel:")] == [
        f"wk:sel:{plan_id}:0", f"wk:sel:{plan_id}:1", f"wk:sel:{plan_id}:2",
    ]
    labels = " ".join(_labels(query.reply_markups[-1]))
    for code in ("A", "B", "C"):
        assert f"אימון {code} מותאם אישית" in labels


@pytest.mark.asyncio
async def test_selector_marks_recommended_with_star_and_done_today_with_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⭐ marks the recommendation (a hint, never a forced selection -- every
    row stays tappable); ✅ marks anything already completed today, and the
    'כבר התאמנת היום' banner appears alongside it."""
    db = await _make_db(tmp_path, "marks")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    await _seed_tier1(db, now, codes=["A", "B", "C"])
    await _mark_completed_today(db, "A", now)

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, "menu:workout")

    labels = _labels(query.reply_markups[-1])
    done_labels = [label for label in labels if "✅" in label]
    star_labels = [label for label in labels if "⭐" in label]
    assert len(done_labels) == 1 and "A" in done_labels[0]
    assert len(star_labels) == 1 and "A" not in star_labels[0]
    assert "כבר התאמנת היום" in query.messages[-1]
    # Every session remains selectable despite the recommendation.
    assert len([d for d in _callbacks(query.reply_markups[-1]) if d.startswith("wk:sel:")]) == 3


@pytest.mark.asyncio
async def test_single_session_plan_skips_selector_and_opens_overview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One selectable session -> straight to its overview (no pointless
    one-row list), and Back goes home rather than to an empty selector."""
    db = await _make_db(tmp_path, "single")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    plan_id = await _seed_tier1(db, now, codes=["A"], session_a_exercises=_PERSONALIZED_A)

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, "menu:workout")

    assert "לחיצת רגליים" in query.messages[-1]
    datas = _callbacks(query.reply_markups[-1])
    assert f"wk:start:{plan_id}:0" in datas
    assert "menu:home" in datas
    assert "wk:list" not in datas


@pytest.mark.asyncio
async def test_selecting_a_session_renders_that_sessions_personalized_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tapping session B opens B's OWN payload content and a Start button
    carrying B's index -- not the recommendation's, and not the template's."""
    db = await _make_db(tmp_path, "selb")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    plan_id = await _seed_tier1(db, now, codes=["A", "B", "C"])

    query = FakeQuery()
    assert await callback_plans_bot.handle_workout_setup_callback(
        query, _ctx(), 1, f"wk:sel:{plan_id}:1"
    ) is True

    assert "Exercise B" in query.messages[-1]
    assert "Exercise A" not in query.messages[-1]
    datas = _callbacks(query.reply_markups[-1])
    assert f"wk:start:{plan_id}:1" in datas
    assert "wk:list" in datas


@pytest.mark.asyncio
async def test_wk_list_returns_to_the_selector(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The Back button on an overview (`wk:list`) re-renders the selector."""
    db = await _make_db(tmp_path, "backlist")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    plan_id = await _seed_tier1(db, now, codes=["A", "B"])

    query = FakeQuery()
    assert await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, "wk:list") is True
    datas = _callbacks(query.reply_markups[-1])
    assert f"wk:sel:{plan_id}:0" in datas and f"wk:sel:{plan_id}:1" in datas


# ---------------------------------------------------------------------------
# Tier-2 selector (fact-only users).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tier2_selector_preserves_duplicate_code_split(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fact-only ["F","F"] plan yields TWO distinct index-keyed choices
    sharing one fact_rev -- the split is never collapsed into one row."""
    db = await _make_db(tmp_path, "t2sel")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    fact = await _seed_tier2(db, now, codes=["F", "F"])
    fact_rev = workout_catalog.compute_fact_rev(fact)

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, "menu:workout")

    datas = [d for d in _callbacks(query.reply_markups[-1]) if d.startswith("wk:fsel:")]
    assert datas == [f"wk:fsel:{fact_rev}:0", f"wk:fsel:{fact_rev}:1"]


@pytest.mark.asyncio
async def test_tier2_selection_renders_template_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier-2 sessions carry no exercises of their own; content comes from the
    guarded PLANS template path (W1 residual, by design) and Start carries the
    fingerprinted identity."""
    db = await _make_db(tmp_path, "t2over")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    fact = await _seed_tier2(db, now, codes=["F", "F"])
    fact_rev = workout_catalog.compute_fact_rev(fact)

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, f"wk:fsel:{fact_rev}:1")

    assert PLANS["F"]["exercises"][0]["name"] in query.messages[-1]
    assert f"wk:fstart:{fact_rev}:1" in _callbacks(query.reply_markups[-1])


# ---------------------------------------------------------------------------
# Stale references: regeneration between render and tap.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_tier1_selection_refuses_and_renders_fresh_selector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regeneration between selector render and tap: the old plan_id refuses
    with stale_plan_reference and the user gets the CURRENT plan's selector --
    never a silently-resolved session from the superseded plan."""
    db = await _make_db(tmp_path, "stale1")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    old_plan_id = await _seed_tier1(db, now, codes=["A", "B", "C"])
    new_plan_id = await _regenerate_tier1(db, now, codes=["A", "B", "C"])
    assert new_plan_id != old_plan_id

    query = FakeQuery()
    assert await callback_plans_bot.handle_workout_setup_callback(
        query, _ctx(), 1, f"wk:sel:{old_plan_id}:0"
    ) is True

    assert any(a and "התוכנית התעדכנה" in a for a in query.answers)
    datas = _callbacks(query.reply_markups[-1])
    assert f"wk:sel:{new_plan_id}:0" in datas
    # Exact-segment check: a substring test would false-positive when the old
    # id is a prefix of the new one (plan_id 1 vs 10).
    assert not any(d.startswith(f"wk:sel:{old_plan_id}:") for d in datas)


@pytest.mark.asyncio
async def test_stale_tier2_same_length_replacement_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The blocker-1 regression: a fact replaced by a DIFFERENT plan with the
    SAME number of sessions must invalidate the old callback. A bare
    (fact, index) identity would have silently resolved to the wrong session;
    the content fingerprint catches it."""
    db = await _make_db(tmp_path, "stale2")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    old_fact = await _seed_tier2(db, now, codes=["F", "F"])
    old_rev = workout_catalog.compute_fact_rev(old_fact)

    new_fact = await _seed_tier2(db, now, codes=["A", "B"])  # same length, different content
    new_rev = workout_catalog.compute_fact_rev(new_fact)
    assert len(new_fact["sessions"]) == len(old_fact["sessions"])
    assert new_rev != old_rev

    query = FakeQuery()
    assert await callback_plans_bot.handle_workout_setup_callback(
        query, _ctx(), 1, f"wk:fsel:{old_rev}:0"
    ) is True

    assert any(a and "התוכנית התעדכנה" in a for a in query.answers)
    datas = _callbacks(query.reply_markups[-1])
    assert all(old_rev not in d for d in datas)
    assert f"wk:fsel:{new_rev}:0" in datas


@pytest.mark.asyncio
async def test_malformed_and_out_of_range_refs_refuse_without_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hand-crafted / corrupted callbacks must refuse, never raise."""
    db = await _make_db(tmp_path, "malformed")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    plan_id = await _seed_tier1(db, now, codes=["A", "B"])

    for data in (f"wk:sel:{plan_id}:99", "wk:sel:notanint:0", "wk:sel:1", f"wk:sel:{plan_id}:-1"):
        query = FakeQuery()
        assert await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, data) is True
        assert query.messages, data  # something was rendered; no exception escaped


# ---------------------------------------------------------------------------
# Active-session guard and no-plan fallback.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_active_session_reopens_instead_of_offering_a_second(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With an active session, every read-side entry point reopens it rather
    than offering a new selection (unchanged product behavior)."""
    db = await _make_db(tmp_path, "activeguard")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    plan_id = await _seed_tier1(db, now, codes=["A", "B"])
    await db.execute(
        "INSERT INTO sessions(user_id, code, name, plan, status, exercise_index, set_number, started_at) "
        "VALUES(1, 'A', 'A', ?, 'active', 0, 1, ?)",
        # A fully normalized snapshot -- the shape workout.show_session hard-reads
        # (workout.py:426 needs "cues"), i.e. what materialize_snapshot produces.
        (json.dumps({"name": "A", "exercises": [{"id": "x", "name": "X", "sets": 3, "rmin": 8,
                                                 "rmax": 12, "rest": 90, "weight": 20.0, "inc": 2.5,
                                                 "cues": [], "alts": [], "muscle": "test"}]}), utc_now()),
    )

    for data in ("menu:workout", "wk:list", f"wk:sel:{plan_id}:1"):
        query = FakeQuery()
        assert await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, data) is True
        assert not any(d.startswith("wk:sel:") for d in _callbacks(query.reply_markups[-1])), data


@pytest.mark.asyncio
async def test_no_plan_falls_back_to_plans_keyboard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No plan of either tier -> the honest no-plan message (never a false
    'already completed' claim -- audit F-A3 preserved)."""
    db = await _make_db(tmp_path, "noplan")
    _bind(monkeypatch, db)

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, "menu:workout")

    assert "עוד אין לך תוכנית אימונים פעילה" in query.messages[-1]
    assert not any(d.startswith("wk:") for d in _callbacks(query.reply_markups[-1]))


# ---------------------------------------------------------------------------
# Assistant surface consistency (W2).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assistant_overview_shows_catalog_content_with_wk_button(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The assistant card renders the personalized session and its Start
    button carries that exact identity -- it can never display one workout and
    start another."""
    db = await _make_db(tmp_path, "assistant")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    plan_id = await _seed_tier1(db, now, codes=["A"], session_a_exercises=_PERSONALIZED_A)

    recommended = await workout_catalog.resolve_recommended(db, 1)
    assert recommended is not None
    message = FakeMessage()
    await assistant_bot.render_workout_overview_reply(message, 1, recommended.ref)

    assert "לחיצת רגליים" in message.texts[-1]
    assert PLANS["A"]["exercises"][0]["name"] not in message.texts[-1]
    assert f"wk:start:{plan_id}:0" in _callbacks(message.markups[-1])


@pytest.mark.asyncio
async def test_assistant_and_callback_surfaces_render_identical_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both surfaces share build_workout_overview_text_v2, so they cannot
    drift apart -- the defect pin #10 characterized."""
    db = await _make_db(tmp_path, "twosurfaces")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    plan_id = await _seed_tier1(db, now, codes=["A", "B"], session_a_exercises=_PERSONALIZED_A)

    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, f"wk:sel:{plan_id}:0")

    ref = workout_catalog.WorkoutSelectionRef(tier="plan", plan_id=plan_id, fact_rev=None, session_index=0)
    message = FakeMessage()
    await assistant_bot.render_workout_overview_reply(message, 1, ref)

    assert query.messages[-1] == message.texts[-1]


@pytest.mark.asyncio
async def test_assistant_stale_ref_does_not_render_an_unverified_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the plan regenerates before the assistant renders, it says so
    instead of showing a card from the superseded plan."""
    db = await _make_db(tmp_path, "assistantstale")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    old_plan_id = await _seed_tier1(db, now, codes=["A", "B"])
    await _regenerate_tier1(db, now, codes=["A", "B"])

    stale = workout_catalog.WorkoutSelectionRef(
        tier="plan", plan_id=old_plan_id, fact_rev=None, session_index=0
    )
    message = FakeMessage()
    await assistant_bot.render_workout_overview_reply(message, 1, stale)

    assert "התוכנית התעדכנה" in message.texts[-1]
    assert "wk:list" in _callbacks(message.markups[-1])
