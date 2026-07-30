"""Pain-aware training regression tests.

Before this change, a pain report made mid-workout (painloc/painlevel ->
medical_constraints) never fed back into anything: recommend_load ignored
it, the exercise card never mentioned it, and substitution offers ("מכשיר
תפוס" / "⚠️ כאב") only matched by target muscle, so a row/pull-up could be
offered as a "safe" alternative to a lat pulldown even when the reported
pain was in the elbow (which those alternatives also load).

Covers:
  * An elbow/tennis-elbow pain report is stored as a region recommend_load
    and the exercise card can both discover (not just raw free text).
  * recommend_load never raises weight/reps for an exercise whose
    joint_load intersects an active pain region, regardless of RIR history.
  * show_session prints a specific per-exercise warning when the exercise
    about to be shown loads a painful region.
  * Sharp/strong pain (severity 3) still only offers skip/finish — no
    "continue at the same load" option.
  * Mild/moderate pain (severity 1-2) still offers alternatives, but the
    alternatives are filtered by pain region, not just target muscle.
  * constraint_banner renders a clean Hebrew label instead of a raw/mixed
    location string.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import coach_bot
import training_intelligence
import user_model
from db import Database
from exercise_plans import PLANS
from helpers import utc_now
from noam_coach.bot import callback_session as callback_session_bot
from noam_coach.bot import ui as ui_bot
from noam_coach.bot import workout as workout_bot


class _FakeQuery:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.reply_markups: list[Any] = []

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


async def _make_db(tmp_path: Path, name: str) -> Database:
    db = Database(str(tmp_path / name))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (utc_now(),),
    )
    return db


def _patch_all(monkeypatch: pytest.MonkeyPatch, db: Database) -> None:
    import noam_coach.services.training as training

    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(training, "DB", db)
    monkeypatch.setattr(callback_session_bot, "DB", db)
    monkeypatch.setattr(ui_bot, "DB", db)
    monkeypatch.setattr(workout_bot, "DB", db)


async def _insert_pain_constraint(
    db: Database, *, location: str, severity: int, created_at: str | None = None
) -> None:
    await db.execute(
        """
        INSERT INTO medical_constraints(
            user_id, kind, location, severity, status, note, affects, created_at
        ) VALUES(1, 'pain', ?, ?, 'active', 'reported during workout', '["exercise_selection"]', ?)
        """,
        (location, severity, created_at or utc_now()),
    )


# ---------------------------------------------------------------------------
# training_intelligence.active_pain_regions — the aggregation itself
# ---------------------------------------------------------------------------


def test_active_pain_regions_recognizes_tennis_elbow_free_text() -> None:
    rows = [
        {
            "kind": "pain",
            "status": "active",
            "location": "טניס אלכן",
            "severity": 2,
            "created_at": utc_now(),
        }
    ]
    regions = training_intelligence.active_pain_regions(rows)
    assert "elbow" in regions
    assert regions["elbow"].label == "מרפק"


def test_active_pain_regions_recognizes_english_token_from_in_workout_report() -> None:
    rows = [
        {
            "kind": "pain",
            "status": "active",
            "location": "elbow",
            "severity": 2,
            "created_at": utc_now(),
        }
    ]
    regions = training_intelligence.active_pain_regions(rows)
    assert "elbow" in regions
    assert regions["elbow"].label == "מרפק"


def test_active_pain_regions_expires_after_ttl() -> None:
    from datetime import datetime, timedelta, timezone

    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    rows = [
        {"kind": "pain", "status": "active", "location": "elbow", "severity": 2, "created_at": old}
    ]
    regions = training_intelligence.active_pain_regions(rows)
    assert regions == {}


def test_active_pain_regions_ignores_resolved_status() -> None:
    rows = [
        {
            "kind": "pain",
            "status": "resolved",
            "location": "elbow",
            "severity": 2,
            "created_at": utc_now(),
        }
    ]
    assert training_intelligence.active_pain_regions(rows) == {}


def test_pain_safety_guidance_escalates_sharp_pain_without_diagnosis() -> None:
    region = training_intelligence.ActivePainRegion(
        region="elbow",
        label="מרפק",
        severity=4,
        age_days=0,
    )

    guidance = training_intelligence.pain_safety_guidance(region)

    assert "לעצור" in guidance
    assert "בדיקה מקצועית" in guidance
    assert "אבח" not in guidance


def test_pain_safety_guidance_mentions_persistent_active_pain() -> None:
    region = training_intelligence.ActivePainRegion(
        region="elbow",
        label="מרפק",
        severity=2,
        age_days=8,
    )

    guidance = training_intelligence.pain_safety_guidance(region)

    assert "כבר כמה ימים" in guidance
    assert "בדיקה מקצועית" in guidance


# ---------------------------------------------------------------------------
# recommend_load never raises weight/reps for a painful joint
# ---------------------------------------------------------------------------


async def _add_completed_session(
    db: Database, *, exercise_id: str, sets: list[tuple[int, int, float]]
) -> None:
    plan = {
        "name": "Test",
        "exercises": [
            {"id": exercise_id, "name": exercise_id, "sets": len(sets), "rmin": 8, "rmax": 12, "inc": 2.5, "weight": 20}
        ],
    }
    sid = await db.execute(
        "INSERT INTO sessions(user_id, code, name, plan, status, exercise_index, set_number, started_at, ended_at) "
        "VALUES(1,'T','Test',?, 'completed', 0, ?, ?, ?)",
        (json.dumps(plan, ensure_ascii=False), len(sets) + 1, utc_now(), utc_now()),
    )
    for set_no, (reps, rir, weight) in enumerate(sets, start=1):
        await db.execute(
            "INSERT INTO sets(session_id, exercise_id, exercise_name, set_number, weight, reps, rir, source, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, 'telegram_adjusted', ?)",
            (sid, exercise_id, exercise_id, set_no, weight, reps, rir, utc_now()),
        )


@pytest.mark.asyncio
async def test_recommend_load_holds_weight_when_elbow_pain_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """one_arm_row loads ("back", "elbow") in CATALOG. Even a perfect,
    mastery-grade session (top reps, RIR>=2 on every set) must not raise the
    weight while elbow pain is active — pain caution outranks RIR history."""
    import noam_coach.services.training as training

    db = await _make_db(tmp_path, "pain_recommend_load.db")
    _patch_all(monkeypatch, db)

    await _insert_pain_constraint(db, location="elbow", severity=2)
    # A textbook "mastered" session: full reps, RIR 3 on every set.
    await _add_completed_session(
        db, exercise_id="one_arm_row", sets=[(12, 3, 20), (12, 3, 20), (12, 3, 20)]
    )

    current_exercise = {"id": "one_arm_row", "name": "חתירה ביד אחת", "sets": 3, "rmin": 8, "rmax": 12, "inc": 2.5, "weight": 20}
    weight, reps, why = await training.recommend_load(1, current_exercise)

    assert weight == 20.0  # never raised despite a mastery-grade session
    assert reps == 8  # never nudged toward the top of the range either
    assert "מרפק" in why


@pytest.mark.asyncio
async def test_recommend_load_reduces_when_pain_active_and_recent_sessions_were_hard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pain still blocks progression, but it should not hide the existing
    deload rule: if the same exercise has three hard sessions in a row, a
    relevant active pain report makes the conservative choice a small load
    reduction, not just holding the same weight."""
    import noam_coach.services.training as training

    db = await _make_db(tmp_path, "pain_hard_history_recommend_load.db")
    _patch_all(monkeypatch, db)

    await _insert_pain_constraint(db, location="elbow", severity=2)
    for _ in range(3):
        await _add_completed_session(
            db, exercise_id="one_arm_row", sets=[(8, 0, 20), (8, 0, 20), (8, 0, 20)]
        )

    current_exercise = {"id": "one_arm_row", "name": "חתירה ביד אחת", "sets": 3, "rmin": 8, "rmax": 12, "inc": 2.5, "weight": 20}
    weight, reps, why = await training.recommend_load(1, current_exercise)

    assert weight < 20.0
    assert reps == 8
    assert "מרפק" in why
    assert "מורידים" in why


@pytest.mark.asyncio
async def test_recommend_load_decision_explains_pain_and_hard_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recommendation should be explainable as structured data, not only
    as free Hebrew text. This locks the coach-facing reasoning surface."""
    import noam_coach.services.training as training

    db = await _make_db(tmp_path, "pain_hard_history_decision.db")
    _patch_all(monkeypatch, db)

    await _insert_pain_constraint(db, location="elbow", severity=2)
    for _ in range(3):
        await _add_completed_session(
            db, exercise_id="one_arm_row", sets=[(8, 0, 20), (8, 0, 20), (8, 0, 20)]
        )

    current_exercise = {"id": "one_arm_row", "name": "חתירה ביד אחת", "sets": 3, "rmin": 8, "rmax": 12, "inc": 2.5, "weight": 20}
    decision = await training.recommend_load_decision(1, current_exercise)
    audit = decision.to_audit_dict()

    assert decision.decision == "reduce_load_for_pain_and_hard_history"
    assert decision.weight < 20.0
    assert "active_pain:elbow" in decision.signals
    assert "hard_sessions:3" in decision.signals
    assert audit["decision"] == decision.decision
    assert audit["confidence"] >= 80


@pytest.mark.asyncio
async def test_recommend_load_decision_marks_missing_rir_as_lower_completeness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unknown RIR must not become fake confidence. The coach can still make a
    conservative recommendation, but the audit should say that the signal is
    incomplete."""
    import noam_coach.services.training as training

    db = await _make_db(tmp_path, "missing_rir_decision.db")
    _patch_all(monkeypatch, db)

    await _add_completed_session(
        db, exercise_id="one_arm_row", sets=[(10, -1, 20), (10, -1, 20), (10, -1, 20)]
    )

    current_exercise = {"id": "one_arm_row", "name": "חתירה ביד אחת", "sets": 3, "rmin": 8, "rmax": 12, "inc": 2.5, "weight": 20}
    decision = await training.recommend_load_decision(1, current_exercise)

    assert decision.decision == "hold_or_progress_reps"
    assert decision.weight == 20.0
    assert decision.data_completeness < 80
    assert "rir_missing" in decision.signals


@pytest.mark.asyncio
async def test_recommend_load_progresses_normally_without_pain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control: the exact same mastery-grade session, with no pain reported,
    DOES raise the weight — proves the pain check doesn't just always hold."""
    import noam_coach.services.training as training

    db = await _make_db(tmp_path, "no_pain_recommend_load.db")
    _patch_all(monkeypatch, db)

    await _add_completed_session(
        db, exercise_id="one_arm_row", sets=[(12, 3, 20), (12, 3, 20), (12, 3, 20)]
    )

    current_exercise = {"id": "one_arm_row", "name": "חתירה ביד אחת", "sets": 3, "rmin": 8, "rmax": 12, "inc": 2.5, "weight": 20}
    weight, _reps, _why = await training.recommend_load(1, current_exercise)

    assert weight > 20.0


@pytest.mark.asyncio
async def test_recommend_load_decision_explains_clean_progression(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import noam_coach.services.training as training

    db = await _make_db(tmp_path, "clean_progression_decision.db")
    _patch_all(monkeypatch, db)

    await _add_completed_session(
        db, exercise_id="one_arm_row", sets=[(12, 3, 20), (12, 3, 20), (12, 3, 20)]
    )

    current_exercise = {"id": "one_arm_row", "name": "חתירה ביד אחת", "sets": 3, "rmin": 8, "rmax": 12, "inc": 2.5, "weight": 20}
    decision = await training.recommend_load_decision(1, current_exercise)

    assert decision.decision == "increase_load"
    assert decision.weight > 20.0
    assert "mastered_top_range" in decision.signals
    assert decision.missing_context == ()


@pytest.mark.asyncio
async def test_recommend_load_unaffected_by_pain_in_unrelated_region(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Knee pain must not cap progression on an exercise that only loads
    back/elbow — the check is joint-specific, not a blanket freeze."""
    import noam_coach.services.training as training

    db = await _make_db(tmp_path, "unrelated_pain.db")
    _patch_all(monkeypatch, db)

    await _insert_pain_constraint(db, location="knee", severity=2)
    await _add_completed_session(
        db, exercise_id="one_arm_row", sets=[(12, 3, 20), (12, 3, 20), (12, 3, 20)]
    )

    current_exercise = {"id": "one_arm_row", "name": "חתירה ביד אחת", "sets": 3, "rmin": 8, "rmax": 12, "inc": 2.5, "weight": 20}
    weight, _reps, _why = await training.recommend_load(1, current_exercise)

    assert weight > 20.0


# ---------------------------------------------------------------------------
# show_session — specific per-exercise warning
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_show_session_prints_specific_pain_warning_before_relevant_exercise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path, "pain_warning.db")
    _patch_all(monkeypatch, db)

    await _insert_pain_constraint(db, location="elbow", severity=2)
    plan = {
        "name": "Test",
        "exercises": [
            {
                "id": "one_arm_row",
                "name": "חתירה ביד אחת",
                "sets": 3,
                "rmin": 8,
                "rmax": 12,
                "inc": 2.5,
                "weight": 20,
                "cues": ["דגש טכני"],
                "muscle": "גב",
                "alts": [],
            }
        ],
    }
    sid = await db.execute(
        "INSERT INTO sessions(user_id, code, name, plan, status, exercise_index, set_number, started_at) "
        "VALUES(1,'T','Test',?, 'active', 0, 1, ?)",
        (json.dumps(plan, ensure_ascii=False), utc_now()),
    )

    query = _FakeQuery()
    await workout_bot.show_session(query, 1, sid)

    text = query.messages[-1]
    assert "כאב" in text and "מרפק" in text


@pytest.mark.asyncio
async def test_show_session_exposes_load_explanation_button(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path, "load_explanation_button.db")
    _patch_all(monkeypatch, db)

    plan = {
        "name": "Test",
        "exercises": [
            {
                "id": "one_arm_row",
                "name": "חתירה ביד אחת",
                "sets": 3,
                "rmin": 8,
                "rmax": 12,
                "inc": 2.5,
                "weight": 20,
                "cues": ["דגש טכני"],
                "muscle": "גב",
                "alts": [],
            }
        ],
    }
    sid = await db.execute(
        "INSERT INTO sessions(user_id, code, name, plan, status, exercise_index, set_number, started_at) "
        "VALUES(1,'T','Test',?, 'active', 0, 1, ?)",
        (json.dumps(plan, ensure_ascii=False), utc_now()),
    )

    query = _FakeQuery()
    await workout_bot.show_session(query, 1, sid)

    labels = [button.text for row in query.reply_markups[-1].inline_keyboard for button in row]
    assert "איך חושב?" in labels


@pytest.mark.asyncio
async def test_load_explanation_callback_renders_structured_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path, "load_explanation_callback.db")
    _patch_all(monkeypatch, db)

    await _insert_pain_constraint(db, location="elbow", severity=2)
    await _add_completed_session(
        db, exercise_id="one_arm_row", sets=[(12, 3, 20), (12, 3, 20), (12, 3, 20)]
    )
    plan = {
        "name": "Test",
        "exercises": [
            {
                "id": "one_arm_row",
                "name": "חתירה ביד אחת",
                "sets": 3,
                "rmin": 8,
                "rmax": 12,
                "inc": 2.5,
                "weight": 20,
                "cues": ["דגש טכני"],
                "muscle": "גב",
                "alts": [],
            }
        ],
    }
    sid = await db.execute(
        "INSERT INTO sessions(user_id, code, name, plan, status, exercise_index, set_number, started_at) "
        "VALUES(1,'T','Test',?, 'active', 0, 1, ?)",
        (json.dumps(plan, ensure_ascii=False), utc_now()),
    )
    session = await db.fetch_one("SELECT * FROM sessions WHERE id=?", (sid,))
    assert session is not None

    query = _FakeQuery()
    await callback_session_bot.handle_session_action_callback(
        query,
        context=None,
        user_id=1,
        data=coach_bot.session_action_data("loadwhy", dict(session)),
    )

    text = query.messages[-1]
    assert "איך חושב?" in text
    assert "משקל מומלץ" in text
    assert "כאב פעיל: elbow" in text
    assert "ביטחון:" in text
    labels = [button.text for row in query.reply_markups[-1].inline_keyboard for button in row]
    assert "↩️ חזרה לאימון" in labels


@pytest.mark.asyncio
async def test_show_session_escalates_sharp_active_pain_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path, "sharp_pain_warning.db")
    _patch_all(monkeypatch, db)

    await _insert_pain_constraint(db, location="elbow", severity=4)
    plan = {
        "name": "Test",
        "exercises": [
            {
                "id": "one_arm_row",
                "name": "חתירה ביד אחת",
                "sets": 3,
                "rmin": 8,
                "rmax": 12,
                "inc": 2.5,
                "weight": 20,
                "cues": ["דגש טכני"],
                "muscle": "גב",
                "alts": [],
            }
        ],
    }
    sid = await db.execute(
        "INSERT INTO sessions(user_id, code, name, plan, status, exercise_index, set_number, started_at) "
        "VALUES(1,'T','Test',?, 'active', 0, 1, ?)",
        (json.dumps(plan, ensure_ascii=False), utc_now()),
    )

    query = _FakeQuery()
    await workout_bot.show_session(query, 1, sid)

    text = query.messages[-1]
    assert "לעצור" in text
    assert "בדיקה מקצועית" in text


@pytest.mark.asyncio
async def test_show_session_mentions_persistent_active_pain_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import datetime, timedelta, timezone

    db = await _make_db(tmp_path, "persistent_pain_warning.db")
    _patch_all(monkeypatch, db)

    created_at = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    await _insert_pain_constraint(db, location="elbow", severity=2, created_at=created_at)
    plan = {
        "name": "Test",
        "exercises": [
            {
                "id": "one_arm_row",
                "name": "חתירה ביד אחת",
                "sets": 3,
                "rmin": 8,
                "rmax": 12,
                "inc": 2.5,
                "weight": 20,
                "cues": ["דגש טכני"],
                "muscle": "גב",
                "alts": [],
            }
        ],
    }
    sid = await db.execute(
        "INSERT INTO sessions(user_id, code, name, plan, status, exercise_index, set_number, started_at) "
        "VALUES(1,'T','Test',?, 'active', 0, 1, ?)",
        (json.dumps(plan, ensure_ascii=False), utc_now()),
    )

    query = _FakeQuery()
    await workout_bot.show_session(query, 1, sid)

    text = query.messages[-1]
    assert "כבר כמה ימים" in text
    assert "בדיקה מקצועית" in text


@pytest.mark.asyncio
async def test_show_session_no_pain_warning_for_unrelated_exercise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path, "no_pain_warning.db")
    _patch_all(monkeypatch, db)

    await _insert_pain_constraint(db, location="knee", severity=2)
    plan = {
        "name": "Test",
        "exercises": [
            {
                "id": "one_arm_row",
                "name": "חתירה ביד אחת",
                "sets": 3,
                "rmin": 8,
                "rmax": 12,
                "inc": 2.5,
                "weight": 20,
                "cues": ["דגש טכני"],
                "muscle": "גב",
                "alts": [],
            }
        ],
    }
    sid = await db.execute(
        "INSERT INTO sessions(user_id, code, name, plan, status, exercise_index, set_number, started_at) "
        "VALUES(1,'T','Test',?, 'active', 0, 1, ?)",
        (json.dumps(plan, ensure_ascii=False), utc_now()),
    )

    query = _FakeQuery()
    await workout_bot.show_session(query, 1, sid)

    text = query.messages[-1]
    assert "מרפק" not in text
    assert "בגלל שדיווחת" not in text


# ---------------------------------------------------------------------------
# painlevel handler — severity branching + fact mirroring + region-aware alts
# ---------------------------------------------------------------------------


async def _make_lat_pull_session(db: Database) -> dict[str, Any]:
    """Use the real "F" (Full Body) plan's lat_pull_fb exercise, whose real
    alts include "one_arm_row" — a genuine CATALOG-tagged elbow-loading
    exercise — so the pain-region filtering test exercises real plan data,
    not a synthetic fixture."""
    plan = {"name": PLANS["F"]["name"], "exercises": [PLANS["F"]["exercises"][3]]}
    assert plan["exercises"][0]["id"] == "lat_pull_fb"
    sid = await db.execute(
        "INSERT INTO sessions(user_id, code, name, plan, status, exercise_index, set_number, started_at) "
        "VALUES(1,'T','Test',?, 'active', 0, 1, ?)",
        (json.dumps(plan, ensure_ascii=False), utc_now()),
    )
    session = await db.fetch_one("SELECT * FROM sessions WHERE id=?", (sid,))
    assert session is not None
    return dict(session)


async def _make_all_elbow_alts_session(db: Database) -> dict[str, Any]:
    plan = {
        "name": "All elbow alternatives",
        "exercises": [
            {
                "id": "lat_pull_fb",
                "name": "Lat pull",
                "sets": 3,
                "rmin": 8,
                "rmax": 12,
                "inc": 2.5,
                "weight": 40,
                "cues": ["דגש טכני"],
                "muscle": "גב",
                "alts": [
                    {"id": "one_arm_row", "name": "Alt Row", "weight": 20},
                    {"id": "bar_curl", "name": "Alt Curl", "weight": 15},
                    {"id": "rope_push", "name": "Alt Push", "weight": 25},
                ],
            }
        ],
    }
    sid = await db.execute(
        "INSERT INTO sessions(user_id, code, name, plan, status, exercise_index, set_number, started_at) "
        "VALUES(1,'T','Test',?, 'active', 0, 1, ?)",
        (json.dumps(plan, ensure_ascii=False), utc_now()),
    )
    session = await db.fetch_one("SELECT * FROM sessions WHERE id=?", (sid,))
    assert session is not None
    return dict(session)


@pytest.mark.asyncio
async def test_severe_pain_only_offers_skip_and_finish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path, "severe_pain.db")
    _patch_all(monkeypatch, db)
    session = await _make_lat_pull_session(db)

    query = _FakeQuery()
    painloc_data = coach_bot.session_action_data("painloc", session, "elbow")
    await callback_session_bot.handle_session_action_callback(
        query, context=None, user_id=1, data=painloc_data
    )
    refreshed = await db.fetch_one("SELECT * FROM sessions WHERE id=?", (session["id"],))

    severity_4_data = coach_bot.session_action_data("painlevel", dict(refreshed), 4)
    await callback_session_bot.handle_session_action_callback(
        query, context=None, user_id=1, data=severity_4_data
    )

    markup = query.reply_markups[-1]
    labels = [btn.text for row in markup.inline_keyboard for btn in row]
    assert labels == ["דלג", "סיים"]
    assert "4/10" in query.messages[-1]


@pytest.mark.asyncio
async def test_mild_pain_with_no_safe_alternatives_offers_only_skip_and_finish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path, "no_safe_pain_alternatives.db")
    _patch_all(monkeypatch, db)
    session = await _make_all_elbow_alts_session(db)

    query = _FakeQuery()
    painloc_data = coach_bot.session_action_data("painloc", session, "elbow")
    await callback_session_bot.handle_session_action_callback(
        query, context=None, user_id=1, data=painloc_data
    )
    refreshed = await db.fetch_one("SELECT * FROM sessions WHERE id=?", (session["id"],))

    severity_2_data = coach_bot.session_action_data("painlevel", dict(refreshed), 2)
    await callback_session_bot.handle_session_action_callback(
        query, context=None, user_id=1, data=severity_2_data
    )

    markup = query.reply_markups[-1]
    labels = [btn.text for row in markup.inline_keyboard for btn in row]
    assert labels == ["דלג", "סיים"]
    assert "אין לי חלופה מספיק בטוחה" in query.messages[-1]
    assert "Alt Row" not in query.messages[-1]
    assert "Alt Curl" not in query.messages[-1]
    assert "Alt Push" not in query.messages[-1]


@pytest.mark.asyncio
async def test_occupied_without_pain_still_offers_regular_alternatives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path, "occupied_without_pain_all_elbow_alts.db")
    _patch_all(monkeypatch, db)
    session = await _make_all_elbow_alts_session(db)

    query = _FakeQuery()
    data = coach_bot.session_action_data("occupied", session)
    await callback_session_bot.handle_session_action_callback(
        query, context=None, user_id=1, data=data
    )

    markup = query.reply_markups[-1]
    labels = [btn.text for row in markup.inline_keyboard for btn in row]
    assert any("Alt Row" in label for label in labels)
    assert any("Alt Curl" in label for label in labels)
    assert any("Alt Push" in label for label in labels)
    assert any(label == "דלג" for label in labels)


@pytest.mark.asyncio
async def test_mild_pain_filters_real_lat_pull_alternatives_by_pain_region(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """lat_pull_fb's real alts are assisted_pullup, cable_row, one_arm_row.
    All are now CATALOG-tagged as elbow-loading pulls/rows, so with elbow pain
    just reported none should be offered as a "safe" swap."""
    db = await _make_db(tmp_path, "mild_pain.db")
    _patch_all(monkeypatch, db)
    session = await _make_lat_pull_session(db)

    query = _FakeQuery()
    painloc_data = coach_bot.session_action_data("painloc", session, "elbow")
    await callback_session_bot.handle_session_action_callback(
        query, context=None, user_id=1, data=painloc_data
    )
    refreshed = await db.fetch_one("SELECT * FROM sessions WHERE id=?", (session["id"],))

    severity_2_data = coach_bot.session_action_data("painlevel", dict(refreshed), 2)
    await callback_session_bot.handle_session_action_callback(
        query, context=None, user_id=1, data=severity_2_data
    )

    markup = query.reply_markups[-1]
    labels = [btn.text for row in markup.inline_keyboard for btn in row]
    assert labels == ["דלג", "סיים"]
    assert "אין לי חלופה מספיק בטוחה" in query.messages[-1]
    assert not any("מתח בסיוע" in label for label in labels)
    assert not any("חתירה בכבל" in label for label in labels)
    assert not any("חתירה ביד אחת" in label for label in labels)


@pytest.mark.asyncio
async def test_painlevel_mirrors_into_training_limitations_fact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before this fix, an in-workout pain report only reached
    medical_constraints and never touched the planning limitation fact, so it had
    zero effect on future plan generation (planning.py's adapt_exercises
    reads training_limitations, not medical_constraints)."""
    db = await _make_db(tmp_path, "fact_mirror.db")
    _patch_all(monkeypatch, db)
    session = await _make_lat_pull_session(db)

    query = _FakeQuery()
    painloc_data = coach_bot.session_action_data("painloc", session, "elbow")
    await callback_session_bot.handle_session_action_callback(
        query, context=None, user_id=1, data=painloc_data
    )
    refreshed = await db.fetch_one("SELECT * FROM sessions WHERE id=?", (session["id"],))
    severity_2_data = coach_bot.session_action_data("painlevel", dict(refreshed), 2)
    await callback_session_bot.handle_session_action_callback(
        query, context=None, user_id=1, data=severity_2_data
    )

    fact = await user_model.get_fact(db, 1, "training_limitations")
    assert fact is not None
    assert "מרפק" in fact["value"]["location"]


@pytest.mark.asyncio
async def test_painlevel_survives_a_failing_limitation_mirror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing mirror must not surface as an error on a pain report that worked.

    The mirror (`training_limitations`) ran *after* the `except _StaleSetStep`
    block that closes the pain transaction, and nothing guarded it. So a failure
    there escaped the handler: the constraint row was already committed and the
    pain genuinely recorded, but the user saw an error and the two stores
    silently disagreed -- `medical_constraints` said "elbow", the planning fact
    said nothing.

    Pain reporting is the one interaction that must never look broken. The
    constraint is what actually gates load and exercise selection; the mirror is
    a projection for planning. A projection failing is not a reason to tell the
    user their pain was not recorded.
    """
    db = await _make_db(tmp_path, "mirror_failure.db")
    _patch_all(monkeypatch, db)
    session = await _make_lat_pull_session(db)

    real_set_fact = user_model.set_fact

    async def _fail_on_limitations(*args: Any, **kwargs: Any) -> Any:
        # Positional: (db, user_id, key, value, ...) -- key is args[2].
        if len(args) > 2 and args[2] == "training_limitations":
            raise RuntimeError("fact store unavailable")
        return await real_set_fact(*args, **kwargs)

    monkeypatch.setattr(user_model, "set_fact", _fail_on_limitations)

    query = _FakeQuery()
    painloc_data = coach_bot.session_action_data("painloc", session, "elbow")
    await callback_session_bot.handle_session_action_callback(
        query, context=None, user_id=1, data=painloc_data
    )
    refreshed = await db.fetch_one("SELECT * FROM sessions WHERE id=?", (session["id"],))
    severity_data = coach_bot.session_action_data("painlevel", dict(refreshed), 4)

    # The handler must absorb the projection failure, not propagate it. It is
    # reaching this call at all -- rather than raising -- that is the assertion.
    await callback_session_bot.handle_session_action_callback(
        query, context=None, user_id=1, data=severity_data
    )

    # Severity 4 is a stop: the user still gets the stop card, not an error.
    assert any("לעצור" in message for message in query.messages)

    # The safety-critical write is intact and still gates training.
    rows = await db.fetch_all(
        "SELECT * FROM medical_constraints WHERE user_id=1 AND kind='pain'"
    )
    assert len(rows) == 1
    assert rows[0]["location"] == "elbow"
    assert rows[0]["severity"] == 4
    assert rows[0]["status"] == "active"


# ---------------------------------------------------------------------------
# constraint_banner — clean Hebrew label instead of raw/mixed text
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_constraint_banner_shows_clean_hebrew_label_not_raw_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path, "banner.db")
    _patch_all(monkeypatch, db)
    await _insert_pain_constraint(db, location="elbow", severity=2)

    banner = await ui_bot.constraint_banner(1)
    assert "מרפק" in banner
    assert "elbow" not in banner


@pytest.mark.asyncio
async def test_constraint_banner_dedupes_mixed_hebrew_and_english_same_region(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reproduces the exact screenshot bug: an onboarding-time free-text
    report ("טניס אלכן") and an in-workout report ("elbow") are two
    different rows for the same region — the banner must show "מרפק" once,
    not both raw strings side by side."""
    db = await _make_db(tmp_path, "banner_dedupe.db")
    _patch_all(monkeypatch, db)
    await _insert_pain_constraint(db, location="טניס אלכן", severity=1)
    await _insert_pain_constraint(db, location="elbow", severity=2)

    banner = await ui_bot.constraint_banner(1)
    assert banner.count("מרפק") == 1
    assert "elbow" not in banner
    assert "טניס אלכן" not in banner
