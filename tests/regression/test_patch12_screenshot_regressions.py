from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_profile_renderer_is_not_debug_dump() -> None:
    src = read("noam_coach/bot/onboarding.py")
    profile_block = src[src.index("async def build_profile_text"):src.index("async def command_cancel")]
    assert "דווח על ידך" not in profile_block
    assert "זמינות פעילה לתוכנית" in profile_block
    assert "HealthKit נשמר כרמז" in profile_block
    assert "weekly_availability" not in profile_block.split("_add_block", 4)[-1]


def test_nutrition_completion_returns_to_nutrition_actions() -> None:
    src = read("noam_coach/bot/onboarding.py")
    assert "async def render_plan_completion_done" in src
    done_block = src[src.index("async def render_plan_completion_done"):src.index("async def ask_next_plan_completion_question")]
    assert "לא אעבור עכשיו לשאלות אימון" in done_block
    assert "menu:daily_menu" in done_block
    assert "menu:nextmeal" in done_block
    assert "planv2:generate:workout" in done_block


def test_abc_request_does_not_drop_four_day_availability() -> None:
    src = read("noam_coach/bot/assistant.py")
    block = src[src.index("def requested_split_frequency"):src.index("async def _split_availability_gate")]
    assert "return 4" in block
    assert "ABC + Full Body" in src
    plan_src = read("planning.py")
    assert '"performance": _PERFORMANCE_SPLIT_OVERRIDES' in plan_src
    assert '4: ["A", "B", "C", "F"]' in plan_src


def test_weekly_plan_output_contains_professional_exercise_details() -> None:
    src = read("noam_coach/bot/onboarding.py")
    start = src.index("def format_weekly_plan")
    block = src[start:src.index("_CANCEL_WORDS", start)]
    assert "RIR 2" in block
    assert "מנוחה" in block
    assert "דגש" in block
    assert "PLANS.get" in block


def test_telegram_errors_are_not_sent_raw_to_chat() -> None:
    src = read("noam_coach/bot/callback_router.py")
    admin_block = src[src.index("if decision.notify_admin") : src.index("if isinstance(update, Update)")]
    assert "Telegram error" not in admin_block
    assert "safe_error" not in admin_block
    assert "traceback" in admin_block.lower()


def test_nutrition_strategy_screen_is_not_daily_menu() -> None:
    src = read("noam_coach/bot/onboarding.py")
    assert "בחירת סגנון תזונה — חד־פעמי" in src
    assert "זה לא תפריט יומי" in src
    assert "בחר סגנון" in src
