"""Core definitions, settings, error classes, and schemas."""

from src.agentic.core.config import Settings, get_settings
from src.agentic.core.errors import (
    A2ACommunicationError,
    AgentError,
    GuardrailViolation,
    HITLRequiredError,
    LLMGatewayError,
    ToolExecutionError,
)
from src.agentic.core.redis import (
    close_redis_pools,
    get_async_redis_client,
    get_redis_client,
    get_redis_connection_pool,
)
from src.agentic.core.schemas import (
    ApprovalRequest,
    ApprovalResponse,
    ChatRequest,
    ChatResponse,
    Decision,
    SSEEvent,
    StepResult,
)

__all__ = [
    "Settings",
    "get_settings",
    "AgentError",
    "GuardrailViolation",
    "HITLRequiredError",
    "ToolExecutionError",
    "A2ACommunicationError",
    "LLMGatewayError",
    "get_redis_connection_pool",
    "get_redis_client",
    "get_async_redis_client",
    "close_redis_pools",
    "ChatRequest",
    "ChatResponse",
    "SSEEvent",
    "Decision",
    "ApprovalRequest",
    "ApprovalResponse",
    "StepResult",
]
