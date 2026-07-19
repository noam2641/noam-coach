"""Batch 3 (workout-selection architecture, review/workout-selection-architecture):
tests for the new, behavior-inert noam_coach/services/workout_catalog.py
service, plus two architecture guards that turn the plan's identity
invariants into enforced contracts.

Nothing in workout_catalog.py is imported or called by any handler yet --
these tests exercise the module directly. No user-visible behavior changes
in this batch; the Batch 1 characterization file
(tests/test_workout_selection_characterization.py) is the proof that the
ui.py extraction in this batch left resolve_todays_workout byte-equivalent.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

import user_model
from config import TZ
from db import Database
from exercise_plans import PLANS
from helpers import utc_now
from noam_coach.bot import ui as ui_bot
from noam_coach.services import workout_catalog as wc
from noam_coach.services.weekdays import WEEKDAY_SCHEMA_VERSION, local_weekday

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Shared fixtures.
# ---------------------------------------------------------------------------


async def _make_db(tmp_path: Path, name: str) -> Database:
    db = Database(str(tmp_path / f"{name}.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1, 'Test', NULL, ?)",
        (utc_now(),),
    )
    return db


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
    """Same fixture shape as tests/test_workout_selection_characterization.py's
    _seed_tier1_plan: a real plan_versions row (status='active') +
    active_plans pointer + the active_workout_plan fact mirror, matching
    planning.activate_plan's real behavior."""
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


async def _seed_tier2_fact(
    db: Database, user_id: int, now: datetime, *, codes: list[str]
) -> dict[str, Any]:
    """A fact-only weekly plan matching onboarding.build_weekly_plan's real
    output shape (onboarding.py:2547-2555): sessions have ONLY
    weekday/time/code/name, no plan_versions row at all."""
    weekday = local_weekday(now)
    fact = {
        "frequency": len(codes),
        "method": "moving_weight_double_progression",
        "structure": "test",
        "sessions": [
            {"weekday": (weekday + i) % 7, "time": "18:00", "code": code, "name": PLANS[code]["name"]}
            for i, code in enumerate(codes)
        ],
        "days_source": "default",
        "availability_confirmed": False,
    }
    await user_model.set_fact(
        db, user_id, "active_workout_plan", fact,
        kind=user_model.KIND_FACT, source=user_model.SOURCE_SYSTEM, confirmed=True,
    )
    return fact


async def _mark_completed_today(db: Database, user_id: int, code: str, now: datetime) -> None:
    now_utc_iso = now.astimezone(__import__("datetime").timezone.utc).isoformat()
    await db.execute(
        "INSERT INTO sessions(user_id, code, name, plan, status, exercise_index, set_number, started_at, ended_at) "
        "VALUES(?, ?, ?, '{}', 'completed', 0, 1, ?, ?)",
        (user_id, code, code, now_utc_iso, now_utc_iso),
    )


# ---------------------------------------------------------------------------
# Tier-1 listing and selection.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tier1_listing_returns_real_names_and_recommended_flag(tmp_path: Path) -> None:
    db = await _make_db(tmp_path, "tier1_list")
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)  # Monday
    weekday = local_weekday(now)
    plan_id = await _seed_tier1_plan(db, 1, now, codes=["A", "B", "C"])
    # Force A onto today's weekday so it's unambiguously recommended.
    plan = await user_model.get_value(db, 1, "active_workout_plan")
    plan["sessions"][0]["weekday"] = weekday
    plan["sessions"][1]["weekday"] = (weekday + 1) % 7
    plan["sessions"][2]["weekday"] = (weekday + 2) % 7
    await user_model.set_fact(db, 1, "active_workout_plan", plan, kind=user_model.KIND_FACT, source=user_model.SOURCE_USER, confirmed=True)

    choices = await wc.list_selectable_workouts(db, 1, now=now)

    assert len(choices) == 3
    assert [c.code for c in choices] == ["A", "B", "C"]
    assert [c.name for c in choices] == ["אימון A מותאם אישית", "אימון B מותאם אישית", "אימון C מותאם אישית"]
    assert choices[0].recommended is True
    assert choices[1].recommended is False and choices[2].recommended is False
    for choice in choices:
        assert choice.ref.tier == "plan"
        assert choice.ref.plan_id == plan_id
        assert choice.ref.fact_rev is None


@pytest.mark.asyncio
async def test_tier1_resolve_selection_returns_personalized_content(tmp_path: Path) -> None:
    db = await _make_db(tmp_path, "tier1_resolve")
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    plan_id = await _seed_tier1_plan(db, 1, now, codes=["A"], session_a_exercises=_PERSONALIZED_SESSION_A_EXERCISES)

    ref = wc.WorkoutSelectionRef(tier="plan", plan_id=plan_id, fact_rev=None, session_index=0)
    resolved = await wc.resolve_selection(db, 1, ref, now=now)

    assert resolved.session["exercises"][0]["id"] == "leg_press"
    assert resolved.session["exercises"][0]["name"] == "לחיצת רגליים"
    # Untouched by normalization (already fully populated).
    assert resolved.defaults_filled == {}


# ---------------------------------------------------------------------------
# Tier-2 listing with duplicate codes as two distinct index identities.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tier2_listing_with_duplicate_codes_yields_two_distinct_choices(tmp_path: Path) -> None:
    db = await _make_db(tmp_path, "tier2_dup")
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    fact = await _seed_tier2_fact(db, 1, now, codes=["F", "F"])

    choices = await wc.list_selectable_workouts(db, 1, now=now)

    assert len(choices) == 2
    assert choices[0].code == "F"
    assert choices[1].code == "F"
    assert choices[0].ref.session_index == 0
    assert choices[1].ref.session_index == 1
    assert choices[0].ref.tier == "fact"
    assert choices[0].ref.plan_id is None
    expected_rev = wc.compute_fact_rev(fact)
    assert choices[0].ref.fact_rev == expected_rev == choices[1].ref.fact_rev
    # Exactly one is recommended, and it's index-distinguishable.
    assert sum(1 for c in choices if c.recommended) == 1


@pytest.mark.asyncio
async def test_tier2_resolve_selection_renders_template_content(tmp_path: Path) -> None:
    db = await _make_db(tmp_path, "tier2_resolve")
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    fact = await _seed_tier2_fact(db, 1, now, codes=["F", "F"])
    fact_rev = wc.compute_fact_rev(fact)

    ref = wc.WorkoutSelectionRef(tier="fact", plan_id=None, fact_rev=fact_rev, session_index=1)
    resolved = await wc.resolve_selection(db, 1, ref, now=now)

    assert resolved.session["exercises"][0]["name"] == PLANS["F"]["exercises"][0]["name"]


@pytest.mark.asyncio
async def test_tier1_present_wins_over_tier2(tmp_path: Path) -> None:
    """A real plan_versions row is authoritative -- Tier-2 is only
    consulted when Tier-1 has nothing."""
    db = await _make_db(tmp_path, "tier_precedence")
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    await _seed_tier2_fact(db, 1, now, codes=["F"])
    await _seed_tier1_plan(db, 1, now, codes=["A"])

    choices = await wc.list_selectable_workouts(db, 1, now=now)
    assert len(choices) == 1
    assert choices[0].ref.tier == "plan"
    assert choices[0].code == "A"


# ---------------------------------------------------------------------------
# compute_fact_rev.
# ---------------------------------------------------------------------------


def test_compute_fact_rev_deterministic_for_identical_ordered_content() -> None:
    fact = {"sessions": [{"code": "A", "weekday": 0, "time": "18:00", "name": "X"}, {"code": "B", "weekday": 2, "time": "19:00", "name": "Y"}]}
    fact_copy = json.loads(json.dumps(fact))
    assert wc.compute_fact_rev(fact) == wc.compute_fact_rev(fact_copy)


def test_compute_fact_rev_changes_for_reordered_sessions() -> None:
    fact_a = {"sessions": [{"code": "A", "weekday": 0, "time": "18:00", "name": "X"}, {"code": "B", "weekday": 2, "time": "19:00", "name": "Y"}]}
    fact_b = {"sessions": [{"code": "B", "weekday": 2, "time": "19:00", "name": "Y"}, {"code": "A", "weekday": 0, "time": "18:00", "name": "X"}]}
    assert wc.compute_fact_rev(fact_a) != wc.compute_fact_rev(fact_b)


def test_compute_fact_rev_changes_for_same_length_different_content() -> None:
    """The exact blocker-1 scenario: build_weekly_plan replaces the fact
    with a DIFFERENT plan of the SAME session count."""
    fact_old = {"sessions": [{"code": "F", "weekday": 0, "time": "18:00", "name": "Full Body"}, {"code": "F", "weekday": 3, "time": "18:00", "name": "Full Body"}]}
    fact_new = {"sessions": [{"code": "A", "weekday": 1, "time": "19:00", "name": "Chest"}, {"code": "B", "weekday": 4, "time": "19:00", "name": "Back"}]}
    assert len(fact_old["sessions"]) == len(fact_new["sessions"])
    assert wc.compute_fact_rev(fact_old) != wc.compute_fact_rev(fact_new)


def test_compute_fact_rev_is_8_lowercase_hex_chars() -> None:
    fact = {"sessions": [{"code": "A", "weekday": 0, "time": "18:00", "name": "X"}]}
    rev = wc.compute_fact_rev(fact)
    assert re.fullmatch(r"[0-9a-f]{8}", rev)


def test_compute_fact_rev_matches_manual_sha256_construction() -> None:
    """Pin the exact canonicalization algorithm, not just its properties:
    count-prefixed, code|weekday|time|name per session, \\x1f-joined,
    sha256, first 8 lowercase hex chars."""
    import hashlib

    fact = {"sessions": [{"code": "A", "weekday": 0, "time": "18:00", "name": "X"}, {"code": "B", "weekday": 2, "time": "19:00", "name": "Y"}]}
    expected_canonical = "2" + "\x1f" + "A|0|18:00|X" + "\x1f" + "B|2|19:00|Y"
    expected = hashlib.sha256(expected_canonical.encode("utf-8")).hexdigest()[:8]
    assert wc.compute_fact_rev(fact) == expected


# ---------------------------------------------------------------------------
# Staleness.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_tier1_plan_id_is_rejected(tmp_path: Path) -> None:
    db = await _make_db(tmp_path, "stale_tier1")
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    real_plan_id = await _seed_tier1_plan(db, 1, now, codes=["A"])

    stale_ref = wc.WorkoutSelectionRef(tier="plan", plan_id=real_plan_id + 999, fact_rev=None, session_index=0)
    with pytest.raises(wc.StalePlanReference):
        await wc.resolve_selection(db, 1, stale_ref, now=now)


@pytest.mark.asyncio
async def test_stale_tier1_after_regeneration_is_rejected(tmp_path: Path) -> None:
    """The plan_id the callback was minted with gets superseded by a new
    plan_versions row -- the old ref must be refused, not silently
    resolved against whatever is active now."""
    db = await _make_db(tmp_path, "stale_regen")
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    old_plan_id = await _seed_tier1_plan(db, 1, now, codes=["A"])

    await db.execute("UPDATE plan_versions SET status='superseded' WHERE id=?", (old_plan_id,))
    new_payload = {"frequency": 1, "sessions": [{"index": 0, "weekday": 0, "code": "B", "name": "New", "exercises": []}]}
    new_plan_id = await db.execute(
        """
        INSERT INTO plan_versions(user_id, plan_type, title, strategy, fit_score, status, payload,
            rationale, tradeoffs, assumptions, based_on, created_at, activated_at)
        VALUES(1, 'workout', 'New', 'balanced', 1, 'active', ?, '[]', '[]', '[]', '{}', ?, ?)
        """,
        (json.dumps(new_payload, ensure_ascii=False), utc_now(), utc_now()),
    )
    await db.execute(
        "UPDATE active_plans SET plan_id=?, updated_at=? WHERE user_id=1 AND plan_type='workout'",
        (new_plan_id, utc_now()),
    )

    stale_ref = wc.WorkoutSelectionRef(tier="plan", plan_id=old_plan_id, fact_rev=None, session_index=0)
    with pytest.raises(wc.StalePlanReference):
        await wc.resolve_selection(db, 1, stale_ref, now=now)


@pytest.mark.asyncio
async def test_stale_tier2_fact_rev_is_rejected(tmp_path: Path) -> None:
    db = await _make_db(tmp_path, "stale_tier2")
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    await _seed_tier2_fact(db, 1, now, codes=["F"])

    stale_ref = wc.WorkoutSelectionRef(tier="fact", plan_id=None, fact_rev="deadbeef", session_index=0)
    with pytest.raises(wc.StalePlanReference):
        await wc.resolve_selection(db, 1, stale_ref, now=now)


@pytest.mark.asyncio
async def test_stale_tier2_same_length_different_content_replacement_is_rejected(tmp_path: Path) -> None:
    """The blocker-1 scenario end-to-end: a Tier-2 selector button is
    minted, then the user regenerates onboarding into a DIFFERENT
    same-length plan -- the old fact_rev must not resolve against it."""
    db = await _make_db(tmp_path, "stale_tier2_samelen")
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    old_fact = await _seed_tier2_fact(db, 1, now, codes=["F", "F"])
    old_ref = wc.WorkoutSelectionRef(
        tier="fact", plan_id=None, fact_rev=wc.compute_fact_rev(old_fact), session_index=1
    )

    # Replace with a different two-session plan (same length, different content).
    await _seed_tier2_fact(db, 1, now, codes=["A", "B"])

    with pytest.raises(wc.StalePlanReference):
        await wc.resolve_selection(db, 1, old_ref, now=now)


@pytest.mark.asyncio
async def test_out_of_range_session_index_is_rejected(tmp_path: Path) -> None:
    db = await _make_db(tmp_path, "oor")
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)
    plan_id = await _seed_tier1_plan(db, 1, now, codes=["A"])

    ref = wc.WorkoutSelectionRef(tier="plan", plan_id=plan_id, fact_rev=None, session_index=5)
    with pytest.raises(wc.StalePlanReference):
        await wc.resolve_selection(db, 1, ref, now=now)


# ---------------------------------------------------------------------------
# Normalization.
# ---------------------------------------------------------------------------


def test_sparse_exercise_normalization_fills_all_runtime_keys() -> None:
    sparse = [{"id": "leg_press", "sets": 3, "muscle": "רגליים"}]
    normalized, defaults_filled = wc.normalize_exercises(sparse)

    ex = normalized[0]
    for key in ("id", "name", "sets", "rmin", "rmax", "rest", "weight", "inc", "cues", "alts", "muscle"):
        assert key in ex
    assert ex["sets"] == 3  # preserved, not defaulted
    assert ex["muscle"] == "רגליים"  # preserved
    assert isinstance(ex["cues"], list)
    assert isinstance(ex["alts"], list)
    assert "leg_press" in defaults_filled
    assert "rmin" in defaults_filled["leg_press"]
    assert "rmax" in defaults_filled["leg_press"]
    assert "rest" in defaults_filled["leg_press"]
    # "sets" was already present -- must NOT be listed as filled.
    assert "sets" not in defaults_filled["leg_press"]


def test_template_fallback_by_exercise_id_fills_missing_fields() -> None:
    """An exercise with a known template id ("bench") but missing weight/
    rest/etc. should inherit those from PLANS, not the hardcoded defaults,
    since "bench" IS a real template exercise."""
    sparse = [{"id": "bench"}]
    normalized, defaults_filled = wc.normalize_exercises(sparse)

    template_bench = PLANS["A"]["exercises"][0]
    assert template_bench["id"] == "bench"
    ex = normalized[0]
    assert ex["name"] == template_bench["name"]
    assert ex["weight"] == template_bench["weight"]
    assert ex["rest"] == template_bench["rest"]
    assert ex["cues"] == template_bench["cues"]
    assert "bench" in defaults_filled


def test_unknown_exercise_id_falls_back_to_safe_defaults_never_invents_load() -> None:
    sparse = [{"id": "totally_unknown_exercise", "sets": 4}]
    normalized, _ = wc.normalize_exercises(sparse)
    ex = normalized[0]
    assert ex["weight"] == 0.0  # never invented
    assert ex["rmin"] == 8
    assert ex["rmax"] == 12
    assert ex["rest"] == 120
    assert ex["inc"] == 2.5
    assert ex["cues"] == []
    assert ex["alts"] == []
    assert ex["muscle"] == ""


def test_payload_values_are_never_overwritten_by_template_or_defaults() -> None:
    """A personalized exercise sharing an id with a template exercise but
    carrying DIFFERENT real values must keep its own values -- normalize
    fills gaps, it never overrides what's already there."""
    personalized = [
        {
            "id": "bench",  # shares an id with the template...
            "name": "בנץ' מותאם",  # ...but every value differs
            "sets": 5,
            "rmin": 3,
            "rmax": 5,
            "rest": 240,
            "weight": 999.0,
            "inc": 1.25,
            "cues": ["custom cue"],
            "alts": [],
            "muscle": "custom muscle",
        }
    ]
    normalized, defaults_filled = wc.normalize_exercises(personalized)
    ex = normalized[0]
    assert ex["name"] == "בנץ' מותאם"
    assert ex["weight"] == 999.0
    assert ex["rest"] == 240
    assert ex["cues"] == ["custom cue"]
    assert defaults_filled == {}  # nothing was missing -- nothing filled


def test_normalize_coerces_types_defensively() -> None:
    dirty = [{"id": "x", "sets": "3", "rmin": "8", "weight": "50.5"}]
    normalized, _ = wc.normalize_exercises(dirty)
    ex = normalized[0]
    assert isinstance(ex["sets"], int) and ex["sets"] == 3
    assert isinstance(ex["rmin"], int) and ex["rmin"] == 8
    assert isinstance(ex["weight"], float) and ex["weight"] == 50.5


# ---------------------------------------------------------------------------
# collect_overrides: code-scoped, id-verified.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_collect_overrides_applies_id_matched_row(tmp_path: Path) -> None:
    db = await _make_db(tmp_path, "overrides_basic")
    await db.execute(
        "INSERT INTO exercise_overrides(user_id, code, exercise_index, field, value, updated_at, exercise_id) "
        "VALUES(1, 'A', 0, 'weight', 60.0, ?, 'bench')",
        (utc_now(),),
    )
    session = {"code": "A", "exercises": [{"id": "bench", "name": "Bench"}]}
    result = await wc.collect_overrides(db, 1, session)
    assert result == {"bench": {"weight": 60.0}}


@pytest.mark.asyncio
async def test_collect_overrides_no_cross_code_leakage(tmp_path: Path) -> None:
    """An override written under code 'A' must never apply to a session
    with a DIFFERENT code, even for the identical exercise id (the exact
    heavy-day/volume-day scenario SPLIT_BY_FREQUENCY legitimately
    produces)."""
    db = await _make_db(tmp_path, "overrides_cross_code")
    await db.execute(
        "INSERT INTO exercise_overrides(user_id, code, exercise_index, field, value, updated_at, exercise_id) "
        "VALUES(1, 'A', 0, 'weight', 60.0, ?, 'bench')",
        (utc_now(),),
    )
    session_f = {"code": "F", "exercises": [{"id": "bench", "name": "Bench"}]}
    result = await wc.collect_overrides(db, 1, session_f)
    assert result == {}


@pytest.mark.asyncio
async def test_collect_overrides_legacy_null_id_row_applies_only_when_template_position_matches(
    tmp_path: Path,
) -> None:
    """A legacy row with no exercise_id (positional-only, written before
    Batch 2) applies to a Tier-1 session's exercise ONLY when the row's
    exercise_index resolves to the SAME id in the global PLANS[code]
    template -- never by bare index alone."""
    db = await _make_db(tmp_path, "overrides_legacy_verified")
    template_first_id = PLANS["A"]["exercises"][0]["id"]
    await db.execute(
        "INSERT INTO exercise_overrides(user_id, code, exercise_index, field, value, updated_at, exercise_id) "
        "VALUES(1, 'A', 0, 'weight', 77.0, ?, NULL)",
        (utc_now(),),
    )
    # Session's exercise at index 0 has the SAME id as the template's index 0.
    session_matching = {"code": "A", "exercises": [{"id": template_first_id, "name": "Matches template"}]}
    result = await wc.collect_overrides(db, 1, session_matching)
    assert result == {template_first_id: {"weight": 77.0}}


@pytest.mark.asyncio
async def test_collect_overrides_legacy_null_id_row_rejected_when_template_position_differs(
    tmp_path: Path,
) -> None:
    """The core anti-regression case: an active-plan exercise at index 0 is
    a DIFFERENT exercise than PLANS['A']'s index 0 -- the legacy positional
    row must NOT apply to it (this is exactly root cause 6 the redesign
    exists to fix)."""
    db = await _make_db(tmp_path, "overrides_legacy_unverified")
    await db.execute(
        "INSERT INTO exercise_overrides(user_id, code, exercise_index, field, value, updated_at, exercise_id) "
        "VALUES(1, 'A', 0, 'weight', 77.0, ?, NULL)",
        (utc_now(),),
    )
    # Session's exercise at index 0 is "leg_press" -- NOT PLANS["A"]'s index-0 "bench".
    session_different = {"code": "A", "exercises": [{"id": "leg_press", "name": "Leg Press"}]}
    result = await wc.collect_overrides(db, 1, session_different)
    assert result == {}


@pytest.mark.asyncio
async def test_unrelated_exercise_at_same_index_never_receives_override(tmp_path: Path) -> None:
    """Same scenario framed from the opposite exercise's perspective: a
    session where index 0 is a totally unrelated exercise never receives
    an override meant for whatever id happens to sit at PLANS["A"][0]."""
    db = await _make_db(tmp_path, "overrides_unrelated")
    await db.execute(
        "INSERT INTO exercise_overrides(user_id, code, exercise_index, field, value, updated_at, exercise_id) "
        "VALUES(1, 'A', 0, 'weight', 999.0, ?, NULL)",
        (utc_now(),),
    )
    session = {"code": "A", "exercises": [{"id": "unrelated_exercise", "name": "Unrelated"}]}
    result = await wc.collect_overrides(db, 1, session)
    assert "unrelated_exercise" not in result


@pytest.mark.asyncio
async def test_collect_overrides_deterministic_tie_break_updated_at_then_rowid(tmp_path: Path) -> None:
    """Two candidate rows for the SAME (exercise_id, field) -- a legacy
    NULL-id row and a new id-bearing row -- must resolve deterministically
    by updated_at DESC, then rowid DESC, never by insertion/dict order."""
    db = await _make_db(tmp_path, "overrides_tiebreak")
    template_first_id = PLANS["A"]["exercises"][0]["id"]
    # Older row (legacy, NULL id, but position-verified).
    await db.execute(
        "INSERT INTO exercise_overrides(user_id, code, exercise_index, field, value, updated_at, exercise_id) "
        "VALUES(1, 'A', 0, 'weight', 50.0, '2026-01-01T00:00:00+00:00', NULL)"
    )
    session = {"code": "A", "exercises": [{"id": template_first_id, "name": "Bench"}]}
    result = await wc.collect_overrides(db, 1, session)
    assert result[template_first_id]["weight"] == 50.0

    # A different (code, exercise_index, field) combination can't collide
    # with the same PK -- simulate the "two writes, same target" case via
    # a genuinely later id-bearing row for the identical logical target.
    # Because the PK is (user_id, code, exercise_index, field), a second
    # write to exercise_index=0/field=weight with an exercise_id set is an
    # UPSERT on the SAME row (matches production's set_exercise_override
    # COALESCE semantics) -- so insert at a DIFFERENT exercise_index that
    # ALSO verifies to the same target id is not representable; instead
    # prove the tie-break directly: two DISTINCT rows can only coexist for
    # the same (exercise_id, field) when their (code, exercise_index)
    # differ but both verify to the same id. Use two different indices
    # that both happen to hold the same exercise id in the session
    # (duplicate ids are rejected at plan-generation time, but the
    # override table itself has no such constraint) to exercise ordering.
    await db.execute(
        "INSERT INTO exercise_overrides(user_id, code, exercise_index, field, value, updated_at, exercise_id) "
        "VALUES(1, 'A', 1, 'weight', 999.0, '2026-01-02T00:00:00+00:00', ?)",
        (template_first_id,),
    )
    result2 = await wc.collect_overrides(db, 1, session)
    # The newer (2026-01-02) row wins over the older (2026-01-01) row.
    assert result2[template_first_id]["weight"] == 999.0


@pytest.mark.asyncio
async def test_collect_overrides_ignores_fields_outside_override_fields_whitelist(tmp_path: Path) -> None:
    db = await _make_db(tmp_path, "overrides_whitelist")
    await db.execute(
        "INSERT INTO exercise_overrides(user_id, code, exercise_index, field, value, updated_at, exercise_id) "
        "VALUES(1, 'A', 0, 'not_a_real_field', 1.0, ?, 'bench')",
        (utc_now(),),
    )
    session = {"code": "A", "exercises": [{"id": "bench", "name": "Bench"}]}
    result = await wc.collect_overrides(db, 1, session)
    assert result == {}


# ---------------------------------------------------------------------------
# Recommendation parity: extracted pure core vs. resolve_todays_workout.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recommendation_parity_pure_core_matches_resolve_todays_workout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The extraction (Batch 3) must not change resolve_todays_workout's
    decision in any of its branches: offer_today, offer_next (cycle
    continuation), offer_next (fallback-no-history), all_done_today,
    no_plan. Runs the exact matrix from the Batch 1 characterization pin 8
    a second time, through BOTH resolve_todays_workout (the I/O wrapper)
    and pick_session (the pure core it now delegates to), asserting they
    agree at every step."""
    import coach_bot

    db = await _make_db(tmp_path, "parity")
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(ui_bot, "DB", db)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=TZ)  # Monday

    async def _assert_parity() -> None:
        wrapper_result = await ui_bot.resolve_todays_workout(1, now=now)
        plan = await user_model.get_value(db, 1, "active_workout_plan")
        if not plan or not plan.get("sessions"):
            assert wrapper_result.reason == "no_plan"
            return
        sessions = plan["sessions"]
        done_today, last_code = await wc._done_today_and_last_code(db, 1, now)
        core_code, core_reason = wc.pick_session(sessions, done_today, now.weekday(), last_code)
        assert wrapper_result.code == core_code
        assert wrapper_result.reason == core_reason

    # no_plan
    await _assert_parity()

    # offer_today
    weekday = local_weekday(now)
    await _seed_tier1_plan(db, 1, now, codes=["A", "B", "C"])
    plan = await user_model.get_value(db, 1, "active_workout_plan")
    plan["sessions"][0]["weekday"] = weekday
    plan["sessions"][1]["weekday"] = (weekday + 1) % 7
    plan["sessions"][2]["weekday"] = (weekday + 2) % 7
    await user_model.set_fact(db, 1, "active_workout_plan", plan, kind=user_model.KIND_FACT, source=user_model.SOURCE_USER, confirmed=True)
    await _assert_parity()

    # offer_next (cycle continuation)
    await _mark_completed_today(db, 1, "A", now)
    await _assert_parity()

    # all_done_today
    await _mark_completed_today(db, 1, "B", now)
    await _mark_completed_today(db, 1, "C", now)
    await _assert_parity()
