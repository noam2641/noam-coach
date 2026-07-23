#!/usr/bin/env python3
"""Validate project files, imports, settings, storage and an existing database."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import os
import py_compile
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv

REQUIRED_FILES = (
    "coach_bot.py",
    "assistant.py",
    "conversation.py",
    "coach_intelligence.py",
    "data_quality.py",
    "event_log.py",
    "feature_flags.py",
    "health_import.py",
    "meal_intelligence.py",
    "onboarding.py",
    "planning.py",
    "questions.py",
    "recommendations.py",
    "reconcile.py",
    "routine.py",
    "targets.py",
    "training_intelligence.py",
    "user_model.py",
    "requirements.lock",
)
MODULES = tuple(Path(name).stem for name in REQUIRED_FILES if name.endswith(".py"))
STRUCTURED_MODULES = (
    "noam_coach.runtime_bind",
    "noam_coach.services.core",
    "noam_coach.services.training",
    "noam_coach.bot.callback_router",
    "noam_coach.bot.callback_session",
    "noam_coach.api.security",
    "noam_coach.api.system_routes",
    "noam_coach.api.health_routes",
    "noam_coach.api.watch_routes",
    "noam_coach.jobs.proactive",
    "noam_coach.app.runtime",
)


def check_database(path: Path) -> list[str]:
    if not path.exists():
        return ["database does not exist yet; it will be created on first start"]
    connection = sqlite3.connect(path)
    try:
        quick = connection.execute("PRAGMA quick_check").fetchone()[0]
        if quick != "ok":
            raise RuntimeError(f"SQLite quick_check failed: {quick}")
        connection.execute("PRAGMA foreign_keys=ON")
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(f"Foreign key violations: {violations[:5]}")
    finally:
        connection.close()
    return []


async def migrate_if_requested(path: Path) -> None:
    import coach_bot

    db = coach_bot.Database(str(path))
    await db.init()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--migrate", action="store_true")
    parser.add_argument("--skip-runtime-secrets", action="store_true")
    args = parser.parse_args()
    load_dotenv()
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    failures: list[str] = []
    warnings: list[str] = []

    for name in REQUIRED_FILES:
        if not (root / name).exists():
            failures.append(f"missing file: {name}")
    for name in REQUIRED_FILES:
        if name.endswith(".py") and (root / name).exists():
            try:
                py_compile.compile(str(root / name), doraise=True)
            except Exception as exc:
                failures.append(f"compile {name}: {exc}")
    package_root = root / "noam_coach"
    if not package_root.is_dir():
        failures.append("missing directory: noam_coach")
    else:
        for source in package_root.rglob("*.py"):
            try:
                py_compile.compile(str(source), doraise=True)
            except Exception as exc:
                failures.append(f"compile {source.relative_to(root)}: {exc}")

    for module in MODULES + STRUCTURED_MODULES:
        try:
            importlib.import_module(module)
        except Exception as exc:
            failures.append(f"import {module}: {type(exc).__name__}: {exc}")

    if not failures:
        import coach_bot

        if not args.skip_runtime_secrets:
            try:
                coach_bot.SETTINGS.validate_runtime()
            except Exception as exc:
                failures.append(f"runtime settings: {exc}")
        # Startup-boundary guard: an empty/relative/unresolved DATABASE_PATH must
        # fail here rather than silently create dirs (and later a DB) under the
        # repository root. On failure, skip the directory/DB probes below.
        db_path: Path | None = None
        try:
            db_path = coach_bot.assert_safe_database_path(
                coach_bot.SETTINGS.database_path
            )
        except Exception as exc:
            failures.append(f"database path: {exc}")
        storage = Path(coach_bot.SETTINGS.storage_dir).expanduser()
        backup_dir = Path(os.getenv("BACKUP_DIR", "./backups")).expanduser()
        folders = [("storage", storage), ("backup directory", backup_dir)]
        if db_path is not None:
            folders.insert(0, ("database parent", db_path.parent))
        for label, folder in folders:
            try:
                folder.mkdir(parents=True, exist_ok=True)
                probe = folder / ".preflight_write"
                probe.write_text("ok", encoding="utf-8")
                probe.unlink()
            except Exception as exc:
                failures.append(f"{label} is not writable: {exc}")
        if coach_bot.SETTINGS.app_env == "production":
            domain = os.getenv("DOMAIN", "").strip()
            if not domain:
                failures.append("DOMAIN is required for the bundled Caddy deployment")
        if db_path is not None:
            try:
                warnings.extend(check_database(db_path))
            except Exception as exc:
                failures.append(f"database: {exc}")
        if args.migrate and not failures:
            asyncio.run(migrate_if_requested(db_path))

    for warning in warnings:
        print(f"WARNING: {warning}")
    if failures:
        for failure in failures:
            print(f"ERROR: {failure}")
        raise SystemExit(1)
    print("Preflight passed")


if __name__ == "__main__":
    main()
