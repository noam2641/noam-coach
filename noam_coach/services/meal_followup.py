"""Follow-up decisions for planned meals that were not logged."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from config import TZ


@dataclass(frozen=True)
class PlannedMealFollowup:
    key: str
    meal_name: str
    planned_at: datetime
    text: str


def _parse_local_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ)
    return parsed.astimezone(TZ)


def _norm(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _meal_matches_any_consumed(planned_name: str, consumed_names: set[str]) -> bool:
    """Text-log FALLBACK matcher only (B12/ARCH-13 closes FIX 49).

    The PRIMARY resolver is now the persisted planned-meal lifecycle: a
    planned meal saved through the recommendation flow transitions to
    status=consumed by fingerprint identity and never reaches this function
    (nutrition_context surfaces only effectively-planned entries). What
    remains here is the genuinely fuzzy case — the user logged the meal as
    FREE TEXT/photo with a different title ("חזה עוף עם אורז" planned,
    "חזה עוף" logged) where no fingerprint exists to match; substring
    containment either direction keeps the follow-up from nagging about a
    meal that was clearly already eaten.
    """
    if not planned_name:
        return False
    for consumed in consumed_names:
        if not consumed:
            continue
        if planned_name == consumed or planned_name in consumed or consumed in planned_name:
            return True
    return False


def planned_meal_followup(
    nutrition_context: Any,
    *,
    now: datetime | None = None,
    grace_minutes: int = 120,
) -> PlannedMealFollowup | None:
    current = (now or datetime.now(TZ)).astimezone(TZ)
    consumed_names = {_norm(meal.name) for meal in getattr(nutrition_context, "reported_meals", [])}
    consumed_names |= {_norm(getattr(meal, "name", "")) for meal in getattr(nutrition_context, "reported_meals", [])}

    candidates: list[tuple[datetime, dict[str, Any]]] = []
    for meal in getattr(nutrition_context, "planned_meals", []) or []:
        if not isinstance(meal, dict) or meal.get("source") != "next_meal_plan":
            continue
        name = _norm(meal.get("name"))
        if not name or _meal_matches_any_consumed(name, consumed_names):
            continue
        planned_at = _parse_local_dt(meal.get("planned_at"))
        if not planned_at:
            continue
        if current - planned_at < timedelta(minutes=grace_minutes):
            continue
        candidates.append((planned_at, meal))

    if not candidates:
        return None
    planned_at, meal = sorted(candidates, key=lambda item: item[0])[0]
    name = str(meal.get("name") or "הארוחה שתכננו")
    fingerprint = str(meal.get("fingerprint") or name)
    stable_key = hashlib.sha1(fingerprint.encode("utf-8")).hexdigest()[:10]
    return PlannedMealFollowup(
        key=f"planned_meal_followup_{stable_key}",
        meal_name=name,
        planned_at=planned_at,
        text=(
            f"תכננו את <b>{name}</b> להמשך היום, ועדיין לא ראיתי שתיעדת אותה.\n"
            "אכלת אותה, דחית, או שנבנה משהו אחר שמתאים יותר לעכשיו?"
        ),
    )
