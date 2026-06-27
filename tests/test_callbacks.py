from __future__ import annotations

import coach_bot


def test_workout_callback_rejects_missing_or_stale_state() -> None:
    session = {"id": 10, "exercise_index": 2, "set_number": 3}
    assert not coach_bot.is_current_session_step(["skip", "10"], session)
    assert not coach_bot.is_current_session_step(["skip", "10", "1", "3"], session)
    assert not coach_bot.is_current_session_step(["skip", "10", "2", "2"], session)
    assert coach_bot.is_current_session_step(["skip", "10", "2", "3"], session)


def test_workout_callback_fits_telegram_limit() -> None:
    session = {"id": 999999999, "exercise_index": 99, "set_number": 99}
    data = coach_bot.session_action_data("painlevel", session, 10)
    assert len(data.encode("utf-8")) <= 64
