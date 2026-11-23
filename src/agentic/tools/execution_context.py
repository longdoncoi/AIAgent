"""Request-scoped authorization context propagated across A2A tool calls."""

from contextvars import ContextVar, Token
from dataclasses import dataclass


@dataclass(frozen=True)
class ToolExecutionContext:
    """Authorization context for one tool execution.

    ``approval_token`` is a signed capability token minted by the supervisor
    after an actual HITL decision. It replaces the former caller-controlled
    ``hitl_approved`` boolean, which could be forged by any A2A caller.
    """

    agent_name: str
    step_id: str
    approval_token: str | None = None
    owner_id: str | None = None
    step_fingerprint: str | None = None


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
