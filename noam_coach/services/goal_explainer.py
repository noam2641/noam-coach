"""AI-phrased goal explanation, with deterministic numbers (RE10-10).

The daily calorie/protein/steps targets themselves are always computed by
``targets.compute_targets`` — this module only asks the model to turn the
already-decided numbers into a warmer, more complete Hebrew explanation than
the fixed template in ``targets.explain_targets``. The model is never allowed
to invent or change a number: the response is rejected (falling back to the
deterministic template) unless every key figure it was given reappears
verbatim in the returned text.
"""

from __future__ import annotations

from typing import Any

import targets


def _required_numbers(t: "targets.Targets") -> list[str]:
    """The figures that MUST appear verbatim in an AI explanation.

    Kept intentionally strict: calories and protein are the two numbers the
    user actually acts on, so an explanation that alters or omits either one
    is worse than the plain template.
    """
    return [f"{t.calories:,}", str(t.protein)]


def _build_prompt(t: "targets.Targets") -> list[dict[str, Any]]:
    b = t.basis
    goal_he = {
        "fat_loss_muscle_retention": "ירידה בשומן עם שמירת שריר",
        "muscle_gain": "עלייה במסה",
        "strength": "כוח",
        "general_health": "בריאות כללית",
    }.get(b["goal_type"], b["goal_type"])
    facts = {
        "משקל נוכחי (ק\"ג)": b.get("weight_kg"),
        "ממוצע צעדים ביום": b.get("avg_steps"),
        "מטרה": goal_he,
        "אחזקה משוערת (קל')": t.maintenance,
        "יעד קלורי יומי (קל')": t.calories,
        "יעד חלבון יומי (גרם)": t.protein,
        "משקל יעד (ק\"ג)": b.get("goal_weight_kg"),
        "משך זמן ליעד (שבועות)": b.get("goal_timeframe_weeks"),
        "קצב משוער (ק\"ג/שבוע)": b.get("rate_based_kg_per_week"),
    }
    facts_text = "\n".join(f"- {label}: {value}" for label, value in facts.items() if value is not None)
    return [
        {
            "role": "system",
            "content": (
                "You are a fitness/nutrition coach writing a short Hebrew "
                "explanation of how a user's daily calorie and protein target "
                "was calculated. CRITICAL RULES:\n"
                "1. You are given the FINAL numbers already computed by "
                "deterministic code. You must NOT invent, recalculate, or "
                "change ANY number. Use exactly the calorie and protein "
                "figures given to you, written out in full (not abbreviated).\n"
                "2. Follow this structure: what the baseline (maintenance) is "
                "based on -> how the daily target was derived from it -> what "
                "that means in practice day to day -> a short note that the "
                "target will be revisited as the user's data updates.\n"
                "3. Warm, direct, 3-5 sentences. No medical advice, no "
                "guarantees, no emojis.\n"
                "4. Hebrew only."
            ),
        },
        {
            "role": "user",
            "content": "הנתונים לשימוש (אל תשנה אף מספר):\n" + facts_text,
        },
    ]


def _numbers_present(text: str, required: list[str]) -> bool:
    return all(number in text for number in required)


async def explain_targets_with_ai(t: "targets.Targets", *, openai_client: Any, model: str) -> str:
    """Return an AI-phrased explanation, or the deterministic template on any failure.

    Never raises — a broken/missing AI client, a timeout, or a response that
    fails the number-integrity check all fall back to
    ``targets.explain_targets`` so this feature can never block the goal
    screen from rendering.
    """
    fallback = targets.explain_targets(t)
    if openai_client is None:
        return fallback
    required = _required_numbers(t)
    try:
        response = await openai_client.responses.create(
            model=model,
            input=_build_prompt(t),
        )
        text = (response.output_text or "").strip()
    except Exception:  # noqa: BLE001 - AI failures must never block the goal screen
        return fallback
    if not text or not _numbers_present(text, required):
        return fallback
    return text
