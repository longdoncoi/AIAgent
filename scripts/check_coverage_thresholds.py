"""Fail closed when measured coverage misses the production quality floors."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

# Security and state-integrity code receives explicit per-file floors in
# addition to the repository-wide thresholds. This prevents excellent coverage
# in low-risk modules from hiding a regression in an authorization boundary.
CRITICAL_COVERAGE_FLOORS: dict[str, tuple[float, float]] = {
    "src/agentic/a2a/identity.py": (100.0, 95.0),
    "src/agentic/api/middleware.py": (86.0, 68.0),
    "src/agentic/memory/migrations.py": (99.0, 75.0),
    "src/agentic/memory/short_term.py": (90.0, 85.0),
    "src/agentic/memory/thread_registry.py": (94.0, 84.0),
    "src/agentic/observability/tracing.py": (100.0, 85.0),
    "src/agentic/safety/approvals.py": (89.0, 78.0),
    "src/agentic/safety/audit.py": (97.0, 95.0),
    "src/agentic/safety/guardrails.py": (95.0, 87.0),
    "src/agentic/tools/servers/security.py": (87.0, 85.0),
}


def _percentage(covered: int, total: int) -> float:
    return 100.0 if total == 0 else covered * 100.0 / total


def _line_counts(summary: dict[str, Any]) -> tuple[int, int]:
    return int(summary.get("covered_lines", 0)), int(summary.get("num_statements", 0))


def _branch_counts(summary: dict[str, Any]) -> tuple[int, int]:
    return int(summary.get("covered_branches", 0)), int(summary.get("num_branches", 0))


def measured_percentages(report: dict[str, Any]) -> tuple[float, float, float]:
    """Return overall line, overall branch, and core line percentages."""
    totals = report.get("totals")
    files = report.get("files")
    if not isinstance(totals, dict) or not isinstance(files, dict):
        raise ValueError("coverage JSON must contain object fields 'totals' and 'files'")

    covered_lines, total_lines = _line_counts(totals)
    covered_branches, total_branches = _branch_counts(totals)
    if total_lines == 0 or total_branches == 0:
        raise ValueError("coverage JSON has no measured lines or branches")

    core_covered = 0
    core_total = 0
    for filename, details in files.items():
        normalized = str(filename).replace("\\", "/")
        if not normalized.startswith("src/agentic/core/"):
            continue
        summary = details.get("summary", {}) if isinstance(details, dict) else {}
        covered, total = _line_counts(summary)
        core_covered += covered
        core_total += total
    if core_total == 0:
        raise ValueError("coverage JSON contains no src/agentic/core files")

    return (
        _percentage(covered_lines, total_lines),
        _percentage(covered_branches, total_branches),
        _percentage(core_covered, core_total),
    )


def critical_percentages(report: dict[str, Any]) -> dict[str, tuple[float, float]]:
    """Return line and branch percentages for every critical source file."""
    raw_files = report.get("files")
    if not isinstance(raw_files, dict):
        raise ValueError("coverage JSON must contain object field 'files'")
    files = {str(name).replace("\\", "/"): details for name, details in raw_files.items()}
    measured: dict[str, tuple[float, float]] = {}
    for filename in CRITICAL_COVERAGE_FLOORS:
        details = files.get(filename)
        if not isinstance(details, dict):
            raise ValueError(f"coverage JSON is missing critical file '{filename}'")
        summary = details.get("summary")
        if not isinstance(summary, dict):
            raise ValueError(f"coverage JSON has no summary for critical file '{filename}'")
        covered_lines, total_lines = _line_counts(summary)
        covered_branches, total_branches = _branch_counts(summary)
        if total_lines == 0 or total_branches == 0:
            raise ValueError(f"critical file '{filename}' has no measured lines or branches")
        measured[filename] = (
            _percentage(covered_lines, total_lines),
            _percentage(covered_branches, total_branches),
        )
    return measured


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("report", nargs="?", default="coverage.json")
    parser.add_argument("--line-min", type=float, default=92.0)
    parser.add_argument("--branch-min", type=float, default=81.5)
    parser.add_argument("--core-line-min", type=float, default=98.0)
    args = parser.parse_args()

    report_path = Path(args.report)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    line, branch, core_line = measured_percentages(report)
    print(f"Measured coverage: line={line:.2f}% branch={branch:.2f}% core-line={core_line:.2f}%")
    critical = critical_percentages(report)

    failures: list[str] = []
    for label, actual, minimum in (
        ("line", line, args.line_min),
        ("branch", branch, args.branch_min),
        ("core-line", core_line, args.core_line_min),
    ):
        if actual < minimum:
            failures.append(f"{label} {actual:.2f}% < required {minimum:.2f}%")
    for filename, (actual_line, actual_branch) in critical.items():
        min_line, min_branch = CRITICAL_COVERAGE_FLOORS[filename]
        print(
            f"Critical coverage {filename}: line={actual_line:.2f}% "
            f"branch={actual_branch:.2f}%"
        )
        if actual_line < min_line:
            failures.append(
                f"{filename} line {actual_line:.2f}% < required {min_line:.2f}%"
            )
        if actual_branch < min_branch:
            failures.append(
                f"{filename} branch {actual_branch:.2f}% < required {min_branch:.2f}%"
            )
    if failures:
        for failure in failures:
            print(f"ERROR: {failure}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
