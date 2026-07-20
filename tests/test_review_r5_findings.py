"""Review batch R5 — strict findings model, approval gating, derived report,
and the fully-gated cursor advance."""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any

import pytest

import event_log
from db import Database
from scripts.review_findings import (
    approved_findings,
    compute_eligibility,
    empty_findings_document,
    validate_findings_data,
)
from scripts.review_findings import main as findings_main
from scripts.review_session import main as session_main

USER_ID = 1


def _finding(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "finding_id": "F-1",
        "title": "Menu fallback fired on every generation",
        "kind": "defect",
        "status": "proposed",
        "evidence_level": "CONFIRMED",
        "severity": "HIGH",
        "confidence": "high",
        "frequency": 3,
        "scope": "repeated",
        "user_impact": "The user silently received degraded menus.",
        "deterministic_facts": ["decision.fallback_selected appeared 3 times (events 5,9,13)"],
        "explicit_assumptions": [],
        "missing_information": [],
        "evidence_references": [{"event_ids": [5, 9, 13], "interaction_id": "in_0"}],
        "detector_references": ["decision_fallback"],
        "reproduction_status": "evidence_inspected",
        "suspected_root_cause": "menu repair rejects every AI candidate",
        "affected_components": ["recommendations.py"],
        "proposed_regression": "harness journey asserting decision.finalized source=ai",
        "proposed_implementation_scope": "medium",
        "approval_record": None,
        "implementation_eligibility": "ineligible",
        "final_resolution": None,
    }
    base.update(overrides)
    return base


def _document(*findings: dict[str, Any]) -> dict[str, Any]:
    document = empty_findings_document("2026-07-18_1")
    document["findings"] = list(findings)
    return document


_APPROVAL = {
    "approver": "noam",
    "timestamp": "2026-07-18T10:00:00+00:00",
    "approved_scope": "fix menu repair root cause",
}


def test_valid_document_passes() -> None:
    assert validate_findings_data(_document(_finding())) == []


def test_unknown_fields_rejected_everywhere() -> None:
    document = _document(_finding())
    document["extra"] = 1
    assert any("unknown top-level" in problem for problem in validate_findings_data(document))

    document = _document(_finding(surprise="x"))
    assert any("unknown fields" in problem for problem in validate_findings_data(document))

    approved = _finding(status="approved", approval_record={**_APPROVAL, "mood": "great"},
                        implementation_eligibility="eligible")
    assert any("approval_record unknown fields" in problem
               for problem in validate_findings_data(_document(approved)))


def test_invalid_enums_and_missing_fields_rejected() -> None:
    assert any("severity" in problem
               for problem in validate_findings_data(_document(_finding(severity="BAD"))))
    assert any("kind" in problem
               for problem in validate_findings_data(_document(_finding(kind="bug"))))
    incomplete = _finding()
    del incomplete["user_impact"]
    assert any("missing fields" in problem
               for problem in validate_findings_data(_document(incomplete)))


def test_confirmed_requires_evidence_and_facts() -> None:
    no_refs = _finding(evidence_references=[])
    assert any("requires at least one evidence reference" in problem
               for problem in validate_findings_data(_document(no_refs)))
    no_facts = _finding(deterministic_facts=[])
    assert any("deterministic_facts" in problem
               for problem in validate_findings_data(_document(no_facts)))


def test_approval_without_record_rejected() -> None:
    approved = _finding(status="approved", implementation_eligibility="eligible")
    problems = validate_findings_data(_document(approved))
    assert any("requires an approval_record" in problem for problem in problems)


def test_approved_confirmed_with_record_is_eligible() -> None:
    approved = _finding(status="approved", approval_record=dict(_APPROVAL),
                        implementation_eligibility="eligible")
    assert validate_findings_data(_document(approved)) == []


def test_speculative_defect_can_never_be_approved() -> None:
    finding = _finding(
        status="approved",
        evidence_level="SPECULATIVE",
        evidence_references=[],
        deterministic_facts=["n/a"],
        missing_information=["no evidence in window"],
        approval_record=dict(_APPROVAL),
        implementation_eligibility="ineligible",
    )
    problems = validate_findings_data(_document(finding))
    assert any("SPECULATIVE defect can never be approved" in problem for problem in problems)


def test_speculative_improvement_is_experiment_only() -> None:
    finding = _finding(
        finding_id="F-exp",
        kind="improvement_opportunity",
        status="approved",
        evidence_level="SPECULATIVE",
        severity="LOW",
        evidence_references=[],
        deterministic_facts=["observation only"],
        missing_information=["needs A/B of morning wording"],
        approval_record=dict(_APPROVAL),
        implementation_eligibility="eligible_as_experiment",
    )
    assert validate_findings_data(_document(finding)) == []
    assert compute_eligibility(finding) == "eligible_as_experiment"


def test_probable_defect_requires_reproduction_eligibility() -> None:
    finding = _finding(status="approved", evidence_level="PROBABLE",
                       approval_record=dict(_APPROVAL),
                       implementation_eligibility="eligible_with_reproduction")
    assert validate_findings_data(_document(finding)) == []
    # Hand-editing eligibility to skip reproduction is rejected.
    bypassed = copy.deepcopy(finding)
    bypassed["implementation_eligibility"] = "eligible"
    assert any("cannot be hand-edited" in problem
               for problem in validate_findings_data(_document(bypassed)))


def test_systemic_confirmed_needs_confirmed_shared_cause() -> None:
    weak = _finding(scope="systemic_confirmed", evidence_level="PROBABLE")
    assert any("systemic_confirmed requires CONFIRMED" in problem
               for problem in validate_findings_data(_document(weak)))
    rootless = _finding(scope="systemic_confirmed", suspected_root_cause=None)
    assert any("stated root cause" in problem
               for problem in validate_findings_data(_document(rootless)))
    lone = _finding(scope="repeated", frequency=1)
    assert any("frequency >= 2" in problem
               for problem in validate_findings_data(_document(lone)))


def test_implemented_requires_final_resolution() -> None:
    finding = _finding(status="implemented", approval_record=dict(_APPROVAL),
                       implementation_eligibility="eligible")
    assert any("final_resolution" in problem
               for problem in validate_findings_data(_document(finding)))


def test_duplicate_ids_and_secret_content_rejected() -> None:
    duplicates = _document(_finding(), _finding())
    assert any("duplicate finding_id" in problem
               for problem in validate_findings_data(duplicates))
    leaky = _document(_finding(user_impact="uses key sk-abcdef1234567890abcd"))
    assert any("redactor" in problem for problem in validate_findings_data(leaky))


# ---------------------------------------------------------------------------
# End-to-end over a real package (build → init → approve → report → advance)
# ---------------------------------------------------------------------------


@pytest.fixture
def package(tmp_path: Path) -> dict[str, Any]:
    async def _prepare() -> None:
        database = Database(str(tmp_path / "r5.db"))
        await database.init()
        await database.execute(
            "INSERT INTO users(id, first_name, username, updated_at) VALUES(?, 'Test', NULL, ?)",
            (USER_ID, "2026-07-01T00:00:00+00:00"),
        )
        await event_log.append_event(
            database, USER_ID, "interaction.received",
            trace_id="tr_0", interaction_id="in_0",
            properties={"kind": "text", "content": {"text": "בדיקה"}},
        )
        await event_log.append_event(
            database, USER_ID, "error.captured",
            trace_id="tr_0", interaction_id="in_0", status="failed",
            outcome="unhandled_exception",
            properties={"boundary": "telegram_handler:text", "error_type": "ValueError"},
        )

    asyncio.run(_prepare())
    reviews = str(tmp_path / "reviews")
    assert session_main([
        "build", "--db", str(tmp_path / "r5.db"), "--user", str(USER_ID),
        "--after-id", "0", "--reviews-dir", reviews, "--review-id", "rv1",
    ]) == 0
    return {"reviews": reviews, "review_id": "rv1", "dir": Path(reviews) / "rv1"}


def _write_findings(package: dict[str, Any], *findings: dict[str, Any]) -> None:
    document = empty_findings_document(package["review_id"])
    document["findings"] = list(findings)
    (package["dir"] / "findings.json").write_text(
        json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def test_full_review_completion_flow(package: dict[str, Any], capsys) -> None:
    reviews, review_id = package["reviews"], package["review_id"]

    # Advance refuses before any findings exist.
    assert session_main(["advance", "--review", review_id, "--reviews-dir", reviews]) == 1
    assert "findings.json missing" in capsys.readouterr().out

    finding = _finding(evidence_references=[{"event_ids": [2], "interaction_id": "in_0"}],
                       detector_references=["error_captured"])
    _write_findings(package, finding)
    assert findings_main(["validate", review_id, "--reviews-dir", reviews]) == 0
    capsys.readouterr()

    # Advance still refuses: no report yet.
    assert session_main(["advance", "--review", review_id, "--reviews-dir", reviews]) == 1
    assert "report.md missing" in capsys.readouterr().out

    assert findings_main(["render-report", review_id, "--reviews-dir", reviews]) == 0
    report = (package["dir"] / "report.md").read_text(encoding="utf-8")
    assert "Engineering Review Required." in report
    assert "F-1" in report and "show-finding" in report
    capsys.readouterr()

    # Now the review is complete — advance succeeds and records the window end.
    assert session_main(["advance", "--review", review_id, "--reviews-dir", reviews]) == 0
    state = json.loads((Path(reviews) / "state.json").read_text(encoding="utf-8"))
    assert state["review_id"] == review_id
    assert state["last_reviewed_event_id"] == 2


def test_advance_refuses_invalid_findings_and_tampering(
    package: dict[str, Any], capsys
) -> None:
    reviews, review_id = package["reviews"], package["review_id"]
    _write_findings(package, _finding(severity="WAT"))
    (package["dir"] / "report.md").write_text("stub", encoding="utf-8")
    assert session_main(["advance", "--review", review_id, "--reviews-dir", reviews]) == 1
    assert "severity" in capsys.readouterr().out
    assert not (Path(reviews) / "state.json").exists()

    # Fix findings but tamper with hashed evidence → still refused.
    _write_findings(package, _finding(
        evidence_references=[{"event_ids": [2]}], detector_references=["error_captured"],
    ))
    (package["dir"] / "events.jsonl").write_text("{}\n", encoding="utf-8")
    assert session_main(["advance", "--review", review_id, "--reviews-dir", reviews]) == 1
    assert "rewritten" in capsys.readouterr().out
    assert not (Path(reviews) / "state.json").exists()


def test_set_status_approval_flow_and_gating(package: dict[str, Any], capsys) -> None:
    reviews, review_id = package["reviews"], package["review_id"]
    _write_findings(package, _finding(
        evidence_references=[{"event_ids": [2]}], detector_references=["error_captured"],
    ))

    # Approving without approver/scope is refused.
    with pytest.raises(SystemExit, match="approver"):
        findings_main(["set-status", review_id, "F-1", "approved", "--reviews-dir", reviews])

    assert findings_main([
        "set-status", review_id, "F-1", "approved",
        "--approver", "noam", "--scope", "root-cause fix + regression",
        "--reviews-dir", reviews,
    ]) == 0
    capsys.readouterr()

    approved = approved_findings(package["dir"] / "findings.json")
    assert len(approved) == 1
    assert approved[0]["approval_record"]["approver"] == "noam"
    assert approved[0]["implementation_eligibility"] == "eligible"

    # An unapproved finding never comes back from the approved gate.
    assert findings_main(["set-status", review_id, "F-1", "rejected",
                          "--reviews-dir", reviews]) == 0
    assert approved_findings(package["dir"] / "findings.json") == []


def test_malformed_file_cannot_yield_approved_findings(package: dict[str, Any]) -> None:
    # A hand-edited "approved" finding with no approval record: the approved
    # gate validates the WHOLE file first and refuses everything.
    _write_findings(package, _finding(status="approved",
                                      implementation_eligibility="eligible"))
    with pytest.raises(ValueError, match="approval_record"):
        approved_findings(package["dir"] / "findings.json")


def test_show_finding_prints_cited_evidence(package: dict[str, Any], capsys) -> None:
    reviews, review_id = package["reviews"], package["review_id"]
    _write_findings(package, _finding(
        evidence_references=[{"event_ids": [2], "note": "the crash"}],
        detector_references=["error_captured"],
    ))
    assert session_main([
        "show-finding", review_id, "F-1", "--reviews-dir", reviews,
    ]) == 0
    out = capsys.readouterr().out
    assert '"finding_id": "F-1"' in out
    assert "referenced evidence events" in out
    assert '"error.captured"' in out  # the cited event row itself


def test_init_creates_versioned_shell(package: dict[str, Any], capsys) -> None:
    reviews, review_id = package["reviews"], package["review_id"]
    assert findings_main(["init", review_id, "--focus", "reliability",
                          "--reviews-dir", reviews]) == 0
    document = json.loads((package["dir"] / "findings.json").read_text(encoding="utf-8"))
    assert document["focus"] == "reliability"
    assert document["findings"] == []
    assert document["findings_schema_version"]
    assert validate_findings_data(document) == []
