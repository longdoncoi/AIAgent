"""State Schema definitions for LangGraph Supervisor."""

from typing import Annotated, Any, Literal

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field


class Step(BaseModel):
    """Represents a single granular task in the execution plan."""
    id: str = Field(..., description="Unique step identifier (e.g. step_1)")
    description: str = Field(..., description="Summary of the action to perform")
    agent: Literal["chat", "rag", "coding"] = Field(..., description="Sub-agent responsible")
    input_query: str = Field(..., description="Instruction prompt for the sub-agent")
    status: Literal["pending", "running", "completed", "failed", "interrupted"] = Field(default="pending")
    output: str | None = Field(default=None, description="Result output from sub-agent")
    risk_level: Literal["low", "medium", "high"] = Field(default="low")
    depends_on: list[str] = Field(default_factory=list, description="IDs of prerequisite steps before this step can run")


class Plan(BaseModel):
    """Execution plan formulated by the Planner node."""
    goal: str = Field(..., description="High-level goal derived from user query")
    steps: list[Step] = Field(default_factory=list, description="Ordered or DAG steps")


class AgentState(BaseModel):
    """Global execution state for the LangGraph Supervisor Orchestrator."""
    messages: Annotated[list[BaseMessage], add_messages] = Field(default_factory=list)
    plan: Plan | None = None
    current_step_index: int = 0
    reflection_notes: list[str] = Field(default_factory=list)
    reflection_count: int = 0
    requires_approval: bool = False
    pending_approval_step_id: str | None = None
    approval_granted: bool | None = None
    final_answer: str | None = None
    thread_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
