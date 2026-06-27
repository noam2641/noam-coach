#!/usr/bin/env python3
"""Run application migrations against the configured or supplied SQLite DB."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


async def main_async(path: Path) -> None:
    import coach_bot

    db = coach_bot.Database(str(path))
    await db.init()
    print(f"Migrations complete: {path}")


def main() -> None:
    import coach_bot

    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=coach_bot.SETTINGS.database_path)
    args = parser.parse_args()
    asyncio.run(main_async(Path(args.db).expanduser().resolve()))


if __name__ == "__main__":
    main()
