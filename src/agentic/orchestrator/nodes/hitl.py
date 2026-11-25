"""Human-In-The-Loop (HITL) node pausing graph on high-risk steps."""

import asyncio
import hashlib
import logging
import threading
from typing import Any

from langgraph.errors import GraphInterrupt
from langgraph.types import interrupt

from src.agentic.observability.metrics import HITL_APPROVALS_TOTAL, HITL_PENDING_APPROVALS
from src.agentic.observability.tracing import traced_operation
from src.agentic.orchestrator.state import AgentState, Step
from src.agentic.safety.audit import record_audit_event
from src.agentic.safety.policy import get_policy_manager

logger = logging.getLogger(__name__)

_PENDING_APPROVAL_KEYS: set[str] = set()
_PENDING_APPROVAL_LOCK = threading.Lock()


def _pending_gauge_key(
    thread_id: str | None,
    step_id: str,
    step_fp: str,
    pending_tool: dict[str, Any] | None,
    turn_id: str | None = None,
) -> str:
    tool_digest = str(pending_tool.get("args_digest") or "") if pending_tool else ""
    return f"{thread_id or ''}:{turn_id or ''}:{step_id}:{step_fp}:{tool_digest}"


def compute_step_fingerprint(step: Step) -> str:
    """Computes a cryptographic hash of step parameters to bind approval to exact contents."""
    raw = f"{step.id}:{step.agent}:{step.input_query}:{step.risk_level}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def compute_idempotency_key(
    step: Step,
    *,
    tenant_id: str,
    owner_id: str,
    thread_id: str | None = None,
    turn_id: str | None = None,
    step_fingerprint: str | None = None,
) -> str:
    """Computes a tenant-, owner-, thread-, and turn-scoped A2A idempotency key."""
    fp = step_fingerprint or compute_step_fingerprint(step)
    raw = (
        f"{step.id}:{step.agent}:{tenant_id}:{owner_id}:"
        f"{thread_id or 'no_thread'}:{turn_id or 'no_turn'}:{fp}"
    )
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
    pending_tool = state.pending_tool_approval if (
        isinstance(state.pending_tool_approval, dict)
        and state.pending_tool_approval.get("step_id") == high_risk_step.id
    ) else None

    # Check if previously approved with matching cryptographic fingerprint
    # (unless a specific tool-call approval was requested by the sub-agent).
    if (
        pending_tool is None
        and high_risk_step.id in state.approved_step_ids
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
    policy = get_policy_manager()
    agent_tools = policy.get_agent_tools(high_risk_step.agent)
    approval_tools = [t for t in agent_tools if policy.is_approval_required(t)]
    turn_id = str(state.metadata.get("turn_id") or state.last_turn_human_message_id or "")
    pending_key = _pending_gauge_key(
        state.thread_id, high_risk_step.id, step_fp, pending_tool, turn_id=turn_id
    )
    with _PENDING_APPROVAL_LOCK:
        already_pending = pending_key in _PENDING_APPROVAL_KEYS
    if not already_pending:
        HITL_PENDING_APPROVALS.inc()
    decremented = False
    try:
        interrupt_payload: dict[str, Any] = {
            "step_id": high_risk_step.id,
            "agent": high_risk_step.agent,
            "description": high_risk_step.description,
            "input_query": high_risk_step.input_query,
            "risk_level": high_risk_step.risk_level,
            "fingerprint": step_fp,
            "allowed_tools": agent_tools,
            "approval_required_tools": approval_tools,
        }
        if pending_tool is not None:
            interrupt_payload["tool_name"] = pending_tool.get("tool_name")
            interrupt_payload["tool_arguments"] = pending_tool.get("arguments")
            interrupt_payload["args_digest"] = pending_tool.get("args_digest")
        approval_data = interrupt(interrupt_payload)

        # When resumed, approval_data contains the user's decision
        approved = False
        updated_plan = state.plan
        if isinstance(approval_data, dict):
            approved = bool(approval_data.get("approved", False))
            modified_input = approval_data.get("modified_input")
            if modified_input is not None and str(modified_input).strip():
                from src.agentic.safety.guardrails import get_guardrails

                sanitized_input = get_guardrails().sanitize_input(str(modified_input))
                updated_steps = [
                    s.model_copy(update={"input_query": sanitized_input})
                    if s.id == high_risk_step.id
                    else s
                    for s in state.plan.steps
                ]
                updated_plan = state.plan.model_copy(update={"steps": updated_steps})
                high_risk_step = next(s for s in updated_steps if s.id == high_risk_step.id)
                step_fp = compute_step_fingerprint(high_risk_step)
        with _PENDING_APPROVAL_LOCK:
            _PENDING_APPROVAL_KEYS.discard(pending_key)
        HITL_PENDING_APPROVALS.dec()
        decremented = True
    except GraphInterrupt:
        # GraphInterrupt is the expected pause signal; record pending key once so
        # LangGraph node replay on resume does not double-increment the gauge.
        with _PENDING_APPROVAL_LOCK:
            if len(_PENDING_APPROVAL_KEYS) >= 8192:
                _PENDING_APPROVAL_KEYS.clear()
            _PENDING_APPROVAL_KEYS.add(pending_key)
        raise
    except Exception:
        with _PENDING_APPROVAL_LOCK:
            _PENDING_APPROVAL_KEYS.discard(pending_key)
        if not decremented:
            HITL_PENDING_APPROVALS.dec()
        raise

    approver_from_resume = (
        approval_data.get("approver_id") if isinstance(approval_data, dict) else None
    )
    actor_id = str(approver_from_resume or state.metadata.get("user_id") or "supervisor")
    reason_note = (
        str(approval_data.get("reason"))
        if isinstance(approval_data, dict) and approval_data.get("reason")
        else None
    )
    if approved:
        HITL_APPROVALS_TOTAL.labels(decision="approved", risk_level="high").inc()
        await asyncio.to_thread(
            record_audit_event,
            event_type="hitl_decision",
            actor_id=actor_id,
            thread_id=state.thread_id,
            step_id=high_risk_step.id,
            decision="approved",
            risk_level=high_risk_step.risk_level,
            details={"agent": high_risk_step.agent, "fingerprint": step_fp, "reason": reason_note},
        )
        new_ids = (
            state.approved_step_ids
            if high_risk_step.id in state.approved_step_ids
            else [*state.approved_step_ids, high_risk_step.id]
        )
        new_fps = {**state.approved_step_fingerprints, high_risk_step.id: step_fp}
        new_tool_digests = dict(state.approved_tool_call_digests)
        new_tool_calls = dict(state.approved_tool_calls)
        if pending_tool is not None and pending_tool.get("args_digest"):
            existing = list(new_tool_digests.get(high_risk_step.id, []))
            digest = str(pending_tool["args_digest"])
            if digest not in existing:
                existing.append(digest)
            new_tool_digests[high_risk_step.id] = existing
            new_tool_calls[high_risk_step.id] = {
                "tool_name": str(pending_tool.get("tool_name") or ""),
                "arguments": dict(pending_tool.get("arguments") or {}),
                "args_digest": digest,
            }
        return {
            "plan": updated_plan,
            "requires_approval": False,
            "approval_granted": True,
            "approved_step_ids": new_ids,
            "approved_step_fingerprints": new_fps,
            "approved_tool_call_digests": new_tool_digests,
            "approved_tool_calls": new_tool_calls,
            "pending_tool_approval": None,
            "pending_approval_step_id": None,
        }
    else:
        HITL_APPROVALS_TOTAL.labels(decision="rejected", risk_level="high").inc()
        await asyncio.to_thread(
            record_audit_event,
            event_type="hitl_decision",
            actor_id=actor_id,
            thread_id=state.thread_id,
            step_id=high_risk_step.id,
            decision="rejected",
            risk_level=high_risk_step.risk_level,
            details={"agent": high_risk_step.agent, "fingerprint": step_fp, "reason": reason_note},
        )
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
