"""The single source of truth for "what happens next in this workout".

TASK-WORKOUT-REST-NEXT-ACTION.

A rest timer that only says "01:24" makes the user carry the plan in their
head. This module answers the one question the rest card must answer — *what
do I do when this ends?* — and it answers it for BOTH consumers:

  1. the rest-timer CARD (``noam_coach.bot.workout_runtime.rest_text``), and
  2. the transition that actually happens when rest reaches zero
     (``rest_timer_tick`` -> ``show_session``).

Both consult ``resolve_next_action`` over the SAME canonical inputs, so the
instruction the user reads and the state the bot moves to cannot diverge. If
this function says "set 3 of 4 — bench press", ``show_session`` renders set 3
of 4 of bench press, because the session pointer this reads is the pointer
``show_session`` reads.

Why a pure function
-------------------
The advance rule lives inline, three times, as raw SQL branch chains
(``workout.save_set``, ``workout_runtime.save_split_set``,
``callback_session`` skip). Copying it a fourth time into the display layer
would guarantee eventual drift. Instead the rule is stated once, here, with no
I/O and no globals: give it a plan and a pointer, get back a description. It
is therefore exhaustively testable without a database, a bot, or a clock.

Canonical inputs, not last-message inputs
-----------------------------------------
The caller passes the CURRENT ``sessions`` row and its plan JSON — never
anything derived from the last rendered message. ``rest_timers`` denormalises
the last set's weight/reps/rir for the card's "what you just did" line; that
data describes the PAST and is deliberately not an input here. Because the
resolver is re-run on each card edit against a freshly read session row, a
state change during rest (an alternative chosen, a set undone, the workout
finished elsewhere) is reflected on the very next edit rather than freezing.

State this must get right
-------------------------
* another set of the CURRENT exercise (``SAME_EXERCISE``);
* a transition to a DIFFERENT exercise (``NEXT_EXERCISE``);
* nothing left to perform (``WORKOUT_COMPLETE``).

Supersets/circuits do not exist in this codebase — ``plan["exercises"]`` is a
flat list — so no grouping is invented here. Exercise ALTERNATIVES need no
special case by construction: ``sub`` rewrites ``plan["exercises"][i]`` in
place, so reading the plan JSON already yields the substituted name. Skipped
exercises and undo likewise need no special case: both move the persisted
pointer, and the pointer is the input.

Copy rules (owner-approved)
---------------------------
* A weight is shown as CONTEXT ("בסט הקודם: 70 ק״ג"), never as a mandatory
  target, and only when the caller actually has one.
* Setup info is echoed only when the plan already carries it. Nothing is
  invented, and no AI is consulted.
* No internal identifiers (exercise ids, indices, session ids) are ever
  rendered.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

# What kind of thing happens after this rest.
SAME_EXERCISE = "same_exercise"
NEXT_EXERCISE = "next_exercise"
WORKOUT_COMPLETE = "workout_complete"

NextActionKind = Literal["same_exercise", "next_exercise", "workout_complete"]

# The completion copy. Rendered when the pointer has nothing left to perform:
# the card must NOT invent a next exercise in that state.
COMPLETION_HEADER = "לאחר המנוחה:"
COMPLETION_LINE = "סיום האימון ומעבר לסיכום."


@dataclass(frozen=True)
class NextAction:
    """What the user should do when the current rest ends.

    ``kind`` drives the copy; the remaining fields are the already-humanised
    pieces of it. ``exercise_index`` is carried for the CALLER's own
    consistency checks (it is what ``show_session`` will land on) and is never
    rendered.
    """

    kind: NextActionKind
    exercise_name: str = ""
    set_number: int = 0
    total_sets: int = 0
    reps_min: int | None = None
    reps_max: int | None = None
    setup_note: str = ""
    previous_weight: float | None = None
    exercise_index: int = -1

    @property
    def is_complete(self) -> bool:
        return self.kind == WORKOUT_COMPLETE

    @property
    def target_line(self) -> str:
        """"יעד: 8–10 חזרות" — omitted entirely when the plan has no range."""
        if self.reps_min is None and self.reps_max is None:
            return ""
        low = self.reps_min if self.reps_min is not None else self.reps_max
        high = self.reps_max if self.reps_max is not None else self.reps_min
        if low == high:
            return f"יעד: {low} חזרות"
        return f"יעד: {low}–{high} חזרות"


def _exercises(plan: Any) -> list[dict[str, Any]]:
    if not isinstance(plan, dict):
        return []
    exercises = plan.get("exercises")
    if not isinstance(exercises, list):
        return []
    return [ex for ex in exercises if isinstance(ex, dict)]


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _setup_note(exercise: dict[str, Any]) -> str:
    """Setup text ONLY if the workout definition already carries it.

    Nothing is generated. If a plan grows an explicit setup/equipment field
    later, it appears here; until then this is empty and the card simply omits
    the line rather than guessing which machine to prepare.
    """
    for key in ("setup", "setup_note", "equipment_note"):
        value = exercise.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def resolve_next_action(
    plan: Any,
    exercise_index: int,
    set_number: int,
    status: str = "active",
    *,
    from_exercise_index: Any = None,
    previous_weight: float | None = None,
) -> NextAction:
    """Describe the step the session will be on once this rest ends.

    Pure: no I/O, no clock, no globals.

    Args:
        plan: the parsed ``sessions.plan`` JSON (a dict with ``exercises``).
            Malformed or empty plans degrade to ``WORKOUT_COMPLETE`` rather
            than raising — a rest card must never crash the workout.
        exercise_index: the session's CURRENT ``exercise_index``. The
            convention matches the persisted pointer exactly: the session has
            ALREADY been advanced by the time rest starts, so this is the step
            that is about to be performed, not the one just finished.
        set_number: the session's CURRENT ``set_number`` (1-based).
        status: the session's ``status``. Anything other than ``"active"``
            means there is nothing further to perform.
        previous_weight: an optional weight to echo as CONTEXT for a further
            set of the same exercise. Passed in (not looked up) to keep this
            pure. Ignored for a different exercise, where a previous load
            would be misleading rather than helpful.

    Returns:
        A :class:`NextAction`. ``kind`` is exactly one of ``SAME_EXERCISE``
        (another set of the exercise the user is on), ``NEXT_EXERCISE`` (a
        transition to a different exercise) or ``WORKOUT_COMPLETE`` (nothing
        left to perform — the caller must show the completion step, never a
        nonexistent exercise).
    """
    if status != "active":
        return NextAction(kind=WORKOUT_COMPLETE)

    exercises = _exercises(plan)
    if not exercises:
        return NextAction(kind=WORKOUT_COMPLETE)

    index = _int(exercise_index, -1)
    if not 0 <= index < len(exercises):
        # The pointer ran past the plan (or is nonsense): there is no next
        # exercise to name, so say so instead of naming the wrong one.
        return NextAction(kind=WORKOUT_COMPLETE)

    current = exercises[index]
    total_sets = max(1, _int(current.get("sets"), 1))
    upcoming_set = max(1, _int(set_number, 1))

    if upcoming_set <= total_sets:
        # Still inside the exercise the pointer names. But the pointer has
        # ALREADY been advanced by the caller that finished the previous set,
        # so "inside this exercise" is not by itself enough to tell a
        # continuation from a transition: after the last set of bench the
        # pointer reads (row, set 1), which is the FIRST set of a DIFFERENT
        # movement. ``from_exercise_index`` — the exercise the just-completed
        # set belonged to — disambiguates. When it differs from the pointer's
        # exercise this rest precedes a transition, and the card must announce
        # the next EXERCISE (and must not carry the previous exercise's weight
        # across, which would read as context for a movement it never applied
        # to).
        origin = _optional_int(from_exercise_index)
        if origin is not None and origin != index:
            return _transition_to(exercises, index, set_number=upcoming_set)
        return NextAction(
            kind=SAME_EXERCISE,
            exercise_name=str(current.get("name") or ""),
            set_number=upcoming_set,
            total_sets=total_sets,
            reps_min=_optional_int(current.get("rmin")),
            reps_max=_optional_int(current.get("rmax")),
            setup_note="",
            previous_weight=previous_weight,
            exercise_index=index,
        )

    # The pointer sits past this exercise's last set (a state the advance
    # chains normally skip over, but a resumed/edited session can present):
    # the real next action is the following exercise, if one exists.
    if index + 1 < len(exercises):
        return _transition_to(exercises, index + 1)

    return NextAction(kind=WORKOUT_COMPLETE)


def _transition_to(
    exercises: list[dict[str, Any]], index: int, *, set_number: int = 1
) -> NextAction:
    nxt = exercises[index]
    return NextAction(
        kind=NEXT_EXERCISE,
        exercise_name=str(nxt.get("name") or ""),
        set_number=max(1, int(set_number or 1)),
        total_sets=max(1, _int(nxt.get("sets"), 1)),
        reps_min=_optional_int(nxt.get("rmin")),
        reps_max=_optional_int(nxt.get("rmax")),
        setup_note=_setup_note(nxt),
        previous_weight=None,
        exercise_index=index,
    )


def resolve_from_session(
    session: Any,
    *,
    previous_weight: float | None = None,
    plan: Any = None,
    from_exercise_index: Any = None,
) -> NextAction:
    """``resolve_next_action`` over a canonical ``sessions`` row.

    Accepts the row (or any mapping) directly so both call sites can pass the
    freshly-read row without each re-deriving the pointer. ``plan`` may be
    supplied pre-parsed; otherwise ``session["plan"]`` is parsed here. A row
    that cannot be read at all yields ``WORKOUT_COMPLETE``, which renders the
    completion step — the safe direction, since it never names an exercise the
    user is not about to perform.
    """
    import json

    if session is None:
        return NextAction(kind=WORKOUT_COMPLETE)

    def _get(key: str, default: Any = None) -> Any:
        try:
            value = session[key]
        except (KeyError, IndexError, TypeError):
            return default
        return default if value is None else value

    if plan is None:
        raw = _get("plan")
        if isinstance(raw, (str, bytes)):
            try:
                plan = json.loads(raw)
            except (ValueError, TypeError):
                plan = None
        else:
            plan = raw

    return resolve_next_action(
        plan,
        _int(_get("exercise_index"), -1),
        _int(_get("set_number"), 1),
        str(_get("status", "active")),
        from_exercise_index=from_exercise_index,
        previous_weight=previous_weight,
    )


def render_next_action(action: NextAction) -> str:
    """The next-action block shown inside the rest card.

    Plain lines, no internal ids, short enough to stay readable on a phone.
    HTML-safe by construction at the call site: the caller escapes the
    plan-supplied names before this text reaches Telegram (see
    ``workout_runtime.rest_text``), which is why this function does no
    escaping of its own — it must stay a pure string builder.
    """
    if action.is_complete:
        return f"{COMPLETION_HEADER}\n{COMPLETION_LINE}"

    lines: list[str] = []
    if action.kind == SAME_EXERCISE:
        lines.append("הבא:")
        lines.append(
            f"סט {action.set_number} מתוך {action.total_sets} — {action.exercise_name}"
        )
    else:
        lines.append("התרגיל הבא:")
        lines.append(
            f"{action.exercise_name} — סט {action.set_number} מתוך {action.total_sets}"
        )

    target = action.target_line
    if target:
        lines.append(target)

    if action.kind == SAME_EXERCISE and action.previous_weight is not None:
        # Context, never a prescription: this is what was lifted, not what the
        # user is being told to lift, and it is never read back as a report.
        lines.append(f"בסט הקודם: {action.previous_weight:g} ק״ג")

    if action.setup_note:
        lines.append(action.setup_note)

    return "\n".join(lines)
