"""TASK-WORKOUT-REST-NEXT-ACTION — the pure next-action resolver.

Every visible rest timer must say what the user does when it ends. The
resolver is the single decision behind both the rest card's text and the
transition that actually happens at zero, so these tests pin the decision
itself: given a plan and a persisted pointer, which of the three cases is it
(another set of the same exercise / a different exercise / nothing left), and
does the copy describe that case truthfully.

Pure by construction: no DB, no bot, no clock.
"""
from __future__ import annotations

import json
from typing import Any

from noam_coach.services.workout_next_action import (
    NEXT_EXERCISE,
    SAME_EXERCISE,
    WORKOUT_COMPLETE,
    render_next_action,
    resolve_from_session,
    resolve_next_action,
)


def _ex(
    exercise_id: str,
    name: str,
    sets: int,
    rmin: int,
    rmax: int,
    **extra: Any,
) -> dict[str, Any]:
    """A plan exercise with the fields the real plan JSON carries."""
    return {
        "id": exercise_id,
        "name": name,
        "sets": sets,
        "rmin": rmin,
        "rmax": rmax,
        "rest": 90,
        "weight": 50.0,
        "inc": 2.5,
        "cues": ["cue"],
        "muscle": "חזה",
        "alts": [],
        **extra,
    }


def _plan() -> dict[str, Any]:
    return {
        "name": "אימון A",
        "exercises": [
            _ex("bench", "לחיצת חזה", 4, 8, 10),
            _ex("row", "חתירה בישיבה", 3, 10, 12),
            _ex("curl", "כפיפת מרפקים", 2, 12, 15),
        ],
    }


# ---------------------------------------------------------------------------
# A — another set of the CURRENT exercise
# ---------------------------------------------------------------------------


def test_same_exercise_names_the_upcoming_set_and_total() -> None:
    action = resolve_next_action(_plan(), exercise_index=0, set_number=3)

    assert action.kind == SAME_EXERCISE
    assert action.exercise_name == "לחיצת חזה"
    assert action.set_number == 3
    assert action.total_sets == 4
    assert action.exercise_index == 0


def test_same_exercise_copy_matches_the_approved_shape() -> None:
    action = resolve_next_action(
        _plan(), exercise_index=0, set_number=3, previous_weight=70.0
    )
    text = render_next_action(action)

    assert text == "הבא:\nסט 3 מתוך 4 — לחיצת חזה\nיעד: 8–10 חזרות\nבסט הקודם: 70 ק״ג"


def test_previous_weight_is_context_only_and_omitted_when_unknown() -> None:
    """An uncertain weight must never be presented as a mandatory target."""
    text = render_next_action(
        resolve_next_action(_plan(), exercise_index=0, set_number=2)
    )

    assert "ק״ג" not in text
    assert "יעד: 8–10 חזרות" in text
    # The only prescriptive line is the rep target the plan actually carries.
    assert "משקל" not in text


def test_previous_weight_line_is_never_a_target_line() -> None:
    text = render_next_action(
        resolve_next_action(
            _plan(), exercise_index=0, set_number=2, previous_weight=70.0
        )
    )

    assert "בסט הקודם: 70 ק״ג" in text
    assert "יעד: 70" not in text


def test_single_value_rep_target_is_not_rendered_as_a_range() -> None:
    plan = {"exercises": [_ex("plank", "פלאנק", 3, 10, 10)]}
    action = resolve_next_action(plan, exercise_index=0, set_number=2)

    assert action.target_line == "יעד: 10 חזרות"


def test_missing_rep_range_omits_the_target_line_entirely() -> None:
    plan = {"exercises": [{"id": "x", "name": "תרגיל", "sets": 3}]}
    action = resolve_next_action(plan, exercise_index=0, set_number=2)

    assert action.kind == SAME_EXERCISE
    assert action.target_line == ""
    assert "יעד" not in render_next_action(action)


# ---------------------------------------------------------------------------
# B — transition to a DIFFERENT exercise
# ---------------------------------------------------------------------------


def test_next_exercise_is_named_exactly_with_its_first_set() -> None:
    """Pointer already advanced to exercise 2, set 1 — a transition."""
    action = resolve_next_action(_plan(), exercise_index=1, set_number=1)

    assert action.kind == SAME_EXERCISE  # set 1 of 3 is still "this exercise"
    assert action.exercise_name == "חתירה בישיבה"
    assert action.set_number == 1
    assert action.total_sets == 3


def test_pointer_past_the_last_set_transitions_to_the_next_exercise() -> None:
    action = resolve_next_action(_plan(), exercise_index=0, set_number=5)

    assert action.kind == NEXT_EXERCISE
    assert action.exercise_name == "חתירה בישיבה"
    assert action.set_number == 1
    assert action.total_sets == 3
    assert action.exercise_index == 1


def test_transition_copy_matches_the_approved_shape_with_setup_note() -> None:
    plan = _plan()
    plan["exercises"][1]["setup"] = "הכן את מכשיר החתירה."
    action = resolve_next_action(plan, exercise_index=0, set_number=99)

    assert render_next_action(action) == (
        "התרגיל הבא:\nחתירה בישיבה — סט 1 מתוך 3\n"
        "יעד: 10–12 חזרות\nהכן את מכשיר החתירה."
    )


def test_setup_info_is_never_invented_when_the_plan_lacks_it() -> None:
    action = resolve_next_action(_plan(), exercise_index=0, set_number=99)
    text = render_next_action(action)

    assert action.setup_note == ""
    assert "הכן" not in text


def test_a_transition_never_carries_a_previous_weight() -> None:
    """A load from a different movement would be actively misleading."""
    action = resolve_next_action(
        _plan(), exercise_index=0, set_number=99, previous_weight=70.0
    )

    assert action.previous_weight is None
    assert "ק״ג" not in render_next_action(action)


# ---------------------------------------------------------------------------
# D — final rest / completion
# ---------------------------------------------------------------------------


def test_last_set_of_last_exercise_yields_completion_not_a_ghost_exercise() -> None:
    action = resolve_next_action(_plan(), exercise_index=2, set_number=3)

    assert action.kind == WORKOUT_COMPLETE
    assert action.exercise_name == ""


def test_completion_copy_states_the_real_next_step() -> None:
    action = resolve_next_action(_plan(), exercise_index=2, set_number=3)

    assert render_next_action(action) == "לאחר המנוחה:\nסיום האימון ומעבר לסיכום."


def test_a_non_active_session_is_complete_regardless_of_pointer() -> None:
    action = resolve_next_action(
        _plan(), exercise_index=0, set_number=1, status="completed"
    )

    assert action.kind == WORKOUT_COMPLETE


def test_a_pointer_past_the_plan_never_names_an_exercise() -> None:
    action = resolve_next_action(_plan(), exercise_index=7, set_number=1)

    assert action.kind == WORKOUT_COMPLETE
    assert render_next_action(action).endswith("סיום האימון ומעבר לסיכום.")


def test_malformed_plans_degrade_to_completion_instead_of_raising() -> None:
    for bad in (None, {}, {"exercises": []}, {"exercises": "nope"}, "garbage", 17):
        action = resolve_next_action(bad, exercise_index=0, set_number=1)
        assert action.kind == WORKOUT_COMPLETE


# ---------------------------------------------------------------------------
# C — special sequencing over the canonical state
# ---------------------------------------------------------------------------


def test_after_a_skipped_exercise_the_named_action_is_the_one_skipped_to() -> None:
    """Skip moves the pointer to the next exercise, set 1 — the resolver reads
    that pointer, so the card names the exercise the user actually lands on
    and never the one that was skipped."""
    plan = _plan()
    # Skip fired while on exercise 0: session is now (index=1, set=1).
    action = resolve_next_action(plan, exercise_index=1, set_number=1)

    assert action.exercise_name == "חתירה בישיבה"
    assert action.set_number == 1
    assert "לחיצת חזה" not in render_next_action(action)


def test_after_a_selected_alternative_the_substituted_name_is_shown() -> None:
    """`sub` rewrites plan["exercises"][i] IN PLACE, so reading the plan JSON
    already yields the alternative — no special case, and no stale name."""
    plan = _plan()
    plan["exercises"][0] = {
        **plan["exercises"][0],
        "id": "chest_machine",
        "name": "Chest Press",
        "original_id": "bench",
    }
    action = resolve_next_action(plan, exercise_index=0, set_number=2)

    assert action.exercise_name == "Chest Press"
    assert "לחיצת חזה" not in render_next_action(action)


def test_alternative_chosen_for_the_next_exercise_is_reflected_in_a_transition() -> None:
    plan = _plan()
    plan["exercises"][1] = {**plan["exercises"][1], "name": "חתירה בכבל"}
    action = resolve_next_action(plan, exercise_index=0, set_number=99)

    assert action.kind == NEXT_EXERCISE
    assert action.exercise_name == "חתירה בכבל"


def test_split_set_advances_exactly_like_a_normal_set() -> None:
    """save_split_set logs two rows for ONE set and advances the pointer once,
    so the next action after a split set 2 of 4 is set 3 of 4 — not set 4."""
    action = resolve_next_action(_plan(), exercise_index=0, set_number=3)

    assert action.kind == SAME_EXERCISE
    assert action.set_number == 3
    assert action.total_sets == 4


def test_split_set_on_the_final_set_transitions_to_the_next_exercise() -> None:
    action = resolve_next_action(_plan(), exercise_index=1, set_number=4)

    assert action.kind == NEXT_EXERCISE
    assert action.exercise_name == "כפיפת מרפקים"


def test_post_undo_pointer_rewind_is_reflected_immediately() -> None:
    """undo_last_set rewinds (exercise_index, set_number) and reactivates the
    session. The resolver reads the rewound pointer, so the instruction goes
    BACK to the redone set rather than staying on the one that was undone."""
    before_undo = resolve_next_action(_plan(), exercise_index=0, set_number=4)
    after_undo = resolve_next_action(_plan(), exercise_index=0, set_number=3)

    assert before_undo.set_number == 4
    assert after_undo.set_number == 3
    assert after_undo.kind == SAME_EXERCISE


def test_undo_across_an_exercise_boundary_returns_to_the_previous_exercise() -> None:
    """Undo of the first set of exercise 2 rewinds to the last set of
    exercise 1 — the resolver must name exercise 1 again, not exercise 2."""
    after_undo = resolve_next_action(_plan(), exercise_index=0, set_number=4)

    assert after_undo.kind == SAME_EXERCISE
    assert after_undo.exercise_name == "לחיצת חזה"
    assert after_undo.set_number == 4


def test_undo_of_a_completed_workout_reopens_the_final_set() -> None:
    """undo_last_set sets status back to 'active' and rewinds the pointer, so
    a card that said "completion" must go back to naming the final set."""
    completed = resolve_next_action(
        _plan(), exercise_index=2, set_number=2, status="completed"
    )
    reopened = resolve_next_action(_plan(), exercise_index=2, set_number=2)

    assert completed.kind == WORKOUT_COMPLETE
    assert reopened.kind == SAME_EXERCISE
    assert reopened.set_number == 2
    assert reopened.exercise_name == "כפיפת מרפקים"


# ---------------------------------------------------------------------------
# resolve_from_session — the canonical-row entry point both call sites use
# ---------------------------------------------------------------------------


def test_resolve_from_session_parses_the_persisted_plan_json() -> None:
    session = {
        "plan": json.dumps(_plan(), ensure_ascii=False),
        "exercise_index": 1,
        "set_number": 2,
        "status": "active",
    }
    action = resolve_from_session(session)

    assert action.kind == SAME_EXERCISE
    assert action.exercise_name == "חתירה בישיבה"
    assert action.set_number == 2


def test_resolve_from_session_agrees_with_the_pure_function() -> None:
    """The row entry point must be the pure function, not a second opinion."""
    plan = _plan()
    for index in range(3):
        for set_no in range(1, 6):
            session = {
                "plan": json.dumps(plan, ensure_ascii=False),
                "exercise_index": index,
                "set_number": set_no,
                "status": "active",
            }
            assert resolve_from_session(session) == resolve_next_action(
                plan, index, set_no, "active"
            )


def test_resolve_from_session_survives_unreadable_state() -> None:
    for session in (None, {}, {"plan": "{not json", "exercise_index": 0, "set_number": 1}):
        assert resolve_from_session(session).kind == WORKOUT_COMPLETE


# ---------------------------------------------------------------------------
# Agreement with the three INLINE advance chains.
#
# The advance rule is still written as raw SQL in three places
# (workout.save_set, workout_runtime.save_split_set, callback_session skip).
# Those were deliberately NOT rewritten to call the resolver: their value is
# the ATOMIC optimistic UPDATE (rowcount != 1 => duplicate/stale), which a pure
# describer cannot provide, so replacing them would trade a real concurrency
# guard for tidiness. What must never drift is the DECISION they encode, so
# it is pinned here instead: the same branch conditions, evaluated as data.
# ---------------------------------------------------------------------------


def _advance_chain_branch(plan: dict[str, Any], idx: int, set_no: int) -> str:
    """The branch the inline SQL chains take, transcribed literally.

    Mirrors ``if set_no < current["sets"] / elif idx + 1 < len(exercises) /
    else completed`` from all three chains.
    """
    exercises = plan["exercises"]
    if set_no < exercises[idx]["sets"]:
        return "same_exercise"
    if idx + 1 < len(exercises):
        return "next_exercise"
    return "workout_complete"


def test_resolver_agrees_with_the_inline_advance_chains() -> None:
    """For every reachable step, the branch the SQL chains take must equal the
    case the card announces — otherwise the instruction and the real
    transition would disagree, which is the exact bug this task exists to
    prevent."""
    plan = _plan()
    for idx, exercise in enumerate(plan["exercises"]):
        for set_no in range(1, exercise["sets"] + 1):
            chain = _advance_chain_branch(plan, idx, set_no)

            # The chain advances FIRST, then the rest card renders from the
            # already-advanced pointer — so the resolver is asked about the
            # post-advance state, exactly as the runtime asks it.
            if chain == "same_exercise":
                post_index, post_set = idx, set_no + 1
            elif chain == "next_exercise":
                post_index, post_set = idx + 1, 1
            else:
                post_index, post_set = idx, set_no

            status = "completed" if chain == "workout_complete" else "active"
            action = resolve_next_action(plan, post_index, post_set, status)

            if chain == "workout_complete":
                assert action.kind == WORKOUT_COMPLETE, (idx, set_no)
            else:
                # Both non-terminal branches land the user on a real step, and
                # the resolver must name that exact step.
                assert action.kind == SAME_EXERCISE, (idx, set_no)
                assert action.exercise_index == post_index
                assert action.set_number == post_set
                assert action.exercise_name == plan["exercises"][post_index]["name"]


def test_resolver_never_leaks_internal_identifiers() -> None:
    """No exercise ids, indices or session ids in anything the user reads."""
    plan = _plan()
    for index in range(3):
        for set_no in range(1, 6):
            text = render_next_action(resolve_next_action(plan, index, set_no))
            for leaked in ("bench", "row", "curl", "exercise_index", "session_id", "id="):
                assert leaked not in text


def test_rest_after_finishing_an_exercise_announces_a_transition() -> None:
    """A rest entered after the LAST set of an exercise is a TRANSITION.

    Regression for a real defect: the session pointer is advanced by the code
    that saves the set, so by the time rest starts it already reads the FIRST
    set of the NEXT movement. Judging only "is the pointer still inside an
    exercise?" therefore rendered "הבא: סט 1 מתוך 2 — Row" — continuation
    framing for what is actually a switch — and carried the previous
    exercise's weight across as context for a movement it never applied to.

    ``from_exercise_index`` (the exercise the just-completed set belonged to,
    already persisted on ``rest_timers``) disambiguates the two cases without a
    schema change.
    """
    plan = {
        "exercises": [
            {"id": "bench", "name": "Bench", "sets": 2, "rmin": 8, "rmax": 10, "rest": 90},
            {"id": "row", "name": "Row", "sets": 2, "rmin": 10, "rmax": 12, "rest": 90},
        ]
    }

    # Finished bench set 1 -> pointer (0, 2): still the SAME exercise.
    same = resolve_next_action(
        plan, 0, 2, "active", from_exercise_index=0, previous_weight=70.0
    )
    assert same.kind == SAME_EXERCISE
    assert same.exercise_name == "Bench"
    assert same.previous_weight == 70.0

    # Finished bench set 2 (its last) -> pointer (1, 1): a TRANSITION to Row.
    transition = resolve_next_action(
        plan, 1, 1, "active", from_exercise_index=0, previous_weight=70.0
    )
    assert transition.kind == NEXT_EXERCISE
    assert transition.exercise_name == "Row"
    assert transition.set_number == 1
    assert transition.total_sets == 2
    # Bench's load must NOT be echoed under Row.
    assert transition.previous_weight is None

    rendered = render_next_action(transition)
    # Transition framing, not continuation framing. (The header must be the
    # full "התרגיל הבא:" — a bare "הבא:" line would read as another set.)
    assert rendered.splitlines()[0] == "התרגיל הבא:"
    assert "70" not in rendered


def test_missing_origin_index_keeps_the_previous_behaviour() -> None:
    """No ``from_exercise_index`` (e.g. no timer row) must not crash or change
    the pre-existing classification — the guard is additive only."""
    plan = {"exercises": [{"id": "a", "name": "A", "sets": 2, "rmin": 8, "rmax": 10}]}
    action = resolve_next_action(plan, 0, 2, "active")
    assert action.kind == SAME_EXERCISE
    assert action.exercise_name == "A"
