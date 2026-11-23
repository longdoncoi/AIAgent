"""RAGAS evaluation suite with an honest split between smoke and release modes.

Evaluates RAG and Multi-Agent pipelines with canonical RAGAS metrics:
1. Faithfulness: factual consistency of the *generated response* against contexts.
2. Answer Relevance: how directly the generated answer addresses the question.
3. Context Precision: signal-to-noise ratio and ranking order of retrieved contexts.
4. Context Recall: coverage of ground-truth reference points across retrieved contexts.

* ``smoke`` (default): deterministic heuristic approximations, clearly labeled
  ``heuristic proxy — not a release-quality measurement``.
* ``release``: uses the real ``ragas`` library with an LLM judge and embeddings;
  fails if prerequisites are missing. No 15% slack, no string-based free passes.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import logging
import sys
from pathlib import Path
from typing import Any

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import yaml  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from evals.answer_generator import (  # noqa: E402
    PROXY_BANNER,
    generate_answer,
    is_empty_retrieval,
    llm_judge_available,
    release_blockers,
)
from evals.run_eval import resolve_mode  # noqa: E402
from src.agentic.core.config import get_settings  # noqa: E402
from src.agentic.observability.langsmith_tracer import (  # noqa: E402
    get_langsmith_client,
    is_langsmith_enabled,
    log_feedback,
    sync_evaluation_dataset,
)
from src.agentic.rag.tools import search_policy  # noqa: E402

logger = logging.getLogger(__name__)

RAGAS_INSTALLED = importlib.util.find_spec("ragas") is not None

# Smoke-only thresholds — never used by the release gate.
SMOKE_THRESHOLDS = {
    "faithfulness": 0.40,
    "answer_relevance": 0.50,
    "context_precision": 0.40,
    "context_recall": 0.40,
}

NEGATIVE_GT_MARKERS = (
    "không có thông tin",
    "không tìm thấy",
    "no relevant",
    "chưa có quy định",
)
REFUSAL_MARKERS = NEGATIVE_GT_MARKERS + ("không có", "no information", "không rõ")


def _is_negative_ground_truth(ground_truth: str) -> bool:
    lowered = ground_truth.lower()
    return any(marker in lowered for marker in NEGATIVE_GT_MARKERS)


def _looks_like_refusal(answer: str) -> bool:
    lowered = answer.lower()
    return any(marker in lowered for marker in REFUSAL_MARKERS)


# ------------------------------------------------------------------------------
# 1. Structured schemas for RAGAS judges
# ------------------------------------------------------------------------------
class RagasFaithfulnessOutput(BaseModel):
    faithfulness: float = Field(ge=0.0, le=1.0, description="Ratio of claims supported by context")
    unsupported_claims: list[str] = Field(
        default_factory=list, description="Extracted claims not found in context"
    )
    reasoning: str = Field(description="Explanation of factual fidelity")


class RagasAnswerRelevanceOutput(BaseModel):
    answer_relevance: float = Field(
        ge=0.0, le=1.0, description="Relevance score between 0.0 and 1.0"
    )
    reasoning: str = Field(description="Explanation of relevance to the original prompt")


class RagasContextPrecisionOutput(BaseModel):
    context_precision: float = Field(ge=0.0, le=1.0, description="Relevance of retrieved chunks")
    relevant_chunks: list[int] = Field(
        default_factory=list, description="0-indexed positions of relevant chunks"
    )
    reasoning: str = Field(description="Explanation of ranking precision")


class RagasContextRecallOutput(BaseModel):
    context_recall: float = Field(
        ge=0.0, le=1.0, description="Ratio of ground-truth statements found in contexts"
    )
    missing_points: list[str] = Field(
        default_factory=list, description="Ground truth statements missing from contexts"
    )
    reasoning: str = Field(description="Explanation of recall coverage")


class RagasSampleResult(BaseModel):
    id: str
    question: str
    faithfulness: float
    answer_relevance: float
    context_precision: float
    context_recall: float
    passed: bool


# ------------------------------------------------------------------------------
# 2. Local heuristic RAGAS evaluator (smoke only)
# ------------------------------------------------------------------------------
def _tokenize(text: str) -> list[str]:
    """Extracts alphanumeric tokens with punctuation stripped, filtering out single characters."""
    import re

    return [w.lower() for w in re.findall(r"\w+", text) if len(w) >= 2]


class RagasLocalEvaluator:
    """Heuristic RAGAS-style evaluator (smoke only; NOT official RAGAS metrics)."""

    def __init__(self, model_alias: str = "reasoning-llm") -> None:
        self.settings = get_settings()
        self.model_alias = model_alias

    def _is_llm_online(self) -> bool:
        from evals.answer_generator import gateway_reachable

        return gateway_reachable()

    async def evaluate_faithfulness(self, question: str, contexts: list[str], answer: str) -> float:
        """Computes RAGAS faithfulness: fraction of claims in answer supported by contexts."""
        if not answer.strip() or not contexts:
            return 0.0

        joined_ctx = "\n---\n".join(contexts)

        if self._is_llm_online():
            try:
                from src.agentic.llm.gateway import get_llm

                llm = get_llm(alias=self.model_alias, temperature=0.0, timeout=15.0)
                structured = llm.with_structured_output(RagasFaithfulnessOutput)
                prompt = (
                    "You are a strict evaluator measuring FAITHFULNESS.\n"
                    f"Contexts:\n{joined_ctx}\n\n"
                    f"Answer:\n{answer}\n\n"
                    "Evaluate whether every factual claim in the answer is directly supported by the contexts."
                )
                res = await structured.ainvoke(prompt)
                if isinstance(res, RagasFaithfulnessOutput):
                    return round(res.faithfulness, 3)
            except Exception as e:
                logger.debug(f"Faithfulness LLM evaluation timed out or failed: {e}")

        # Refusal credit is ONLY given when retrieval is genuinely empty.
        if is_empty_retrieval(contexts):
            return 1.0 if _looks_like_refusal(answer) else 0.0

        ans_tokens = _tokenize(answer)
        if not ans_tokens:
            return 1.0
        ctx_lower = joined_ctx.lower()
        supported = sum(1 for token in ans_tokens if token in ctx_lower)
        return round(float(supported / len(ans_tokens)), 3)

    async def evaluate_answer_relevance(self, question: str, answer: str) -> float:
        """Computes RAGAS answer relevance: degree to which response directly answers question."""
        if not question.strip() or not answer.strip():
            return 0.0

        if self._is_llm_online():
            try:
                from src.agentic.llm.gateway import get_llm

                llm = get_llm(alias=self.model_alias, temperature=0.0, timeout=15.0)
                structured = llm.with_structured_output(RagasAnswerRelevanceOutput)
                prompt = (
                    "You are an impartial evaluator measuring ANSWER RELEVANCE.\n"
                    f"Question: {question}\n\n"
                    f"Answer: {answer}\n\n"
                    "Rate the relevance and completeness of the answer to the question (0.0 to 1.0)."
                )
                res = await structured.ainvoke(prompt)
                if isinstance(res, RagasAnswerRelevanceOutput):
                    return round(res.answer_relevance, 3)
            except Exception as e:
                logger.debug(f"Answer relevance LLM evaluation timed out or failed: {e}")

        q_tokens = set(_tokenize(question))
        a_tokens = set(_tokenize(answer))
        if not q_tokens:
            return 1.0
        overlap = len(q_tokens.intersection(a_tokens)) / len(q_tokens)
        return round(min(1.0, max(0.5, overlap + 0.35)), 3)

    async def evaluate_context_precision(
        self, question: str, contexts: list[str], ground_truth: str
    ) -> float:
        """Computes RAGAS context precision: whether relevant context chunks are ranked higher."""
        if not contexts:
            return 0.0

        if self._is_llm_online():
            try:
                from src.agentic.llm.gateway import get_llm

                llm = get_llm(alias=self.model_alias, temperature=0.0, timeout=15.0)
                structured = llm.with_structured_output(RagasContextPrecisionOutput)
                prompt = (
                    "You are a strict evaluator measuring CONTEXT PRECISION.\n"
                    f"Question: {question}\n\n"
                    f"Ground Truth Reference: {ground_truth}\n\n"
                    "Contexts:\n"
                    + "\n".join([f"[{i}] {ctx}" for i, ctx in enumerate(contexts)])
                    + "\n\nIdentify relevant chunks and assess ranking precision (0.0 to 1.0)."
                )
                res = await structured.ainvoke(prompt)
                if isinstance(res, RagasContextPrecisionOutput):
                    return round(res.context_precision, 3)
            except Exception as e:
                logger.debug(f"Context precision LLM evaluation timed out or failed: {e}")

        # Refusal credit ONLY when retrieval is actually empty AND ground truth
        # is a negative ("no information") case. No string-based free pass.
        if is_empty_retrieval(contexts) and _is_negative_ground_truth(ground_truth):
            return 1.0

        gt_tokens = set(_tokenize(ground_truth))
        if not gt_tokens:
            return 1.0

        relevant_chunks = 0
        for ctx in contexts:
            if len(gt_tokens.intersection(set(_tokenize(ctx)))) >= 1:
                relevant_chunks += 1

        if relevant_chunks == 0:
            return 0.0
        return round(float(relevant_chunks / len(contexts)), 3)

    async def evaluate_context_recall(
        self, question: str, contexts: list[str], ground_truth: str
    ) -> float:
        """Computes RAGAS context recall: whether contexts contain all information from ground truth."""
        if not ground_truth.strip() or not contexts:
            return 0.0

        joined_ctx = "\n---\n".join(contexts)

        if self._is_llm_online():
            try:
                from src.agentic.llm.gateway import get_llm

                llm = get_llm(alias=self.model_alias, temperature=0.0, timeout=15.0)
                structured = llm.with_structured_output(RagasContextRecallOutput)
                prompt = (
                    "You are a strict evaluator measuring CONTEXT RECALL.\n"
                    f"Question: {question}\n\n"
                    f"Ground Truth: {ground_truth}\n\n"
                    f"Contexts:\n{joined_ctx}\n\n"
                    "Evaluate what proportion of ground truth facts are recalled in the contexts (0.0 to 1.0)."
                )
                res = await structured.ainvoke(prompt)
                if isinstance(res, RagasContextRecallOutput):
                    return round(res.context_recall, 3)
            except Exception as e:
                logger.debug(f"Context recall LLM evaluation timed out or failed: {e}")

        if is_empty_retrieval(contexts) and _is_negative_ground_truth(ground_truth):
            return 1.0

        gt_tokens = set(_tokenize(ground_truth))
        if not gt_tokens:
            return 1.0
        ctx_lower = joined_ctx.lower()
        recalled = sum(1 for token in gt_tokens if token in ctx_lower)
        return round(min(1.0, float(recalled / len(gt_tokens))), 3)


# ------------------------------------------------------------------------------
# 3. Real RAGAS library path (release mode only)
# ------------------------------------------------------------------------------
def _run_real_ragas(
    samples: list[dict[str, Any]],
    answers: dict[str, str],
    contexts_by_id: dict[str, list[str]],
    settings: Any,
) -> dict[str, float]:
    """Run the official ``ragas`` library. Raises on any failure (no heuristic fallback)."""
    from langchain_openai import ChatOpenAI, OpenAIEmbeddings
    from ragas import EvaluationDataset
    from ragas import evaluate as ragas_evaluate
    from ragas.dataset_schema import SingleTurnSample
    from ragas.embeddings import LangchainEmbeddingsWrapper
    from ragas.llms import LangchainLLMWrapper
    from ragas.metrics.collections import (
        AnswerRelevancy,
        ContextPrecision,
        ContextRecall,
        Faithfulness,
    )

    llm = LangchainLLMWrapper(
        ChatOpenAI(
            model=settings.default_reasoning_model,
            base_url=settings.litellm_base_url,
            api_key=settings.litellm_api_key,
            temperature=0.0,
        )
    )
    embeddings = LangchainEmbeddingsWrapper(
        OpenAIEmbeddings(
            model=settings.default_embedding_model,
            base_url=settings.litellm_base_url,
            api_key=settings.litellm_api_key,
        )
    )
    rows = [
        SingleTurnSample(
            user_input=item["question"],
            retrieved_contexts=contexts_by_id[item["id"]],
            response=answers[item["id"]],
            reference=item["ground_truth"],
        )
        for item in samples
    ]
    dataset = EvaluationDataset(samples=rows)
    result = ragas_evaluate(
        dataset,
        metrics=[Faithfulness(), AnswerRelevancy(), ContextPrecision(), ContextRecall()],
        llm=llm,
        embeddings=embeddings,
        show_progress=False,
    )
    frame = result.to_pandas()
    return {
        "faithfulness": float(frame["faithfulness"].mean()),
        "answer_relevance": float(frame["answer_relevancy"].mean()),
        "context_precision": float(frame["context_precision"].mean()),
        "context_recall": float(frame["context_recall"].mean()),
    }


# ------------------------------------------------------------------------------
# 4. Benchmark suite runner with LangSmith sync
# ------------------------------------------------------------------------------
def _load_release_thresholds(settings: Any) -> dict[str, float]:
    thresholds = {
        "faithfulness": settings.ragas_faithfulness_threshold,
        "answer_relevance": settings.ragas_answer_relevance_threshold,
        "context_precision": settings.ragas_context_precision_threshold,
        "context_recall": settings.ragas_context_recall_threshold,
    }
    thresholds_file = settings.project_root / "configs" / "eval_thresholds.yaml"
    if thresholds_file.exists():
        with open(thresholds_file, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        ragas_gates = cfg.get("ragas_gates") or {}
        for key in thresholds:
            if key in ragas_gates:
                thresholds[key] = float(ragas_gates[key])
    return thresholds


async def run_ragas_benchmark(mode: str | None = None) -> bool:
    """Execute the RAGAS benchmark in smoke or release mode."""
    mode = resolve_mode(mode)
    settings = get_settings()

    print("==================================================================")
    print("        RAGAS EVALUATION SUITE (RAG QUALITY GATE)                  ")
    print(f"        MODE: {mode.upper()}                                       ")
    print("        ragas library installed: " + ("YES" if RAGAS_INSTALLED else "NO"))
    print("==================================================================")

    if mode == "release":
        blockers = release_blockers()
        if not RAGAS_INSTALLED:
            blockers.append("ragas library not installed — cannot run official RAGAS metrics")
        if blockers:
            print(">> RELEASE GATE BLOCKED:")
            for blocker in blockers:
                print(f"   - {blocker}")
            print(">> RELEASE GATE FAILED (no production-readiness claim is made).")
            print("==================================================================")
            return False
        thresholds = _load_release_thresholds(settings)
    else:
        thresholds = dict(SMOKE_THRESHOLDS)
        print(f">> {PROXY_BANNER}; no production-readiness claim is made.")

    dataset_path = settings.project_root / "evals" / "datasets" / "rag_qa.jsonl"
    if not dataset_path.exists():
        from scripts.seed_eval import main as seed_main

        seed_main()

    from src.agentic.cache.rag_cache import get_rag_cache

    get_rag_cache().clear()

    samples: list[dict[str, Any]] = []
    with open(dataset_path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                samples.append(json.loads(line))

    if is_langsmith_enabled():
        sync_evaluation_dataset(
            dataset_name="agentic-rag-benchmarks",
            examples=samples,
            description="Golden RAG Evaluation Benchmark Cases",
        )

    evaluator = RagasLocalEvaluator(model_alias=settings.eval_judge_model)
    answers: dict[str, str] = {}
    contexts_by_id: dict[str, list[str]] = {}
    proxy_count = 0
    faith_list: list[float] = []
    rel_list: list[float] = []
    prec_list: list[float] = []
    rec_list: list[float] = []

    for item in samples:
        tc_id = item["id"]
        q = item["question"]
        gt = item["ground_truth"]

        retrieved_raw = search_policy(q)
        contexts = [c.strip() for c in retrieved_raw.split("\n\n") if c.strip()]
        if not contexts:
            contexts = [retrieved_raw]
        contexts_by_id[tc_id] = contexts

        # Score the GENERATED answer, never the ground truth / context.
        generated = await generate_answer(q, retrieved_raw)
        answers[tc_id] = generated.answer
        if generated.is_proxy:
            proxy_count += 1

        faith = await evaluator.evaluate_faithfulness(question=q, contexts=contexts, answer=generated.answer)
        relev = await evaluator.evaluate_answer_relevance(question=q, answer=generated.answer)
        prec = await evaluator.evaluate_context_precision(
            question=q, contexts=contexts, ground_truth=gt
        )
        rec = await evaluator.evaluate_context_recall(
            question=q, contexts=contexts, ground_truth=gt
        )

        faith_list.append(faith)
        rel_list.append(relev)
        prec_list.append(prec)
        rec_list.append(rec)

        client = get_langsmith_client()
        if client and is_langsmith_enabled():
            log_feedback(run_id=tc_id, key="ragas_faithfulness", score=faith)
            log_feedback(run_id=tc_id, key="ragas_answer_relevance", score=relev)
            log_feedback(run_id=tc_id, key="ragas_context_precision", score=prec)
            log_feedback(run_id=tc_id, key="ragas_context_recall", score=rec)

        status_tag = "[PASS]" if all(
            [
                faith >= thresholds["faithfulness"],
                relev >= thresholds["answer_relevance"],
                prec >= thresholds["context_precision"],
                rec >= thresholds["context_recall"],
            ]
        ) else "[WARN]"
        proxy_tag = " [PROXY]" if generated.is_proxy else ""
        print(
            f"  {tc_id}: {status_tag}{proxy_tag} Faith: {faith:.2f} | Relev: {relev:.2f} | Prec: {prec:.2f} | Recall: {rec:.2f}"
        )

    if proxy_count:
        print(f">> WARNING: {proxy_count} answer(s) were {PROXY_BANNER}.")

    if mode == "release":
        # No 15% slack. Official library, official thresholds, no free passes.
        try:
            official = await asyncio.to_thread(
                _run_real_ragas, samples, answers, contexts_by_id, settings
            )
        except Exception as exc:
            print(f">> RELEASE GATE FAILED: official RAGAS evaluation failed: {exc}")
            print("==================================================================")
            return False
        avg_faith = official["faithfulness"]
        avg_relev = official["answer_relevance"]
        avg_prec = official["context_precision"]
        avg_rec = official["context_recall"]
        print(">> Official ragas library metrics (release-quality):")
    else:
        avg_faith = sum(faith_list) / max(1, len(faith_list))
        avg_relev = sum(rel_list) / max(1, len(rel_list))
        avg_prec = sum(prec_list) / max(1, len(prec_list))
        avg_rec = sum(rec_list) / max(1, len(rec_list))

    print("\n---------------- RAGAS SUMMARY METRICS ----------------")
    print(f"  >> Average Faithfulness:      {avg_faith:.2f} (Gate: {thresholds['faithfulness']:.2f})")
    print(f"  >> Average Answer Relevance:  {avg_relev:.2f} (Gate: {thresholds['answer_relevance']:.2f})")
    print(f"  >> Average Context Precision: {avg_prec:.2f} (Gate: {thresholds['context_precision']:.2f})")
    print(f"  >> Average Context Recall:    {avg_rec:.2f} (Gate: {thresholds['context_recall']:.2f})")
    print("-------------------------------------------------------")

    passed_all = (
        avg_faith >= thresholds["faithfulness"]
        and avg_relev >= thresholds["answer_relevance"]
        and avg_prec >= thresholds["context_precision"]
        and avg_rec >= thresholds["context_recall"]
    )

    print("==================================================================")
    if mode == "release" and (proxy_count or not llm_judge_available()):
        print(">> RELEASE GATE FAILED: proxy answers or missing LLM judge.")
    elif passed_all:
        print(
            ">> RAGAS QUALITY GATE PASSED! (Ready for Production)"
            if mode == "release"
            else f">> SMOKE CHECKS PASSED ({PROXY_BANNER}; no production-readiness claim)."
        )
    else:
        print(">> RAGAS QUALITY GATE FAILED.")
    print("==================================================================")
    return passed_all and (mode == "smoke" or not (proxy_count or not llm_judge_available()))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run RAGAS evaluation suite.")
    parser.add_argument("--mode", choices=["smoke", "release"], default=None)
    success = asyncio.run(run_ragas_benchmark(parser.parse_args().mode))
    sys.exit(0 if success else 1)
