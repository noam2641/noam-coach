"""A slot is the professional need, not the exercise that implements it (A11b).

A11a landed the vocabulary and the reader helpers and deliberately wrote
nothing. This file covers what A11b adds: minting an identity that survives
regeneration, resolving substitutions by that identity instead of by list
position, and carrying a bounded reason so A12 can tell a safety-driven change
from a convenience one.

The defect that motivates the identity work is measurable, not theoretical.
`current["alts"].index(alt)` matched by VALUE, so:

    alts = [A, B, A']        # A' has content equal to A
    safe = [alts[2], alts[1]]
    [alts.index(x) for x in safe]  ->  [0, 1]   # expected [2, 1]

The user taps the third alternative and receives the first. The bounds check in
the handler passes, so nothing surfaces: a valid-but-wrong exercise is applied
silently. That is why identity is matched here and position never is.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import planning
import user_model
from db import Database
from helpers import utc_now
from noam_coach.services import plan_mutations
from noam_coach.services import workout_slots as slots

_WORKOUT_FACTS = {
    "primary_goal": "strength",
    "training_days_per_week": 3,
    "session_minutes": 45,
    "training_location": "gym",
    "equipment": "full_gym",
    "strength_experience": "intermediate",
    "weekly_availability": "mon,wed,fri",
    "training_limitations": "none",
}


async def _db(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "slots.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (utc_now(),),
    )
    return db


def _bind(monkeypatch: pytest.MonkeyPatch, db: Database) -> None:
    import coach_bot
    from noam_coach.services import core as core_services

    monkeypatch.setattr(coach_bot, "DB", db, raising=False)
    monkeypatch.setattr(core_services, "DB", db, raising=False)


async def _ready_user(db: Database) -> None:
    for key, value in _WORKOUT_FACTS.items():
        await user_model.set_fact(
            db, 1, key, value,
            kind=user_model.KIND_FACT, source=user_model.SOURCE_USER,
        )


# ---------------------------------------------------------------------------
# Identity — the property the whole item rests on
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_slot_identity_survives_real_regeneration(tmp_path, monkeypatch) -> None:
    """Not "a copied id stays equal to itself" — a genuinely fresh build.

    A random uuid carried along during a mutation would pass a substitution
    test and fail here, because regeneration mints new randomness and every
    identity is lost. That is the recorded defect: "No identity survives
    regeneration". The id is derived from `(session_code, template_ordinal)`,
    so a second pipeline run reproduces it by construction with no state
    carried between builds.
    """
    db = await _db(tmp_path)
    _bind(monkeypatch, db)
    await _ready_user(db)

    first = await planning.generate_candidates(db, 1, "workout")
    second = await planning.generate_candidates(db, 1, "workout")

    def _ids(candidates):
        return [
            [e.get("slot_id") for e in session["exercises"]]
            for session in candidates[0].payload["sessions"]
        ]

    assert _ids(first) == _ids(second), (
        "regenerating must reproduce the same slot ids; if this fails the "
        "identity is not derived from the professional need"
    )
    assert all(
        slots.is_valid_slot_id(slot_id)
        for session in _ids(first)
        for slot_id in session
    )


def test_identity_is_not_keyed_on_the_exercise_or_the_position() -> None:
    """Substitution changes the exercise; adaptation changes the position.

    Keying on either would destroy identity in exactly the case the slot model
    exists to survive.
    """
    entries = [
        {"slot_id": "A#0:bench", "slot_key": "bench", "id": "bench"},
        {"slot_id": "A#0:fly", "slot_key": "fly", "id": "fly"},
    ]
    # Substituted: same slot, different exercise.
    entries[0]["id"] = "db_press"
    assert slots.find_by_slot_id(entries, "A#0:bench")[1]["id"] == "db_press"
    # Reordered: same slot, different position.
    entries.reverse()
    found = slots.find_by_slot_id(entries, "A#0:bench")
    assert found is not None and found[0] == 1, "identity must follow the entry"


def test_two_slots_may_share_one_exercise_without_collision() -> None:
    """A legitimate programme repeats an exercise across sessions.

    Keying identity on `exercise_id` would merge them, and a substitution in one
    session would silently change the other.
    """
    entries = [
        {"slot_id": "A#0:press_primary", "id": "bench"},
        {"slot_id": "A#1:press_primary", "id": "bench"},
    ]
    first = slots.find_by_slot_id(entries, "A#0:press_primary")
    second = slots.find_by_slot_id(entries, "A#1:press_primary")

    assert first is not None and second is not None
    assert first[0] != second[0], "two slots sharing an exercise must stay distinct"


def test_an_unmintable_slot_yields_none_rather_than_a_placeholder() -> None:
    """A placeholder id would collide with every other unmintable entry."""
    assert slots.mint_slot_id("", "bench") is None
    assert slots.mint_slot_id("A#0", "") is None
    assert slots.mint_slot_id("A", "bench") is None, "occurrence must carry #N"
    assert slots.mint_slot_id("A#x", "bench") is None, "occurrence must round-trip"
    assert slots.mint_slot_id("A#0", "a:b") is None, "key containing the separator"
    assert slots.mint_session_occurrence("A", -1) is None
    assert slots.mint_session_occurrence("A:B", 0) is None


def test_slot_id_validation_is_a_real_check() -> None:
    """An id that does not round-trip cannot be reconciled against a rebuild."""
    assert slots.is_valid_slot_id("A#0:bench") is True
    for bad in (
        "", "A", "bench", "A:0", "A#0", ":bench", "A#x:bench", "A#-1:bench",
        None, 7, "A#0:a:b",
    ):
        assert slots.is_valid_slot_id(bad) is False, bad


def test_assign_never_overwrites_an_existing_identity() -> None:
    """A repaired or migrated payload keeps the identity it already had."""
    entries = [{"slot_id": "F#0:squat", "id": "bench"}, {"id": "fly"}]
    minted = slots.assign_slot_ids(entries, "A#0")

    assert entries[0]["slot_id"] == "F#0:squat", "an existing valid id is preserved"
    assert entries[1]["slot_id"] == "A#0:fly", "falls back to the seed exercise id"
    assert minted == 1


# ---------------------------------------------------------------------------
# The three absences stay distinguishable (A11a contract, extended)
# ---------------------------------------------------------------------------
def test_absent_state_is_legacy_and_unknown_state_is_malformed() -> None:
    """The A11b correction, and the reason for it.

    An ABSENT `slot_state` is a legacy entry: valid, common, renders normally.
    An unrecognised state that is explicitly PRESENT is a defect — a version
    skew or a typo — and used to silently become `mapped`, turning corrupt data
    into an exercise the user was told to perform.
    """
    assert slots.slot_state_of({"id": "bench"}) == slots.SLOT_STATE_MAPPED
    assert slots.classify_entry({"id": "bench"}) == slots.ENTRY_LEGACY_NO_SLOT

    assert slots.slot_state_of({"id": "bench", "slot_state": "garbage"}) == (
        slots.SLOT_STATE_UNKNOWN
    )
    assert slots.classify_entry({"id": "bench", "slot_state": "garbage"}) == (
        slots.ENTRY_MALFORMED
    ), "an unknown declared state must never render as a normal exercise"


def test_the_unknown_marker_can_never_be_written_into_a_payload() -> None:
    """It is a classifier answer, not a vocabulary member."""
    assert slots.SLOT_STATE_UNKNOWN not in slots.KNOWN_SLOT_STATES
    assert slots.SLOT_STATE_UNKNOWN not in slots.SHIPPED_SLOT_STATES
    assert slots.SLOT_STATE_UNKNOWN not in slots.RESERVED_SLOT_STATES


def test_a_malformed_entry_is_not_performable_and_contributes_no_sets() -> None:
    entry = {"id": "bench", "slot_state": "garbage", "sets": 3}
    assert slots.is_performable(entry) is False
    assert slots.planned_sets_of(entry) == 0


def test_the_four_states_remain_four(caplog) -> None:
    """legacy / unmapped / blocked / malformed must not collapse into one."""
    import logging

    with caplog.at_level(logging.WARNING):
        counts = slots.observe_plan_entries(
            [
                {"id": "bench"},                                  # legacy
                {"slot_id": "A#0:fly"},                           # unmapped
                {"slot_id": "A#0:row", "id": "row", "slot_state": "blocked"},
                {"slot_id": "A#0:x", "id": "x", "slot_state": "??"},  # malformed
            ],
            user_id=1,
            session_id=1,
            context="test",
        )

    assert counts[slots.ENTRY_LEGACY_NO_SLOT] == 1
    assert counts[slots.ENTRY_UNMAPPED] == 1
    assert counts[slots.ENTRY_BLOCKED] == 1
    assert counts[slots.ENTRY_MALFORMED] == 1
    assert any("malformed" in r.message for r in caplog.records), (
        "a malformed entry must stay observable, not merely be counted"
    )


# ---------------------------------------------------------------------------
# Callback tokens
# ---------------------------------------------------------------------------
def test_a_slot_token_cannot_be_read_as_a_version() -> None:
    """`:` is the field separator and `^v\\d{1,9}$` is the version grammar.

    A raw slot id in callback data would add a field and shift every later one;
    a token shaped like a version would be read as one and the router would
    refuse the tap as stale.
    """
    import re

    token = slots.slot_token_of({"slot_id": "A#0:bench"})
    assert token == "A#0-bench"
    assert ":" not in token
    assert re.match(r"^v\d{1,9}$", token) is None
    assert slots.slot_id_from_token(token) == "A#0:bench"


def test_a_token_that_is_not_a_slot_id_is_refused() -> None:
    for bad in ("", "bench", "v123", "A-x", "A#0", None):
        assert slots.slot_id_from_token(bad) is None, bad


# ---------------------------------------------------------------------------
# The A9 boundary
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_saved_substitution_creates_a_new_version(tmp_path, monkeypatch) -> None:
    """Copy-on-write: the active payload is never edited in place."""
    db = await _db(tmp_path)
    _bind(monkeypatch, db)
    await _ready_user(db)

    candidates = await planning.generate_candidates(db, 1, "workout")
    plan_id = candidates[0].id
    await planning.activate_plan(db, 1, plan_id)

    before = await db.fetch_one(
        "SELECT payload FROM plan_versions WHERE id=?", (plan_id,)
    )
    session = json.loads(before["payload"])["sessions"][0]
    slot_id = session["exercises"][0]["slot_id"]

    outcome = await plan_mutations.substitute_slot_in_saved_plan(
        db, 1, slot_id, "replacement_exercise", reason="pain"
    )

    assert outcome.outcome == plan_mutations.OUTCOME_REALIGNED
    assert outcome.plan_id != plan_id, "a new version, not an edit"

    unchanged = await db.fetch_one(
        "SELECT payload FROM plan_versions WHERE id=?", (plan_id,)
    )
    assert unchanged["payload"] == before["payload"], (
        "the superseded version must survive byte-identical, or a completed "
        "session can no longer be explained against the plan that was live"
    )


@pytest.mark.asyncio
async def test_an_unknown_slot_is_refused_not_applied_by_position(
    tmp_path, monkeypatch
) -> None:
    """A stale offer must find nothing rather than hit whatever moved there."""
    db = await _db(tmp_path)
    _bind(monkeypatch, db)
    await _ready_user(db)

    candidates = await planning.generate_candidates(db, 1, "workout")
    await planning.activate_plan(db, 1, candidates[0].id)

    outcome = await plan_mutations.substitute_slot_in_saved_plan(
        db, 1, "Z:42", "anything", reason="pain"
    )

    assert outcome.outcome == plan_mutations.OUTCOME_BLOCKED
    assert outcome.reason == plan_mutations.REASON_UNKNOWN_SLOT


@pytest.mark.asyncio
async def test_a_malformed_slot_id_never_reaches_the_plan(tmp_path, monkeypatch) -> None:
    db = await _db(tmp_path)
    _bind(monkeypatch, db)

    outcome = await plan_mutations.substitute_slot_in_saved_plan(
        db, 1, "not-a-slot", "anything", reason="pain"
    )
    assert outcome.outcome == plan_mutations.OUTCOME_BLOCKED
    assert outcome.reason == plan_mutations.REASON_UNKNOWN_SLOT


@pytest.mark.asyncio
async def test_substituting_the_same_exercise_reports_no_change(
    tmp_path, monkeypatch
) -> None:
    """Idempotent: a repeated tap is a success, not a new version each time."""
    db = await _db(tmp_path)
    _bind(monkeypatch, db)
    await _ready_user(db)

    candidates = await planning.generate_candidates(db, 1, "workout")
    await planning.activate_plan(db, 1, candidates[0].id)
    active = await planning.get_active_plan(db, 1, "workout")
    entry = active["payload"]["sessions"][0]["exercises"][0]

    outcome = await plan_mutations.substitute_slot_in_saved_plan(
        db, 1, entry["slot_id"], entry["id"], reason="pain"
    )

    assert outcome.outcome == plan_mutations.OUTCOME_NO_CHANGE
    assert outcome.is_failure is False


@pytest.mark.asyncio
async def test_the_slot_survives_the_substitution(tmp_path, monkeypatch) -> None:
    """The need persists; only the implementation changes."""
    db = await _db(tmp_path)
    _bind(monkeypatch, db)
    await _ready_user(db)

    candidates = await planning.generate_candidates(db, 1, "workout")
    await planning.activate_plan(db, 1, candidates[0].id)
    active = await planning.get_active_plan(db, 1, "workout")
    slot_id = active["payload"]["sessions"][0]["exercises"][0]["slot_id"]
    original_id = active["payload"]["sessions"][0]["exercises"][0]["id"]

    await plan_mutations.substitute_slot_in_saved_plan(
        db, 1, slot_id, "swapped_in", reason="pain"
    )

    updated = await planning.get_active_plan(db, 1, "workout")
    found = slots.find_by_slot_id(
        updated["payload"]["sessions"][0]["exercises"], slot_id
    )
    assert found is not None, "the slot must still exist after a substitution"
    assert found[1]["id"] == "swapped_in"
    assert found[1]["original_id"] == original_id, (
        "the slot remembers what it originally implemented"
    )


@pytest.mark.asyncio
async def test_no_slot_is_deleted_by_a_substitution(tmp_path, monkeypatch) -> None:
    db = await _db(tmp_path)
    _bind(monkeypatch, db)
    await _ready_user(db)

    candidates = await planning.generate_candidates(db, 1, "workout")
    await planning.activate_plan(db, 1, candidates[0].id)
    active = await planning.get_active_plan(db, 1, "workout")

    def _all_slots(payload):
        return sorted(
            e.get("slot_id")
            for s in payload["sessions"]
            for e in s["exercises"]
        )

    before = _all_slots(active["payload"])
    await plan_mutations.substitute_slot_in_saved_plan(
        db, 1, before[0], "swapped_in", reason="pain"
    )
    after = _all_slots((await planning.get_active_plan(db, 1, "workout"))["payload"])

    assert before == after, "a substitution must never add or remove a slot"


def test_repair_keeps_two_slots_that_share_an_exercise_across_sessions() -> None:
    """A slot is a need; two needs may be met by the same movement.

    Keying repair's dedupe on `exercise_id` alone deleted the second, which is
    a silent slot deletion. Across sessions -- a programme that presses on
    Monday and again on Thursday -- both must survive.
    """
    payload = {
        "sessions": [
            {
                "code": "A", "weekday": 0, "time": "18:00", "minutes": 45,
                "session_occurrence": "A#0",
                "exercises": [
                    {"slot_id": "A#0:press", "id": "bench",
                     "sets": 3, "rmin": 8, "rmax": 12},
                ],
            },
            {
                "code": "A", "weekday": 3, "time": "18:00", "minutes": 45,
                "session_occurrence": "A#1",
                "exercises": [
                    {"slot_id": "A#1:press", "id": "bench",
                     "sets": 3, "rmin": 8, "rmax": 12},
                ],
            },
        ]
    }
    repaired = planning.repair_workout_payload(payload)
    surviving = [
        e.get("slot_id")
        for s in repaired["sessions"]
        for e in s["exercises"]
    ]

    assert surviving == ["A#0:press", "A#1:press"], (
        "repair deleted a slot that shares an exercise with another session; "
        "two needs may be met by the same movement"
    )


def test_repair_drops_a_repeated_slot_id_within_one_session() -> None:
    """One professional need cannot appear twice in the same session.

    That is a corrupt payload, not a legitimate repeat, so the duplicate is
    removed rather than rendered twice.
    """
    payload = {
        "sessions": [
            {
                "code": "A", "weekday": 0, "time": "18:00", "minutes": 45,
                "exercises": [
                    {"slot_id": "A#0:press", "id": "bench",
                     "sets": 3, "rmin": 8, "rmax": 12},
                    {"slot_id": "A#0:press", "id": "db_press",
                     "sets": 3, "rmin": 8, "rmax": 12},
                ],
            }
        ]
    }
    repaired = planning.repair_workout_payload(payload)
    surviving = [e["slot_id"] for e in repaired["sessions"][0]["exercises"]]

    assert surviving == ["A#0:press"]


def test_repair_still_collapses_a_repeated_exercise_within_one_session() -> None:
    """The pre-A11b rule, KEPT deliberately rather than relaxed.

    `workout_quality_issues` flags a duplicated exercise and
    `_repair_workout_candidates` discards an unrepairable candidate whole.
    Relaxing this rule silently reduced three offered strategies to one --
    measured, not predicted -- because two candidates became unrepairable.

    So within a single session the exercise-id rule stands, and the slot model
    is expressed across sessions instead (see the test above).
    """
    payload = {
        "sessions": [
            {
                "code": "A", "weekday": 0, "time": "18:00", "minutes": 45,
                "exercises": [
                    {"slot_id": "A#0:press_primary", "id": "bench",
                     "sets": 3, "rmin": 8, "rmax": 12},
                    {"slot_id": "A#0:press_accessory", "id": "bench",
                     "sets": 3, "rmin": 8, "rmax": 12},
                ],
            }
        ]
    }
    repaired = planning.repair_workout_payload(payload)
    surviving = [e["slot_id"] for e in repaired["sessions"][0]["exercises"]]

    assert surviving == ["A#0:press_primary"], (
        "a session listing the same exercise twice is a quality defect the "
        "activation gate rejects; repair must still collapse it"
    )


# ---------------------------------------------------------------------------
# The active-session snapshot (owner decision: do NOT block; prove continuity)
#
# `REASON_ACTIVE_SESSION` was declared and never returned. It is removed rather
# than implemented, because the protection it named already exists in a better
# form: `sessions.plan` snapshots the payload at session start, so a live
# workout is decoupled from the saved plan by construction. Blocking would have
# refused a legitimate correction to defend against a corruption that cannot
# occur. These tests are the evidence for that decision.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_live_session_continues_on_its_own_snapshot(
    tmp_path, monkeypatch
) -> None:
    """The current workout is unaffected by a saved-plan substitution."""
    db = await _db(tmp_path)
    _bind(monkeypatch, db)
    await _ready_user(db)

    candidates = await planning.generate_candidates(db, 1, "workout")
    await planning.activate_plan(db, 1, candidates[0].id)
    active = await planning.get_active_plan(db, 1, "workout")
    session_payload = active["payload"]["sessions"][0]
    slot_id = session_payload["exercises"][0]["slot_id"]
    original_id = session_payload["exercises"][0]["id"]

    # A workout starts: the plan is snapshotted onto the session row.
    session_id = await db.execute(
        "INSERT INTO sessions(user_id, code, name, plan, status, exercise_index, "
        "set_number, started_at) VALUES(1, ?, ?, ?, 'active', 0, 1, ?)",
        (
            session_payload.get("code") or "A",
            session_payload.get("name") or "A",
            json.dumps(session_payload, ensure_ascii=False),
            utc_now(),
        ),
    )

    outcome = await plan_mutations.substitute_slot_in_saved_plan(
        db, 1, slot_id, "swapped_in", reason="pain"
    )
    assert outcome.outcome == plan_mutations.OUTCOME_REALIGNED

    live = await db.fetch_one("SELECT plan FROM sessions WHERE id=?", (session_id,))
    live_first = json.loads(live["plan"])["exercises"][0]

    assert live_first["id"] == original_id, (
        "the in-progress workout must continue on the exercise it started with; "
        "changing it mid-session would move the ground under the user"
    )


@pytest.mark.asyncio
async def test_the_next_session_receives_the_new_mapping(tmp_path, monkeypatch) -> None:
    """The change is not lost — it applies from the next session onward."""
    db = await _db(tmp_path)
    _bind(monkeypatch, db)
    await _ready_user(db)

    candidates = await planning.generate_candidates(db, 1, "workout")
    await planning.activate_plan(db, 1, candidates[0].id)
    active = await planning.get_active_plan(db, 1, "workout")
    slot_id = active["payload"]["sessions"][0]["exercises"][0]["slot_id"]

    await plan_mutations.substitute_slot_in_saved_plan(
        db, 1, slot_id, "swapped_in", reason="pain"
    )

    # A session started AFTER the change snapshots the corrected plan.
    updated = await planning.get_active_plan(db, 1, "workout")
    next_session = updated["payload"]["sessions"][0]
    found = slots.find_by_slot_id(next_session["exercises"], slot_id)

    assert found is not None and found[1]["id"] == "swapped_in", (
        "the next session must train the substituted exercise, or the change "
        "silently did nothing"
    )


@pytest.mark.asyncio
async def test_the_governed_fact_mirrors_the_new_version(tmp_path, monkeypatch) -> None:
    """Activation goes through A9 -> activate_plan, the sole A4-authorized writer."""
    db = await _db(tmp_path)
    _bind(monkeypatch, db)
    await _ready_user(db)

    candidates = await planning.generate_candidates(db, 1, "workout")
    await planning.activate_plan(db, 1, candidates[0].id)
    active = await planning.get_active_plan(db, 1, "workout")
    slot_id = active["payload"]["sessions"][0]["exercises"][0]["slot_id"]

    outcome = await plan_mutations.substitute_slot_in_saved_plan(
        db, 1, slot_id, "swapped_in", reason="pain"
    )

    fact = await user_model.get_value(db, 1, "active_workout_plan")
    assert fact and fact.get("plan_id") == outcome.plan_id, (
        "the governed fact must mirror the NEW version, not the superseded one"
    )


def test_a11b_adds_no_writer_of_its_own() -> None:
    """Source scan: the A11b operation must route through A9, not around it.

    Parsed with `ast` and stripped of docstrings, so this file's own prose
    about `INSERT INTO plan_versions` cannot trip or satisfy the check.
    """
    import ast

    root = Path(__file__).resolve().parents[1]
    source = (root / "noam_coach" / "services" / "plan_mutations.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            if (
                node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)
            ):
                node.body = node.body[1:]
    code = ast.unparse(tree)

    assert "authorize_governed_fact_write" not in code, (
        "A11b must not wrap A4's authorization around itself; activation goes "
        "through planning.activate_plan, the sole authorized owner"
    )
    assert code.count("INSERT INTO plan_versions") <= 1, (
        "copy-on-write has exactly one insert site (_insert_corrected_version)"
    )


# ---------------------------------------------------------------------------
# Rendering — the surface where a substitution was previously invisible
# ---------------------------------------------------------------------------
def _render(plan: dict) -> str:
    from noam_coach.bot.onboarding import format_weekly_plan

    return format_weekly_plan(plan)


def test_the_renderer_shows_the_stored_exercise_not_the_template() -> None:
    """The defect this rewrite exists to close.

    `format_weekly_plan` read `PLANS.get(code)["exercises"]` and ignored the
    stored payload entirely, so a substitution made for pain simply did not
    appear on the screen where the user decides what to train. The plan said
    one thing and the surface showed another.
    """
    plan = {
        "frequency": 1,
        "structure": "A/B/C",
        "sessions": [
            {
                "weekday": 0,
                "time": "18:00",
                "name": "אימון A",
                "code": "A",
                "exercises": [
                    {
                        "slot_id": "A#0:bench",
                        "id": "swapped_in",
                        "name": "SUBSTITUTED EXERCISE",
                        "sets": 3, "rmin": 8, "rmax": 12, "rest": 90,
                    }
                ],
            }
        ],
    }

    rendered = _render(plan)

    assert "SUBSTITUTED EXERCISE" in rendered, (
        "the stored exercise must be rendered; reading the global template "
        "here is what made every substitution invisible"
    )


def test_a_canonical_plan_never_falls_back_to_the_template() -> None:
    """The fallback is for legacy fact-only payloads only.

    A canonical plan carries its exercises, so it must not reach the template
    branch. Proven by rendering a canonical-shaped session whose stored
    exercises are deliberately unlike anything in `PLANS`.
    """
    from exercise_plans import PLANS

    template_names = {
        str(ex.get("name") or "")
        for entry in PLANS.values()
        for ex in entry.get("exercises", [])
    }

    plan = {
        "frequency": 1,
        "structure": "A/B/C",
        "sessions": [
            {
                "weekday": 0, "time": "18:00", "name": "אימון A", "code": "A",
                "exercises": [
                    {
                        "slot_id": "A#0:bench", "id": "only_this",
                        "name": "ONLY THIS ONE", "sets": 3,
                        "rmin": 8, "rmax": 12, "rest": 90,
                    }
                ],
            }
        ],
    }

    rendered = _render(plan)
    leaked = [name for name in template_names if name and name in rendered]

    assert "ONLY THIS ONE" in rendered
    assert not leaked, (
        f"template exercises leaked into a canonical render: {leaked[:3]} -- "
        "the stored payload must be the only source"
    )


def test_a_legacy_fact_only_session_still_renders() -> None:
    """The narrowly justified fallback.

    A10 retired the writer that produced these, so no NEW plan takes this
    branch -- but payloads already in the database carry `code`/`name` and no
    exercises, and must not render as an empty day.
    """
    plan = {
        "frequency": 1,
        "structure": "A/B/C",
        "sessions": [
            {"weekday": 0, "time": "18:00", "name": "אימון A", "code": "A"}
        ],
    }

    rendered = _render(plan)

    assert "אימון A" in rendered
    assert rendered.count("1.") >= 1, "a legacy session must still list exercises"


def test_unmapped_blocked_and_malformed_each_render_distinctly() -> None:
    """D-2: a slot without an implementation is a state every surface renders.

    Skipping them would renumber the list and hide that a professional need
    exists but is unfilled; rendering all three the same way would collapse
    "not chosen yet", "withheld for your safety" and "this data is broken" into
    one indistinguishable blank.
    """
    plan = {
        "frequency": 1,
        "structure": "A/B/C",
        "sessions": [
            {
                "weekday": 0, "time": "18:00", "name": "אימון A", "code": "A",
                "exercises": [
                    {"slot_id": "A#0:bench", "slot_state": "unmapped"},
                    {"slot_id": "A#0:fly", "id": "fly", "slot_state": "blocked"},
                    {"slot_id": "A#0:x", "id": "x", "slot_state": "garbage"},
                ],
            }
        ],
    }

    rendered = _render(plan)

    assert "⬜" in rendered, "an unmapped slot must be shown, not skipped"
    assert "🛡️" in rendered, "a blocked slot must say it is protective"
    assert "⚠️" in rendered, "a malformed entry must stay visible"
    # And they must not be the same message.
    assert rendered.count("⬜") == 1
    assert rendered.count("🛡️") == 1


def test_a_malformed_entry_does_not_crash_the_renderer() -> None:
    """A non-dict entry used to reach `ex.get(...)` and raise."""
    plan = {
        "frequency": 1,
        "structure": "A/B/C",
        "sessions": [
            {
                "weekday": 0, "time": "18:00", "name": "אימון A", "code": "A",
                "exercises": ["not a dict", None, 42],
            }
        ],
    }

    rendered = _render(plan)
    assert "אימון A" in rendered
    assert rendered.count("⚠️") == 3


def test_the_in_session_swap_says_the_saved_plan_is_unchanged() -> None:
    """The UX half of the snapshot decision.

    An in-workout substitution writes the SESSION snapshot, not the saved plan
    — that is what keeps a live workout immune to plan edits. But a user who
    swaps an exercise reasonably assumes next week is fixed too, and nothing
    told them otherwise. Asserted at the source, because the alternative is a
    full callback round-trip that would test the fake query more than the copy.
    """
    import ast

    root = Path(__file__).resolve().parents[1]
    source = (root / "noam_coach" / "bot" / "callback_session.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)

    messages: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if called != "safe_answer_callback":
            continue
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                messages.append(arg.value)

    assert any("התוכנית הקבועה לא השתנתה" in m for m in messages), (
        "after an in-session swap the user must be told the saved plan is "
        "unchanged, or they will assume the change carries to next week"
    )


# ---------------------------------------------------------------------------
# Discriminating tests
#
# Added after deliberate breakage found four protections that no test could
# distinguish from their broken form. Each of these fails when the guarded
# behaviour is reverted, which the previous tests did not -- they used fixtures
# where identity and position happened to agree, so "resolve by position"
# returned the right answer for the wrong reason.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_re_ranked_alternative_list_resolves_by_identity_not_position(
    tmp_path, monkeypatch
) -> None:
    """The `.index(alt)` defect, in the one shape that exposes it.

    The alternative the user taps is deliberately NOT first, so resolving by
    position returns a different -- valid, therefore silent -- exercise. A test
    whose target sits at position 0 cannot tell the two implementations apart.
    """
    import coach_bot
    from noam_coach.bot import callback_session as cs_bot
    from noam_coach.bot import ui as ui_bot

    db = await _db(tmp_path)
    _bind(monkeypatch, db)
    monkeypatch.setattr(cs_bot, "DB", db, raising=False)
    monkeypatch.setattr(ui_bot, "DB", db, raising=False)

    plan = {
        "name": "A",
        "exercises": [
            {
                "id": "leg_press", "name": "Leg press", "sets": 3,
                "rmin": 8, "rmax": 12, "inc": 5, "weight": 100,
                "muscle": "legs", "cues": [],
                "alts": [
                    {"id": "first_alt", "name": "First", "weight": 40},
                    {"id": "second_alt", "name": "Second", "weight": 50},
                    {"id": "third_alt", "name": "Third", "weight": 60},
                ],
            }
        ],
    }
    session_id = await db.execute(
        "INSERT INTO sessions(user_id, code, name, plan, status, exercise_index, "
        "set_number, started_at) VALUES(1,'A','A',?,'active',0,1,?)",
        (json.dumps(plan, ensure_ascii=False), utc_now()),
    )
    session = dict(await db.fetch_one("SELECT * FROM sessions WHERE id=?", (session_id,)))

    class _Q:
        async def edit_message_text(self, *a, **k) -> None: ...
        async def answer(self, *a, **k) -> None: ...

    # Tap the THIRD alternative. Position-based resolution would apply the first.
    await cs_bot.handle_session_action_callback(
        _Q(), context=None, user_id=1,
        data=coach_bot.session_action_data("sub", session, "third_alt", "equipment"),
    )

    refreshed = await db.fetch_one("SELECT plan FROM sessions WHERE id=?", (session_id,))
    applied = json.loads(refreshed["plan"])["exercises"][0]["id"]

    assert applied == "third_alt", (
        f"tapped the third alternative and got {applied!r} -- resolution is "
        "still positional, so a re-ranked list applies the wrong exercise"
    )


@pytest.mark.asyncio
async def test_an_unknown_alternative_id_is_refused(tmp_path, monkeypatch) -> None:
    """A stale keyboard names an alternative the list no longer offers."""
    import coach_bot
    from noam_coach.bot import callback_session as cs_bot
    from noam_coach.bot import ui as ui_bot

    db = await _db(tmp_path)
    _bind(monkeypatch, db)
    monkeypatch.setattr(cs_bot, "DB", db, raising=False)
    monkeypatch.setattr(ui_bot, "DB", db, raising=False)

    plan = {
        "name": "A",
        "exercises": [
            {
                "id": "leg_press", "name": "Leg press", "sets": 3, "rmin": 8,
                "rmax": 12, "inc": 5, "weight": 100, "muscle": "legs",
                "cues": [], "alts": [{"id": "only_alt", "name": "Only", "weight": 40}],
            }
        ],
    }
    session_id = await db.execute(
        "INSERT INTO sessions(user_id, code, name, plan, status, exercise_index, "
        "set_number, started_at) VALUES(1,'A','A',?,'active',0,1,?)",
        (json.dumps(plan, ensure_ascii=False), utc_now()),
    )
    session = dict(await db.fetch_one("SELECT * FROM sessions WHERE id=?", (session_id,)))

    class _Q:
        async def edit_message_text(self, *a, **k) -> None: ...
        async def answer(self, *a, **k) -> None: ...

    await cs_bot.handle_session_action_callback(
        _Q(), context=None, user_id=1,
        data=coach_bot.session_action_data("sub", session, "vanished_alt", "pain"),
    )

    refreshed = await db.fetch_one("SELECT plan FROM sessions WHERE id=?", (session_id,))
    assert json.loads(refreshed["plan"])["exercises"][0]["id"] == "leg_press", (
        "a stale alternative must be refused, never resolved to whatever is "
        "at that position now"
    )


@pytest.mark.asyncio
async def test_the_superseded_version_is_not_mutated_in_place(
    tmp_path, monkeypatch
) -> None:
    """Copy-on-write, asserted where a shallow reference would show.

    The earlier test compared the old row to a snapshot taken from the same
    object, so an in-place edit compared equal to itself. This reads the row
    back from the database and asserts the OLD version still names the OLD
    exercise.
    """
    db = await _db(tmp_path)
    _bind(monkeypatch, db)
    await _ready_user(db)

    candidates = await planning.generate_candidates(db, 1, "workout")
    original_plan_id = candidates[0].id
    await planning.activate_plan(db, 1, original_plan_id)

    active = await planning.get_active_plan(db, 1, "workout")
    entry = active["payload"]["sessions"][0]["exercises"][0]
    slot_id, original_exercise = entry["slot_id"], entry["id"]

    await plan_mutations.substitute_slot_in_saved_plan(
        db, 1, slot_id, "swapped_in", reason="pain"
    )

    old_row = await db.fetch_one(
        "SELECT payload, status FROM plan_versions WHERE id=?", (original_plan_id,)
    )
    old_entry = slots.find_by_slot_id(
        json.loads(old_row["payload"])["sessions"][0]["exercises"], slot_id
    )

    assert old_entry is not None
    assert old_entry[1]["id"] == original_exercise, (
        "the superseded version was edited in place; a completed session can "
        "no longer be explained against the plan that was live when it ran"
    )
    assert old_row["status"] != "active"


def test_the_two_reason_paths_mint_different_callbacks() -> None:
    """Pain and equipment must stay distinguishable at the button.

    Before A11b both paths minted byte-identical callbacks, so the reason was
    destroyed before any handler ran and A12 could not tell a safety-driven
    change from a convenience one. Asserted on the minted strings, because a
    test of the audit alone passes when both sides write the same constant.
    """
    from noam_coach.bot.callback_session import (
        SUB_REASON_EQUIPMENT,
        SUB_REASON_PAIN,
        _substitution_callback,
    )

    session = {"id": 5, "exercise_index": 0, "set_number": 1}
    current = {"id": "leg_press"}
    alt = {"id": "hack_squat"}

    pain = _substitution_callback(session, current, alt, SUB_REASON_PAIN)
    equipment = _substitution_callback(session, current, alt, SUB_REASON_EQUIPMENT)

    assert pain != equipment, (
        "the two reason paths mint identical callbacks -- the distinction is "
        "destroyed at the button, exactly as it was before A11b"
    )
    assert pain.endswith(":pain")
    assert equipment.endswith(":equipment")


def test_an_unrecognised_reason_is_coerced_not_stored() -> None:
    from noam_coach.bot.callback_session import (
        SUB_REASON_UNKNOWN,
        _substitution_callback,
    )

    minted = _substitution_callback(
        {"id": 5, "exercise_index": 0, "set_number": 1},
        {"id": "a"}, {"id": "b"}, "free text that is not a code",
    )
    assert minted.endswith(":" + SUB_REASON_UNKNOWN), (
        "free text must never reach callback data"
    )


@pytest.mark.asyncio
async def test_the_audit_allowlist_registration_actually_constrains(
    tmp_path, monkeypatch
) -> None:
    """Registration must CONSTRAIN, not merely describe.

    Every field A11b audits is a bounded scalar, so the unregistered
    `_scalar_only` fallback preserves them anyway -- which is why deregistering
    the pair broke no test. What registration buys is the opposite guarantee: a
    field nobody vetted is dropped. A limitation string is exactly the kind of
    scalar the fallback would wave through.
    """
    from noam_coach.services import core as core_services

    db = await _db(tmp_path)
    _bind(monkeypatch, db)

    await core_services.write_audit(
        1, "approve_substitution", "exercise", 99,
        source="leg_press", target="hack_squat", reason="pain",
        slot_id="A#0:leg_press",
        limitation_detail="\u05db\u05d0\u05d1 \u05d1\u05d1\u05e8\u05da",
    )

    row = await db.fetch_one(
        "SELECT details FROM audit WHERE user_id=1 AND action='approve_substitution'"
    )
    details = json.loads(row["details"])

    assert details["reason"] == "pain", "vetted fields must survive"
    assert details["slot_id"] == "A#0:leg_press"
    assert "limitation_detail" not in details, (
        "an unlisted field must be dropped BY RULE, not stored because it "
        "happened to be a scalar"
    )
    assert "\u05d1\u05e8\u05da" not in json.dumps(details, ensure_ascii=False)


# ---------------------------------------------------------------------------
# The six identity cases
#
# The first scheme was `<session_code>:<template_ordinal>`. Two identical
# `generate_candidates` runs agreed, which looked like proof and was not: it
# tested DETERMINISM while missing UNIQUENESS entirely. Measured on a 6-day
# plan, whose split is `['A','B','C','A','B','C']`, **10 of 20 slot ids were
# duplicates** -- every slot in the second half collided with its counterpart
# in the first.
#
# The rule is now `<session_occurrence>:<slot_key>`:
#
#   * `session_occurrence` is the code's Nth appearance in the split (`A#0`,
#     `A#1`) -- not the weekday, which the user changes freely and which A9
#     realignment moves, and not the array index of the session.
#   * `slot_key` is DECLARED on the template by `exercise_plans.exercise(...)`,
#     defaulting to the seed exercise id -- not the current `id`, which
#     substitution changes, and not a position, which reordering renames.
#
# Each test below fails under the old scheme.
# ---------------------------------------------------------------------------
async def _plan_for(tmp_path, monkeypatch, *, frequency: int, days: str, name: str):
    """A generated plan for a given split, on its own database."""
    db = Database(str(tmp_path / f"{name}.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (utc_now(),),
    )
    _bind(monkeypatch, db)
    facts = dict(
        _WORKOUT_FACTS, training_days_per_week=frequency, weekly_availability=days
    )
    for key, value in facts.items():
        await user_model.set_fact(
            db, 1, key, value,
            kind=user_model.KIND_FACT, source=user_model.SOURCE_USER,
        )
    return db, await planning.generate_candidates(db, 1, "workout")


def _every_slot_id(payload) -> list[str]:
    return [e.get("slot_id") for s in payload["sessions"] for e in s["exercises"]]


@pytest.mark.asyncio
async def test_case_1_repeated_session_codes_produce_no_duplicate_slot_id(
    tmp_path, monkeypatch
) -> None:
    """A 6-day split trains `['A','B','C','A','B','C']`.

    Under the ordinal scheme this produced 10 duplicate ids out of 20, so the
    two appearances of session A were one indistinguishable slot set. A
    substitution in week-half one would have resolved into week-half two.
    """
    _, candidates = await _plan_for(
        tmp_path, monkeypatch, frequency=6,
        days="sun,mon,tue,wed,thu,fri", name="six",
    )

    for candidate in candidates:
        codes = [s["code"] for s in candidate.payload["sessions"]]
        ids = _every_slot_id(candidate.payload)
        assert len(codes) != len(set(codes)), (
            "this fixture must actually repeat a session code, or it proves "
            "nothing about the collision"
        )
        assert len(ids) == len(set(ids)), (
            f"duplicate slot ids in a {codes} plan: "
            f"{len(ids) - len(set(ids))} of {len(ids)}"
        )


@pytest.mark.asyncio
async def test_case_6_every_slot_id_is_unique_across_the_whole_payload(
    tmp_path, monkeypatch
) -> None:
    """Uniqueness is a property of the PLAN, not of one session."""
    for frequency, days in ((2, "mon,thu"), (3, "mon,wed,fri"),
                            (5, "sun,mon,tue,wed,thu")):
        _, candidates = await _plan_for(
            tmp_path, monkeypatch, frequency=frequency,
            days=days, name=f"f{frequency}",
        )
        for candidate in candidates:
            ids = _every_slot_id(candidate.payload)
            assert ids, "a plan must carry slot ids"
            assert all(slots.is_valid_slot_id(i) for i in ids)
            assert len(ids) == len(set(ids)), (
                f"frequency {frequency}: {len(ids) - len(set(ids))} duplicates"
            )


@pytest.mark.asyncio
async def test_case_2_reordering_sessions_preserves_slot_identity(
    tmp_path, monkeypatch
) -> None:
    """Identity travels with the session, not with its position in the list."""
    _, candidates = await _plan_for(
        tmp_path, monkeypatch, frequency=6,
        days="sun,mon,tue,wed,thu,fri", name="reorder",
    )
    payload = candidates[0].payload

    def _by_occurrence(p):
        return {
            s["session_occurrence"]: sorted(e["slot_id"] for e in s["exercises"])
            for s in p["sessions"]
        }

    before = _by_occurrence(payload)
    payload["sessions"].reverse()
    assert _by_occurrence(payload) == before, (
        "reordering sessions renamed slots; identity is still positional"
    )


def test_case_3_reordering_template_exercises_preserves_slot_identity() -> None:
    """The sharpest case, and the one an ordinal key cannot survive.

    The same professional needs, listed in a different order, must keep their
    identities. Under `<code>:<ordinal>` every slot after the moved one is
    renamed -- silently reassigning an identity to a DIFFERENT need, which is
    worse than losing it.
    """
    template = [
        {"slot_key": "horizontal_press", "id": "bench"},
        {"slot_key": "incline_press", "id": "incline_db"},
        {"slot_key": "chest_isolation", "id": "fly"},
    ]

    forward = [dict(e) for e in template]
    slots.assign_slot_ids(forward, "A#0")

    reordered = [dict(e) for e in reversed(template)]
    slots.assign_slot_ids(reordered, "A#0")

    assert {e["slot_key"]: e["slot_id"] for e in forward} == {
        e["slot_key"]: e["slot_id"] for e in reordered
    }, "reordering the template reassigned identities to different needs"


def test_case_4_two_needs_sharing_one_exercise_stay_distinct() -> None:
    """A programme may press twice; that is two needs, not one duplicated."""
    entries = [
        {"slot_key": "press_primary", "id": "bench"},
        {"slot_key": "press_accessory", "id": "bench"},
    ]
    slots.assign_slot_ids(entries, "A#0")

    assert entries[0]["slot_id"] != entries[1]["slot_id"]
    assert slots.find_by_slot_id(entries, entries[0]["slot_id"])[0] == 0
    assert slots.find_by_slot_id(entries, entries[1]["slot_id"])[0] == 1


@pytest.mark.asyncio
async def test_case_5_identity_matches_across_independent_databases(
    tmp_path, monkeypatch
) -> None:
    """No state is carried between builds -- not even a database.

    Two runs against the SAME database could agree by reading something the
    first run left behind. Separate databases remove that possibility, so
    agreement can only come from the rule being derived.
    """
    _, first = await _plan_for(
        tmp_path, monkeypatch, frequency=6,
        days="sun,mon,tue,wed,thu,fri", name="db_one",
    )
    _, second = await _plan_for(
        tmp_path, monkeypatch, frequency=6,
        days="sun,mon,tue,wed,thu,fri", name="db_two",
    )

    assert _every_slot_id(first[0].payload) == _every_slot_id(second[0].payload), (
        "identity differs between independent builds; it is not derived from "
        "the professional need"
    )


@pytest.mark.asyncio
async def test_identity_does_not_depend_on_the_weekday(tmp_path, monkeypatch) -> None:
    """A9 realignment moves sessions between days without changing what they
    train, so weekday must not participate in identity."""
    _, candidates = await _plan_for(
        tmp_path, monkeypatch, frequency=3, days="mon,wed,fri", name="weekday",
    )
    payload = candidates[0].payload

    before = sorted(_every_slot_id(payload))
    for session in payload["sessions"]:
        session["weekday"] = (session["weekday"] + 3) % 7

    assert sorted(_every_slot_id(payload)) == before


def test_the_declared_slot_key_is_unique_within_every_template_session() -> None:
    """The premise the whole scheme rests on.

    If a template session ever declared the same `slot_key` twice, two needs
    would share one identity and a substitution would hit both. Pinned here so
    a future template edit fails loudly rather than colliding silently.
    """
    from exercise_plans import PLANS

    for code, entry in PLANS.items():
        keys = [e.get("slot_key") for e in entry.get("exercises", [])]
        assert all(keys), f"{code}: every template slot must declare a slot_key"
        assert len(keys) == len(set(keys)), (
            f"{code}: duplicate slot_key {keys} -- two needs would share one id"
        )


@pytest.mark.asyncio
async def test_weekday_does_not_participate_in_minting(tmp_path, monkeypatch) -> None:
    """Weekday-independence asserted at the MINTING site.

    Mutating weekdays on an already-built payload proves nothing: the ids were
    minted before the mutation, so they cannot change. The property that
    matters is that the minting input never contains a weekday, which is what
    makes rescheduling and A9 realignment safe.

    Asserted two ways: the occurrence recorded on each session must count
    appearances of its code (`A#0`, `A#1`), never encode a day; and a plan whose
    sessions fall on high weekdays must still mint `#0`-based occurrences.
    """
    _, candidates = await _plan_for(
        tmp_path, monkeypatch, frequency=6,
        days="sun,mon,tue,wed,thu,fri", name="mint_wk",
    )
    payload = candidates[0].payload

    seen: dict[str, int] = {}
    for session in payload["sessions"]:
        code = session["code"]
        expected = slots.mint_session_occurrence(code, seen.get(code, 0))
        seen[code] = seen.get(code, 0) + 1
        assert session["session_occurrence"] == expected, (
            f"session on weekday {session['weekday']} carries "
            f"{session['session_occurrence']!r}, expected {expected!r} -- the "
            "occurrence is not counting code appearances"
        )

    # No occurrence may encode a weekday that is not also a valid appearance
    # count. With 6 sessions over codes A/B/C every occurrence is #0 or #1,
    # while the weekdays run 0..5 -- so a weekday-keyed scheme cannot produce
    # this set.
    occurrences = {s["session_occurrence"].split("#")[1] for s in payload["sessions"]}
    weekdays = {str(s["weekday"]) for s in payload["sessions"]}
    assert occurrences == {"0", "1"}, occurrences
    assert occurrences != weekdays, (
        "occurrences match the weekdays exactly; identity may be weekday-keyed"
    )


@pytest.mark.asyncio
async def test_a9_realignment_preserves_slot_identity(tmp_path, monkeypatch) -> None:
    """The same property, through the real mutation that moves sessions.

    A9's `realign_saved_plan_to_weekdays` exists to move a plan onto different
    days. It must not silently reassign every slot on the way.
    """
    db, candidates = await _plan_for(
        tmp_path, monkeypatch, frequency=3, days="mon,wed,fri", name="realign",
    )
    await planning.activate_plan(db, 1, candidates[0].id)
    before = sorted(
        _every_slot_id((await planning.get_active_plan(db, 1, "workout"))["payload"])
    )

    outcome = await plan_mutations.realign_saved_plan_to_weekdays(
        db, 1, [1, 3, 5], reason="test_realign"
    )
    assert outcome.outcome in (
        plan_mutations.OUTCOME_REALIGNED,
        plan_mutations.OUTCOME_NO_CHANGE,
    ), f"realignment did not run: {outcome.outcome}/{outcome.reason}"

    after = sorted(
        _every_slot_id((await planning.get_active_plan(db, 1, "workout"))["payload"])
    )
    assert after == before, (
        "A9 realignment changed slot identities; moving a session between days "
        "must not change what it trains"
    )


def test_reserved_states_are_never_performable() -> None:
    """"Accepted on read" must not mean "silently normal".

    `calibrating`, `retired` and `temporarily_unavailable` fell through the
    classifier to the id check and came back `ok`, so a RETIRED slot was
    performable and counted its sets -- the user would have been told to train
    a slot that was explicitly withdrawn. They are declared states without a
    producer, not exercises.
    """
    for state in sorted(slots.RESERVED_SLOT_STATES):
        entry = {
            "slot_state": state, "id": "bench", "sets": 3,
            "slot_id": "A#0:bench",
        }
        assert slots.classify_entry(entry) != slots.ENTRY_OK, state
        assert slots.is_performable(entry) is False, (
            f"a {state!r} slot is performable -- the user would be told to "
            "train it"
        )
        assert slots.planned_sets_of(entry) == 0, state


def test_shipped_states_keep_their_distinct_classifications() -> None:
    """The reserved-state fix must not blur the three shipped states."""
    base = {"id": "bench", "sets": 3, "slot_id": "A#0:bench"}

    assert slots.classify_entry({**base, "slot_state": "mapped"}) == slots.ENTRY_OK
    assert slots.classify_entry({**base, "slot_state": "blocked"}) == slots.ENTRY_BLOCKED
    assert slots.classify_entry({**base, "slot_state": "unmapped"}) == (
        slots.ENTRY_UNMAPPED
    )
    # And the two non-state cases stay distinct from all of them.
    assert slots.classify_entry({"id": "bench", "sets": 3}) == (
        slots.ENTRY_LEGACY_NO_SLOT
    )
    assert slots.classify_entry({**base, "slot_state": "zzz"}) == slots.ENTRY_MALFORMED


def test_a_version_shaped_exercise_id_cannot_break_the_callback() -> None:
    """`^v\\d{1,9}$` is the version grammar, matched in the terminal two fields.

    No exercise id in the catalog or any template matches it today -- checked
    across all 56 -- but that is an accident of the current data, not a
    guarantee. If one ever did, `strict_extract_version` would read it as a
    flow version and the router would refuse EVERY tap on that alternative as
    stale: a substitution that silently stops working for one exercise, with
    nothing in the logs naming the cause.
    """
    from noam_coach.bot import callback_session as cs_bot
    from noam_coach.services import callback_grammar

    # The strict extractors are called DIRECTLY rather than through
    # `install_callback_grammar()`. That installer rebinds
    # `conversation.extract_version` / `extract_flow_id` process-wide and is
    # idempotent by a module-level sentinel, so it can never be undone within a
    # session -- it leaked into `test_extract_flow_id_legacy`, which asserts the
    # LEGACY extractor still accepts `legacy-42`. Locally the two files never
    # ran in that order; CI ran them in one process and failed. Calling the
    # strict functions directly tests exactly the same thing with no global
    # state touched.
    session = {"id": 5, "exercise_index": 0, "set_number": 1}

    for alt_id in ("v123", "v9", "v999999999"):
        minted = cs_bot._substitution_callback(
            session, {"id": "x"}, {"id": alt_id}, cs_bot.SUB_REASON_PAIN
        )
        assert callback_grammar.strict_extract_version(minted) is None, (
            f"{minted!r} parses as carrying a flow version -- the router would "
            "refuse this tap as stale"
        )
        assert callback_grammar.strict_extract_flow_id(minted) is None, minted
        assert minted.split(":")[1].isdigit(), (
            "parts[1] must stay numeric or the callback is rejected before any "
            "handler sees it"
        )
        assert len(minted.encode("utf-8")) <= 64


def test_every_minted_substitution_callback_fits_telegrams_limit() -> None:
    """64 bytes, asserted on the longest real ids rather than assumed."""
    from noam_coach.bot import callback_session as cs_bot
    from training_intelligence import CATALOG

    session = {"id": 9999999, "exercise_index": 99, "set_number": 99}
    longest = max(CATALOG.keys(), key=len) if CATALOG else "long_exercise_id"

    for reason in sorted(cs_bot.SUB_REASONS):
        minted = cs_bot._substitution_callback(
            session, {"id": "x"}, {"id": longest}, reason
        )
        assert len(minted.encode("utf-8")) <= 64, (
            f"{minted!r} is {len(minted.encode())} bytes"
        )


# ---------------------------------------------------------------------------
# Backfill inheritance
#
# `adapt_exercises` removes an exercise that loads a painful joint and appends a
# replacement. The replacement FILLS the original professional need -- same
# slot, different implementation -- so it must inherit that slot's identity.
#
# Measured before this was fixed: a knee-pain adaptation of session F removed
# `leg_press` and appended `glute_bridge`, which was then minted
# `F#0:glute_bridge` from its final list position. The slot's history was lost
# and the swap looked like a brand-new need.
# ---------------------------------------------------------------------------
def _adapted_session_f():
    """Session F adapted for knee pain: `leg_press` is removed and backfilled."""
    import copy

    import training_intelligence as ti
    from exercise_plans import PLANS

    entries = copy.deepcopy(PLANS["F"]["exercises"])
    slots.assign_slot_ids(entries, "F#0")
    original_ids = [e["id"] for e in entries]
    adapted, _changes = ti.adapt_exercises(
        entries,
        equipment_value="full_gym",
        location="gym",
        pain_value="knee pain",
        medical_avoidance="knee pain",
        experience="intermediate",
        pain_detail={},
    )
    return original_ids, adapted


def test_a_backfilled_exercise_inherits_the_slot_it_fills() -> None:
    """The discriminating case: inherited identity, not positional identity.

    `glute_bridge` replaces `leg_press`, so it must carry `F#0:leg_press`. If it
    were minted from its position it would carry `F#0:glute_bridge` -- a
    different id for the same need, which is exactly the identity loss the slot
    model exists to prevent.
    """
    original_ids, adapted = _adapted_session_f()

    assert "leg_press" in original_ids, (
        "the fixture must actually contain the exercise that gets removed"
    )
    replaced = [e for e in adapted if e["id"] != "leg_press" and e.get("original_id")]
    assert replaced, (
        "knee pain must actually trigger a backfill here, or this test proves "
        "nothing"
    )

    entry = replaced[0]
    assert entry["slot_id"] == "F#0:leg_press", (
        f"the backfilled {entry['id']!r} carries {entry['slot_id']!r}; it must "
        "inherit the identity of the need it fills, not be minted from its "
        "list position"
    )
    assert entry["id"] != "leg_press", "the implementation did change"
    assert entry["original_id"] == "leg_press", (
        "the slot must remember what it originally implemented"
    )


def test_a_backfilled_exercise_is_not_minted_from_its_position() -> None:
    """Stated as its own assertion, because the two are easy to confuse.

    A positional mint would produce an id derived from the NEW exercise. No
    surviving slot id may name the replacement.
    """
    _original_ids, adapted = _adapted_session_f()
    slot_ids = [e.get("slot_id") for e in adapted if e.get("slot_id")]

    for entry in adapted:
        if entry.get("original_id"):
            assert f"F#0:{entry['id']}" not in slot_ids, (
                f"a slot id was minted from the replacement {entry['id']!r} "
                "rather than inherited from the need it fills"
            )


def test_backfill_does_not_duplicate_a_slot_identity() -> None:
    """Inheritance must not collide with a slot that already exists."""
    _original_ids, adapted = _adapted_session_f()
    slot_ids = [e.get("slot_id") for e in adapted if e.get("slot_id")]

    assert slot_ids, "the adapted session must carry slot ids"
    assert len(slot_ids) == len(set(slot_ids)), (
        f"duplicate slot ids after backfill: {slot_ids}"
    )


@pytest.mark.asyncio
async def test_backfilled_slots_survive_into_the_stored_plan(
    tmp_path, monkeypatch
) -> None:
    """End to end: a pain-adapted plan stores inherited identities, not new ones."""
    db = Database(str(tmp_path / "backfill.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (utc_now(),),
    )
    _bind(monkeypatch, db)
    for key, value in dict(_WORKOUT_FACTS, training_limitations="knee pain").items():
        await user_model.set_fact(
            db, 1, key, value,
            kind=user_model.KIND_FACT, source=user_model.SOURCE_USER,
        )

    candidates = await planning.generate_candidates(db, 1, "workout")
    ids = [
        e.get("slot_id")
        for c in candidates
        for s in c.payload["sessions"]
        for e in s["exercises"]
    ]

    assert ids and all(slots.is_valid_slot_id(i) for i in ids), (
        "every stored entry must carry a valid slot id, including backfilled ones"
    )
    for candidate in candidates:
        for session in candidate.payload["sessions"]:
            session_ids = [e.get("slot_id") for e in session["exercises"]]
            assert len(session_ids) == len(set(session_ids)), (
                f"duplicate slot ids within one session: {session_ids}"
            )
