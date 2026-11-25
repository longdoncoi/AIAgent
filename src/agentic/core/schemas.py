"""Core Pydantic data schemas used across all system layers."""

from typing import Any, Literal

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    """User incoming chat request."""

    query: str = Field(..., max_length=50000, description="User query or instruction")
    thread_id: str | None = Field(
        default=None, max_length=128, description="Conversation session ID for checkpointing"
    )
    user_id: str | None = Field(default="default_user", max_length=128, description="Identifier of the requester")
    stream: bool = Field(default=True, description="Whether to stream response via SSE")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Custom request metadata")


class StepResult(BaseModel):
    """Result of an individual plan step execution."""

    step_id: str
    agent: str
    output: str
    status: Literal["completed", "failed", "pending", "running", "interrupted"] = "completed"
    error: str | None = None
    duration_seconds: float = 0.0


class ChatResponse(BaseModel):
    """Final response returned to caller."""

    thread_id: str
    answer: str
    steps: list[StepResult] = Field(default_factory=list)
    requires_approval: bool = False
    pending_approval_step: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SSEEvent(BaseModel):
    """Server-Sent Event payload."""

    event: Literal[
        "plan",
        "step_start",
        "step_progress",
        "step_complete",
        "hitl_required",
        "reflection",
        "token",
        "final",
        "error",
    ]
    data: dict[str, Any]


class Decision(BaseModel):
    """Human decision payload for approval checkpoints."""

    approved: bool = Field(..., description="Whether the action is approved or rejected")
    reason: str | None = Field(default=None, description="Feedback or rejection rationale")
    modified_input: str | None = Field(default=None, description="Optional modified instruction")


class ApprovalRequest(BaseModel):
    """Payload to resume a suspended HITL execution."""

    thread_id: str
    step_id: str
    decision: Decision


class ApprovalResponse(BaseModel):
    """Acknowledgement of approval decision."""

    thread_id: str
    status: Literal["resumed", "aborted"]
    message: str
    final_answer: str | None = None
    requires_approval: bool = False
    pending_approval_step: str | None = None
