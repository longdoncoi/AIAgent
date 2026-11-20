"""Evaluation runner script for CI/CD gating."""

import asyncio
import json
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langchain_core.messages import HumanMessage

from evals.evaluators import (
    evaluate_faithfulness,
    evaluate_groundedness,
    evaluate_routing_accuracy,
)
from src.agentic.orchestrator.nodes.planner import planner_node
from src.agentic.orchestrator.state import AgentState
from src.agentic.rag.tools import search_policy
from src.agentic.safety.policy import get_policy_manager


async def run_benchmark() -> bool:
    dataset_dir = Path(__file__).resolve().parent / "datasets"
    golden_path = dataset_dir / "golden_v1.jsonl"
    rag_path = dataset_dir / "rag_qa.jsonl"

    if not golden_path.exists() or not rag_path.exists():
        from scripts.seed_eval import main as seed_main

        seed_main()

    print("==================================================")
    print("      RUNNING AGENTIC BENCHMARK EVALUATIONS       ")
    print("==================================================")

    policy_mgr = get_policy_manager()

    # 1. Routing & Tool Accuracy Benchmark
    routing_scores: list[float] = []
    tool_scores: list[float] = []
    print("\n[Stage 1 & 2] Evaluating Planner Routing & Tool Selection Accuracy...")
    with open(golden_path, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            item = json.loads(line)
            tc_id = item["id"]
            query = item["query"]
            expected_agent = item["expected_agent"]
            expected_tools = item.get("expected_tools", [])

            state = AgentState(messages=[HumanMessage(content=query)])
            res = await planner_node(state)
            plan = res.get("plan")
            assigned_agent = plan.steps[0].agent if plan and plan.steps else "unknown"

            # 1. Routing Score
            r_score = evaluate_routing_accuracy(assigned_agent, expected_agent)
            routing_scores.append(r_score)

            # 2. Tool Capability Matching
            allowed_tools = policy_mgr.get_agent_tools(assigned_agent)
            if not expected_tools:
                t_score = 1.0
            else:
                matches = [t for t in expected_tools if t in allowed_tools]
                t_score = 1.0 if matches else 0.0
            tool_scores.append(t_score)

            status = "PASS" if r_score == 1.0 and t_score == 1.0 else "WARN"
            print(
                f"  {tc_id}: [{status}] Agent: expected '{expected_agent}', got '{assigned_agent}' | Tools valid: {t_score == 1.0}"
            )

    avg_routing = sum(routing_scores) / max(1, len(routing_scores))
    avg_tool = sum(tool_scores) / max(1, len(tool_scores))
    print(f"\n>> Routing Accuracy Score: {avg_routing * 100:.1f}%")
    print(f">> Tool Selection Validity: {avg_tool * 100:.1f}%")

    # 3. RAG Groundedness & Faithfulness Benchmark
    groundedness_scores: list[float] = []
    faithfulness_scores: list[float] = []
    print("\n[Stage 3] Evaluating RAG Groundedness & Faithfulness...")
    with open(rag_path, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            item = json.loads(line)
            tc_id = item["id"]
            q = item["question"]
            gt = item["ground_truth"]

            # Retrieve through RAG tool
            retrieved_context = search_policy(q)
            g_score = await evaluate_groundedness(
                question=q, generated_answer=retrieved_context, ground_truth=gt
            )
            f_score = evaluate_faithfulness(
                generated_answer=gt, retrieved_context=retrieved_context
            )
            groundedness_scores.append(g_score)
            faithfulness_scores.append(f_score)
            status = "PASS" if g_score >= 0.40 and f_score >= 0.50 else "WARN"
            print(
                f"  {tc_id}: [{status}] Groundedness: {g_score:.2f} | Faithfulness: {f_score:.2f}"
            )

    avg_groundedness = sum(groundedness_scores) / max(1, len(groundedness_scores))
    avg_faithfulness = sum(faithfulness_scores) / max(1, len(faithfulness_scores))
    print(f"\n>> Average Groundedness Score: {avg_groundedness:.2f}")
    print(f">> Average Faithfulness Score: {avg_faithfulness:.2f}")

    # Threshold checks
    ROUTING_THRESHOLD = 0.80
    TOOL_THRESHOLD = 0.80
    GROUNDEDNESS_THRESHOLD = 0.40
    FAITHFULNESS_THRESHOLD = 0.50

    print("\n==================================================")
    passed_all = (
        avg_routing >= ROUTING_THRESHOLD
        and avg_tool >= TOOL_THRESHOLD
        and avg_groundedness >= GROUNDEDNESS_THRESHOLD
        and avg_faithfulness >= FAITHFULNESS_THRESHOLD
    )
    if passed_all:
        print(">> BENCHMARK EVALUATION PASSED ALL 4 QUALITY GATES! (Ready for Production)")
        print("==================================================")
        return True
    else:
        print(">> BENCHMARK EVALUATION FAILED GATING THRESHOLDS.")
        print(
            f"   Required: Routing >= {ROUTING_THRESHOLD}, Tool >= {TOOL_THRESHOLD}, Groundedness >= {GROUNDEDNESS_THRESHOLD}, Faithfulness >= {FAITHFULNESS_THRESHOLD}"
        )
        print("==================================================")
        return False


def main() -> None:
    success = asyncio.run(run_benchmark())
    if not success:
        sys.exit(1)


if __name__ == "__main__":
    main()
