"""Audit the frozen production dependency graph with the active Python runtime."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> None:
    if sys.version_info[:2] != (3, 12):
        raise SystemExit("Dependency audit must run with the production Python 3.12 runtime.")
    with tempfile.TemporaryDirectory(prefix="agentic-audit-") as tmp_dir:
        requirements = Path(tmp_dir) / "production-requirements.txt"
        subprocess.run(
            [
                "uv",
                "export",
                "--frozen",
                "--no-dev",
                "--no-hashes",
                "--no-emit-project",
                "--output-file",
                str(requirements),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        subprocess.run(
            [sys.executable, "-m", "pip_audit", "--desc", "on", "-r", str(requirements)],
            check=True,
        )


if __name__ == "__main__":
    main()
