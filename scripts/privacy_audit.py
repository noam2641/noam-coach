#!/usr/bin/env python3
"""Scan the workspace for files that need privacy review before sharing.

The audit is read-only. It does not delete, move, stage, or modify files.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

EXCLUDED_DIRS = {
    ".git",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "backups",
    "storage",
}

HIGH_RISK_SUFFIXES = {
    ".db",
    ".sqlite",
    ".sqlite3",
    ".env",
    ".log",
    ".mht",
}

REVIEW_SUFFIXES = {
    ".docx",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".heic",
    ".mov",
    ".mp4",
    ".zip",
}

REVIEW_NAME_TOKENS = (
    "recording",
    "screenshot",
    "screen",
    "telegram",
    "health",
    "export",
)


@dataclass(frozen=True)
class PrivacyFinding:
    path: str
    severity: str
    reason: str


def _is_excluded(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    return any(part in EXCLUDED_DIRS for part in relative.parts)


def classify_path(path: Path, root: Path) -> PrivacyFinding | None:
    relative = path.relative_to(root).as_posix()
    name = path.name.casefold()
    suffix = path.suffix.casefold()
    if name == ".env" or name.endswith(".env"):
        return PrivacyFinding(relative, "high", "forbidden sensitive file type .env")
    if suffix in HIGH_RISK_SUFFIXES:
        return PrivacyFinding(relative, "high", f"forbidden sensitive file type {suffix}")
    if suffix in REVIEW_SUFFIXES and any(token in name for token in REVIEW_NAME_TOKENS):
        return PrivacyFinding(
            relative,
            "review",
            "media/export/recording file; blur Telegram contacts and health details before sharing",
        )
    return None


def scan_privacy_risks(root: Path) -> list[PrivacyFinding]:
    root = root.resolve()
    findings: list[PrivacyFinding] = []
    for path in root.rglob("*"):
        if not path.is_file() or _is_excluded(path, root):
            continue
        finding = classify_path(path, root)
        if finding is not None:
            findings.append(finding)
    return sorted(findings, key=lambda item: (item.severity, item.path))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", default=".")
    parser.add_argument(
        "--fail-on-review",
        action="store_true",
        help="Exit non-zero for review-level findings as well as high-risk findings.",
    )
    args = parser.parse_args()
    findings = scan_privacy_risks(Path(args.root))
    for finding in findings:
        print(f"{finding.severity.upper()}: {finding.path} - {finding.reason}")
    high = [finding for finding in findings if finding.severity == "high"]
    review = [finding for finding in findings if finding.severity == "review"]
    if high or (args.fail_on_review and review):
        raise SystemExit(1)
    print("Privacy audit passed")


if __name__ == "__main__":
    main()
