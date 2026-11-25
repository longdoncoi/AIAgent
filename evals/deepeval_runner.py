"""DeepEval evaluation runner with an honest split between smoke and release modes.

* ``smoke`` (default): uses ``LocalJudge`` heuristic approximations labeled
  ``heuristic proxy — not a release-quality measurement``.
* ``release``: uses the official ``deepeval`` metrics (``FaithfulnessMetric``,
  ``AnswerRelevancyMetric``, ``HallucinationMetric``) backed by the live
  LiteLLM judge gateway; refuses to fall back to heuristics.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import logging
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml
from pydantic import BaseModel, Field

from evals.answer_generator import (
    PROXY_BANNER,
    generate_answer,
    is_empty_retrieval,
    llm_judge_available,
    release_blockers,
)
from evals.dataset_io import load_jsonl
from evals.ragas_runner import (
    _is_negative_ground_truth,
    _looks_like_refusal,
    _parse_retrieved_contexts,
)
from evals.run_eval import RELEASE_DEFAULT_THRESHOLDS, resolve_mode
from src.agentic.core.config import get_settings

logger = logging.getLogger(__name__)

DEEPEVAL_INSTALLED = importlib.util.find_spec("deepeval") is not None

# Smoke-only thresholds — never used by the release gate.
SMOKE_THRESHOLDS = {
    "routing_accuracy": 0.80,
    "tool_selection": 0.80,
    "faithfulness": 0.50,
    "answer_relevancy": 0.50,
    "max_hallucination_ratio": 0.50,
}


# ------------------------------------------------------------------------------
# 1. Structured metric output schemas
# ------------------------------------------------------------------------------
class FaithfulnessJudgeOutput(BaseModel):
    faithfulness_score: float = Field(
        ge=0.0, le=1.0, description="Ratio of claims supported by context"
    )
    unsupported_claims: list[str] = Field(
        default_factory=list, description="Extracted claims not found in context"
    )
    reasoning: str = Field(description="Explanation of factual fidelity")


class RelevancyJudgeOutput(BaseModel):
    relevancy_score: float = Field(ge=0.0, le=1.0, description="Degree of relevance to the query")
    reasoning: str = Field(description="Explanation of how directly the answer addresses query")


class HallucinationJudgeOutput(BaseModel):
    hallucination_score: float = Field(
        ge=0.0, le=1.0, description="Ratio of fabricated or conflicting facts"
    )
    hallucination_detected: bool = Field(description="True if unsupported claims are found")
    reasoning: str = Field(description="Explanation of hallucination detection")


# ------------------------------------------------------------------------------
# 2. Local heuristic judge (LLM-assisted when reachable, fallback otherwise)
# ------------------------------------------------------------------------------
class LocalJudge:
    """Heuristic local judge with optional LLM assistance (NOT the DeepEval library)."""

    def __init__(self, model_alias: str = "reasoning-llm") -> None:
        self.settings = get_settings()
        self.model_alias = model_alias

    def _is_online(self) -> bool:
        """Fast probe to check if live LLM judging is enabled and LiteLLM Gateway is running."""
        return get_settings().environment != "test" and llm_judge_available()

    async def evaluate_faithfulness(self, query: str, context: str, answer: str) -> float:
        """Measures whether the answer is derived strictly from the context."""
        if not answer.strip() or not context.strip():
            return 0.0
        if is_empty_retrieval(context):
            return 1.0 if _looks_like_refusal(answer) else 0.0

        if self._is_online():
            try:
                from src.agentic.llm.gateway import get_llm

                judge_timeout = 1.0 if self.settings.environment == "test" else 120.0
                llm = get_llm(
                    alias=self.model_alias, temperature=0.0, max_tokens=512, timeout=judge_timeout
                )
                structured_llm = llm.with_structured_output(FaithfulnessJudgeOutput)
                prompt = (
                    "You are an impartial judge assessing FAITHFULNESS.\n"
                    f"Context: {context}\n"
                    f"Answer: {answer}\n"
                    "Evaluate whether each claim in the Answer is directly supported by the Context."
                )
                res = await structured_llm.ainvoke(prompt)
                if isinstance(res, FaithfulnessJudgeOutput):
                    return round(res.faithfulness_score, 3)
            except Exception as e:
                logger.debug(f"Judge LLM offline/timeout ({e}). Using token overlap fallback.")

        ans_tokens = [w.lower() for w in answer.split() if len(w) > 3]
        if not ans_tokens:
            return 1.0
        ctx_lower = context.lower()
        supported = sum(1 for token in ans_tokens if token in ctx_lower)
        return round(float(supported / len(ans_tokens)), 3)

    async def evaluate_relevancy(self, query: str, answer: str) -> float:
        """Measures how pertinent the answer is to the original user query."""
        if not query.strip() or not answer.strip():
            return 0.0

        if self._is_online():
            try:
                from src.agentic.llm.gateway import get_llm

                judge_timeout = 1.0 if self.settings.environment == "test" else 120.0
                llm = get_llm(
                    alias=self.model_alias, temperature=0.0, max_tokens=512, timeout=judge_timeout
                )
                structured_llm = llm.with_structured_output(RelevancyJudgeOutput)
                prompt = (
                    "You are an impartial judge assessing ANSWER RELEVANCY.\n"
                    f"User Query: {query}\n"
                    f"Answer: {answer}\n"
                    "Rate how concisely and completely the answer addresses the question."
                )
                res = await structured_llm.ainvoke(prompt)
                if isinstance(res, RelevancyJudgeOutput):
                    return round(res.relevancy_score, 3)
            except Exception:
                pass

        q_tokens = {w.lower() for w in query.split() if len(w) > 3}
        a_tokens = {w.lower() for w in answer.split() if len(w) > 3}
        if not q_tokens:
            return 1.0
        overlap = len(q_tokens.intersection(a_tokens)) / len(q_tokens)
        return round(min(1.0, max(0.5, overlap + 0.3)), 3)

    async def evaluate_hallucination(self, context: str, answer: str) -> float:
        """Measures hallucination ratio (0.0 means 0% hallucination)."""
        faithfulness = await self.evaluate_faithfulness("", context, answer)
        return round(max(0.0, 1.0 - faithfulness), 3)


# ------------------------------------------------------------------------------
# 2b. Official DeepEval LLM bridge & release runner (release mode only)
# ------------------------------------------------------------------------------
async def _run_real_deepeval(
    samples: list[dict[str, str]],
    answers: dict[str, str],
    contexts_by_id: dict[str, list[str]],
    thresholds: dict[str, float],
    model_alias: str,
) -> dict[str, float]:
    """Execute official DeepEval Faithfulness, AnswerRelevancy, and Hallucination metrics."""
    import math

    from deepeval.metrics import (
        AnswerRelevancyMetric,
        FaithfulnessMetric,
        HallucinationMetric,
    )
    from deepeval.models import DeepEvalBaseLLM
    from deepeval.test_case import LLMTestCase

    from src.agentic.llm.gateway import get_llm

    # Avoid subclassing DeepEvalBaseLLM directly under TYPE_CHECKING: CI runs mypy with
    # --extra dev only (resolving it to Any -> [misc]), whereas with deepeval installed
    # its untyped __init_subclass__ triggers [no-untyped-call].
    if TYPE_CHECKING:

        class _GatewayLLMBase:
            model: Any

            def __init__(self, model: str | None = None, *args: object, **kwargs: object) -> None: ...
    else:
        _GatewayLLMBase = DeepEvalBaseLLM

    class _GatewayDeepEvalLLM(_GatewayLLMBase):
        def __init__(self, alias: str) -> None:
            self._alias = alias
            super().__init__(model=alias)

        def load_model(self, *args: object, **kwargs: object) -> Any:
            return get_llm(alias=self._alias, temperature=0.0, max_tokens=1024, timeout=120.0)

        def get_model_name(self) -> str:
            return self._alias

        def generate(
            self,
            prompt: str,
            schema: object | None = None,
            **kwargs: object,
        ) -> Any:
            model = cast(Any, self.model)
            if schema is not None:
                structured = model.with_structured_output(schema)
                return structured.invoke(prompt)
            msg = model.invoke(prompt)
            return str(getattr(msg, "content", msg))

        async def a_generate(
            self,
            prompt: str,
            schema: object | None = None,
            **kwargs: object,
        ) -> Any:
            model = cast(Any, self.model)
            if schema is not None:
                structured = model.with_structured_output(schema)
                return await structured.ainvoke(prompt)
            msg = await model.ainvoke(prompt)
            return str(getattr(msg, "content", msg))

    judge_llm = cast(DeepEvalBaseLLM, _GatewayDeepEvalLLM(model_alias))
    faith_metric = FaithfulnessMetric(
        threshold=thresholds["faithfulness"],
        model=judge_llm,
        async_mode=True,
        include_reason=False,
    )
    rel_metric = AnswerRelevancyMetric(
        threshold=thresholds["answer_relevancy"],
        model=judge_llm,
        async_mode=True,
        include_reason=False,
    )
    hall_metric = HallucinationMetric(
        threshold=max(0.0, 1.0 - thresholds["max_hallucination_ratio"]),
        model=judge_llm,
        async_mode=True,
        include_reason=False,
    )

    faith_vals: list[float] = []
    rel_vals: list[float] = []
    hall_vals: list[float] = []
    abstention_scores: list[float] = []

    for item in samples:
        tc_id = item["id"]
        q = item["question"]
        gt = item["ground_truth"]
        ans = answers[tc_id]
        ctxs = contexts_by_id[tc_id]

        if _is_negative_ground_truth(gt):
            abstained = is_empty_retrieval(ctxs) and _looks_like_refusal(ans)
            abstention_scores.append(1.0 if abstained else 0.0)
            print(
                f"  [Official DeepEval] {tc_id} (out-of-scope abstention): "
                f"empty_retrieval={is_empty_retrieval(ctxs)} refusal={_looks_like_refusal(ans)}",
                flush=True,
            )
            continue

        faith_tc = LLMTestCase(
            input=q,
            actual_output=ans,
            retrieval_context=cast(list[Any], ctxs),
        )
        rel_tc = LLMTestCase(input=q, actual_output=ans)
        hall_tc = LLMTestCase(input=q, actual_output=ans, context=ctxs)

        await faith_metric.a_measure(faith_tc)
        await rel_metric.a_measure(rel_tc)
        await hall_metric.a_measure(hall_tc)

        f_val = float(faith_metric.score if faith_metric.score is not None else math.nan)
        r_val = float(rel_metric.score if rel_metric.score is not None else math.nan)
        # DeepEval 4.x flipped HallucinationMetric polarity: score=1.0 means 0% hallucination.
        h_pass_score = float(hall_metric.score if hall_metric.score is not None else math.nan)
        h_val = round(max(0.0, 1.0 - h_pass_score), 4) if not math.isnan(h_pass_score) else math.nan
        if any(math.isnan(v) for v in (f_val, r_val, h_val)):
            raise RuntimeError(
                f"Official deepeval returned NaN for {tc_id}: ({f_val}, {r_val}, {h_val})"
            )

        faith_vals.append(f_val)
        rel_vals.append(r_val)
        hall_vals.append(h_val)
        print(
            f"  [Official DeepEval] {tc_id}: Faith={f_val:.3f} | "
            f"Relev={r_val:.3f} | Halluc={h_val:.3f}",
            flush=True,
        )

    if not faith_vals:
        raise RuntimeError("No answerable samples available for official DeepEval evaluation.")

    n = len(faith_vals)
    abstention_acc = (
        sum(abstention_scores) / len(abstention_scores) if abstention_scores else 1.0
    )
    return {
        "faithfulness": sum(faith_vals) / n,
        "answer_relevancy": sum(rel_vals) / n,
        "max_hallucination_ratio": sum(hall_vals) / n,
        "abstention_accuracy": abstention_acc,
    }


# ------------------------------------------------------------------------------
# 3. Benchmark runner (smoke vs release)
# ------------------------------------------------------------------------------
def _load_release_thresholds() -> dict[str, float]:
    settings = get_settings()
    thresholds = dict(RELEASE_DEFAULT_THRESHOLDS)
    thresholds.update(
        {
            "answer_relevancy": 0.80,
            "max_hallucination_ratio": 0.05,
        }
    )
    thresholds_file = settings.project_root / "configs" / "eval_thresholds.yaml"
    if thresholds_file.exists():
        with open(thresholds_file, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        quality_gates = cfg.get("quality_gates") or {}
        for key in thresholds:
            if key in quality_gates:
                thresholds[key] = float(quality_gates[key])
    return thresholds


async def run_deepeval_benchmark(mode: str | None = None) -> bool:
    """Execute the DeepEval-style benchmark in smoke or release mode."""
    mode = resolve_mode(mode)
    settings = get_settings()

    print("==================================================================")
    banner_title = (
        "OFFICIAL DEEPEVAL EVALUATION SUITE"
        if mode == "release"
        else "DEEPEVAL-STYLE HEURISTIC SMOKE JUDGE"
    )
    print(f"      {banner_title:<58}")
    print(f"      MODE: {mode.upper()}                                        ")
    print("      DeepEval library installed: " + ("YES" if DEEPEVAL_INSTALLED else "NO"))
    print("==================================================================")

    if mode == "release":
        blockers = release_blockers()
        if not DEEPEVAL_INSTALLED:
            blockers.append("deepeval library not installed — cannot produce official DeepEval metrics")
        if blockers:
            print(">> RELEASE GATE BLOCKED:")
            for blocker in blockers:
                print(f"   - {blocker}")
            print(">> RELEASE GATE FAILED (no production-readiness claim is made).")
            print("==================================================================")
            return False
        thresholds = _load_release_thresholds()
    else:
        from evals.answer_generator import gateway_reachable

        if not gateway_reachable():
            print(">> [Notice] LLM Gateway offline. Smoke mode uses deterministic heuristics.")
            os.environ["ENVIRONMENT"] = "test"
            os.environ["FAST_HEURISTIC_PLANNER"] = "true"
            os.environ["SKIP_LLM_JUDGE"] = "true"
        thresholds = dict(SMOKE_THRESHOLDS)
        print(f">> {PROXY_BANNER}; no production-readiness claim is made.")

    from langchain_core.messages import HumanMessage

    from src.agentic.orchestrator.nodes.planner import planner_node
    from src.agentic.orchestrator.state import AgentState
    from src.agentic.rag.tools import search_policy
    from src.agentic.safety.policy import get_policy_manager

    judge = LocalJudge(model_alias=settings.eval_judge_model)
    datasets_dir = settings.project_root / "evals" / "datasets"
    golden_path = datasets_dir / "golden_v1.jsonl"
    rag_path = datasets_dir / "rag_qa.jsonl"

    policy_mgr = get_policy_manager()
    routing_scores: list[float] = []
    tool_scores: list[float] = []

    print("\n[Metric 1 & 2] Evaluating Routing & Tool Correctness...")
    for item in await load_jsonl(golden_path):
        q = item["query"]
        exp_agent = item["expected_agent"]
        exp_tools = item.get("expected_tools", [])

        state = AgentState(messages=[HumanMessage(content=q)])
        plan_res = await planner_node(state)
        plan = plan_res.get("plan")
        actual_agent = plan.steps[0].agent if plan and plan.steps else "unknown"

        r_score = (
            1.0
            if actual_agent.lower().replace("_agent", "")
            == exp_agent.lower().replace("_agent", "")
            else 0.0
        )
        routing_scores.append(r_score)

        allowed = policy_mgr.get_agent_tools(actual_agent)
        t_score = 1.0 if not exp_tools or any(t in allowed for t in exp_tools) else 0.0
        tool_scores.append(t_score)

    avg_routing = sum(routing_scores) / max(1, len(routing_scores))
    avg_tool = sum(tool_scores) / max(1, len(tool_scores))
    print(f"  >> Avg Routing Accuracy:  {avg_routing * 100:.1f}%")
    print(f"  >> Avg Tool Correctness:   {avg_tool * 100:.1f}%")

    faith_scores: list[float] = []
    relevancy_scores: list[float] = []
    hallucination_scores: list[float] = []
    rag_samples: list[dict[str, str]] = []
    answers: dict[str, str] = {}
    contexts_by_id: dict[str, list[str]] = {}
    proxy_count = 0

    print("\n[Metric 3, 4 & 5] Evaluating generated RAG answers...")
    for item in await load_jsonl(rag_path):
        tc_id = item["id"]
        q = item["question"]

        retrieved = search_policy(q)
        contexts = _parse_retrieved_contexts(retrieved) or [retrieved]
        generated = await generate_answer(q, retrieved)
        if generated.is_proxy:
            proxy_count += 1

        rag_samples.append(item)
        answers[tc_id] = generated.answer
        contexts_by_id[tc_id] = contexts

        if mode == "smoke":
            f_score = await judge.evaluate_faithfulness(q, retrieved, generated.answer)
            r_score = await judge.evaluate_relevancy(q, generated.answer)
            h_score = await judge.evaluate_hallucination(retrieved, generated.answer)

            faith_scores.append(f_score)
            relevancy_scores.append(r_score)
            hallucination_scores.append(h_score)

    abstention_acc: float | None = None
    if mode == "release":
        try:
            official = await _run_real_deepeval(
                rag_samples,
                answers,
                contexts_by_id,
                thresholds,
                model_alias=settings.eval_judge_model,
            )
        except Exception as exc:
            print(f">> RELEASE GATE FAILED: official DeepEval evaluation failed: {exc}")
            print("==================================================================")
            return False
        avg_faith = official["faithfulness"]
        avg_relevancy = official["answer_relevancy"]
        avg_hallucination = official["max_hallucination_ratio"]
        abstention_acc = official.get("abstention_accuracy", 1.0)
        print(">> Official deepeval library metrics (release-quality):")
    else:
        avg_faith = sum(faith_scores) / max(1, len(faith_scores))
        avg_relevancy = sum(relevancy_scores) / max(1, len(relevancy_scores))
        avg_hallucination = sum(hallucination_scores) / max(1, len(hallucination_scores))

    print(f"  >> Avg Faithfulness:       {avg_faith:.2f}")
    print(f"  >> Avg Answer Relevancy:   {avg_relevancy:.2f}")
    print(f"  >> Avg Hallucination Rate: {avg_hallucination:.2f}")
    if abstention_acc is not None:
        print(f"  >> Out-of-Scope Abstention:{abstention_acc * 100:.1f}% (Gate: 100.0%)")
    if proxy_count:
        print(f">> WARNING: {proxy_count} answer(s) were {PROXY_BANNER}.")

    passed = (
        avg_routing >= thresholds["routing_accuracy"]
        and avg_tool >= thresholds["tool_selection"]
        and avg_faith >= thresholds["faithfulness"]
        and avg_relevancy >= thresholds["answer_relevancy"]
        and avg_hallucination <= thresholds["max_hallucination_ratio"]
        and (abstention_acc is None or abstention_acc >= 1.0)
    )

    print("==================================================================")
    if mode == "release" and (proxy_count or not llm_judge_available()):
        print(">> RELEASE GATE FAILED: proxy answers or missing LLM judge.")
    elif passed:
        print(
            ">> BENCHMARK PASSED! (Ready for Production)"
            if mode == "release"
            else f">> SMOKE CHECKS PASSED ({PROXY_BANNER}; no production-readiness claim)."
        )
    else:
        print(">> EVALUATION FAILED GATING THRESHOLDS.")
    print("==================================================================")
    return passed and (mode == "smoke" or not (proxy_count or not llm_judge_available()))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run DeepEval-style heuristic evaluation.")
    parser.add_argument("--mode", choices=["smoke", "release"], default=None)
    success = asyncio.run(run_deepeval_benchmark(parser.parse_args().mode))
    sys.exit(0 if success else 1)
