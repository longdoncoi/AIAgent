"""CLI script to run database migrations."""

import os
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.agentic.core.config import get_settings
from src.agentic.memory.migrations import run_database_migrations


def main() -> None:
    print("==================================================")
    print("        AGENTIC PLATFORM DATABASE MIGRATIONS      ")
    print("==================================================")
    settings = get_settings()
    success = run_database_migrations()
    if success:
        print(">> Database migration completed successfully.")
        sys.exit(0)
    else:
        print(">> ERROR: Database migrations failed or target database offline.")
        if settings.environment == "production" or os.environ.get("STRICT_MIGRATION") == "true":
            sys.exit(1)
        else:
            print(">> [Dev/Test Mode] Non-production environment; migration check logged without hard exit.")
            sys.exit(0)


if __name__ == "__main__":
    main()
