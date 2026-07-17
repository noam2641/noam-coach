"""Review batch R6 — workflow acceptance tests.

Mapping to the program's acceptance list (items covered elsewhere are
noted): 1 multi-day window (here), 2 >2000-event package (R4), 3
interrupted build (R4), 4 interrupted review (R5), 5 old-review rebuild
(here, via CLI --rebuild), 6 overlapping windows (R2), 7 deterministic
ordering (R2), 8 crash capture + re-raise (R1, re-proven here through the
full pipeline), 9 cancellation (R1), 10 sensitive values (R4/R5), 11 raw
artifacts gitignored (here), 12 review makes no product change (builder
read-only, R4; procedural for the skill), 13 unapproved not implementable
(R5), 14 malformed approval rejected (R5), 15 SPECULATIVE gate (R5), 16
finding inspection (R5), 17 harness reproduction of a supported finding
(here), 18 no false replay claims (here), 19 stale finding not
implemented (here), 20 push-not-merge (procedural, skill text asserted
here).
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
from pathlib import Path

import pytest

import coach_bot
import event_log
import mini_api
from db import Database
from helpers import utc_now
from noam_coach.observability import ObservabilityMode, set_mode
from noam_coach.observability.harness import run_failing_user_turn
from noam_coach.observability.modes import reset_mode
from noam_coach.observability.taxonomy import ERROR_CAPTURED
from scripts.review_findings import (
    REPRODUCTION_STATUSES,
    approved_findings,
    empty_findings_document,
)
from scripts.review_findings import main as findings_main
from scripts.review_session import main as session_main

USER_ID = 1
REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
async def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Database:
    database = Database(str(tmp_path / "acc.db"))
    await database.init()
    await database.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(?, 'Test', NULL, ?)",
        (USER_ID, utc_now()),
    )
    monkeypatch.setattr(coach_bot, "DB", database)
    monkeypatch.setattr(mini_api, "DB", database)
    from config import SETTINGS

    monkeypatch.setattr(SETTINGS, "telegram_allowed_user_id", USER_ID, raising=False)
    set_mode(ObservabilityMode.CONTENT)
    yield database
    reset_mode()


# ---------------------------------------------------------------------------
# 11 — raw private artifacts can never be staged
# ---------------------------------------------------------------------------


def test_raw_review_artifacts_are_gitignored() -> None:
    ignored = [
        "reviews/state.json",
        "reviews/2026-07-22_1/events.jsonl",
        "reviews/2026-07-22_1/timeline.md",
        "reviews/.building_2026-07-22_1/manifest.json",
    ]
    committable = [
        "reviews/2026-07-22_1/manifest.json",
        "reviews/2026-07-22_1/stats.json",
        "reviews/2026-07-22_1/signals.json",
        "reviews/2026-07-22_1/findings.json",
        "reviews/2026-07-22_1/report.md",
    ]
    for path in ignored:
        proc = subprocess.run(
            ["git", "check-ignore", "-q", path], cwd=REPO_ROOT, capture_output=True,
        )
        assert proc.returncode == 0, f"{path} must be gitignored"
    for path in committable:
        proc = subprocess.run(
            ["git", "check-ignore", "-q", path], cwd=REPO_ROOT, capture_output=True,
        )
        assert proc.returncode != 0, f"{path} must be committable"


# ---------------------------------------------------------------------------
# Skills reference only real commands (the operator flow cannot rot silently)
# ---------------------------------------------------------------------------


def _skill_text(name: str) -> str:
    return (REPO_ROOT / ".claude" / "skills" / name / "SKILL.md").read_text(encoding="utf-8")


def test_skills_reference_only_existing_scripts_and_subcommands() -> None:
    from scripts.review_findings import build_parser as findings_parser
    from scripts.review_session import build_parser as session_parser

    session_cmds = set(session_parser()._subparsers._group_actions[0].choices)
    findings_cmds = set(findings_parser()._subparsers._group_actions[0].choices)

    for skill in ("review-session", "implement-review"):
        text = _skill_text(skill)
        for script in re.findall(r"scripts/([\w_]+\.py)", text):
            assert (REPO_ROOT / "scripts" / script).exists(), f"{skill}: {script}"
        for command in re.findall(r"review_session\.py\s+([a-z-]+)", text):
            assert command in session_cmds, f"{skill}: review_session.py {command}"
        for command in re.findall(r"review_findings\.py\s+([a-z-]+)", text):
            assert command in findings_cmds, f"{skill}: review_findings.py {command}"
    assert "Engineering Review Required." in _skill_text("review-session")
    # 20 — the implementation flow pushes and never merges (procedural rule
    # is stated, in exactly those words).
    assert "Never merge" in _skill_text("implement-review")
    protocol = (REPO_ROOT / "docs" / "REVIEW_PROTOCOL.md").read_text(encoding="utf-8")
    assert "Engineering Review Required." in protocol


# ---------------------------------------------------------------------------
# 18 — reproduction vocabulary stays honest (no fake "session replay")
# ---------------------------------------------------------------------------


def test_no_false_full_session_replay_claims() -> None:
    assert set(REPRODUCTION_STATUSES) == {
        "evidence_inspected", "deterministically_replayed",
        "synthetically_reproduced", "manually_reproduced",
        "not_reproducible", "reproduction_not_required",
    }
    protocol = (REPO_ROOT / "docs" / "REVIEW_PROTOCOL.md").read_text(encoding="utf-8")
    assert "ONE turn" in protocol  # the harness's real capability, stated
    assert "this is NOT replay" in protocol


# ---------------------------------------------------------------------------
# 1, 5, 8, 17, 19 — the full pipeline over REAL ingress evidence
# ---------------------------------------------------------------------------


async def _crashing_handler(update, context):  # noqa: ANN001
    raise ValueError("crash for acceptance")


async def test_end_to_end_crash_review_reproduction_and_rebuild(
    db: Database, tmp_path: Path, capsys
) -> None:
    # 8 — a real crash through the real ingress envelope: captured + re-raised.
    exc, trace = await run_failing_user_turn(db, USER_ID, _crashing_handler, text="שלום")
    assert isinstance(exc, ValueError)
    crash_events = [e for e in trace.events if e.event == ERROR_CAPTURED]
    assert len(crash_events) == 1
    crash_id = crash_events[0].id

    # Widen the window artificially so the selection is multi-day (item 1).
    await db.execute(
        "UPDATE product_events SET created_at=? WHERE id=?",
        ("2026-07-15T09:00:00+00:00", trace.events[0].id),
    )

    def run_cli(argv: list[str]) -> int:
        return session_main(argv)

    reviews = str(tmp_path / "reviews")
    db_path = str(tmp_path / "acc.db")
    assert await asyncio.to_thread(run_cli, [
        "build", "--db", db_path, "--user", str(USER_ID),
        "--start", "2026-07-14T00:00:00", "--reviews-dir", reviews,
        "--review-id", "acc_1",
    ]) == 0
    capsys.readouterr()

    package_dir = Path(reviews) / "acc_1"
    signals = json.loads((package_dir / "signals.json").read_text(encoding="utf-8"))
    detector_ids = {signal["detector_id"] for signal in signals["signals"]}
    assert "error_captured" in detector_ids  # the crash surfaced mechanically

    # A finding citing the crash, reproduced through the harness (17).
    reproduced_exc, reproduced_trace = await run_failing_user_turn(
        db, USER_ID, _crashing_handler, text="שלום"
    )
    assert isinstance(reproduced_exc, ValueError)
    assert any(e.event == ERROR_CAPTURED for e in reproduced_trace.events)

    document = empty_findings_document("acc_1")
    document["findings"] = [{
        "finding_id": "F-ACC-1",
        "title": "Text handler crashes on greeting",
        "kind": "defect",
        "status": "proposed",
        "evidence_level": "CONFIRMED",
        "severity": "CRITICAL",
        "confidence": "high",
        "frequency": 1,
        "scope": "isolated",
        "user_impact": "The user got no reply at all.",
        "deterministic_facts": [f"error.captured event {crash_id}, boundary telegram_handler:text"],
        "explicit_assumptions": [],
        "missing_information": [],
        "evidence_references": [{"event_ids": [crash_id], "interaction_id": trace.interaction_id}],
        "detector_references": ["error_captured"],
        "reproduction_status": "synthetically_reproduced",
        "suspected_root_cause": "unguarded parse in the text handler",
        "affected_components": ["noam_coach/bot/assistant.py"],
        "proposed_regression": "run_failing_user_turn journey asserting error.captured",
        "proposed_implementation_scope": "small",
        "approval_record": None,
        "implementation_eligibility": "ineligible",
        "final_resolution": None,
    }, {
        "finding_id": "F-ACC-2",
        "title": "Old wording issue that no longer exists",
        "kind": "ux_problem",
        "status": "proposed",
        "evidence_level": "PROBABLE",
        "severity": "LOW",
        "confidence": "low",
        "frequency": 1,
        "scope": "isolated",
        "user_impact": "Minor confusion.",
        "deterministic_facts": [],
        "explicit_assumptions": ["wording is the cause"],
        "missing_information": [],
        "evidence_references": [{"event_ids": [crash_id]}],
        "detector_references": [],
        "reproduction_status": "evidence_inspected",
        "suspected_root_cause": None,
        "affected_components": [],
        "proposed_regression": "",
        "proposed_implementation_scope": "small",
        "approval_record": None,
        "implementation_eligibility": "ineligible",
        "final_resolution": None,
    }]
    (package_dir / "findings.json").write_text(
        json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    def run_findings(argv: list[str]) -> int:
        return findings_main(argv)

    assert await asyncio.to_thread(run_findings, [
        "validate", "acc_1", "--reviews-dir", reviews]) == 0
    assert await asyncio.to_thread(run_findings, [
        "set-status", "acc_1", "F-ACC-1", "approved",
        "--approver", "noam", "--scope", "fix crash", "--reviews-dir", reviews]) == 0
    # 19 — a stale finding is excluded from implementation input entirely.
    assert await asyncio.to_thread(run_findings, [
        "set-status", "acc_1", "F-ACC-2", "stale", "--reviews-dir", reviews]) == 0
    approved = approved_findings(package_dir / "findings.json")
    assert [f["finding_id"] for f in approved] == ["F-ACC-1"]

    assert await asyncio.to_thread(run_findings, [
        "render-report", "acc_1", "--reviews-dir", reviews]) == 0
    assert await asyncio.to_thread(run_cli, [
        "advance", "--review", "acc_1", "--reviews-dir", reviews]) == 0
    capsys.readouterr()

    # 5 — the old review rebuilds from its recorded selection, immutably linked.
    assert await asyncio.to_thread(run_cli, [
        "build", "--db", db_path, "--user", str(USER_ID),
        "--rebuild", "acc_1", "--reviews-dir", reviews]) == 0
    out = capsys.readouterr().out
    assert "acc_1_rb1" in out
    rebuilt = json.loads(
        (Path(reviews) / "acc_1_rb1" / "manifest.json").read_text(encoding="utf-8")
    )
    assert rebuilt["rebuilt_from"] == "acc_1"
    original = json.loads((package_dir / "manifest.json").read_text(encoding="utf-8"))
    assert rebuilt["first_event_id"] == original["first_event_id"]
    assert rebuilt["last_event_id"] == original["last_event_id"]


# ---------------------------------------------------------------------------
# 12 — building a review changes nothing in the product store
# ---------------------------------------------------------------------------


async def test_building_changes_no_product_rows(db: Database, tmp_path: Path, capsys) -> None:
    await event_log.append_event(db, USER_ID, "state.mutated", trace_id="tr_1",
                                 interaction_id="in_1", properties={"n": 1})
    before = await db.fetch_all("SELECT COUNT(*) AS c FROM product_events", ())
    reviews = str(tmp_path / "rv")

    def run_cli() -> int:
        return session_main([
            "build", "--db", str(tmp_path / "acc.db"), "--user", str(USER_ID),
            "--after-id", "0", "--reviews-dir", reviews, "--review-id", "ro",
        ])

    assert await asyncio.to_thread(run_cli) == 0
    capsys.readouterr()
    after = await db.fetch_all("SELECT COUNT(*) AS c FROM product_events", ())
    assert after[0]["c"] == before[0]["c"]
