"""Every model used as an OpenAI ``text_format`` must be strict-mode legal.

The Responses API runs structured outputs in strict mode: every object node
needs ``additionalProperties: false`` with all properties listed in
``required``. A ``dict[str, Any]`` field cannot express that, so the request
is rejected with BadRequestError before any tokens are generated.

That is not hypothetical. ``Intent.slots`` was declared as a free-form dict,
and intent classification therefore failed 100% of the time -- 9 of 9 calls
in the 2026-07-26 live session -- while every other AI purpose succeeded.
Because the failure happens at request time, the bot silently degraded to
``keyword_fallback`` and looked like it was working.

These tests fail the moment someone reintroduces a free-form mapping.
"""

from __future__ import annotations

import pytest
from openai.lib._pydantic import to_strict_json_schema
from pydantic import BaseModel

import assistant
import models
import recommendations

# Every model passed as ``text_format=`` to client.responses.parse().
STRUCTURED_OUTPUT_MODELS: tuple[tuple[str, type[BaseModel]], ...] = (
    ("Intent", assistant.Intent),
    ("MealAnalysis", models.MealAnalysis),
    ("RoutineExtraction", models.RoutineExtraction),
    ("MorningMenu", recommendations.MorningMenu),
    ("NextMealSuggestion", recommendations.NextMealSuggestion),
    ("EveningSummary", recommendations.EveningSummary),
)


def _object_nodes(schema: dict) -> list[tuple[str, dict]]:
    """Every object node in a schema, including nested ``$defs``."""
    nodes: list[tuple[str, dict]] = []

    def walk(name: str, node: object) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                nodes.append((name, node))
            for key, value in node.items():
                if key == "$defs" and isinstance(value, dict):
                    for def_name, def_node in value.items():
                        walk(def_name, def_node)
                else:
                    walk(name, value)
        elif isinstance(node, list):
            for item in node:
                walk(name, item)

    walk("root", schema)
    return nodes


@pytest.mark.parametrize(
    ("name", "model"),
    STRUCTURED_OUTPUT_MODELS,
    ids=[name for name, _ in STRUCTURED_OUTPUT_MODELS],
)
def test_model_converts_to_a_strict_schema(name: str, model: type[BaseModel]) -> None:
    """The SDK's own strict conversion must not raise."""
    to_strict_json_schema(model)


@pytest.mark.parametrize(
    ("name", "model"),
    STRUCTURED_OUTPUT_MODELS,
    ids=[name for name, _ in STRUCTURED_OUTPUT_MODELS],
)
def test_every_object_node_is_closed(name: str, model: type[BaseModel]) -> None:
    schema = to_strict_json_schema(model)
    nodes = _object_nodes(schema)
    assert nodes, f"{name}: no object nodes found -- the walker is broken"

    for node_name, node in nodes:
        assert node.get("additionalProperties") is False, (
            f"{name}/{node_name}: additionalProperties must be false"
        )
        assert set(node.get("properties", {})) == set(node.get("required", [])), (
            f"{name}/{node_name}: strict mode requires every property in 'required'"
        )


def test_intent_slots_is_a_closed_model_not_a_mapping() -> None:
    """The specific regression: slots must never be a free-form dict again."""
    slots_field = assistant.Intent.model_fields["slots"]
    assert issubclass(slots_field.annotation, BaseModel), (
        "Intent.slots must stay a closed Pydantic model -- a dict[str, Any] "
        "makes the whole classification request invalid under strict mode"
    )


def test_unknown_slot_is_rejected() -> None:
    """extra='forbid' is what produces additionalProperties: false."""
    with pytest.raises(Exception):
        assistant.Slots(not_a_real_slot="x")


def test_slots_covers_every_key_the_prompt_asks_for() -> None:
    """A slot the prompt requests but the model omits can never be returned."""
    declared = set(assistant.Slots.model_fields)
    # Named explicitly in SYSTEM_PROMPT's slot instructions.
    prompt_slots = {
        "frequency",
        "goal_weight",
        "calories",
        "kind",
        "item",
        "polarity",
        "flag",
        "note",
        "location",
        "weight_kg",
        "height_cm",
        "body_fat_pct",
    }
    missing = prompt_slots - declared
    assert not missing, f"prompt asks for slots the model cannot return: {missing}"


def test_keyword_fallback_slots_are_all_declared() -> None:
    """Every slot the deterministic fallback sets must exist on the model.

    A dict with an undeclared key now raises at construction, so an
    unnoticed drift here would crash the fallback path -- the one path that
    keeps the bot responsive when the AI is unavailable.
    """
    probes = (
        "לקחתי ריטלין",
        "היום צום",
        "המכשיר תפוס",
        "הכאב עבר",
        "כואבת לי הברך",
        "תבנה לי 4 אימונים",
        "המשקל שלי 89",
        "הגובה שלי 174",
        "יעד 2100",
        "אני רוצה לרדת ל-85",
        "אני לא שותה אלכוהול",
        "אני צמחוני",
        "אלרגי לבוטנים",
        "אכלתי 2 ביצים",
        "שלום",
    )
    for probe in probes:
        intent = assistant.keyword_fallback(probe)
        assert isinstance(intent.slots, assistant.Slots)


def test_slots_flatten_to_a_plain_dict_for_consumers() -> None:
    """Consumers read ctx.slots with .get(); unset slots must read as absent."""
    intent = assistant.keyword_fallback("יעד 2100")
    flattened = intent.slots.model_dump(exclude_none=True)

    assert flattened == {"calories": 2100}
    assert flattened.get("weight_kg") is None


def test_slots_supports_mapping_reads() -> None:
    """Slots replaced a dict, so dict-style reads must keep working."""
    slots = assistant.Slots(calories=2100)

    assert slots.get("calories") == 2100
    assert slots.get("weight_kg") is None
    assert slots.get("weight_kg", 0) == 0
    assert slots["calories"] == 2100
    assert "calories" in slots
    assert "weight_kg" not in slots

    with pytest.raises(KeyError):
        slots["weight_kg"]
    with pytest.raises(KeyError):
        slots["not_a_slot"]


def test_mapping_helpers_do_not_leak_into_the_schema() -> None:
    """get/__getitem__ are behaviour, not fields -- they must not be emitted."""
    schema = to_strict_json_schema(assistant.Slots)

    assert "get" not in schema.get("properties", {})
    assert set(schema["properties"]) == set(assistant.Slots.model_fields)
