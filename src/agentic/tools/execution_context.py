"""Request-scoped authorization context propagated across A2A tool calls."""

from contextvars import ContextVar, Token
from dataclasses import dataclass


@dataclass(frozen=True)
class ToolExecutionContext:
    agent_name: str
    step_id: str
    hitl_approved: bool = False
    owner_id: str | None = None


_context: ContextVar[ToolExecutionContext | None] = ContextVar(
    "agentic_tool_execution_context", default=None
)


def set_tool_execution_context(
    value: ToolExecutionContext,
) -> Token[ToolExecutionContext | None]:
    return _context.set(value)


def reset_tool_execution_context(token: Token[ToolExecutionContext | None]) -> None:
    _context.reset(token)


def get_tool_execution_context() -> ToolExecutionContext | None:
    return _context.get()
