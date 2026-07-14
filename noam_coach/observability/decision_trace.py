"""Coaching AI decision chains (Observability O6).

Attribute wraps (module + facade — no source edits) that make the
AI-output → deterministic-decision relationships explicit:

- intent classification: decision.finalized carries BOTH the raw AI intent
  and the final selected intent, so a keyword-fallback override or a
  low-confidence preference for the keyword guess stays visible.
- evening summary: context.built records the DETERMINISTICALLY computed
  food flags (compute_food_flags — hard signals) separately from the AI
  prose (which lives in ai.call.completed); decision.finalized records
  whether the final summary came from the AI or the deterministic fallback.
- routine extraction: a failure or missing client silently yields an empty
  RoutineExtraction by product design — decision.fallback_selected makes
  that emptiness visible instead of indistinguishable from "the user said
  nothing".

The morning-menu generate → validate → repair → fallback chain is
instrumented directly inside ``noam_coach/services/morning_menu_pipeline.py``
(an unprotected orchestration module that already owns those decisions).
"""

from __future__ import annotations

import functools
from typing import Any

from noam_coach.observability import taxonomy
from noam_coach.observability.ai_invocation import capture_ai_outputs
from noam_coach.observability.emit import emit_event
from noam_coach.observability.obs_context import current_user_id

_installed: dict[str, list] = {"wraps": []}


def _resolve_db_user() -> tuple[Any, int] | None:
    import coach_bot

    user_id = current_user_id()
    if user_id is None:
        try:
            from config import SETTINGS

            user_id = getattr(SETTINGS, "telegram_allowed_user_id", None)
        except Exception:  # noqa: BLE001
            user_id = None
    if user_id is None:
        return None
    return coach_bot.DB, int(user_id)


def _wrap(module: Any, name: str, factory: Any) -> None:
    import coach_bot

    original = getattr(module, name, None)
    if original is None or getattr(original, "__obs_decision_trace__", False):
        return
    wrapped = factory(original)
    wrapped.__obs_decision_trace__ = True
    setattr(module, name, wrapped)
    _installed["wraps"].append((module, name, original))
    if getattr(coach_bot, name, None) is original:
        setattr(coach_bot, name, wrapped)
        _installed["wraps"].append((coach_bot, name, original))


def _classify_intent_wrapper(original: Any) -> Any:
    @functools.wraps(original)
    async def wrapper(client: Any, model: str, text: str, profile_summary: str = "") -> Any:
        resolved = _resolve_db_user()
        if resolved is None:
            return await original(client, model, text, profile_summary)
        db, user_id = resolved
        with capture_ai_outputs() as raw_outputs:
            intent = await original(client, model, text, profile_summary)
        ai_intent = raw_outputs[-1] if raw_outputs else None
        if ai_intent is None:
            resolution = "keyword_fallback_no_ai_result"
        elif ai_intent.get("action") == getattr(intent, "action", None):
            resolution = "ai"
        else:
            resolution = "fallback_overrode_ai"
        await emit_event(
            db, user_id, taxonomy.DECISION_FINALIZED,
            entity="intent",
            source="intent_router", status="finalized",
            outcome=getattr(intent, "action", None),
            properties={
                "stage": "intent_classification",
                "resolution": resolution,
                "ai_action": (ai_intent or {}).get("action"),
                "ai_confidence": (ai_intent or {}).get("confidence"),
                "final_action": getattr(intent, "action", None),
                "final_confidence": getattr(intent, "confidence", None),
            },
        )
        return intent

    return wrapper


def _evening_summary_wrapper(original: Any) -> Any:
    @functools.wraps(original)
    async def wrapper(client: Any, model: str, *args: Any, **kwargs: Any) -> Any:
        resolved = _resolve_db_user()
        if resolved is None:
            return await original(client, model, *args, **kwargs)
        db, user_id = resolved
        # The deterministic hard signals, recorded as such (same pure
        # function, same input the summary itself uses).
        import recommendations

        meals = kwargs.get("meals", args[4] if len(args) >= 5 else [])
        try:
            computed_flags = recommendations.compute_food_flags(list(meals or []))
        except Exception:  # noqa: BLE001
            computed_flags = []
        await emit_event(
            db, user_id, taxonomy.CONTEXT_BUILT,
            entity="food_flags",
            source="evening_summary", status="built",
            properties={"deterministic": True, "flag_count": len(computed_flags)},
            content={"computed_food_flags": computed_flags},
        )
        with capture_ai_outputs() as raw_outputs:
            summary = await original(client, model, *args, **kwargs)
        await emit_event(
            db, user_id, taxonomy.DECISION_FINALIZED,
            entity="evening_summary",
            source="evening_summary", status="finalized",
            outcome="ai" if raw_outputs else "deterministic_fallback",
            properties={
                "resolution": "ai" if raw_outputs else "deterministic_fallback",
                "computed_flag_count": len(computed_flags),
            },
            content={"final_summary": summary.model_dump() if hasattr(summary, "model_dump") else None},
        )
        return summary

    return wrapper


def _extract_routine_wrapper(original: Any) -> Any:
    @functools.wraps(original)
    async def wrapper(description: str) -> Any:
        resolved = _resolve_db_user()
        if resolved is None:
            return await original(description)
        db, user_id = resolved
        with capture_ai_outputs() as raw_outputs:
            extraction = await original(description)
        dumped = extraction.model_dump() if hasattr(extraction, "model_dump") else {}
        is_empty = not any(value for value in dumped.values())
        if not raw_outputs:
            # Product design: no client / AI failure yields an EMPTY
            # extraction. Make that explicit instead of indistinguishable
            # from "the user's text contained nothing extractable".
            await emit_event(
                db, user_id, taxonomy.DECISION_FALLBACK_SELECTED,
                entity="routine_extraction",
                source="routine", status="selected",
                outcome="empty_default",
                properties={"reason": "ai_unavailable_or_failed", "empty_result": is_empty},
            )
        await emit_event(
            db, user_id, taxonomy.DECISION_FINALIZED,
            entity="routine_extraction",
            source="routine", status="finalized",
            outcome="ai" if raw_outputs else "empty_default",
            properties={"resolution": "ai" if raw_outputs else "empty_default", "empty_result": is_empty},
            content={"final_extraction": dumped},
        )
        return extraction

    return wrapper


def install_decision_trace() -> None:
    """Wrap the coaching decision boundaries (idempotent, uninstallable)."""
    import assistant as assistant_module
    import recommendations as recommendations_module
    from noam_coach.services import profile as profile_module

    _wrap(assistant_module, "classify_intent", _classify_intent_wrapper)
    _wrap(recommendations_module, "evening_summary", _evening_summary_wrapper)
    _wrap(profile_module, "extract_daily_routine", _extract_routine_wrapper)


def uninstall_decision_trace() -> None:
    for module, name, original in reversed(_installed["wraps"]):
        setattr(module, name, original)
    _installed["wraps"] = []
