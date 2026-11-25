"""LLM-as-a-judge and heuristic evaluators for routing accuracy and groundedness."""

import logging
import os

from pydantic import BaseModel, Field

from src.agentic.llm.gateway import get_llm

logger = logging.getLogger(__name__)


class GroundednessScore(BaseModel):
    groundedness: float = Field(
        ge=0.0,
        le=1.0,
        description="Normalized decimal score strictly between 0.0 and 1.0 indicating factual fidelity",
    )
    hallucination_detected: bool = Field(description="True if unsupported claims are found")
    reasoning: str = Field(description="Explanation of the assigned score")


def evaluate_routing_accuracy(planned_agent: str, expected_agent: str) -> float:
    """Computes exact match score for sub-agent routing."""
    norm_planned = planned_agent.lower().replace("_agent", "")
    norm_expected = expected_agent.lower().replace("_agent", "")
    return 1.0 if norm_planned == norm_expected else 0.0


def evaluate_tool_selection(selected_tools: list[str], expected_tools: list[str]) -> float:
    """Computes Jaccard similarity score for agent tool selection."""
    if not expected_tools and not selected_tools:
        return 1.0
    if not expected_tools or not selected_tools:
        return 0.0
    set_sel = set(selected_tools)
    set_exp = set(expected_tools)
    intersection = len(set_sel.intersection(set_exp))
    union = len(set_sel.union(set_exp))
    return float(intersection / union) if union > 0 else 0.0


def evaluate_faithfulness(generated_answer: str, retrieved_context: str) -> float:
    """Computes statistical fidelity ratio ensuring answer tokens originate from context."""
    from evals.answer_generator import is_empty_retrieval

    if not generated_answer.strip() or not retrieved_context.strip():
        return 0.0
    ans_lower = generated_answer.lower()
    if is_empty_retrieval(retrieved_context):
        abstention_cues = (
            "không có thông tin",
            "không tìm thấy",
            "no relevant",
            "does not contain",
            "not found in",
        )
        return 1.0 if any(cue in ans_lower for cue in abstention_cues) else 0.0
    ans_tokens = [w for w in ans_lower.split() if len(w) > 3]
    if not ans_tokens:
        return 0.0
    ctx_text = retrieved_context.lower()
    supported = sum(1 for token in ans_tokens if token in ctx_text)
    return round(float(supported / len(ans_tokens)), 3)


async def evaluate_groundedness(
    question: str, generated_answer: str, ground_truth: str, *, mode: str = "smoke"
) -> float:
    """Mode-aware groundedness: judge failure in release mode is a hard error."""
    if not generated_answer.strip():
        return 0.0

    if os.environ.get("SKIP_LLM_JUDGE") != "true":
        try:
            llm = get_llm(alias="reasoning-llm", temperature=0.0, max_tokens=512, timeout=120.0)
            structured_llm = llm.with_structured_output(GroundednessScore)
            prompt = (
                f"Question: {question}\n\n"
                f"Ground Truth Reference: {ground_truth}\n\n"
                f"Candidate Answer: {generated_answer}\n\n"
                "Score the factual groundedness of the candidate answer against the reference "
                "strictly as a normalized float between 0.0 and 1.0 (never use a 0-10 scale)."
            )
            judge_res = await structured_llm.ainvoke(prompt)
            if isinstance(judge_res, GroundednessScore):
                return min(1.0, max(0.0, float(judge_res.groundedness)))
        except Exception as e:
            if mode == "release":
                raise RuntimeError(
                    "RELEASE GATE FAIL: groundedness LLM judge unavailable; "
                    "token overlap is not a release-quality measurement."
                ) from e
            logger.warning("Using SMOKE-ONLY token-overlap proxy for groundedness: %s", e)

    gt_tokens = {w for w in ground_truth.lower().split() if len(w) > 3}
    ans_tokens = {w for w in generated_answer.lower().split() if len(w) > 3}
    overlap = len(gt_tokens.intersection(ans_tokens)) / max(1, len(gt_tokens))
    return round(min(1.0, overlap), 3)


# ------------------------------------------------------------------------------
# RAGAS Framework Evaluator Adapters
# ------------------------------------------------------------------------------
async def evaluate_ragas_faithfulness(
    question: str, contexts: list[str] | str, answer: str
) -> float:
    """Computes RAGAS faithfulness using RagasLocalEvaluator."""
    from evals.ragas_runner import RagasLocalEvaluator

    ctx_list = [contexts] if isinstance(contexts, str) else contexts
    evaluator = RagasLocalEvaluator()
    return await evaluator.evaluate_faithfulness(question, ctx_list, answer)


async def evaluate_ragas_answer_relevance(question: str, answer: str) -> float:
    """Computes RAGAS answer relevance using RagasLocalEvaluator."""
    from evals.ragas_runner import RagasLocalEvaluator

    evaluator = RagasLocalEvaluator()
    return await evaluator.evaluate_answer_relevance(question, answer)


async def evaluate_ragas_context_precision(
    question: str, contexts: list[str] | str, ground_truth: str
) -> float:
    """Computes RAGAS context precision using RagasLocalEvaluator."""
    from evals.ragas_runner import RagasLocalEvaluator

    ctx_list = [contexts] if isinstance(contexts, str) else contexts
    evaluator = RagasLocalEvaluator()
    return await evaluator.evaluate_context_precision(question, ctx_list, ground_truth)


async def evaluate_ragas_context_recall(
    question: str, contexts: list[str] | str, ground_truth: str
) -> float:
    """Computes RAGAS context recall using RagasLocalEvaluator."""
    from evals.ragas_runner import RagasLocalEvaluator

    ctx_list = [contexts] if isinstance(contexts, str) else contexts
    evaluator = RagasLocalEvaluator()
    return await evaluator.evaluate_context_recall(question, ctx_list, ground_truth)


async def evaluate_ragas_sample(
    question: str,
    contexts: list[str] | str,
    answer: str,
    ground_truth: str,
) -> dict[str, float]:
    """Evaluates all 4 canonical RAGAS metrics for a single QA turn."""
    from evals.ragas_runner import RagasLocalEvaluator

    ctx_list = [contexts] if isinstance(contexts, str) else contexts
    evaluator = RagasLocalEvaluator()
    faith = await evaluator.evaluate_faithfulness(question, ctx_list, answer)
    relev = await evaluator.evaluate_answer_relevance(question, answer)
    prec = await evaluator.evaluate_context_precision(question, ctx_list, ground_truth)
    rec = await evaluator.evaluate_context_recall(question, ctx_list, ground_truth)
    return {
        "faithfulness": faith,
        "answer_relevance": relev,
        "context_precision": prec,
        "context_recall": rec,
    }
