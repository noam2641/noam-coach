"""Unified nutrition personalization model (TASK-1/2).

This module answers one question in a single, explainable place: *given
everything we know about a user's food preferences, is a candidate food
allowed, and how much should it be preferred?*

It replaces two implicit assumptions that used to be scattered across the
nutrition code:

1. "The user ate X a lot" is NOT the same as "the user likes X". Frequency in
   meal history only tells us the user is *familiar* with a food (they know
   how to get it, they have eaten it without incident). It is a weak,
   defeasible signal. An explicit statement ("אני לא אוהב X" / "אני אוהב X")
   is a strong, authoritative signal that must dominate frequency.
2. Explicit dislikes are eliminations, not penalties. A disliked food must
   never appear in a generated menu, regardless of how well it fits the
   macros — this is enforced as a hard exclusion, not a scoring deduction.

Signal precedence (highest wins on conflict):
    1. explicit dislike / allergy / intolerance   -> HARD_EXCLUDED (elimination)
    2. explicit like / preference                  -> strong positive signal
    3. familiarity (repeated approved history)      -> small positive signal
    4. no signal                                    -> neutral

Historical consumption alone can never out-rank an explicit statement, and a
single historical occurrence can never count as "familiar" (see
``FAMILIARITY_MIN_COUNT``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import user_model
from noam_coach.services.dietary_restrictions import (
    DietaryRestriction,
    load_restrictions_from_facts,
)
from noam_coach.services.food_preferences import (
    DISLIKE_FACT,
    PREFERENCE_FACT,
    preference_restrictions_from_facts,
    split_fact_list,
)
from noam_coach.services.learned_foods import (
    LearnedFood,
    learned_foods_from_meals,
    normalize_food_key,
)

# A food must appear at least this many times before repetition counts as
# "familiar" evidence at all. One-off meals are noise, not personalization
# signal (spec requirement + module convention in learned_foods.py).
FAMILIARITY_MIN_COUNT = 2

# Polarity values for a PreferenceSignal.
POLARITY_HARD_EXCLUDE = "hard_exclude"   # allergy / intolerance / explicit dislike
POLARITY_PREFERRED = "preferred"         # explicit like
POLARITY_FAMILIAR = "familiar"           # repeated history only, no statement
POLARITY_NEUTRAL = "neutral"

# Source tags — who/what produced this signal.
SOURCE_EXPLICIT_DISLIKE = "explicit_dislike"
SOURCE_EXPLICIT_PREFERENCE = "explicit_preference"
SOURCE_ALLERGY = "allergy_or_restriction"
SOURCE_MEAL_HISTORY = "approved_meal_history"

# Confidence/strength constants. Explicit statements are near-certain and
# strong; familiarity is a small nudge that can never outweigh them (Task 2:
# "never stronger than an explicit preference statement").
_EXPLICIT_CONFIDENCE = 1.0
_EXPLICIT_STRENGTH = 1.0
_FAMILIARITY_MAX_STRENGTH = 0.3  # strictly below any explicit strength


@dataclass(frozen=True)
class PreferenceSignal:
    """One piece of preference evidence about a single food.

    ``canonical_key`` is the normalized food identity (see
    ``learned_foods.normalize_food_key``) so surface-form variants of the same
    food (e.g. "טורטייה" vs "טורטיית חלבון") can be reconciled by callers that
    already do morphological matching (next_meal's ``_food_word_matches``).
    """

    canonical_key: str
    user_label: str
    polarity: str
    source: str
    confidence: float  # 0..1 — how sure we are this signal is correctly attributed
    strength: float  # 0..1 — how strong the preference is, given the polarity
    updated_at: str
    evidence_count: int = 0


@dataclass(frozen=True)
class NutritionPreferenceProfile:
    """Reusable personalization snapshot for menu/meal generation.

    Distinct semantic categories (Task 2) are kept separate rather than
    conflated into one "preferred foods" bucket:

    * ``hard_exclusions`` — allergies, intolerances, explicit dislikes. Any
      food matching one of these MUST be eliminated, never merely scored low.
    * ``explicit_likes`` — foods the user explicitly said they like/prefer.
    * ``familiar_foods`` — foods the user has repeatedly eaten (>=
      ``FAMILIARITY_MIN_COUNT`` times) with no explicit statement either way.
      This is "what the user is familiar with", not "what the user prefers".
    * ``temporarily_avoided_foods`` — day-scoped avoidances (e.g. "אין לי זמן
      לבשל היום" style constraints funnel into food_environment, not here;
      this bucket is reserved for daily_flags-scoped food avoidances distinct
      from permanent facts).
    * ``recently_rejected_meals`` — meals the user explicitly turned down.
    """

    user_id: int
    generated_at: str
    hard_exclusions: list[PreferenceSignal] = field(default_factory=list)
    explicit_likes: list[PreferenceSignal] = field(default_factory=list)
    familiar_foods: list[PreferenceSignal] = field(default_factory=list)
    temporarily_avoided_foods: list[str] = field(default_factory=list)
    recently_rejected_meals: list[str] = field(default_factory=list)
    restrictions: list[DietaryRestriction] = field(default_factory=list)
    learned_foods: list[LearnedFood] = field(default_factory=list)
    food_environment: dict[str, Any] | None = None

    def is_hard_excluded(self, food_text: str) -> bool:
        """True when ``food_text`` matches an allergy/intolerance/dislike.

        Uses the same generic substring+stem matching next_meal already
        proved out (imported lazily to avoid a circular import), so the two
        callers share one definition of "matches" instead of drifting.
        """
        from noam_coach.services.next_meal import _matches_free_text_preference

        if self.restrictions and _matches_free_text_preference([food_text], self.restrictions):
            return True
        key = normalize_food_key(food_text)
        if not key:
            return False
        for signal in self.hard_exclusions:
            if signal.canonical_key and (
                signal.canonical_key in key or key in signal.canonical_key
            ):
                return True
        return False

    def preference_bonus(self, food_text: str) -> tuple[float, str]:
        """Small explainable positive-evidence bonus in [0, 1] plus a reason.

        Explicit likes always outrank familiarity, and familiarity is capped
        well below any explicit signal (Task 2 precedence rule).
        """
        key = normalize_food_key(food_text)
        if not key:
            return 0.0, ""
        for signal in self.explicit_likes:
            if signal.canonical_key and (signal.canonical_key in key or key in signal.canonical_key):
                return _EXPLICIT_STRENGTH, "מאכל שאהבת במפורש"
        for signal in self.familiar_foods:
            if signal.canonical_key and (signal.canonical_key in key or key in signal.canonical_key):
                return signal.strength, "מאכל מוכר מהיסטוריית הארוחות שלך"
        return 0.0, ""

    def familiarity_for(self, food_text: str) -> PreferenceSignal | None:
        key = normalize_food_key(food_text)
        if not key:
            return None
        for signal in self.familiar_foods:
            if signal.canonical_key and (signal.canonical_key in key or key in signal.canonical_key):
                return signal
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _signals_from_fact_list(
    items: list[str],
    *,
    polarity: str,
    source: str,
    updated_at: str,
) -> list[PreferenceSignal]:
    signals: list[PreferenceSignal] = []
    strength = _EXPLICIT_STRENGTH
    for raw in items:
        label = str(raw or "").strip()
        if not label:
            continue
        signals.append(
            PreferenceSignal(
                canonical_key=normalize_food_key(label),
                user_label=label,
                polarity=polarity,
                source=source,
                confidence=_EXPLICIT_CONFIDENCE,
                strength=strength,
                updated_at=updated_at,
            )
        )
    return signals


def _familiarity_signals(learned: list[LearnedFood]) -> list[PreferenceSignal]:
    """Repeated history -> familiarity signal, explicitly NOT a preference.

    Strength scales mildly with count but is capped below any explicit signal
    strength so it can never outrank an explicit statement (Task 2). A single
    occurrence never produces a signal at all (FAMILIARITY_MIN_COUNT).
    """
    signals: list[PreferenceSignal] = []
    for food in learned:
        if food.count < FAMILIARITY_MIN_COUNT:
            continue
        # count=2 -> ~0.12, count>=10 -> capped at _FAMILIARITY_MAX_STRENGTH.
        strength = min(_FAMILIARITY_MAX_STRENGTH, 0.06 * food.count)
        signals.append(
            PreferenceSignal(
                canonical_key=food.key,
                user_label=food.display_name,
                polarity=POLARITY_FAMILIAR,
                source=SOURCE_MEAL_HISTORY,
                confidence=min(1.0, 0.4 + 0.05 * food.count),
                strength=strength,
                updated_at=food.last_eaten_at or _now_iso(),
                evidence_count=food.count,
            )
        )
    return signals


async def build_preference_profile(
    db: Any,
    user_id: int,
    *,
    now: datetime | None = None,
    flags: dict[str, Any] | None = None,
) -> NutritionPreferenceProfile:
    """Build the full personalization snapshot for ``user_id``.

    CRITICAL PRECEDENCE: if the same food appears in both ``disliked_foods``
    (explicit, permanent fact) and the approved meal history (familiarity),
    the explicit dislike always wins — familiar_foods excludes anything that
    is also hard-excluded, and hard_exclusions is built from the freshest
    fact value, so a later "I don't like X" statement overrides any earlier
    frequency-based signal even if X was eaten 20 times before.
    """
    generated_at = _now_iso()
    disliked_raw = split_fact_list(await user_model.get_value(db, user_id, DISLIKE_FACT))
    preferred_raw = split_fact_list(await user_model.get_value(db, user_id, PREFERENCE_FACT))

    dislike_fact_row = await user_model.get_fact(db, user_id, DISLIKE_FACT)
    like_fact_row = await user_model.get_fact(db, user_id, PREFERENCE_FACT)
    dislike_updated = str((dislike_fact_row or {}).get("updated_at") or generated_at)
    like_updated = str((like_fact_row or {}).get("updated_at") or generated_at)

    diet_value = await user_model.get_value(db, user_id, "diet_restrictions")
    allergy_value = await user_model.get_value(db, user_id, "allergies")
    base_restrictions = load_restrictions_from_facts(
        str(diet_value) if diet_value not in (None, "", "none") else None,
        str(allergy_value) if allergy_value not in (None, "", "none") else None,
    )
    preference_restrictions = await preference_restrictions_from_facts(db, user_id)
    restrictions = [*base_restrictions, *preference_restrictions]

    hard_exclusions = _signals_from_fact_list(
        disliked_raw, polarity=POLARITY_HARD_EXCLUDE, source=SOURCE_EXPLICIT_DISLIKE, updated_at=dislike_updated
    )
    allergy_labels = [r.user_label or r.canonical_id for r in base_restrictions if r.restriction_type == "allergy"]
    hard_exclusions += _signals_from_fact_list(
        allergy_labels, polarity=POLARITY_HARD_EXCLUDE, source=SOURCE_ALLERGY, updated_at=generated_at
    )
    explicit_likes = _signals_from_fact_list(
        preferred_raw, polarity=POLARITY_PREFERRED, source=SOURCE_EXPLICIT_PREFERENCE, updated_at=like_updated
    )

    hard_keys = [signal.canonical_key for signal in hard_exclusions if signal.canonical_key]

    def _matches_any_hard_key(candidate_key: str) -> bool:
        # Morphology-generic match (not exact-equality): a dislike statement
        # "טורטייה" must also cover the learned food key "טורטיית חלבון"
        # (same substring+Hebrew-stem primitive as next_meal's matching, so
        # this precedence rule can't be defeated by a surface-form mismatch).
        from noam_coach.services.next_meal import _food_word_matches

        return any(hard_key and _food_word_matches(hard_key, candidate_key) for hard_key in hard_keys)

    learned = await learned_foods_from_meals(db, user_id, limit=12, min_count=FAMILIARITY_MIN_COUNT)
    familiar_all = _familiarity_signals(learned)
    # A food that is explicitly disliked never counts as "familiar" positive
    # evidence, no matter how often it was eaten (Task 1 precedence rule).
    familiar_foods = [
        signal for signal in familiar_all if not _matches_any_hard_key(signal.canonical_key)
    ]
    # Likewise, drop any "explicit like" that has since been superseded by a
    # later dislike (record_food_preference_from_slots already clears the
    # opposite fact on write, but stay defensive here too).
    explicit_likes = [signal for signal in explicit_likes if not _matches_any_hard_key(signal.canonical_key)]

    day_flags = flags or {}
    recently_rejected = split_fact_list(day_flags.get("recently_rejected_meals"))
    temporarily_avoided = split_fact_list(day_flags.get("temporarily_avoided_foods"))

    food_environment_value = await user_model.get_value(db, user_id, "food_environment_context")
    food_environment = None
    if food_environment_value not in (None, "", "none"):
        from noam_coach.services.food_environment import normalize_food_environment_context

        food_environment = normalize_food_environment_context(food_environment_value)

    return NutritionPreferenceProfile(
        user_id=user_id,
        generated_at=generated_at,
        hard_exclusions=hard_exclusions,
        explicit_likes=explicit_likes,
        familiar_foods=familiar_foods,
        temporarily_avoided_foods=temporarily_avoided,
        recently_rejected_meals=recently_rejected,
        restrictions=restrictions,
        learned_foods=learned,
        food_environment=food_environment,
    )
