"""Batch 7 — structured evidence for the meal correction lifecycle.

The 2026-07 audit had to reconstruct the ``quantity_count=3`` /
``quantity_unit="כדור"`` / ``grams=3`` incident by reading exported traces and
inferring intent. That is not diagnosable at scale: a bare
"correction applied" event cannot say whether the parser understood the text,
which identity was confirmed, why a count did not become grams, or what the
card finally showed.

This module builds the missing evidence. It owns no I/O and no policy:

- it does not decide anything — Batch 6.1's classifier is the authority on
  conversion outcomes, and this module only reports what it already decided;
- it does not write events — callers pass these payloads to ``emit_event``,
  the canonical boundary that owns redaction, mode policy and failure
  containment;
- it never returns raw user text in ``properties``. Raw text belongs in
  ``emit_event``'s ``content``, where OFF/METADATA/CONTENT/DEBUG policy and
  the existing digest machinery apply. There is no second hasher here.

Join keys, in every payload where they are meaningful:

``entity_id``   the approval id (passed to emit_event, not duplicated here)
``revision``    orders events within one approval
trace/interaction correlation is supplied automatically by the ambient
observability scope — this module never mints identifiers.
"""

from __future__ import annotations

from typing import Any

# Final rendered quantity modes — what the user actually SEES on the card.
# These mirror ``_quantity_text`` in noam_coach/bot/meals.py; the render-mode
# event is what proves the card and the evidence agree.
RENDER_MODE_COUNT_WITH_ESTIMATE = "count_with_estimate"  # "3 יחידות (~450 גרם)"
RENDER_MODE_COUNT_ONLY = "count_only"                    # "3 יחידות"
RENDER_MODE_GRAMS = "grams"                              # "450 גרם"

# Evidence strength, coarsest-first. Lets a reader answer "did explicit user
# grams outrank the count/AI evidence?" without re-deriving the hierarchy.
_EVIDENCE_STRENGTH = {
    "user": "explicit_user",
    "user_grams": "explicit_user",
    "package_label": "package_label",
    "count_derived": "count_derived",
    "user_count": "stated_count",
    "visual_count": "ai_visual_count",
    "estimate": "ai_estimate",
    "": "unknown",
}


def evidence_strength(source: str | None) -> str:
    """Coarse strength label for a ``quantity_source``."""
    return _EVIDENCE_STRENGTH.get(str(source or ""), "other")


def render_quantity_mode(item: Any) -> str:
    """The mode the meal card will render this item's quantity in.

    Mirrors ``_quantity_text``'s branch order exactly. Emitted so an auditor
    can confirm the number the user saw matches the evidence we recorded —
    the specific failure in the incident, where the card showed "3 גרם".
    """
    count = getattr(item, "quantity_count", None)
    source = str(getattr(item, "quantity_source", "") or "")
    if count and source == "count_derived":
        return RENDER_MODE_COUNT_WITH_ESTIMATE
    if count and source in {"", "visual_count", "estimate", "user_count"}:
        return RENDER_MODE_COUNT_ONLY
    return RENDER_MODE_GRAMS


def _item_quantity_evidence(item: Any, index: int) -> dict[str, Any]:
    """Compact per-item quantity evidence. Numbers and enums only.

    The item NAME is included because identity and quantity must be joinable
    ("which food was this count about?") — it is a food name the user already
    sees on their own card, not conversational content.
    """
    source = str(getattr(item, "quantity_source", "") or "")
    count = getattr(item, "quantity_count", None)
    return {
        "index": index,
        "name": str(getattr(item, "name", "") or ""),
        "grams": _num(getattr(item, "grams", None)),
        "quantity_count": _num(count),
        "quantity_unit": str(getattr(item, "quantity_unit", "") or "") or None,
        "quantity_source": source or None,
        "evidence_strength": evidence_strength(source),
        "render_mode": render_quantity_mode(item),
    }


def _num(value: Any) -> float | None:
    """Best-effort number, never raising on a malformed draft."""
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def correction_parse_properties(
    corrections: Any,
    *,
    used_deterministic: bool,
    revision: int | None = None,
    text_length: int | None = None,
) -> dict[str, Any]:
    """Evidence for the parser decision: deterministic vs AI, and what kinds.

    ``corrections`` is the parser's output list. Only the KINDS are recorded
    — never the matched text, which is user content.
    """
    kinds: list[str] = []
    for correction in corrections or []:
        kind = str(getattr(correction, "kind", "") or "")
        if kind:
            kinds.append(kind)
    return {
        "resolution_source": "deterministic" if used_deterministic else "ai_reanalysis",
        "deterministic": bool(used_deterministic),
        "correction_kinds": sorted(set(kinds)),
        "correction_count": len(kinds),
        "revision": revision,
        "text_length": text_length,
    }


def identity_constraint_properties(
    rejected: Any = None,
    confirmed: Any = None,
) -> dict[str, Any]:
    """Rejected/confirmed identities, so identity joins to quantity evidence.

    Food names only — the same names rendered on the user's card.
    """
    def _names(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value] if value else []
        try:
            return [str(entry) for entry in value if str(entry or "")]
        except TypeError:
            return [str(value)]

    rejected_names = _names(rejected)
    confirmed_names = _names(confirmed)
    return {
        "rejected_identities": rejected_names,
        "confirmed_identities": confirmed_names,
        "identity_constrained": bool(rejected_names or confirmed_names),
    }


def conversion_properties(
    diagnostic: Any,
    *,
    item_index: int | None = None,
    item_name: str | None = None,
) -> dict[str, Any]:
    """Authoritative conversion evidence from a Batch 6.1 diagnostic.

    The diagnostic is consumed READ-ONLY — Batch 7 never changes what it
    decided. ``outcome`` is the authoritative reason a count did or did not
    become grams, which is exactly what the incident lacked.
    """
    props: dict[str, Any] = {
        "outcome": getattr(diagnostic, "outcome", None),
        "converted": bool(getattr(diagnostic, "converted", False)),
        "is_failure": bool(getattr(diagnostic, "is_failure", False)),
        "quantity_count": _num(getattr(diagnostic, "count", None)),
        "quantity_unit": (getattr(diagnostic, "unit", "") or None),
        "grams_before": _num(getattr(diagnostic, "grams_before", None)),
        "grams_after": _num(getattr(diagnostic, "grams_after", None)),
        "source_before": (getattr(diagnostic, "source_before", "") or None),
        "conversion_source": (
            getattr(diagnostic, "source_after", "")
            or getattr(diagnostic, "source_before", "")
            or None
        ),
        "evidence_strength": evidence_strength(
            getattr(diagnostic, "source_after", "")
            or getattr(diagnostic, "source_before", "")
        ),
    }
    derived = _num(getattr(diagnostic, "derived_grams", None))
    if derived is not None:
        # The number the converter WANTED, even when it refused it — an
        # auditor needs "it wanted 4500 g, the ceiling refused" to be visible.
        props["derived_grams"] = derived
    per_unit = _num(getattr(diagnostic, "per_unit_grams", None))
    if per_unit is not None:
        props["per_unit_grams"] = per_unit
    codes = getattr(diagnostic, "blocking_codes", ()) or ()
    if codes:
        props["blocking_codes"] = list(codes)
    if item_index is not None:
        props["item_index"] = item_index
    if item_name:
        props["item_name"] = item_name
    return props


def plausibility_properties(issues: Any) -> dict[str, Any]:
    """Plausibility outcome: blocked or clean, with the codes that fired.

    Only stable codes and severities — never the Hebrew user-facing message,
    which embeds food names and numbers already covered by other fields.
    """
    blocking: list[str] = []
    warnings: list[str] = []
    for issue in issues or []:
        code = str(getattr(issue, "code", "") or "")
        if not code:
            continue
        if str(getattr(issue, "severity", "")) == "block":
            blocking.append(code)
        else:
            warnings.append(code)
    return {
        "blocked": bool(blocking),
        "blocking_codes": blocking,
        "warning_codes": warnings,
        "issue_count": len(blocking) + len(warnings),
    }


def clarification_properties(
    pending: Any,
    *,
    status: str,
    selected_decision: str | None = None,
    duplicate: bool = False,
    stale: bool = False,
    mutated: bool | None = None,
) -> dict[str, Any]:
    """Clarification lifecycle evidence.

    ``status`` is one of ``raised`` / ``resolved`` / ``cancelled`` / ``stale``
    / ``duplicate``. ``mutated`` records whether this response actually
    changed the draft — the field that distinguishes a real mutation from a
    duplicate tap or a stale answer, both of which must show zero mutation.

    The question TEXT is not recorded here: it is generated from the reason,
    the item name and the count, all of which are present as fields.
    """
    props: dict[str, Any] = {
        "status": status,
        "reason": str(getattr(pending, "reason", "") or "") or None,
        "token": str(getattr(pending, "token", "") or "") or None,
        "item_index": getattr(pending, "item_index", None),
        "item_name": str(getattr(pending, "item_name", "") or "") or None,
        "quantity_count": _num(getattr(pending, "count", None)),
        "quantity_unit": str(getattr(pending, "unit_label", "") or "") or None,
        "resolved": bool(getattr(pending, "is_resolved", False)),
        "resolution": str(getattr(pending, "resolution", "") or "") or None,
        "resolved_grams": _num(getattr(pending, "resolved_grams", None)),
        "prior_grams": _num(getattr(pending, "prior_grams", None)),
        "prior_source": str(getattr(pending, "prior_source", "") or "") or None,
        "duplicate_response": bool(duplicate),
        "stale_response": bool(stale),
    }
    if selected_decision is not None:
        props["selected_decision"] = selected_decision
    if mutated is not None:
        props["mutated"] = bool(mutated)
    return props


def render_mode_properties(analysis: Any, *, revision: int | None = None) -> dict[str, Any]:
    """What the card finally shows, per item.

    The last link in the chain: it proves the rendered quantity matches the
    evidence. In the incident this is where "3 גרם" would have been visible
    as ``render_mode="grams"`` on an item carrying a count — a contradiction
    a reader can now detect directly.
    """
    items = list(getattr(analysis, "items", None) or [])
    evidence = [_item_quantity_evidence(item, index) for index, item in enumerate(items)]
    modes = [entry["render_mode"] for entry in evidence]
    return {
        "revision": revision,
        "item_count": len(evidence),
        "items": evidence,
        "render_modes": sorted(set(modes)),
        "has_question": bool(getattr(analysis, "question", None)),
        # A count rendered as a bare gram value is the incident signature.
        "count_rendered_as_grams": any(
            entry["quantity_count"] and entry["render_mode"] == RENDER_MODE_GRAMS
            for entry in evidence
        ),
    }


def error_properties(exc: BaseException, *, context: str) -> dict[str, Any]:
    """Diagnosable error evidence that cannot leak user text.

    Deliberately records the exception CLASS and a context label, never
    ``str(exc)`` or a traceback: a correction failure's message routinely
    embeds the user's own text, and that is precisely what must not land in
    ``properties``. The class plus the pipeline stage is enough to triage;
    the raw text remains available as mode-governed ``content`` on the
    lifecycle event that preceded the failure.
    """
    return {
        "error_type": type(exc).__name__,
        "error_module": type(exc).__module__,
        "context": context,
    }
