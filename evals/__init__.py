"""Evaluation subsystem with an explicit smoke/release split.

Smoke mode uses clearly-labeled heuristic proxies. Release mode uses real
generation + LLM judging + the official ragas library and never downgrades
thresholds.
"""

from evals.answer_generator import (
    PROXY_BANNER,
    AnswerResult,
    generate_answer,
    llm_judge_available,
    release_blockers,
)
from evals.evaluators import (
    evaluate_faithfulness,
    evaluate_groundedness,
    evaluate_ragas_answer_relevance,
    evaluate_ragas_context_precision,
    evaluate_ragas_context_recall,
    evaluate_ragas_faithfulness,
    evaluate_ragas_sample,
    evaluate_routing_accuracy,
    evaluate_tool_selection,
)
from evals.ragas_runner import RagasLocalEvaluator, run_ragas_benchmark
from evals.release_gate import run_release_gate
from evals.run_eval import run_benchmark

__all__ = [
    "PROXY_BANNER",
    "AnswerResult",
    "generate_answer",
    "llm_judge_available",
    "release_blockers",
    "run_release_gate",
    "run_benchmark",
    "evaluate_routing_accuracy",
    "evaluate_groundedness",
    "evaluate_faithfulness",
    "evaluate_tool_selection",
    "evaluate_ragas_faithfulness",
    "evaluate_ragas_answer_relevance",
    "evaluate_ragas_context_precision",
    "evaluate_ragas_context_recall",
    "evaluate_ragas_sample",
    "run_ragas_benchmark",
    "RagasLocalEvaluator",
]
