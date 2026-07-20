"""Coaching-memory taxonomy and persistence policy (B10 / ARCH-12).

Six canonical memory classes with an EXPLICIT persistence contract each —
the policy layer that decides what a passing remark is allowed to become:

1. one_turn          — draft/turn-scoped constraints ("בלי טונה עכשיו").
                       NEVER persisted as coaching memory; the policy API
                       refuses them so a caller cannot promote one by
                       accident. Turn-local handling stays where it is.
2. meal_instance     — corrections of one meal/analysis instance. They
                       persist ON the instance (meals/approvals already do)
                       and are never generalized automatically; the policy
                       API only records the traced observation.
3. food_identity     — "קוטג' אצלי זה 250 גרם": after ≥2 CONSISTENT
                       instance corrections the system auto-creates a
                       PROPOSAL; it becomes decision-grade only after one
                       explicit user confirmation.
4. terminology_alias — user words → food identity. Persisted only from an
                       explicit statement or an explicitly confirmed
                       clarification; never inferred silently from one
                       ambiguous use.
5. preference        — delegated to the existing preference service policy
                       (record_food_preference_from_slots).
6. safety_medical    — manual/explicit user input only; never derived from
                       behavioral inference, never silently generalized.

Storage: classes 3-4 live in ``user_facts`` (``food_identity:<key>`` /
``food_alias:<key>``) whose value carries the observation provenance chain
and confirmation state — the fact machinery (kind/source/confirmed) is the
existing confirmation policy, reused rather than duplicated. Every
persistent mutation emits ``state.mutated`` with
``domain=coaching_memory`` + memory class + provenance + confirmation
state, so "why did the coach assume 250g?" is answerable from the trace:
observation events → proposal event → confirmation event → the prompt
block that consumed the confirmed fact.
"""

from __future__ import annotations

import json
from datetime import datetime
from enum import Enum
from typing import Any

from config import TZ

FOOD_IDENTITY_PREFIX = "food_identity:"
FOOD_ALIAS_PREFIX = "food_alias:"

# Observations within this relative tolerance are "consistent".
_CONSISTENT_TOLERANCE = 0.10
_PROPOSAL_MIN_OBSERVATIONS = 2

_ALIAS_ALLOWED_PROVENANCE = ("explicit_statement", "confirmed_clarification")
_SAFETY_ALLOWED_PROVENANCE = ("user_explicit", "manual")


class MemoryClass(str, Enum):
    ONE_TURN = "one_turn"
    MEAL_INSTANCE = "meal_instance"
    FOOD_IDENTITY = "food_identity"
    TERMINOLOGY_ALIAS = "terminology_alias"
    PREFERENCE = "preference"
    SAFETY_MEDICAL = "safety_medical"


class MemoryPolicyViolation(Exception):
    """A write that the persistence contract forbids."""


async def _emit_memory_mutation(
    db: Any,
    user_id: int,
    memory_class: MemoryClass,
    key: str,
    *,
    provenance: str,
    confirmation_state: str,
    outcome: str,
    extra: dict[str, Any] | None = None,
) -> None:
    from noam_coach.observability import taxonomy
    from noam_coach.observability.emit import emit_event

    properties: dict[str, Any] = {
        "domain": "coaching_memory",
        "memory_class": memory_class.value,
        "provenance": provenance,
        "confirmation_state": confirmation_state,
        "key": key,
    }
    if extra:
        properties.update(extra)
    await emit_event(
        db, user_id, taxonomy.STATE_MUTATED,
        entity="coaching_memory", entity_id=key,
        source="coaching_memory", status="mutated", outcome=outcome,
        properties=properties,
    )


# ---------------------------------------------------------------------------
# Canonical write API — policy enforcement per class
# ---------------------------------------------------------------------------


async def record_coaching_memory(
    db: Any,
    user_id: int,
    memory_class: MemoryClass,
    key: str,
    value: Any,
    *,
    provenance: str,
    entity_ref: str | None = None,
) -> None:
    """The single policy gate for persistent coaching-memory writes."""
    if memory_class is MemoryClass.ONE_TURN:
        raise MemoryPolicyViolation(
            "one-turn constraints are draft/turn-scoped and are never "
            "persisted as coaching memory"
        )
    if memory_class is MemoryClass.MEAL_INSTANCE:
        if not entity_ref:
            raise MemoryPolicyViolation(
                "meal-instance corrections persist on the instance — an "
                "entity_ref is required and nothing is generalized"
            )
        await _emit_memory_mutation(
            db, user_id, memory_class, key,
            provenance=provenance, confirmation_state="instance_scoped",
            outcome="recorded", extra={"entity_ref": entity_ref},
        )
        return
    if memory_class is MemoryClass.FOOD_IDENTITY:
        raise MemoryPolicyViolation(
            "food-identity knowledge is created through "
            "record_food_identity_observation / confirm_food_identity — "
            "direct writes would bypass the proposal+confirmation contract"
        )
    if memory_class is MemoryClass.TERMINOLOGY_ALIAS:
        if provenance not in _ALIAS_ALLOWED_PROVENANCE:
            raise MemoryPolicyViolation(
                "terminology aliases persist only from explicit statements "
                "or explicitly confirmed clarifications"
            )
        await _persist_alias(db, user_id, key, value, provenance=provenance)
        return
    if memory_class is MemoryClass.PREFERENCE:
        from noam_coach.services.food_preferences import (
            record_food_preference_from_slots,
        )

        await record_food_preference_from_slots(db, user_id, dict(value))
        await _emit_memory_mutation(
            db, user_id, memory_class, key,
            provenance=provenance, confirmation_state="confirmed",
            outcome="persisted",
        )
        return
    if memory_class is MemoryClass.SAFETY_MEDICAL:
        if provenance not in _SAFETY_ALLOWED_PROVENANCE:
            raise MemoryPolicyViolation(
                "safety/medical memory is manual or explicit user input "
                "only — behavioral inference is never allowed"
            )
        await _emit_memory_mutation(
            db, user_id, memory_class, key,
            provenance=provenance, confirmation_state="confirmed",
            outcome="recorded", extra={"entity_ref": entity_ref},
        )
        return
    raise MemoryPolicyViolation(f"unknown memory class: {memory_class!r}")


async def _persist_alias(
    db: Any, user_id: int, alias: str, target: Any, *, provenance: str
) -> None:
    import user_model
    from noam_coach.services.learned_foods import normalize_food_key

    key = FOOD_ALIAS_PREFIX + normalize_food_key(alias)
    await user_model.set_fact(
        db, user_id, key,
        {"alias": alias, "target": target, "provenance": provenance},
        kind=user_model.KIND_FACT,
        source=user_model.SOURCE_USER,
        confirmed=True,
    )
    await _emit_memory_mutation(
        db, user_id, MemoryClass.TERMINOLOGY_ALIAS, key,
        provenance=provenance, confirmation_state="confirmed",
        outcome="persisted",
    )


# ---------------------------------------------------------------------------
# Food-identity knowledge: observation → proposal → explicit confirmation
# ---------------------------------------------------------------------------


def _fact_key(food_key: str) -> str:
    return FOOD_IDENTITY_PREFIX + food_key


async def record_food_identity_observation(
    db: Any,
    user_id: int,
    food_text: str,
    grams: float,
    *,
    provenance: str,
    entity_ref: str | None = None,
) -> dict[str, Any]:
    """One instance-correction observation. Returns the stored state.

    ≥2 consistent observations auto-create a PROPOSAL (calibration-only);
    decision-grade requires confirm_food_identity (explicit confirmation).
    """
    import user_model
    from noam_coach.services.learned_foods import normalize_food_key

    food_key = normalize_food_key(food_text)
    if not food_key:
        return {}
    key = _fact_key(food_key)
    fact = await user_model.get_fact(db, user_id, key)
    value: dict[str, Any] = {}
    if fact is not None and isinstance(fact.get("value"), dict):
        value = dict(fact["value"])
    elif fact is not None and isinstance(fact.get("value"), str):
        try:
            value = json.loads(fact["value"])
        except (TypeError, ValueError):
            value = {}
    observations = [o for o in value.get("observations") or [] if isinstance(o, dict)]
    observations.append(
        {
            "grams": float(grams),
            "provenance": provenance,
            "entity_ref": entity_ref,
            "at": datetime.now(TZ).isoformat(),
        }
    )
    observations = observations[-10:]
    state = str(value.get("state") or "observed")
    if state != "confirmed":
        consistent = [
            o for o in observations
            if abs(float(o["grams"]) - float(grams)) <= _CONSISTENT_TOLERANCE * float(grams)
        ]
        state = (
            "proposal" if len(consistent) >= _PROPOSAL_MIN_OBSERVATIONS else "observed"
        )
    new_value = {
        "food": food_text,
        "grams": float(grams),
        "state": state,
        "observations": observations,
    }
    import user_model as um

    await um.set_fact(
        db, user_id, key, new_value,
        kind=um.KIND_ESTIMATE if state != "confirmed" else um.KIND_FACT,
        source=um.SOURCE_USER,
        confirmed=state == "confirmed",
    )
    await _emit_memory_mutation(
        db, user_id, MemoryClass.FOOD_IDENTITY, key,
        provenance=provenance,
        confirmation_state=state,
        outcome="proposal_created" if state == "proposal" else "observation_recorded",
        extra={"grams": float(grams), "observations": len(observations)},
    )
    return new_value


async def confirm_food_identity(
    db: Any,
    user_id: int,
    food_text: str,
    *,
    grams: float | None = None,
    provenance: str = "explicit_confirmation",
) -> dict[str, Any] | None:
    """Explicit user confirmation — the ONLY path to decision-grade."""
    import user_model
    from noam_coach.services.learned_foods import normalize_food_key

    food_key = normalize_food_key(food_text)
    key = _fact_key(food_key)
    fact = await user_model.get_fact(db, user_id, key)
    value: dict[str, Any] = {}
    if fact is not None and isinstance(fact.get("value"), dict):
        value = dict(fact["value"])
    if grams is None:
        grams = value.get("grams")
    if grams is None:
        return None
    observations = value.get("observations") or []
    new_value = {
        "food": value.get("food") or food_text,
        "grams": float(grams),
        "state": "confirmed",
        "observations": observations,
        "confirmed_at": datetime.now(TZ).isoformat(),
    }
    await user_model.set_fact(
        db, user_id, key, new_value,
        kind=user_model.KIND_FACT,
        source=user_model.SOURCE_USER,
        confirmed=True,
    )
    await _emit_memory_mutation(
        db, user_id, MemoryClass.FOOD_IDENTITY, key,
        provenance=provenance, confirmation_state="confirmed",
        outcome="confirmed",
        extra={"grams": float(grams), "observations": len(observations)},
    )
    return new_value


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def coaching_memory_snapshot(db: Any, user_id: int) -> dict[str, Any]:
    """Bounded decision-grade snapshot for AssistantTurnContext.coaching_memory."""
    rows = await db.fetch_all(
        "SELECT key, value, confirmed FROM user_facts "
        "WHERE user_id=? AND (key LIKE ? OR key LIKE ?) AND COALESCE(valid, 1)=1",
        (user_id, FOOD_IDENTITY_PREFIX + "%", FOOD_ALIAS_PREFIX + "%"),
    )
    identities: list[dict[str, Any]] = []
    aliases: list[dict[str, Any]] = []
    for row in rows:
        try:
            value = json.loads(row["value"]) if isinstance(row["value"], str) else row["value"]
        except (TypeError, ValueError):
            continue
        if not isinstance(value, dict):
            continue
        if str(row["key"]).startswith(FOOD_IDENTITY_PREFIX):
            identities.append(
                {
                    "food": value.get("food"),
                    "grams": value.get("grams"),
                    "state": value.get("state"),
                }
            )
        else:
            aliases.append({"alias": value.get("alias"), "target": value.get("target")})
    return {
        "food_identities": identities[:10],
        "aliases": aliases[:10],
    }


async def food_identity_prompt_lines(db: Any, user_id: int) -> list[str]:
    """Prompt lines for meal analysis / recommendation consumers.

    Confirmed identities are decision-grade instructions; proposals are
    calibration-only and explicitly marked as unconfirmed.
    """
    snapshot = await coaching_memory_snapshot(db, user_id)
    lines: list[str] = []
    for item in snapshot["food_identities"]:
        food, grams, state = item.get("food"), item.get("grams"), item.get("state")
        if not food or not grams:
            continue
        if state == "confirmed":
            lines.append(
                f"- {food}: the user CONFIRMED their usual portion is {grams:g}g — "
                "use it as the default quantity when the item appears without an "
                "explicit amount (user-confirmed knowledge)."
            )
        elif state == "proposal":
            lines.append(
                f"- {food}: repeated corrections suggest ~{grams:g}g is typical, "
                "but this is UNCONFIRMED — calibration hint only, never present "
                "it as established knowledge."
            )
    for alias in snapshot["aliases"]:
        if alias.get("alias") and alias.get("target"):
            lines.append(
                f"- When the user says '{alias['alias']}' they mean: {alias['target']} "
                "(explicitly established terminology)."
            )
    return lines


# ---------------------------------------------------------------------------
# Capture install: meal-correction quantities → food-identity observations
# ---------------------------------------------------------------------------

# Deictics/verbs the greedy quantity parser can capture as "food text".
_NON_FOOD_WORDS = frozenset(
    {"זה", "זאת", "היה", "הייתה", "היו", "הכל", "שוב", "עוד", "גם", "רק", "בערך"}
)

_original_reanalyze: Any = None


def install_coaching_memory_capture() -> None:
    """Wrap the meal re-analysis boundary: every EXPLICIT gram correction the
    user sends for a meal instance is also a food-identity observation
    (class 2 stays on the instance; class 3 accumulates toward a proposal).
    When an observation upgrades to a PROPOSAL, the returned analysis gains
    a note offering the explicit confirmation phrase — the only path to
    decision-grade knowledge."""
    global _original_reanalyze
    if _original_reanalyze is not None:
        return
    import coach_bot

    _original_reanalyze = coach_bot.reanalyze_meal_with_text_and_image
    original = _original_reanalyze

    async def capturing_reanalyze(
        image_path: str,
        correction_text: str,
        locked_corrections: Any = None,
        nutrition_context: Any = None,
    ) -> Any:
        analysis = await original(
            image_path, correction_text, locked_corrections, nutrition_context
        )
        try:
            import coach_bot as facade
            import meal_intelligence

            user_id = None
            if isinstance(nutrition_context, dict):
                try:
                    user_id = int(nutrition_context.get("user_id") or 0) or None
                except (TypeError, ValueError):
                    user_id = None
            if user_id:
                from noam_coach.services.learned_foods import _is_useful_name

                for locked in meal_intelligence.parse_locked_quantities(correction_text):
                    # The quantity parser is deliberately greedy for locking
                    # purposes; only meaningful FOOD names become memory
                    # observations (never deictics like "זה").
                    food = locked.food_text.strip()
                    if (
                        len(food) < 3
                        or food in _NON_FOOD_WORDS
                        or not _is_useful_name(food)
                    ):
                        continue
                    state = await record_food_identity_observation(
                        facade.DB, user_id, locked.food_text, float(locked.grams),
                        provenance="meal_instance_correction",
                    )
                    if state.get("state") == "proposal":
                        analysis.notes.append(
                            f"💡 שמתי לב שכבר תיקנת את {locked.food_text} "
                            f"ל~{float(locked.grams):g} גרם יותר מפעם אחת. "
                            f"כדי שאזכור את זה קבוע, כתוב: "
                            f"\"קבע {locked.food_text} {float(locked.grams):g} גרם\""
                        )
        except Exception:  # noqa: BLE001 — capture must never break analysis
            pass
        return analysis

    coach_bot.reanalyze_meal_with_text_and_image = capturing_reanalyze


def uninstall_coaching_memory_capture() -> None:
    global _original_reanalyze
    if _original_reanalyze is not None:
        import coach_bot

        coach_bot.reanalyze_meal_with_text_and_image = _original_reanalyze
        _original_reanalyze = None
