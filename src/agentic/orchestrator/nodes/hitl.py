"""Human-In-The-Loop (HITL) node pausing graph on high-risk steps."""

import logging
from typing import Any

from langgraph.types import interrupt

from src.agentic.observability.metrics import HITL_APPROVALS_TOTAL
from src.agentic.observability.tracing import traced_operation
from src.agentic.orchestrator.state import AgentState

logger = logging.getLogger(__name__)


@traced_operation("orchestrator.hitl")
async def hitl_node(state: AgentState) -> dict[str, Any]:
    """Inspects pending steps for high-risk actions and triggers HITL interrupt."""
    if not state.plan or not state.plan.steps:
        return {"requires_approval": False}

    completed_ids = {s.id for s in state.plan.steps if s.status == "completed"}
    ready_steps = [
        s
        for s in state.plan.steps
        if s.status == "pending" and all(dep in completed_ids for dep in s.depends_on)
    ]

    if not ready_steps:
        return {"requires_approval": False}

    # Find first high-risk step among ready steps
    high_risk_step = next((s for s in ready_steps if s.risk_level == "high"), None)

    # If no high-risk step is ready, proceed directly
    if not high_risk_step:
        return {"requires_approval": False}

    if high_risk_step.id in state.approved_step_ids:
        return {"requires_approval": False, "pending_approval_step_id": None}

    # Compatibility for explicitly constructed/restored states. Graph-managed approvals
    # additionally persist the step ID, preventing this flag from authorizing later steps.
    if state.approval_granted is True:
        return {
            "requires_approval": False,
            "approval_granted": True,
            "approved_step_ids": [*state.approved_step_ids, high_risk_step.id],
            "pending_approval_step_id": None,
        }
    if state.approval_granted is False:
        HITL_APPROVALS_TOTAL.labels(decision="rejected", risk_level="high").inc()
        high_risk_step.status = "failed"
        high_risk_step.output = "Action rejected by Human-in-the-Loop supervisor."
        return {
            "plan": state.plan,
            "requires_approval": False,
            "approval_granted": False,
            "pending_approval_step_id": None,
            "current_step_index": state.current_step_index + 1,
        }

    # Trigger LangGraph HITL interrupt
    logger.info(
        f"Triggering HITL interrupt for high-risk step: {high_risk_step.id} ('{high_risk_step.description}')"
    )
    approval_data = interrupt(
        {
            "step_id": high_risk_step.id,
            "description": high_risk_step.description,
            "input_query": high_risk_step.input_query,
            "risk_level": high_risk_step.risk_level,
            "approval_tools": high_risk_step.approval_tools,
        }
    )

    # When resumed, approval_data contains the user's decision
    approved = False
    if isinstance(approval_data, dict):
        approved = bool(approval_data.get("approved", False))
        modified_input = approval_data.get("modified_input")
        if modified_input:
            high_risk_step.input_query = modified_input

    if approved:
        HITL_APPROVALS_TOTAL.labels(decision="approved", risk_level="high").inc()
        return {
            "requires_approval": False,
            "approval_granted": True,
            "approved_step_ids": [*state.approved_step_ids, high_risk_step.id],
            "pending_approval_step_id": None,
        }
    else:
        HITL_APPROVALS_TOTAL.labels(decision="rejected", risk_level="high").inc()
        high_risk_step.status = "failed"
        high_risk_step.output = "Action rejected by Human-in-the-Loop supervisor."
        return {
            "requires_approval": False,
            "approval_granted": False,
            "pending_approval_step_id": None,
            "current_step_index": state.current_step_index + 1,
        }
