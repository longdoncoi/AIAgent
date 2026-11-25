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
    "A2ACommunicationError",
    "AgentError",
    "ApprovalRequest",
    "ApprovalResponse",
    "ChatRequest",
    "ChatResponse",
    "Decision",
    "GuardrailViolation",
    "HITLRequiredError",
    "LLMGatewayError",
    "SSEEvent",
    "Settings",
    "StepResult",
    "ToolExecutionError",
    "close_redis_pools",
    "get_async_redis_client",
    "get_redis_client",
    "get_redis_connection_pool",
    "get_settings",
]
