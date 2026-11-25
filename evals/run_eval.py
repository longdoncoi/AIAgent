"""Evaluation runner script for CI/CD gating.

Two explicit modes exist:

* ``smoke`` (default; used by CI): fast coverage check. May use heuristic proxies
  and relaxed *smoke-only* thresholds. It MUST NOT claim production readiness.
* ``release``: strict release gate. Uses the unmodified thresholds from
  ``configs/eval_thresholds.yaml``, scores the *generated answer*, and fails hard
  when the LLM gateway / judge is unavailable or any answer is a proxy.

Usage::

    python evals/run_eval.py --mode smoke
    python evals/run_eval.py --mode release
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Awaitable
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langchain_core.messages import HumanMessage

from evals.answer_generator import (
    PROXY_BANNER,
    gateway_reachable,
    generate_answer,
    llm_judge_available,
    release_blockers,
)
from evals.dataset_io import load_jsonl
from evals.evaluators import (
    evaluate_faithfulness,
    evaluate_groundedness,
    evaluate_ragas_faithfulness,
    evaluate_routing_accuracy,
)
from src.agentic.core.config import get_settings
from src.agentic.orchestrator.nodes.planner import planner_node
from src.agentic.orchestrator.state import AgentState
from src.agentic.rag.tools import asearch_policy
from src.agentic.safety.policy import get_policy_manager

# Smoke-only thresholds. These are NOT production thresholds and are never used
# by release mode. They exist so CI can detect gross regressions cheaply.
SMOKE_THRESHOLDS = {
    "routing_accuracy": 0.80,
    "tool_selection": 0.80,
    "groundedness": 0.40,
    "faithfulness": 0.50,
}
RELEASE_DEFAULT_THRESHOLDS = {
    "routing_accuracy": 0.95,
    "tool_selection": 0.90,
    "groundedness": 0.90,
    "faithfulness": 0.85,
}

_SMOKE_ENV_OVERRIDES = {
    "FAST_HEURISTIC_PLANNER": "true",
    "SKIP_LLM_JUDGE": "true",
}
class EvaluationTimeout(RuntimeError):
    """Raised when a bounded evaluation operation exceeds its hard deadline."""


async def _bounded[T](awaitable: Awaitable[T], *, label: str, timeout: float) -> T:
    """Await one evaluation operation with a diagnostic fail-closed timeout."""
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout)
    except TimeoutError as exc:
        raise EvaluationTimeout(f"{label} timed out after {timeout:.1f}s") from exc


def resolve_mode(requested: str | None = None) -> str:
    """Resolve evaluation mode from CLI arg, env var, then default to smoke."""
    mode = (requested or os.environ.get("EVAL_MODE") or "smoke").strip().lower()
    if mode not in {"smoke", "release"}:
        raise ValueError(f"Unknown evaluation mode: {mode!r} (expected 'smoke' or 'release')")
    return mode


def _load_release_thresholds() -> dict[str, float]:
    """Load official thresholds unchanged. No CI/offline downgrades are permitted."""
    settings = get_settings()
    thresholds = dict(RELEASE_DEFAULT_THRESHOLDS)
    thresholds_file = settings.project_root / "configs" / "eval_thresholds.yaml"
    if thresholds_file.exists():
        import yaml

        with open(thresholds_file, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        quality_gates = cfg.get("quality_gates") or {}
        for key in thresholds:
            if key in quality_gates:
                thresholds[key] = float(quality_gates[key])
    return thresholds


async def run_benchmark(mode: str | None = None) -> bool:
    """Run a bounded benchmark and restore temporary smoke-mode switches."""
    mode = resolve_mode(mode)
    gateway_online = gateway_reachable()
    previous_env = {key: os.environ.get(key) for key in _SMOKE_ENV_OVERRIDES}
    if mode == "smoke":
        os.environ.update(_SMOKE_ENV_OVERRIDES)
    try:
        return await _run_benchmark_impl(mode, gateway_online)
    except EvaluationTimeout as exc:
        print(f">> EVALUATION FAILED CLOSED: {exc}")
        print("==================================================")
        return False
    finally:
        if mode == "smoke":
            for key, value in previous_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


async def _run_benchmark_impl(mode: str, gateway_online: bool) -> bool:
    """Implementation for :func:`run_benchmark` after mode isolation is active."""
    settings = get_settings()
    sample_timeout = (
        settings.eval_release_sample_timeout_seconds
        if mode == "release"
        else settings.eval_smoke_sample_timeout_seconds
    )

    print("==================================================")
    print("      RUNNING AGENTIC BENCHMARK EVALUATIONS       ")
    print(f"      MODE: {mode.upper()}                                ")
    print("==================================================")

    if mode == "release":
        blockers = release_blockers()
        if blockers:
            print(">> RELEASE GATE BLOCKED — missing release-quality prerequisites:")
            for blocker in blockers:
                print(f"   - {blocker}")
            print(">> RELEASE GATE FAILED (no production-readiness claim is made).")
            print("==================================================")
            return False
        thresholds = _load_release_thresholds()
        print(
            ">> RELEASE MODE: official thresholds are enforced unchanged. "
            "Low-scoring results are NOT silently rounded or downgraded."
        )
    else:
        if not gateway_online:
            print(">> [Notice] LLM Gateway offline. Smoke mode uses deterministic heuristics.")
        else:
            print(">> [Notice] Smoke mode remains deterministic even with a live gateway.")
        thresholds = dict(SMOKE_THRESHOLDS)
        print(
            f">> SMOKE MODE: {PROXY_BANNER}. "
            "These thresholds are NOT production thresholds."
        )

    dataset_dir = Path(__file__).parent / "datasets"
    golden_path = dataset_dir / "golden_v1.jsonl"
    rag_path = dataset_dir / "rag_qa.jsonl"

    if not golden_path.exists() or not rag_path.exists():
        from scripts.seed_eval import main as seed_main

        seed_main()

    policy_mgr = get_policy_manager()

    # 1. Routing, Risk & Tool Accuracy Benchmark
    routing_scores: list[float] = []
    tool_scores: list[float] = []
    risk_scores: list[float] = []
    degraded_planner_count = 0
    planner_mode = "llm" if mode == "release" else "heuristic_proxy"
    print(
        f"\n[Stage 1 & 2] Evaluating Planner Routing, Risk & Tool Selection "
        f"(planner_mode={planner_mode})..."
    )
    for item in await load_jsonl(golden_path):
        tc_id = item["id"]
        query = item["query"]
        expected_agent = item["expected_agent"]
        expected_risk = item.get("expected_risk")
        expected_tools = item.get("expected_tools", [])

        state = AgentState(messages=[HumanMessage(content=query)])
        res = await _bounded(
            planner_node(state),
            label=f"planner sample {tc_id}",
            timeout=sample_timeout,
        )
        if not isinstance(res, dict):
            raise TypeError(f"planner sample {tc_id} returned {type(res).__name__}, expected dict")
        if res.get("degraded"):
            degraded_planner_count += 1
        plan = res.get("plan")
        assigned_agent = plan.steps[0].agent if plan and plan.steps else "unknown"
        assigned_risk = plan.steps[0].risk_level if plan and plan.steps else "unknown"

        r_score = evaluate_routing_accuracy(assigned_agent, expected_agent)
        if mode == "release" and res.get("degraded"):
            r_score = 0.0
        routing_scores.append(r_score)
        if expected_risk is not None:
            risk_scores.append(1.0 if assigned_risk == expected_risk else 0.0)

        allowed_tools = policy_mgr.get_agent_tools(assigned_agent)
        if not expected_tools:
            t_score = 1.0
        else:
            matches = [t for t in expected_tools if t in allowed_tools]
            t_score = 1.0 if matches else 0.0
        tool_scores.append(t_score)

        risk_ok = expected_risk is None or assigned_risk == expected_risk
        status = "PASS" if r_score == 1.0 and t_score == 1.0 and risk_ok else "WARN"
        print(
            f"  {tc_id}: [{status}] ({planner_mode}) Agent: expected '{expected_agent}', got '{assigned_agent}' | Risk: '{assigned_risk}' (exp '{expected_risk}') | Tools valid: {t_score == 1.0}"
        )

    avg_routing = sum(routing_scores) / max(1, len(routing_scores))
    avg_tool = sum(tool_scores) / max(1, len(tool_scores))
    avg_risk = sum(risk_scores) / max(1, len(risk_scores)) if risk_scores else 1.0
    print(f"\n>> Planner Mode: {planner_mode} (degraded_count={degraded_planner_count})")
    print(f">> Routing Accuracy Score ({planner_mode}): {avg_routing * 100:.1f}%")
    print(f">> Risk Classification Score: {avg_risk * 100:.1f}%")
    print(f">> Tool Selection Validity: {avg_tool * 100:.1f}%")

    # 2. RAG Groundedness & Faithfulness Benchmark — score the GENERATED answer.
    groundedness_scores: list[float] = []
    faithfulness_scores: list[float] = []
    proxy_count = 0
    print("\n[Stage 3] Evaluating RAG Groundedness & Faithfulness (generated answers)...")
    for item in await load_jsonl(rag_path):
        tc_id = item["id"]
        q = item["question"]
        gt = item["ground_truth"]

        retrieved_context = await _bounded(
            asearch_policy(
                q,
                force_heuristic_reranker=(mode == "smoke"),
            ),
            label=f"retrieval sample {tc_id}",
            timeout=sample_timeout,
        )
        if not isinstance(retrieved_context, str):
            raise TypeError(
                f"retrieval sample {tc_id} returned {type(retrieved_context).__name__}, expected str"
            )
        answer = await _bounded(
            generate_answer(q, retrieved_context),
            label=f"answer generation sample {tc_id}",
            timeout=sample_timeout,
        )
        if answer.is_proxy:
            proxy_count += 1

        # Groundedness: is the generated answer faithful to the reference?
        g_score = await _bounded(
            evaluate_groundedness(
                question=q,
                generated_answer=answer.answer,
                ground_truth=gt,
                mode=mode,
            ),
            label=f"groundedness sample {tc_id}",
            timeout=sample_timeout,
        )
        # Faithfulness: release mode uses the RAGAS faithfulness evaluator; smoke uses token overlap.
        if mode == "release":
            f_score = await _bounded(
                evaluate_ragas_faithfulness(
                    question=q,
                    contexts=retrieved_context,
                    answer=answer.answer,
                ),
                label=f"faithfulness sample {tc_id}",
                timeout=sample_timeout,
            )
        else:
            f_score = evaluate_faithfulness(
                generated_answer=answer.answer, retrieved_context=retrieved_context
            )
        groundedness_scores.append(g_score)
        faithfulness_scores.append(f_score)
        status = (
            "PASS"
            if g_score >= thresholds["groundedness"]
            and f_score >= thresholds["faithfulness"]
            else "WARN"
        )
        proxy_tag = " [PROXY]" if answer.is_proxy else ""
        print(
            f"  {tc_id}: [{status}]{proxy_tag} Groundedness: {g_score:.2f} | Faithfulness: {f_score:.2f}"
        )

    avg_groundedness = sum(groundedness_scores) / max(1, len(groundedness_scores))
    avg_faithfulness = sum(faithfulness_scores) / max(1, len(faithfulness_scores))
    print(f"\n>> Average Groundedness Score: {avg_groundedness:.2f}")
    print(f"\n>> Average Faithfulness Score: {avg_faithfulness:.2f}")

    if proxy_count:
        print(f">> WARNING: {proxy_count} answer(s) were {PROXY_BANNER}.")

    passed_all = (
        avg_routing >= thresholds["routing_accuracy"]
        and avg_tool >= thresholds["tool_selection"]
        and avg_groundedness >= thresholds["groundedness"]
        and avg_faithfulness >= thresholds["faithfulness"]
    )

    print("\n==================================================")
    if mode == "release" and proxy_count:
        print(">> RELEASE GATE FAILED: proxy answers are not release-quality measurements.")
        print("==================================================")
        return False
    if mode == "release" and degraded_planner_count:
        print(
            f">> RELEASE GATE FAILED: planner fell back to degraded heuristic mode on {degraded_planner_count} query(ies)."
        )
        print("==================================================")
        return False
    if mode == "release" and not llm_judge_available():
        print(">> RELEASE GATE FAILED: no live LLM judge available.")
        print("==================================================")
        return False

    if passed_all:
        if mode == "release":
            print(">> BENCHMARK EVALUATION PASSED ALL 4 QUALITY GATES! (Ready for Production)")
        else:
            print(">> SMOKE CHECKS PASSED (NOT a release gate; no production-readiness claim).")
            print(f">> {PROXY_BANNER} where proxies were used.")
        print("==================================================")
        return True

    print(">> EVALUATION FAILED GATING THRESHOLDS.")
    print(
        f"   Required: Routing >= {thresholds['routing_accuracy']}, Tool >= {thresholds['tool_selection']}, "
        f"Groundedness >= {thresholds['groundedness']}, Faithfulness >= {thresholds['faithfulness']}"
    )
    print("==================================================")
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Run agentic quality evaluation.")
    parser.add_argument(
        "--mode",
        choices=["smoke", "release"],
        default=None,
        help="smoke (CI default) or release (strict production gate).",
    )
    args = parser.parse_args()
    success = asyncio.run(run_benchmark(args.mode))
    if not success:
        sys.exit(1)


if __name__ == "__main__":
    main()
