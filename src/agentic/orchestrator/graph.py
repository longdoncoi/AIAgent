"""Assembly and compilation of the LangGraph Supervisor Orchestrator Graph."""

import logging
from typing import Any, Literal

from langgraph.graph import END, START, StateGraph

from src.agentic.memory.short_term import get_checkpointer
from src.agentic.orchestrator.nodes.dispatch import dispatch_node
from src.agentic.orchestrator.nodes.finalize import finalize_node
from src.agentic.orchestrator.nodes.hitl import hitl_node
from src.agentic.orchestrator.nodes.planner import planner_node
from src.agentic.orchestrator.nodes.reflector import reflector_node
from src.agentic.orchestrator.state import AgentState

logger = logging.getLogger(__name__)


def should_continue_steps(state: AgentState) -> Literal["hitl", "reflector"]:
    """Determines if there are more steps to execute or if we should proceed to reflection."""
    if not state.plan or not state.plan.steps:
        return "reflector"

    has_pending = any(s.status == "pending" for s in state.plan.steps)
    if has_pending:
        return "hitl"
    return "reflector"


def should_replan_or_finalize(state: AgentState) -> Literal["planner", "finalize"]:
    """Determines whether to loop back to planner for self-correction or proceed to finalization."""
    if state.reflection_count < 2 and state.reflection_notes:
        latest_note = state.reflection_notes[-1].lower()
        if "replan_required" in latest_note or "retry" in latest_note:
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

    # Connect workflow edges
    workflow.add_edge(START, "planner")
    workflow.add_edge("planner", "hitl")
    workflow.add_edge("hitl", "dispatch")

    workflow.add_conditional_edges(
        "dispatch",
        should_continue_steps,
        {
            "hitl": "hitl",
            "reflector": "reflector",
        },
    )

    workflow.add_conditional_edges(
        "reflector",
        should_replan_or_finalize,
        {
            "planner": "planner",
            "finalize": "finalize",
        },
    )
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
