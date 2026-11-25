"""Assembly and compilation of the LangGraph Supervisor Orchestrator Graph."""

import logging
from typing import Any, Literal

from langgraph.graph import END, START, StateGraph

from src.agentic.core.config import get_settings
from src.agentic.memory.short_term import get_checkpointer
from src.agentic.orchestrator.nodes.dispatch import dispatch_node
from src.agentic.orchestrator.nodes.finalize import fast_finalize_node, finalize_node
from src.agentic.orchestrator.nodes.hitl import hitl_node
from src.agentic.orchestrator.nodes.planner import planner_node
from src.agentic.orchestrator.nodes.reflector import reflector_node
from src.agentic.orchestrator.state import MAX_REFLECTION_ATTEMPTS, AgentState

logger = logging.getLogger(__name__)


def _is_simple_chat_fast_path(state: AgentState) -> bool:
    """True for single-step low-risk chat turns that need no extra judge/synthesis hops."""
    settings = get_settings()
    if not getattr(settings, "enable_chat_fast_path", True):
        return False
    if state.degraded or state.reflection_count != 0 or not state.plan:
        return False
    if len(state.plan.steps) != 1:
        return False
    step = state.plan.steps[0]
    return (
        step.agent == "chat"
        and step.risk_level == "low"
        and step.status == "completed"
        and bool((step.output or "").strip())
    )


def should_continue_steps(state: AgentState) -> Literal["hitl", "fast_finalize", "reflector"]:
    """Determines whether to run more steps, take conversational fast-path, or reflect."""
    if not state.plan or not state.plan.steps:
        return "reflector"

    has_pending = any(s.status == "pending" for s in state.plan.steps)
    if has_pending:
        return "hitl"
    if _is_simple_chat_fast_path(state):
        return "fast_finalize"
    return "reflector"


def should_replan_or_finalize(state: AgentState) -> Literal["planner", "hitl", "finalize"]:
    """Routes after reflection: bounded retry/replan, or fail-closed finalization.

    * ``degraded`` (judge unavailable) never finalizes while budget remains —
      it re-plans first, then hands off to Finalize for manual review.
    * ``retry`` keeps the plan and re-enters HITL/dispatch for failed steps.
    * ``replan`` discards the plan and returns to the Planner.
    """
    latest_note = state.reflection_notes[-1] if state.reflection_notes else ""

    if state.degraded:
        if state.reflection_count < MAX_REFLECTION_ATTEMPTS:
            logger.warning(
                "Reflector degraded (reason=%s); re-planning with budget %d/%d.",
                state.degraded_reason,
                state.reflection_count,
                MAX_REFLECTION_ATTEMPTS,
            )
            return "planner"
        logger.error(
            "Reflector still degraded after %d attempts (reason=%s); "
            "routing to Finalize for manual review — outputs are NOT verified.",
            state.reflection_count,
            state.degraded_reason,
        )
        return "finalize"

    if state.reflection_count >= MAX_REFLECTION_ATTEMPTS:
        return "finalize"

    if "RETRY_REQUIRED" in latest_note:
        logger.info("Reflector requested retry of failed steps; re-entering HITL/dispatch.")
        return "hitl"
    if "REPLAN_REQUIRED" in latest_note or "replan_required" in latest_note.lower():
        logger.info("Reflector triggered self-correction loop back to Planner.")
        return "planner"
    return "finalize"


def create_supervisor_graph(checkpointer: Any | None = None) -> Any:
    """Constructs and compiles the Supervisor StateGraph."""
    workflow = StateGraph(AgentState)

    # Register nodes
    workflow.add_node("planner", planner_node)
    workflow.add_node("hitl", hitl_node)
    workflow.add_node("dispatch", dispatch_node)
    workflow.add_node("reflector", reflector_node)
    workflow.add_node("finalize", finalize_node)
    workflow.add_node("fast_finalize", fast_finalize_node)

    # Connect workflow edges
    workflow.add_edge(START, "planner")
    workflow.add_edge("planner", "hitl")
    workflow.add_edge("hitl", "dispatch")

    workflow.add_conditional_edges(
        "dispatch",
        should_continue_steps,
        {
            "hitl": "hitl",
            "fast_finalize": "fast_finalize",
            "reflector": "reflector",
        },
    )

    workflow.add_conditional_edges(
        "reflector",
        should_replan_or_finalize,
        {
            "planner": "planner",
            "hitl": "hitl",
            "finalize": "finalize",
        },
    )
    workflow.add_edge("fast_finalize", END)
    workflow.add_edge("finalize", END)

    saver = checkpointer or get_checkpointer()
    compiled_graph = workflow.compile(checkpointer=saver)
    logger.info("Compiled Supervisor LangGraph with active checkpointer.")
    return compiled_graph


_supervisor_graph_instance: Any = None


def get_supervisor_graph(checkpointer: Any | None = None) -> Any:
    """Returns singleton instance of the compiled Supervisor Graph."""
    global _supervisor_graph_instance
    if _supervisor_graph_instance is None:
        _supervisor_graph_instance = create_supervisor_graph(checkpointer=checkpointer)
    return _supervisor_graph_instance
