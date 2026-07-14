"""TASK-7 — the daily menu is a chronological time-based eating schedule.

Each entry: time — meal-context / calories | protein / components. No
"ארוחה 1/2/3" numbering; entries ordered by time; no default-target warning
when a confirmed goal exists; no generic closing text.
"""
from __future__ import annotations

from noam_coach.services import health_jobs
from noam_coach.services.goals import format_morning_menu
from recommendations import MenuMeal, MorningMenu


def test_schedule_format_time_context_macros_components() -> None:
    menu = MorningMenu(
        headline="תפריט להיום",
        meals=[
            MenuMeal(name="ארוחת בוקר", time_hint="08:00", calories=600, protein=46, note="חביתה וסלט"),
            MenuMeal(name="ארוחת צהריים", time_hint="12:30", calories=770, protein=59, note="עוף ואורז"),
        ],
    )
    text = format_morning_menu(menu)
    assert "08:00 — " in text
    assert "12:30 — " in text
    assert "600 קל׳ | 46 ג׳ חלבון" in text
    assert "חביתה וסלט" in text
    assert "ארוחה 1" not in text and "ארוחה 2" not in text


def test_entries_sorted_chronologically() -> None:
    menu = MorningMenu(
        headline="תפריט להיום",
        meals=[
            MenuMeal(name="ארוחת ערב", time_hint="20:30", calories=500, protein=40),
            MenuMeal(name="ארוחת בוקר", time_hint="08:00", calories=400, protein=30),
        ],
    )
    text = format_morning_menu(menu)
    assert text.index("08:00") < text.index("20:30")


def test_numbering_stripped_from_name() -> None:
    menu = MorningMenu(
        headline="תפריט להיום",
        meals=[MenuMeal(name="ארוחה 2 - ארוחת צהריים", time_hint="12:30", calories=700, protein=55)],
    )
    text = format_morning_menu(menu)
    assert "ארוחה 2" not in text
    assert "ארוחת צהריים" in text


def test_no_generic_closing_text() -> None:
    menu = MorningMenu(
        headline="תפריט להיום",
        meals=[MenuMeal(name="ארוחת בוקר", time_hint="08:00", calories=400, protein=30)],
        closing="בהצלחה ושיהיה יום נעים!",
    )
    text = format_morning_menu(menu)
    assert "בהצלחה" not in text  # TASK-7: no generic closing


class _Ctx:
    def __init__(self, goal_computed: bool, goal: dict, calorie_target: int, protein_target: int) -> None:
        self.goal_computed = goal_computed
        self.goal = goal
        self.calorie_target = calorie_target
        self.protein_target = protein_target


def test_no_default_warning_when_confirmed_goal_exists() -> None:
    ctx = _Ctx(False, {"status": "active"}, 2100, 165)
    assert "היעדים עדיין לא חושבו" not in health_jobs._data_quality_disclaimer(ctx)


def test_default_warning_still_shown_for_default_goal() -> None:
    ctx = _Ctx(False, {"status": "default"}, 2000, 150)
    assert "היעדים עדיין לא חושבו" in health_jobs._data_quality_disclaimer(ctx)
