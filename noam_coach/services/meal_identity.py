"""Deterministic meal item-identity enforcement + uncertainty gate (TASK-58).

Production incident: the user corrected "לא טחינה חציל במיונז", yet the
re-analysis restored raw tahini. Three independent failures converged:

1. The negation-first correction form was not parsed deterministically, so an
   item-scoped identity correction fell through to whole-meal AI reanalysis
   (fixed in meal_intelligence: negation-first replacement patterns).
2. Nothing enforced the identity after the AI + deterministic normalization —
   prompt compliance was the only guard (fixed HERE: post-AI enforcement).
3. The Israeli-food override canonicalized the generic word "טחינה" back to
   raw tahini paste macros (fixed in israeli_foods: evidence-aware lookup).

This module is the deterministic mirror of "the user's text is authoritative":

- ``install_meal_identity_enforcement`` wraps the facade's
  ``reanalyze_meal_with_text_and_image``. Identity constraints are parsed
  from the CURRENT correction text plus every LOCKED prior correction (the
  approval row's ``locked_corrections`` — the meal-instance memory store; no
  second store is created, per the B10 taxonomy an identity correction is a
  class-2 meal_instance memory persisted on the instance). After the AI +
  Israeli-food normalization produce their answer, every item still carrying
  a rejected identity is deterministically renamed to the confirmed identity
  (item-scoped: grams preserved, curated macros applied) or dropped when the
  confirmed food already exists. A rejected identity therefore cannot return
  for the meal's lifecycle unless the user explicitly reverses the
  correction.
- The same install wraps ``analyze_meal_image`` with the HIGH-IMPACT
  UNCERTAINTY GATE (TASK-58 H): when the model did not ask its one allowed
  clarification and a low-confidence item alone carries a large share of the
  meal's calories, a targeted question about THAT item is attached
  deterministically — the system asks instead of silently trusting an
  uncertain identity that dominates the estimate.

Trace chain (canonical product events):
  ai original interpretation (O-series meal traces)
  → state.mutated(entity=meal_identity_constraint)      — the user's lock
  → reanalysis output (existing meal_reanalysis trace)
  → decision.finalized(entity=identity_enforcement)     — deterministic step
  → final meal decision (existing approval flow events)
"""

from __future__ import annotations

from typing import Any

from meal_intelligence import (
    enforce_identity_constraints,
    identity_constraints_from_texts,
)

# H-gate thresholds (documented, tested): an item is "high-impact uncertain"
# when the analyzer itself was not confident about it AND it alone accounts
# for a large share of the meal — exactly the shape of the tahini incident
# (298/738 kcal ≈ 40% riding on a wrong visual identity). A plausible
# identity swap on such an item shifts the meal total far beyond the 15%
# materiality bar the prompt already uses.
_UNCERTAIN_CONFIDENCE_MAX = 0.6
_HIGH_IMPACT_SHARE = 0.30
_MIN_MEAL_CALORIES = 250.0


def high_impact_uncertainty_question(analysis: Any) -> str | None:
    """A targeted clarification for the dominant uncertain item, or None."""
    items = list(getattr(analysis, "items", []) or [])
    if not items or getattr(analysis, "question", None):
        return None
    total = sum(float(item.calories or 0) for item in items)
    if total < _MIN_MEAL_CALORIES:
        return None
    candidates = [
        item
        for item in items
        if float(item.confidence or 0) <= _UNCERTAIN_CONFIDENCE_MAX
        and total > 0
        and float(item.calories or 0) / total >= _HIGH_IMPACT_SHARE
    ]
    if not candidates:
        return None
    item = max(candidates, key=lambda candidate: float(candidate.calories or 0))
    share = round(100 * float(item.calories or 0) / total)
    return (
        f"רק מוודא לפני שנקבע: זיהיתי {item.name} (כ-{item.calories:.0f} קל׳, "
        f"בערך {share}% מהארוחה) אבל בביטחון נמוך. זה נכון, או שזה מאכל אחר? "
        "אם זה משהו אחר — כתוב מה זה."
    )


async def _emit(db: Any, user_id: Any, event: str, **kwargs: Any) -> None:
    if not user_id:
        return
    try:
        from noam_coach.observability import taxonomy
        from noam_coach.observability.emit import emit_event

        await emit_event(db, int(user_id), getattr(taxonomy, event), **kwargs)
    except Exception:  # noqa: BLE001 — tracing must never break analysis
        pass


def _user_id_from_context(nutrition_context: Any) -> int | None:
    if isinstance(nutrition_context, dict):
        try:
            return int(nutrition_context.get("user_id") or 0) or None
        except (TypeError, ValueError):
            return None
    return None


_original_reanalyze: Any = None
_original_analyze_image: Any = None


def install_meal_identity_enforcement() -> None:
    """Idempotent install of both wraps on the coach_bot facade."""
    global _original_reanalyze, _original_analyze_image
    if _original_reanalyze is not None:
        return
    import coach_bot

    _original_reanalyze = coach_bot.reanalyze_meal_with_text_and_image
    original_reanalyze = _original_reanalyze

    async def identity_enforced_reanalyze(
        image_path: str,
        correction_text: str,
        locked_corrections: Any = None,
        nutrition_context: Any = None,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        import coach_bot as facade

        texts = [correction_text, *(locked_corrections or [])]
        constraints = identity_constraints_from_texts(texts)
        db = facade.DB
        user_id = _user_id_from_context(nutrition_context)
        current_constraints = identity_constraints_from_texts([correction_text])
        for constraint in current_constraints:
            await _emit(
                db, user_id, "STATE_MUTATED",
                entity="meal_identity_constraint",
                entity_id=constraint.rejected,
                source="meal_identity", status="mutated", outcome="locked",
                properties={
                    "domain": "coaching_memory",
                    "memory_class": "meal_instance",
                    "provenance": "explicit_statement",
                    "confirmation_state": "confirmed",
                    "rejected": constraint.rejected,
                    "confirmed": constraint.confirmed,
                },
            )
        analysis = await original_reanalyze(
            image_path, correction_text, locked_corrections,
            nutrition_context, *args, **kwargs,
        )
        analysis, enforced = enforce_identity_constraints(analysis, constraints)
        await _emit(
            db, user_id, "DECISION_FINALIZED",
            entity="identity_enforcement",
            source="meal_identity", status="finalized",
            outcome="enforced" if enforced else "clean",
            properties={
                "constraints": [
                    {"rejected": c.rejected, "confirmed": c.confirmed}
                    for c in constraints
                ],
                "actions": enforced,
            },
        )
        return analysis

    coach_bot.reanalyze_meal_with_text_and_image = identity_enforced_reanalyze

    _original_analyze_image = coach_bot.analyze_meal_image
    original_analyze = _original_analyze_image

    async def uncertainty_gated_analyze_image(
        image_bytes: bytes,
        user_id: Any = None,
        nutrition_context: Any = None,
        caption: Any = None,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        import coach_bot as facade

        analysis = await original_analyze(
            image_bytes, user_id, nutrition_context, caption, *args, **kwargs
        )
        question = high_impact_uncertainty_question(analysis)
        if question:
            analysis.question = question
            await _emit(
                facade.DB, user_id or _user_id_from_context(nutrition_context),
                "DECISION_FINALIZED",
                entity="meal_uncertainty_gate",
                source="meal_identity", status="finalized",
                outcome="clarification_required",
                properties={"question": question[:200]},
            )
        return analysis

    coach_bot.analyze_meal_image = uncertainty_gated_analyze_image


def uninstall_meal_identity_enforcement() -> None:
    global _original_reanalyze, _original_analyze_image
    import coach_bot

    if _original_reanalyze is not None:
        coach_bot.reanalyze_meal_with_text_and_image = _original_reanalyze
        _original_reanalyze = None
    if _original_analyze_image is not None:
        coach_bot.analyze_meal_image = _original_analyze_image
        _original_analyze_image = None
