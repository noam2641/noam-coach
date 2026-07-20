"""Batch 3 (workout-selection architecture): architecture guards that turn
two of the plan's core identity invariants into enforced, mechanical
contracts, following the established style of tests/test_architecture.py
and tests/regression/test_re10_regression.py's callback-prefix scan
(regex over raw source text, deliberately narrow to avoid false positives
from comments/docstrings/unrelated strings).
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Directories that are never production source for this scan: tests, the
# review worktree's own sibling worktrees (not applicable inside a single
# worktree, but excluded defensively), caches, and version control.
_EXCLUDED_DIR_NAMES = {"tests", "__pycache__", ".git", "node_modules", ".venv"}


def _iter_production_py_files() -> list[Path]:
    files: list[Path] = []
    for path in ROOT.rglob("*.py"):
        if any(part in _EXCLUDED_DIR_NAMES for part in path.relative_to(ROOT).parts):
            continue
        files.append(path)
    return files


# ---------------------------------------------------------------------------
# Guard 1: plan_versions.payload is copy-on-write -- no production code may
# ever UPDATE it in place. This is the load-bearing assumption behind the
# Tier-1 identity (plan_id, session_index): a payload UPDATE would let an
# existing plan_id silently point at different session content.
# ---------------------------------------------------------------------------

# Matches "UPDATE plan_versions" (any whitespace/case) followed, within a
# reasonable distance, by an assignment to `payload=`. Deliberately scans
# raw SQL string literals in .py source (this codebase writes SQL as
# triple-quoted strings, not through an ORM), not just plain "UPDATE"
# statements -- narrow enough that legitimate status/timestamp-only
# UPDATEs (planning.py:1432,1437) do not match, since they never assign
# `payload=`.
_UPDATE_PLAN_VERSIONS_RE = re.compile(
    r"UPDATE\s+plan_versions\b[^;]{0,400}?\bpayload\s*=",
    re.IGNORECASE | re.DOTALL,
)


def test_no_production_code_updates_plan_versions_payload() -> None:
    """Fails if any production .py file contains an UPDATE plan_versions
    statement that assigns `payload=`. Regeneration must remain
    copy-on-write (INSERT a new plan_versions row + supersede the old one)
    -- never mutate an existing row's payload in place. See the
    architecture plan, section F: 'session indexes are valid identities
    ONLY within an immutable plan revision'.
    """
    offenders: list[str] = []
    for path in _iter_production_py_files():
        text = path.read_text(encoding="utf-8")
        for match in _UPDATE_PLAN_VERSIONS_RE.finditer(text):
            line_number = text.count("\n", 0, match.start()) + 1
            offenders.append(f"{path.relative_to(ROOT)}:{line_number}")
    assert not offenders, (
        "Found production code that UPDATEs plan_versions.payload in place, "
        f"breaking the (plan_id, session_index) copy-on-write identity contract: {offenders}. "
        "Regeneration must INSERT a new plan_versions row and supersede the old one instead."
    )


# ---------------------------------------------------------------------------
# Guard 2: the allowlist of direct active_workout_plan fact readers can
# only shrink. workout_catalog is meant to become the ONLY resolver for
# interaction flows -- new direct get_value(..., "active_workout_plan")
# call sites outside the allowlist are a regression back toward the
# split-brain (W1) the redesign exists to contain.
# ---------------------------------------------------------------------------

# The plan (architecture plan section E) names ui.py:523, assistant.py:982,
# callback_plans.py:614 (until Batch 6), and user_state.py as "current
# legitimate readers". Direct inspection of this checkout at Batch 3 time
# found user_state.py does NOT actually call get_value(...,
# "active_workout_plan") anywhere -- it only uses the string
# "active_workout_plan" as a WorkoutState.source label sourced from
# planning.get_active_plan (Tier-1), which the module's own docstring
# states explicitly (user_state.py:9-11: it does not replace or
# reconstruct ui.py's fact-mirror logic). This guard freezes the allowlist
# at the three files verified to contain a real call site; user_state.py
# is intentionally NOT included since it is not a reader. If a future
# change makes it one, this guard should fail and be updated deliberately,
# not have the allowlist pre-expanded to accommodate an undiscovered case.
_ALLOWED_READER_FILES = frozenset(
    {
        "noam_coach/bot/ui.py",
        "noam_coach/bot/assistant.py",
        "noam_coach/bot/callback_plans.py",
    }
)

# Matches get_value(<db>, <user_id>, "active_workout_plan") allowing for
# any argument spelling/whitespace in the first two positions (the DB
# handle and user_id expressions vary by call site) but requiring the
# literal fact key as the third argument.
_GET_VALUE_ACTIVE_WORKOUT_PLAN_RE = re.compile(
    r'get_value\s*\([^,]+,[^,]+,\s*"active_workout_plan"',
)


def _iter_active_workout_plan_readers() -> dict[str, int]:
    """Returns {relative_path: match_count} for every production .py file
    (excluding workout_catalog.py itself, and this guard file) containing
    at least one get_value(..., "active_workout_plan") call site."""
    readers: dict[str, int] = {}
    for path in _iter_production_py_files():
        relative = str(path.relative_to(ROOT)).replace("\\", "/")
        if relative == "noam_coach/services/workout_catalog.py":
            continue  # the catalog itself reads the fact for Tier-2 -- by design, not a violation
        text = path.read_text(encoding="utf-8")
        count = len(_GET_VALUE_ACTIVE_WORKOUT_PLAN_RE.findall(text))
        if count:
            readers[relative] = count
    return readers


def test_active_workout_plan_direct_reader_allowlist_can_only_shrink() -> None:
    """Fails if any production file OUTSIDE the frozen allowlist directly
    reads the active_workout_plan fact. workout_catalog.py is the intended
    sole resolver for interaction flows going forward; every new caller
    should go through it (or Tier-1's planning.get_active_plan) instead of
    reaching into the fact mirror directly.
    """
    readers = _iter_active_workout_plan_readers()
    unexpected = set(readers) - _ALLOWED_READER_FILES
    assert not unexpected, (
        f"Found direct active_workout_plan fact readers outside the approved allowlist: {sorted(unexpected)}. "
        "Route through noam_coach.services.workout_catalog instead. If this is a "
        "genuinely new legitimate reader, that is itself a signal worth stopping "
        "and reporting on, per the Batch 3 task instructions -- do not silently "
        "broaden _ALLOWED_READER_FILES to make this test pass."
    )


def test_active_workout_plan_allowlist_readers_are_still_present() -> None:
    """Companion sanity check: every allowlisted file still actually
    contains a matching call site (catches the allowlist going stale in
    the other direction -- a file removed from the codebase, or a call
    site refactored away, without updating this guard)."""
    readers = _iter_active_workout_plan_readers()
    missing = _ALLOWED_READER_FILES - set(readers)
    assert not missing, (
        f"Expected active_workout_plan reader(s) no longer found: {sorted(missing)}. "
        "Update _ALLOWED_READER_FILES if this reader was legitimately removed/refactored."
    )
