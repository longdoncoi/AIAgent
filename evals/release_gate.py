"""Strict release gate: production-readiness measurement, no heuristics allowed.

This is the ONLY entry point allowed to produce a go-live assessment. It runs:

* ``evals/run_eval.py --mode release`` (routing / groundedness / faithfulness)
* ``evals/ragas_runner.py --mode release`` (official ragas library)
* ``evals/red_teaming_runner.py`` (guardrail defense)

Smoke-only heuristics (e.g. the DeepEval-style local judge when ``deepeval`` is
not installed) are reported as SKIPPED and are never used as release evidence.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

# Ensure project root is in sys.path when invoked as a standalone script.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals.answer_generator import PROXY_BANNER, release_blockers  # noqa: E402
from evals.deepeval_runner import DEEPEVAL_INSTALLED
from evals.ragas_runner import run_ragas_benchmark
from evals.red_teaming_runner import run_red_teaming_benchmark
from evals.run_eval import run_benchmark


async def run_release_gate() -> bool:
    """Return True only if every release-quality gate passes."""
    print("==========================================================")
    print("       RELEASE QUALITY GATE (production-readiness)         ")
    print("==========================================================")

    blockers = release_blockers()
    if blockers:
        print(">> RELEASE GATE FAILED — prerequisites missing:")
        for blocker in blockers:
            print(f"   - {blocker}")
        return False

    if not importlib.util.find_spec("ragas"):
        print(">> RELEASE GATE FAILED: ragas library not installed.")
        return False

    # Production retrieval must use the neural reranker and real BM25, not the
    # labelled heuristic fallbacks. Install with `.[dev,neural]`.
    missing_neural = [
        name
        for name in ("sentence_transformers", "rank_bm25")
        if importlib.util.find_spec(name) is None
    ]
    if missing_neural:
        print(f">> RELEASE GATE FAILED: missing neural retrieval stack: {missing_neural}")
        return False

    results: dict[str, bool] = {}
    results["routing_groundedness"] = await run_benchmark("release")
    results["ragas"] = await run_ragas_benchmark("release")
    results["red_teaming"] = run_red_teaming_benchmark()

    if DEEPEVAL_INSTALLED:
        from evals.deepeval_runner import run_deepeval_benchmark

        results["deepeval"] = await run_deepeval_benchmark("release")
    else:
        print(">> SKIPPED: deepeval library not installed — heuristic judge is smoke-only.")

    print("\n---------------- RELEASE GATE SUMMARY ----------------")
    for name, passed in results.items():
        print(f"  {name:<24}: {'PASS' if passed else 'FAIL'}")
    print("-----------------------------------------------------")

    passed = all(results.values())
    if passed:
        print(">> ALL RELEASE QUALITY GATES PASSED (Ready for Production).")
    else:
        print(f">> RELEASE GATE FAILED — no production-readiness claim. ({PROXY_BANNER})")
    return passed


def main() -> None:
    if not asyncio.run(run_release_gate()):
        sys.exit(1)


if __name__ == "__main__":
    main()
