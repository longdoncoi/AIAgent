"""CLI script to run or verify database migrations."""

import argparse
import os
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.agentic.core.config import get_settings
from src.agentic.memory.migrations import check_database_migrations, run_database_migrations


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run or verify database migrations.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify database schema is at Alembic head without modifying state.",
    )
    args = parser.parse_args(argv)

    print("==================================================")
    print("        AGENTIC PLATFORM DATABASE MIGRATIONS      ")
    print("==================================================")
    settings = get_settings()

    if args.check:
        try:
            at_head = check_database_migrations()
        except Exception as exc:
            print(f">> ERROR: Schema check failed: {exc}")
            sys.exit(1)
        if at_head:
            print(">> Database schema is up-to-date (at Alembic head).")
            sys.exit(0)
        print(">> ERROR: Database schema is NOT at Alembic head.")
        sys.exit(1)

    try:
        success = run_database_migrations()
    except RuntimeError as exc:
        print(f">> ERROR: Database migrations failed: {exc}")
        if settings.environment == "production" or os.environ.get("STRICT_MIGRATION") == "true":
            sys.exit(1)
        print(">> [Dev/Test Mode] Non-production environment; migration check logged without hard exit.")
        sys.exit(0)

    if success:
        print(">> Database migration completed successfully.")
        sys.exit(0)

    print(">> ERROR: Database migrations failed or target database offline.")
    if settings.environment == "production" or os.environ.get("STRICT_MIGRATION") == "true":
        sys.exit(1)
    print(">> [Dev/Test Mode] Non-production environment; migration check logged without hard exit.")
    sys.exit(0)


if __name__ == "__main__":
    main()
