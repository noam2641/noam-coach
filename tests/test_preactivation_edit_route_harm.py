"""Does the pre-activation edit button actually cause harm?

`render_workout_exercise_review` is wizard step 3: the user is looking at a
CANDIDATE plan, before activation, and each session offers
`editparams_menu:<code>`. That callback carries only a plan CODE -- no plan id,
no session index -- so nothing in it identifies the plan being reviewed.

`workout_compat.resolve_legacy_code` maps a bare code onto the user's CURRENT
plan, and returns `template_fallback` when `list_selectable_workouts` finds
nothing -- i.e. no Tier-1 plan and no Tier-2 fact. A user reviewing their FIRST
plan is exactly that user: the candidate is not active yet, so they have no
current plan at all.

The hypothesis this file exists to settle: in that state the button routes to
`get_user_plan`, which reads the GLOBAL template rather than the candidate, so
edits land on template positions while the user believes they are editing the
plan on screen. When the candidate is later activated with a different exercise
ordering, those overrides apply to the wrong exercises.

A6 is classified [H] precisely because that chain is plausible but unproven.
If these tests cannot demonstrate harm, A6 closes as documentation and no
production code changes -- adding a writer or widening an allowlist to "fix" an
unproven defect would be worse than leaving it alone.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import coach_bot
from db import Database
from exercise_plans import PLANS
from helpers import utc_now
from noam_coach.bot import workout_compat
from noam_coach.services import profile as profile_service


async def _user(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Database:
    db = Database(str(tmp_path / "a6.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (utc_now(),),
    )
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(profile_service, "DB", db)
    return db


async def _candidate_with_reordered_exercises(db: Database) -> int:
    """A candidate whose session A lists the template's exercises reversed.

    Reversal is the cheapest way to make "position in the candidate" and
    "position in the template" disagree for every entry, so an override applied
    by the wrong one is unambiguous rather than coincidentally correct.
    """
    template = PLANS["A"]["exercises"]
    reordered = list(reversed(template))
    payload = {
        "frequency": 1,
        "sessions": [
            {
                "index": 0,
                "weekday": 0,
                "time": "18:00",
                "minutes": 45,
                "code": "A",
                "name": "A",
                "exercises": reordered,
            }
        ],
    }
    plan_id = await db.execute(
        "INSERT INTO plan_versions("
        "  user_id, plan_type, title, strategy, fit_score, status, payload, created_at"
        ") VALUES(1, 'workout', 'A', 'balanced', 0.8, 'candidate', ?, ?)",
        (json.dumps(payload, ensure_ascii=False), utc_now()),
    )
    return int(plan_id)


@pytest.mark.asyncio
async def test_a_user_reviewing_a_candidate_has_no_current_plan(
    tmp_path, monkeypatch
) -> None:
    """Premise 1: the reviewing user really does fall to template_fallback.

    If this fails the whole hypothesis collapses -- the button would resolve to
    a real plan and there would be no harm to demonstrate.
    """
    db = await _user(tmp_path, monkeypatch)
    await _candidate_with_reordered_exercises(db)

    outcome = await workout_compat.resolve_legacy_code(db, 1, "A")

    assert outcome["outcome"] == workout_compat.OUTCOME_TEMPLATE_FALLBACK, (
        "a candidate plan is not selectable, so the reviewing user has no "
        "current plan and the legacy code resolves to the global template"
    )


@pytest.mark.asyncio
async def test_the_edit_route_reads_the_template_not_the_candidate(
    tmp_path, monkeypatch
) -> None:
    """Premise 2: what the edit screen shows is the template, not the plan.

    This is the substance of the harm. The user is looking at a candidate whose
    exercises are in one order; the edit route hands back the template's order.
    """
    db = await _user(tmp_path, monkeypatch)
    plan_id = await _candidate_with_reordered_exercises(db)

    candidate = await db.fetch_one(
        "SELECT payload FROM plan_versions WHERE id=?", (plan_id,)
    )
    reviewed = json.loads(candidate["payload"])["sessions"][0]["exercises"]

    edit_screen = await profile_service.get_user_plan(1, "A")
    shown = edit_screen["exercises"]

    assert [e["id"] for e in shown] != [e["id"] for e in reviewed], (
        "if these matched there would be no harm: the edit screen would be "
        "showing the plan under review"
    )
    assert [e["id"] for e in shown] == [e["id"] for e in PLANS["A"]["exercises"]], (
        "the edit screen is showing the global template"
    )


@pytest.mark.asyncio
async def test_an_override_lands_on_a_different_exercise_than_the_user_edited(
    tmp_path, monkeypatch
) -> None:
    """Premise 3 -- the harm itself, stated as the user would experience it.

    The user edits the FIRST exercise on the screen in front of them. The
    override is keyed by that screen position. Once the candidate activates,
    position 0 of their real plan is a different exercise, so the weight they
    set for one movement is applied to another.
    """
    db = await _user(tmp_path, monkeypatch)
    plan_id = await _candidate_with_reordered_exercises(db)

    # What the user believes they are editing: first row of the reviewed plan.
    candidate = await db.fetch_one(
        "SELECT payload FROM plan_versions WHERE id=?", (plan_id,)
    )
    reviewed = json.loads(candidate["payload"])["sessions"][0]["exercises"]
    intended_exercise_id = reviewed[0]["id"]

    # What the edit route actually writes: position 0 of the TEMPLATE.
    await profile_service.set_exercise_override(1, "A", 0, "weight", 99.0)

    template_exercise_id = PLANS["A"]["exercises"][0]["id"]

    assert intended_exercise_id != template_exercise_id, (
        "fixture sanity: the two orderings must disagree at position 0"
    )

    applied = await profile_service.get_user_plan(1, "A")
    assert applied["exercises"][0]["weight"] == 99.0
    assert applied["exercises"][0]["id"] == template_exercise_id, (
        f"HARM: the user edited {intended_exercise_id!r} on screen, but the "
        f"override is bound to position 0, which is {template_exercise_id!r} "
        "in the template. After activation this weight follows the position, "
        "not the exercise the user chose."
    )


@pytest.mark.asyncio
async def test_the_override_survives_activation_and_hits_the_wrong_exercise(
    tmp_path, monkeypatch
) -> None:
    """Premise 4: the damage persists past the moment it was created.

    An override written against template positions is not cleaned up when the
    candidate activates. `exercise_overrides` is keyed `(user_id, code,
    exercise_index, field)` with no plan id, so the row simply reapplies -- now
    against a plan whose ordering it was never written for.
    """
    db = await _user(tmp_path, monkeypatch)
    plan_id = await _candidate_with_reordered_exercises(db)

    await profile_service.set_exercise_override(1, "A", 0, "weight", 99.0)

    # Activate the candidate by hand -- planning.activate_plan enforces a
    # readiness gate that is irrelevant to this question.
    await db.execute(
        "UPDATE plan_versions SET status='active', activated_at=? WHERE id=?",
        (utc_now(), plan_id),
    )
    await db.execute(
        "INSERT INTO active_plans(user_id, plan_type, plan_id, updated_at) "
        "VALUES(1, 'workout', ?, ?)",
        (plan_id, utc_now()),
    )

    row = await db.fetch_one(
        "SELECT exercise_index, exercise_id, value FROM exercise_overrides "
        "WHERE user_id=1 AND code='A' AND field='weight'"
    )
    assert row is not None, "the override outlives the review it was made in"
    assert row["exercise_index"] == 0
    assert row["exercise_id"] is None, (
        "written through the legacy route, so it carries no stable identity "
        "and can only ever be reapplied by position"
    )


# ---------------------------------------------------------------------------
# The fix
# ---------------------------------------------------------------------------
def test_the_review_screen_no_longer_mints_the_legacy_edit_route() -> None:
    """The wizard must not offer an editor that cannot see the plan on screen.

    Asserted against the review renderer specifically, not the whole file:
    `editparams_menu:` remains legitimate elsewhere, where the user genuinely
    has an active plan for the code to resolve against.

    The fix is removal rather than redirection. The managed v2 editor
    (`wk:ex:<identity>:<sidx>:<index>`) requires a resolved plan identity,
    which a candidate does not have -- so routing there would mean inventing a
    third editing surface for a plan that may never be activated. A6's rule is
    that the route must stop needing an unmanaged write, and withholding the
    button achieves exactly that.
    """
    import ast
    import inspect

    from noam_coach.bot import onboarding

    source = inspect.getsource(
        inspect.unwrap(onboarding.render_workout_exercise_review)
    )
    tree = ast.parse(source.lstrip())
    body = [
        node for node in tree.body[0].body
        if not (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        )
    ]
    code = "\n".join(ast.unparse(node) for node in body)

    assert "editparams_menu" not in code, (
        "the pre-activation review screen mints a legacy edit callback again; "
        "it resolves against the user's CURRENT plan, which a reviewing user "
        "does not have -- see the harm tests above"
    )


def test_the_fix_does_not_disturb_legitimate_edit_entry_points() -> None:
    """`editparams_menu:` is still minted where a plan actually exists.

    Removing it everywhere would be over-correction: the route is sound once
    the user has an active plan for the bare code to resolve against.
    """
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parents[1]
    still_minting = [
        relative
        for relative in ("noam_coach/bot/ui.py", "noam_coach/bot/callback_plans.py")
        if "editparams_menu:" in (root / relative).read_text(encoding="utf-8")
    ]
    assert still_minting, (
        "the legacy edit entry point should survive where it is valid -- only "
        "the pre-activation review case was harmful"
    )


# ---------------------------------------------------------------------------
# The stale button: removal does not reach into Telegram history
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_stale_review_button_cannot_still_write_a_template_override(
    tmp_path, monkeypatch
) -> None:
    """Removing the button does not retract the ones already sent.

    Telegram messages are durable. Every user who reached wizard step 3 before
    this change still has an `editparams_menu:` button in their chat history,
    and pressing it re-enters the same route the harm tests describe. If the
    handler still resolves it through the template path, the fix protects only
    new users and the defect stays live for exactly the people who already met
    it.

    The adapter returns False for template_fallback, letting the caller run the
    explicit legacy template path -- correct for a plan-less user editing the
    template deliberately, wrong for a stale review button. This pins that the
    press cannot silently produce a template-bound override.
    """
    from noam_coach.bot import callback_plans

    db = await _user(tmp_path, monkeypatch)
    await _candidate_with_reordered_exercises(db)
    monkeypatch.setattr(callback_plans, "DB", db)

    before = await db.fetch_all("SELECT * FROM exercise_overrides WHERE user_id=1")
    assert before == [], "fixture starts with no overrides"

    class _Query:
        def __init__(self) -> None:
            self.messages: list[str] = []
            self.answers: list[str] = []
            self.message = type("M", (), {"chat_id": 1, "message_id": 1})()

        async def edit_message_text(self, text, reply_markup=None, parse_mode=None):
            self.messages.append(text)

        async def answer(self, text=None, show_alert=False):
            if text:
                self.answers.append(text)

    query = _Query()
    # The exact callback a stale review button carries.
    await callback_plans.handle_plan_callback(query, 1, "editparams_menu:A")

    after = await db.fetch_all("SELECT * FROM exercise_overrides WHERE user_id=1")
    assert after == [], (
        "a stale review button wrote an override bound to template positions -- "
        "the same defect the removal was meant to close, still reachable for "
        "every user who saw the old screen"
    )


@pytest.mark.asyncio
async def test_the_stale_picker_still_leads_to_a_template_bound_write(
    tmp_path, monkeypatch
) -> None:
    """The honest bound on what removing the button achieved.

    Opening the picker writes nothing, so the previous test passes trivially.
    The write happens on the NEXT tap -- `param:<code>:<index>:<field>:<delta>`
    -- and that route is unchanged: it is the legitimate template editor a
    plan-less user reaches from the workout menu, and it cannot tell a
    deliberate template edit from a stale review button.

    Recording this as an asserted fact rather than a footnote. A6 removes the
    route that CREATES the confusion for new users; it does not and cannot
    retract buttons already in Telegram history. Closing that gap needs stable
    identity in the callback itself -- A11b's slot identity -- not another
    special case here.

    If this assertion ever flips, the legacy template editor has gained
    identity awareness and this bound should be revisited.
    """
    from noam_coach.bot import callback_plans

    db = await _user(tmp_path, monkeypatch)
    await _candidate_with_reordered_exercises(db)
    monkeypatch.setattr(callback_plans, "DB", db)

    outcome = await workout_compat.resolve_legacy_code(db, 1, "A")
    assert outcome["outcome"] == workout_compat.OUTCOME_TEMPLATE_FALLBACK

    # The legacy template editor remains reachable and template-bound. This is
    # correct for its intended user and unavoidable for a stale button, since
    # the callback carries no plan identity to distinguish them.
    await profile_service.set_exercise_override(1, "A", 0, "weight", 77.0)
    row = await db.fetch_one(
        "SELECT exercise_index, exercise_id FROM exercise_overrides "
        "WHERE user_id=1 AND code='A' AND field='weight'"
    )
    assert row is not None
    assert row["exercise_id"] is None, (
        "the legacy route still writes positionally; a stale button is "
        "indistinguishable from a deliberate template edit until the callback "
        "carries stable identity (A11b)"
    )
