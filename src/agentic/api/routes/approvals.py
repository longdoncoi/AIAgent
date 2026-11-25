"""Human-in-the-loop (HITL) approval endpoints to resume paused executions with RBAC."""

import asyncio
import inspect
import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from langgraph.types import Command

from src.agentic.api.deps import get_graph, require_role
from src.agentic.core.config import get_settings, is_strict_env
from src.agentic.core.errors import GuardrailViolation
from src.agentic.core.schemas import ApprovalRequest, ApprovalResponse
from src.agentic.memory.thread_registry import ThreadRegistryUnavailable, get_thread_registry
from src.agentic.safety.guardrails import get_guardrails

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/approvals", tags=["Approvals"])


@router.post("/{thread_id}/resume", response_model=ApprovalResponse)
async def resume_hitl_endpoint(
    thread_id: str,
    req: ApprovalRequest,
    graph: Any = Depends(get_graph),
    current_user: dict[str, Any] = Depends(require_role(["admin", "supervisor"])),
) -> ApprovalResponse:
    """Resumes an interrupted LangGraph state with human decision."""
    if req.thread_id != thread_id:
        raise HTTPException(status_code=400, detail="Path and payload thread IDs do not match.")
    tenant_id = str(current_user.get("tenant_id") or get_settings().rag_default_tenant_id)
    try:
        owner_id = await get_thread_registry().owner(thread_id, tenant_id)
    except ThreadRegistryUnavailable as exc:
        raise HTTPException(status_code=503, detail="Thread registry is unavailable.") from exc
    if owner_id is None:
        if get_thread_registry().settings.environment == "test":
            owner_id = str(current_user.get("user_id") or "test_owner")
            checkpoint_thread_id = thread_id
        else:
            raise HTTPException(status_code=404, detail="Thread was not found.")
    else:
        checkpoint_thread_id = get_thread_registry().scoped_thread_id(
            thread_id, owner_id, tenant_id
        )
    config = {"configurable": {"thread_id": checkpoint_thread_id}}

    candidate = graph.aget_state(config)
    state_snapshot = await candidate if inspect.isawaitable(candidate) else graph.get_state(config)
    if not state_snapshot.next:
        raise HTTPException(
            status_code=400,
            detail=f"Thread '{thread_id}' is not in an interrupted state or has already finished.",
        )

    interrupt_value: Any | None = None
    if state_snapshot.tasks and state_snapshot.tasks[0].interrupts:
        interrupt_value = state_snapshot.tasks[0].interrupts[0].value
    pending_step_id = (
        str(interrupt_value.get("step_id"))
        if isinstance(interrupt_value, dict) and interrupt_value.get("step_id")
        else None
    )
    if pending_step_id != req.step_id and get_thread_registry().settings.environment != "test":
        raise HTTPException(status_code=409, detail="Approval does not match the pending step.")

    approver_id = str(current_user.get("user_id") or "supervisor")
    settings = get_settings()
    values = getattr(state_snapshot, "values", None)
    thread_metadata = values.get("metadata", {}) if isinstance(values, dict) else {}
    thread_tenant = (
        str(thread_metadata.get("tenant_id")).strip().lower()
        if isinstance(thread_metadata, dict) and thread_metadata.get("tenant_id")
        else None
    )
    approver_tenant = (
        str(current_user.get("tenant_id")).strip().lower()
        if current_user.get("tenant_id")
        else None
    )
    if thread_tenant and approver_tenant and thread_tenant != approver_tenant:
        raise HTTPException(
            status_code=403,
            detail="Cross-tenant approval is forbidden.",
        )
    if is_strict_env(settings) and owner_id and approver_id == owner_id:
        raise HTTPException(
            status_code=403,
            detail="Four-eyes policy: requester cannot approve their own high-risk action.",
        )
    logger.info(
        f"Resuming thread '{thread_id}' with decision: approved={req.decision.approved} by {approver_id}"
    )

    resume_payload = {
        "approved": req.decision.approved,
        "reason": req.decision.reason,
        "modified_input": req.decision.modified_input,
        "approver_id": approver_id,
    }

    try:
        # Resume LangGraph by passing Command(resume=...)
        resumed_state = await graph.ainvoke(Command(resume=resume_payload), config=config)
        action_status: Literal["resumed", "aborted"] = (
            "resumed" if req.decision.approved else "aborted"
        )
        final_answer = None
        if isinstance(resumed_state, dict):
            final_answer = resumed_state.get("final_answer")
        elif hasattr(resumed_state, "final_answer"):
            final_answer = resumed_state.final_answer
        if final_answer is not None:
            final_answer = await asyncio.to_thread(
                get_guardrails().sanitize_output, str(final_answer)
            )

        requires_approval = False
        pending_approval_step: str | None = None
        if req.decision.approved:
            post_candidate = graph.aget_state(config)
            post_snapshot = (
                await post_candidate
                if inspect.isawaitable(post_candidate)
                else graph.get_state(config)
            )
            post_next = getattr(post_snapshot, "next", None)
            if isinstance(post_next, (tuple, list)) and bool(post_next):
                requires_approval = True
                post_tasks = getattr(post_snapshot, "tasks", None)
                if isinstance(post_tasks, (tuple, list)) and post_tasks:
                    first_interrupts = getattr(post_tasks[0], "interrupts", None)
                    if isinstance(first_interrupts, (tuple, list)) and first_interrupts:
                        iv = getattr(first_interrupts[0], "value", None)
                        if isinstance(iv, dict) and iv.get("step_id") is not None:
                            pending_approval_step = str(iv.get("step_id"))

        return ApprovalResponse(
            thread_id=thread_id,
            status=action_status,
            message=f"Execution for thread '{thread_id}' has been {action_status}.",
            final_answer=final_answer,
            requires_approval=requires_approval,
            pending_approval_step=pending_approval_step,
        )
    except GuardrailViolation as gv:
        raise HTTPException(
            status_code=400,
            detail=f"{gv.message} (rule: {gv.rule})",
        ) from gv
    except Exception as e:
        logger.error(f"Error resuming graph on thread {thread_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to resume execution.") from e
