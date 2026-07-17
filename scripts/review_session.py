"""Session-review CLI — the operator entry point of the continuous-
improvement workflow (batches R4/R5).

Build an immutable, self-contained review package from real production
evidence, inspect it, and manage the review lifecycle. The builder is
strictly read-only against the database (safe on a live DB or a copied
snapshot), makes no AI call, and never advances the review cursor — the
cursor moves only through ``advance``, which demands a complete package,
validated findings and a generated report.

Usage:
    python scripts/review_session.py build [selection] [--db PATH] [--user U]
        selection (exactly one):
          --since-last-review            everything after the last completed review
          --start T [--end T]            explicit UTC window, start-inclusive end-exclusive
          --last-hours H                 recent duration
          --after-id N [--until-id N]    explicit canonical event-id range
          --trace TR [--trace TR2 ...]   explicit traces
          --interaction IN [...]         explicit interactions
        options: --rebuild REVIEW_ID     rebuild an old review's recorded selection
                 --review-id ID          explicit id (tests/tooling)
                 --reviews-dir DIR       default: <repo>/reviews
    python scripts/review_session.py list            [--reviews-dir DIR]
    python scripts/review_session.py show REVIEW_ID  [--reviews-dir DIR]
    python scripts/review_session.py validate REVIEW_ID
    python scripts/review_session.py show-finding REVIEW_ID FINDING_ID
    python scripts/review_session.py advance --review REVIEW_ID
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_REVIEWS_DIR = REPO_ROOT / "reviews"


def _default_user() -> int | None:
    try:
        from config import SETTINGS

        return int(SETTINGS.telegram_allowed_user_id)
    except Exception:  # noqa: BLE001
        return None


def _open_db(path: str | None):
    from db import Database

    if path is None:
        from config import SETTINGS

        path = SETTINGS.database_path
    if not Path(path).exists():
        raise SystemExit(f"error: database not found: {path}")
    return Database(str(path))


def _requested_selection(args: argparse.Namespace):
    from noam_coach.observability.review_window import RequestedSelection

    chosen = [
        name for name, on in (
            ("since-last-review", args.since_last_review),
            ("start/end", bool(args.start or args.end)),
            ("last-hours", args.last_hours is not None),
            ("after-id/until-id", args.after_id is not None or args.until_id is not None),
            ("trace", bool(args.trace)),
            ("interaction", bool(args.interaction)),
        ) if on
    ]
    if args.rebuild:
        if chosen:
            raise SystemExit("error: --rebuild uses the recorded selection; drop other selectors")
        return None
    if len(chosen) != 1:
        raise SystemExit(
            "error: pass exactly one selection "
            "(--since-last-review | --start/--end | --last-hours | --after-id | --trace | --interaction)"
            + (f"; got: {', '.join(chosen)}" if chosen else "")
        )
    if args.since_last_review:
        return RequestedSelection(mode="since_last_review")
    if args.start or args.end:
        return RequestedSelection(mode="time_window", start_time=args.start, end_time=args.end)
    if args.last_hours is not None:
        return RequestedSelection(mode="last_hours", last_hours=args.last_hours)
    if args.after_id is not None or args.until_id is not None:
        return RequestedSelection(
            mode="event_id_range",
            after_event_id=args.after_id or 0,
            until_event_id=args.until_id,
        )
    if args.trace:
        return RequestedSelection(mode="traces", trace_ids=tuple(args.trace))
    return RequestedSelection(mode="interactions", interaction_ids=tuple(args.interaction))


async def _cmd_build(args: argparse.Namespace) -> int:
    from noam_coach.observability.review_package import build_package, load_manifest
    from noam_coach.observability.review_window import (
        FIRST_REVIEW_DEFAULT_HOURS,
        NoCursorError,
        RequestedSelection,
        resolve_selection,
    )

    db = _open_db(args.db)
    user_id = int(args.user) if args.user else _default_user()
    if user_id is None:
        raise SystemExit("error: no user id (pass --user)")
    reviews_dir = Path(args.reviews_dir)

    rebuilt_from = None
    if args.rebuild:
        source_manifest = load_manifest(reviews_dir, args.rebuild)
        requested = RequestedSelection.from_dict(source_manifest["requested_selection"])
        if requested.mode in ("since_last_review", "last_hours"):
            # Relative selections drift; rebuild the exact recorded range.
            requested = RequestedSelection(
                mode="event_id_range",
                after_event_id=(source_manifest.get("first_event_id") or 1) - 1,
                until_event_id=source_manifest.get("last_event_id"),
            )
        rebuilt_from = args.rebuild
    else:
        requested = _requested_selection(args)

    try:
        resolved = await resolve_selection(db, user_id, requested, reviews_dir=reviews_dir)
    except NoCursorError as exc:
        raise SystemExit(
            f"error: {exc}\nfirst review? try: --last-hours {FIRST_REVIEW_DEFAULT_HOURS}"
        ) from exc

    result = await build_package(
        db,
        user_id,
        requested,
        resolved,
        reviews_dir=reviews_dir,
        review_id=args.review_id,
        rebuilt_from=rebuilt_from,
        repo_root=REPO_ROOT,
    )
    print(f"review package: {result.review_id}")
    print(f"  path: {result.path}")
    print(f"  events: {result.event_count} "
          f"(ids {result.manifest['first_event_id']}..{result.manifest['last_event_id']})")
    print(f"  interactions: {len(result.manifest['interaction_ids'])}"
          f" · traces: {len(result.manifest['trace_ids'])}")
    signals = json.loads((result.path / "signals.json").read_text(encoding="utf-8"))
    print(f"  deterministic signals: {len(signals['signals'])} (see signals.md)")
    print("  cursor NOT advanced — run the review, then: advance --review " + result.review_id)
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    from noam_coach.observability.review_package import list_packages

    packages = list_packages(args.reviews_dir)
    if not packages:
        print("no review packages")
        return 0
    for package in packages:
        line = f"{package['review_id']:24s} {package.get('status') or '?':10s}"
        if package.get("event_count") is not None:
            line += f" events={package['event_count']}"
        if package.get("rebuilt_from"):
            line += f" (rebuild of {package['rebuilt_from']})"
        print(line)
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    from noam_coach.observability.review_package import load_manifest

    manifest = load_manifest(args.reviews_dir, args.review_id)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    from noam_coach.observability.review_package import verify_package

    problems = verify_package(args.reviews_dir, args.review_id)
    if problems:
        for problem in problems:
            print(f"INVALID: {problem}")
        return 1
    print(f"package {args.review_id} is intact and complete")
    return 0


def _cmd_show_finding(args: argparse.Namespace) -> int:
    """Evidence navigation: one finding + the exact events it cites."""
    from noam_coach.observability.review_package import package_dir
    from scripts.review_findings import load_findings

    directory = package_dir(args.reviews_dir, args.review_id)
    findings = load_findings(directory)
    matched = [f for f in findings if f.get("finding_id") == args.finding_id]
    if not matched:
        known = ", ".join(f.get("finding_id", "?") for f in findings) or "none"
        raise SystemExit(f"error: no finding {args.finding_id!r} (known: {known})")
    finding = matched[0]
    print(json.dumps(finding, ensure_ascii=False, indent=2))

    events_path = directory / "events.jsonl"
    wanted: set[int] = set()
    for reference in finding.get("evidence_references") or []:
        wanted.update(int(item) for item in reference.get("event_ids") or [])
    if wanted and events_path.exists():
        print("\n--- referenced evidence events ---")
        with events_path.open(encoding="utf-8") as handle:
            for line in handle:
                row = json.loads(line)
                if row.get("id") in wanted:
                    print(json.dumps(row, ensure_ascii=False))
    elif wanted:
        print("\n(events.jsonl not present locally — raw evidence is gitignored)")
    return 0


def _cmd_advance(args: argparse.Namespace) -> int:
    """Move the last-review cursor — ONLY once the review truly completed:
    intact package, findings.json present AND valid, report.md generated."""
    from noam_coach.observability.review_package import load_manifest, package_dir, verify_package
    from noam_coach.observability.review_window import advance_cursor
    from scripts.review_findings import validate_findings_file

    reviews_dir = Path(args.reviews_dir)
    review_id = args.review
    problems = verify_package(reviews_dir, review_id)
    directory = package_dir(reviews_dir, review_id)
    findings_path = directory / "findings.json"
    report_path = directory / "report.md"
    if not findings_path.exists():
        problems.append("findings.json missing — the review analysis has not completed")
    else:
        problems.extend(validate_findings_file(findings_path))
    if not report_path.exists():
        problems.append("report.md missing — the review report has not been generated")
    if problems:
        for problem in problems:
            print(f"NOT ADVANCED: {problem}")
        return 1
    manifest = load_manifest(reviews_dir, review_id)
    last_id = manifest.get("last_event_id")
    if last_id is None:
        # Empty window: keep the cursor where the resolution left it.
        last_id = manifest["resolved_selection"].get("after_event_id") or 0
    state = advance_cursor(reviews_dir, last_reviewed_event_id=int(last_id), review_id=review_id)
    print(f"cursor advanced to event {state['last_reviewed_event_id']} (review {review_id})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Noam Coach session-review workflow")
    sub = parser.add_subparsers(dest="command", required=True)

    def _common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--reviews-dir", default=str(DEFAULT_REVIEWS_DIR))

    p_build = sub.add_parser("build", help="build an immutable review package")
    p_build.add_argument("--db", default=None)
    p_build.add_argument("--user", default=None)
    p_build.add_argument("--since-last-review", action="store_true")
    p_build.add_argument("--start", default=None)
    p_build.add_argument("--end", default=None)
    p_build.add_argument("--last-hours", type=float, default=None)
    p_build.add_argument("--after-id", type=int, default=None)
    p_build.add_argument("--until-id", type=int, default=None)
    p_build.add_argument("--trace", action="append", default=[])
    p_build.add_argument("--interaction", action="append", default=[])
    p_build.add_argument("--rebuild", default=None, metavar="REVIEW_ID")
    p_build.add_argument("--review-id", default=None)
    _common(p_build)

    p_list = sub.add_parser("list", help="list review packages")
    _common(p_list)

    p_show = sub.add_parser("show", help="print a package manifest")
    p_show.add_argument("review_id")
    _common(p_show)

    p_validate = sub.add_parser("validate", help="verify package integrity/hashes")
    p_validate.add_argument("review_id")
    _common(p_validate)

    p_finding = sub.add_parser("show-finding", help="print one finding + its evidence events")
    p_finding.add_argument("review_id")
    p_finding.add_argument("finding_id")
    _common(p_finding)

    p_advance = sub.add_parser(
        "advance", help="advance the last-review cursor (only after a COMPLETE review)"
    )
    p_advance.add_argument("--review", required=True)
    _common(p_advance)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "build":
        return asyncio.run(_cmd_build(args))
    handler = {
        "list": _cmd_list,
        "show": _cmd_show,
        "validate": _cmd_validate,
        "show-finding": _cmd_show_finding,
        "advance": _cmd_advance,
    }[args.command]
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
