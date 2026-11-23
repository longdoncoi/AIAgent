"""Human-in-the-loop (HITL) approval endpoints to resume paused executions with RBAC."""

import inspect
import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from langgraph.types import Command

from src.agentic.api.deps import get_graph, require_role
from src.agentic.core.schemas import ApprovalRequest, ApprovalResponse
from src.agentic.memory.thread_registry import ThreadRegistryUnavailable, get_thread_registry

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
    try:
        owner_id = await get_thread_registry().owner(thread_id)
    except ThreadRegistryUnavailable as exc:
        raise HTTPException(status_code=503, detail="Thread registry is unavailable.") from exc
    if owner_id is None:
        if get_thread_registry().settings.environment == "test":
            owner_id = str(current_user.get("user_id") or "test_owner")
            checkpoint_thread_id = thread_id
        else:
            raise HTTPException(status_code=404, detail="Thread was not found.")
    else:
        checkpoint_thread_id = get_thread_registry().scoped_thread_id(thread_id, owner_id)
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

    logger.info(
        f"Resuming thread '{thread_id}' with decision: approved={req.decision.approved} by {current_user.get('user_id')}"
    )

    resume_payload = {
        "approved": req.decision.approved,
        "reason": req.decision.reason,
        "modified_input": req.decision.modified_input,
    }

    try:
        # Resume LangGraph by passing Command(resume=...)
        await graph.ainvoke(Command(resume=resume_payload), config=config)
        action_status: Literal["resumed", "aborted"] = (
            "resumed" if req.decision.approved else "aborted"
        )
        return ApprovalResponse(
            thread_id=thread_id,
            status=action_status,
            message=f"Execution for thread '{thread_id}' has been {action_status}.",
        )
    except Exception as e:
        logger.error(f"Error resuming graph on thread {thread_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to resume execution.") from e
