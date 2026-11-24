"""Human-In-The-Loop (HITL) node pausing graph on high-risk steps."""

import hashlib
import logging
from typing import Any

from langgraph.types import interrupt

from src.agentic.observability.metrics import HITL_APPROVALS_TOTAL, HITL_PENDING_APPROVALS
from src.agentic.observability.tracing import traced_operation
from src.agentic.orchestrator.state import AgentState, Step

logger = logging.getLogger(__name__)


def compute_step_fingerprint(step: Step) -> str:
    """Computes a cryptographic hash of step parameters to bind approval to exact contents."""
    raw = f"{step.id}:{step.agent}:{step.input_query}:{step.risk_level}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


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

    step_fp = compute_step_fingerprint(high_risk_step)

    # Check if previously approved with matching cryptographic fingerprint
    if (
        high_risk_step.id in state.approved_step_ids
        and state.approved_step_fingerprints.get(high_risk_step.id) == step_fp
    ):
        return {"requires_approval": False, "pending_approval_step_id": None}

    # A bare boolean in state is NOT proof of a human decision; the legacy
    # compatibility branch was removed to prevent HITL bypass via checkpoint.
    if state.approval_granted is False:
        HITL_APPROVALS_TOTAL.labels(decision="rejected", risk_level="high").inc()
        updated_steps = [
            s.model_copy(
                update={
                    "status": "failed",
                    "output": "Action rejected by Human-in-the-Loop supervisor.",
                }
            )
            if s.id == high_risk_step.id
            else s
            for s in state.plan.steps
        ]
        updated_plan = state.plan.model_copy(update={"steps": updated_steps})
        return {
            "plan": updated_plan,
            "requires_approval": False,
            "approval_granted": False,
            "pending_approval_step_id": None,
            "current_step_index": state.current_step_index + 1,
        }

    # Trigger LangGraph HITL interrupt
    logger.info(
        f"Triggering HITL interrupt for high-risk step: {high_risk_step.id} ('{high_risk_step.description}') [fp={step_fp[:8]}]"
    )
    HITL_PENDING_APPROVALS.inc()
    approval_data = interrupt(
        {
            "step_id": high_risk_step.id,
            "agent": high_risk_step.agent,
            "description": high_risk_step.description,
            "input_query": high_risk_step.input_query,
            "risk_level": high_risk_step.risk_level,
            "fingerprint": step_fp,
        }
    )

    # When resumed, approval_data contains the user's decision
    approved = False
    updated_plan = state.plan
    if isinstance(approval_data, dict):
        approved = bool(approval_data.get("approved", False))
        modified_input = approval_data.get("modified_input")
        if modified_input:
            updated_steps = [
                s.model_copy(update={"input_query": modified_input})
                if s.id == high_risk_step.id
                else s
                for s in state.plan.steps
            ]
            updated_plan = state.plan.model_copy(update={"steps": updated_steps})
            high_risk_step = next(s for s in updated_steps if s.id == high_risk_step.id)
            step_fp = compute_step_fingerprint(high_risk_step)

    HITL_PENDING_APPROVALS.dec()
    if approved:
        HITL_APPROVALS_TOTAL.labels(decision="approved", risk_level="high").inc()
        new_ids = (
            state.approved_step_ids
            if high_risk_step.id in state.approved_step_ids
            else [*state.approved_step_ids, high_risk_step.id]
        )
        new_fps = {**state.approved_step_fingerprints, high_risk_step.id: step_fp}
        return {
            "plan": updated_plan,
            "requires_approval": False,
            "approval_granted": True,
            "approved_step_ids": new_ids,
            "approved_step_fingerprints": new_fps,
            "pending_approval_step_id": None,
        }
    else:
        HITL_APPROVALS_TOTAL.labels(decision="rejected", risk_level="high").inc()
        # Direct mutation for legacy in-memory references alongside immutable plan delta
        high_risk_step.status = "failed"
        high_risk_step.output = "Action rejected by Human-in-the-Loop supervisor."
        updated_steps = [
            s.model_copy(
                update={
                    "status": "failed",
                    "output": "Action rejected by Human-in-the-Loop supervisor.",
                }
            )
            if s.id == high_risk_step.id
            else s
            for s in updated_plan.steps
        ]
        updated_plan = updated_plan.model_copy(update={"steps": updated_steps})
        return {
            "plan": updated_plan,
            "requires_approval": False,
            "approval_granted": False,
            "pending_approval_step_id": None,
            "current_step_index": state.current_step_index + 1,
        }
