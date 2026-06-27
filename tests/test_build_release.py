"""Tests for the release builder — it must produce a clean, forbidden-free archive."""

from __future__ import annotations

import importlib.util
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Import scripts/build_release.py as a module (scripts/ is not a package).
_spec = importlib.util.spec_from_file_location(
    "build_release", ROOT / "scripts" / "build_release.py"
)
assert _spec and _spec.loader
build_release = importlib.util.module_from_spec(_spec)
sys.modules["build_release"] = build_release
_spec.loader.exec_module(build_release)


def test_collect_files_excludes_forbidden() -> None:
    files = build_release.collect_files()
    rels = {p.relative_to(ROOT).as_posix() for p in files}
    # Core source is present.
    assert "coach_bot.py" in rels
    assert any(r.startswith("tests/") for r in rels)
    assert "noam_coach/runtime_bind.py" in rels
    assert "noam_coach/bot/callback_router.py" in rels
    assert "noam_coach/api/health_routes.py" in rels
    # Nothing forbidden leaked in.
    for rel in rels:
        assert not build_release._is_forbidden(Path(rel)), rel
    assert not any(".venv" in r or ".claude" in r or "__pycache__" in r for r in rels)
    assert not any(r.endswith(".db") or r.endswith(".env") for r in rels)


def test_inspect_zip_flags_forbidden(tmp_path: Path) -> None:
    dirty = tmp_path / "dirty.zip"
    with zipfile.ZipFile(dirty, "w") as zf:
        zf.writestr("coach_bot.py", "ok")
        zf.writestr(".env", "TELEGRAM_BOT_TOKEN=secret")
        zf.writestr(".venv/lib/x.py", "junk")
    problems = build_release.inspect_zip(dirty)
    assert ".env" in problems
    assert any(".venv" in p for p in problems)
    assert "coach_bot.py" not in problems


def test_build_produces_clean_zip(tmp_path: Path) -> None:
    out = tmp_path / "rel.zip"
    built = build_release.build(out)
    assert built.exists()
    assert build_release.inspect_zip(built) == []
    with zipfile.ZipFile(built) as zf:
        names = set(zf.namelist())
        assert "coach_bot.py" in names
        assert "noam_coach/app/runtime.py" in names
        assert "noam_coach/bot/callback_session.py" in names
