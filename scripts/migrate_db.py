"""CLI script to run database migrations."""

import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.agentic.memory.migrations import run_database_migrations


def main() -> None:
    print("==================================================")
    print("        AGENTIC PLATFORM DATABASE MIGRATIONS      ")
    print("==================================================")
    success = run_database_migrations()
    if success:
        print(">> Database migration completed successfully.")
        sys.exit(0)
    else:
        print(">> Note: Database migrations skipped or target database offline.")
        sys.exit(0)


if __name__ == "__main__":
    main()
