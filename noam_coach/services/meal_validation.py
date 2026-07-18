"""Validation gates for AI-produced meal analyses."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import user_model
from models import MealAnalysis
from noam_coach.services.dietary_restrictions import (
    load_restrictions_from_facts,
    validate_meal_restrictions,
)


@dataclass(frozen=True)
class MealValidationIssue:
    code: str
    severity: str
    message: str
    item_name: str | None = None


@dataclass(frozen=True)
class MealValidationResult:
    issues: list[MealValidationIssue] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return any(issue.severity == "block" for issue in self.issues)

    @property
    def warnings(self) -> list[MealValidationIssue]:
        return [issue for issue in self.issues if issue.severity != "block"]


def validate_meal_analysis(
    analysis: MealAnalysis,
    *,
    diet_restrictions: Any = None,
    allergies: Any = None,
) -> MealValidationResult:
    issues: list[MealValidationIssue] = []
    if not analysis.is_meaningful():
        issues.append(
            MealValidationIssue(
                code="empty_meal",
                severity="block",
                message="אי אפשר לשמור ארוחה ללא מזון או ערכים תזונתיים",
            )
        )

    restrictions = load_restrictions_from_facts(
        str(diet_restrictions) if diet_restrictions not in (None, "", "none") else None,
        str(allergies) if allergies not in (None, "", "none") else None,
    )
    violations = validate_meal_restrictions(
        [{"item_name": item.name} for item in analysis.items],
        restrictions,
    )
    for violation in violations:
        restriction = violation["restriction"]
        action = str(violation["action"])
        item_name = str(violation["item_name"])
        label = restriction.user_label or restriction.canonical_id
        if action == "block":
            issues.append(
                MealValidationIssue(
                    code="restricted_food_block",
                    severity="block",
                    item_name=item_name,
                    message=f"{item_name} מתנגש עם אלרגיה/רגישות: {label}",
                )
            )
        elif action == "warn":
            issues.append(
                MealValidationIssue(
                    code="restricted_food_warning",
                    severity="warn",
                    item_name=item_name,
                    message=f"{item_name} רשום אצלך כהימנעות: {label}",
                )
            )
        elif action == "substitute":
            issues.append(
                MealValidationIssue(
                    code="unavailable_food",
                    severity="warn",
                    item_name=item_name,
                    message=f"{item_name} מסומן כלא זמין כרגע: {label}",
                )
            )

    totals = analysis.totals()
    if totals["protein"] * 4 > totals["calories"] + 15:
        issues.append(
            MealValidationIssue(
                code="implausible_protein_calories",
                severity="block",
                message="סך החלבון גבוה מסך הקלוריות האפשרי בארוחה",
            )
        )

    # Audit F-A2: canonical quantity/plausibility rules — a serving count
    # written into the grams field ("3 שניצלים" → 3 גרם), impossible
    # protein/energy densities and zero-energy solids can never persist.
    # The rules live in meal_plausibility (versioned, reusable); flowing
    # them through this result means BOTH the card render and the persist
    # transaction enforce them with no extra wiring.
    from noam_coach.services.meal_plausibility import check_analysis

    for finding in check_analysis(analysis.items):
        issues.append(
            MealValidationIssue(
                code=finding.code,
                severity=finding.severity,
                item_name=finding.item_name,
                message=finding.message,
            )
        )
    return MealValidationResult(issues=issues)


async def validate_meal_analysis_for_user(db: Any, user_id: int, analysis: MealAnalysis) -> MealValidationResult:
    diet_restrictions = await user_model.get_value(db, user_id, "diet_restrictions")
    allergies = await user_model.get_value(db, user_id, "allergies")
    return validate_meal_analysis(
        analysis,
        diet_restrictions=diet_restrictions,
        allergies=allergies,
    )
