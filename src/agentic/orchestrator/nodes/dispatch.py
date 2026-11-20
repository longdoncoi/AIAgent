"""Dispatch Node executing plan steps via A2A Sub-Agents with Fan-out parallelism."""

import asyncio
import logging
import time
from typing import Any

from src.agentic.a2a.client import get_a2a_client
from src.agentic.observability.metrics import NODE_DURATION
from src.agentic.observability.tracing import traced_operation
from src.agentic.orchestrator.state import AgentState, Step

logger = logging.getLogger(__name__)


@traced_operation("orchestrator.dispatch")
async def dispatch_node(state: AgentState) -> dict[str, Any]:
    """Dispatches ready steps (fan-out in parallel) to designated A2A sub-agents."""
    start_t = time.time()
    try:
        plan = state.plan
        if not plan or not plan.steps:
            return {"current_step_index": 0}

        steps = plan.steps
        completed_ids = {s.id for s in steps if s.status == "completed"}
        failed_ids = {s.id for s in steps if s.status == "failed"}

        # Find pending steps that are ready to run
        ready_steps = [
            s
            for s in steps
            if s.status == "pending" and all(dep in completed_ids for dep in s.depends_on)
        ]

        # If no steps are ready because a prerequisite failed, mark blocked steps as failed
        if not ready_steps:
            for s in steps:
                if s.status == "pending" and any(dep in failed_ids for dep in s.depends_on):
                    s.status = "failed"
                    s.output = "Dependency step failed; aborted."
            completed_count = sum(1 for s in steps if s.status in ["completed", "failed"])
            return {
                "plan": plan,
                "current_step_index": completed_count,
                "approval_granted": None,
            }

        a2a_client = get_a2a_client()

        async def _execute_step(step: Step) -> None:
            logger.info(f"Dispatching step '{step.id}' to agent '{step.agent}'...")
            step.status = "running"
            prior_context = {s.id: s.output for s in steps if s.output and s.id != step.id}
            try:
                tokens: list[str] = []
                async for event in a2a_client.call_agent_stream(
                    target_agent=step.agent,
                    step_id=step.id,
                    instruction=step.input_query,
                    context=prior_context,
                    timeout=60.0,
                ):
                    ev_type = event.get("event")
                    if ev_type == "token":
                        t = str(event.get("token", ""))
                        tokens.append(t)
                    elif ev_type == "completed":
                        final_out = str(event.get("output", ""))
                        if final_out:
                            step.output = final_out
                    elif ev_type == "error":
                        step.status = "failed"
                        step.output = f"Sub-agent error: {event.get('error')}"

                if not step.output and tokens:
                    step.output = "".join(tokens)

                if step.status != "failed":
                    step.status = "completed"
                    if not step.output:
                        step.output = "Completed with empty response."
            except Exception as e:
                logger.error(f"Dispatch error for step {step.id}: {e}", exc_info=True)
                step.status = "failed"
                step.output = f"Execution error: {str(e)}"

        # Fan-out: execute ready steps concurrently
        await asyncio.gather(*[_execute_step(s) for s in ready_steps])

        completed_count = sum(1 for s in steps if s.status in ["completed", "failed"])

        return {
            "plan": plan,
            "current_step_index": completed_count,
            "approval_granted": None,  # Reset for next steps
        }
    finally:
        NODE_DURATION.labels(node_name="dispatch").observe(time.time() - start_t)
