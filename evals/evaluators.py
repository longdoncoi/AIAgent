"""LLM-as-a-judge and heuristic evaluators for routing accuracy and groundedness."""

import logging
import os

from pydantic import BaseModel, Field

from src.agentic.llm.gateway import get_llm

logger = logging.getLogger(__name__)


class GroundednessScore(BaseModel):
    groundedness: float = Field(description="Score between 0.0 and 1.0 indicating factual fidelity")
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
    if not generated_answer.strip() or not retrieved_context.strip():
        return 0.0
    ans_tokens = [w for w in generated_answer.lower().split() if len(w) > 3]
    if not ans_tokens:
        return 1.0
    ctx_text = retrieved_context.lower()
    supported = sum(1 for token in ans_tokens if token in ctx_text)
    return round(float(supported / len(ans_tokens)), 3)


async def evaluate_groundedness(question: str, generated_answer: str, ground_truth: str) -> float:
    """Evaluates whether the generated answer is faithful to the reference truth."""
    if not generated_answer.strip():
        return 0.0

    # Objective token recall against reference ground truth
    gt_tokens = {w for w in ground_truth.lower().split() if len(w) > 3}
    ans_tokens = {w for w in generated_answer.lower().split() if len(w) > 3}
    overlap = len(gt_tokens.intersection(ans_tokens)) / max(1, len(gt_tokens))

    # Try LLM-as-a-judge if model is responsive
    if os.environ.get("SKIP_LLM_JUDGE") != "true":
        try:
            llm = get_llm(alias="reasoning-llm", temperature=0.0)
            structured_llm = llm.with_structured_output(GroundednessScore)
            prompt = (
                f"Question: {question}\n\n"
                f"Ground Truth Reference: {ground_truth}\n\n"
                f"Candidate Answer: {generated_answer}\n\n"
                "Score the factual groundedness of the candidate answer against the reference."
            )
            judge_res = await structured_llm.ainvoke(prompt)
            if isinstance(judge_res, GroundednessScore):
                return judge_res.groundedness
        except Exception as e:
            logger.debug(f"LLM-as-a-judge fallback to objective token recall ({e})")

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
