"""State Schema definitions for LangGraph Supervisor."""

from typing import Annotated, Any, Literal

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field, model_validator

# Bounded self-correction budget shared by the Reflector node and the graph router.
MAX_REFLECTION_ATTEMPTS = 2


class Step(BaseModel):
    """Represents a single granular task in the execution plan."""

    id: str = Field(..., description="Unique step identifier (e.g. step_1)")
    description: str = Field(..., description="Summary of the action to perform")
    agent: Literal["chat", "rag", "coding"] = Field(..., description="Sub-agent responsible")
    input_query: str = Field(..., description="Instruction prompt for the sub-agent")
    # NOTE: "running" is an internal, transient status used by dispatch while a
    # step is in flight. It is never a valid final/DTO status — use
    # ``public_status`` when emitting StepResult to the API layer (the API
    # currently passes ``status`` through directly and needs a coordinated fix
    # in api/routes/chat.py or core/schemas.py; see review hand-off notes).
    status: Literal["pending", "running", "completed", "failed", "interrupted"] = Field(
        default="pending"
    )
    output: str | None = Field(default=None, description="Result output from sub-agent")
    risk_level: Literal["low", "medium", "high"] = Field(default="low")
    depends_on: list[str] = Field(
        default_factory=list, description="IDs of prerequisite steps before this step can run"
    )

    @property
    def public_status(self) -> Literal["pending", "completed", "failed", "interrupted"]:
        """DTO-safe status: collapse the transient 'running' state to 'pending'."""
        return "pending" if self.status == "running" else self.status


class Plan(BaseModel):
    """Execution plan formulated by the Planner node."""

    goal: str = Field(..., description="High-level goal derived from user query")
    steps: list[Step] = Field(default_factory=list, description="Ordered or DAG steps")

    @model_validator(mode="after")
    def validate_dag(self) -> "Plan":
        """Reject ambiguous IDs, unknown dependencies, self edges, and cycles."""
        ids = [step.id for step in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("Plan step IDs must be unique.")
        known = set(ids)
        graph: dict[str, list[str]] = {}
        for step in self.steps:
            unknown = set(step.depends_on) - known
            if unknown:
                raise ValueError(f"Step '{step.id}' has unknown dependencies: {sorted(unknown)}")
            if step.id in step.depends_on:
                raise ValueError(f"Step '{step.id}' cannot depend on itself.")
            graph[step.id] = step.depends_on

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node: str) -> None:
            if node in visiting:
                raise ValueError("Plan dependency graph contains a cycle.")
            if node in visited:
                return
            visiting.add(node)
            for dependency in graph[node]:
                visit(dependency)
            visiting.remove(node)
            visited.add(node)

        for step_id in ids:
            visit(step_id)
        return self


class AgentState(BaseModel):
    """Global execution state for the LangGraph Supervisor Orchestrator."""

    messages: Annotated[list[BaseMessage], add_messages] = Field(default_factory=list)
    plan: Plan | None = None
    # Retained for checkpoint/back-compatibility. No current node uses this to
    # decide routing — dispatch tracks executable steps via plan step statuses.
    current_step_index: int = 0
    reflection_notes: list[str] = Field(default_factory=list)
    reflection_count: int = 0
    # Fail-closed signal: set by the Reflector when the LLM judge is unavailable.
    # A degraded run must never be finalized as if outputs were verified.
    degraded: bool = False
    degraded_reason: str | None = None
    requires_approval: bool = False
    pending_approval_step_id: str | None = None
    approval_granted: bool | None = None
    approved_step_ids: list[str] = Field(default_factory=list)
    approved_step_fingerprints: dict[str, str] = Field(default_factory=dict)
    final_answer: str | None = None
    thread_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
