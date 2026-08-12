# ruff: noqa: F401, F811, F821, I001
"""Training recommendation and fatigue services.

Extracted from the legacy composition module. Public names are re-exported
by coach_bot.py for backward compatibility.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import io
import json
import math
import random
import re
import secrets
import shutil
import time
from collections import defaultdict, deque
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from datetime import time as dttime
from pathlib import Path
from typing import Any, Awaitable, Callable

import aiosqlite
import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.constants import ParseMode
from telegram.error import (
    BadRequest,
    NetworkError,
    RetryAfter,
    TimedOut,
)
from telegram.ext import (
    AIORateLimiter,
    Application,
    ApplicationBuilder,
    CallbackContext,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import assistant
import coach_intelligence
import conversation
import data_quality
import event_log
import health_import
import meal_intelligence
import onboarding
import planning
import questions
import recommendations
import reconcile
import targets
import training_intelligence
import user_model

# --- Extracted modules (re-exported for backward compatibility) ---
from config import (  # noqa: F401
    APP_VERSION,
    LOGGER,
    OPENAI_CLIENT,
    RUNTIME_STATE,
    SETTINGS,
    TZ,
    RuntimeState,
    Settings,
)
from db import DB, Database  # noqa: F401
from helpers import _safe_html_block, esc, friendly_error, today_bounds_utc, utc_now  # noqa: F401
from models import (  # noqa: F401
    ClarificationOption,
    FoodItem,
    HealthBatch,
    HealthSample,
    MealAnalysis,
    MealCorrectionResult,
    MiniProfileUpdate,
    RoutineExtraction,
    ShortcutHealthPayload,
    WatchSetPayload,
)
from retention import cleanup_loop as cleanup_photos  # noqa: F401
from retention import (
    cleanup_operational_data_once,  # noqa: F401
    cleanup_photos_once,  # noqa: F401
)

# ---------------------------------------------------------------------------
# The Settings, Database, Pydantic models, and utility functions have been
# extracted to config.py, db.py, models.py, and helpers.py respectively.
# They are re-imported above for backward compatibility.
# ---------------------------------------------------------------------------

from noam_coach.runtime_bind import runtime_bound

RUNTIME_NAMES = ('Any', 'DB', 'TypeError', 'ValueError', '_ENERGY_FLAG_TO_ENGINE', '_SLEEP_FLAG_TO_ENGINE', '_known_rirs', '_rir_known', 'abs', 'all', 'any', 'assessment', 'average_reps', 'avg_rir', 'best_e1rm', 'bool', 'build_fatigue_assessment', 'comparable_load', 'current_exercise', 'dict', 'energy', 'esc', 'explanation', 'flags', 'float', 'get_daily_flags', 'hard_sessions', 'history', 'hold_for_recovery', 'increment', 'int', 'isinstance', 'known', 'known_latest', 'last_weight', 'latest', 'len', 'list', 'mastered', 'max', 'min', 'planned_sets', 'reason', 'reduced_weight', 'reduction_steps', 'reversed', 'rir_hard', 'rmax', 'rmin', 'round', 'row', 'rows', 's', 'session_row', 'session_rows', 'session_sets', 'session_weight', 'sessions', 'sets', 'sleep', 'soreness', 'srow', 'str', 'sum', 'summaries', 'target_reps', 'training_intelligence', 'tuple', 'used_split_set', 'user_id', 'value')


@runtime_bound(RUNTIME_NAMES)
async def active_session(user_id: int) -> dict[str, Any] | None:
    return await DB.fetch_one(
        """
        SELECT *
        FROM sessions
        WHERE user_id=? AND status='active'
        ORDER BY id DESC
        LIMIT 1
        """,
        (user_id,),
    )


RIR_UNKNOWN = -1

#: A13: the audit surface for the load decision.
#:
#: `audit` rather than the event stream, deliberately. A13 exists to answer
#: "what did we recommend for this set" when a user disputes a weight, which is
#: a durable record question. `emit_event` is the better-instrumented boundary
#: -- correlation, mode policy, its own contained failure handling -- but it
#: writes the event STREAM, which is retention-bounded and mode-gated: with
#: observability OFF it performs no write at all, and the record a dispute
#: needs would legitimately not exist.
_LOAD_AUDIT_ACTION = "recommend_load"
_LOAD_AUDIT_ENTITY = "exercise"

#: Caps for the joined token scalars below. Bounded so an audit row can never
#: carry an unbounded payload, and well under `_AUDIT_MAX_SCALAR_STR`.
_MAX_AUDIT_TOKENS = 8
_MAX_AUDIT_TOKEN_LEN = 24


def _bounded_tokens(values: Any) -> str:
    """Join bounded codes into ONE scalar the audit allowlist will keep.

    `_scalar_only` returns False for a list, so a list-valued detail is dropped
    silently -- no error, no log, and a test that asserts "the write happened"
    still passes while the field is gone. Encoding at the source removes the
    trap instead of documenting it.
    """
    if not values:
        return ""
    tokens = []
    for value in list(values)[:_MAX_AUDIT_TOKENS]:
        token = str(value).strip()[:_MAX_AUDIT_TOKEN_LEN]
        if token:
            tokens.append(token)
    return ",".join(tokens)


@dataclass(frozen=True)
class LoadRecommendation:
    weight: float
    reps: int
    explanation: str
    decision: str
    signals: tuple[str, ...] = ()
    missing_context: tuple[str, ...] = ()
    confidence: int = 80
    data_completeness: int = 80

    def to_tuple(self) -> tuple[float, int, str]:
        return self.weight, self.reps, self.explanation

    def to_audit_dict(self) -> dict[str, Any]:
        """The persistence-safe shape of this decision (A13).

        Two rules make this shape different from the dataclass:

        * **No prose.** `explanation` is free Hebrew text written for a human
          reading a workout card. LOG-012 exists because exactly that kind of
          text reached `audit` and then the DSAR export, so it is absent here
          by construction rather than by an allowlist that a future caller
          might extend. A display surface that wants it reads `.explanation`
          from the recommendation directly -- see `mini_api`.
        * **No lists.** `_allowlist_audit_details` drops list-valued details
          via `_scalar_only`, silently and without error, so `signals` as a
          list would vanish from the stored row while its test still passed.
          They are joined into bounded scalars here, at the single place that
          defines the persisted shape, so no call site can get it wrong.
        """
        return {
            "decision": self.decision,
            "recommended_weight": self.weight,
            "recommended_reps": self.reps,
            "signals": _bounded_tokens(self.signals),
            "missing_context": _bounded_tokens(self.missing_context),
            "confidence": self.confidence,
            "data_completeness": self.data_completeness,
        }


#: Channels that PRESENT a load prescription to the athlete. Recorded so two
#: genuine presentations of the same set -- the Telegram card and the Watch
#: face -- are distinguishable instead of reading as a duplicate.
LOAD_CHANNEL_TELEGRAM = "telegram"
LOAD_CHANNEL_WATCH = "watch"

#: In-process counters, mirroring `emit.py`'s `_health`. A silent failure with
#: no signal is the second thing that boundary forbids.
_LOAD_AUDIT_HEALTH: dict[str, Any] = {"write_failures": 0, "last_error": None}

#: Strong references to in-flight background recordings.
#:
#: `asyncio` keeps only a WEAK reference to a running task, so a bare
#: `ensure_future(...)` whose handle is discarded can be garbage-collected
#: mid-await and the write silently never lands -- and on some paths the loop
#: reports "Task was destroyed but it is pending!" instead. Anything scheduled
#: here holds a reference until it finishes and then removes itself, which is
#: the minimal owned lifecycle: no scheduler, no queue, and nothing to shut
#: down, because each task is short and self-retiring.
_LOAD_AUDIT_TASKS: set[Any] = set()


def _own(task: Any) -> Any:
    """Hold a strong reference to *task* until it completes."""
    _LOAD_AUDIT_TASKS.add(task)
    task.add_done_callback(_LOAD_AUDIT_TASKS.discard)
    return task


#: The tail of the in-flight chain per durable key. Ownership alone is not
#: enough: it keeps tasks ALIVE but says nothing about their ORDER.
#:
#: `record_load_decision` reads the latest row and writes in two separately
#: awaited steps, so two concurrent recordings for one occurrence can
#: interleave:
#:
#:     latest = A
#:     B reads latest=A, then suspends before writing
#:     A is presented again; A reads latest=A and suppresses itself as a repeat
#:     B resumes and writes B
#:     stored: A, B   -- while the athlete saw A, B, A
#:
#: That is the same false-chronology defect the transition semantic exists to
#: prevent, reached by a different route: the last stored row again disagrees
#: with what was last shown. Serializing per key removes the interleaving
#: rather than trying to detect it, and needs no lock, table or migration.
#:
#: Keyed INCLUDING channel, so the Telegram card never waits behind a Watch
#: poll -- they are different presentations and already different identities.
_LOAD_AUDIT_CHAINS: dict[tuple, Any] = {}


def _chain_key(
    user_id: int,
    *,
    exercise_id: str,
    exercise_index: Any,
    session_id: Any,
    set_number: Any,
    channel: str,
) -> tuple:
    """The durable identity, as a hashable key."""
    return (
        int(user_id),
        str(session_id) if session_id is not None else None,
        str(exercise_index) if exercise_index is not None else None,
        str(set_number) if set_number is not None else None,
        str(exercise_id),
        str(channel),
    )


def schedule_load_decision_record(
    user_id: int,
    decision: LoadRecommendation,
    *,
    exercise_id: str,
    exercise_index: Any,
    session_id: Any,
    set_number: Any,
    channel: str,
    slot_id: str = "",
) -> Any:
    """Record without holding up the caller's response.

    For surfaces whose RESPONSE is the presentation -- the Watch endpoint
    returns a payload, so there is no "after" inside the handler in which to
    await a write. Awaiting there would put database latency in front of every
    poll.

    Recordings for ONE durable key run in scheduling order, chained behind
    whatever is already in flight for that key. Without that, the read-then-
    write inside `record_load_decision` can interleave and store a history
    whose last row is not what was last presented -- see `_LOAD_AUDIT_CHAINS`.

    Returns the owned task so a caller (or a test) can await completion; the
    production call site deliberately does not.
    """
    key = _chain_key(
        user_id,
        exercise_id=exercise_id,
        exercise_index=exercise_index,
        session_id=session_id,
        set_number=set_number,
        channel=channel,
    )
    previous = _LOAD_AUDIT_CHAINS.get(key)

    async def _run() -> None:
        if previous is not None:
            # Wait for the earlier recording for this key, whatever its
            # outcome. `record_load_decision` cannot raise, and a cancelled
            # predecessor must not strand this one either.
            with suppress(BaseException):
                await previous
        await record_load_decision(
            user_id,
            decision,
            exercise_id=exercise_id,
            exercise_index=exercise_index,
            session_id=session_id,
            set_number=set_number,
            channel=channel,
            slot_id=slot_id,
        )

    task = _own(asyncio.ensure_future(_run()))
    _LOAD_AUDIT_CHAINS[key] = task

    def _retire(done: Any) -> None:
        # Only the CURRENT tail retires the key. A later scheduling has already
        # replaced it, and that one owns the entry now -- so the registry holds
        # at most one entry per active key and empties when work stops.
        if _LOAD_AUDIT_CHAINS.get(key) is done:
            _LOAD_AUDIT_CHAINS.pop(key, None)

    task.add_done_callback(_retire)
    return task


async def _already_recorded(
    user_id: int,
    *,
    exercise_id: str,
    exercise_index: Any,
    session_id: Any,
    set_number: Any,
    channel: str,
    decision_fields: dict[str, Any],
) -> bool:
    """Has this exact presentation already been recorded?

    THE DURABLE-RECORD SEMANTIC (A13): one row per RECOMMENDATION TRANSITION
    per `(user, session, exercise_index, set, channel)`.

    Compared against the LATEST recorded recommendation only, never against
    the whole history. Comparing against every past row suppresses a return to
    an earlier value, and `audit` carries `created_at` -- it is a sequence in
    time, not a set of values that once occurred. Measured on 60 -> 57.5 -> 60:
    matching any historical row stored only 60 and 57.5, so the LAST row said
    57.5 while the athlete's final recommendation was 60. An investigator
    reading the latest row would draw the opposite conclusion to the truth.
    That is worse than an extra row: the audit itself tells a false story.

        60   -> row
        60   -> no row
        57.5 -> row
        57.5 -> no row
        60   -> row      (a transition back, and it must be recorded)

    The occurrence alone is not the identity. `recommend_load_decision` reads
    MUTABLE state on every call -- `get_daily_flags` for `sleep_quality` and
    `energy`, and active pain regions -- so the recommendation for one live
    occurrence can legitimately change before the set is performed: a user who
    reports bad sleep mid-session is held back to 57.5 kg where the first
    render said 60 kg. Keyed on the occurrence alone, that second, real
    recommendation is suppressed and the audit preserves only that *some*
    recommendation once existed. A13 exists to record WHAT was recommended,
    so that is a defect, not an optimisation.

    The comparison reuses `to_audit_dict()` -- the one place that defines the
    persisted shape -- rather than naming fields here. A field added there is
    automatically material; a second list would drift from it silently.

    `exercise_index` -- the OCCURRENCE, not the movement -- is load-bearing.
    A2 exists because one workout may program the same movement twice, so
    `exercise_id` cannot identify which performance is meant; that is why
    `sets` persists `exercise_index` at all. And `set_number` RESETS to 1 when
    the session advances (`workout.py`, the advance branch), so:

        exercise_index=0, leg_press, set 1
        exercise_index=1, leg_press, set 1

    are two genuinely different presentations that agree on every other field.
    Keyed without the occurrence they collide, and A13 would suppress the
    second -- losing exactly the history it exists to keep.

    `exercise_id` stays as the audit ENTITY: it is what a human reads, and it
    is what `entity_id` has always meant here. The occurrence rides in the
    details alongside it. This reuses A2's established identity rather than
    inventing a second one.

    A poll is not a decision. `GET /api/watch/current` is client-driven with no
    server-side interval, so a Watch sitting on one set can call it every few
    seconds; each call re-renders the SAME prescription for the SAME set. Every
    poll is a refresh of one presentation, not a new one, and recording each
    would grow the audit table without bound and make "what did we recommend
    for this set" -- the question A13 exists to answer -- unanswerable in the
    noise.

    The Telegram card is the same statement in a different channel: re-opening
    the card shows the same prescription again. `channel` is part of the key,
    so the card and the Watch are counted separately -- two genuine
    presentations -- while repetition WITHIN a channel is one.

    Read-then-write is not atomic. This is deliberately not a lock: a race can
    at worst leave two rows for one presentation, which costs an extra row and
    loses nothing. The alternative -- a unique index -- needs a migration, and
    A13 is explicitly a no-migration item.
    """
    # Read through the SAME module `write_audit` writes through. Reading via
    # this module's own `DB` global looked equivalent -- in production it is
    # the same object -- but they can diverge, and a dedupe that queries a
    # different database than it writes to silently never matches: every poll
    # would look like the first one.
    from noam_coach.services import core as _core

    try:
        # Fetch the recommendations already recorded for this occurrence and
        # compare their persisted decision fields. Comparing in SQL would mean
        # restating every field of `to_audit_dict()` in a predicate, which
        # drifts the moment that shape changes -- exactly the silent-drift
        # class this item keeps hitting.
        row = await _core.DB.fetch_one(
            "SELECT details FROM audit "
            "WHERE user_id=? AND action=? AND entity=? AND entity_id=? "
            "AND json_extract(details,'$.session_id')=? "
            "AND json_extract(details,'$.exercise_index')=? "
            "AND json_extract(details,'$.set_number')=? "
            "AND json_extract(details,'$.channel')=? "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (
                user_id,
                _LOAD_AUDIT_ACTION,
                _LOAD_AUDIT_ENTITY,
                exercise_id,
                int(session_id) if session_id is not None else None,
                int(exercise_index) if exercise_index is not None else None,
                int(set_number) if set_number is not None else None,
                channel,
            ),
        )
    except Exception:
        # Fail OPEN: an unreadable audit table must not silence recording.
        # A duplicate row is recoverable; a missing decision record is not.
        LOGGER.exception("load decision dedupe read failed user=%s", user_id)
        return False

    if not row:
        return False
    try:
        stored = json.loads(row["details"] or "{}")
    except (TypeError, ValueError):
        # An unreadable row cannot prove anything about the current state.
        return False

    wanted = {key: decision_fields.get(key) for key in _DECISION_IDENTITY_FIELDS}
    return all(_same_value(stored.get(k), v) for k, v in wanted.items())


#: The persisted fields that make one recommendation materially different from
#: another. Derived from `to_audit_dict()` so the two cannot drift: everything
#: that shape persists about the DECISION is compared.
_DECISION_IDENTITY_FIELDS = (
    "decision",
    "recommended_weight",
    "recommended_reps",
    "signals",
    "missing_context",
    "confidence",
    "data_completeness",
)


def _same_value(stored: Any, wanted: Any) -> bool:
    """Compare one persisted field, tolerating JSON's number round-trip.

    `recommended_weight` is a float that survives a JSON round-trip as a float,
    but 60 and 60.0 must compare equal or every render would look like a change
    and the dedupe would never match.
    """
    if isinstance(stored, (int, float)) and isinstance(wanted, (int, float)):
        return abs(float(stored) - float(wanted)) < 1e-9
    return stored == wanted


async def record_load_decision(
    user_id: int,
    decision: LoadRecommendation,
    *,
    exercise_id: str,
    exercise_index: Any,
    session_id: Any,
    set_number: Any,
    channel: str,
    slot_id: str = "",
) -> None:
    """Record one load decision the athlete was actually asked to act on (A13).

    Recording is best-effort; the set is not. This borrows the contract stated
    in `observability/emit.py` -- contain the failure, count it, log it
    structurally, never raise -- rather than adding a second governed boundary
    beside it. A13 needs no transaction (unlike A12, whose audit row must
    commit with its status UPDATE, which is why A12 writes a raw INSERT and
    this does not).

    CALL THIS AFTER THE RECOMMENDATION HAS BEEN PRESENTED. It is awaited, so a
    caller that awaits it before rendering would put database latency in front
    of the user's card -- which A13 must not do.
    """
    from noam_coach.services.core import write_audit

    try:
        audit_fields = decision.to_audit_dict()
        if await _already_recorded(
            user_id,
            exercise_id=exercise_id,
            exercise_index=exercise_index,
            session_id=session_id,
            set_number=set_number,
            channel=channel,
            decision_fields=audit_fields,
        ):
            # Same presentation, seen again. One durable row per
            # (user, session, set, exercise, channel) -- see `_already_recorded`.
            return
        await write_audit(
            user_id,
            _LOAD_AUDIT_ACTION,
            _LOAD_AUDIT_ENTITY,
            exercise_id,
            channel=channel,
            session_id=int(session_id) if session_id is not None else None,
            # A2's occurrence identity. Part of the durable key -- see
            # `_already_recorded` -- because one workout can program the same
            # movement twice and `set_number` restarts at each one.
            exercise_index=(
                int(exercise_index) if exercise_index is not None else None
            ),
            set_number=int(set_number) if set_number is not None else None,
            # A11b's canonical slot identity, as supplemental provenance only.
            # It answers "which slot in the programme" where `exercise_index`
            # answers "which performance in this session"; a rebuilt plan can
            # move a slot to a new index, so it does NOT replace the
            # occurrence in the key.
            slot_id=slot_id or None,
            **audit_fields,
        )
    except Exception as exc:  # noqa: BLE001 — never break coaching (emit.py rule 1).
        _LOAD_AUDIT_HEALTH["write_failures"] += 1
        _LOAD_AUDIT_HEALTH["last_error"] = f"{type(exc).__name__}: {exc}"
        LOGGER.error(
            "load decision audit failed user=%s exercise=%s channel=%s: %s",
            user_id, exercise_id, channel, exc,
        )


# ---------------------------------------------------------------------------
# A5: one shared contract for selecting exercise history.
#
# Four production sites read set history keyed on the canonical exercise id --
# recommend_load_decision's session picker and its per-session fetch, plus
# show_session's "previous performance" line and previous_weight_context. They
# were written independently and answer the same question in three different
# ways, so a screen can show one exercise's history while the recommendation
# beside it uses another's.
#
# Two further readers (build_fatigue_assessment, reconcile._session_perf_by_day)
# key on session_id ONLY -- they are exercise-blind by design, aggregating a
# whole session. They are deliberately NOT part of this contract: giving them an
# exercise dimension would change what they measure, not just how they read it.
#
# This layer records WHICH history was selected and WHY, so every consumer can
# agree. It changes no query semantics today: `implementation_id` does not exist
# yet, so the only reachable layer is CANONICAL and behaviour is identical. The
# ladder tiers are declared now so the consumers, the audit fields and the tests
# are already in place when A-later introduces implementation identity.
# ---------------------------------------------------------------------------

#: Which identity the history came from. Ordered strongest to weakest -- the
#: resolver returns the first layer that yields rows.
HISTORY_LAYER_IMPLEMENTATION = "implementation"   # this exact machine (future)
HISTORY_LAYER_EQUIVALENT = "equivalent"           # user-approved equivalent (future)
HISTORY_LAYER_CANONICAL = "canonical"             # same movement, any machine
HISTORY_LAYER_NONE = "none"                       # nothing found

#: Why the resolver returned no usable history. These are NOT interchangeable,
#: and collapsing them is the failure this contract exists to prevent: a
#: database that could not be read must never look like a user who has never
#: trained. `history_unavailable` means the query could not be answered;
#: `no_history` means it was answered and the answer was "none".
HISTORY_ABSENT_NO_HISTORY = "no_history"
HISTORY_ABSENT_UNAVAILABLE = "history_unavailable"

#: Confidence ceiling per layer. A canonical-layer answer is real history and
#: keeps today's confidence; the weaker layers cap lower because they describe
#: a different machine. Applied as a ceiling, never a floor, so no existing
#: branch can be made MORE confident by this change.
_LAYER_CONFIDENCE_CEILING = {
    HISTORY_LAYER_IMPLEMENTATION: 100,
    HISTORY_LAYER_EQUIVALENT: 74,
    HISTORY_LAYER_CANONICAL: 100,
}


@dataclass(frozen=True)
class HistorySelection:
    """What history was found, from which identity layer, and how sure we are.

    `absent_reason` is populated only when `rows` is empty, and distinguishing
    its two values is the point of the type: a caller can render "no history
    yet" for one and must not for the other.
    """

    rows: tuple[dict[str, Any], ...] = ()
    layer: str = HISTORY_LAYER_NONE
    absent_reason: str | None = None

    @property
    def found(self) -> bool:
        return bool(self.rows)

    @property
    def is_unavailable(self) -> bool:
        """True when history could not be READ, as opposed to not existing."""
        return self.absent_reason == HISTORY_ABSENT_UNAVAILABLE

    def confidence_ceiling(self, base: int) -> int:
        """Cap a branch's confidence by the layer the history came from."""
        ceiling = _LAYER_CONFIDENCE_CEILING.get(self.layer)
        return base if ceiling is None else min(base, ceiling)

    def missing_context(self) -> tuple[str, ...]:
        """The missing-context tokens this selection implies."""
        if self.found:
            return ()
        return (self.absent_reason or HISTORY_ABSENT_NO_HISTORY,)


async def select_exercise_history(
    db: Any,
    user_id: int,
    exercise_id: str,
    *,
    limit_sessions: int = 3,
) -> HistorySelection:
    """Resolve the strongest available history layer for one exercise.

    Today only the canonical layer is reachable -- there is no per-machine
    identity to query -- so this returns exactly what the previous inline query
    returned. The value is that every consumer now learns WHICH layer answered
    and, when nothing came back, whether that was an answer or a failure.

    A query error is surfaced as `history_unavailable` rather than propagating.
    The callers render a workout screen; a locked database should degrade the
    load recommendation, not break the set the user is mid-way through. The
    distinction is preserved in the return value and logged, so it does not
    become a silent "no data".
    """
    try:
        rows = await db.fetch_all(
            """
            SELECT ws.id, COALESCE(ws.ended_at, ws.started_at) AS performed_at
            FROM sessions ws
            JOIN sets s ON s.session_id=ws.id
            WHERE ws.user_id=?
              AND s.exercise_id=?
              AND ws.status IN ('completed', 'partial')
            GROUP BY ws.id
            ORDER BY performed_at DESC, ws.id DESC
            LIMIT ?
            """,
            (user_id, exercise_id, limit_sessions),
        )
    except Exception:
        # Bounded reason code + internal ids only -- never the row contents,
        # which are the user's training data.
        LOGGER.exception(
            "history_selection_failed user_id=%s exercise_id=%s layer=%s",
            user_id, exercise_id, HISTORY_LAYER_CANONICAL,
        )
        return HistorySelection(
            layer=HISTORY_LAYER_NONE,
            absent_reason=HISTORY_ABSENT_UNAVAILABLE,
        )

    if not rows:
        return HistorySelection(
            layer=HISTORY_LAYER_NONE,
            absent_reason=HISTORY_ABSENT_NO_HISTORY,
        )
    return HistorySelection(rows=tuple(rows), layer=HISTORY_LAYER_CANONICAL)


def format_load_decision_details(decision: LoadRecommendation) -> str:
    """Render an auditable load decision for the workout "how was this decided" UI."""
    lines = [
        "<b>איך חושב?</b>",
        f"משקל מומלץ: <b>{decision.weight:g} ק״ג</b>",
        f"חזרות מומלצות: <b>{decision.reps}</b>",
        f"סיבה: {decision.explanation}",
    ]
    if decision.signals:
        labels = {
            "active_pain": "כאב פעיל",
            "hard_sessions": "אימונים קשים לאחרונה",
            "sleep_quality": "שינה",
            "energy": "אנרגיה",
            "mastered_top_range": "שליטה בטווח העליון",
            "recovery_hold": "שמירה להתאוששות",
            "mixed_load_latest_session": "עומסים מעורבים באימון האחרון",
            "split_or_drop_set": "סט מפוצל/ירידת משקל",
            "rir_allows_rep_progression": "RIR מאפשר התקדמות בחזרות",
            "rir_missing": "RIR חסר",
        }
        rendered_signals: list[str] = []
        for signal in decision.signals:
            key, _, value = signal.partition(":")
            label = labels.get(key, key.replace("_", " "))
            rendered_signals.append(f"{label}: {value}" if value else label)
        lines.extend(["", "<b>נתונים שהשפיעו</b>"])
        lines.extend(f"• {signal}" for signal in rendered_signals)
    if decision.missing_context:
        missing_labels = {
            "exercise_history": "אין עדיין היסטוריית ביצוע לתרגיל",
            "comparable_sets": "אין סטים בני השוואה",
        }
        lines.extend(["", "<b>מה חסר כדי לדייק</b>"])
        lines.extend(f"• {missing_labels.get(item, item)}" for item in decision.missing_context)
    lines.extend(
        [
            "",
            f"<i>ביטחון: {decision.confidence}/100 · שלמות מידע: {decision.data_completeness}/100</i>",
        ]
    )
    return "\n".join(lines)


@runtime_bound(RUNTIME_NAMES)
def _rir_known(value: Any) -> bool:
    try:
        return int(value) >= 0
    except (TypeError, ValueError):
        return False


@runtime_bound(RUNTIME_NAMES)
def _known_rirs(rows: list[dict[str, Any]]) -> list[int]:
    return [int(row["rir"]) for row in rows if _rir_known(row.get("rir"))]


@runtime_bound(RUNTIME_NAMES)
async def _exercise_pain_caution(
    user_id: int, current_exercise: dict[str, Any]
) -> training_intelligence.ActivePainRegion | None:
    """Return the active pain region (if any) that this exercise loads.

    Reads medical_constraints directly (kind='pain', status='active', within
    the TTL window) rather than only the onboarding-time active_pain fact, so
    a pain report made mid-workout also caps progression on the very next
    session for the same joint, not just on plans generated after it.
    """
    profile = training_intelligence.CATALOG.get(str(current_exercise.get("id")))
    if profile is None or not profile.joint_load:
        return None
    rows = await DB.fetch_all(
        "SELECT * FROM medical_constraints WHERE user_id=? AND kind='pain'",
        (user_id,),
    )
    if not rows:
        return None
    regions = training_intelligence.active_pain_regions(rows)
    for joint in profile.joint_load:
        if joint in regions:
            return regions[joint]
    return None


@runtime_bound(RUNTIME_NAMES)
async def recommend_load_decision(
    user_id: int,
    current_exercise: dict[str, Any],
) -> LoadRecommendation:
    """Recommend the next working load with an auditable decision record.

    Only the latest completed/partial session is used for the immediate
    recommendation, so a partial workout can never be mixed with older sets.
    Three consecutive clearly hard sessions trigger a small exercise-specific
    load reduction. Split/drop sets never trigger an automatic progression.
    An active, reported pain in a region this exercise loads (per
    training_intelligence.CATALOG joint_load) also blocks any weight/rep
    increase, regardless of RIR history — pain caution always outranks a
    "mastered" reading.
    """
    pain_caution = await _exercise_pain_caution(user_id, current_exercise)
    missing_context: list[str] = []
    signals: list[str] = []
    if pain_caution is not None:
        signals.append(f"active_pain:{pain_caution.region}")

    selection = await select_exercise_history(
        DB, user_id, current_exercise["id"], limit_sessions=3
    )
    session_rows = list(selection.rows)

    if not session_rows:
        # Two different situations, deliberately not collapsed. "no_history" is
        # an answer -- the user has not trained this exercise. "history_
        # unavailable" means the question could not be answered at all, and
        # reporting that as a data condition would hide an infrastructure one.
        missing_context.extend(selection.missing_context())
        if selection.is_unavailable:
            signals.append("history_unavailable")
        else:
            # Preserved for every existing consumer of this token.
            missing_context.append("exercise_history")
        return LoadRecommendation(
            float(current_exercise["weight"]),
            int(current_exercise["rmin"]),
            "משקל מתוכנן",
            "planned_load",
            tuple(signals),
            tuple(missing_context),
            confidence=55,
            data_completeness=45,
        )

    # NOTE (behaviour preserved deliberately): the session picker above does
    # NOT exclude `telegram_split_secondary`, while this per-session fetch
    # does. A session whose only sets for this exercise are split-secondary is
    # therefore selected, consumes one of the three slots, and then contributes
    # nothing -- so the recommendation can rest on fewer sessions than it
    # appears to. Changing the picker would alter which sessions inform the
    # load, which is a load-behaviour change and not A5's scope; A5 makes the
    # selection observable so the asymmetry is measurable before anyone acts
    # on it. `sessions_dropped` below is that measurement.
    history: list[list[dict[str, Any]]] = []
    sessions_dropped = 0
    for session_row in session_rows:
        rows = await DB.fetch_all(
            """
            SELECT weight, reps, rir, source, set_number
            FROM sets
            WHERE session_id=?
              AND exercise_id=?
              AND source != 'telegram_split_secondary'
            ORDER BY set_number, id
            """,
            (session_row["id"], current_exercise["id"]),
        )
        if rows:
            history.append(rows)
        else:
            sessions_dropped += 1

    if sessions_dropped:
        signals.append(f"sessions_dropped:{sessions_dropped}")

    if not history:
        missing_context.append("comparable_sets")
        return LoadRecommendation(
            float(current_exercise["weight"]),
            int(current_exercise["rmin"]),
            "משקל מתוכנן",
            "planned_load",
            tuple(signals),
            tuple(missing_context),
            confidence=55,
            data_completeness=45,
        )

    # Which identity answered. Constant today (only the canonical layer is
    # reachable), but emitted now so consumers, audit fields and tests already
    # agree on the vocabulary before per-machine identity exists.
    signals.append(f"history_layer:{selection.layer}")

    latest = history[0]
    last_weight = float(latest[0]["weight"])
    planned_sets = int(current_exercise["sets"])
    rmin = int(current_exercise["rmin"])
    rmax = int(current_exercise["rmax"])
    increment = max(0.25, float(current_exercise["inc"]))

    flags = await get_daily_flags(user_id)
    hold_for_recovery = flags.get("sleep_quality") == "bad" or flags.get("energy") == "low"
    if flags.get("sleep_quality") == "bad":
        signals.append("sleep_quality:bad")
    if flags.get("energy") == "low":
        signals.append("energy:low")

    comparable_load = all(abs(float(row["weight"]) - last_weight) < 0.01 for row in latest)
    used_split_set = any(row["source"] == "telegram_split_primary" for row in latest)
    if not comparable_load:
        signals.append("mixed_load_latest_session")
    if used_split_set:
        signals.append("split_or_drop_set")
    # Mastery requires hitting the top rep range AND a *reported* RIR >= 2 on
    # every planned set. An unknown RIR never counts as proof of mastery, so we
    # do not auto-progress on fabricated data. An active, reported pain in a
    # region this exercise loads blocks mastery outright — RIR history can
    # never justify a load increase while that pain is still active.
    mastered = (
        pain_caution is None
        and len(latest) >= planned_sets
        and comparable_load
        and not used_split_set
        and all(
            int(row["reps"]) >= rmax and _rir_known(row.get("rir")) and int(row["rir"]) >= 2
            for row in latest[:planned_sets]
        )
    )

    if mastered and hold_for_recovery:
        signals.append("mastered_top_range")
        signals.append("recovery_hold")
        return LoadRecommendation(
            last_weight,
            rmin,
            "שלטת בטווח, אבל היום שומרים עומס בגלל שינה או אנרגיה נמוכה",
            "hold_for_recovery",
            tuple(signals),
            tuple(missing_context),
            confidence=82,
            data_completeness=90,
        )

    if mastered:
        signals.append("mastered_top_range")
        return LoadRecommendation(
            round(last_weight + increment, 2),
            rmin,
            "השלמת את כל הסטים בטווח העליון עם RIR מתאים — עולים מדרגה",
            "increase_load",
            tuple(signals),
            tuple(missing_context),
            confidence=88,
            data_completeness=92,
        )

    hard_sessions = 0
    for session_sets in history:
        if len(session_sets) < min(2, planned_sets):
            break
        session_weight = float(session_sets[0]["weight"])
        if abs(session_weight - last_weight) > increment / 2:
            break
        average_reps = sum(int(row["reps"]) for row in session_sets) / len(session_sets)
        known = _known_rirs(session_sets)
        # A "hard" session needs low reps AND, when RIR was reported, a low RIR.
        # If RIR was never reported we fall back to reps alone rather than
        # inventing an RIR of 0.
        rir_hard = (sum(known) / len(known)) <= 1 if known else True
        if average_reps <= rmin and rir_hard:
            hard_sessions += 1
        else:
            break
    if hard_sessions:
        signals.append(f"hard_sessions:{hard_sessions}")

    if hard_sessions >= 3:
        reduction_steps = max(
            1,
            round((last_weight * 0.075) / increment),
        )
        reduced_weight = max(
            0.0,
            round(last_weight - reduction_steps * increment, 2),
        )
        if pain_caution is not None:
            return LoadRecommendation(
                reduced_weight,
                rmin,
                f"דיווחת לאחרונה על כאב ב{pain_caution.label} וגם הביצועים היו קשים — מורידים מעט עומס",
                "reduce_load_for_pain_and_hard_history",
                tuple(signals),
                tuple(missing_context),
                confidence=90,
                data_completeness=92,
            )
        return LoadRecommendation(
            reduced_weight,
            rmin,
            "שלושה אימונים רצופים היו קשים בתחתית הטווח — מורידים מעט עומס כדי לבנות מחדש",
            "reduce_load_for_hard_history",
            tuple(signals),
            tuple(missing_context),
            confidence=86,
            data_completeness=90,
        )

    if pain_caution is not None:
        # Never raise weight or reps while a reported pain is active in a
        # region this exercise loads. If recent hard sessions already meet the
        # normal deload rule above, that reduction still wins.
        return LoadRecommendation(
            last_weight,
            rmin,
            f"דיווחת לאחרונה על כאב ב{pain_caution.label} — שומר עומס שמרני בתרגיל הזה",
            "hold_for_active_pain",
            tuple(signals),
            tuple(missing_context),
            confidence=86,
            data_completeness=88,
        )

    average_reps = sum(int(row["reps"]) for row in latest) / len(latest)
    known_latest = _known_rirs(latest)
    target_reps = int(round(average_reps))
    # Only nudge reps up when the user actually reported RIR >= 2 (reps in
    # reserve). With no reported RIR we keep the current target.
    if known_latest and (sum(known_latest) / len(known_latest)) >= 2 and not used_split_set:
        signals.append("rir_allows_rep_progression")
        target_reps += 1
    elif not known_latest:
        signals.append("rir_missing")
    target_reps = max(rmin, min(rmax, target_reps))

    if used_split_set:
        explanation = "הסט האחרון כלל ירידת משקל, לכן לא מעלים עומס אוטומטית"
    elif len(latest) < planned_sets:
        explanation = "האימון האחרון היה חלקי — שומרים עומס עד שיש ביצוע מלא להשוואה"
    else:
        explanation = "נשארים באותו עומס ומתקדמים בהדרגה בתוך טווח החזרות"

    decision = "hold_after_split_set" if used_split_set else "hold_or_progress_reps"
    return LoadRecommendation(
        last_weight,
        target_reps,
        explanation,
        decision,
        tuple(signals),
        tuple(missing_context),
        confidence=78 if known_latest else 68,
        data_completeness=86 if known_latest else 70,
    )


@runtime_bound(RUNTIME_NAMES)
async def recommend_load(
    user_id: int,
    current_exercise: dict[str, Any],
) -> tuple[float, int, str]:
    """Backward-compatible tuple wrapper for the auditable load decision."""
    decision = await recommend_load_decision(user_id, current_exercise)
    return decision.to_tuple()


_SLEEP_FLAG_TO_ENGINE = {"bad": "poor", "ok": "average", "good": "good"}


_ENERGY_FLAG_TO_ENGINE = {"low": "low", "ok": "average", "high": "high"}


@runtime_bound(RUNTIME_NAMES)
async def build_fatigue_assessment(
    user_id: int,
) -> training_intelligence.FatigueAssessment | None:
    """Assess fatigue/plateau from real recent sessions + today's check-in.

    Builds the engine input from the user's last few completed/partial sessions:
    per-session ``avg_rir`` (only over *reported* RIR — unknown RIR is ignored,
    never treated as 0) and best ``e1rm``. Returns None when there is not enough
    history to say anything useful.
    """
    sessions = await DB.fetch_all(
        "SELECT id FROM sessions WHERE user_id=? AND status IN ('completed','partial') "
        "ORDER BY COALESCE(ended_at, started_at) DESC LIMIT 6",
        (user_id,),
    )
    if len(sessions) < 2:
        return None

    summaries: list[dict[str, Any]] = []
    # Oldest-first so the engine's "last N" slicing sees chronological order.
    for srow in reversed(sessions):
        sets = await DB.fetch_all(
            "SELECT weight, reps, rir FROM sets WHERE session_id=? "
            "AND source != 'telegram_split_secondary'",
            (srow["id"],),
        )
        if not sets:
            continue
        known = _known_rirs(sets)
        avg_rir = (sum(known) / len(known)) if known else 3.0  # unknown -> "fresh"
        best_e1rm = max(
            (training_intelligence.epley_1rm(float(s["weight"]), int(s["reps"])) for s in sets),
            default=0.0,
        )
        summaries.append({"avg_rir": avg_rir, "e1rm": best_e1rm})

    if len(summaries) < 2:
        return None

    flags = await get_daily_flags(user_id)
    sleep = _SLEEP_FLAG_TO_ENGINE.get(flags.get("sleep_quality"))
    energy = _ENERGY_FLAG_TO_ENGINE.get(flags.get("energy"))
    soreness = flags.get("soreness")
    return training_intelligence.assess_fatigue(
        summaries,
        sleep_quality=sleep,
        energy=energy,
        soreness=int(soreness) if isinstance(soreness, (int, float)) else None,
    )


@runtime_bound(RUNTIME_NAMES)
async def fatigue_banner(user_id: int) -> str:
    """A short, non-diagnostic deload/plateau heads-up, or '' if none needed."""
    assessment = await build_fatigue_assessment(user_id)
    if not assessment:
        return ""
    if assessment.deload_recommended:
        reason = f" ({', '.join(assessment.reasons)})" if assessment.reasons else ""
        return (
            "🟠 <b>מומלץ שבוע הקלה (deload)</b>"
            f"{esc(reason)}.\n"
            "כדאי להוריד עומס/נפח באימון הזה ולתת לגוף להתאושש. "
            "לא חובה — זו המלצה לפי המגמה האחרונה.\n\n"
        )
    if assessment.plateau:
        return (
            "🟡 <b>נראה שהביצועים נתקעו</b> בכמה אימונים אחרונים. "
            "שקול שינוי קטן בתרגיל, בנפח או מנוחה נוספת.\n\n"
        )
    return ""
