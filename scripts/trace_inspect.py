"""Local/admin trace inspection CLI (Observability O10).

Inspect the canonical product_events trace stream without hand-written
SQLite queries. This is a LOCAL tool — it is not exposed over HTTP.

Usage:
    python scripts/trace_inspect.py last            [--user U] [--json]
    python scripts/trace_inspect.py user <user_id>  [--limit N] [--json]
    python scripts/trace_inspect.py interaction <interaction_id> [--json]
    python scripts/trace_inspect.py trace-id <trace_id>          [--json]
    python scripts/trace_inspect.py ai <ai_call_id>              [--json]
    common: [--db PATH]  (defaults to SETTINGS.database_path)

Output: the deterministic human timeline by default, or structured JSON
with --json. Redaction guarantees hold by construction: events were
redacted at WRITE time by the canonical boundary, media appear only as
references, and this tool adds no un-redacted source.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


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


def _events_json(events: list[Any]) -> list[dict[str, Any]]:
    return [asdict(event) for event in events]


async def _emit_output(session, as_json: bool) -> None:
    from noam_coach.observability.session_trace import render_timeline

    if as_json:
        payload = {
            "user_id": session.user_id,
            "interactions": [
                {
                    "interaction_id": item.interaction_id,
                    "trace_id": item.trace_id,
                    "events": _events_json(list(item.events)),
                }
                for item in session.interactions
            ],
            "legacy_uncorrelated_events": _events_json(session.legacy_events),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render_timeline(session))


async def _cmd_user(args: argparse.Namespace) -> int:
    from noam_coach.observability.session_trace import load_session_trace

    db = _open_db(args.db)
    user_id = int(args.user_id)
    session = await load_session_trace(db, user_id, limit=args.limit)
    if not session.events:
        print(f"no events for user {user_id}")
        return 2
    await _emit_output(session, args.json)
    return 0


async def _cmd_last(args: argparse.Namespace) -> int:
    from noam_coach.observability.session_trace import load_session_trace, load_trace

    db = _open_db(args.db)
    user_id = int(args.user) if args.user else _default_user()
    if user_id is None:
        print("error: no user id (pass --user)")
        return 2
    session = await load_session_trace(db, user_id, limit=args.limit)
    last_trace_id = next(
        (e.trace_id for e in reversed(session.events) if e.trace_id), None
    )
    if last_trace_id is None:
        print(f"no correlated traces for user {user_id}")
        return 2
    await _emit_output(await load_trace(db, user_id, last_trace_id), args.json)
    return 0


async def _cmd_interaction(args: argparse.Namespace) -> int:
    from noam_coach.observability.session_trace import SessionTrace, load_interaction

    db = _open_db(args.db)
    user_id = int(args.user) if args.user else _default_user()
    trace = await load_interaction(db, user_id, args.interaction_id)
    if trace is None:
        print(f"error: no events for interaction {args.interaction_id!r}")
        return 2
    session = SessionTrace(user_id=user_id, events=trace.events, interactions=(trace,))
    await _emit_output(session, args.json)
    return 0


async def _cmd_trace_id(args: argparse.Namespace) -> int:
    from noam_coach.observability.session_trace import load_trace

    db = _open_db(args.db)
    user_id = int(args.user) if args.user else _default_user()
    session = await load_trace(db, user_id, args.trace_id)
    if not session.events:
        print(f"error: no events for trace {args.trace_id!r}")
        return 2
    await _emit_output(session, args.json)
    return 0


async def _cmd_ai(args: argparse.Namespace) -> int:
    import event_log

    db = _open_db(args.db)
    user_id = int(args.user) if args.user else _default_user()
    events = await event_log.list_events(db, user_id, limit=args.limit)
    matched = [
        e for e in events if e.properties.get("ai_call_id") == args.ai_call_id
    ]
    if not matched:
        print(f"error: no events for ai_call {args.ai_call_id!r}")
        return 2
    if args.json:
        print(json.dumps(_events_json(matched), ensure_ascii=False, indent=2))
    else:
        for event in matched:
            print(f"{event.created_at} · {event.event} · outcome={event.outcome}")
            detail = {
                key: event.properties.get(key)
                for key in ("purpose", "model", "operation", "duration_ms",
                            "error_type", "failure_class", "output_schema", "media_ids")
                if event.properties.get(key) is not None
            }
            print(f"  {json.dumps(detail, ensure_ascii=False)}")
            content = event.properties.get("content")
            if content:
                print(f"  content: {json.dumps(content, ensure_ascii=False)[:1500]}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspect Noam Coach interaction traces")
    parser.add_argument("--db", default=None, help="SQLite path (default: SETTINGS.database_path)")
    sub = parser.add_subparsers(dest="command", required=True)

    def _common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--json", action="store_true", help="structured JSON output")
        p.add_argument("--limit", type=int, default=2000)
        p.add_argument("--user", default=None, help="user id (default: allowed user)")
        p.add_argument("--db", dest="db", default=None)

    p_last = sub.add_parser("last", help="latest correlated trace")
    _common(p_last)
    p_user = sub.add_parser("user", help="recent session timeline for a user")
    p_user.add_argument("user_id")
    _common(p_user)
    p_inter = sub.add_parser("interaction", help="one interaction")
    p_inter.add_argument("interaction_id")
    _common(p_inter)
    p_trace = sub.add_parser("trace-id", help="one trace")
    p_trace.add_argument("trace_id")
    _common(p_trace)
    p_ai = sub.add_parser("ai", help="one AI call")
    p_ai.add_argument("ai_call_id")
    _common(p_ai)
    return parser


async def main_async(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handler = {
        "last": _cmd_last,
        "user": _cmd_user,
        "interaction": _cmd_interaction,
        "trace-id": _cmd_trace_id,
        "ai": _cmd_ai,
    }[args.command]
    return await handler(args)


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(main_async(argv))


if __name__ == "__main__":
    raise SystemExit(main())
