"""Dispatch Node executing plan steps via A2A Sub-Agents with Fan-out parallelism."""

import asyncio
import json
import logging
import time
import uuid
from typing import Any

from src.agentic.a2a.client import get_a2a_client
from src.agentic.core.config import get_settings
from src.agentic.observability.metrics import NODE_DURATION
from src.agentic.observability.tracing import traced_operation
from src.agentic.orchestrator.state import AgentState, Step
from src.agentic.safety.approvals import issue_approval_grant
from src.agentic.safety.policy import get_policy_manager

logger = logging.getLogger(__name__)


@traced_operation("orchestrator.dispatch")
async def dispatch_node(state: AgentState) -> dict[str, Any]:
    """Dispatches ready steps (fan-out in parallel) to designated A2A sub-agents."""
    start_t = time.time()
    try:
        plan = state.plan
        if not plan or not plan.steps:
            return {"current_step_index": 0}

        steps = [s.model_copy() for s in plan.steps]
        completed_ids = {s.id for s in steps if s.status == "completed"}
        failed_ids = {s.id for s in steps if s.status == "failed"}

        from src.agentic.orchestrator.nodes.hitl import (
            compute_idempotency_key,
            compute_step_fingerprint,
        )

        def is_step_approved(s: Step) -> bool:
            if s.risk_level != "high":
                return True
            if s.id not in state.approved_step_ids:
                return False
            # A high-risk step is approved only if the recorded fingerprint
            # matches exactly. An empty fingerprint map is NEVER a pass.
            return state.approved_step_fingerprints.get(s.id) == compute_step_fingerprint(s)

        # Find pending steps that are ready to run
        ready_steps = [
            s
            for s in steps
            if s.status == "pending" and all(dep in completed_ids for dep in s.depends_on)
            and is_step_approved(s)
        ]

        # If no steps are ready because a prerequisite failed, mark blocked steps as failed
        if not ready_steps:
            changed = False
            for s in steps:
                if s.status == "pending" and any(dep in failed_ids for dep in s.depends_on):
                    s.status = "failed"
                    s.output = "Dependency step failed; aborted."
                    changed = True
            if not changed and not any(
                s.status == "pending" and s.risk_level == "high" for s in steps
            ):
                for s in steps:
                    if s.status == "pending":
                        s.status = "failed"
                        s.output = "Invalid dependency graph; no executable step remains."
            completed_count = sum(1 for s in steps if s.status in ["completed", "failed"])
            return {
                "plan": plan.model_copy(update={"steps": steps}),
                "current_step_index": completed_count,
                "approval_granted": None,
            }

        a2a_client = get_a2a_client()
        policy = get_policy_manager()
        settings = get_settings()
        owner_id = str(state.metadata.get("user_id") or "anonymous_user")
        tenant_id = str(state.metadata.get("tenant_id") or settings.rag_default_tenant_id)
        thread_id = str(state.thread_id or state.metadata.get("thread_id") or "")
        turn_id = str(state.metadata.get("turn_id") or state.last_turn_human_message_id or "")
        pending_approvals: list[dict[str, Any]] = []

        async def _execute_step(step: Step) -> None:
            logger.info(f"Dispatching step '{step.id}' to agent '{step.agent}'...")
            step.status = "running"
            prior_context = {s.id: s.output for s in steps if s.output and s.id != step.id}
            approved_digests = state.approved_tool_call_digests.get(step.id)
            approved_tool_call = state.approved_tool_calls.get(step.id)
            step_fp = compute_step_fingerprint(step)
            idem_key = compute_idempotency_key(
                step,
                tenant_id=tenant_id,
                owner_id=owner_id,
                thread_id=thread_id,
                turn_id=turn_id,
                step_fingerprint=step_fp,
            )
            step_metadata = {
                # Signed, step-scoped capability: the sub-agent verifies
                # it at the tool boundary. No caller-controlled boolean.
                "approval_token": issue_approval_grant(
                    step_id=step.id,
                    fingerprint=step_fp,
                    owner_id=owner_id,
                    agent_name=step.agent,
                    approved_args_digests=approved_digests,
                    tools=[
                        tool
                        for tool in policy.get_agent_tools(step.agent)
                        # Approval-gated tools are only granted after a
                        # genuine HITL decision on a high-risk step.
                        if not policy.is_approval_required(tool)
                        or (step.risk_level == "high" and is_step_approved(step))
                    ],
                ),
                "owner_id": owner_id,
                "step_fingerprint": step_fp,
                "idempotency_key": idem_key,
                "thread_id": thread_id,
                "turn_id": turn_id,
                "user_roles": list(state.metadata.get("user_roles") or []),
                "department": str(state.metadata.get("department") or "public"),
                # Exact command captured before HITL. The sub-agent executes this
                # immutable call directly instead of asking the LLM to regenerate
                # arguments after resume.
                "approved_tool_call": approved_tool_call,
                "client_id": str(state.metadata.get("client_id") or "unattributed"),
                "tenant_id": tenant_id,
            }
            try:
                if settings.use_temporal_dispatch:
                    from temporalio.client import Client

                    from src.agentic.workflows.agent_workflow import AgentExecutionWorkflow

                    temporal_client = await Client.connect(
                        settings.temporal_host, namespace=settings.temporal_namespace
                    )
                    wf_id = (
                        f"agent-step-{state.thread_id or 'anon'}-{step.id}-"
                        f"r{state.reflection_count}-{uuid.uuid4().hex[:8]}"
                    )
                    try:
                        outputs = await temporal_client.execute_workflow(
                            AgentExecutionWorkflow.run,
                            [
                                {
                                    "id": step.id,
                                    "agent": step.agent,
                                    "instruction": step.input_query,
                                    "metadata": step_metadata,
                                }
                            ],
                            id=wf_id,
                            task_queue=settings.temporal_task_queue,
                        )
                    except Exception as wf_exc:
                        cause = getattr(wf_exc, "cause", None)
                        raw_msg = str(cause or wf_exc)
                        try:
                            err_payload = json.loads(raw_msg)
                        except (TypeError, ValueError):
                            err_payload = None
                        if (
                            isinstance(err_payload, dict)
                            and err_payload.get("type") == "tool_call_approval_required"
                        ):
                            pending_approvals.append(
                                {
                                    "step_id": step.id,
                                    "tool_name": str(err_payload.get("tool_name") or ""),
                                    "arguments": dict(err_payload.get("arguments") or {}),
                                    "args_digest": str(err_payload.get("args_digest") or ""),
                                }
                            )
                            step.status = "pending"
                            step.risk_level = "high"
                            return
                        raise
                    step.output = str(outputs[0]) if outputs else "Completed with empty response."
                    step.status = "completed"
                    return

                tokens: list[str] = []
                async for event in a2a_client.call_agent_stream(
                    target_agent=step.agent,
                    step_id=step.id,
                    instruction=step.input_query,
                    context={
                        "prior_steps": prior_context,
                        "request_metadata": state.metadata,
                    },
                    metadata=step_metadata,
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
                    elif ev_type == "approval_required":
                        pending_approvals.append(
                            {
                                "step_id": step.id,
                                "tool_name": str(event.get("tool_name") or ""),
                                "arguments": dict(event.get("arguments") or {}),
                                "args_digest": str(event.get("args_digest") or ""),
                            }
                        )
                        step.status = "pending"
                        step.risk_level = "high"
                        return
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
                step.output = f"Execution error: {e!s}"

        # Fan-out: execute ready steps concurrently
        await asyncio.gather(*[_execute_step(s) for s in ready_steps])

        pending_approvals.sort(key=lambda item: str(item.get("step_id", "")))
        pending_tool_approval = pending_approvals[0] if pending_approvals else None
        completed_count = sum(1 for s in steps if s.status in ["completed", "failed"])

        return {
            "plan": plan.model_copy(update={"steps": steps}),
            "current_step_index": completed_count,
            "approval_granted": None,  # Reset for next steps
            "pending_tool_approval": pending_tool_approval,
        }
    finally:
        NODE_DURATION.labels(node_name="dispatch").observe(time.time() - start_t)
