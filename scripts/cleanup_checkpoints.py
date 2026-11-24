"""Purge PostgreSQL checkpoint threads older than the configured retention window."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.agentic.memory.data_lifecycle import (  # noqa: E402
    cleanup_stale_postgres_checkpoints,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-idle-seconds", type=int, default=86400 * 30)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    count = cleanup_stale_postgres_checkpoints(
        args.max_idle_seconds,
        dry_run=args.dry_run,
    )
    action = "would purge" if args.dry_run else "purged"
    print(f"Checkpoint retention {action} {count} stale thread(s).")


if __name__ == "__main__":
    main()
