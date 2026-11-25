"""Purge PostgreSQL checkpoint threads older than the configured retention window."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.agentic.memory.data_lifecycle import (
    cleanup_stale_postgres_checkpoints,
)
from src.agentic.memory.long_term import get_long_term_store


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-idle-seconds", type=int, default=86400 * 30)
    parser.add_argument("--ltm-max-age-days", type=int, default=90)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    count = cleanup_stale_postgres_checkpoints(
        args.max_idle_seconds,
        dry_run=args.dry_run,
    )
    action = "would purge" if args.dry_run else "purged"
    print(f"Checkpoint retention {action} {count} stale thread(s).")
    if not args.dry_run:
        ltm_deleted = get_long_term_store().delete_older_than(args.ltm_max_age_days)
        print(f"Long-term memory retention purged {ltm_deleted} expired record(s).")


if __name__ == "__main__":
    main()
