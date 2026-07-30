"""Batch 3 (workout-selection architecture): architecture guards that turn
two of the plan's core identity invariants into enforced, mechanical
contracts, following the established style of tests/test_architecture.py
and tests/regression/test_re10_regression.py's callback-prefix scan
(regex over raw source text, deliberately narrow to avoid false positives
from comments/docstrings/unrelated strings).
"""
from __future__ import annotations

import ast
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


# ---------------------------------------------------------------------------
# Guard 3 (A4): the WRITE surface of the active_workout_plan fact.
#
# Guard 2 above governs get_value only. `set_fact` was invisible to it, so the
# two writers below were never governed by anything -- the weaker store had the
# weaker protection, which is backwards: plan_versions is copy-on-write and
# test-enforced, while the fact mirror could be overwritten from anywhere with
# no review signal at all.
#
# This guard parses instead of matching. Guard 2's pattern
# (get_value\s*\([^,]+,[^,]+,\s*"active_workout_plan") cannot see a call whose
# first two arguments contain a comma -- get_value(get_db(a, b), uid, ...)
# passes it silently. A write guard built the same way would inherit that hole
# while looking stronger.
# ---------------------------------------------------------------------------

#: The fact key this guard governs.
_GOVERNED_FACT_KEY = "active_workout_plan"

#: Files permitted to WRITE the fact. Deliberately NOT the reader allowlist:
#: reading the mirror from a UI path is reasonable, writing it is an authority
#: claim.
#:
#: THE CONTRACT -- later items depend on this being explicit rather than implied:
#:
#:   1. Permitted owners: exactly the files below. A new writer is an
#:      architecture decision, not a test update.
#:   2. Legitimate wrappers: a helper that writes the fact must LIVE IN one of
#:      these files. Wrapping the write elsewhere and calling it from here does
#:      not make it legitimate -- this guard checks where the `set_fact` call
#:      physically is, so an indirect writer fails.
#:   3. Indirect/dynamic calls: a non-literal key is flagged wherever it
#:      appears (see test_no_production_set_fact_call_hides_its_key), because
#:      the guard cannot prove what such a call writes. An unprovable write is
#:      treated as a violation rather than waved through.
#:   4. Verification: test_the_write_guard_actually_detects_a_violation drives
#:      synthetic offenders through the same scanner the real test uses. A guard
#:      that has never been seen to reject anything is not evidence.
#:
#: Do NOT add an entry here to make a new route pass. Route the write through an
#: existing owner instead. Broadening this set to satisfy CI is the precise
#: failure mode this guard exists to prevent.
_ALLOWED_FACT_WRITER_FILES = frozenset(
    {
        "planning.py",
        "noam_coach/bot/onboarding.py",
    }
)


def _set_fact_key_arg(node: ast.Call) -> ast.expr | None:
    """The `key` argument of a set_fact call, positional or keyword.

    Signature is set_fact(db, user_id, key, value, ...) -- key is third.
    """
    for keyword in node.keywords:
        if keyword.arg == "key":
            return keyword.value
    if len(node.args) >= 3:
        return node.args[2]
    return None


def _callee_name(node: ast.Call) -> str:
    """The called name, ignoring how it was reached.

    Matching on the attribute alone means `user_model.set_fact`, an aliased
    `um.set_fact`, and a bare imported `set_fact` are all caught -- module
    aliasing is not a bypass.
    """
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _scan_fact_writes(text: str) -> tuple[int, list[int]]:
    """(governed_write_count, lines_with_unprovable_keys) for one source text.

    A governed write names the fact key as a string literal. An unprovable key
    is any other expression -- a variable, an f-string, a dict lookup. The two
    are reported separately: the first is an ownership question, the second is
    a hole in what static analysis can establish at all.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:  # pragma: no cover - syntax errors fail compileall
        return 0, []

    governed = 0
    unprovable: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _callee_name(node) != "set_fact":
            continue
        key = _set_fact_key_arg(node)
        if key is None:
            continue
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            if key.value == _GOVERNED_FACT_KEY:
                governed += 1
        else:
            unprovable.append(getattr(key, "lineno", 0))
    return governed, unprovable


def _iter_fact_writers() -> dict[str, int]:
    writers: dict[str, int] = {}
    for path in _iter_production_py_files():
        relative = str(path.relative_to(ROOT)).replace("\\", "/")
        governed, _ = _scan_fact_writes(path.read_text(encoding="utf-8"))
        if governed:
            writers[relative] = governed
    return writers


def test_active_workout_plan_writer_allowlist_can_only_shrink() -> None:
    """Fails if any production file outside the allowlist writes the fact.

    The write-side counterpart to Guard 2. It matters because the fact is a
    *mirror*: plan_versions is authoritative, and an ungoverned write to the
    mirror is exactly how the two stores drift apart.
    """
    writers = _iter_fact_writers()
    unexpected = set(writers) - _ALLOWED_FACT_WRITER_FILES
    assert not unexpected, (
        "Found active_workout_plan fact WRITERS outside the approved allowlist: "
        f"{sorted(unexpected)}. The fact is a derived mirror of plan_versions -- "
        "route the change through a permitted owner rather than writing the "
        "mirror directly. Do not add the new file to _ALLOWED_FACT_WRITER_FILES "
        "to make this test pass; that defeats the guard while appearing to "
        "satisfy it."
    )


def test_active_workout_plan_allowlist_writers_are_still_present() -> None:
    """Bidirectional, like Guard 2: a writer vanishing silently is also drift.

    When the plan-mutation boundary takes ownership of the write, this test is
    the one that will fail -- and updating it then is a deliberate record of the
    handover rather than an unnoticed change.
    """
    writers = _iter_fact_writers()
    missing = _ALLOWED_FACT_WRITER_FILES - set(writers)
    assert not missing, (
        f"Expected active_workout_plan writer(s) no longer found: {sorted(missing)}. "
        "Update _ALLOWED_FACT_WRITER_FILES if this write was legitimately moved "
        "or removed -- for example once the plan-mutation boundary owns it."
    )


#: Files where a dynamic-key `set_fact` is expected and legitimate: generic
#: fact plumbing (write-any-fact helpers, gap markers, wizard confirmation) and
#: free-text handlers that resolve which fact the user just answered. These are
#: the fact model working as designed, not holes.
#:
#: Measured at the time this guard landed: 27 such call sites across 15 files.
#: Demanding they all pass literals would rewrite the fact layer to protect a
#: single key, so the rule is scoped instead -- see the residual limitation in
#: test_dynamic_key_writers_stay_confined below.
_DYNAMIC_KEY_FACT_WRITERS = frozenset(
    {
        "user_model.py",
        "onboarding.py",
        "questions.py",
        "health_service.py",
        "mini_api.py",
        "noam_coach/bot/assistant.py",
        "noam_coach/bot/onboarding.py",
        "noam_coach/services/coaching_memory.py",
        "noam_coach/services/day_plan.py",
        "noam_coach/services/food_preferences.py",
        "noam_coach/services/health_jobs.py",
        "noam_coach/services/morning_policy.py",
        "noam_coach/services/multi_fact.py",
        "noam_coach/services/question_dedup.py",
    }
)


def test_dynamic_key_writers_stay_confined() -> None:
    """Where a dynamic-key `set_fact` may appear is itself frozen.

    RESIDUAL LIMITATION, stated rather than hidden: a static guard cannot prove
    what `set_fact(db, uid, key, value)` writes. Any file in the set above
    could in principle write the governed fact through a computed key, and this
    guard would not see it. That is why the runtime assertion exists -- the
    static half narrows the surface, the runtime half is what actually holds.

    What this test does buy: the surface cannot grow silently. A NEW file
    introducing a dynamic-key write is a deliberate decision, and any new
    module in the workout-plan path must use a literal key so the ownership
    guard above can reason about it.
    """
    offenders: list[str] = []
    for path in _iter_production_py_files():
        relative = str(path.relative_to(ROOT)).replace("\\", "/")
        if relative in _DYNAMIC_KEY_FACT_WRITERS:
            continue
        _, unprovable = _scan_fact_writes(path.read_text(encoding="utf-8"))
        offenders.extend(f"{relative}:{line}" for line in unprovable)

    assert not offenders, (
        "set_fact called with a non-literal key in a file not approved for it, "
        f"so no static guard can prove which fact is written: {sorted(offenders)}. "
        "Pass the key as a string literal so the ownership guard can see it, or "
        "-- if this really is generic fact plumbing -- add the file to "
        "_DYNAMIC_KEY_FACT_WRITERS deliberately and say why."
    )


def test_the_write_guard_actually_detects_a_violation() -> None:
    """The guard must be seen to fail, not merely to pass.

    A guard that has never rejected anything is indistinguishable from one that
    cannot. These drive synthetic offenders through the same scanner the real
    test uses, covering the shapes that matter: a plain attribute call, an
    aliased module, a keyword-passed key, and -- the reason this guard parses --
    commas inside the first two arguments.
    """
    plain = 'user_model.set_fact(DB, user_id, "active_workout_plan", plan)'
    aliased = 'um.set_fact(db, uid, "active_workout_plan", payload, confirmed=True)'
    keyword = 'set_fact(db, uid, key="active_workout_plan", value=plan)'
    multiline_first_args = (
        "user_model.set_fact(\n"
        "    get_db(a, b),\n"
        "    resolve_user(x, y),\n"
        '    "active_workout_plan",\n'
        "    plan,\n"
        ")"
    )

    for source in (plain, aliased, keyword, multiline_first_args):
        governed, _ = _scan_fact_writes(source)
        assert governed == 1, f"guard failed to detect a governed write in: {source!r}"

    # Guard 2's regex cannot see the multiline form -- commas inside the first
    # two arguments defeat its [^,]+ segments. Pinning that here documents why
    # the write guard parses instead of matching, and will start failing if
    # Guard 2 is ever strengthened.
    assert (
        _GET_VALUE_ACTIVE_WORKOUT_PLAN_RE.search(
            multiline_first_args.replace("set_fact", "get_value")
        )
        is None
    ), (
        "Guard 2's known blind spot has closed; revisit this note and consider "
        "whether the reader guard should now be AST-based too."
    )

    # A write to a DIFFERENT fact must not be flagged.
    unrelated = 'user_model.set_fact(DB, user_id, "training_limitations", value)'
    governed, _ = _scan_fact_writes(unrelated)
    assert governed == 0, "guard must not flag writes to other facts"


def test_the_write_guard_detects_a_hidden_key() -> None:
    """The non-literal-key rule must also be seen to fire."""
    hidden = 'key = "active_workout_plan"\nuser_model.set_fact(db, uid, key, plan)'
    _, unprovable = _scan_fact_writes(hidden)
    assert unprovable, "guard failed to flag a set_fact whose key is a variable"


#: Files allowed to write the `user_facts` table with raw SQL.
#:
#: The runtime assertion lives inside `set_fact`, so it protects only writes
#: that go THROUGH that function. A direct INSERT/UPDATE bypasses it entirely
#: and would be invisible to both halves of the governance. This freezes the
#: set of files that can do that at all.
#:
#: `user_model.py` is the fact layer itself -- its own INSERT/UPDATE statements
#: ARE the implementation of set_fact and the history append.
#:
#: Only `user_model.py` gets a whole-file exemption: its INSERT/UPDATE
#: statements ARE the implementation of set_fact and the history append.
_ALLOWED_RAW_FACT_SQL_FILES = frozenset({"user_model.py"})

#: Narrower exemptions, scoped to a single FUNCTION rather than a whole file.
#:
#: `db.py` would otherwise need a blanket pass for one historical migration,
#: which would silently permit any future raw fact write anywhere in a 1500-line
#: module. Scoping to the function keeps the rest of db.py governed.
#:
#: `_migration_clean_polluted_gap_values` is migration 10, a one-time cleanup of
#: corrupted gap strings. It is bounded twice over: it selects only
#: `kind='fact' AND value LIKE '%missing%'`, then skips any row whose decoded
#: value is not a `str`. The governed fact is a dict, so the path cannot reach
#: it. Established by reading the migration, not inferred from its name.
_ALLOWED_RAW_FACT_SQL_FUNCTIONS = frozenset(
    {"db.py::_migration_clean_polluted_gap_values"}
)

#: INSERT INTO user_facts / UPDATE user_facts, in raw SQL string literals.
_RAW_FACT_TABLE_WRITE_RE = re.compile(
    r"(?:INSERT\s+INTO|UPDATE)\s+user_facts\b",
    re.IGNORECASE,
)


def _exempt_function_ranges(relative: str, source: str) -> list[tuple[int, int]]:
    """Line ranges of functions exempted for raw fact SQL in this file.

    Function-scoped rather than file-scoped so that exempting one historical
    migration does not quietly license every future raw write in the same
    module.
    """
    ranges: list[tuple[int, int]] = []
    exempt_names = {
        entry.split("::", 1)[1]
        for entry in _ALLOWED_RAW_FACT_SQL_FUNCTIONS
        if entry.split("::", 1)[0] == relative
    }
    if not exempt_names:
        return ranges
    try:
        tree = ast.parse(source)
    except SyntaxError:  # pragma: no cover
        return ranges
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in exempt_names:
                ranges.append((node.lineno, node.end_lineno or node.lineno))
    return ranges


def test_no_new_raw_sql_write_path_to_user_facts() -> None:
    """A direct INSERT/UPDATE would bypass the entire governance mechanism.

    The runtime assertion sits inside `set_fact`. Raw SQL never calls it, so a
    new module writing `user_facts` directly would defeat the static guard, the
    runtime assertion, and the writer allowlist all at once -- silently, and
    without touching any of the code those guards inspect.

    This does not attempt to prove the two permitted files cannot reach the
    governed key; it proves no THIRD file can open a new path. That bound is
    what makes the rest of the governance meaningful.
    """
    offenders: list[str] = []
    for path in _iter_production_py_files():
        relative = str(path.relative_to(ROOT)).replace("\\", "/")
        if relative in _ALLOWED_RAW_FACT_SQL_FILES:
            continue
        source = path.read_text(encoding="utf-8")
        exempt_ranges = _exempt_function_ranges(relative, source)
        for match in _RAW_FACT_TABLE_WRITE_RE.finditer(source):
            line_number = source.count("\n", 0, match.start()) + 1
            if any(start <= line_number <= end for start, end in exempt_ranges):
                continue
            offenders.append(f"{relative}:{line_number}")

    assert not offenders, (
        "Found raw SQL writes to the user_facts table outside the fact layer: "
        f"{offenders}. These bypass user_model.set_fact and therefore bypass "
        "the governed-fact runtime assertion entirely. Route the write through "
        "set_fact so it is subject to the same authorization as every other "
        "fact write."
    )


def test_the_raw_sql_guard_actually_detects_a_violation() -> None:
    """Verified by breakage, like every other guard here."""
    offending_sources = (
        'await db.execute("INSERT INTO user_facts(user_id, key) VALUES(?, ?)", args)',
        'await conn.execute("UPDATE user_facts SET value=? WHERE key=?", args)',
        'await conn.execute("update  user_facts  set value=?", args)',  # case/spacing
    )
    for source in offending_sources:
        assert _RAW_FACT_TABLE_WRITE_RE.search(source), (
            f"raw-SQL guard failed to detect: {source!r}"
        )

    # A read must not be flagged -- the guard is about write paths.
    assert _RAW_FACT_TABLE_WRITE_RE.search(
        'await db.fetch_all("SELECT value FROM user_facts WHERE user_id=?", args)'
    ) is None
