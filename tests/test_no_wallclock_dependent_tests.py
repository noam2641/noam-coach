"""No test may pass or fail depending on the hour it runs.

`test_mini_api_returns_same_rendered_recommendation` seeded a meal at a
hardcoded 15:00 and then let the endpoint resolve its own `now`. A meal
pinned to 15:00 is in the *future* for any run before 15:00, falls outside the
coaching day, and the assertion read 0 instead of 700.

Measured: it was broken **15 hours out of every 24**. It survived only because
the work happened to be done in the afternoon, and it broke CI at 00:40 the
moment someone worked late. A test that passes 9 hours a day is not flaky --
it is wrong, and the clock decides whether anyone finds out.

This guard makes that combination impossible to reintroduce silently: a test
that pins an hour must also inject that time into the code under test, so the
production clock is never consulted.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"

#: `datetime.now(TZ).replace(hour=...)` and friends — a wall-clock read whose
#: hour is then overridden, which is what creates the dependency.
_PINNED_HOUR = re.compile(r"\.now\([^)]*\)\s*\.replace\(\s*hour\s*=", re.S)

#: Evidence the test threads its own time into the code under test rather than
#: letting production call `datetime.now()`.
#:
#: Matched structurally, not by exact string. A first version looked for the
#: literal `now=now` and flagged two innocent tests -- one passes its time
#: positionally, the other names the variable `early_evening`. A guard whose
#: false positives must be exempted one by one decays into an allowlist, which
#: is how the original defect would slip back in.
_INJECTS_TIME = (
    #: a keyword ARGUMENT carrying the time into the code under test.
    #: The preceding "," or "(" is load-bearing: without it this matches the
    #: assignment `now = datetime.now(TZ)...` that CREATES the pinned value,
    #: so every offender looks injected and the guard passes on the very bug
    #: it exists to catch. That is exactly what the first two versions did.
    re.compile(r"[(,]\s*(?:local_)?(?:now|anchor|when|clock)\s*="),
    #: the pinned variable passed POSITIONALLY as the last argument, e.g.
    #: `parse_reschedule_time("19:30", now)`. Deliberately anchored to `, now)`
    #: rather than "a time-ish word anywhere in a call" -- the loose version
    #: also matched `_ready_user(db, now)`, which merely SEEDS the database and
    #: is the defect itself.
    re.compile(r",\s*(?:local_)?now\s*\)"),
    #: an explicitly frozen or patched clock
    re.compile(r"freeze|monkeypatch\.setattr\([^)]*(?:datetime|now|utcnow)"),
)

#: Calls that seed fixture data from a pinned time. Seeding alone is NOT
#: injection: `_ready_user(db, now)` puts the pinned hour into the DATABASE
#: while the code under test still reads the real clock -- which is exactly
#: the defect. A first version of this guard accepted any positional use of a
#: time-looking variable and therefore waved the original bug straight
#: through; restoring the defect did not fail the guard. Seeding is subtracted
#: before asking whether the time was injected anywhere.
_SEEDS_FIXTURE = re.compile(
    r"\b(?:_ready_user|_meal|_add_meal|_seed\w*|_workout_plan|_daily_flags|"
    r"_active_goal|_set_fact|INSERT\s+INTO)\b[^\n]*",
    re.I,
)


def _test_files() -> list[Path]:
    return sorted(p for p in TESTS.rglob("test_*.py") if "__pycache__" not in p.parts)


def _functions_with_pinned_hours(path: Path) -> list[str]:
    """Test functions that pin an hour but never inject it anywhere."""
    source = path.read_text(encoding="utf-8", errors="ignore")
    if not _PINNED_HOUR.search(source):
        return []

    tree = ast.parse(source)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("test_"):
            continue
        body = ast.get_source_segment(source, node) or ""
        if not _PINNED_HOUR.search(body):
            continue
        # Seeding lines are removed first: putting a pinned time into the
        # database is not the same as handing it to the code under test.
        non_seeding = _SEEDS_FIXTURE.sub("", body)
        if any(pattern.search(non_seeding) for pattern in _INJECTS_TIME):
            continue
        offenders.append(f"{path.relative_to(ROOT).as_posix()}::{node.name}")
    return offenders


def test_a_pinned_hour_must_be_injected_not_left_to_the_real_clock() -> None:
    """The regression: a fixed hour + a production `now()` = a timed bomb.

    Pinning an hour is fine on its own -- most tests here do it and pass the
    value into the call under test. The defect is pinning an hour and then
    letting production read the real clock, because the two only agree during
    part of the day.

    If this fails, do not add the test to an exemption list. Either inject the
    pinned time into the code under test, or seed relative to `datetime.now`
    so the fixture is valid at every hour.
    """
    offenders: list[str] = []
    for path in _test_files():
        offenders.extend(_functions_with_pinned_hours(path))

    assert not offenders, (
        "these tests pin an hour but never inject it, so they depend on the "
        "time of day they run:\n  " + "\n  ".join(offenders)
    )
