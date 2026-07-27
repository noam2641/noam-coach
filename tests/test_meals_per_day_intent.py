"""The intent taxonomy must have a label for "N meals a day".

The defect this covers, from the live event stream: at 06:59:45 the user typed
"6 ארוחות ביום" -- 6 MEALS a day. The classifier answered
``build_plan(frequency=6)`` at 0.95 confidence and the weekly TRAINING plan was
rebuilt to 6 workouts.

The root cause was taxonomic, not a bad guess: the ``Action`` Literal had no
entry meaning "how many meals per day". ``set_dietary_pref`` is about WHAT the
user eats, not how often. With no correct label available the model picked the
closest numeric-sounding one, and the system prompt pushed a bare number toward
``build_plan.frequency``. A confirmation gate contains the damage but does not
prevent the misclassification.

These tests pin both directions: a meal-count statement reaches the new
``set_meal_frequency`` action and the existing ``preferred_meal_count`` writer,
while a workout-count statement still reaches ``build_plan`` untouched.
"""

from __future__ import annotations

import inspect

import pytest
from openai.lib._pydantic import to_strict_json_schema

import assistant

# The exact message from the incident.
INCIDENT_TEXT = "6 ארוחות ביום"
# The message that must keep building a training plan.
TRAINING_TEXT = "4 אימונים בשבוע"


# ---------------------------------------------------------------------------
# The taxonomy itself
# ---------------------------------------------------------------------------


def test_action_literal_has_a_meals_per_day_label() -> None:
    """Without this entry the model has no correct label to choose."""
    assert "set_meal_frequency" in assistant.Action.__args__


def test_slots_declares_the_meal_count_fields() -> None:
    """Slots is extra='forbid' -- an undeclared slot cannot be returned."""
    declared = set(assistant.Slots.model_fields)
    assert "meals_per_day" in declared
    assert "meals_per_day_max" in declared


def test_meal_count_slot_is_not_the_workout_frequency_slot() -> None:
    """Sharing 'frequency' is exactly how 6 meals became 6 workouts."""
    slots = assistant.Slots(meals_per_day=6)
    assert slots.get("meals_per_day") == 6
    assert slots.get("frequency") is None


def test_system_prompt_teaches_the_action_and_separates_it_from_build_plan() -> None:
    prompt = assistant.SYSTEM_PROMPT
    assert "set_meal_frequency" in prompt
    assert "meals_per_day" in prompt
    # The prompt must state the distinction, not merely list both actions.
    assert "ארוחות" in prompt
    build_plan_at = prompt.index("- build_plan:")
    build_plan_block = prompt[build_plan_at : prompt.index("\n- ", build_plan_at + 1)]
    assert "set_meal_frequency" in build_plan_block, (
        "build_plan's guidance must redirect meal counts, or a bare number "
        "keeps landing on frequency"
    )


# ---------------------------------------------------------------------------
# Classification -- the deterministic path, which runs whenever AI is down
# ---------------------------------------------------------------------------


def test_incident_message_classifies_to_the_meal_action() -> None:
    intent = assistant.keyword_fallback(INCIDENT_TEXT)

    assert intent.action == "set_meal_frequency"
    assert intent.slots.get("meals_per_day") == 6
    # The regression: the count must never reach the training-plan slot.
    assert intent.slots.get("frequency") is None


def test_incident_message_is_not_build_plan() -> None:
    """The single assertion that would have caught the live defect."""
    assert assistant.keyword_fallback(INCIDENT_TEXT).action != "build_plan"


@pytest.mark.parametrize(
    ("text", "low", "high"),
    [
        ("6 ארוחות ביום", 6, None),
        ("אני אוכל 6 ארוחות ביום", 6, None),
        ("6 ארוחות", 6, None),
        ("אני אוכל 5-6 ארוחות ביום", 5, 6),
        ("תחלק לי ל-5 ארוחות", 5, None),
        ("אני אוכל 5 פעמים ביום", 5, None),
    ],
)
def test_meal_count_statements_and_their_extracted_band(
    text: str, low: int, high: int | None
) -> None:
    intent = assistant.keyword_fallback(text)

    assert intent.action == "set_meal_frequency"
    assert intent.slots.get("meals_per_day") == low
    assert intent.slots.get("meals_per_day_max") == high


def test_build_plan_still_works_without_a_model() -> None:
    """The constraint: a workout-count statement is untouched."""
    intent = assistant.keyword_fallback(TRAINING_TEXT)

    assert intent.action == "build_plan"
    assert intent.slots.get("frequency") == 4
    assert intent.slots.get("meals_per_day") is None


@pytest.mark.parametrize(
    "text",
    ["4 אימונים בשבוע", "תבנה לי תוכנית 4 אימונים בשבוע", "תבנה לי תוכנית", "ספליט"],
)
def test_training_messages_never_become_meal_frequency(text: str) -> None:
    assert assistant.keyword_fallback(text).action == "build_plan"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Questions ABOUT logged meals stay meal_status -- they are not a
        # statement of how many meals the user eats.
        ("כמה ארוחות שמרתי?", "meal_status"),
        ("מה עם הארוחות?", "meal_status"),
        # A calorie target still wins its own branch.
        ("יעד 2100", "set_calorie_goal"),
        ("אני רוצה לאכול 2100 ביום", "set_calorie_goal"),
        # Eating reports and standing preferences are unaffected.
        ("אכלתי 2 ביצים", "log_meal_text"),
        ("אני צמחוני", "set_dietary_pref"),
        ("המשקל שלי 89", "update_measurement"),
    ],
)
def test_neighbouring_intents_are_not_captured(text: str, expected: str) -> None:
    """The new branch must not steal messages from adjacent handlers."""
    assert assistant.keyword_fallback(text).action == expected


def test_implausible_meal_counts_are_left_alone() -> None:
    """12 meals a day is not a meal-count statement worth persisting."""
    assert assistant.keyword_fallback("12 ארוחות ביום").action != "set_meal_frequency"


def test_a_bare_number_is_not_read_as_a_meal_count() -> None:
    """A unit word is required -- a bare number stays ambiguous."""
    assert assistant.keyword_fallback("6").action != "set_meal_frequency"


async def test_classify_intent_uses_the_fallback_when_no_client() -> None:
    """classify_intent(None, ...) is the AI-unavailable path end to end."""
    intent = await assistant.classify_intent(None, "gpt-x", INCIDENT_TEXT)
    assert intent.action == "set_meal_frequency"
    assert intent.slots.get("meals_per_day") == 6

    training = await assistant.classify_intent(None, "gpt-x", TRAINING_TEXT)
    assert training.action == "build_plan"
    assert training.slots.get("frequency") == 4


# ---------------------------------------------------------------------------
# Strict structured outputs -- a new slot must not break the schema
# ---------------------------------------------------------------------------


def test_intent_still_converts_to_a_strict_schema() -> None:
    schema = to_strict_json_schema(assistant.Intent)
    slots = schema["$defs"]["Slots"]

    assert slots["additionalProperties"] is False
    assert set(slots["properties"]) == set(slots["required"])
    assert "meals_per_day" in slots["properties"]
    assert "meals_per_day_max" in slots["properties"]


# ---------------------------------------------------------------------------
# The handler -- must reach the EXISTING writer, not a second storage path
# ---------------------------------------------------------------------------


class _Ctx:
    """Minimal stand-in for FreeTextContext (the handler only uses these)."""

    def __init__(self, text: str, slots: dict, user_id: int = 7) -> None:
        self.text = text
        self.slots = slots
        self.user_id = user_id
        self.action = "set_meal_frequency"
        self.follow = None
        self.sent: list[str] = []

    async def send(self, body: str, kb: object = None) -> None:
        self.sent.append(body)


async def test_handler_persists_through_the_existing_writer(monkeypatch) -> None:
    from noam_coach.services import day_plan

    calls: list[tuple] = []

    async def fake_persist(db, user_id, *, minimum, maximum=None):
        calls.append((user_id, minimum, maximum))
        return True

    monkeypatch.setattr(day_plan, "persist_preferred_meal_count", fake_persist)

    from noam_coach.bot.assistant import _handle_meal_frequency

    ctx = _Ctx(INCIDENT_TEXT, {"meals_per_day": 6})
    await _handle_meal_frequency(ctx)

    assert calls == [(7, 6, None)]
    assert ctx.sent, "the user must be told what was recorded"
    assert "6" in ctx.sent[0]


async def test_handler_persists_a_stated_range(monkeypatch) -> None:
    from noam_coach.services import day_plan

    calls: list[tuple] = []

    async def fake_persist(db, user_id, *, minimum, maximum=None):
        calls.append((user_id, minimum, maximum))
        return True

    monkeypatch.setattr(day_plan, "persist_preferred_meal_count", fake_persist)

    from noam_coach.bot.assistant import _handle_meal_frequency

    ctx = _Ctx("אני אוכל 5-6 ארוחות ביום", {"meals_per_day": 5, "meals_per_day_max": 6})
    await _handle_meal_frequency(ctx)

    assert calls == [(7, 5, 6)]


async def test_handler_asks_again_instead_of_persisting_nonsense(monkeypatch) -> None:
    from noam_coach.services import day_plan

    calls: list[tuple] = []

    async def fake_persist(db, user_id, *, minimum, maximum=None):
        calls.append((user_id, minimum, maximum))
        return True

    monkeypatch.setattr(day_plan, "persist_preferred_meal_count", fake_persist)

    from noam_coach.bot.assistant import _handle_meal_frequency

    ctx = _Ctx("ארוחות", {})
    await _handle_meal_frequency(ctx)

    assert calls == [], "a missing count must never be written"
    assert ctx.sent


def test_handler_uses_the_canonical_writer_and_adds_no_second_path() -> None:
    """No new storage path: the handler calls the one existing writer."""
    from noam_coach.bot.assistant import _handle_meal_frequency

    source = inspect.getsource(_handle_meal_frequency)
    assert "persist_preferred_meal_count" in source
    # It must not write the fact itself. Checking for set_fact is the real
    # assertion; the fact KEY may legitimately appear in the docstring
    # explaining where the value lands, so only executable lines are scanned.
    code = "\n".join(
        line
        for line in source.splitlines()
        if not line.strip().startswith(("#", '"', "'"))
    )
    assert "set_fact" not in code
    assert "user_model" not in code


def test_the_goal_handler_routes_the_new_action() -> None:
    from noam_coach.bot.assistant import _handle_goal_text_action

    source = inspect.getsource(_handle_goal_text_action)
    assert "set_meal_frequency" in source
