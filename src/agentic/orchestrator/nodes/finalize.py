"""Finalize Node synthesizing outputs and consolidating long-term memory."""

import asyncio
import logging
from typing import Any

from langchain_core.messages import AIMessage

from src.agentic.llm.gateway import get_llm
from src.agentic.memory.consolidation import consolidate_session_memory
from src.agentic.observability.tracing import traced_operation
from src.agentic.orchestrator.state import AgentState
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
    logger.info(f"Waiting for {len(_background_tasks)} inflight background task(s) to complete (timeout={timeout}s)...")
    tasks = list(_background_tasks)
    try:
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=timeout)
        logger.info("All background tasks completed successfully.")
    except asyncio.TimeoutError:
        logger.warning(f"Timed out waiting for background tasks ({len(_background_tasks)} still pending).")


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
    step_outputs = [
        f"### {s.id} ({s.agent})\n{s.output or 'No output'}"
        for s in state.plan.steps
    ]
    combined_steps = "\n\n".join(step_outputs)

    # Synthesis via LLM or direct aggregation
    try:
        llm = get_llm(alias="reasoning-llm", temperature=0.2)
        prompt = (
            f"You are the Supervisor. Synthesize a coherent, final answer for the user based on the executed steps.\n\n"
            f"User Goal: {goal}\n\n"
            f"Step Details:\n{combined_steps}\n\n"
            f"Deliver a clear, professional, well-formatted response addressing the original request."
        )
        ai_resp = await llm.ainvoke(prompt)
        raw_answer = str(ai_resp.content)
    except Exception as e:
        logger.warning(f"Synthesis LLM call failed ({e}). Using compiled step outputs.")
        raw_answer = f"**Goal Completed**: {goal}\n\n" + combined_steps

    # Sanitize output via Guardrails
    guardrails = get_guardrails()
    sanitized_answer = guardrails.sanitize_output(raw_answer)

    # Fire background task to consolidate session into long-term memory with strong reference tracking
    thread_id = state.thread_id or "session_unknown"
    track_background_task(
        consolidate_session_memory(
            thread_id=thread_id,
            goal=goal,
            final_answer=sanitized_answer,
            metadata={"step_count": len(state.plan.steps)},
        ),
        task_name=f"consolidate_memory_{thread_id}",
    )

    return {
        "final_answer": sanitized_answer,
        "messages": [AIMessage(content=sanitized_answer)],
    }
