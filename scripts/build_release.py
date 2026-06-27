#!/usr/bin/env python3
"""Build a clean release ZIP from an allowlist, then inspect it for forbidden files.

The release archive must contain ONLY source, tests, migrations, docs, examples
and deployment files — never secrets, private data, virtualenvs or caches. This
script never zips "the whole folder"; it copies an explicit allowlist and then
fails loudly if any forbidden pattern slipped in.

Usage:
    python scripts/build_release.py                 # build dist/noam_coach_<version>.zip
    python scripts/build_release.py --inspect-only  # only validate an existing build
"""

from __future__ import annotations

import argparse
import fnmatch
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Top-level files included verbatim.
ALLOWED_FILES = (
    "README.md",
    "CHANGELOG.md",
    "MANIFEST.md",
    "IMPLEMENTATION_STATUS.md",
    "VALIDATION_REPORT.md",
    "ENVIRONMENT_VARIABLES.md",
    "SECURITY.md",
    "REBUILD_REPORT.md",
    "PROJECT_TREE.txt",
    "VERSION",
    "Makefile",
    "pyproject.toml",
    "pytest.ini",
    "requirements.in",
    "requirements.txt",
    "requirements.lock",
    "requirements-dev.txt",
    "Dockerfile",
    "compose.yaml",
    "Caddyfile",
    ".dockerignore",
    ".gitignore",
)

# Every *.py at the project root is source and is included.
INCLUDE_ROOT_PY = True

# Directories copied recursively (with per-file filtering below).
ALLOWED_DIRS = (
    "noam_coach",
    "scripts",
    "tests",
    "docs",
    "examples",
    "evaluations",
    "miniapp",
    ".github",
)

# A file is dropped if it matches any of these (defence in depth on top of dirs).
FORBIDDEN_GLOBS = (
    "*.db", "*.db-wal", "*.db-shm", "*.pre_migration_*.db",
    "*.pyc", "*.pyo", "*.log", "*.zip", "*.mht",
    ".env", ".env.*",
    "*.png", "*.jpg", "*.jpeg", "*.gif", "*.mp4", "*.mov",
)

# A path is dropped if any of its parts is one of these.
FORBIDDEN_PARTS = {
    ".venv", ".git", ".claude", "__pycache__",
    ".pytest_cache", ".ruff_cache", ".mypy_cache",
    "data", "storage", "backups", "htmlcov", "dist",
}


def _read_version() -> str:
    try:
        return (ROOT / "VERSION").read_text(encoding="utf-8").strip() or "0.0.0"
    except OSError:
        return "0.0.0"


def _is_forbidden(rel: Path) -> bool:
    if any(part in FORBIDDEN_PARTS for part in rel.parts):
        return True
    name = rel.name
    return any(fnmatch.fnmatch(name, pat) for pat in FORBIDDEN_GLOBS)


def collect_files() -> list[Path]:
    """Return the allowlisted, forbidden-filtered set of files to ship."""
    files: list[Path] = []
    for name in ALLOWED_FILES:
        path = ROOT / name
        if path.is_file():
            files.append(path)
    if INCLUDE_ROOT_PY:
        files.extend(p for p in ROOT.glob("*.py") if p.is_file())
    for dir_name in ALLOWED_DIRS:
        base = ROOT / dir_name
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if path.is_file():
                rel = path.relative_to(ROOT)
                if not _is_forbidden(rel):
                    files.append(path)
    # Deduplicate, stable order for a deterministic archive.
    unique = sorted({p.relative_to(ROOT).as_posix() for p in files})
    return [ROOT / rel for rel in unique]


def inspect_zip(zip_path: Path) -> list[str]:
    """Return a list of forbidden entries found inside *zip_path* (empty == clean)."""
    problems: list[str] = []
    with zipfile.ZipFile(zip_path) as zf:
        for entry in zf.namelist():
            rel = Path(entry)
            if _is_forbidden(rel):
                problems.append(entry)
    return problems


def build(output: Path) -> Path:
    files = collect_files()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in files:
            zf.write(path, path.relative_to(ROOT).as_posix())
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="Build/inspect a clean release ZIP.")
    parser.add_argument("--output", default=None, help="Output ZIP path.")
    parser.add_argument(
        "--inspect-only",
        metavar="ZIP",
        help="Only inspect an existing ZIP for forbidden files.",
    )
    args = parser.parse_args()

    if args.inspect_only:
        zip_path = Path(args.inspect_only)
        if not zip_path.is_file():
            print(f"ERROR: {zip_path} not found", file=sys.stderr)
            return 2
        problems = inspect_zip(zip_path)
        if problems:
            print("FORBIDDEN ENTRIES FOUND:", file=sys.stderr)
            for entry in problems:
                print(f"  - {entry}", file=sys.stderr)
            return 1
        print(f"OK: {zip_path} contains no forbidden files.")
        return 0

    version = _read_version()
    output = Path(args.output) if args.output else (ROOT / "dist" / f"noam_coach_{version}.zip")
    built = build(output)
    problems = inspect_zip(built)
    if problems:
        print("BUILD PRODUCED A DIRTY ARCHIVE:", file=sys.stderr)
        for entry in problems:
            print(f"  - {entry}", file=sys.stderr)
        built.unlink(missing_ok=True)
        return 1

    with zipfile.ZipFile(built) as zf:
        count = len(zf.namelist())
    print(f"OK: built {built.relative_to(ROOT)} with {count} files (no forbidden entries).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
