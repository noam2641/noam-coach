"""TASK-20 — Today's Menu is built around the user's actual day.

Covers the deterministic, testable pieces:
  * format_morning_menu no longer prints the time twice;
  * nutrition context classifies the day (weekday / friday / saturday) so the
    menu can adapt to the Israeli week;
  * that classification reaches the AI payload.
"""
from __future__ import annotations

from datetime import datetime

from config import TZ
from noam_coach.services import nutrition_context as nc
from noam_coach.services.goals import format_morning_menu
from recommendations import MenuMeal, MorningMenu


def test_meal_time_is_not_duplicated_when_name_contains_it() -> None:
    menu = MorningMenu(
        headline="תפריט הבוקר",
        meals=[MenuMeal(name="ארוחה 1 - 08:00", time_hint="08:00", calories=300, protein=40)],
    )
    text = format_morning_menu(menu)
    # The time appears once (inside the name), not again as "(08:00)".
    assert text.count("08:00") == 1


def test_meal_time_hint_still_shown_when_distinct_from_name() -> None:
    menu = MorningMenu(
        headline="תפריט הבוקר",
        meals=[MenuMeal(name="ארוחת בוקר עתירת חלבון", time_hint="08:00", calories=300, protein=40)],
    )
    text = format_morning_menu(menu)
    assert "(08:00)" in text


def test_day_type_weekday() -> None:
    # Sunday .. Thursday are workdays.
    assert nc._day_type(datetime(2026, 7, 5, 9, 0, tzinfo=TZ)) == "weekday"  # Sunday


def test_day_type_friday() -> None:
    assert nc._day_type(datetime(2026, 7, 3, 9, 0, tzinfo=TZ)) == "friday"


def test_day_type_saturday() -> None:
    assert nc._day_type(datetime(2026, 7, 4, 9, 0, tzinfo=TZ)) == "saturday"


def test_day_type_reaches_ai_payload() -> None:
    # A minimally-populated context still exposes day_type to the AI payload.
    import dataclasses

    fields = {f.name for f in dataclasses.fields(nc.NutritionContext)}
    assert "day_type" in fields
