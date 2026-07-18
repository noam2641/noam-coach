"""Canonical findings model, strict validator and report renderer (batch R5).

``findings.json`` inside a review package is the ONE machine-readable
findings representation — ``report.md`` is derived from it, never edited
independently. The validator is the approval gate's enforcement point:
malformed findings, invalid enum values, impossible status/evidence
combinations, approvals without an approval record, and manually edited
implementation eligibility are all rejected — hand-editing the file
cannot bypass the rules, because every consumer (cursor advance, the
implementation flow) validates first.

Evidence / severity / frequency / scope stay separate concepts:
evidence_level says how strongly production facts prove the issue;
severity says how harmful an occurrence is; frequency counts observed
occurrences in the selected evidence; scope says whether a shared cause
is proven (``systemic_confirmed`` requires CONFIRMED evidence and a
stated root cause — repetition alone never confirms it).

Usage:
    python scripts/review_findings.py init REVIEW_ID [--focus FOCUS]
    python scripts/review_findings.py validate REVIEW_ID
    python scripts/review_findings.py approved REVIEW_ID
    python scripts/review_findings.py set-status REVIEW_ID FINDING_ID STATUS
        [--approver NAME --scope TEXT --notes TEXT]
    python scripts/review_findings.py render-report REVIEW_ID
    common: [--reviews-dir DIR]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

FINDINGS_NAME = "findings.json"
REPORT_NAME = "report.md"

KINDS = (
    "defect",
    "ux_problem",
    "conversation_quality",
    "ai_interpretation",
    "state_consistency",
    "routing",
    "validation_gap",
    "observability_gap",
    "regression_gap",
    "technical_debt",
    "improvement_opportunity",
)
EVIDENCE_LEVELS = ("CONFIRMED", "PROBABLE", "SPECULATIVE")
SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW")
STATUSES = (
    "proposed",
    "needs_investigation",
    "approved",
    "rejected",
    "deferred",
    "implementing",
    "implemented",
    "verification_failed",
    "stale",
    "already_resolved",
    "blocked",
)
SCOPES = ("isolated", "repeated", "systemic_candidate", "systemic_confirmed")
CONFIDENCES = ("high", "medium", "low")
REPRODUCTION_STATUSES = (
    "evidence_inspected",
    "deterministically_replayed",
    "synthetically_reproduced",
    "manually_reproduced",
    "not_reproducible",
    "reproduction_not_required",
)
IMPLEMENTATION_SCOPES = ("small", "medium", "large")
ELIGIBILITIES = (
    "eligible",
    "eligible_with_reproduction",
    "eligible_as_experiment",
    "ineligible",
)
FOCUSES = ("full", "nutrition", "workout", "conversation", "ux", "architecture", "reliability")

# Statuses that mean "implementation may look at this finding at all".
_POST_APPROVAL_STATUSES = ("approved", "implementing", "implemented", "verification_failed")
# Reproduction states that count as an actual reproduction.
REPRODUCED_STATES = (
    "deterministically_replayed",
    "synthetically_reproduced",
    "manually_reproduced",
)

_FILE_FIELDS = {
    "findings_schema_version",
    "review_id",
    "review_protocol_version",
    "focus",
    "findings",
}
_FINDING_FIELDS = {
    "finding_id",
    "title",
    "kind",
    "status",
    "evidence_level",
    "severity",
    "confidence",
    "frequency",
    "scope",
    "user_impact",
    "deterministic_facts",
    "explicit_assumptions",
    "missing_information",
    "evidence_references",
    "detector_references",
    "reproduction_status",
    "suspected_root_cause",
    "affected_components",
    "proposed_regression",
    "proposed_implementation_scope",
    "approval_record",
    "implementation_eligibility",
    "final_resolution",
}
_APPROVAL_FIELDS = {"approver", "timestamp", "approved_scope", "notes"}


def compute_eligibility(finding: dict[str, Any]) -> str:
    """The ONLY way implementation eligibility is determined.

    - CONFIRMED + approved → eligible.
    - PROBABLE defect → eligible_with_reproduction (must be reproduced
      before it may be treated as a factual defect).
    - PROBABLE non-defect → eligible (uncertainty preserved).
    - SPECULATIVE improvement_opportunity → eligible_as_experiment.
    - Everything else (unapproved, or SPECULATIVE non-improvement) →
      ineligible. Validation separately REJECTS a SPECULATIVE defect that
      someone tries to approve.
    """
    if finding.get("status") not in _POST_APPROVAL_STATUSES:
        return "ineligible"
    level = finding.get("evidence_level")
    kind = finding.get("kind")
    if level == "CONFIRMED":
        return "eligible"
    if level == "PROBABLE":
        return "eligible_with_reproduction" if kind == "defect" else "eligible"
    if level == "SPECULATIVE" and kind == "improvement_opportunity":
        return "eligible_as_experiment"
    return "ineligible"


def _validate_finding(finding: Any, index: int, seen_ids: set[str]) -> list[str]:
    where = f"findings[{index}]"
    if not isinstance(finding, dict):
        return [f"{where}: finding must be an object"]
    problems: list[str] = []
    prefix = f"{where} ({finding.get('finding_id', '?')})"

    unknown = set(finding) - _FINDING_FIELDS
    if unknown:
        problems.append(f"{prefix}: unknown fields {sorted(unknown)}")
    missing = _FINDING_FIELDS - set(finding)
    if missing:
        problems.append(f"{prefix}: missing fields {sorted(missing)}")
        return problems

    finding_id = finding["finding_id"]
    if not isinstance(finding_id, str) or not finding_id.strip():
        problems.append(f"{prefix}: finding_id must be a non-empty string")
    elif finding_id in seen_ids:
        problems.append(f"{prefix}: duplicate finding_id {finding_id!r}")
    else:
        seen_ids.add(finding_id)

    for name, allowed in (
        ("kind", KINDS),
        ("status", STATUSES),
        ("evidence_level", EVIDENCE_LEVELS),
        ("severity", SEVERITIES),
        ("confidence", CONFIDENCES),
        ("scope", SCOPES),
        ("reproduction_status", REPRODUCTION_STATUSES),
        ("proposed_implementation_scope", IMPLEMENTATION_SCOPES),
        ("implementation_eligibility", ELIGIBILITIES),
    ):
        if finding[name] not in allowed:
            problems.append(f"{prefix}: {name}={finding[name]!r} not in {allowed}")

    if not isinstance(finding["title"], str) or not finding["title"].strip():
        problems.append(f"{prefix}: title must be non-empty")
    if not isinstance(finding["user_impact"], str) or not finding["user_impact"].strip():
        problems.append(f"{prefix}: user_impact must be non-empty")
    if not isinstance(finding["frequency"], int) or finding["frequency"] < 1:
        problems.append(f"{prefix}: frequency must be an integer >= 1")

    for name in ("deterministic_facts", "explicit_assumptions", "missing_information",
                 "detector_references", "affected_components"):
        value = finding[name]
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            problems.append(f"{prefix}: {name} must be a list of strings")

    references = finding["evidence_references"]
    if not isinstance(references, list):
        problems.append(f"{prefix}: evidence_references must be a list")
        references = []
    for ref_index, reference in enumerate(references):
        if not isinstance(reference, dict) or not isinstance(reference.get("event_ids"), list) \
                or not reference["event_ids"] \
                or any(not isinstance(item, int) for item in reference["event_ids"]):
            problems.append(
                f"{prefix}: evidence_references[{ref_index}] needs a non-empty "
                "integer event_ids list"
            )

    level = finding["evidence_level"]
    if level in ("CONFIRMED", "PROBABLE") and not references:
        problems.append(
            f"{prefix}: {level} requires at least one evidence reference"
        )
    if level == "SPECULATIVE" and not references and not finding["missing_information"]:
        problems.append(
            f"{prefix}: SPECULATIVE with no evidence must state missing_information"
        )

    if not isinstance(finding["deterministic_facts"], list) or (
        level == "CONFIRMED" and not finding["deterministic_facts"]
    ):
        problems.append(f"{prefix}: CONFIRMED requires non-empty deterministic_facts")

    status = finding["status"]
    approval = finding["approval_record"]
    if status in _POST_APPROVAL_STATUSES:
        if not isinstance(approval, dict):
            problems.append(f"{prefix}: status {status!r} requires an approval_record")
        else:
            unknown_approval = set(approval) - _APPROVAL_FIELDS
            if unknown_approval:
                problems.append(f"{prefix}: approval_record unknown fields {sorted(unknown_approval)}")
            for required in ("approver", "approved_scope"):
                if not isinstance(approval.get(required), str) or not approval[required].strip():
                    problems.append(f"{prefix}: approval_record.{required} must be non-empty")
            timestamp = approval.get("timestamp")
            try:
                datetime.fromisoformat(str(timestamp))
            except (TypeError, ValueError):
                problems.append(f"{prefix}: approval_record.timestamp is not ISO-8601")
    elif approval is not None and not isinstance(approval, dict):
        problems.append(f"{prefix}: approval_record must be an object or null")

    if level == "SPECULATIVE" and finding["kind"] == "defect" \
            and status in _POST_APPROVAL_STATUSES:
        problems.append(
            f"{prefix}: a SPECULATIVE defect can never be approved for implementation "
            "— reproduce it (→ CONFIRMED/PROBABLE) or reclassify as "
            "improvement_opportunity for an explicit experiment"
        )

    if finding["scope"] == "systemic_confirmed":
        if level != "CONFIRMED":
            problems.append(
                f"{prefix}: systemic_confirmed requires CONFIRMED evidence of the shared cause"
            )
        root = finding["suspected_root_cause"]
        if not isinstance(root, str) or not root.strip():
            problems.append(f"{prefix}: systemic_confirmed requires a stated root cause")
    if finding["scope"] in ("repeated", "systemic_candidate", "systemic_confirmed") \
            and isinstance(finding["frequency"], int) and finding["frequency"] < 2:
        problems.append(f"{prefix}: scope {finding['scope']!r} requires frequency >= 2")

    if status == "implemented":
        resolution = finding["final_resolution"]
        if not isinstance(resolution, str) or not resolution.strip():
            problems.append(f"{prefix}: implemented requires a final_resolution")

    expected = compute_eligibility(finding)
    if finding["implementation_eligibility"] != expected:
        problems.append(
            f"{prefix}: implementation_eligibility={finding['implementation_eligibility']!r} "
            f"does not match the computed value {expected!r} — eligibility cannot be "
            "hand-edited"
        )
    return problems


def validate_findings_data(data: Any) -> list[str]:
    from noam_coach.observability.redaction import redact
    from noam_coach.observability.review_versions import FINDINGS_SCHEMA_VERSION

    if not isinstance(data, dict):
        return ["findings file root must be an object"]
    problems: list[str] = []
    unknown = set(data) - _FILE_FIELDS
    if unknown:
        problems.append(f"unknown top-level fields {sorted(unknown)}")
    missing = _FILE_FIELDS - set(data)
    if missing:
        problems.append(f"missing top-level fields {sorted(missing)}")
        return problems
    if data["findings_schema_version"] != FINDINGS_SCHEMA_VERSION:
        problems.append(
            f"findings_schema_version {data['findings_schema_version']!r} != "
            f"current {FINDINGS_SCHEMA_VERSION!r}"
        )
    if data["focus"] not in FOCUSES:
        problems.append(f"focus={data['focus']!r} not in {FOCUSES}")
    findings = data["findings"]
    if not isinstance(findings, list):
        return problems + ["findings must be a list"]
    seen: set[str] = set()
    for index, finding in enumerate(findings):
        problems.extend(_validate_finding(finding, index, seen))
    # Committable-artifact policy: findings.json may be committed, so it must
    # be a fixed point of the canonical redactor (fail closed on secrets).
    if redact(data) != data:
        problems.append(
            "findings contain content the canonical redactor would alter — "
            "remove secret-shaped material before completing the review"
        )
    return problems


def load_findings_file(path: Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def validate_findings_file(path: Path) -> list[str]:
    try:
        data = load_findings_file(path)
    except (OSError, ValueError) as exc:
        return [f"cannot read findings file: {exc}"]
    return validate_findings_data(data)


def load_findings(package_dir: Path) -> list[dict[str, Any]]:
    path = Path(package_dir) / FINDINGS_NAME
    if not path.exists():
        return []
    return list(load_findings_file(path).get("findings") or [])


def approved_findings(path: Path) -> list[dict[str, Any]]:
    """Approved findings ONLY, from a findings file that must validate.

    The implementation flow's single entry point: invalid files yield no
    findings at all (raises), so a malformed approval cannot slip through.
    """
    problems = validate_findings_file(path)
    if problems:
        raise ValueError("findings file is invalid: " + "; ".join(problems))
    data = load_findings_file(path)
    return [f for f in data["findings"] if f["status"] == "approved"]


def empty_findings_document(review_id: str, *, focus: str = "full") -> dict[str, Any]:
    from noam_coach.observability.review_versions import (
        FINDINGS_SCHEMA_VERSION,
        REVIEW_PROTOCOL_VERSION,
    )

    return {
        "findings_schema_version": FINDINGS_SCHEMA_VERSION,
        "review_id": review_id,
        "review_protocol_version": REVIEW_PROTOCOL_VERSION,
        "focus": focus,
        "findings": [],
    }


# ---------------------------------------------------------------------------
# Derived report
# ---------------------------------------------------------------------------

_SEVERITY_ORDER = {name: index for index, name in enumerate(SEVERITIES)}


def render_report(package_dir: Path) -> str:
    """Deterministic report.md derived from findings.json + manifest/stats."""
    package_dir = Path(package_dir)
    data = load_findings_file(package_dir / FINDINGS_NAME)
    manifest = json.loads((package_dir / "manifest.json").read_text(encoding="utf-8"))
    stats = json.loads((package_dir / "stats.json").read_text(encoding="utf-8"))

    lines: list[str] = []
    lines.append(f"# Engineering Review Report — {data['review_id']}")
    lines.append("")
    lines.append(f"- Review protocol: v{data['review_protocol_version']} · focus: {data['focus']}")
    lines.append(f"- Source commit: `{manifest.get('source_commit')}`"
                 + (" (dirty tree)" if manifest.get("working_tree_dirty") else ""))
    lines.append(
        f"- Evidence: events {manifest.get('first_event_id')}–{manifest.get('last_event_id')}"
        f" ({manifest.get('event_count')} events, {stats.get('interactions_total')} interactions,"
        f" {stats.get('first_event_at')} → {stats.get('last_event_at')})"
    )
    lines.append(f"- Detector set: v{manifest.get('detector_set_version')}"
                 f" · package schema: v{manifest.get('package_schema_version')}")
    lines.append("")

    findings = sorted(
        data["findings"],
        key=lambda f: (_SEVERITY_ORDER.get(f["severity"], 9), f["finding_id"]),
    )
    if not findings:
        lines.append("No findings were produced for this window.")
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding["severity"]] = counts.get(finding["severity"], 0) + 1
    if findings:
        summary = " · ".join(f"{name}: {counts[name]}" for name in SEVERITIES if name in counts)
        lines.append(f"**{len(findings)} findings** ({summary})")
        lines.append("")

    for finding in findings:
        lines.append(f"## {finding['finding_id']} — {finding['title']}")
        lines.append("")
        lines.append(
            f"`{finding['severity']}` · {finding['kind']} · evidence {finding['evidence_level']}"
            f" · confidence {finding['confidence']} · frequency {finding['frequency']}"
            f" · scope {finding['scope']} · status {finding['status']}"
        )
        lines.append("")
        lines.append(f"**User impact:** {finding['user_impact']}")
        if finding["deterministic_facts"]:
            lines.append("")
            lines.append("**Observed facts:**")
            lines.extend(f"- {fact}" for fact in finding["deterministic_facts"])
        if finding["explicit_assumptions"]:
            lines.append("")
            lines.append("**Assumptions (not facts):**")
            lines.extend(f"- {item}" for item in finding["explicit_assumptions"])
        if finding["missing_information"]:
            lines.append("")
            lines.append("**Missing information:**")
            lines.extend(f"- {item}" for item in finding["missing_information"])
        lines.append("")
        for reference in finding["evidence_references"]:
            ref = f"events {reference.get('event_ids')}"
            if reference.get("interaction_id"):
                ref += f" · interaction {reference['interaction_id']}"
            if reference.get("note"):
                ref += f" — {reference['note']}"
            lines.append(f"- Evidence: {ref}")
        if finding["detector_references"]:
            lines.append(f"- Detectors: {', '.join(finding['detector_references'])}")
        lines.append(f"- Reproduction: {finding['reproduction_status']}")
        if finding["suspected_root_cause"]:
            lines.append(f"- Root-cause hypothesis: {finding['suspected_root_cause']}")
        if finding["affected_components"]:
            lines.append(f"- Affected components: {', '.join(finding['affected_components'])}")
        if finding["proposed_regression"]:
            lines.append(f"- Proposed regression test: {finding['proposed_regression']}")
        lines.append(
            f"- Implementation: scope {finding['proposed_implementation_scope']}"
            f" · eligibility {finding['implementation_eligibility']}"
        )
        lines.append("")
        lines.append(
            f"Inspect: `python scripts/review_session.py show-finding "
            f"{data['review_id']} {finding['finding_id']}`"
        )
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("**Engineering Review Required.** Approve findings with")
    lines.append("`python scripts/review_findings.py set-status <review> <finding> approved "
                 "--approver <you> --scope <text>`,")
    lines.append("then run the implementation flow. Nothing is implemented automatically.")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _package_dir(args: argparse.Namespace) -> Path:
    from noam_coach.observability.review_package import package_dir

    directory = package_dir(args.reviews_dir, args.review_id)
    if not directory.exists():
        raise SystemExit(f"error: no review package {args.review_id!r} under {args.reviews_dir}")
    return directory


def _cmd_init(args: argparse.Namespace) -> int:
    directory = _package_dir(args)
    path = directory / FINDINGS_NAME
    if path.exists():
        raise SystemExit(f"error: {path} already exists")
    document = empty_findings_document(args.review_id, focus=args.focus)
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"created {path} (focus: {args.focus})")
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    problems = validate_findings_file(_package_dir(args) / FINDINGS_NAME)
    if problems:
        for problem in problems:
            print(f"INVALID: {problem}")
        return 1
    print("findings are valid")
    return 0


def _cmd_approved(args: argparse.Namespace) -> int:
    try:
        findings = approved_findings(_package_dir(args) / FINDINGS_NAME)
    except ValueError as exc:
        raise SystemExit(f"error: {exc}") from exc
    print(json.dumps(findings, ensure_ascii=False, indent=2))
    return 0


def _cmd_set_status(args: argparse.Namespace) -> int:
    path = _package_dir(args) / FINDINGS_NAME
    if args.status not in STATUSES:
        raise SystemExit(f"error: status must be one of {STATUSES}")
    data = load_findings_file(path)
    matched = [f for f in data.get("findings", []) if f.get("finding_id") == args.finding_id]
    if not matched:
        raise SystemExit(f"error: no finding {args.finding_id!r}")
    finding = matched[0]
    finding["status"] = args.status
    if args.status in _POST_APPROVAL_STATUSES and args.status == "approved":
        if not args.approver or not args.scope:
            raise SystemExit("error: approving requires --approver and --scope")
        finding["approval_record"] = {
            "approver": args.approver,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "approved_scope": args.scope,
            **({"notes": args.notes} if args.notes else {}),
        }
    finding["implementation_eligibility"] = compute_eligibility(finding)

    problems = validate_findings_data(data)
    if problems:
        # The mutation was in-memory only — the file on disk is untouched.
        for problem in problems:
            print(f"REFUSED: {problem}")
        return 1
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{args.finding_id} → {args.status}"
          f" (eligibility: {finding['implementation_eligibility']})")
    return 0


def _cmd_render_report(args: argparse.Namespace) -> int:
    directory = _package_dir(args)
    problems = validate_findings_file(directory / FINDINGS_NAME)
    if problems:
        for problem in problems:
            print(f"INVALID: {problem}")
        return 1
    report = render_report(directory)
    (directory / REPORT_NAME).write_text(report, encoding="utf-8")
    print(f"wrote {directory / REPORT_NAME}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Canonical review findings tooling")
    sub = parser.add_subparsers(dest="command", required=True)
    default_reviews = str(REPO_ROOT / "reviews")

    def _common(p: argparse.ArgumentParser) -> None:
        p.add_argument("review_id")
        p.add_argument("--reviews-dir", default=default_reviews)

    p_init = sub.add_parser("init", help="create an empty findings.json for a package")
    _common(p_init)
    p_init.add_argument("--focus", default="full", choices=FOCUSES)

    p_validate = sub.add_parser("validate", help="strictly validate findings.json")
    _common(p_validate)

    p_approved = sub.add_parser("approved", help="print approved findings (validates first)")
    _common(p_approved)

    p_status = sub.add_parser("set-status", help="transition one finding's status")
    _common(p_status)
    p_status.add_argument("finding_id")
    p_status.add_argument("status")
    p_status.add_argument("--approver", default=None)
    p_status.add_argument("--scope", default=None)
    p_status.add_argument("--notes", default=None)

    p_report = sub.add_parser("render-report", help="derive report.md from findings.json")
    _common(p_report)
    return parser


def main(argv: list[str] | None = None) -> int:
    # Windows consoles may run a narrow codepage (e.g. cp1255): never let
    # operator feedback crash on an arrow or Hebrew evidence text.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    # argparse subcommands share the review_id positional except set-status's extras.
    handler = {
        "init": _cmd_init,
        "validate": _cmd_validate,
        "approved": _cmd_approved,
        "set-status": _cmd_set_status,
        "render-report": _cmd_render_report,
    }[args.command]
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
