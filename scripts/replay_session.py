"""Print a privacy-conscious event timeline for one user.

Usage:
    python scripts/replay_session.py --user-id 123456 --limit 200
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import event_log  # noqa: E402
from config import SETTINGS  # noqa: E402
from db import Database  # noqa: E402


async def _run(user_id: int, limit: int) -> None:
    db = Database(SETTINGS.database_path)
    await db.init()
    print(await event_log.replay_summary(db, user_id, limit=limit))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-id", type=int, required=True)
    parser.add_argument("--limit", type=int, default=200)
    args = parser.parse_args()
    asyncio.run(_run(args.user_id, args.limit))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
