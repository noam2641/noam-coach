"""Canonical observable AI invocation boundary (Observability O4).

One proxy around the AsyncOpenAI client instance makes every production
``responses.create`` / ``responses.parse`` call observable — including the
four call sites inside the protected ``noam_coach/services/profile.py``,
which reach the client through the ``coach_bot`` facade's runtime_bound
sync and therefore receive the proxy without any source edit.

Contract preservation (hard requirement): the proxy returns the ORIGINAL
provider response object untouched — ``output_parsed`` keeps its exact
Pydantic type, ``output_text`` stays a plain string, attribute access
passes through. Call sites cannot tell they are observed.

Purpose classification: each of the 11 inventoried production call sites
is wrapped (module + facade attribute wrap, no file edits) with an
``ai_purpose(...)`` scope:

  intent_classification      assistant.classify_intent
  motivation_rephrasing      recommendations.motivation_message
  morning_menu_generation    recommendations.morning_menu
  menu_repair                recommendations.repair_menu_meals
  next_meal_recommendation   recommendations.intraday_next_meals
  evening_summary            recommendations.evening_summary
  goal_explanation           goal_explainer.explain_targets_with_ai
  meal_image_analysis        profile.analyze_meal_image
  meal_text_analysis         profile.analyze_meal_text
  meal_reanalysis            profile.reanalyze_meal_with_text_and_image
  routine_extraction         profile.extract_daily_routine

A call arriving with no purpose scope is recorded as purpose="unclassified"
(never dropped) so a future unwrapped call site remains visible.

Media policy: image content inside the request (``input_image`` items /
data URLs) is NEVER stored — each is replaced by a reference containing
its sha256 and size; ambient media ids (``media_refs_scope``, set by the
meal-photo pipeline in O5) are attached to the AI events.

Prompt/template identity: prompts are built inline by the call sites (no
template registry exists in this codebase), so the trace records purpose +
model + output schema + the resolved redacted request instead of a
template id — documented as a known limitation.
"""

from __future__ import annotations

import functools
import hashlib
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

from noam_coach.observability import taxonomy
from noam_coach.observability.emit import emit_event
from noam_coach.observability.ids import new_ai_call_id
from noam_coach.observability.obs_context import current_user_id, span_scope

_ai_purpose: ContextVar[str | None] = ContextVar("obs_ai_purpose", default=None)
_media_refs: ContextVar[tuple[str, ...]] = ContextVar("obs_media_refs", default=())


@contextmanager
def ai_purpose(purpose: str) -> Iterator[None]:
    token = _ai_purpose.set(purpose)
    try:
        yield
    finally:
        _ai_purpose.reset(token)


@contextmanager
def media_refs_scope(media_ids: list[str]) -> Iterator[None]:
    """Attach media identities to AI calls made inside this scope (O5)."""
    token = _media_refs.set(tuple(media_ids))
    try:
        yield
    finally:
        _media_refs.reset(token)


def _digest_text(value: str) -> dict[str, Any]:
    return {
        "sha256": hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest(),
        "chars": len(value),
    }


def _sanitize_content_item(item: Any) -> Any:
    """Replace image payloads with references; pass text through (the
    canonical redactor still runs on everything at emit time)."""
    if isinstance(item, dict):
        item_type = item.get("type")
        if item_type in {"input_image", "image_url"}:
            url = item.get("image_url") or item.get("url") or ""
            if isinstance(url, dict):
                url = url.get("url", "")
            return {
                "type": item_type,
                "image_ref": _digest_text(str(url)),
            }
        return {key: _sanitize_content_item(value) for key, value in item.items()}
    if isinstance(item, list):
        return [_sanitize_content_item(entry) for entry in item]
    return item


def _request_snapshot(kwargs: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """(sanitized request messages, image count) — no bytes, no data URLs."""
    image_count = 0
    messages: list[Any] = []
    for message in kwargs.get("input") or []:
        sanitized = _sanitize_content_item(message)
        messages.append(sanitized)
    raw = str(kwargs.get("input"))
    image_count = raw.count("input_image")
    return {"input": messages}, image_count


def _resolve_db_and_user() -> tuple[Any, int] | None:
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


def _output_snapshot(operation: str, response: Any) -> tuple[dict[str, Any], str]:
    """(content payload, output kind) for the completed event."""
    if operation == "responses.parse":
        parsed = getattr(response, "output_parsed", None)
        if parsed is not None:
            dump = parsed.model_dump() if hasattr(parsed, "model_dump") else parsed
            return {"output": dump}, type(parsed).__name__
        return {"output": None}, "none"
    text = getattr(response, "output_text", None)
    return {"output": text}, "text"


class _ObservedResponses:
    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def parse(self, **kwargs: Any) -> Any:
        return await self._observed_call("responses.parse", self._inner.parse, kwargs)

    async def create(self, **kwargs: Any) -> Any:
        return await self._observed_call("responses.create", self._inner.create, kwargs)

    async def _observed_call(
        self, operation: str, method: Any, kwargs: dict[str, Any]
    ) -> Any:
        resolved = _resolve_db_and_user()
        if resolved is None:
            return await method(**kwargs)
        db, user_id = resolved
        ai_call_id = new_ai_call_id()
        purpose = _ai_purpose.get() or "unclassified"
        schema = kwargs.get("text_format")
        schema_name = getattr(schema, "__name__", None) if schema is not None else None
        request_content, image_count = _request_snapshot(kwargs)
        media_ids = list(_media_refs.get())
        base_properties: dict[str, Any] = {
            "ai_call_id": ai_call_id,
            "purpose": purpose,
            "provider": "openai",
            "model": kwargs.get("model"),
            "operation": operation,
            "output_schema": schema_name,
            "image_count": image_count,
            "media_ids": media_ids,
        }
        started = time.monotonic()
        with span_scope():
            await emit_event(
                db, user_id, taxonomy.AI_CALL_STARTED,
                entity="ai_call", entity_id=ai_call_id,
                source="ai", status="started",
                properties=dict(base_properties),
                content={"request": request_content},
            )
            try:
                response = await method(**kwargs)
            except Exception as exc:
                await emit_event(
                    db, user_id, taxonomy.AI_CALL_FAILED,
                    entity="ai_call", entity_id=ai_call_id,
                    source="ai", status="failed",
                    outcome=type(exc).__name__,
                    properties={
                        **base_properties,
                        "duration_ms": int((time.monotonic() - started) * 1000),
                        "error_type": type(exc).__name__,
                        "failure_class": _classify_failure(exc),
                    },
                )
                raise
            output_content, output_kind = _output_snapshot(operation, response)
            usage = getattr(response, "usage", None)
            await emit_event(
                db, user_id, taxonomy.AI_CALL_COMPLETED,
                entity="ai_call", entity_id=ai_call_id,
                source="ai", status="completed",
                outcome=output_kind,
                properties={
                    **base_properties,
                    "duration_ms": int((time.monotonic() - started) * 1000),
                    "output_kind": output_kind,
                    # Named without the word "token" — the canonical redactor
                    # deliberately redacts any *token* key, and these are
                    # innocent usage counts.
                    "usage_input": getattr(usage, "input_tokens", None),
                    "usage_output": getattr(usage, "output_tokens", None),
                },
                content=output_content,
            )
            return response


def _classify_failure(exc: BaseException) -> str:
    name = type(exc).__name__
    lowered = name.lower()
    if "timeout" in lowered:
        return "timeout"
    if "rate" in lowered:
        return "rate_limit"
    if "connection" in lowered or "network" in lowered:
        return "network"
    if "validation" in lowered or "pydantic" in lowered:
        return "schema_validation"
    return "api_error"


class ObservedOpenAIClient:
    """Transparent proxy over AsyncOpenAI. ``client is None`` checks at the
    call sites keep working (the proxy only exists when a real client does)."""

    def __init__(self, inner: Any) -> None:
        self._obs_inner = inner
        self._obs_responses = _ObservedResponses(inner.responses)

    @property
    def responses(self) -> Any:
        return self._obs_responses

    def __getattr__(self, name: str) -> Any:
        return getattr(self._obs_inner, name)


_PURPOSE_WRAPS: tuple[tuple[str, str, str], ...] = (
    # (module path, function name, purpose)
    ("assistant", "classify_intent", "intent_classification"),
    ("recommendations", "motivation_message", "motivation_rephrasing"),
    ("recommendations", "morning_menu", "morning_menu_generation"),
    ("recommendations", "repair_menu_meals", "menu_repair"),
    ("recommendations", "intraday_next_meals", "next_meal_recommendation"),
    ("recommendations", "evening_summary", "evening_summary"),
    ("noam_coach.services.goal_explainer", "explain_targets_with_ai", "goal_explanation"),
    ("noam_coach.services.profile", "analyze_meal_image", "meal_image_analysis"),
    ("noam_coach.services.profile", "analyze_meal_text", "meal_text_analysis"),
    ("noam_coach.services.profile", "reanalyze_meal_with_text_and_image", "meal_reanalysis"),
    ("noam_coach.services.profile", "extract_daily_routine", "routine_extraction"),
)

_installed_state: dict[str, Any] = {"client": None, "wraps": []}


def _purpose_wrapped(purpose: str, function: Any) -> Any:
    @functools.wraps(function)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        with ai_purpose(purpose):
            return await function(*args, **kwargs)

    wrapper.__obs_purpose__ = purpose
    return wrapper


def install_ai_observability() -> None:
    """Wrap the OpenAI client + purpose scopes (idempotent, uninstallable)."""
    import importlib

    import coach_bot

    if _installed_state["client"] is None:
        inner = getattr(coach_bot, "OPENAI_CLIENT", None)
        if inner is not None and not isinstance(inner, ObservedOpenAIClient):
            proxy = ObservedOpenAIClient(inner)
            _installed_state["client"] = inner
            coach_bot.OPENAI_CLIENT = proxy
            import config

            config.OPENAI_CLIENT = proxy

    if not _installed_state["wraps"]:
        for module_path, function_name, purpose in _PURPOSE_WRAPS:
            module = importlib.import_module(module_path)
            original = getattr(module, function_name, None)
            if original is None or getattr(original, "__obs_purpose__", None):
                continue
            wrapped = _purpose_wrapped(purpose, original)
            setattr(module, function_name, wrapped)
            _installed_state["wraps"].append((module, function_name, original))
            # The coach_bot facade re-exports several of these; runtime_bound
            # modules re-sync from the facade, so it must carry the wrap too.
            if getattr(coach_bot, function_name, None) is original:
                setattr(coach_bot, function_name, wrapped)
                _installed_state["wraps"].append((coach_bot, function_name, original))


def uninstall_ai_observability() -> None:
    import coach_bot

    if _installed_state["client"] is not None:
        coach_bot.OPENAI_CLIENT = _installed_state["client"]
        import config

        config.OPENAI_CLIENT = _installed_state["client"]
        _installed_state["client"] = None
    for module, function_name, original in reversed(_installed_state["wraps"]):
        setattr(module, function_name, original)
    _installed_state["wraps"] = []
