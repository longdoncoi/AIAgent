"""Explicit answer generation + judge availability helpers for truthful evaluation.

WHY THIS MODULE EXISTS
----------------------
Previous evaluation runners scored the *retrieval context* or the *ground-truth
reference* as if it were the model output. That measured the retriever / the answer
key, never the LLM. This module provides a single, explicit entry point:

    await generate_answer(query, context) -> AnswerResult

Callers MUST evaluate ``AnswerResult.answer`` and MUST mark results as
``heuristic proxy — not a release-quality measurement`` when
``AnswerResult.is_proxy`` is True. A release gate MUST fail on any proxy result.
"""

from __future__ import annotations

from dataclasses import dataclass

from langchain_core.messages import HumanMessage, SystemMessage

from evals.prerequisites import (
    PROXY_BANNER,
    gateway_reachable,
    heuristic_mode_active,
    llm_judge_available,
    release_blockers,
)
from src.agentic.core.config import get_settings

__all__ = [
    "PROXY_BANNER",
    "gateway_reachable",
    "generate_answer",
    "heuristic_mode_active",
    "is_empty_retrieval",
    "llm_judge_available",
    "release_blockers",
]

NO_CONTEXT_MARKER = "No relevant enterprise documentation found"


@dataclass(frozen=True)
class AnswerResult:
    """A generated answer plus provenance about how it was produced."""

    answer: str
    is_proxy: bool
    reason: str = ""

    @property
    def quality_label(self) -> str:
        return PROXY_BANNER if self.is_proxy else "model-generated answer (release-quality candidate)"


def is_empty_retrieval(contexts: list[str] | str | None) -> bool:
    """Whether retrieval genuinely returned nothing (negative/refusal case).

    A refusal is only legitimate when the retrieval layer is *actually* empty.
    It must never be inferred from strings appearing in the generated answer.
    """
    if contexts is None:
        return True
    items = [contexts] if isinstance(contexts, str) else contexts
    cleaned = [c.strip() for c in items if isinstance(c, str) and c.strip()]
    if not cleaned:
        return True
    return all(NO_CONTEXT_MARKER in c for c in cleaned)


def _proxy_answer(query: str, context: str) -> str:
    """Extractive, deterministic fallback. Explicitly NOT a model generation."""
    if is_empty_retrieval(context):
        return "Không có thông tin liên quan trong tài liệu nội bộ để trả lời câu hỏi này."
    query_tokens = {
        w.lower() for w in "".join(c if c.isalnum() else " " for c in query).split() if len(w) > 2
    }
    candidates = [s.strip() for s in context.replace("\n", " ").split(".") if s.strip()]
    ranked = sorted(
        candidates,
        key=lambda s: -len(query_tokens & {w.lower() for w in s.split() if len(w) > 2}),
    )
    excerpt = ". ".join(ranked[:2]).strip()
    return f"[{PROXY_BANNER}] {excerpt}"


async def generate_answer(
    query: str, context: str, alias: str | None = None
) -> AnswerResult:
    """Generate an answer to ``query`` grounded in ``context`` via the real model gateway.

    Returns an :class:`AnswerResult`. When the gateway is offline or the call
    fails, returns a clearly labeled heuristic proxy — callers must surface this
    label and release gates must reject it.
    """
    settings = get_settings()
    model_alias = alias or settings.eval_generator_model
    if llm_judge_available():
        try:
            from src.agentic.llm.gateway import get_llm

            llm = get_llm(alias=model_alias, temperature=0.0, max_tokens=512, timeout=120.0)
            messages = [
                SystemMessage(
                    content=(
                        "You answer strictly from the provided enterprise context. "
                        "Always respond in the exact same language as the user's question "
                        "(e.g. Vietnamese for Vietnamese questions). Be concise, factual, and direct. "
                        "If the context says 'No relevant enterprise documentation found' or does not "
                        "contain the answer, state clearly in the question's language that the internal "
                        "policy documentation does not contain this information. Never use outside knowledge."
                    )
                ),
                HumanMessage(content=f"Context:\n{context}\n\nQuestion: {query}"),
            ]
            response = await llm.ainvoke(messages)
            content = response.content if hasattr(response, "content") else str(response)
            if isinstance(content, list):  # some providers return content blocks
                content = "".join(
                    part.get("text", "") if isinstance(part, dict) else str(part)
                    for part in content
                )
            if str(content).strip():
                return AnswerResult(answer=str(content).strip(), is_proxy=False)
            reason = "model gateway returned an empty answer"
        except Exception as exc:  # pragma: no cover - depends on live gateway
            reason = f"model gateway call failed: {type(exc).__name__}: {exc}"
    else:
        reason = "LLM gateway unreachable or SKIP_LLM_JUDGE=true (heuristic judge forced)"

    return AnswerResult(answer=_proxy_answer(query, context), is_proxy=True, reason=reason)
