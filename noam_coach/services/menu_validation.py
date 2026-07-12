"""Deterministic validation, repair and scoring for AI-composed daily menus.

TASK-5/6/11. This module is the CODE VALIDATES / CODE REPAIRS step of the
target architecture:

    CODE CONSTRAINS -> AI COMPOSES -> CODE VALIDATES -> CODE REPAIRS OR REJECTS
    -> SAFE PERSONALIZED OUTPUT

It extracts the proven, already-shipped primitives from
``noam_coach.services.next_meal`` (hard dislike/allergy matching, Hebrew
morphology tolerance, restriction validation) so a generated ``MorningMenu``
and a single next-meal ``MealOption`` share ONE definition of "this food is
not allowed for this user" instead of two implementations that could drift.

It also operationalizes ``prompt_builder.SAFETY_CONTRACT`` (TASK-6): every key
in that contract corresponds to a concrete check here, not just prose sent to
the model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from noam_coach.services.dietary_restrictions import (
    DietaryRestriction,
    validate_meal_restrictions,
)
from noam_coach.services.next_meal import (
    _food_word_matches,
    _free_text_preference_key,
    _matches_free_text_preference,
)
from noam_coach.services.preference_profile import NutritionPreferenceProfile

# ---------------------------------------------------------------------------
# Problem codes (also used as the basis for TASK-12 observability event names)
# ---------------------------------------------------------------------------
PROBLEM_DISLIKED_FOOD = "disliked_food"
PROBLEM_RESTRICTION_VIOLATION = "restriction_violation"
PROBLEM_SLOT_AFFINITY_VIOLATION = "slot_affinity_violation"
PROBLEM_INVALID_MEAL = "invalid_meal_values"
PROBLEM_IMPLAUSIBLE_MACROS = "implausible_macros"
PROBLEM_CALORIE_TARGET_MISS = "calorie_target_miss"
PROBLEM_PROTEIN_TARGET_MISS = "protein_target_miss"
PROBLEM_NOT_CHRONOLOGICAL = "not_chronological"
PROBLEM_PLANNED_TREATED_AS_CONSUMED = "planned_treated_as_consumed"
PROBLEM_REPEATS_RECENT_MEAL = "repeats_recent_meal"
PROBLEM_EMPTY_MENU = "empty_menu"

# How far the menu total may drift from calorie/protein targets before it is
# considered a material miss requiring repair (Task 5: "approx match").
_CALORIE_TOLERANCE_FRACTION = 0.15
_PROTEIN_TOLERANCE_FRACTION = 0.20


@dataclass(frozen=True)
class MealViolation:
    meal_index: int | None  # None = menu-level violation, not tied to one meal
    meal_name: str
    code: str
    detail: str


@dataclass(frozen=True)
class MenuValidationResult:
    violations: list[MealViolation] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations

    @property
    def affected_meal_indices(self) -> set[int]:
        return {v.meal_index for v in self.violations if v.meal_index is not None}

    @property
    def hard_fail_codes(self) -> set[str]:
        return {v.code for v in self.violations}

    def codes_for_meal(self, index: int) -> set[str]:
        return {v.code for v in self.violations if v.meal_index == index}


def _meal_text_fields(meal: Any) -> list[str]:
    """All free-text surfaces of a MenuMeal worth scanning for a disliked food.

    Blank fields are dropped: ``_food_word_matches``/``_matches_free_text_
    preference`` (next_meal.py) treat an empty candidate string as a
    substring of everything (``"" in x`` is always True in Python), which is
    correct for "no ingredients provided" but would false-positive here
    whenever a meal has no ``note`` (the deterministic fallback often
    leaves it blank) — so only non-empty text is ever handed to the matcher.
    """
    return [
        text
        for text in (
            str(getattr(meal, "name", "") or "").strip(),
            str(getattr(meal, "note", "") or "").strip(),
            str(getattr(meal, "time_hint", "") or "").strip(),
        )
        if text
    ]


def meal_matches_hard_exclusion(
    meal: Any,
    restrictions: list[DietaryRestriction],
    profile: NutritionPreferenceProfile | None = None,
) -> bool:
    """Shared hard-exclusion check (TASK-5/14): same primitive next_meal uses.

    A meal is hard-excluded when its text matches an explicit dislike,
    allergy, intolerance, or avoidance — via the same generic
    substring+Hebrew-stem matching next_meal already proved out. This keeps
    next_meal and morning_menu semantically identical instead of two
    divergent implementations (required regression test #14).
    """
    texts = _meal_text_fields(meal)
    if not texts:
        return False
    if restrictions and _matches_free_text_preference(texts, restrictions):
        return True
    if profile is not None:
        for text in texts:
            if profile.is_hard_excluded(text):
                return True
    return False


def meal_restriction_violations(meal: Any, restrictions: list[DietaryRestriction]) -> list[dict]:
    items = [{"item_name": text} for text in _meal_text_fields(meal) if text]
    if not items:
        return []
    return validate_meal_restrictions(items, restrictions)


def _slot_from_time_hint(time_hint: str) -> str | None:
    """Best-effort clock-hour extraction from a free-text time hint."""
    text = str(time_hint or "").strip()
    if ":" not in text:
        return None
    head = text.split()[0] if text.split() else text
    try:
        hour = int(head.split(":", 1)[0])
    except ValueError:
        return None
    if not (0 <= hour <= 23):
        return None
    from noam_coach.services.learned_foods import meal_slot_for_hour

    return meal_slot_for_hour(hour)


def meal_violates_slot_affinity(
    meal: Any,
    slot: str | None,
    profile: NutritionPreferenceProfile,
) -> bool:
    """TASK-3/5: a strongly single-slot food placed far outside that slot.

    Only fires when there is STRONG evidence (dominant_slot_confidence ==
    "strong") for a different slot than where the meal is scheduled, and the
    scheduled slot is not adjacent/compatible (late counts as compatible with
    dinner). This deliberately does not fire on weak/moderate evidence —
    the goal is removing false confidence, not inventing a new one.
    """
    if slot is None:
        return False
    texts = _meal_text_fields(meal)
    for food in profile.learned_foods:
        if food.dominant_slot_confidence != "strong":
            continue
        dominant = food.dominant_slot
        if not dominant or dominant == slot:
            continue
        # dinner/late are adjacent and both "evening"; treat as compatible.
        if {dominant, slot} <= {"dinner", "late"}:
            continue
        food_key = food.key
        for text in texts:
            key = _free_text_preference_key(text)
            if food_key and _food_word_matches(food_key, key):
                return True
    return False


def _meal_totals(meals: list[Any]) -> tuple[float, float]:
    calories = sum(float(getattr(m, "calories", 0) or 0) for m in meals)
    protein = sum(float(getattr(m, "protein", 0) or 0) for m in meals)
    return calories, protein


def validate_menu(
    menu: Any,
    *,
    restrictions: list[DietaryRestriction],
    profile: NutritionPreferenceProfile | None = None,
    calorie_target: float | None = None,
    protein_target: float | None = None,
    meal_slots: list[str | None] | None = None,
    recently_rejected_keys: set[str] | None = None,
    consumed_meal_keys: set[str] | None = None,
) -> MenuValidationResult:
    """Deterministic post-generation validator for a ``recommendations.MorningMenu``.

    Operationalizes prompt_builder.SAFETY_CONTRACT (TASK-6):
      * ``validate_against_allergies_after_generation`` -> restriction checks below.
      * ``require_output_validation`` -> this function running at all, always,
        before a menu is ever rendered/shown (enforced by the caller pipeline).
      * ``do_not_treat_planned_meals_as_consumed`` -> ``consumed_meal_keys`` is
        built exclusively from ``meals`` table rows (actually eaten), never
        from ``planned_meals``; a generated meal that exactly duplicates an
        already-consumed meal today is flagged so it is not re-offered as if
        the day were still open for it.

    Meal-level checks (hard fail -> meal must be repaired or dropped):
      * explicit dislike / allergy / intolerance (never a warning, always a
        hard fail — TASK-1/5 "must never be shown, full stop")
      * invalid/negative/missing macro values
      * implausible protein-to-calorie ratio
      * strong slot-affinity violation

    Menu-level checks:
      * empty menu
      * total calories/protein materially off target
      * chronological ordering of time hints (when parseable)
    """
    violations: list[MealViolation] = []
    meals = list(getattr(menu, "meals", None) or [])
    if not meals:
        violations.append(MealViolation(None, "", PROBLEM_EMPTY_MENU, "menu has no meals"))
        return MenuValidationResult(violations=violations)

    rejected_keys = recently_rejected_keys or set()
    consumed_keys = consumed_meal_keys or set()

    parsed_hours: list[int | None] = []
    for index, meal in enumerate(meals):
        name = str(getattr(meal, "name", "") or "")
        calories = getattr(meal, "calories", None)
        protein = getattr(meal, "protein", None)

        if meal_matches_hard_exclusion(meal, restrictions, profile):
            violations.append(MealViolation(index, name, PROBLEM_DISLIKED_FOOD, "matches explicit dislike/avoidance"))

        for violation in meal_restriction_violations(meal, restrictions):
            action = str(violation.get("action"))
            if action == "block":
                restriction = violation["restriction"]
                label = restriction.user_label or restriction.canonical_id
                violations.append(
                    MealViolation(index, name, PROBLEM_RESTRICTION_VIOLATION, f"conflicts with {label}")
                )

        if calories is None or protein is None or float(calories) < 0 or float(protein) < 0:
            violations.append(MealViolation(index, name, PROBLEM_INVALID_MEAL, "negative or missing macro value"))
        elif float(protein) * 4 > float(calories) + 15:
            violations.append(
                MealViolation(index, name, PROBLEM_IMPLAUSIBLE_MACROS, "protein grams imply more energy than calories")
            )

        if profile is not None and meal_slots is not None and index < len(meal_slots):
            if meal_violates_slot_affinity(meal, meal_slots[index], profile):
                violations.append(
                    MealViolation(index, name, PROBLEM_SLOT_AFFINITY_VIOLATION, "strong dominant-slot food placed outside its slot")
                )

        meal_key = _free_text_preference_key(name)
        if meal_key and (meal_key in rejected_keys or meal_key in consumed_keys):
            code = PROBLEM_PLANNED_TREATED_AS_CONSUMED if meal_key in consumed_keys else PROBLEM_REPEATS_RECENT_MEAL
            violations.append(MealViolation(index, name, code, "duplicates a meal already logged/rejected today"))

        time_hint = str(getattr(meal, "time_hint", "") or "")
        parsed_hours.append(_parse_hour(time_hint))

    ordered_hours = [h for h in parsed_hours if h is not None]
    if len(ordered_hours) >= 2 and ordered_hours != sorted(ordered_hours):
        violations.append(MealViolation(None, "", PROBLEM_NOT_CHRONOLOGICAL, "meal times are not chronological"))

    total_calories, total_protein = _meal_totals(meals)
    if calorie_target and calorie_target > 0:
        drift = abs(total_calories - calorie_target) / calorie_target
        if drift > _CALORIE_TOLERANCE_FRACTION:
            violations.append(
                MealViolation(None, "", PROBLEM_CALORIE_TARGET_MISS, f"total {total_calories:.0f} vs target {calorie_target:.0f}")
            )
    if protein_target and protein_target > 0:
        drift = abs(total_protein - protein_target) / protein_target
        if drift > _PROTEIN_TOLERANCE_FRACTION:
            violations.append(
                MealViolation(None, "", PROBLEM_PROTEIN_TARGET_MISS, f"total {total_protein:.0f} vs target {protein_target:.0f}")
            )

    return MenuValidationResult(violations=violations)


@dataclass(frozen=True)
class RepairRequest:
    """Everything a targeted repair needs (TASK-7): the original menu, the
    exact violations, immutable constraints, and which meal slots may change.

    Only meals referenced by ``affected_meal_indices`` may be regenerated —
    unaffected meals must be preserved byte-for-byte (Task 5/10 requirement:
    "do not regenerate meals that were not flagged").
    """

    menu: Any
    result: MenuValidationResult
    calorie_target: float | None
    protein_target: float | None
    immutable_constraints: dict[str, Any]

    @property
    def affected_meal_indices(self) -> set[int]:
        return self.result.affected_meal_indices

    @property
    def menu_level_problem(self) -> bool:
        return bool(self.result.violations) and not self.affected_meal_indices


def build_repair_request(
    menu: Any,
    result: MenuValidationResult,
    *,
    calorie_target: float | None,
    protein_target: float | None,
    restrictions: list[DietaryRestriction],
) -> RepairRequest:
    disliked_labels = sorted(
        {r.user_label or r.canonical_id for r in restrictions if r.restriction_type in {"preference", "avoidance", "unavailable"}}
    )
    allergy_labels = sorted({r.user_label or r.canonical_id for r in restrictions if r.restriction_type == "allergy"})
    return RepairRequest(
        menu=menu,
        result=result,
        calorie_target=calorie_target,
        protein_target=protein_target,
        immutable_constraints={
            "hard_excluded_foods": disliked_labels,
            "allergies": allergy_labels,
            "calorie_target": calorie_target,
            "protein_target": protein_target,
        },
    )


def _parse_hour(time_hint: str) -> int | None:
    text = str(time_hint or "").strip()
    if ":" not in text:
        return None
    head = text.split()[0] if text.split() else text
    try:
        hour = int(head.split(":", 1)[0])
    except ValueError:
        return None
    return hour if 0 <= hour <= 23 else None


# ---------------------------------------------------------------------------
# TASK-11: deterministic, explainable scoring for menu/meal candidates.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MealScore:
    value: float
    reason: str
    hard_excluded: bool = False


def score_meal(
    meal: Any,
    *,
    profile: NutritionPreferenceProfile,
    slot: str | None = None,
    calorie_target: float | None = None,
    protein_target: float | None = None,
    workout_relevant: bool = False,
    recently_rejected_keys: set[str] | None = None,
) -> MealScore:
    """Deterministic, explainable score in [-1, 1] following the precedence:

        1. hard validation first (elimination, not a negative score)
        2. explicit positive preference relevance
        3. meal-slot relevance
        4. nutrition fit (calorie/protein)
        5. workout timing
        6. practicality/food-environment
        7. freshness/variety (recency/repetition, recent rejection)

    The strongest single reason is returned alongside the score so callers can
    explain a recommendation (debugging/explainability, not necessarily
    user-facing) without re-deriving it.
    """
    texts = _meal_text_fields(meal)
    combined_text = " ".join(texts)

    if profile.is_hard_excluded(combined_text):
        return MealScore(value=-1.0, reason="נמצא רכיב לא מתאים (אלרגיה/העדפה מפורשת)", hard_excluded=True)

    reasons: list[tuple[float, str]] = []

    # 2. explicit positive preference relevance (dominates familiarity).
    bonus, reason = profile.preference_bonus(combined_text)
    if bonus > 0:
        reasons.append((bonus * 0.35, reason))

    # 3. meal-slot relevance.
    if slot:
        for food in profile.learned_foods:
            key = food.key
            if key and _food_word_matches(key, _free_text_preference_key(combined_text)):
                relevance = food.slot_relevance(slot)
                if relevance > 0:
                    reasons.append((relevance * 0.2, "מתאים לזמן הארוחה הרגיל שלך"))
                break

    # 4. nutrition fit.
    calories = float(getattr(meal, "calories", 0) or 0)
    protein = float(getattr(meal, "protein", 0) or 0)
    if calorie_target and calorie_target > 0:
        cal_fit = max(0.0, 1.0 - abs(calories - calorie_target) / calorie_target)
        reasons.append((cal_fit * 0.2, "מתאים ליעד הקלוריות"))
    if protein_target and protein_target > 0:
        prot_fit = max(0.0, 1.0 - abs(protein - protein_target) / protein_target)
        reasons.append((prot_fit * 0.15, "מתאים ליעד החלבון"))

    # 5. workout timing (small steady bonus, candidate pool already phase-aware).
    if workout_relevant:
        reasons.append((0.1, "מתאים לתזמון האימון"))

    # 6. practicality / food-environment.
    if profile.food_environment:
        from noam_coach.services.food_environment import personal_fit_signals

        signals = personal_fit_signals(profile.food_environment)
        if signals.get("quick_or_limited_access") and len(combined_text) < 40:
            reasons.append((0.05, "ארוחה פשוטה ומהירה"))

    # 7. freshness/variety.
    meal_key = _free_text_preference_key(str(getattr(meal, "name", "") or ""))
    rejected = recently_rejected_keys or set()
    if meal_key and meal_key in rejected:
        reasons.append((-0.4, "הוצע/נדחה לאחרונה"))

    total = sum(weight for weight, _ in reasons)
    total = max(-1.0, min(1.0, total))
    best_reason = max((r for r in reasons if r[1]), key=lambda r: r[0], default=(0.0, ""))[1]
    return MealScore(value=total, reason=best_reason)
