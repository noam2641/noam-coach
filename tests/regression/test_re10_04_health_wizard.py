"""RE10-4 regression tests — per-fact confirmation wizard after Health import.

Covers:
  * ask_next_health_confirm_step walks pending facts in the documented order.
  * Confirming a fact hardens it (confirmed=1) and advances to the next one.
  * "ציין אחרת" (edit) accepts free text and persists it as a user-sourced fact.
  * "skip the rest" and "reaching the end" both finish the wizard and run the
    correct continuation (onboarding vs reconciliation).
  * The old bulk gate (health:activate / pending_import_facts) still works
    for the health:review path — RE10-4 adds the wizard, it does not remove it.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest
from telegram.error import BadRequest

import coach_bot
import user_model
from db import Database
from helpers import utc_now
from noam_coach.bot import callback_menu as callback_menu_bot
from noam_coach.bot import onboarding as onboarding_bot
from noam_coach.services import core as core_services
from noam_coach.services import health_jobs


class FakeMessage:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.markups: list[Any] = []

    async def reply_text(self, text: str, reply_markup: Any = None, parse_mode: str | None = None) -> None:
        del parse_mode
        self.texts.append(text)
        self.markups.append(reply_markup)


class FakeTarget:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.reply_markups: list[Any] = []
        self.message = FakeMessage()

    async def edit_message_text(self, text: str, reply_markup: Any = None, parse_mode: str | None = None) -> None:
        del parse_mode
        self.messages.append(text)
        self.reply_markups.append(reply_markup)

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        del text, show_alert


class StaleEditTarget(FakeTarget):
    async def edit_message_text(self, text: str, reply_markup: Any = None, parse_mode: str | None = None) -> None:
        del text, reply_markup, parse_mode
        raise BadRequest("Message to edit not found")


async def _make_db(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "health_wizard.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'Test',NULL,?)",
        (utc_now(),),
    )
    return db


def _patch_db(monkeypatch: pytest.MonkeyPatch, db: Database) -> None:
    monkeypatch.setattr(coach_bot, "DB", db)
    monkeypatch.setattr(health_jobs, "DB", db)
    monkeypatch.setattr(onboarding_bot, "DB", db)
    monkeypatch.setattr(core_services, "DB", db)
    monkeypatch.setattr(callback_menu_bot, "DB", db)


async def _seed_pending_import(db: Database) -> None:
    await user_model.set_fact(
        db, 1, "workout_pattern", {"weekly_frequency": 3, "typical_hour": "18:30"},
        kind=user_model.KIND_ESTIMATE, source=user_model.SOURCE_DERIVED, confirmed=False,
    )
    await user_model.set_fact(
        db, 1, "weight_kg", 101.8,
        kind=user_model.KIND_FACT, source=user_model.SOURCE_APPLE_HEALTH, confirmed=False,
    )
    await user_model.set_fact(
        db, 1, "sleep_schedule", {"typical_bedtime": "23:15", "typical_wake_time": "06:45"},
        kind=user_model.KIND_ESTIMATE, source=user_model.SOURCE_DERIVED, confirmed=False,
    )


@pytest.mark.asyncio
async def test_wizard_walks_facts_in_documented_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await _seed_pending_import(db)

    target = FakeTarget()
    started = await health_jobs.ask_next_health_confirm_step(target, 1)
    assert started is True
    assert "אימונים בשבוע" in target.messages[-1]  # workout_pattern first


@pytest.mark.asyncio
async def test_wizard_sends_new_message_when_edit_target_is_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await _seed_pending_import(db)

    target = StaleEditTarget()
    started = await health_jobs.ask_next_health_confirm_step(target, 1)

    assert started is True
    assert target.messages == []
    assert target.message.texts
    assert "3" in target.message.texts[-1]


@pytest.mark.asyncio
async def test_confirming_each_item_separately_and_advancing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """RE12: frequency and hour are confirmed as SEPARATE steps; only after
    the last applicable workout sub-step is the pattern fact hardened."""
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await _seed_pending_import(db)

    target = FakeTarget()
    await health_jobs.ask_next_health_confirm_step(target, 1)
    # Step 1: weekly frequency (detected value shown, uniform phrasing).
    assert "אימונים בשבוע" in target.messages[-1]
    assert "האם לאשר" in target.messages[-1]

    handled = await callback_menu_bot.handle_menu_callback(
        target, 1, "health:confirm:workout_pattern.frequency"
    )
    assert handled is True
    training_days = await user_model.get_fact(db, 1, "training_days_per_week")
    assert training_days["value"] == 3
    assert training_days["confirmed"] is True
    # Pattern is NOT confirmed yet — the hour step is still pending.
    fact = await user_model.get_fact(db, 1, "workout_pattern")
    assert fact["confirmed"] is False
    assert "שעת אימון" in target.messages[-1]

    handled = await callback_menu_bot.handle_menu_callback(
        target, 1, "health:confirm:workout_pattern.hour"
    )
    assert handled is True
    window = await user_model.get_fact(db, 1, "workout_window")
    assert window["value"] == "18:30"
    assert window["confirmed"] is True
    fact = await user_model.get_fact(db, 1, "workout_pattern")
    assert fact["confirmed"] is True
    # Advanced to the next fact (weight_kg), echoing what was just approved.
    assert 'ק"ג' in target.messages[-1]
    assert "אושר" in target.messages[-1]


@pytest.mark.asyncio
async def test_detected_training_days_get_their_own_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RE12: detected training days are surfaced for explicit approval, and
    approving them feeds the plan's weekly_availability."""
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await user_model.set_fact(
        db, 1, "workout_pattern",
        {
            "weekly_frequency": 3,
            "typical_hour": "18:30",
            "common_weekdays": [6, 1, 3],  # Sun, Tue, Thu (Monday-first)
            "weekday_schema": "monday_first_v1",
        },
        kind=user_model.KIND_ESTIMATE, source=user_model.SOURCE_DERIVED, confirmed=False,
    )
    await _seed_health_workout_days(db, [6, 1, 3])

    target = FakeTarget()
    await health_jobs.ask_next_health_confirm_step(target, 1)
    await callback_menu_bot.handle_menu_callback(
        target, 1, "health:confirm:workout_pattern.frequency"
    )
    # Step 2: the proposed training days are shown by name and offered for approval.
    days_prompt = target.messages[-1]
    assert "היסטוריית האימונים" in days_prompt
    assert "ראשון" in days_prompt and "שלישי" in days_prompt and "חמישי" in days_prompt

    handled = await callback_menu_bot.handle_menu_callback(
        target, 1, "health:confirm:workout_pattern.days"
    )
    assert handled is True
    availability = await user_model.get_fact(db, 1, "weekly_availability")
    assert availability["confirmed"] is True
    weekdays = sorted(slot["weekday"] for slot in availability["value"])
    assert weekdays == [1, 3, 6]

    # Hour step still follows; after it the pattern hardens.
    await callback_menu_bot.handle_menu_callback(
        target, 1, "health:confirm:workout_pattern.hour"
    )
    fact = await user_model.get_fact(db, 1, "workout_pattern")
    assert fact["confirmed"] is True


@pytest.mark.asyncio
async def test_edit_persists_user_value_and_advances(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await _seed_pending_import(db)

    target = FakeTarget()
    await health_jobs.ask_next_health_confirm_step(target, 1)

    class FakeUpdate:
        effective_message = FakeMessage()
        effective_message.text = "4"  # type: ignore[attr-defined]

    handled = await onboarding_bot.handle_onboarding_text(FakeUpdate(), 1)
    assert handled is True
    # RE12: a typed number at the frequency step corrects the frequency INSIDE
    # the pattern (not clobbering the whole dict) and records the plan fact.
    fact = await user_model.get_fact(db, 1, "workout_pattern")
    assert fact["value"]["weekly_frequency"] == 4.0
    assert fact["value"]["typical_hour"] == "18:30"
    training_days = await user_model.get_fact(db, 1, "training_days_per_week")
    assert training_days["value"] == 4
    assert training_days["source"] == user_model.SOURCE_USER
    assert training_days["confirmed"] is True


@pytest.mark.asyncio
async def test_health_weight_edit_rejects_sleep_window_without_saving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await user_model.set_fact(
        db, 1, "weight_kg", 101.8,
        kind=user_model.KIND_FACT,
        source=user_model.SOURCE_APPLE_HEALTH,
        confirmed=False,
    )
    await onboarding_bot.set_pending(1, "__health_edit_weight_kg__")

    class FakeUpdate:
        effective_message = FakeMessage()
        effective_message.text = "00:20-06:50"  # type: ignore[attr-defined]

    handled = await onboarding_bot.handle_onboarding_text(FakeUpdate(), 1)

    assert handled is True
    fact = await user_model.get_fact(db, 1, "weight_kg")
    assert fact["value"] == 101.8
    assert fact["source"] == user_model.SOURCE_APPLE_HEALTH
    assert fact["confirmed"] is False
    assert FakeUpdate.effective_message.texts


@pytest.mark.asyncio
async def test_health_weight_edit_stores_numeric_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await onboarding_bot.set_pending(1, "__health_edit_weight_kg__")

    async def no_next_step(*_args: Any, **_kwargs: Any) -> bool:
        return False

    async def finish_noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(health_jobs, "ask_next_health_confirm_step", no_next_step)
    monkeypatch.setattr(health_jobs, "finish_health_confirm_wizard", finish_noop)

    class FakeUpdate:
        effective_message = FakeMessage()
        effective_message.text = "101.8"  # type: ignore[attr-defined]

    handled = await onboarding_bot.handle_onboarding_text(FakeUpdate(), 1)

    assert handled is True
    fact = await user_model.get_fact(db, 1, "weight_kg")
    assert fact["value"] == 101.8
    assert fact["source"] == user_model.SOURCE_USER
    assert fact["confirmed"] is True


@pytest.mark.asyncio
async def test_health_sleep_edit_stores_structured_schedule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await onboarding_bot.set_pending(1, "__health_edit_sleep_schedule__")

    async def no_next_step(*_args: Any, **_kwargs: Any) -> bool:
        return False

    async def finish_noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(health_jobs, "ask_next_health_confirm_step", no_next_step)
    monkeypatch.setattr(health_jobs, "finish_health_confirm_wizard", finish_noop)

    class FakeUpdate:
        effective_message = FakeMessage()
        effective_message.text = "00:20-06:50"  # type: ignore[attr-defined]

    handled = await onboarding_bot.handle_onboarding_text(FakeUpdate(), 1)

    assert handled is True
    fact = await user_model.get_fact(db, 1, "sleep_schedule")
    # P1.1b: the onboarding sleep-edit writer now stores the CANONICAL shape.
    assert fact["value"] == {
        "bedtime": "00:20",
        "wake_time": "06:50",
    }
    assert fact["source"] == user_model.SOURCE_USER
    assert fact["confirmed"] is True


@pytest.mark.asyncio
async def test_edit_training_days_by_hebrew_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await user_model.set_fact(
        db, 1, "workout_pattern",
        {
            "weekly_frequency": 3,
            "typical_hour": "18:30",
            "common_weekdays": [0, 2],
            "weekday_schema": "monday_first_v1",
        },
        kind=user_model.KIND_ESTIMATE, source=user_model.SOURCE_DERIVED, confirmed=False,
    )

    target = FakeTarget()
    await health_jobs.ask_next_health_confirm_step(target, 1)
    await callback_menu_bot.handle_menu_callback(
        target, 1, "health:confirm:workout_pattern.frequency"
    )
    assert "כתוב את הימים המועדפים" in target.messages[-1]

    class FakeUpdate:
        effective_message = FakeMessage()
        effective_message.text = "ראשון, שלישי, חמישי"  # type: ignore[attr-defined]

    handled = await onboarding_bot.handle_onboarding_text(FakeUpdate(), 1)
    assert handled is True
    availability = await user_model.get_fact(db, 1, "weekly_availability")
    assert availability["confirmed"] is True
    weekdays = sorted(slot["weekday"] for slot in availability["value"])
    assert weekdays == [1, 3, 6]  # Tue, Thu, Sun in Monday-first indices
    fact = await user_model.get_fact(db, 1, "workout_pattern")
    assert sorted(fact["value"]["common_weekdays"]) == [1, 3, 6]


@pytest.mark.asyncio
async def test_skip_wizard_finishes_and_runs_reconciliation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await _seed_pending_import(db)
    await core_services.set_flow_state(
        1, health_jobs.HEALTH_POST_WIZARD_FLOW, "reconciliation", {"summary_text": "<b>סיכום</b>"}
    )

    target = FakeTarget()
    handled = await callback_menu_bot.handle_menu_callback(target, 1, "health:skip_wizard")
    assert handled is True
    assert "סיכום הייבוא" in target.messages[-1] or "סיכום" in target.messages[-1]


@pytest.mark.asyncio
async def test_skip_item_defers_only_current_question(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await user_model.set_fact(
        db, 1, "workout_pattern", {"weekly_frequency": 2, "typical_hour": "18:30"},
        kind=user_model.KIND_ESTIMATE, source=user_model.SOURCE_DERIVED, confirmed=False,
    )
    await user_model.set_fact(
        db, 1, "weight_kg", 101.8,
        kind=user_model.KIND_FACT, source=user_model.SOURCE_APPLE_HEALTH, confirmed=False,
    )

    target = FakeTarget()
    await health_jobs.ask_next_health_confirm_step(target, 1)
    assert onboarding_bot.PENDING_QUESTION[1] == (
        f"__health_edit_{health_jobs.WIZARD_STEP_WORKOUT_FREQUENCY}__"
    )

    handled = await callback_menu_bot.handle_menu_callback(target, 1, "health:skip_item")
    assert handled is True
    assert onboarding_bot.PENDING_QUESTION[1] == (
        f"__health_edit_{health_jobs.WIZARD_STEP_WORKOUT_HOUR}__"
    )

    await callback_menu_bot.handle_menu_callback(
        target, 1, f"health:confirm:{health_jobs.WIZARD_STEP_WORKOUT_HOUR}"
    )
    assert onboarding_bot.PENDING_QUESTION[1] == "__health_edit_weight_kg__"

    await callback_menu_bot.handle_menu_callback(target, 1, "health:confirm:weight_kg")
    assert onboarding_bot.PENDING_QUESTION[1] == (
        f"__health_edit_{health_jobs.WIZARD_STEP_WORKOUT_FREQUENCY}__"
    )


@pytest.mark.asyncio
async def test_finish_wizard_does_not_run_legacy_onboarding_basics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TASK-1: the new post-import summary + reduced menu is terminal on the
    onboarding path; the legacy base-data confirmation (show_onboarding_basics)
    must NOT run afterwards (it duplicated weight/body-fat/base data)."""
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)

    calls: list[str] = []

    async def fake_show_onboarding_basics(_message: Any, _user_id: int) -> None:
        calls.append("onboarding")

    monkeypatch.setattr(onboarding_bot, "show_onboarding_basics", fake_show_onboarding_basics)

    await core_services.set_flow_state(1, health_jobs.HEALTH_POST_WIZARD_FLOW, "onboarding", {"summary_text": "x"})
    target = FakeTarget()
    await health_jobs.finish_health_confirm_wizard(target, 1)
    assert calls == []


@pytest.mark.asyncio
async def test_bulk_gate_still_available_via_health_review(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """RE10-4 adds the wizard; it must not remove the existing bulk activation path."""
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await _seed_pending_import(db)

    pending = await health_jobs.pending_import_facts(1)
    assert len(pending) == 3
    activated = await health_jobs.activate_imported_health_facts(1)
    assert activated == 3


# ---------------------------------------------------------------------------
# The import wizard never offers a computed calorie target for confirmation,
# and never shows a placeholder value.
# ---------------------------------------------------------------------------


async def _all_wizard_step_keys(db: Database) -> list[str]:
    """Every step the wizard would offer, by marking each returned step done
    and asking for the next — without needing the confirm-callback plumbing."""
    keys: list[str] = []
    done: list[str] = []
    for _ in range(20):  # generous cap; the wizard is short
        step = await health_jobs._next_wizard_step(1, done)
        if step is None:
            break
        step_id, _fact = step
        keys.append(step_id)
        done.append(step_id)
    return keys


@pytest.mark.asyncio
async def test_wizard_never_offers_calorie_target_placeholder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pending calorie_target with no real value (only its label) must never
    be surfaced for confirmation — that produced 'זוהה יעד קלוריות: יעד קלוריות'."""
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await _seed_pending_import(db)
    # A computed calorie target that leaked in as a pending import fact, with a
    # non-numeric value (the exact broken state seen in the bug).
    await user_model.set_fact(
        db, 1, "calorie_target", "יעד קלוריות",
        kind=user_model.KIND_ESTIMATE, source=user_model.SOURCE_DERIVED, confirmed=False,
    )

    step_keys = await _all_wizard_step_keys(db)
    # calorie_target is never a wizard step (real workout/weight/sleep steps are).
    assert "calorie_target" not in step_keys
    assert step_keys  # the real facts are still offered

    # And the placeholder text never renders on any real prompt.
    target = FakeTarget()
    await health_jobs.ask_next_health_confirm_step(target, 1)
    shown = "\n".join(target.messages + target.message.texts)
    assert "יעד קלוריות: יעד קלוריות" not in shown


@pytest.mark.asyncio
async def test_wizard_next_step_skips_excluded_and_placeholder_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    # Only a computed calorie_target is pending — the wizard has nothing real
    # to confirm, so it must not start with a placeholder step.
    await user_model.set_fact(
        db, 1, "calorie_target", 2100,
        kind=user_model.KIND_ESTIMATE, source=user_model.SOURCE_DERIVED, confirmed=False,
    )
    step = await health_jobs._next_wizard_step(1, [])
    assert step is None


# ---------------------------------------------------------------------------
# Training-day proposal matches the requested weekly frequency (never fewer).
# ---------------------------------------------------------------------------


async def _seed_two_detected_days_and_desired_four(db: Database) -> None:
    """User asked for 4 workouts/week, HealthKit only found 2 recurring days."""
    await user_model.set_fact(
        db, 1, "workout_pattern",
        {
            "weekly_frequency": 2,
            "typical_hour": "18:30",
            "common_weekdays": [0, 2],  # Mon, Wed (Monday-first)
            "weekday_schema": "monday_first_v1",
        },
        kind=user_model.KIND_ESTIMATE, source=user_model.SOURCE_DERIVED, confirmed=False,
    )
    await user_model.set_fact(
        db, 1, "training_days_per_week", 4,
        kind=user_model.KIND_FACT, source=user_model.SOURCE_USER, confirmed=True,
    )


async def _seed_health_workout_days(
    db: Database,
    weekdays: list[int],
    *,
    weeks: int = 4,
    start_monday: dt.date = dt.date(2026, 5, 4),
) -> None:
    """Seed real HealthKit workout rows. Weekday indices follow date.weekday()."""
    created_at = utc_now()
    for week in range(weeks):
        monday = start_monday + dt.timedelta(days=week * 7)
        for weekday in weekdays:
            start = dt.datetime.combine(
                monday + dt.timedelta(days=weekday),
                dt.time(18, 30, tzinfo=dt.timezone.utc),
            )
            end = start + dt.timedelta(minutes=55)
            await db.execute(
                """
                INSERT INTO health(
                    user_id, external_id, sample_type, value, unit,
                    start_time, end_time, source_device, created_at
                )
                VALUES(?, ?, 'workout', ?, 'min', ?, ?, 'apple_watch', ?)
                """,
                (
                    1,
                    f"workout-{start.date().isoformat()}-{weekday}",
                    55,
                    start.isoformat(),
                    end.isoformat(),
                    created_at,
                ),
            )


@pytest.mark.asyncio
async def test_days_step_proposes_four_days_when_user_wants_four(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The user asked for 4 workouts; expand only from real workout history."""
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await _seed_two_detected_days_and_desired_four(db)
    await _seed_health_workout_days(db, [6, 0, 2, 4])

    target = FakeTarget()
    # Frequency is already answered (manual 4), so the wizard advances to the
    # days step. Mark the frequency sub-step done via the shared helper.
    await onboarding_bot.set_flow_state(
        1, health_jobs.HEALTH_CONFIRM_FLOW, health_jobs.WIZARD_STEP_WORKOUT_DAYS,
        {"done": ["workout_pattern.frequency"]},
    )
    await health_jobs.ask_next_health_confirm_step(target, 1)
    prompt = target.messages[-1]

    assert "היסטוריית האימונים" in prompt
    assert "הימים שבהם התאמנת הכי הרבה" in prompt
    assert "השלמתי" not in prompt
    # The stored proposal now has exactly the 4 real days from HealthKit history.
    fact = await user_model.get_fact(db, 1, "workout_pattern")
    assert fact["value"]["common_weekdays"] == [6, 0, 2, 4]
    assert fact["value"]["weekday_selection_basis"] == "health_workout_history"
    # A confirm button IS shown (the list matches the requested frequency).
    buttons = [b.callback_data for row in target.reply_markups[-1].inline_keyboard for b in row]
    assert "health:confirm:workout_pattern.days" in buttons


@pytest.mark.asyncio
async def test_days_step_expands_past_sparse_recent_oneoffs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One-off recent workout days must not override an older recurring pattern."""
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await _seed_two_detected_days_and_desired_four(db)
    await _seed_health_workout_days(db, [6, 0, 2, 4], start_monday=dt.date(2026, 1, 5))
    await _seed_health_workout_days(
        db,
        [1, 3, 5, 6],
        weeks=1,
        start_monday=dt.date(2026, 6, 1),
    )
    await onboarding_bot.set_flow_state(
        1, health_jobs.HEALTH_CONFIRM_FLOW, health_jobs.WIZARD_STEP_WORKOUT_DAYS,
        {"done": ["workout_pattern.frequency"]},
    )

    target = FakeTarget()
    await health_jobs.ask_next_health_confirm_step(target, 1)

    fact = await user_model.get_fact(db, 1, "workout_pattern")
    assert fact["value"]["common_weekdays"] == [6, 0, 2, 4]
    assert fact["value"]["common_weekdays"] != [6, 1, 3, 5]
    assert fact["value"]["weekday_selection_basis"] == "health_workout_history"


@pytest.mark.asyncio
async def test_days_step_asks_instead_of_guessing_when_history_is_insufficient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await _seed_two_detected_days_and_desired_four(db)
    await onboarding_bot.set_flow_state(
        1, health_jobs.HEALTH_CONFIRM_FLOW, health_jobs.WIZARD_STEP_WORKOUT_DAYS,
        {"done": ["workout_pattern.frequency"]},
    )

    target = FakeTarget()
    await health_jobs.ask_next_health_confirm_step(target, 1)

    prompt = target.messages[-1]
    assert "לא זיהיתי דפוס מספיק ברור" in prompt
    assert "כתוב את הימים המועדפים" in prompt
    fact = await user_model.get_fact(db, 1, "workout_pattern")
    assert fact["value"]["common_weekdays"] == [0, 2]
    buttons = [b.callback_data for row in target.reply_markups[-1].inline_keyboard for b in row]
    assert "health:confirm:workout_pattern.days" not in buttons
    assert "health:skip_item" in buttons
    assert "health:skip_wizard" in buttons


@pytest.mark.asyncio
async def test_manual_day_edit_rejected_when_count_mismatches_frequency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Typing 3 days when 4 workouts were requested is rejected with a fix hint."""
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await _seed_two_detected_days_and_desired_four(db)

    ok, reply = await health_jobs.apply_health_wizard_text_edit(
        1, health_jobs.WIZARD_STEP_WORKOUT_DAYS, "ראשון, שלישי, חמישי"
    )
    assert ok is False
    assert "3 ימים" in reply and "4 אימונים" in reply


@pytest.mark.asyncio
async def test_manual_day_edit_accepted_when_count_matches_frequency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Typing exactly 4 days for a 4-workout goal is accepted."""
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await _seed_two_detected_days_and_desired_four(db)

    ok, reply = await health_jobs.apply_health_wizard_text_edit(
        1, health_jobs.WIZARD_STEP_WORKOUT_DAYS, "ראשון, שני, רביעי, שישי"
    )
    assert ok is True
    availability = await user_model.get_fact(db, 1, "weekly_availability")
    assert len(availability["value"]) == 4


@pytest.mark.asyncio
async def test_days_step_has_no_edit_button_but_typing_is_armed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The days screen drops the '✏️ שנה ימים' button: the message tells the
    user to just type a correction, and the free-text capture is already armed
    the moment the screen is shown (no button press needed)."""
    db = await _make_db(tmp_path)
    _patch_db(monkeypatch, db)
    await _seed_two_detected_days_and_desired_four(db)
    await _seed_health_workout_days(db, [6, 0, 2, 4])
    await onboarding_bot.set_flow_state(
        1, health_jobs.HEALTH_CONFIRM_FLOW, health_jobs.WIZARD_STEP_WORKOUT_DAYS,
        {"done": ["workout_pattern.frequency"]},
    )

    target = FakeTarget()
    await health_jobs.ask_next_health_confirm_step(target, 1)

    prompt = target.messages[-1]
    # The message invites a plain-text correction instead of a button.
    assert "פשוט כתוב" in prompt
    buttons = [b.callback_data for row in target.reply_markups[-1].inline_keyboard for b in row]
    assert not any(b and b.startswith("health:edit:") for b in buttons)  # no edit button
    assert "health:confirm:workout_pattern.days" in buttons  # confirm stays

    # Typing is already armed: pending points at the days edit key, so the next
    # free-text message routes into apply_health_wizard_text_edit as if the
    # (now-removed) button had been pressed.
    assert onboarding_bot.PENDING_QUESTION[1] == (
        f"__health_edit_{health_jobs.WIZARD_STEP_WORKOUT_DAYS}__"
    )
    ok, reply = await health_jobs.apply_health_wizard_text_edit(
        1, health_jobs.WIZARD_STEP_WORKOUT_DAYS, "ראשון, שני, רביעי, שישי"
    )
    assert ok is True
    assert "עודכן" in reply
