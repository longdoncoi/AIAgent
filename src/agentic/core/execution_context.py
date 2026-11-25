"""Request-scoped identity and authorization context shared by all layers."""

from contextvars import ContextVar, Token
from dataclasses import dataclass


@dataclass(frozen=True)
class ToolExecutionContext:
    """Verified authorization and cost-allocation context for one delegated step."""

    agent_name: str
    step_id: str
    approval_token: str | None = None
    owner_id: str | None = None
    step_fingerprint: str | None = None
    user_roles: tuple[str, ...] = ()
    department: str | None = None
    client_id: str | None = None
    tenant_id: str | None = None


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


def cost_allocation_tags(environment: str) -> dict[str, str]:
    """Return bounded billing tags derived only from verified context."""
    context = get_tool_execution_context()
    return {
        "client_id": str(context.client_id or "unattributed")[:128]
        if context
        else "unattributed",
        "department": str(context.department or "unknown")[:128]
        if context
        else "unknown",
        "env": str(environment)[:32],
        "agent": str(context.agent_name)[:64] if context else "unknown",
    }
