"""Pure, unit-testable acceptance criteria for the Locust release profile."""

from __future__ import annotations

from typing import Protocol

MAX_ERROR_RATIO = 0.001
MAX_P95_MS = 2500
MAX_P99_MS = 4500


class AggregateStats(Protocol):
    fail_ratio: float

    def get_response_time_percentile(self, percentile: float) -> int | None: ...


def load_gate_failures(stats: AggregateStats) -> list[str]:
    """Return objective release-gate violations from aggregate statistics."""
    failures: list[str] = []
    p95 = int(stats.get_response_time_percentile(0.95) or 0)
    p99 = int(stats.get_response_time_percentile(0.99) or 0)
    if float(stats.fail_ratio) >= MAX_ERROR_RATIO:
        failures.append(
            f"error rate {float(stats.fail_ratio) * 100:.3f}% must be < 0.100%"
        )
    if p95 >= MAX_P95_MS:
        failures.append(f"P95 {p95}ms must be < {MAX_P95_MS}ms")
    if p99 >= MAX_P99_MS:
        failures.append(f"P99 {p99}ms must be < {MAX_P99_MS}ms")
    return failures
