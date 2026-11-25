"""Evaluation subsystem with lazy public exports.

Importing :mod:`evals` or a lightweight prerequisite probe must not eagerly
load Torch, LlamaIndex, RAGAS, DeepEval, or the orchestration graph. Individual
symbols retain the previous public API and are imported only when requested.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "PROXY_BANNER": ("evals.answer_generator", "PROXY_BANNER"),
    "AnswerResult": ("evals.answer_generator", "AnswerResult"),
    "generate_answer": ("evals.answer_generator", "generate_answer"),
    "llm_judge_available": ("evals.answer_generator", "llm_judge_available"),
    "release_blockers": ("evals.answer_generator", "release_blockers"),
    "run_release_gate": ("evals.release_gate", "run_release_gate"),
    "run_benchmark": ("evals.run_eval", "run_benchmark"),
    "evaluate_routing_accuracy": ("evals.evaluators", "evaluate_routing_accuracy"),
    "evaluate_groundedness": ("evals.evaluators", "evaluate_groundedness"),
    "evaluate_faithfulness": ("evals.evaluators", "evaluate_faithfulness"),
    "evaluate_tool_selection": ("evals.evaluators", "evaluate_tool_selection"),
    "evaluate_ragas_faithfulness": ("evals.evaluators", "evaluate_ragas_faithfulness"),
    "evaluate_ragas_answer_relevance": (
        "evals.evaluators",
        "evaluate_ragas_answer_relevance",
    ),
    "evaluate_ragas_context_precision": (
        "evals.evaluators",
        "evaluate_ragas_context_precision",
    ),
    "evaluate_ragas_context_recall": (
        "evals.evaluators",
        "evaluate_ragas_context_recall",
    ),
    "evaluate_ragas_sample": ("evals.evaluators", "evaluate_ragas_sample"),
    "run_ragas_benchmark": ("evals.ragas_runner", "run_ragas_benchmark"),
    "RagasLocalEvaluator": ("evals.ragas_runner", "RagasLocalEvaluator"),
}

__all__ = list(_LAZY_EXPORTS)


def __getattr__(name: str) -> Any:
    """Resolve the legacy public API without eager package-level imports."""
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute_name = target
    value = getattr(import_module(module_name), attribute_name)
    globals()[name] = value
    return value
