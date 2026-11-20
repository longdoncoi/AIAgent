"""Domain-specific error hierarchy for Agentic Platform."""


class AgentError(Exception):
    """Base exception for all agentic platform errors."""

    def __init__(self, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class GuardrailViolation(AgentError):
    """Raised when an input or output violates guardrail policies."""

    def __init__(self, message: str, rule: str, details: dict | None = None) -> None:
        super().__init__(message, details)
        self.rule = rule


class HITLRequiredError(AgentError):
    """Raised when a task or action requires Human-In-The-Loop approval."""

    def __init__(
        self, step_id: str, action: str, risk_level: str, details: dict | None = None
    ) -> None:
        super().__init__(
            f"Action '{action}' in step '{step_id}' requires HITL approval (risk: {risk_level})",
            details,
        )
        self.step_id = step_id
        self.action = action
        self.risk_level = risk_level


class ToolExecutionError(AgentError):
    """Raised when an MCP tool fails during execution."""

    def __init__(self, tool_name: str, message: str, details: dict | None = None) -> None:
        super().__init__(f"Tool '{tool_name}' execution failed: {message}", details)
        self.tool_name = tool_name


class A2ACommunicationError(AgentError):
    """Raised when communication with a sub-agent over A2A protocol fails."""

    def __init__(
        self, target_agent: str, status_code: int | None = None, message: str = ""
    ) -> None:
        super().__init__(
            f"A2A communication error with '{target_agent}' (status={status_code}): {message}"
        )
        self.target_agent = target_agent
        self.status_code = status_code


class LLMGatewayError(AgentError):
    """Raised when calling LiteLLM, vLLM, or llama.cpp fails."""

    def __init__(self, alias: str, message: str, details: dict | None = None) -> None:
        super().__init__(f"LLM Gateway error for model '{alias}': {message}", details)
        self.alias = alias


class ConfigurationError(AgentError):
    """Raised when a required configuration file or parameter is missing/invalid."""

    pass
