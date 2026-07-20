"""Real reschedule UX for "the workout is later" (B12 / ARCH-14).

Resolved product contract: reschedule must collect a CONCRETE time —
a small set of sensible slot controls plus explicit text input, normalized
and validated before persistence. A vague "later" is never persisted as if
it were a scheduling decision (FIX 40 correctly stopped treating it as
"imminent"; this batch finishes the loop by actually asking WHEN).

Flow (installed as wraps; the protected callback handler is untouched):

    nextmeal:wkt:later
      → status="later" persisted (the claim "not now" is still true)
      → time-collection card: slot buttons (wktat:HHMM) + free-text option
    wktat:HHMM        → validate/normalize → save_workout_reschedule_time
    wktat:text        → conversation_state 'workout_reschedule' await_time
                        + prompt; the next free-text turn is parsed as a
                        time (this wrap sits UNDER the B9 turn-context wrap,
                        so deterministic reference resolution still runs
                        first and non-time messages fall through normally)
    wktat:skip        → explicit "don't know yet": stays a vague later —
                        pending, never persisted as a time
    invalid/past time → user-visible correction, nothing persisted

The persisted decision (next_meal_workout_expected_at) feeds the canonical
workout resolver, so meal timing/budget decisions see the USER-stated time.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from config import TZ

RESCHEDULE_FLOW = "workout_reschedule"


async def _render_next_meal(query: Any, user_id: int, prefix: str) -> None:
    """Render via the protected screen builder with its facade globals
    synced first — _render_next_meal_screen is undecorated, so a wrap
    calling it on a cold path would NameError on names like `button`."""
    from noam_coach.bot import callback_menu as callback_menu_bot
    from noam_coach.runtime_bind import _sync

    _sync(callback_menu_bot._render_next_meal_screen, callback_menu_bot.RUNTIME_NAMES)
    await callback_menu_bot._render_next_meal_screen(query, user_id, prefix=prefix)

_TIME_PATTERN = re.compile(r"^\s*([01]?\d|2[0-3])(?:[:.]([0-5]\d))?\s*$")
_RELATIVE_PATTERNS = (
    (re.compile(r"בעוד\s+חצי\s+שעה"), 30),
    (re.compile(r"בעוד\s+שעה\s+וחצי"), 90),
    (re.compile(r"בעוד\s+שעתיים"), 120),
    (re.compile(r"בעוד\s+שעה"), 60),
)


def parse_reschedule_time(text: str, now: datetime) -> tuple[datetime | None, str | None]:
    """Normalize free text to a concrete future datetime.

    Returns (datetime, None) on success; (None, reason) where reason is
    "past" (valid time, already passed today) or "unparseable".
    """
    cleaned = (text or "").strip()
    for pattern, minutes in _RELATIVE_PATTERNS:
        if pattern.search(cleaned):
            return now + timedelta(minutes=minutes), None
    match = _TIME_PATTERN.match(cleaned)
    if not match:
        return None, "unparseable"
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    candidate = now.astimezone(TZ).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    if candidate <= now:
        return None, "past"
    return candidate, None


def _slot_times(now: datetime, typical_hour: str | None) -> list[datetime]:
    """Up to three sensible concrete slots: +1h, +2h (rounded to :30), and
    the user's typical workout hour when it is still ahead."""
    local = now.astimezone(TZ)

    def rounded(dt: datetime) -> datetime:
        minute = 30 if dt.minute and dt.minute <= 30 else 0
        return dt.replace(minute=minute, second=0, microsecond=0) + (
            timedelta(hours=1) if dt.minute > 30 else timedelta()
        )

    slots = [rounded(local + timedelta(hours=1)), rounded(local + timedelta(hours=2))]
    if typical_hour:
        try:
            hh, mm = (int(x) for x in str(typical_hour).split(":"))
            typical = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if typical > local and all(
                abs((typical - slot).total_seconds()) > 900 for slot in slots
            ):
                slots.append(typical)
        except (TypeError, ValueError):
            pass
    unique: list[datetime] = []
    for slot in sorted(slots):
        if not unique or (slot - unique[-1]).total_seconds() > 900:
            unique.append(slot)
    return unique[:3]


async def _render_time_card(query: Any, user_id: int) -> None:
    from telegram import InlineKeyboardMarkup

    import coach_bot as facade
    from noam_coach.bot.ui import button

    typical = None
    try:
        profile = await facade.load_routine_profile(user_id)
        typical = (profile.get("workout") or {}).get("typical_hour")
    except Exception:  # noqa: BLE001
        typical = None
    now = datetime.now(TZ)
    rows = [
        [
            button(f"🕒 {slot:%H:%M}", f"wktat:{slot:%H%M}")
            for slot in _slot_times(now, typical)
        ],
        [button("✍️ אכתוב שעה", "wktat:text")],
        [button("⏭️ לא יודע עדיין", "wktat:skip")],
    ]
    await facade.safe_edit(
        query,
        "רשמתי שהאימון נדחה. מתי בערך הוא כן יקרה? "
        "זה עוזר לי לתזמן את הארוחות סביבו.",
        InlineKeyboardMarkup(rows),
    )


async def _persist_and_confirm(query_or_message: Any, user_id: int, expected: datetime, *, edit: bool) -> None:
    import coach_bot as facade
    from noam_coach.services import core as core_services
    from noam_coach.services.next_meal import save_workout_reschedule_time

    db = facade.DB
    await save_workout_reschedule_time(db, user_id, expected)
    await core_services.clear_flow_state(user_id, RESCHEDULE_FLOW)
    prefix = (
        f"סגור — האימון בסביבות {expected.astimezone(TZ):%H:%M}. "
        "התאמתי את התזונה בהתאם."
    )
    if edit:
        await _render_next_meal(query_or_message, user_id, prefix)
    else:
        await query_or_message.reply_text(prefix)


_original_menu_handler: Any = None
_original_route_free_text: Any = None


def install_workout_reschedule() -> None:
    """Idempotent install of the reschedule wraps."""
    global _original_menu_handler, _original_route_free_text
    if _original_menu_handler is not None:
        return
    import coach_bot

    _original_menu_handler = coach_bot.handle_menu_callback
    original_menu = _original_menu_handler

    async def reschedule_handle_menu_callback(query: Any, user_id: int, data: str) -> bool:
        import coach_bot as facade

        if data == "nextmeal:wkt:later":
            from noam_coach.services.next_meal import save_next_meal_workout_status

            await save_next_meal_workout_status(facade.DB, user_id, "later")
            await _render_time_card(query, user_id)
            return True
        if isinstance(data, str) and data.startswith("wktat:"):
            from noam_coach.services import core as core_services

            arg = data.split(":", 1)[1]
            if arg == "skip":
                await core_services.clear_flow_state(user_id, RESCHEDULE_FLOW)
                await _render_next_meal(
                    query, user_id,
                    "בסדר, נשאיר את זה פתוח — עדכן אותי כשתדע מתי.",
                )
                return True
            if arg == "text":
                await core_services.set_flow_state(
                    user_id, RESCHEDULE_FLOW, "await_time", {}
                )
                from telegram import InlineKeyboardMarkup

                from noam_coach.bot.ui import button

                await facade.safe_edit(
                    query,
                    'באיזו שעה בערך האימון? כתוב לי שעה (למשל "19:30" או "בעוד שעה").',
                    InlineKeyboardMarkup([[button("⏭️ לא יודע עדיין", "wktat:skip")]]),
                )
                return True
            if re.fullmatch(r"\d{4}", arg):
                now = datetime.now(TZ)
                expected, reason = parse_reschedule_time(f"{arg[:2]}:{arg[2:]}", now)
                if expected is None:
                    await _render_time_card(query, user_id)
                    return True
                await _persist_and_confirm(query, user_id, expected, edit=True)
                return True
            return True  # unknown wktat form: consumed, never guessed
        return await original_menu(query, user_id, data)

    coach_bot.handle_menu_callback = reschedule_handle_menu_callback

    _original_route_free_text = coach_bot.route_free_text
    original_route = _original_route_free_text

    async def reschedule_route_free_text(update: Any, user_id: int) -> None:
        import coach_bot as facade
        from noam_coach.services import core as core_services

        text = (getattr(update.effective_message, "text", None) or "").strip()
        if text:
            state = await core_services.get_flow_state(user_id, RESCHEDULE_FLOW)
            if state is not None:
                now = datetime.now(TZ)
                expected, reason = parse_reschedule_time(text, now)
                if expected is not None:
                    await _persist_and_confirm(
                        update.effective_message, user_id, expected, edit=False
                    )
                    return None
                if reason == "past":
                    await update.effective_message.reply_text(
                        "השעה הזו כבר עברה היום — באיזו שעה בערך כן יקרה האימון?"
                    )
                    return None
                if any(ch.isdigit() for ch in text):
                    # Time-shaped but invalid: correct visibly, stay pending.
                    await update.effective_message.reply_text(
                        'לא הצלחתי להבין את השעה — כתוב למשל "19:30".'
                    )
                    return None
                # Not a time at all: the user moved on. Release the pending
                # question instead of trapping their message.
                await core_services.clear_flow_state(user_id, RESCHEDULE_FLOW)
        del facade
        return await original_route(update, user_id)

    coach_bot.route_free_text = reschedule_route_free_text


def uninstall_workout_reschedule() -> None:
    global _original_menu_handler, _original_route_free_text
    import coach_bot

    if _original_menu_handler is not None:
        coach_bot.handle_menu_callback = _original_menu_handler
        _original_menu_handler = None
    if _original_route_free_text is not None:
        coach_bot.route_free_text = _original_route_free_text
        _original_route_free_text = None
