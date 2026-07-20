"""Two-tier workout catalog and resolution service (workout-selection
architecture, Batch 3).

Behavior-inert: nothing in this module is imported or called by any
handler yet. It exists to give later batches ONE place that resolves
"which workouts can the user choose from" and "what does the selected
workout actually contain" — replacing the current defect where
recommendation (``resolve_todays_workout``) reads the user's personalized
active plan, while overview/edit/Start (``get_user_plan``) silently read
the GLOBAL template ``PLANS[code]`` instead.

Two explicit, permanent tiers (see the architecture plan, section E):

* **Tier 1 (authoritative)** -- a real ``plan_versions`` row via
  ``planning.get_active_plan``. Identity is ``(plan_id, session_index)``,
  safe because a plan payload is never UPDATEd after INSERT (regeneration
  always mints a new row) -- enforced by
  ``tests/test_workout_catalog.py``'s architecture guard.
* **Tier 2 (explicit compatibility)** -- a fact-only weekly plan written by
  ``onboarding.build_weekly_plan`` (thin sessions: weekday/time/code/name,
  NO ``plan_versions`` row at all). Identity is ``(fact_rev, session_index)``
  because the fact has no monotonic id; ``fact_rev`` is a content
  fingerprint of the ordered sessions (``compute_fact_rev``) so a same-length
  replacement plan cannot silently resolve to the wrong session.

``workout_catalog`` is the ONLY module later batches may use to answer
either question -- an architecture test freezes the current allowlist of
direct ``get_value(..., "active_workout_plan")`` readers so that population
can only shrink, never grow.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import user_model
from config import APP_VERSION, TZ
from exercise_plans import EXERCISE_MUSCLES, OVERRIDE_FIELDS, PLANS
from noam_coach.services import daily_state

NORMALIZER_VERSION = 1

# Safe, non-invented defaults for a sparse exercise entry. Mirrors the
# existing fallback precedent at training_intelligence.py:922-925 (pain-
# adaptation replacement candidates use the identical sets/rmin/rmax/rest
# numbers). `weight` is deliberately 0.0, never a guessed load -- see
# `normalize_exercise`.
_DEFAULT_SETS = 3
_DEFAULT_RMIN = 8
_DEFAULT_RMAX = 12
_DEFAULT_REST = 120
_DEFAULT_WEIGHT = 0.0
_DEFAULT_INC = 2.5

# Fields every runtime consumer hard-reads (workout.py:426/454/468-473,
# callback_session.py:554/909, training.py:274-281, ui.py:639-643) or
# soft-reads with a safe fallback in production today. `id` and `name` are
# assumed present on every exercise entry (planning.py:806-814 validates
# id is required+unique per session before a plan can activate at all);
# everything else here is fillable.
_FILLABLE_FIELDS = ("sets", "rmin", "rmax", "rest", "weight", "inc", "cues", "alts", "muscle")


class StalePlanReference(Exception):
    """Raised by resolve_selection when a WorkoutSelectionRef no longer
    matches the user's current active plan/fact (regeneration, activation
    of a different plan, or a fact replaced by a same-length sibling)."""


@dataclass(frozen=True)
class WorkoutSelectionRef:
    """The callback-carried identity of a chosen workout session.

    Exactly one of (plan_id, fact_rev) is set, matching `tier`:
    tier="plan" -> plan_id is the plan_versions.id; tier="fact" -> fact_rev
    is the 8-hex content fingerprint of the fact's ordered sessions
    (compute_fact_rev). `session_index` is always the array position of
    the session within that identity's session list.
    """

    tier: str  # "plan" | "fact"
    plan_id: int | None
    fact_rev: str | None
    session_index: int


@dataclass(frozen=True)
class WorkoutChoice:
    """One selectable entry in the workout selector: enough to render a
    button and a done-today mark without resolving full session content."""

    ref: WorkoutSelectionRef
    code: str
    name: str
    weekday: int | None
    done_today: bool
    recommended: bool
    reason: str  # offer_today | offer_next | no_plan | all_done_today


@dataclass(frozen=True)
class ResolvedWorkout:
    """A fully normalized, ready-to-render/ready-to-start session."""

    choice: WorkoutChoice
    session: dict[str, Any]
    defaults_filled: dict[str, list[str]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# fact_rev: Tier-2 content fingerprint (blocker-1, plan section F).
# ---------------------------------------------------------------------------


def compute_fact_rev(fact: dict[str, Any]) -> str:
    """Deterministic 8-hex-char fingerprint of a Tier-2 fact's ordered
    sessions, pinning the exact content a Tier-2 callback was minted
    against.

    Canonicalization (plan section F, exact): for each session in PAYLOAD
    order (not sorted -- index identity is order-sensitive, a reorder MUST
    change the fingerprint), join ``code|weekday|time|name`` -- the four
    fields onboarding.build_weekly_plan actually writes
    (onboarding.py:2547-2555) -- then join sessions with ``\\x1f``, prefix
    with the session count, sha256 the UTF-8 bytes, take the first 8
    lowercase-hex characters.
    """
    sessions = (fact or {}).get("sessions") or []
    parts = [
        "|".join(
            [
                str(session.get("code", "")),
                str(session.get("weekday", "")),
                str(session.get("time", "")),
                str(session.get("name", "")),
            ]
        )
        for session in sessions
    ]
    canonical = f"{len(sessions)}" + "\x1f" + "\x1f".join(parts)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return digest[:8]


# ---------------------------------------------------------------------------
# Pure recommendation core, extracted from noam_coach/bot/ui.py:539-567.
# ---------------------------------------------------------------------------


def pick_session(
    sessions: list[dict[str, Any]],
    done_today: set[str],
    today_weekday: int,
    last_code: str | None,
) -> tuple[str | None, str]:
    """Pure decision: given a plan's ordered sessions, the set of codes
    already completed/partial today, today's local weekday, and the code
    of the last performed workout (or None), return (code, reason).

    This is byte-equivalent core logic extracted from
    ``noam_coach.bot.ui.resolve_todays_workout`` (lines 539-567 at the time
    of extraction) -- it must never diverge from that function's decision,
    proven by the parity test in tests/test_workout_catalog.py. The caller
    (``resolve_todays_workout`` itself, or this module's
    ``resolve_recommended``) owns all I/O: fetching the plan, computing
    done_today from the sessions table, and resolving last_code.
    """
    cycle = [s["code"] for s in sessions]

    # 1) A session scheduled for today's weekday that wasn't done yet.
    for session in sessions:
        if session.get("weekday") == today_weekday and session["code"] not in done_today:
            return session["code"], "offer_today"

    # 2) Otherwise the next code in the cycle after the last performed workout.
    if last_code is not None and last_code in cycle:
        nxt = (cycle.index(last_code) + 1) % len(cycle)
        candidate = cycle[nxt]
        for _ in range(len(cycle)):
            if candidate not in done_today:
                return candidate, "offer_next"
            nxt = (nxt + 1) % len(cycle)
            candidate = cycle[nxt]
        return None, "all_done_today"

    # Fall back to the first code not yet done today.
    for code in cycle:
        if code not in done_today:
            return code, "offer_next"
    return None, "all_done_today"


async def _done_today_and_last_code(db: Any, user_id: int, current: datetime) -> tuple[set[str], str | None]:
    """Shared I/O core for both tiers' recommendation resolution -- mirrors
    resolve_todays_workout's own two queries exactly (ui.py:530-536,
    546-551), so both tiers see the identical completion evidence."""
    start, end = daily_state.local_day_bounds_utc(current)
    done_today_rows = await db.fetch_all(
        "SELECT DISTINCT code FROM sessions WHERE user_id=? "
        "AND status IN ('completed','partial') AND ended_at>=? AND ended_at<?",
        (user_id, start, end),
    )
    done_today = {r["code"] for r in done_today_rows}
    last = await db.fetch_one(
        "SELECT code FROM sessions WHERE user_id=? "
        "AND status IN ('completed','partial') "
        "ORDER BY ended_at DESC LIMIT 1",
        (user_id,),
    )
    return done_today, (last["code"] if last else None)


# ---------------------------------------------------------------------------
# Tier resolution.
# ---------------------------------------------------------------------------


async def _tier1_sessions(db: Any, user_id: int) -> tuple[int, list[dict[str, Any]]] | None:
    """Returns (plan_id, sessions) for the user's authoritative active
    workout plan, or None if they have none."""
    import planning

    plan = await planning.get_active_plan(db, user_id, "workout")
    if not plan:
        return None
    sessions = (plan.get("payload") or {}).get("sessions") or []
    if not sessions:
        return None
    return int(plan["id"]), sessions


async def _tier2_sessions(db: Any, user_id: int) -> tuple[str, list[dict[str, Any]]] | None:
    """Returns (fact_rev, sessions) for the user's fact-only weekly plan,
    or None if absent. Tier-2 is consulted only when Tier-1 has nothing --
    a real plan_versions row always wins (it is authoritative)."""
    fact = await user_model.get_value(db, user_id, "active_workout_plan")
    if not fact or not fact.get("sessions"):
        return None
    sessions = fact["sessions"]
    if not sessions:
        return None
    return compute_fact_rev(fact), sessions


async def list_selectable_workouts(
    db: Any, user_id: int, now: datetime | None = None
) -> list[WorkoutChoice]:
    """List every selectable session for the user's active plan (Tier-1 if
    present, else Tier-2), each marked with its done-today status and
    whether it is the recommended one. Empty list means no active plan of
    either tier.
    """
    current = (now or datetime.now(TZ)).astimezone(TZ)
    today_wd = current.weekday()

    tier1 = await _tier1_sessions(db, user_id)
    if tier1 is not None:
        plan_id, sessions = tier1
        tier = "plan"
        ref_kwargs: dict[str, Any] = {"tier": "plan", "plan_id": plan_id, "fact_rev": None}
    else:
        tier2 = await _tier2_sessions(db, user_id)
        if tier2 is None:
            return []
        fact_rev, sessions = tier2
        tier = "fact"
        ref_kwargs = {"tier": "fact", "plan_id": None, "fact_rev": fact_rev}

    done_today, last_code = await _done_today_and_last_code(db, user_id, current)
    recommended_code, reason = pick_session(sessions, done_today, today_wd, last_code)
    # pick_session (and resolve_todays_workout, byte-equivalently) always
    # returns on the FIRST session matching recommended_code in iteration
    # order (both its weekday-match scan and its done-today skip-forward
    # walk the session list positionally) -- so the first index whose code
    # matches is unambiguously the recommended one, even with duplicate
    # codes (Tier-2 ["F","F"] plans).
    recommended_index = (
        next((i for i, s in enumerate(sessions) if s.get("code") == recommended_code), None)
        if recommended_code is not None
        else None
    )

    del tier  # kept for readability at the call site above; unused beyond branching
    choices: list[WorkoutChoice] = []
    for index, session in enumerate(sessions):
        code = str(session.get("code") or "")
        choices.append(
            WorkoutChoice(
                ref=WorkoutSelectionRef(session_index=index, **ref_kwargs),
                code=code,
                name=str(session.get("name") or code),
                weekday=session.get("weekday"),
                done_today=code in done_today,
                recommended=(index == recommended_index),
                reason=reason,
            )
        )
    return choices


async def resolve_recommended(db: Any, user_id: int, now: datetime | None = None) -> WorkoutChoice | None:
    """The single recommended choice (never a forced selection) -- the
    first choice flagged `recommended=True`, or None if nothing is
    offerable (no plan / all done today)."""
    choices = await list_selectable_workouts(db, user_id, now=now)
    for choice in choices:
        if choice.recommended:
            return choice
    return None


async def resolve_selection(
    db: Any, user_id: int, ref: WorkoutSelectionRef, now: datetime | None = None
) -> ResolvedWorkout:
    """Validate `ref` against the user's CURRENT active plan/fact and
    return the fully normalized session it points to.

    Tier-1 sessions already carry a real exercise list (the personalized
    payload). Tier-2 sessions carry none at all (the thin
    weekday/time/code/name shape) -- their exercises are seeded from the
    global PLANS[code] template (a deep copy; PLANS itself is never
    mutated) before normalization runs, matching the plan's "guarded
    template path" for Tier-2 content.

    Raises StalePlanReference if the plan was regenerated (Tier-1 plan_id
    mismatch), the fact was replaced by a same-length sibling or removed
    (Tier-2 fact_rev mismatch), the session_index is out of range for the
    current session list, or (Tier-2 only) the session's code is not a
    known PLANS template.
    """
    if ref.tier == "plan":
        tier1 = await _tier1_sessions(db, user_id)
        if tier1 is None or tier1[0] != ref.plan_id:
            raise StalePlanReference(f"Tier-1 plan_id {ref.plan_id} is not the active plan")
        _, sessions = tier1
    elif ref.tier == "fact":
        tier2 = await _tier2_sessions(db, user_id)
        if tier2 is None or tier2[0] != ref.fact_rev:
            raise StalePlanReference(f"Tier-2 fact_rev {ref.fact_rev} no longer matches the live fact")
        _, sessions = tier2
    else:
        raise StalePlanReference(f"Unknown tier {ref.tier!r}")

    if not 0 <= ref.session_index < len(sessions):
        raise StalePlanReference(f"session_index {ref.session_index} out of range for {len(sessions)} sessions")

    session = dict(sessions[ref.session_index])
    code = str(session.get("code") or "")

    current = (now or datetime.now(TZ)).astimezone(TZ)
    done_today, last_code = await _done_today_and_last_code(db, user_id, current)
    recommended_code, reason = pick_session(sessions, done_today, current.weekday(), last_code)
    recommended_index = next((i for i, s in enumerate(sessions) if s.get("code") == recommended_code), None)
    choice = WorkoutChoice(
        ref=ref,
        code=code,
        name=str(session.get("name") or code),
        weekday=session.get("weekday"),
        done_today=code in done_today,
        recommended=(reason in ("offer_today", "offer_next") and ref.session_index == recommended_index),
        reason=reason,
    )

    raw_exercises = session.get("exercises")
    if not raw_exercises:
        # Tier-2 sessions are the thin build_weekly_plan shape
        # (weekday/time/code/name only, onboarding.py:2547-2555) -- they
        # carry NO exercise list at all. Content comes from the guarded
        # template path (plan section E): fact codes are always PLANS
        # keys, verified here rather than assumed, so a corrupted/unknown
        # code refuses instead of crashing (W5 posture) even though no
        # handler routes user input here yet in this batch.
        if code not in PLANS:
            raise StalePlanReference(f"Tier-2 session code {code!r} is not a known template")
        raw_exercises = json.loads(json.dumps(PLANS[code]["exercises"], ensure_ascii=False))

    normalized_exercises, defaults_filled = normalize_exercises(raw_exercises)
    normalized_session = dict(session)
    normalized_session["exercises"] = normalized_exercises

    return ResolvedWorkout(choice=choice, session=normalized_session, defaults_filled=defaults_filled)


# ---------------------------------------------------------------------------
# Normalization choke point.
# ---------------------------------------------------------------------------


def _template_exercise_index() -> dict[str, dict[str, Any]]:
    """Every template exercise across all PLANS codes, keyed by stable id.
    Built fresh per call (PLANS is a small, static, in-memory dict) so this
    module never caches a copy that could drift from PLANS across process
    lifetime -- correctness over micro-optimization for a rarely-hot path
    until this batch wires anything up."""
    index: dict[str, dict[str, Any]] = {}
    for plan in PLANS.values():
        for exercise in plan.get("exercises", []):
            exercise_id = exercise.get("id")
            if exercise_id and exercise_id not in index:
                index[exercise_id] = exercise
    return index


def normalize_exercise(
    exercise: dict[str, Any], template_by_id: dict[str, dict[str, Any]]
) -> tuple[dict[str, Any], list[str]]:
    """Fill ONLY missing keys on one exercise entry. Never overrides a
    value already present in the payload (containment against template
    divergence -- a personalized exercise's real prescription always wins).

    Fill order: (1) the matching template exercise by stable id, if any
    field is still missing after that (2) safe, load-inventing-free
    defaults. Returns (normalized_exercise, defaults_filled_field_names).
    """
    result = dict(exercise)
    exercise_id = result.get("id")
    template = template_by_id.get(exercise_id) if exercise_id else None
    filled: list[str] = []

    for key in _FILLABLE_FIELDS:
        if key in result and result[key] is not None:
            continue
        if template is not None and key in template and template[key] is not None:
            result[key] = template[key]
        else:
            result[key] = {
                "sets": _DEFAULT_SETS,
                "rmin": _DEFAULT_RMIN,
                "rmax": _DEFAULT_RMAX,
                "rest": _DEFAULT_REST,
                "weight": _DEFAULT_WEIGHT,
                "inc": _DEFAULT_INC,
                "cues": [],
                "alts": [],
                "muscle": EXERCISE_MUSCLES.get(exercise_id, "") if exercise_id else "",
            }[key]
        filled.append(key)

    if "name" not in result or not result["name"]:
        result["name"] = (template or {}).get("name") or exercise_id or ""
        filled.append("name")

    # Defensive type coercion (sets/rmin/rmax/rest are integers; weight/inc
    # stay float -- matches profile.get_user_plan's existing convention).
    for int_key in ("sets", "rmin", "rmax", "rest"):
        try:
            result[int_key] = int(result[int_key])
        except (TypeError, ValueError):
            result[int_key] = {
                "sets": _DEFAULT_SETS, "rmin": _DEFAULT_RMIN, "rmax": _DEFAULT_RMAX, "rest": _DEFAULT_REST,
            }[int_key]
    for float_key in ("weight", "inc"):
        try:
            result[float_key] = float(result[float_key])
        except (TypeError, ValueError):
            result[float_key] = _DEFAULT_WEIGHT if float_key == "weight" else _DEFAULT_INC
    if not isinstance(result.get("cues"), list):
        result["cues"] = []
    if not isinstance(result.get("alts"), list):
        result["alts"] = []

    return result, filled


def normalize_exercises(
    exercises: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    """Normalize every exercise in a session. Returns (exercises,
    defaults_filled) where defaults_filled maps exercise_id -> filled
    field names, present only for exercises that actually needed a fill
    (an empty overall dict when nothing was missing)."""
    template_by_id = _template_exercise_index()
    normalized: list[dict[str, Any]] = []
    defaults_filled: dict[str, list[str]] = {}
    for exercise in exercises:
        result, filled = normalize_exercise(exercise, template_by_id)
        normalized.append(result)
        if filled:
            defaults_filled[result.get("id", "")] = filled
    return normalized, defaults_filled


# ---------------------------------------------------------------------------
# Override collection (plan section G) -- code-scoped, id-verified.
# ---------------------------------------------------------------------------


async def collect_overrides(db: Any, user_id: int, session: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Resolve the effective override for every (exercise, field) that has
    ANY candidate row for this session's code, applying the plan's exact
    rule set (section G):

    1. Only rows where row.code == session.code are candidates at all --
       no cross-code sharing (a heavy-day and volume-day copy of the same
       exercise id must stay independent).
    2. A row applies to a session exercise only when its exercise identity
       is verified: either row.exercise_id == exercise.id (new-style
       writes), or for legacy NULL-id rows, the row's positional index
       resolves to the SAME exercise id in the global PLANS[code] template
       (PLANS[code]["exercises"][row.exercise_index]["id"] == exercise.id).
       A NULL-id row whose position doesn't verify is never applied to
       this exercise -- never "apply by bare index alone".
    3. When multiple candidate rows verify for the same (exercise_id,
       field), the winner is chosen by `updated_at` DESC, then `rowid`
       DESC (deterministic; never dict/iteration order).

    Returns {exercise_id: {field: value}} -- ready to be applied on top of
    a normalized session's exercises by exercise id. This function does
    NOT mutate the session; callers apply the result explicitly. No
    production handler calls this yet (Batch 3 is unwired) -- the existing
    legacy `get_user_plan` read path is completely untouched and keeps
    using its own purely-positional query.
    """
    code = str(session.get("code") or "")
    exercises = session.get("exercises") or []
    exercise_by_index = {index: ex.get("id") for index, ex in enumerate(exercises)}
    exercise_ids_in_session = {ex.get("id") for ex in exercises if ex.get("id")}

    rows = await db.fetch_all(
        "SELECT rowid, exercise_index, field, value, exercise_id, updated_at "
        "FROM exercise_overrides WHERE user_id=? AND code=? "
        "ORDER BY updated_at DESC, rowid DESC",
        (user_id, code),
    )

    template_exercises = PLANS.get(code, {}).get("exercises", [])

    result: dict[str, dict[str, Any]] = {}
    seen: set[tuple[str, str]] = set()
    for row in rows:
        field = row["field"]
        if field not in OVERRIDE_FIELDS:
            continue
        row_exercise_id = row["exercise_id"]
        if row_exercise_id:
            target_id = row_exercise_id if row_exercise_id in exercise_ids_in_session else None
        else:
            idx = int(row["exercise_index"])
            target_id = None
            if 0 <= idx < len(template_exercises):
                template_id = template_exercises[idx].get("id")
                if template_id and exercise_by_index.get(idx) == template_id:
                    target_id = template_id
        if not target_id:
            continue
        key = (target_id, field)
        if key in seen:
            continue  # a later (lower-priority) row for the same target -- tie-break already picked the winner
        seen.add(key)
        value = row["value"]
        result.setdefault(target_id, {})[field] = value if field == "weight" else int(value)
    return result


# ---------------------------------------------------------------------------
# Snapshot materialization (returns a dict only -- the INSERT stays in the
# handler; nothing here writes to the database).
# ---------------------------------------------------------------------------


def materialize_snapshot(
    user_id: int,
    resolved: ResolvedWorkout,
    overrides: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the session-shaped dict a Start action would snapshot into
    `sessions.plan`, with `overrides` (from collect_overrides) applied on
    top of the normalized session by exercise id, plus full provenance
    (plan section H). Does not touch the database -- Batch 5 wires this
    into the actual Start INSERT.
    """
    del user_id  # not needed for materialization itself; kept for API symmetry with future audit hooks
    overrides = overrides or {}
    exercises = []
    overrides_applied: list[dict[str, Any]] = []
    for exercise in resolved.session.get("exercises", []):
        entry = dict(exercise)
        exercise_id = entry.get("id")
        for override_field, value in overrides.get(exercise_id, {}).items():
            entry[override_field] = value
            overrides_applied.append({"exercise_id": exercise_id, "field": override_field, "value": value})
        exercises.append(entry)

    ref = resolved.choice.ref
    source = "active_plan" if ref.tier == "plan" else "weekly_fact_template"
    provenance = {
        "schema_version": 2,
        "source": source,
        "plan_id": ref.plan_id,
        "session_index": ref.session_index,
        "session_code": resolved.choice.code,
        "session_name": resolved.choice.name,
        "overrides_applied": overrides_applied,
        "defaults_filled": resolved.defaults_filled,
        "materialized_at": datetime.now(TZ).astimezone().isoformat(),
        "app_version": APP_VERSION,
        "normalizer_version": NORMALIZER_VERSION,
    }

    return {
        "name": resolved.choice.name,
        "exercises": exercises,
        "provenance": provenance,
    }
