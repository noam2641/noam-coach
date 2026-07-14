"""Meal photo / correction end-to-end trace (Observability O5).

Wraps the three meal-analysis functions (module + facade attribute wraps —
the source files stay untouched, which matters because ``profile.py`` is
protected local work) so the canonical stream can prove the whole chain:

photo received (O2)
→ media.received           image identity: sha256 / perceptual hash / size —
                           never bytes; media_id is DERIVED from content
                           (md_<sha256[:16]>) so the same photo carries the
                           same identity through analysis and re-analysis
→ ai.call.* (O4)           vision request/output, media_ids attached
→ validation.completed     deterministic Israeli-table/safety overrides:
                           the AI's raw items vs the returned product items,
                           per-item, with reasons
→ decision.finalized       (re-analysis) the final MealAnalysis after locked
                           corrections + explicit user quantities, with
                           machine-readable override entries like
                           {name, ai_grams: 180, final_grams: 250,
                            reason: user_explicit_quantity}
→ duplicate detection      validation.completed entity=media_duplicate with
                           outcome duplicate|no_duplicate
→ persistence + visible output: the pre-existing domain events
                           (MEAL_ANALYSIS_COMPLETED / meal saves) and the O3
                           render/delivery events join the same trace via
                           append_event's ambient-correlation defaulting.

The difference between "AI interpretation" and "final product MealAnalysis"
is therefore recorded at write time — never inferred after the fact from
final DB state.
"""

from __future__ import annotations

import functools
from typing import Any

import meal_intelligence
from noam_coach.observability import taxonomy
from noam_coach.observability.ai_invocation import capture_ai_outputs, media_refs_scope
from noam_coach.observability.emit import emit_event
from noam_coach.observability.obs_context import current_user_id

_installed: dict[str, list] = {"wraps": []}


def media_id_for_bytes(image_bytes: bytes) -> str:
    """Deterministic, content-derived media identity (never the bytes)."""
    return f"md_{meal_intelligence.sha256_bytes(image_bytes)[:16]}"


def _resolve_db_user(explicit_user_id: Any = None) -> tuple[Any, int] | None:
    import coach_bot

    user_id = explicit_user_id if explicit_user_id else current_user_id()
    if user_id is None:
        try:
            from config import SETTINGS

            user_id = getattr(SETTINGS, "telegram_allowed_user_id", None)
        except Exception:  # noqa: BLE001
            user_id = None
    if user_id is None:
        return None
    return coach_bot.DB, int(user_id)


async def emit_media_received(
    db: Any,
    user_id: int,
    image_bytes: bytes,
    *,
    source: str = "telegram",
    caption: str | None = None,
    storage_ref: str | None = None,
    provider_file_unique_id: str | None = None,
) -> str:
    """Record one image's identity; returns its content-derived media_id."""
    media_id = media_id_for_bytes(image_bytes)
    await emit_event(
        db,
        user_id,
        taxonomy.MEDIA_RECEIVED,
        entity="media",
        entity_id=media_id,
        source=source,
        status="received",
        properties={
            "media_id": media_id,
            "media_kind": "image",
            "provider": source,
            "provider_file_unique_id": provider_file_unique_id,
            "sha256": meal_intelligence.sha256_bytes(image_bytes),
            "perceptual_hash": meal_intelligence.perceptual_hash(image_bytes),
            "byte_size": len(image_bytes),
            "mime_type": "image/jpeg",
            "storage_ref": storage_ref,
        },
        content={"caption": caption} if caption else None,
    )
    return media_id


def _items_of(dumped: Any) -> list[dict[str, Any]]:
    if isinstance(dumped, dict):
        return list(dumped.get("items") or [])
    return []


def _override_entries(
    raw_output: Any,
    final_analysis: Any,
    locked_texts: list[str],
) -> list[dict[str, Any]]:
    """Per-item machine-readable diff between the AI interpretation and the
    final product analysis, with the override reason."""
    locked = []
    for text in locked_texts:
        locked.extend(meal_intelligence.parse_locked_quantities(text))
    raw_items = _items_of(raw_output)
    final_items = [item.model_dump() for item in getattr(final_analysis, "items", [])]

    def _match(raw_item: dict[str, Any], index: int) -> dict[str, Any] | None:
        for candidate in final_items:
            if candidate.get("name") == raw_item.get("name"):
                return candidate
        if index < len(final_items):
            return final_items[index]
        return None

    entries: list[dict[str, Any]] = []
    for index, raw_item in enumerate(raw_items):
        final_item = _match(raw_item, index)
        if final_item is None:
            entries.append({
                "name": raw_item.get("name"),
                "change": "removed_by_product",
                "reason": "deterministic_override",
            })
            continue
        if (
            raw_item.get("grams") == final_item.get("grams")
            and raw_item.get("calories") == final_item.get("calories")
            and raw_item.get("name") == final_item.get("name")
        ):
            continue
        raw_name = str(raw_item.get("name") or "")
        reason = "deterministic_override"
        for constraint in locked:
            food = constraint.food_text.casefold()
            if food and (food in raw_name.casefold() or raw_name.casefold() in food):
                reason = "user_explicit_quantity"
                break
        entries.append({
            "name": final_item.get("name"),
            "ai_name": raw_item.get("name"),
            "ai_grams": raw_item.get("grams"),
            "final_grams": final_item.get("grams"),
            "ai_calories": raw_item.get("calories"),
            "final_calories": final_item.get("calories"),
            "reason": reason,
        })
    return entries


def _wrap(module: Any, name: str, wrapper_factory: Any) -> None:
    import coach_bot

    original = getattr(module, name, None)
    if original is None or getattr(original, "__obs_meal_trace__", False):
        return
    wrapped = wrapper_factory(original)
    wrapped.__obs_meal_trace__ = True
    setattr(module, name, wrapped)
    _installed["wraps"].append((module, name, original))
    if getattr(coach_bot, name, None) is original:
        setattr(coach_bot, name, wrapped)
        _installed["wraps"].append((coach_bot, name, original))


def _analyze_image_wrapper(original: Any) -> Any:
    @functools.wraps(original)
    async def wrapper(image_bytes: bytes, *args: Any, **kwargs: Any) -> Any:
        resolved = _resolve_db_user(kwargs.get("user_id"))
        if resolved is None:
            return await original(image_bytes, *args, **kwargs)
        db, user_id = resolved
        media_id = await emit_media_received(
            db, user_id, image_bytes, caption=kwargs.get("caption"),
        )
        with media_refs_scope([media_id]), capture_ai_outputs() as raw_outputs:
            analysis = await original(image_bytes, *args, **kwargs)
        await emit_event(
            db, user_id, taxonomy.VALIDATION_COMPLETED,
            entity="meal_analysis", entity_id=media_id,
            source="meal_pipeline", status="completed",
            outcome="needs_clarification" if getattr(analysis, "question", None) else "analyzed",
            properties={
                "media_id": media_id,
                "stage": "image_analysis",
                "confidence": getattr(analysis, "confidence", None),
                "requires_clarification": bool(getattr(analysis, "question", None)),
                "overrides": _override_entries(
                    raw_outputs[-1] if raw_outputs else None, analysis, [],
                ),
            },
            content={"final_analysis": analysis.model_dump()},
        )
        return analysis

    return wrapper


def _analyze_text_wrapper(original: Any) -> Any:
    @functools.wraps(original)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        resolved = _resolve_db_user(kwargs.get("user_id"))
        if resolved is None:
            return await original(*args, **kwargs)
        db, user_id = resolved
        with capture_ai_outputs() as raw_outputs:
            analysis = await original(*args, **kwargs)
        await emit_event(
            db, user_id, taxonomy.VALIDATION_COMPLETED,
            entity="meal_analysis",
            source="meal_pipeline", status="completed",
            outcome="needs_clarification" if getattr(analysis, "question", None) else "analyzed",
            properties={
                "stage": "text_analysis",
                "confidence": getattr(analysis, "confidence", None),
                "requires_clarification": bool(getattr(analysis, "question", None)),
                "overrides": _override_entries(
                    raw_outputs[-1] if raw_outputs else None, analysis, [],
                ),
            },
            content={"final_analysis": analysis.model_dump()},
        )
        return analysis

    return wrapper


def _reanalyze_wrapper(original: Any) -> Any:
    @functools.wraps(original)
    async def wrapper(
        image_path: str,
        correction_text: str,
        locked_corrections: Any = None,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        resolved = _resolve_db_user()
        if resolved is None:
            return await original(
                image_path, correction_text, locked_corrections, *args, **kwargs
            )
        db, user_id = resolved
        media_ids: list[str] = []
        try:
            from pathlib import Path

            image_bytes = Path(image_path).read_bytes()
            media_ids = [media_id_for_bytes(image_bytes)]
        except Exception:  # noqa: BLE001 — identity is best-effort here.
            pass
        with media_refs_scope(media_ids), capture_ai_outputs() as raw_outputs:
            analysis = await original(
                image_path, correction_text, locked_corrections, *args, **kwargs
            )
        locked_texts = [correction_text, *(locked_corrections or [])]
        overrides = _override_entries(
            raw_outputs[-1] if raw_outputs else None, analysis, locked_texts,
        )
        await emit_event(
            db, user_id, taxonomy.DECISION_FINALIZED,
            entity="meal_analysis",
            entity_id=media_ids[0] if media_ids else None,
            source="meal_pipeline", status="finalized",
            outcome="corrected",
            properties={
                "stage": "reanalysis",
                "media_id": media_ids[0] if media_ids else None,
                "locked_corrections_count": len(locked_corrections or []),
                "overrides": overrides,
                "confidence": getattr(analysis, "confidence", None),
            },
            content={
                "correction_text": correction_text,
                "locked_corrections": list(locked_corrections or []),
                "final_analysis": analysis.model_dump(),
            },
        )
        return analysis

    return wrapper


def _duplicate_wrapper(original: Any) -> Any:
    @functools.wraps(original)
    async def wrapper(db: Any, *args: Any, **kwargs: Any) -> Any:
        result = await original(db, *args, **kwargs)
        resolved = _resolve_db_user(kwargs.get("user_id"))
        if resolved is not None:
            event_db, user_id = resolved
            image_bytes = kwargs.get("image_bytes")
            await emit_event(
                event_db, user_id, taxonomy.VALIDATION_COMPLETED,
                entity="media_duplicate",
                entity_id=media_id_for_bytes(image_bytes) if image_bytes else None,
                source="meal_pipeline", status="completed",
                outcome="duplicate" if result else "no_duplicate",
                properties={
                    "matched_meal_id": (result or {}).get("meal_id"),
                    "provider_file_unique_id": kwargs.get("telegram_file_unique_id"),
                },
            )
        return result

    return wrapper


def install_meal_trace() -> None:
    """Wrap the meal-analysis boundaries (idempotent, uninstallable)."""
    from noam_coach.services import profile as profile_module

    _wrap(profile_module, "analyze_meal_image", _analyze_image_wrapper)
    _wrap(profile_module, "analyze_meal_text", _analyze_text_wrapper)
    _wrap(profile_module, "reanalyze_meal_with_text_and_image", _reanalyze_wrapper)
    _wrap(meal_intelligence, "find_image_duplicate", _duplicate_wrapper)


def uninstall_meal_trace() -> None:
    for module, name, original in reversed(_installed["wraps"]):
        setattr(module, name, original)
    _installed["wraps"] = []
