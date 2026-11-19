"""Human-in-the-loop (HITL) approval endpoints to resume paused executions with RBAC."""

import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from langgraph.types import Command

from src.agentic.api.deps import get_graph, require_role
from src.agentic.core.schemas import ApprovalRequest, ApprovalResponse

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
    config = {"configurable": {"thread_id": thread_id}}

    state_snapshot = graph.get_state(config)
    if not state_snapshot.next:
        raise HTTPException(
            status_code=400,
            detail=f"Thread '{thread_id}' is not in an interrupted state or has already finished.",
        )

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
        action_status: Literal["resumed", "aborted"] = "resumed" if req.decision.approved else "aborted"
        return ApprovalResponse(
            thread_id=thread_id,
            status=action_status,
            message=f"Execution for thread '{thread_id}' has been {action_status}.",
        )
    except Exception as e:
        logger.error(f"Error resuming graph on thread {thread_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to resume execution: {str(e)}")
