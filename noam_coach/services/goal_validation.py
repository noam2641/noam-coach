"""Goal-weight validation helpers.

The user can accidentally type a very low target (for example 50 instead of
83).  Store normal targets immediately, but require an explicit confirmation
for unusually large changes relative to the current weight.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GoalWeightValidation:
    needs_confirmation: bool
    message: str = ""


def validate_goal_weight(target_kg: float, current_weight_kg: float | None) -> GoalWeightValidation:
    target = float(target_kg)
    if current_weight_kg is None or current_weight_kg <= 0:
        if target < 45:
            return GoalWeightValidation(
                True,
                f'כתבת יעד של {target:g} ק״ג. זה יעד נמוך מאוד — תאשר שזה באמת היעד או כתוב מספר אחר.',
            )
        return GoalWeightValidation(False)

    current = float(current_weight_kg)
    delta = abs(current - target)
    pct = delta / current
    if target < current * 0.65 or target < 50 or pct >= 0.35:
        direction = "ירידה" if target < current else "עלייה"
        return GoalWeightValidation(
            True,
            (
                f'כתבת יעד של {target:g} ק״ג, כשהמשקל הנוכחי השמור הוא {current:g} ק״ג. '
                f'זו {direction} גדולה מאוד ({delta:g} ק״ג). כדי לא לשמור טעות הקלדה, '
                f'כתוב שוב: כן {target:g} — או כתוב יעד אחר, למשל 83.'
            ),
        )
    return GoalWeightValidation(False)


def is_explicit_goal_weight_confirmation(text: str, target_kg: float) -> bool:
    cleaned = str(text or "").strip().lower().replace("ק\"ג", "").replace("קג", "")
    if not cleaned.startswith(("כן", "מאשר", "אישור", "נכון")):
        return False
    return str(int(target_kg)) in cleaned or f"{target_kg:g}" in cleaned
