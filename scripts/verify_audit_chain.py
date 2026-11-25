#!/usr/bin/env python3
"""Verify tamper-evident HMAC hash-chain integrity across stored audit_logs."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.agentic.safety.audit import verify_audit_chain


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify audit_logs HMAC hash chain.")
    parser.add_argument(
        "--sqlite-path",
        type=Path,
        default=None,
        help="Optional local SQLite audit DB path instead of PostgreSQL.",
    )
    return parser


def main() -> int:
    """CLI entrypoint returning 0 on intact chain or 1 on integrity failure."""
    args = _parser().parse_args()
    valid, checked, errors = verify_audit_chain(sqlite_path=args.sqlite_path)
    print(json.dumps({"valid": valid, "checked_records": checked, "errors": errors}, indent=2))
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
