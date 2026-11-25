"""Finalize Node synthesizing outputs and consolidating long-term memory."""

import asyncio
import logging
from typing import Any

from langchain_core.messages import AIMessage

from src.agentic.llm.gateway import get_llm
from src.agentic.memory.consolidation import consolidate_session_memory
from src.agentic.observability.tracing import traced_operation
from src.agentic.orchestrator.state import AgentState, Step
from src.agentic.rag.tools import NO_CONTEXT_MARKER, extract_citations, format_citations
from src.agentic.safety.guardrails import get_guardrails

logger = logging.getLogger(__name__)

_background_tasks: set[asyncio.Task[Any]] = set()


def track_background_task(coro: Any, task_name: str = "background_task") -> asyncio.Task[Any]:
    """Retains a strong reference to background task to prevent garbage collection and logs exceptions."""
    task = asyncio.create_task(coro, name=task_name)
    _background_tasks.add(task)

    def _done_cb(t: asyncio.Task[Any]) -> None:
        _background_tasks.discard(t)
        try:
            if not t.cancelled() and t.exception() is not None:
                logger.error(
                    f"Background task '{t.get_name()}' failed with error: {t.exception()}",
                    exc_info=t.exception(),
                )
        except Exception:
            pass

    task.add_done_callback(_done_cb)
    return task


async def wait_all_background_tasks(timeout: float = 5.0) -> None:
    """Waits for all inflight background tasks to finish during graceful process shutdown."""
    if not _background_tasks:
        return
    logger.info(
        f"Waiting for {len(_background_tasks)} inflight background task(s) to complete (timeout={timeout}s)..."
    )
    tasks = list(_background_tasks)
    try:
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=timeout)
        logger.info("All background tasks completed successfully.")
    except TimeoutError:
        logger.warning(
            f"Timed out waiting for background tasks ({len(_background_tasks)} still pending)."
        )


def _consolidate_and_build_result(
    *,
    state: AgentState,
    goal: str,
    sanitized_answer: str,
    is_all_failed: bool,
    has_failures: bool,
    citation_failure: bool,
) -> dict[str, Any]:
    """Schedule LTM consolidation for verified runs and return final state delta."""
    thread_id = state.thread_id or "session_unknown"
    if not is_all_failed and not state.degraded and not citation_failure:
        track_background_task(
            consolidate_session_memory(
                thread_id=thread_id,
                goal=goal,
                final_answer=sanitized_answer,
                metadata={
                    "step_count": len(state.plan.steps) if state.plan else 0,
                    "status": "partial" if has_failures else "completed",
                    "user_id": str(state.metadata.get("user_id") or ""),
                    "tenant_id": str(state.metadata.get("tenant_id") or ""),
                    "public_thread_id": thread_id,
                },
            ),
            task_name=f"consolidate_memory_{thread_id}",
        )

    result: dict[str, Any] = {
        "final_answer": sanitized_answer,
        "messages": [AIMessage(content=sanitized_answer)],
    }
    if citation_failure:
        result.update({"degraded": True, "degraded_reason": "missing_citations"})
    return result


@traced_operation("orchestrator.fast_finalize")
async def fast_finalize_node(state: AgentState) -> dict[str, Any]:
    """Low-latency fast-path finalization for verified single-step chat responses."""
    if not state.plan or not state.plan.steps:
        fallback_msg = "Task finished without a generated plan."
        return {
            "final_answer": fallback_msg,
            "messages": [AIMessage(content=fallback_msg)],
        }
    step = state.plan.steps[0]
    raw_answer = step.output or ""
    sanitized_answer = get_guardrails().sanitize_output(raw_answer)
    return _consolidate_and_build_result(
        state=state,
        goal=state.plan.goal,
        sanitized_answer=sanitized_answer,
        is_all_failed=False,
        has_failures=False,
        citation_failure=False,
    )


@traced_operation("orchestrator.finalize")
async def finalize_node(state: AgentState) -> dict[str, Any]:
    """Compiles step outputs, formats the final synthesized response, and saves memory."""
    if not state.plan or not state.plan.steps:
        fallback_msg = "Task finished without a generated plan."
        return {
            "final_answer": fallback_msg,
            "messages": [AIMessage(content=fallback_msg)],
        }

    goal = state.plan.goal
    step_outputs = [f"### {s.id} ({s.agent})\n{s.output or 'No output'}" for s in state.plan.steps]
    combined_steps = "\n\n".join(step_outputs)

    failed_steps = [s for s in state.plan.steps if s.status == "failed"]
    is_all_failed = len(failed_steps) == len(state.plan.steps)
    has_failures = len(failed_steps) > 0

    if is_all_failed:
        # Never ask the model to "synthesize" from nothing — that is a licence
        # to hallucinate. Return an explicit, deterministic failure notice.
        logger.warning("All plan steps failed; skipping LLM synthesis.")
        raw_answer = (
            f"**Task Failed**: Could not achieve goal '{goal}'. "
            "All execution steps failed, so no answer could be synthesized.\n\n"
            f"{combined_steps}"
        )
    elif state.degraded:
        # The Reflector could not verify outputs. Finalize must not present
        # unverified material as an approved answer.
        logger.error(
            "Finalizing a degraded run (reason=%s); manual review required, skipping synthesis.",
            state.degraded_reason,
        )
        raw_answer = (
            "**Manual Review Required**: The reflection judge was unavailable "
            f"(reason: {state.degraded_reason or 'unknown'}), so the step outputs below "
            "could not be verified. No synthesized answer is provided.\n\n"
            f"{combined_steps}"
        )
    else:
        # Synthesis via LLM — strictly grounded in the supplied step outputs.
        try:
            # The API consumes `astream_events`, so request real token streaming.
            llm = get_llm(alias="reasoning-llm", temperature=0.2, streaming=True)
            status_guidance = (
                "Some steps failed. State clearly what succeeded and what failed, "
                "and do not present failed output as fact."
                if has_failures
                else "All steps completed successfully."
            )
            prompt = (
                "You are the Supervisor. Synthesize a coherent final answer strictly "
                "from the provided step outputs.\n\n"
                "GROUNDING RULES (non-negotiable):\n"
                "1. Answer ONLY from the step outputs below. Never use external or prior knowledge.\n"
                "2. Cite the step ID (e.g. [step_1]) for every factual claim.\n"
                "3. If the outputs do not support a claim, say "
                "'Insufficient information from step outputs to answer the request.'\n"
                "4. Do not invent step IDs, sources, or results.\n\n"
                f"User Goal: {goal}\n\n"
                f"Step Details:\n{combined_steps}\n\n"
                f"Guidance: {status_guidance}\n\n"
                "Deliver a clear, professional, well-formatted response addressing the original request."
            )
            ai_resp = await llm.ainvoke(prompt)
            raw_answer = str(ai_resp.content)
        except Exception as e:
            logger.warning(f"Synthesis LLM call failed ({e}). Using compiled step outputs.")
            if has_failures:
                raw_answer = (
                    f"**Task Partially Completed**: '{goal}'. Some steps failed. "
                    "No synthesis was available; raw step outputs follow.\n\n" + combined_steps
                )
            else:
                raw_answer = f"**Goal Completed**: {goal}\n\n" + combined_steps

    # Sanitize output via Guardrails
    guardrails = get_guardrails()
    sanitized_answer = guardrails.sanitize_output(raw_answer)

    # Defense-in-depth citation gate. The reflector checks each RAG step, but
    # the synthesis model may still omit sources from the user-facing answer.
    # Preserve validated source records deterministically and refuse to publish
    # an uncited RAG answer when upstream output is malformed.
    rag_steps = [step for step in state.plan.steps if step.agent == "rag" and step.output]
    citations_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    uncited_rag_steps: list[Step] = []
    for step in rag_steps:
        step_citations = extract_citations(step.output or "")
        if step_citations:
            for citation in step_citations:
                key = (str(citation.get("doc_id")), str(citation.get("source") or citation.get("file_name")))
                citations_by_key[key] = citation
        elif NO_CONTEXT_MARKER not in (step.output or ""):
            uncited_rag_steps.append(step)
    citation_failure = bool(uncited_rag_steps)
    if citation_failure:
        logger.error("Refusing to finalize RAG output without verified citations.")
        sanitized_answer = (
            "**Manual Review Required**: RAG evidence did not contain valid structured "
            "citations, so no grounded answer can be published."
        )
    elif citations_by_key and not extract_citations(sanitized_answer):
        sanitized_answer = (
            sanitized_answer.rstrip()
            + "\n\n"
            + format_citations(list(citations_by_key.values()))
        )

    return _consolidate_and_build_result(
        state=state,
        goal=goal,
        sanitized_answer=sanitized_answer,
        is_all_failed=is_all_failed,
        has_failures=has_failures,
        citation_failure=citation_failure,
    )
