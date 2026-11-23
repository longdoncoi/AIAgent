"""ASGI authentication wrapper shared by network-exposed MCP servers."""

import hmac
from typing import Any

from src.agentic.core.config import get_settings
from src.agentic.safety.capabilities import verify_tool_capability
from src.agentic.safety.policy import get_policy_manager
from src.agentic.tools.execution_context import (
    ToolExecutionContext,
    get_tool_execution_context,
    reset_tool_execution_context,
    set_tool_execution_context,
)


async def _reject(send: Any, status: int, detail: str) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    body = ('{"detail":"' + detail + '"}').encode("utf-8")
    await send({"type": "http.response.body", "body": body})


def authorize_tool_execution(tool_name: str) -> None:
    """Enforce authorization again at the network-exposed MCP boundary."""
    settings = get_settings()
    context = get_tool_execution_context()
    if context is None:
        if settings.require_authentication:
            raise PermissionError("Authenticated MCP execution context is required.")
        return
    policy = get_policy_manager()
    if not policy.is_tool_allowed(context.agent_name, tool_name):
        raise PermissionError(f"Tool '{tool_name}' is not allowed for this agent.")
    if policy.is_approval_required(tool_name) and (
        not context.hitl_approved or tool_name not in context.allowed_tools
    ):
        raise PermissionError(f"Tool '{tool_name}' requires a valid step-scoped capability.")


class MCPServiceAuthMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app
        self.settings = get_settings()

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        context_token = None
        if scope.get("type") == "http":
            headers = {key.lower(): value for key, value in scope.get("headers", [])}
            supplied = headers.get(b"x-internal-service-token", b"").decode("utf-8")
            if self.settings.require_authentication and not hmac.compare_digest(
                supplied, self.settings.s2s_internal_token
            ):
                await _reject(send, 401, "Unauthorized")
                return

            agent_name = headers.get(b"x-agent-name", b"").decode("utf-8")
            step_id = headers.get(b"x-step-id", b"").decode("utf-8")
            owner_id = headers.get(b"x-owner-id", b"").decode("utf-8")
            run_id = headers.get(b"x-run-id", b"").decode("utf-8")
            capability = headers.get(b"x-approval-capability", b"").decode("utf-8") or None
            if self.settings.require_authentication and not all(
                (agent_name, step_id, owner_id, run_id)
            ):
                await _reject(send, 400, "Missing execution identity headers")
                return
            if agent_name and step_id:
                claims = verify_tool_capability(
                    capability,
                    run_id=run_id,
                    step_id=step_id,
                    owner_id=owner_id,
                    agent_name=agent_name,
                )
                if capability and claims is None:
                    await _reject(send, 403, "Invalid approval capability")
                    return
                context_token = set_tool_execution_context(
                    ToolExecutionContext(
                        agent_name=agent_name,
                        step_id=step_id,
                        hitl_approved=bool(claims and claims.allowed_tools),
                        owner_id=owner_id or None,
                        run_id=run_id or None,
                        approval_capability=capability if claims else None,
                        allowed_tools=claims.allowed_tools if claims else (),
                    )
                )
        try:
            await self.app(scope, receive, send)
        finally:
            if context_token is not None:
                reset_tool_execution_context(context_token)


def secure_mcp_app(app: Any) -> MCPServiceAuthMiddleware:
    return MCPServiceAuthMiddleware(app)
