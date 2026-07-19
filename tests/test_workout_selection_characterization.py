"""Batch 1 (workout-selection architecture, review/workout-selection-architecture):
characterization tests pinning the CURRENT workout entry/selection/overview/
start/edit behavior, bugs included, before any production change.

This file exists to give the redesign an executable ground truth. Each pin
below documents a real defect confirmed in the architecture plan at
C:\\Users\\user\\.claude\\plans\\sprightly-sauteeing-parrot.md (v2.1) -- do NOT
"fix" these assertions when they look wrong; they are deliberately pinned so
later batches can prove they changed the behavior on purpose. Batches 4-7 flip
specific pins per the plan's Batch-1 pin-to-batch transition table (see plan
section M.1); any pin not listed there must stay green through this whole
project or the batch that broke it must stop and report.

Conventions follow tests/test_batch_c_stale_nextmeal_save.py:75-80 and
tests/test_set_undo.py: per-test tmp_path SQLite DB, monkeypatch.setattr on
the ``coach_bot`` facade DB (propagated to every ``@runtime_bound`` function
via noam_coach/runtime_bind.py's ``_sync``), explicit ``now=`` time injection
where the function under test accepts it (no global clock patching).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

import coach_bot
import mini_api
import planning
import user_model
from config import TZ
from db import Database
from exercise_plans import PLANS
from helpers import utc_now
from noam_coach.bot import assistant as assistant_bot
from noam_coach.bot import callback_plans as callback_plans_bot
from noam_coach.bot import ui as ui_bot
from noam_coach.services import profile as profile_services
from noam_coach.services.weekdays import WEEKDAY_SCHEMA_VERSION, local_weekday
from noam_coach.services.workout_reconciliation import reconciled_workout_days

# ---------------------------------------------------------------------------
# Shared fakes / fixtures
# ---------------------------------------------------------------------------


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
        self.message = FakeMessage()

    async def edit_message_text(
        self,
        text: str,
        reply_markup: Any = None,
        parse_mode: str | None = None,
    ) -> None:
        del parse_mode
        self.messages.append(text)
        self.reply_markups.append(reply_markup)

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        del text, show_alert


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
    # runtime_bind._sync refreshes every @runtime_bound function's globals
    # (across coach_bot.py, noam_coach/bot/*, noam_coach/services/*) from the
    # coach_bot facade immediately before each call -- rebinding coach_bot.DB
    # is sufficient for THOSE. But noam_coach/bot/ui.py's
    # resolve_todays_workout / TodaysWorkout helpers are plain module-level
    # functions with NO @runtime_bound decorator (confirmed: only the thin
    # wrapper select_todays_workout_code carries it), so they keep reading
    # ui.py's own module-scope `DB` import, never refreshed by the facade
    # sync. The established convention (tests/regression/test_audit_2026_07_18.py:78-86)
    # is to bind ui_bot.DB explicitly alongside coach_bot.DB.
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(ui_bot, "DB", db)


# A personalized Tier-1 payload whose session "A" exercises deliberately
# DIFFER from the global template PLANS["A"] (whose first exercise is the
# "bench" chest press). If the redesign is working, session A's overview must
# show "leg_press" content -- today it shows "bench" instead (pin 1/2).
_PERSONALIZED_SESSION_A_EXERCISES = [
    {
        "id": "leg_press",
        "name": "לחיצת רגליים",
        "sets": 3,
        "rmin": 10,
        "rmax": 12,
        "rest": 90,
        "weight": 80.0,
        "inc": 5.0,
        "cues": ["טווח מלא"],
        "muscle": "רגליים",
        "alts": [],
    }
]


async def _seed_tier1_plan(
    db: Database,
    user_id: int,
    now: datetime,
    *,
    codes: list[str] | None = None,
    session_a_exercises: list[dict[str, Any]] | None = None,
) -> int:
    """Seed a Tier-1 personalized plan: a plan_versions row (status='active'),
    an active_plans pointer, AND the active_workout_plan fact mirror that
    resolve_todays_workout actually reads (planning.activate_plan's real
    behavior -- see planning.py:1451-1459). Session A's exercises differ from
    PLANS["A"] by default so overview-content pins can detect template leakage.
    """
    codes = codes or ["A", "B", "C"]
    weekday = local_weekday(now)
    sessions = []
    for i, code in enumerate(codes):
        exercises = (
            session_a_exercises
            if (code == "A" and session_a_exercises is not None)
            else [{"id": f"{code.lower()}_ex", "name": f"Exercise {code}", "sets": 3, "muscle": "test"}]
        )
        sessions.append(
            {
                "index": i,
                "weekday": (weekday + i) % 7,
                "weekday_schema": WEEKDAY_SCHEMA_VERSION,
                "weekday_name": "test",
                "time": "18:00",
                "minutes": 60,
                "code": code,
                "name": f"אימון {code} מותאם אישית",
                "exercises": exercises,
            }
        )
    payload = {"frequency": len(codes), "sessions": sessions}
    plan_id = await db.execute(
        """
        INSERT INTO plan_versions(
            user_id, plan_type, title, strategy, fit_score, status, payload,
            rationale, tradeoffs, assumptions, based_on, created_at, activated_at
        ) VALUES(?, 'workout', 'Test Personalized Plan', 'balanced', 1, 'active', ?, '[]', '[]', '[]', '{}', ?, ?)
        """,
        (user_id, json.dumps(payload, ensure_ascii=False), utc_now(), utc_now()),
    )
    await db.execute(
        "INSERT INTO active_plans(user_id, plan_type, plan_id, updated_at) VALUES(?, 'workout', ?, ?)",
        (user_id, plan_id, utc_now()),
    )
    # Mirror into the fact -- exactly what planning.activate_plan does
    # (planning.py:1451-1459), and the only source resolve_todays_workout /
    # select_todays_workout_code actually read (ui.py:521-527).
    await user_model.set_fact(
        db,
        user_id,
        "active_workout_plan",
        {"plan_id": plan_id, **payload},
        kind=user_model.KIND_FACT,
        source=user_model.SOURCE_USER,
        confirmed=True,
    )
    return int(plan_id)


# ---------------------------------------------------------------------------
# Pin 1: menu:workout renders global PLANS content for the recommended code,
# NOT the personalized active-plan payload.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pin1_menu_workout_renders_plans_template_not_personalized_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """KNOWN DEFECT (root cause 3, plan section C): render_workout_overview
    resolves content via get_user_plan(code) = deep copy of the GLOBAL
    PLANS[code] template + positional overrides. It never consults the
    user's personalized active-plan session exercises. Session A here has
    "leg_press" as its only exercise; the rendered overview shows "bench"
    (PLANS["A"]'s real first exercise name) instead.
    """
    db = await _make_db(tmp_path, "pin1")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    weekday = local_weekday(now)
    await _seed_tier1_plan(
        db,
        1,
        now,
        codes=["A"],
        session_a_exercises=_PERSONALIZED_SESSION_A_EXERCISES,
    )
    # Force the weekday-match branch of resolve_todays_workout to pick "A".
    plan = await user_model.get_value(db, 1, "active_workout_plan")
    plan["sessions"][0]["weekday"] = weekday
    await user_model.set_fact(
        db, 1, "active_workout_plan", plan, kind=user_model.KIND_FACT,
        source=user_model.SOURCE_USER, confirmed=True,
    )

    query = FakeQuery()
    handled = await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, "menu:workout")

    assert handled is True
    rendered_text = query.messages[-1]
    # PIN: the template's exercise ("bench" -> "לחיצת חזה עם מוט") leaks
    # into the overview instead of the personalized "לחיצת רגליים".
    assert PLANS["A"]["exercises"][0]["name"] in rendered_text
    assert "לחיצת רגליים" not in rendered_text


# ---------------------------------------------------------------------------
# Pin 2: startworkout:A snapshots get_user_plan output (template exercises)
# into sessions.plan.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pin2_startworkout_snapshots_template_not_personalized_exercises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """KNOWN DEFECT: _handle_workout_start_actions (callback_plans.py:534-569)
    calls get_user_plan(user_id, code) -- the global template resolver -- to
    build the session snapshot, independently of whatever was shown in the
    overview. The started session's plan JSON contains PLANS["A"]'s
    exercises, not the personalized ones seeded above.
    """
    db = await _make_db(tmp_path, "pin2")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    await _seed_tier1_plan(
        db, 1, now, codes=["A"], session_a_exercises=_PERSONALIZED_SESSION_A_EXERCISES
    )

    query = FakeQuery()
    handled = await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, "startworkout:A")
    assert handled is True

    session = await db.fetch_one("SELECT * FROM sessions WHERE user_id=1 AND status='active'")
    assert session is not None
    snapshot = json.loads(session["plan"])
    snapshot_exercise_ids = {ex["id"] for ex in snapshot["exercises"]}
    # PIN: the snapshot contains the template's exercise ids ("bench" etc.),
    # not the personalized plan's "leg_press".
    assert "bench" in snapshot_exercise_ids
    assert "leg_press" not in snapshot_exercise_ids


# ---------------------------------------------------------------------------
# Pin 3: duplicate start -> IntegrityError -> existing session shown.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pin3_duplicate_start_hits_unique_index_and_shows_existing_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """INVARIANT (must stay green through every batch): the partial unique
    index ux_one_active_session_per_user (db.py:756-758) already makes a
    second concurrent/duplicate Start safe -- it shows the existing active
    session instead of creating a second one.
    """
    db = await _make_db(tmp_path, "pin3")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    await _seed_tier1_plan(db, 1, now, codes=["A"])

    query1 = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query1, _ctx(), 1, "startworkout:A")
    first_session = await db.fetch_one("SELECT id FROM sessions WHERE user_id=1 AND status='active'")
    assert first_session is not None

    query2 = FakeQuery()
    handled = await callback_plans_bot.handle_workout_setup_callback(query2, _ctx(), 1, "startworkout:A")
    assert handled is True

    all_active = await db.fetch_all("SELECT id FROM sessions WHERE user_id=1 AND status='active'")
    assert len(all_active) == 1
    assert all_active[0]["id"] == first_session["id"]


# ---------------------------------------------------------------------------
# Pin 4: param:A:2:weight:2.5 writes a positional exercise_overrides row that
# get_user_plan reflects.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pin4_positional_override_reflected_by_get_user_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """INVARIANT (legacy read path, must stay green): exercise_overrides is
    keyed (user_id, code, exercise_index, field) -- purely positional, no
    exercise identity. get_user_plan applies it on top of the PLANS[code]
    template.
    """
    db = await _make_db(tmp_path, "pin4")
    _bind(monkeypatch, db)

    base_weight = PLANS["A"]["exercises"][2]["weight"]
    query = FakeQuery()
    handled = await callback_plans_bot.handle_workout_setup_callback(
        query, _ctx(), 1, "param:A:2:weight:2.5"
    )
    assert handled is True

    row = await db.fetch_one(
        "SELECT * FROM exercise_overrides WHERE user_id=1 AND code='A' AND exercise_index=2 AND field='weight'"
    )
    assert row is not None
    assert row["value"] == pytest.approx(base_weight + 2.5)

    plan = await profile_services.get_user_plan(1, "A")
    assert plan["exercises"][2]["weight"] == pytest.approx(base_weight + 2.5)


# ---------------------------------------------------------------------------
# Pin 5: editparams:A:2 sets flow payload {"code","exercise_index"}.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pin5_edit_flow_payload_is_code_and_exercise_index_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """KNOWN DEFECT (root cause 6/f): the parameter-edit flow payload
    identifies the target exercise by bare template code + array index --
    no plan/session identity, no stable exercise id. This is exactly the
    "workout identity = bare code" defect the redesign must fix.
    """
    db = await _make_db(tmp_path, "pin5")
    _bind(monkeypatch, db)

    query = FakeQuery()
    handled = await callback_plans_bot.handle_workout_setup_callback(
        query, _ctx(), 1, "editparams:A:2"
    )
    assert handled is True

    import conversation

    flow = await conversation.get_active_flow(db, 1)
    assert flow.name == conversation.FlowName.workout_parameter_edit
    assert set(flow.payload.keys()) == {"code", "exercise_index"}
    assert flow.payload["code"] == "A"
    assert flow.payload["exercise_index"] == 2


# ---------------------------------------------------------------------------
# Pin 6: menu:workout with no plan shows the fallback text + hard-coded
# plans_keyboard.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pin6_no_plan_shows_fallback_and_hardcoded_plans_keyboard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """INVARIANT (must stay green -- the no-plan fallback path is preserved
    by the redesign, per plan section E). plans_keyboard() is hard-coded to
    exactly A/B/C/F (ui.py:404-414), independent of any user data.
    """
    db = await _make_db(tmp_path, "pin6")
    _bind(monkeypatch, db)

    query = FakeQuery()
    handled = await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, "menu:workout")
    assert handled is True
    assert "עוד אין לך תוכנית אימונים פעילה" in query.messages[-1]

    keyboard = query.reply_markups[-1]
    callback_datas = {btn.callback_data for row in keyboard.inline_keyboard for btn in row}
    assert callback_datas == {"workout:A", "workout:B", "workout:C", "workout:F", "menu:home"}


# ---------------------------------------------------------------------------
# Pin 7: after regenerating/activating a NEW plan, old workout:A and
# startworkout:A still silently resolve against PLANS["A"].
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pin7_stale_legacy_callbacks_silently_resolve_to_plans_after_regeneration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """KNOWN DEFECT (root cause 8, stale-callback unsafety): workout setup
    callbacks carry no plan/session identity and no :v version token, so an
    old Telegram message's workout:A / startworkout:A button keeps silently
    resolving against the GLOBAL PLANS["A"] template even after the user's
    active plan has been regenerated/replaced with a completely different
    plan. Nothing here detects or refuses the staleness.
    """
    db = await _make_db(tmp_path, "pin7")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    await _seed_tier1_plan(
        db, 1, now, codes=["A"], session_a_exercises=_PERSONALIZED_SESSION_A_EXERCISES
    )

    # Regenerate: supersede the old plan_versions row and activate a new one
    # (mirrors planning.activate_plan's own supersede step).
    await db.execute("UPDATE plan_versions SET status='superseded' WHERE user_id=1")
    new_payload = {
        "frequency": 1,
        "sessions": [
            {
                "index": 0, "weekday": local_weekday(now), "weekday_schema": WEEKDAY_SCHEMA_VERSION,
                "weekday_name": "test", "time": "18:00", "minutes": 60, "code": "A",
                "name": "אימון A חדש לגמרי", "exercises": [{"id": "totally_different", "name": "X", "sets": 1}],
            }
        ],
    }
    new_plan_id = await db.execute(
        """
        INSERT INTO plan_versions(
            user_id, plan_type, title, strategy, fit_score, status, payload,
            rationale, tradeoffs, assumptions, based_on, created_at, activated_at
        ) VALUES(1, 'workout', 'New Plan', 'balanced', 1, 'active', ?, '[]', '[]', '[]', '{}', ?, ?)
        """,
        (json.dumps(new_payload, ensure_ascii=False), utc_now(), utc_now()),
    )
    await db.execute(
        "UPDATE active_plans SET plan_id=?, updated_at=? WHERE user_id=1 AND plan_type='workout'",
        (new_plan_id, utc_now()),
    )

    # An "old Telegram message" still carrying workout:A / startworkout:A.
    query = FakeQuery()
    handled = await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, "workout:A")
    assert handled is True
    # PIN: it silently shows the (unchanged) global template, oblivious to
    # the plan swap -- no refusal, no staleness detection.
    assert PLANS["A"]["exercises"][0]["name"] in query.messages[-1]

    query2 = FakeQuery()
    handled2 = await callback_plans_bot.handle_workout_setup_callback(query2, _ctx(), 1, "startworkout:A")
    assert handled2 is True
    session = await db.fetch_one("SELECT * FROM sessions WHERE user_id=1 AND status='active'")
    snapshot = json.loads(session["plan"])
    snapshot_ids = {ex["id"] for ex in snapshot["exercises"]}
    assert "bench" in snapshot_ids
    assert "totally_different" not in snapshot_ids


# ---------------------------------------------------------------------------
# Pin 8: resolve_todays_workout matrix: offer_today, offer_next,
# all_done_today, no_plan.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pin8_resolve_todays_workout_matrix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """INVARIANT: the recommendation cycle logic in resolve_todays_workout
    (ui.py:521-567) is preserved by the redesign (extracted into a pure
    core, plan section E) -- this pin freezes its four outcomes before the
    extraction happens.
    """
    db = await _make_db(tmp_path, "pin8")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)  # Monday

    # no_plan
    result = await ui_bot.resolve_todays_workout(1, now=now)
    assert result.reason == "no_plan"
    assert result.code is None

    # offer_today: session A scheduled for today's weekday.
    weekday = local_weekday(now)
    await _seed_tier1_plan(db, 1, now, codes=["A", "B", "C"])
    plan = await user_model.get_value(db, 1, "active_workout_plan")
    plan["sessions"][0]["weekday"] = weekday
    plan["sessions"][1]["weekday"] = (weekday + 1) % 7
    plan["sessions"][2]["weekday"] = (weekday + 2) % 7
    await user_model.set_fact(
        db, 1, "active_workout_plan", plan, kind=user_model.KIND_FACT,
        source=user_model.SOURCE_USER, confirmed=True,
    )
    result = await ui_bot.resolve_todays_workout(1, now=now)
    assert result.reason == "offer_today"
    assert result.code == "A"

    # offer_next: mark A completed "today" -- done-today is windowed against
    # the injected `now`, not the real clock (ui.py:530, local_day_bounds_utc),
    # so the completion timestamp must be derived from the same `now` the
    # resolver is called with, not utc_now() (real clock).
    now_utc_iso = now.astimezone(timezone.utc).isoformat()
    await db.execute(
        "INSERT INTO sessions(user_id, code, name, plan, status, exercise_index, set_number, started_at, ended_at) "
        "VALUES(1, 'A', 'A', '{}', 'completed', 0, 1, ?, ?)",
        (now_utc_iso, now_utc_iso),
    )
    result = await ui_bot.resolve_todays_workout(1, now=now)
    assert result.reason == "offer_next"
    assert result.code == "B"
    assert "A" in result.done_today

    # all_done_today: complete B and C too (all three codes done today).
    await db.execute(
        "INSERT INTO sessions(user_id, code, name, plan, status, exercise_index, set_number, started_at, ended_at) "
        "VALUES(1, 'B', 'B', '{}', 'completed', 0, 1, ?, ?)",
        (now_utc_iso, now_utc_iso),
    )
    await db.execute(
        "INSERT INTO sessions(user_id, code, name, plan, status, exercise_index, set_number, started_at, ended_at) "
        "VALUES(1, 'C', 'C', '{}', 'completed', 0, 1, ?, ?)",
        (now_utc_iso, now_utc_iso),
    )
    result = await ui_bot.resolve_todays_workout(1, now=now)
    assert result.reason == "all_done_today"
    assert result.code is None
    assert set(result.done_today) == {"A", "B", "C"}


# ---------------------------------------------------------------------------
# Pin 9 (Tier-2 baseline): a fact-only weekly plan (build_weekly_plan output
# shape, freq 2 -> codes ["F","F"]) -> recommendation works, overview
# renders template content.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pin9_fact_only_plan_recommends_and_renders_template_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W1 BASELINE (plan section verification gate, W1): build_weekly_plan
    (onboarding.py:2526-2591) writes ONLY the active_workout_plan fact --
    thin sessions {weekday, time, code, name}, no plan_versions row at all.
    This is a SECOND, real population (onboarding + assistant plan flows)
    that resolve_todays_workout / menu:workout must keep working for, and
    that planning.get_active_plan (the Tier-1 authoritative source) cannot
    see. Frequency 2 legitimately produces DUPLICATE codes (["F","F"],
    exercise_plans.py SPLIT_BY_FREQUENCY) -- confirmed by the gate's E3 row.
    """
    db = await _make_db(tmp_path, "pin9")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    weekday = local_weekday(now)

    # Exact build_weekly_plan output shape (onboarding.py:2547-2555) for
    # frequency=2 (SPLIT_BY_FREQUENCY[2] == ["F", "F"]) -- NO plan_versions
    # row, matching the real writer exactly.
    fact_only_plan = {
        "frequency": 2,
        "method": "moving_weight_double_progression",
        "structure": "Full Body כפול",
        "sessions": [
            {"weekday": weekday, "time": "18:00", "code": "F", "name": PLANS["F"]["name"]},
            {"weekday": (weekday + 3) % 7, "time": "18:00", "code": "F", "name": PLANS["F"]["name"]},
        ],
        "days_source": "default",
        "availability_confirmed": False,
    }
    await user_model.set_fact(
        db, 1, "active_workout_plan", fact_only_plan, kind=user_model.KIND_FACT,
        source=user_model.SOURCE_SYSTEM, confidence=0.65, confirmed=True,
        affects=("workout_schedule",),
    )

    # Confirm this is genuinely fact-only: Tier-1 (planning.get_active_plan)
    # sees nothing.
    tier1_plan = await planning.get_active_plan(db, 1, "workout")
    assert tier1_plan is None

    result = await ui_bot.resolve_todays_workout(1, now=now)
    assert result.reason == "offer_today"
    assert result.code == "F"

    query = FakeQuery()
    handled = await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, "menu:workout")
    assert handled is True
    # Overview renders via get_user_plan -> PLANS["F"] template content.
    assert PLANS["F"]["exercises"][0]["name"] in query.messages[-1]


# ---------------------------------------------------------------------------
# Pin 10 (W2 baseline): assistant start_workout action renders template
# content with a startworkout: button.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pin10_assistant_start_workout_renders_template_with_startworkout_button(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W2 BASELINE: assistant.py's "start_workout" intent action
    (assistant.py:465-468) calls render_workout_overview_reply, a SEPARATE
    renderer from the menu:workout path that performs the exact same
    template-only resolution (get_user_plan) and emits the same
    startworkout:{code} button. Confirms the assistant surface duplicates
    the menu surface's defects rather than sharing one resolver.
    """
    db = await _make_db(tmp_path, "pin10")
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    weekday = local_weekday(now)
    await _seed_tier1_plan(
        db, 1, now, codes=["A"], session_a_exercises=_PERSONALIZED_SESSION_A_EXERCISES
    )
    plan = await user_model.get_value(db, 1, "active_workout_plan")
    plan["sessions"][0]["weekday"] = weekday
    await user_model.set_fact(
        db, 1, "active_workout_plan", plan, kind=user_model.KIND_FACT,
        source=user_model.SOURCE_USER, confirmed=True,
    )

    code = await ui_bot.select_todays_workout_code(1, now=now)
    assert code == "A"

    message = FakeMessage()
    await assistant_bot.render_workout_overview_reply(message, 1, code)

    rendered_text = message.texts[-1]
    assert PLANS["A"]["exercises"][0]["name"] in rendered_text
    assert "לחיצת רגליים" not in rendered_text  # personalized content, still absent

    keyboard = message.markups[-1]
    callback_datas = {btn.callback_data for row in keyboard.inline_keyboard for btn in row}
    assert "startworkout:A" in callback_datas


# ---------------------------------------------------------------------------
# Pin 11 (E1 baseline): start -> regenerate+activate a new plan -> complete
# -> reconciled_workout_days + adherence + mini_api._operational_snapshot
# all reflect the completion.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pin11_start_regenerate_complete_history_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E1 BASELINE: proves the row-keyed consumers (reconciled_workout_days,
    planning.adherence_snapshot, mini_api._operational_snapshot) remain
    correct across the start -> plan-regeneration -> complete -> history
    chain, even though the redesign has not happened yet. This is the
    chain the review flagged as untested; it must stay green (no regression
    in a later batch may go unnoticed) as the redesign introduces
    provenance and Tier-1/Tier-2 resolution.
    """
    db = await _make_db(tmp_path, "pin11")
    monkeypatch.setattr(mini_api, "DB", db)
    _bind(monkeypatch, db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    await _seed_tier1_plan(db, 1, now, codes=["A"])

    # Start.
    query = FakeQuery()
    await callback_plans_bot.handle_workout_setup_callback(query, _ctx(), 1, "startworkout:A")
    session = await db.fetch_one("SELECT * FROM sessions WHERE user_id=1 AND status='active'")
    assert session is not None
    session_id = session["id"]

    # Regenerate + activate a brand-new plan while the session is active.
    await db.execute("UPDATE plan_versions SET status='superseded' WHERE user_id=1")
    new_plan_id = await db.execute(
        """
        INSERT INTO plan_versions(
            user_id, plan_type, title, strategy, fit_score, status, payload,
            rationale, tradeoffs, assumptions, based_on, created_at, activated_at
        ) VALUES(1, 'workout', 'Regenerated', 'balanced', 1, 'active', ?, '[]', '[]', '[]', '{}', ?, ?)
        """,
        (json.dumps({"frequency": 1, "sessions": []}, ensure_ascii=False), utc_now(), utc_now()),
    )
    await db.execute(
        "UPDATE active_plans SET plan_id=?, updated_at=? WHERE user_id=1 AND plan_type='workout'",
        (new_plan_id, utc_now()),
    )

    # Complete the (still-open, pre-regeneration) session directly. This
    # chain does not inject `now` into reconciled_workout_days/adherence_
    # snapshot (neither accepts one), so a SINGLE wall-clock snapshot is
    # taken once and every timestamp/window below is derived from it --
    # two independent datetime.now()/utc_now() reads (UTC vs local TZ,
    # taken at different instants) previously risked a real-but-rare local-
    # midnight race where the row's UTC timestamp and the locally-computed
    # window boundary would disagree about which calendar day "now" is.
    snapshot_now = datetime.now(timezone.utc)
    ended_at = snapshot_now.isoformat()
    await db.execute(
        "UPDATE sessions SET status='completed', ended_at=? WHERE id=?",
        (ended_at, session_id),
    )

    # A window guaranteed to cover snapshot_now, derived from the SAME
    # instant (in TZ, matching production's local-calendar-day semantics,
    # daily_state.py:30-39) rather than a fresh, later datetime.now() call.
    real_today = snapshot_now.astimezone(TZ).date()
    window_start = datetime.combine(real_today, datetime.min.time(), tzinfo=TZ).astimezone(timezone.utc).isoformat()
    window_end = (
        datetime.combine(real_today, datetime.min.time(), tzinfo=TZ) + timedelta(days=2)
    ).astimezone(timezone.utc).isoformat()

    reconciled = await reconciled_workout_days(db, 1, window_start, window_end)
    assert len(reconciled) == 1

    adherence = await planning.adherence_snapshot(db, 1, window_start, window_end)
    assert adherence["workouts_completed"] == 1
    assert adherence["workout_days_reconciled"] == 1

    snapshot = await mini_api._operational_snapshot(1)
    assert snapshot["latest_session"]["id"] == session_id
    assert snapshot["latest_session"]["status"] == "completed"


# ---------------------------------------------------------------------------
# Pin 12 (W5 pin): select_todays_workout_code returning a non-PLANS code
# makes build_workout_prompt_text raise KeyError.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pin12_non_template_code_crashes_build_workout_prompt_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W5 PIN (KNOWN DEFECT): assistant.build_workout_prompt_text (:1004)
    hard-indexes PLANS[code]['name'] on the "near usual workout hour, no
    weekday match" branch. planning.repair_workout_payload legitimately
    defaults a session's code to "custom" (planning.py:851) when the
    generator omits one -- a code that is NOT a key of PLANS. Reached via
    select_todays_workout_code returning that code, this branch raises
    KeyError. build_workout_prompt_text reads the real clock directly
    (datetime.now(TZ)), so this test aligns the fact's session/typical_hour
    fields to the CURRENT real time rather than a frozen `now=` -- there is
    no injectable clock on this path today (a residual risk the redesign's
    normalization choke point is intended to close).
    """
    db = await _make_db(tmp_path, "pin12")
    _bind(monkeypatch, db)
    real_now = datetime.now(TZ)
    # A weekday that does NOT match today, so the weekday-match branch is
    # skipped and control reaches the workout_hour proximity branch.
    other_weekday = (real_now.weekday() + 3) % 7
    hour_str = f"{real_now.hour:02d}:{real_now.minute:02d}"

    await user_model.set_fact(
        db, 1, "active_workout_plan",
        {
            "frequency": 1,
            "sessions": [
                {"weekday": other_weekday, "time": hour_str, "code": "custom", "name": "Custom Session"}
            ],
        },
        kind=user_model.KIND_FACT, source=user_model.SOURCE_SYSTEM, confirmed=True,
    )
    await db.execute(
        "INSERT INTO routine_profile(user_id, profile, updated_at) VALUES(1, ?, ?)",
        (json.dumps({"workout": {"typical_hour": hour_str}}, ensure_ascii=False), utc_now()),
    )

    assert "custom" not in PLANS

    with pytest.raises(KeyError):
        await assistant_bot.build_workout_prompt_text(1)
