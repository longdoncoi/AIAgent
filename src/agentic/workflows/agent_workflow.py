"""Temporal Workflow and Activities for Durable, Long-Running Agent Tasks."""

import json
from datetime import timedelta
from typing import Any

from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from src.agentic.a2a.client import get_a2a_client


@activity.defn
async def execute_plan_step(
    target_agent: str,
    step_id: str,
    instruction: str,
    metadata: dict[str, Any] | None = None,
) -> str:
    """Temporal activity executing an individual plan step via A2A client."""
    client = get_a2a_client()
    resp = await client.call_agent(
        target_agent=target_agent,
        step_id=step_id,
        instruction=instruction,
        metadata=metadata,
        timeout=120.0,
    )
    if resp.status == "completed":
        return resp.output
    if resp.error:
        try:
            err_payload = json.loads(resp.error)
            if isinstance(err_payload, dict) and err_payload.get("type") == "tool_call_approval_required":
                raise ApplicationError(
                    resp.error,
                    type="ToolCallApprovalRequired",
                    non_retryable=True,
                )
        except ApplicationError:
            raise
        except (TypeError, ValueError):
            pass
    raise RuntimeError(f"Step {step_id} failed: {resp.error}")


@workflow.defn
class AgentExecutionWorkflow:
    """Durable Temporal workflow executing multi-agent pipeline with automatic retries."""

    @workflow.run
    async def run(self, steps_payload: list[dict[str, Any]]) -> list[str]:
        results: list[str] = []
        for step_data in steps_payload:
            agent = step_data["agent"]
            step_id = step_data["id"]
            instruction = step_data["instruction"]
            metadata = step_data.get("metadata")

            step_output = await workflow.execute_activity(
                execute_plan_step,
                args=[agent, step_id, instruction, metadata],
                start_to_close_timeout=timedelta(seconds=180),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            results.append(step_output)

        return results


# Export for convenience
ExecutePlanStepActivity = execute_plan_step
