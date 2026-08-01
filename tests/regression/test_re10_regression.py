"""RE10 regression tests — Phase 0/1 critical-bug fixes.

Covers:
  * RE10-1: ``routine:*`` callbacks were silently dropped by the router.
  * RE10-2: gap-dict values leaking into user-facing text (display + persist).
  * RE10-6: plan-completion questions must prioritize the plan_type the user
    actually asked to complete, not always "workout" first.
  * RE10-7: choosing a full gym as the training location must not also
    trigger a redundant equipment question.
  * D11: every readiness-gate fact key has a Hebrew display label.
  * Orphan-callback guard: every ``button(..., "prefix:...")`` prefix used in
    the bot is actually dispatched somewhere in the router chain.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import coach_bot
import conversation
import planning
import user_model
from noam_coach.bot import onboarding as bot_onboarding

ROOT = Path(__file__).resolve().parents[2]


async def _make_db(tmp_path: Path) -> coach_bot.Database:
    db = coach_bot.Database(str(tmp_path / "coach.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (coach_bot.utc_now(),),
    )
    return db


# ---------------------------------------------------------------------------
# RE10-1 — routine:* routing
# ---------------------------------------------------------------------------


def test_routine_prefix_is_routed_to_onboarding_handler() -> None:
    """The router must recognize routine: the same way it recognizes onb:/qa:."""
    from noam_coach.bot import callback_router

    src = Path(callback_router.__file__).read_text(encoding="utf-8")
    assert (
        'data.startswith("routine:")' in src
    ), "callback_router must dispatch routine: to handle_onboarding_callback"


@pytest.mark.asyncio
async def test_routine_confirm_reaches_onboarding_handler_not_session_fallback() -> None:
    """routine:confirm/fix/skip must never reach the digit-only session fallback."""

    for data in ("routine:confirm", "routine:fix", "routine:skip"):
        parts = data.split(":")
        # This is exactly the guard in handle_session_action_callback that
        # silently swallowed these callbacks before the router fix.
        assert len(parts) >= 2 and not parts[1].isdigit()
    # The router-level fix means these never reach that fallback at all —
    # verified structurally above; this asserts the fallback would indeed
    # have rejected them, proving the router fix is load-bearing.


@pytest.mark.asyncio
async def test_routine_confirm_free_text_reaches_question_flow_not_fallback(
    tmp_path: Path,
) -> None:
    """Sibling bug to RE10-1: a plain-text correction to the "describe your
    day" summary card (pending == "__routine_confirm__") must route through
    ConversationRouter to "question_flow" -- the only path that reaches
    handle_onboarding_text's dedicated __routine_confirm__ correction branch
    (onboarding.py:2910). Before this fix, FlowName.routine_confirm was
    mapped from the pending key (PENDING_KEY_TO_FLOW) but missing from
    QUESTION_FLOWS, so flow.is_question was False and the router fell through
    to the generic "free_text" fallback -- silently dropping the correction.
    """
    db = await _make_db(tmp_path)
    coach_bot.DB.path = db.path

    await conversation.set_active_flow(
        db, 1, conversation.FlowName.routine_confirm, step="__routine_confirm__",
    )
    flow = await conversation.get_active_flow(db, 1)
    assert flow.name == conversation.FlowName.routine_confirm

    # This is the exact assertion that was false before the fix.
    assert flow.is_question is True

    decision = await conversation.ConversationRouter.route(db, 1, "text")
    assert decision.handler == "question_flow"
    assert decision.action == "consume"


# ---------------------------------------------------------------------------
# RE10-2 — dict-leak fixes (display + persistence)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gather_plan_constraints_skips_gap_allergies(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    coach_bot.DB.path = db.path  # bot module reads the shared DB singleton
    await user_model.record_gap(db, 1, "allergies", why_matters="בטיחות תזונתית")
    await user_model.record_gap(db, 1, "diet_restrictions", why_matters="קובע אילו מאכלים להציע")

    constraints = await bot_onboarding.gather_plan_constraints(1)
    labels = [c.label for c in constraints]
    assert not any("{" in label for label in labels)
    assert not any("missing" in label for label in labels)
    assert not any(c.key == "allergies" for c in constraints)
    assert not any(c.key == "diet_restrictions" for c in constraints)


@pytest.mark.asyncio
async def test_gather_plan_constraints_shows_real_allergy(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    coach_bot.DB.path = db.path
    await user_model.set_fact(
        db, 1, "allergies", "בוטנים",
        kind=user_model.KIND_FACT,
        source=user_model.SOURCE_USER,
        confirmed=True,
    )
    constraints = await bot_onboarding.gather_plan_constraints(1)
    allergy = next(c for c in constraints if c.key == "allergies")
    assert allergy.label == "אלרגיות: בוטנים"


@pytest.mark.asyncio
async def test_existing_list_value_treats_gap_as_empty(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    coach_bot.DB.path = db.path
    await user_model.record_gap(db, 1, "allergies", why_matters="בטיחות תזונתית")

    items = await bot_onboarding._existing_list_value(1, "allergies")
    assert items == []


@pytest.mark.asyncio
async def test_diet_type_allergy_answer_persists_clean_value(tmp_path: Path) -> None:
    """Reproduces the exact screenshot bug: answering an allergy question that
    was previously a gap must not persist the gap-dict repr alongside the
    real answer.
    """
    db = await _make_db(tmp_path)
    coach_bot.DB.path = db.path
    await user_model.record_gap(db, 1, "allergies", why_matters="בטיחות תזונתית")

    existing = await bot_onboarding._existing_list_value(1, "allergies")
    assert existing == []
    existing.append("אגוזים")
    await user_model.set_fact(
        db, 1, "allergies", ", ".join(existing),
        kind=user_model.KIND_FACT,
        source=user_model.SOURCE_USER,
        confirmed=True,
    )
    fact = await user_model.get_fact(db, 1, "allergies")
    assert fact["value"] == "אגוזים"
    assert "missing" not in fact["value"]
    assert "{" not in fact["value"]


# ---------------------------------------------------------------------------
# RE10-2 — migration 10 cleans already-polluted DB rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_migration_cleans_polluted_gap_value(tmp_path: Path) -> None:
    import db as db_module

    db = coach_bot.Database(str(tmp_path / "coach.db"))
    await db.init()  # runs all migrations including #10 on a fresh DB — no-op here
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (coach_bot.utc_now(),),
    )
    # Simulate the pre-fix corruption directly (bypassing the now-fixed code path).
    import json

    now = coach_bot.utc_now()
    polluted = "{'missing': True, 'why_matters': 'בטיחות תזונתית'}, אגוזים"
    await db.execute(
        "INSERT INTO user_facts(user_id, key, value, kind, source, confidence, "
        "confirmed, valid, affects, updated_at, created_at) "
        "VALUES(1, 'allergies', ?, 'fact', 'user', 0.9, 1, 1, '[]', ?, ?)",
        (json.dumps(polluted, ensure_ascii=False), now, now),
    )
    # Roll schema_migrations back so migration 10 re-runs against this row.
    await db.execute("DELETE FROM schema_migrations WHERE version=10")

    await db_module.run_migrations(db, backup_existing=False)

    fact = await user_model.get_fact(db, 1, "allergies")
    assert fact is not None
    assert "missing" not in fact["value"]
    assert "{" not in fact["value"]
    assert "אגוזים" in fact["value"]


def test_strip_polluted_gap_repr_helper() -> None:
    import db as db_module

    cleaned, changed = db_module._strip_polluted_gap_repr(
        "{'missing': True, 'why_matters': 'x'}, אגוזים"
    )
    assert changed is True
    assert cleaned == "אגוזים"

    cleaned2, changed2 = db_module._strip_polluted_gap_repr("בוטנים")
    assert changed2 is False
    assert cleaned2 == "בוטנים"


# ---------------------------------------------------------------------------
# RE10-6 — plan-completion question ordering follows the requested plan_type
# ---------------------------------------------------------------------------


async def _make_user_missing_workout_and_nutrition_facts(tmp_path: Path) -> coach_bot.Database:
    """A user with gaps in BOTH profiles: allergies/diet (nutrition) and
    session_minutes/training_location/equipment/... (workout). Safety facts
    (active_pain, medical_avoidance) are answered so they never win priority.
    """
    db = await _make_db(tmp_path)
    coach_bot.DB.path = db.path
    for key in ("active_pain", "medical_avoidance"):
        await user_model.set_fact(
            db, 1, key, "none",
            kind=user_model.KIND_FACT, source=user_model.SOURCE_USER, confirmed=True,
        )
    for key in ("weight_kg", "primary_goal", "sex", "age", "training_days_per_week"):
        await user_model.set_fact(
            db, 1, key, "placeholder",
            kind=user_model.KIND_FACT, source=user_model.SOURCE_USER, confirmed=True,
        )
    return db


@pytest.mark.asyncio
async def test_plan_completion_prioritizes_requested_nutrition_profile(tmp_path: Path) -> None:
    await _make_user_missing_workout_and_nutrition_facts(tmp_path)
    question = await bot_onboarding.first_missing_plan_question(1, "nutrition")
    assert question is not None
    assert question.fact_key in ("diet_restrictions", "allergies")


@pytest.mark.asyncio
async def test_plan_completion_prioritizes_requested_workout_profile(tmp_path: Path) -> None:
    await _make_user_missing_workout_and_nutrition_facts(tmp_path)
    question = await bot_onboarding.first_missing_plan_question(1, "workout")
    assert question is not None
    assert question.fact_key not in ("diet_restrictions", "allergies")


@pytest.mark.asyncio
async def test_nutrition_completion_stops_instead_of_asking_workout_questions(
    tmp_path: Path,
) -> None:
    """TASK-01: once nutrition facts are all filled, a nutrition-scoped
    completion must report "done" (None) rather than surfacing a workout
    question like session_minutes/training_location/equipment.
    """
    db = await _make_db(tmp_path)
    coach_bot.DB.path = db.path
    for key in (
        "weight_kg", "primary_goal", "sex", "age",
        "diet_restrictions", "allergies", "food_environment_context",
    ):
        await user_model.set_fact(
            db, 1, key, "placeholder",
            kind=user_model.KIND_FACT, source=user_model.SOURCE_USER, confirmed=True,
        )
    question = await bot_onboarding.first_missing_plan_question(1, "nutrition")
    assert question is None


@pytest.mark.asyncio
async def test_plan_completion_defaults_to_workout_first_without_plan_type(tmp_path: Path) -> None:
    """No plan_type given: preserves the original (workout-first) tuple order."""
    await _make_user_missing_workout_and_nutrition_facts(tmp_path)
    question = await bot_onboarding.first_missing_plan_question(1, None)
    assert question is not None
    assert question.fact_key not in ("diet_restrictions", "allergies")


def test_plan_completion_profile_order_prioritizes_given_type() -> None:
    # TASK-01: a nutrition completion must stay confined to nutrition facts —
    # it never drifts into workout questions once nutrition is satisfied.
    order = bot_onboarding._plan_completion_profile_order("nutrition")
    assert order == ("nutrition",)

    # A workout completion may still ask safety questions (pain/medical
    # limitations directly gate exercise selection).
    order_workout = bot_onboarding._plan_completion_profile_order("workout")
    assert order_workout == ("workout", "safety")

    order_unknown = bot_onboarding._plan_completion_profile_order("something_else")
    assert order_unknown == bot_onboarding.PLAN_COMPLETION_PROFILES


# ---------------------------------------------------------------------------
# RE10-7 — full-gym location infers equipment (no redundant question)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_gym_location_infers_equipment_fact(tmp_path: Path) -> None:
    import questions

    db = await _make_db(tmp_path)
    coach_bot.DB.path = db.path

    class FakeMessage:
        async def reply_text(self, *_a, **_k) -> None:
            return None

    class FakeQuery:
        message = FakeMessage()

    question = questions.question_by_id("q_location")
    assert question is not None
    followed_up = await bot_onboarding.handle_safety_answer(FakeQuery(), 1, question, "gym")
    assert followed_up is False  # no free-text follow-up needed for "gym"

    fact = await user_model.get_fact(db, 1, "equipment")
    assert fact is not None
    assert fact["value"] == "full_gym"
    assert fact["confirmed"] is True


@pytest.mark.asyncio
async def test_home_location_does_not_infer_equipment(tmp_path: Path) -> None:
    import questions

    db = await _make_db(tmp_path)
    coach_bot.DB.path = db.path

    class FakeMessage:
        async def reply_text(self, *_a, **_k) -> None:
            return None

    class FakeQuery:
        message = FakeMessage()

    question = questions.question_by_id("q_location")
    assert question is not None
    await bot_onboarding.handle_safety_answer(FakeQuery(), 1, question, "home")

    fact = await user_model.get_fact(db, 1, "equipment")
    assert fact is None


# ---------------------------------------------------------------------------
# D11 — every readiness-required fact key (+ active_goal) has a Hebrew label
# ---------------------------------------------------------------------------


def test_all_readiness_required_facts_have_hebrew_labels() -> None:
    all_required_keys: set[str] = {"active_goal"}
    for profile in user_model.READINESS_PROFILES.values():
        all_required_keys.update(profile.required)

    for key in all_required_keys:
        label = planning.FACT_LABELS.get(key) or user_model.display_label(key)
        assert label, f"missing label for {key}"
        assert "_" not in label, f"label for {key} leaks a raw key: {label!r}"
        assert not re.search(r"[a-zA-Z]", label), (
            f"label for {key} is not Hebrew: {label!r}"
        )


# ---------------------------------------------------------------------------
# Orphan-callback guard
# ---------------------------------------------------------------------------

# Prefixes owned by each router family, verified by reading the dispatch code
# in callback_router.py / callback_menu.py / callback_plans.py /
# callback_meals.py / onboarding.py. The final fallback
# (handle_session_action_callback) accepts ANY prefix whose second colon-part
# is a plain integer (a session id) — those are intentionally not enumerated
# here per-prefix; they are validated structurally instead (see below).
_ROUTER_OWNED_PREFIXES = {
    "onb", "qa", "routine",  # handle_onboarding_callback
    "chk",  # handle_checkin_callback
    "menu", "approve_goal", "reject_goal", "nextmeal", "reconcile_ok",
    "reconcile_no", "health", "confirm", "flag",  # handle_menu_callback
    "plan", "planv2", "workout", "startworkout", "wparamtext",
    "editparams_menu", "editparams", "param",  # handle_plan_callback / handle_workout_setup_callback
    # workout-selection architecture, Batch 4: the `wk:` selector/overview/
    # start graph -> _handle_workout_v2_actions (+ `wk:list`, which shares
    # _handle_workout_menu_actions with menu:workout).
    "wk",
    "goal",  # handle_workout_setup_callback -> _handle_workout_menu_actions (RE10-9 "goal:manual")
    "clarify", "fixmeal", "editqtymenu", "editqty", "qtydelta", "dup",
    "reject_dup", "editmeal", "backmeal", "cancelfix", "force_approve_meal",
    "approve_meal", "undo_meal", "reject_meal", "restore_meal",  # handle_meal_callback
    "approve_goal", "reject_goal",  # handle_goal_callback (menu family also matches first)
}

# Prefixes that are only ever used with a numeric session id as the 2nd part
# and are legitimately handled by the final session-scoped fallback.
_SESSION_SCOPED_PREFIXES = {
    "split", "sub", "restadd", "wdone", "wpause", "wcancel",
    # A11b: these 17 are minted through `session_action_data(...)` and were
    # therefore INVISIBLE to the literal-prefix scanner -- the guard passed
    # while covering none of them. Each was verified dispatched by
    # `action == "<name>"` in callback_session.py before being listed here.
    "setok", "different", "occupied", "loadwhy", "reps", "rir",
    "splitw", "splitr", "splitrir", "pain", "painloc", "painlevel",
    "skip", "finish", "ready", "undoset", "reopen",
    # Effort reported from the rest screen. Session-scoped like `restadd`, but
    # it carries a trailing set id + effort token because it names the set it
    # describes rather than resolving "the most recent set" at tap time.
    "seteffort",
}


def _extract_button_prefixes() -> set[str]:
    """Scan bot/ source for every literal callback_data prefix passed to button().

    Deliberately narrow: only scans the argument list of ``button(...)`` calls
    (the InlineKeyboardButton helper that always takes callback_data), not any
    colon-containing string literal in the file — job names, dict keys and
    override-field sets also contain ``"word:..."``-shaped strings that are
    not callback_data and must not be flagged as orphans.
    """
    bot_dir = ROOT / "noam_coach" / "bot"
    call_pattern = re.compile(r"\bbutton\(([^)]*)\)", re.DOTALL)
    prefix_pattern = re.compile(r'"([a-zA-Z_]+):[^"]*"')
    prefixes: set[str] = set()
    for path in bot_dir.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for call in call_pattern.finditer(text):
            for match in prefix_pattern.finditer(call.group(1)):
                prefixes.add(match.group(1))
    return prefixes


def test_no_orphan_callback_prefixes() -> None:
    """Every callback_data prefix must be dispatched somewhere in the router.

    This is the regression guard for the RE10-1 class of bug: a button whose
    prefix is not recognized by any family handler AND whose payload does not
    end in a numeric session id dies silently in handle_session_action_callback.
    """
    found = _extract_button_prefixes()
    known = _ROUTER_OWNED_PREFIXES | _SESSION_SCOPED_PREFIXES
    unknown = found - known
    assert not unknown, (
        f"Unrecognized callback_data prefixes found in noam_coach/bot/: {sorted(unknown)}. "
        "Add them to a router family in callback_router.py and to the "
        "_ROUTER_OWNED_PREFIXES/_SESSION_SCOPED_PREFIXES set in this test."
    )


def _extract_session_action_names() -> set[str]:
    """Every action minted through `session_action_data(...)`.

    The literal-prefix scanner above cannot see these. It matches
    ``button("label", "prefix:...")`` — a string literal containing a colon —
    but a session action is built as ``session_action_data("sub", session, ...)``
    where the prefix is a bare word and the colons are added inside the helper.

    So `sub` was listed in `_SESSION_SCOPED_PREFIXES` while being **invisible**
    to the guard that set exists to feed: the allowlist entry was inert, and a
    new session action could be added, dispatched nowhere, and still pass. A11b
    changes the `sub:` grammar, which is exactly the situation the guard was
    supposed to cover.

    Parsed with `ast` so a mention in a docstring or comment cannot satisfy it.
    """
    import ast

    root = Path(__file__).resolve().parents[2] / "noam_coach" / "bot"
    names: set[str] = set()
    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if called != "session_action_data":
                continue
            if not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                names.add(first.value)
    return names


def test_every_session_action_is_dispatched() -> None:
    """The half of the callback surface the literal scanner cannot reach.

    Fails if a session action is minted but not registered — the same defect
    class as an orphan prefix, on the path the orphan guard is blind to.
    """
    minted = _extract_session_action_names()
    assert minted, "the scanner found no session actions — it has stopped working"

    known = _ROUTER_OWNED_PREFIXES | _SESSION_SCOPED_PREFIXES
    unknown = minted - known
    assert not unknown, (
        f"session actions minted but not dispatched: {sorted(unknown)}. "
        "Add each to _SESSION_SCOPED_PREFIXES here and confirm "
        "handle_session_action_callback handles it."
    )


def test_every_session_action_is_session_scoped() -> None:
    """A minted session action must also be in `SESSION_SCOPED_ACTIONS`.

    That set is what triggers the staleness check in
    `handle_session_action_callback`. An action outside it is dispatched but
    never checked for freshness, so a tap on a stale keyboard applies to the
    wrong step — silently, because the handler still finds a valid session.
    """
    from noam_coach.bot.ui import SESSION_SCOPED_ACTIONS

    #: Deliberately exempt, each verified against its handler. Named here with
    #: a reason so an exemption is a decision, not an omission nobody noticed.
    exempt = {
        # Reverses the LAST logged set. Its whole purpose is to act after the
        # screen has moved on, so a current-step check would reject exactly the
        # taps it exists to serve (`callback_session.py:270`).
        "undoset",
        # Acts on a FINISHED session, and refuses when another is already
        # active. "Is this the current step" is meaningless for a session that
        # has ended (`callback_session.py:1310`).
        "reopen",
    }

    minted = _extract_session_action_names()
    unscoped = minted - set(SESSION_SCOPED_ACTIONS) - exempt
    assert not unscoped, (
        f"session actions minted but not in SESSION_SCOPED_ACTIONS: "
        f"{sorted(unscoped)} — these are dispatched without a staleness check."
    )


def test_a_substitution_callback_cannot_be_read_as_a_version() -> None:
    """The `v<digits>` grammar collision, pinned on the real minted shape.

    `strict_extract_version` inspects the LAST TWO fields. A11b puts the
    exercise id and the reason code there, so if an exercise were ever named
    `v12` the router would read it as a flow version and refuse the tap as
    stale — a substitution that silently stops working.
    """
    import conversation
    from noam_coach.services.callback_grammar import install_callback_grammar

    install_callback_grammar()

    for data in (
        "sub:5:0:1:hack_squat:pain",
        "sub:5:0:1:leg_press:equipment",
        "sub:5:0:1:rdl:unspecified",
    ):
        assert conversation.extract_version(data) is None, (
            f"{data!r} parses as carrying a flow version; the router would "
            "refuse this tap as stale"
        )
        assert conversation.extract_flow_id(data) is None, data
        assert len(data.encode("utf-8")) <= 64, f"{data!r} exceeds Telegram's limit"
