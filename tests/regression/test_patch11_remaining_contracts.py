from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_strategy_split_override_exists_for_four_day_plans() -> None:
    src = read("planning.py")
    assert "def _strategy_split_override" in src
    assert "FB1" in src and "FB4" in src
    assert "U1" in src and "L2" in src
    assert "ABC + Full Body" in src


def test_next_meal_text_correction_keeps_focused_action_rows() -> None:
    src = read("noam_coach/bot/meal_text.py")
    correction_block = src.split("correction = await handle_recommendation_correction", 1)[1]
    correction_block = correction_block.split("# Workout text and ordinary free text", 1)[0]
    assert "next_meal_action_rows(recommendation)" in correction_block
    assert "keyboard_rows.append" not in correction_block
    assert "menu:home" not in correction_block


def test_daily_menu_edit_text_is_connected_before_generic_router() -> None:
    src = read("noam_coach/bot/meal_text.py")
    assert "try_build_daily_menu_edit_reply" in src
    assert src.index("try_build_daily_menu_edit_reply") < src.rindex("route_free_text(update, user_id)")
    service = read("noam_coach/services/daily_menu_edit.py")
    assert "parse_daily_menu_edit" in service
    assert "daily_menu_last_edit_request" in service
    assert "זה עדיין לא נספר כאכילה בפועל" in service


def test_daily_menu_replace_button_has_real_text_followup_path() -> None:
    callback_src = read("noam_coach/bot/callback_menu.py")
    assert "menu:replace_daily_meal" in callback_src
    service_src = read("noam_coach/services/daily_menu_edit.py")
    for phrase in ["תחליף", "החלף", "תשנה", "רענן"]:
        assert phrase in service_src
