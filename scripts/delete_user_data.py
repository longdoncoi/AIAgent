#!/usr/bin/env python3
"""CLI for the Right-to-be-Forgotten data purge service.

Examples::

    python scripts/delete_user_data.py --user-id alice --dry-run
    python scripts/delete_user_data.py --user-id alice --thread-id public-123 --confirm
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.agentic.memory.data_lifecycle import purge_user_data


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Purge all persisted data for a user or thread.")
    parser.add_argument("--user-id", required=True, help="Principal whose data must be erased.")
    parser.add_argument("--thread-id", default=None, help="Optional public thread id to purge.")
    parser.add_argument(
        "--dry-run", action="store_true", help="Preview deletions without mutating any store."
    )
    parser.add_argument(
        "--confirm", action="store_true", help="Explicit confirmation for a destructive purge."
    )
    parser.add_argument(
        "--actor-id",
        default=os.environ.get("USER") or os.environ.get("USERNAME") or "cli_operator",
        help="Executing operator identifier recorded in audit_logs.",
    )
    return parser


async def _run(args: argparse.Namespace) -> int:
    if not args.dry_run and not args.confirm:
        print("Refusing to purge without --confirm (or use --dry-run).", file=sys.stderr)
        return 2
    report = await purge_user_data(
        args.user_id, args.thread_id, actor_id=args.actor_id, dry_run=args.dry_run
    )
    print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    return 0 if report.status != "partial" else 1


def main() -> None:
    """Entry point."""
    args = _parser().parse_args()
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
