"""Meaningful domain state transitions (Observability O7).

Attribute wraps over the canonical mutation functions — NOT SQL logging.
Each wrapped boundary is a domain decision the product already funnels
through one function, so event volume stays bounded to real transitions:

- conversation flow lifecycle: every transition funnels through
  ``set_active_flow`` (clear/expire/resume included) → flow.started /
  flow.updated / flow.suspended+started / flow.completed / flow.expired /
  flow.resumed with before/after semantic snapshots.
- user facts: set_fact / confirm_fact / invalidate_fact → state.mutated
  (domain=user_fact) with before/after values; NO-OP writes emit nothing.
- goals: activate_goal_version(_provisional) → state.mutated
  (domain=goal, action=activated/activated_provisional; previous actives
  are superseded by the same transaction).
- plans: planning.activate_plan → state.mutated (domain=plan,
  action=activated).
- next-meal recommendation lifecycle: presented / selection-marked /
  invalidated.
- daily menu lifecycle: presented / marked stale (with reason).
- workout sets/sessions: save_set → state.mutated (domain=workout_set,
  + domain=workout_session action=completed when the save closed the
  session).
- Health import: run_post_import_reconciliation → started/completed/
  failed envelope around the (protected-file) pipeline, joining the
  pre-existing domain events (local_health_import_*, health_facts_
  activated) in the same trace.

Meal save/undo/approval and menu generation already emit domain events at
their (protected) call sites; those join traces via append_event's ambient
correlation (O5), so they are deliberately not double-instrumented here.
"""

from __future__ import annotations

import functools
from contextvars import ContextVar
from typing import Any

from noam_coach.observability import taxonomy
from noam_coach.observability.emit import emit_event

_installed: dict[str, list] = {"wraps": []}

# expire_if_needed / resume_suspended funnel through set_active_flow; this
# flag lets the inner wrap name the transition truthfully.
_flow_transition_hint: ContextVar[str | None] = ContextVar(
    "obs_flow_transition_hint", default=None
)


def _wrap(module: Any, name: str, factory: Any) -> None:
    import coach_bot

    original = getattr(module, name, None)
    if original is None or getattr(original, "__obs_state_trace__", False):
        return
    wrapped = factory(original)
    wrapped.__obs_state_trace__ = True
    setattr(module, name, wrapped)
    _installed["wraps"].append((module, name, original))
    if getattr(coach_bot, name, None) is original:
        setattr(coach_bot, name, wrapped)
        _installed["wraps"].append((coach_bot, name, original))


def _bounded_flow(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "flow_id": snapshot.get("flow_id"),
        "name": snapshot.get("name"),
        "step": snapshot.get("step"),
        "version": snapshot.get("version"),
        "status": snapshot.get("status"),
    }


def _set_active_flow_wrapper(original: Any) -> Any:
    import conversation

    @functools.wraps(original)
    async def wrapper(
        db: Any, user_id: int, flow: Any, step: str = "", payload: Any = None, **kwargs: Any
    ) -> int:
        try:
            before = (await conversation.get_active_flow(db, user_id)).snapshot()
        except Exception:  # noqa: BLE001
            before = None
        version = await original(db, user_id, flow, step, payload, **kwargs)
        try:
            after = (await conversation.get_active_flow(db, user_id)).snapshot()
        except Exception:  # noqa: BLE001
            after = None
        if before is None or after is None:
            return version

        hint = _flow_transition_hint.get()
        before_idle = before.get("name") == "idle"
        after_idle = after.get("name") == "idle"
        if hint == "expired":
            event = taxonomy.FLOW_EXPIRED
        elif hint == "resumed":
            event = taxonomy.FLOW_RESUMED
        elif after_idle and not before_idle:
            event = taxonomy.FLOW_COMPLETED
        elif before_idle and not after_idle:
            event = taxonomy.FLOW_STARTED
        elif before.get("flow_id") == after.get("flow_id"):
            event = taxonomy.FLOW_UPDATED
        else:
            event = taxonomy.FLOW_STARTED
        if before_idle and after_idle:
            return version  # idle → idle: nothing meaningful happened

        if kwargs.get("suspend_current") and not before_idle and event == taxonomy.FLOW_STARTED:
            await emit_event(
                db, user_id, taxonomy.FLOW_SUSPENDED,
                entity="flow", entity_id=before.get("flow_id"),
                flow_id=before.get("flow_id"), flow_version=before.get("version"),
                source="conversation", status="suspended",
                properties={"flow": _bounded_flow(before)},
            )
        await emit_event(
            db, user_id, event,
            entity="flow", entity_id=after.get("flow_id") or before.get("flow_id"),
            flow_id=after.get("flow_id") or before.get("flow_id"),
            flow_version=after.get("version"),
            source="conversation",
            status=event.rsplit(".", 1)[-1],
            properties={"flow_before": _bounded_flow(before), "flow_after": _bounded_flow(after)},
            before={"payload": before.get("payload")},
            after={"payload": after.get("payload")},
        )
        return version

    return wrapper


def _expire_wrapper(original: Any) -> Any:
    @functools.wraps(original)
    async def wrapper(db: Any, user_id: int) -> Any:
        token = _flow_transition_hint.set("expired")
        try:
            return await original(db, user_id)
        finally:
            _flow_transition_hint.reset(token)

    return wrapper


def _resume_wrapper(original: Any) -> Any:
    @functools.wraps(original)
    async def wrapper(db: Any, user_id: int) -> Any:
        token = _flow_transition_hint.set("resumed")
        try:
            return await original(db, user_id)
        finally:
            _flow_transition_hint.reset(token)

    return wrapper


def _set_fact_wrapper(original: Any) -> Any:
    import user_model

    @functools.wraps(original)
    async def wrapper(db: Any, user_id: int, key: str, value: Any, **kwargs: Any) -> bool:
        try:
            old_fact = await user_model.get_fact(db, user_id, key)
        except Exception:  # noqa: BLE001
            old_fact = None
        changed = await original(db, user_id, key, value, **kwargs)
        if not changed and old_fact is not None:
            return changed  # no-op upsert: no event (bounded volume)
        await emit_event(
            db, user_id, taxonomy.STATE_MUTATED,
            entity="user_fact", entity_id=key,
            source="user_model",
            status="mutated",
            outcome="created" if old_fact is None else "changed",
            properties={
                "domain": "user_fact",
                "key": key,
                "action": "created" if old_fact is None else "changed",
                "fact_source": kwargs.get("source"),
                "confirmed": kwargs.get("confirmed"),
            },
            before={"value": (old_fact or {}).get("value")} if old_fact else None,
            after={"value": value},
        )
        return changed

    return wrapper


def _fact_status_wrapper(original: Any, action: str) -> Any:
    @functools.wraps(original)
    async def wrapper(db: Any, user_id: int, key: str) -> Any:
        result = await original(db, user_id, key)
        await emit_event(
            db, user_id, taxonomy.STATE_MUTATED,
            entity="user_fact", entity_id=key,
            source="user_model", status="mutated", outcome=action,
            properties={"domain": "user_fact", "key": key, "action": action},
        )
        return result

    return wrapper


def _activate_plan_wrapper(original: Any) -> Any:
    @functools.wraps(original)
    async def wrapper(db: Any, user_id: int, plan_id: int) -> Any:
        result = await original(db, user_id, plan_id)
        if result:
            await emit_event(
                db, user_id, taxonomy.STATE_MUTATED,
                entity="plan", entity_id=plan_id,
                source="planning", status="mutated", outcome="activated",
                properties={
                    "domain": "plan",
                    "action": "activated",
                    "plan_id": plan_id,
                    "plan_type": result.get("plan_type"),
                    "title": result.get("title"),
                },
            )
        return result

    return wrapper


def _activate_goal_wrapper(original: Any, action: str) -> Any:
    @functools.wraps(original)
    async def wrapper(user_id: int, version_id: int) -> Any:
        result = await original(user_id, version_id)
        import coach_bot

        await emit_event(
            coach_bot.DB, user_id, taxonomy.STATE_MUTATED,
            entity="goal", entity_id=version_id,
            source="goals", status="mutated", outcome=action,
            properties={
                "domain": "goal",
                "action": action,
                "goal_version_id": version_id,
                "supersedes_previous_active": True,
            },
        )
        return result

    return wrapper


def _remember_recommendation_wrapper(original: Any) -> Any:
    @functools.wraps(original)
    async def wrapper(db: Any, user_id: int, recommendation: Any, **kwargs: Any) -> Any:
        result = await original(db, user_id, recommendation, **kwargs)
        options = list(getattr(recommendation, "options", []) or [])
        await emit_event(
            db, user_id, taxonomy.STATE_MUTATED,
            entity="next_meal_recommendation",
            source="next_meal", status="mutated", outcome="presented",
            properties={
                "domain": "recommendation",
                "action": "presented",
                "option_count": len(options),
                "option_titles": [getattr(o, "title", None) for o in options],
                "message_id": kwargs.get("message_id"),
            },
        )
        return result

    return wrapper


def _clear_recommendation_wrapper(original: Any) -> Any:
    @functools.wraps(original)
    async def wrapper(db: Any, user_id: int) -> Any:
        result = await original(db, user_id)
        await emit_event(
            db, user_id, taxonomy.STATE_MUTATED,
            entity="next_meal_recommendation",
            source="next_meal", status="mutated", outcome="invalidated",
            properties={"domain": "recommendation", "action": "invalidated"},
        )
        return result

    return wrapper


def _mark_selection_wrapper(original: Any) -> Any:
    @functools.wraps(original)
    async def wrapper(db: Any, user_id: int, option_number: int, **kwargs: Any) -> Any:
        result = await original(db, user_id, option_number, **kwargs)
        await emit_event(
            db, user_id, taxonomy.STATE_MUTATED,
            entity="next_meal_recommendation",
            source="next_meal", status="mutated", outcome="selected",
            properties={
                "domain": "recommendation",
                "action": "selected",
                "option_number": option_number,
            },
        )
        return result

    return wrapper


def _remember_menu_wrapper(original: Any) -> Any:
    @functools.wraps(original)
    async def wrapper(db: Any, user_id: int, **kwargs: Any) -> Any:
        result = await original(db, user_id, **kwargs)
        await emit_event(
            db, user_id, taxonomy.STATE_MUTATED,
            entity="daily_menu", entity_id=(result or {}).get("menu_id"),
            source="daily_menu", status="mutated", outcome="presented",
            properties={
                "domain": "daily_menu",
                "action": "presented",
                "menu_id": (result or {}).get("menu_id"),
                "meal_count": len(kwargs.get("meals") or []),
                "strategy": kwargs.get("strategy"),
            },
        )
        return result

    return wrapper


def _menu_stale_wrapper(original: Any) -> Any:
    @functools.wraps(original)
    async def wrapper(db: Any, user_id: int, *, reason: str, **kwargs: Any) -> Any:
        result = await original(db, user_id, reason=reason, **kwargs)
        if result:
            await emit_event(
                db, user_id, taxonomy.STATE_MUTATED,
                entity="daily_menu",
                source="daily_menu", status="mutated", outcome="invalidated",
                properties={"domain": "daily_menu", "action": "invalidated", "reason": reason},
            )
        return result

    return wrapper


def _save_set_wrapper(original: Any) -> Any:
    @functools.wraps(original)
    async def wrapper(session: dict[str, Any], weight: float, reps: int, rir: int, source: str, client_event_id: Any = None) -> Any:
        result = await original(session, weight, reps, rir, source, client_event_id)
        try:
            import coach_bot

            db = coach_bot.DB
            user_id = int(session.get("user_id"))
            completed = bool(result[0]) if isinstance(result, tuple) and result else False
            await emit_event(
                db, user_id, taxonomy.STATE_MUTATED,
                entity="workout_set", entity_id=session.get("id"),
                source="workout", status="mutated", outcome="saved",
                properties={
                    "domain": "workout_set",
                    "action": "saved",
                    "session_id": session.get("id"),
                    "session_code": session.get("code"),
                    "exercise_index": session.get("exercise_index"),
                    "set_number": session.get("set_number"),
                    "weight": weight,
                    "reps": reps,
                    "rir": rir,
                    "input_source": source,
                },
            )
            if completed:
                await emit_event(
                    db, user_id, taxonomy.STATE_MUTATED,
                    entity="workout_session", entity_id=session.get("id"),
                    source="workout", status="mutated", outcome="completed",
                    properties={
                        "domain": "workout_session",
                        "action": "completed",
                        "session_id": session.get("id"),
                        "session_code": session.get("code"),
                    },
                )
        except Exception:  # noqa: BLE001 — observation never breaks a set save.
            pass
        return result

    return wrapper


def _reconciliation_wrapper(original: Any) -> Any:
    @functools.wraps(original)
    async def wrapper(message: Any, user_id: int) -> Any:
        import coach_bot

        db = coach_bot.DB
        await emit_event(
            db, user_id, taxonomy.STATE_MUTATED,
            entity="health_import",
            source="health", status="mutated", outcome="reconciliation_started",
            properties={"domain": "health_import", "action": "reconciliation_started"},
        )
        try:
            result = await original(message, user_id)
        except Exception as exc:
            await emit_event(
                db, user_id, taxonomy.STATE_MUTATED,
                entity="health_import",
                source="health", status="failed", outcome="reconciliation_failed",
                properties={
                    "domain": "health_import",
                    "action": "reconciliation_failed",
                    "error_type": type(exc).__name__,
                },
            )
            raise
        await emit_event(
            db, user_id, taxonomy.STATE_MUTATED,
            entity="health_import",
            source="health", status="mutated", outcome="reconciliation_completed",
            properties={"domain": "health_import", "action": "reconciliation_completed"},
        )
        return result

    return wrapper


def install_state_trace() -> None:
    """Wrap the canonical mutation boundaries (idempotent, uninstallable)."""
    import conversation
    import planning
    import user_model
    from noam_coach.bot import workout as workout_module
    from noam_coach.services import daily_menu_state, goals, health_jobs, next_meal

    _wrap(conversation, "set_active_flow", _set_active_flow_wrapper)
    _wrap(conversation, "expire_if_needed", _expire_wrapper)
    _wrap(conversation, "resume_suspended", _resume_wrapper)
    _wrap(user_model, "set_fact", _set_fact_wrapper)
    _wrap(user_model, "confirm_fact", lambda fn: _fact_status_wrapper(fn, "confirmed"))
    _wrap(user_model, "invalidate_fact", lambda fn: _fact_status_wrapper(fn, "invalidated"))
    _wrap(planning, "activate_plan", _activate_plan_wrapper)
    _wrap(goals, "activate_goal_version", lambda fn: _activate_goal_wrapper(fn, "activated"))
    _wrap(
        goals,
        "activate_goal_version_provisional",
        lambda fn: _activate_goal_wrapper(fn, "activated_provisional"),
    )
    _wrap(next_meal, "remember_active_recommendation", _remember_recommendation_wrapper)
    _wrap(next_meal, "clear_active_recommendation", _clear_recommendation_wrapper)
    _wrap(next_meal, "mark_active_recommendation_selection", _mark_selection_wrapper)
    _wrap(daily_menu_state, "remember_active_daily_menu", _remember_menu_wrapper)
    _wrap(daily_menu_state, "mark_daily_menu_stale", _menu_stale_wrapper)
    _wrap(workout_module, "save_set", _save_set_wrapper)
    _wrap(health_jobs, "run_post_import_reconciliation", _reconciliation_wrapper)


def uninstall_state_trace() -> None:
    for module, name, original in reversed(_installed["wraps"]):
        setattr(module, name, original)
    _installed["wraps"] = []
