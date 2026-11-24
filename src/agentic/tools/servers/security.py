"""ASGI authorization wrapper shared by network-exposed MCP servers.

Transport authentication is necessary but not sufficient for dangerous MCP
tools.  ``tools/call`` requests for approval-gated tools are therefore checked
again at this final trust boundary using a one-time grant bound to the exact
tool name and arguments.
"""

import hmac
import json
import logging
from typing import Any, cast

from src.agentic.core.config import get_settings
from src.agentic.safety.approvals import verify_tool_call_grant
from src.agentic.safety.policy import get_policy_manager

logger = logging.getLogger(__name__)


class MCPServiceAuthMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app
        self.settings = get_settings()

    @staticmethod
    async def _ok(send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    @staticmethod
    async def _deny(send: Any, status: int, detail: str) -> None:
        body = json.dumps({"detail": detail}).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    async def _read_body(receive: Any) -> tuple[bytes, Any]:
        """Buffer one HTTP body and return a replayable receive callable."""
        messages: list[dict[str, Any]] = []
        chunks: list[bytes] = []
        while True:
            message = await receive()
            messages.append(message)
            if message.get("type") != "http.request":
                break
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break

        async def replay() -> dict[str, Any]:
            if messages:
                return messages.pop(0)
            return cast(dict[str, Any], await receive())

        return b"".join(chunks), replay

    async def _authorize_tool_call(
        self, headers: dict[bytes, bytes], body: bytes, send: Any
    ) -> bool:
        """Fail closed for approval-gated JSON-RPC ``tools/call`` requests."""
        if not body:
            return True
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, ValueError):
            return True  # Let the MCP implementation return its protocol error.

        requests = payload if isinstance(payload, list) else [payload]
        policy = get_policy_manager()
        for request in requests:
            if not isinstance(request, dict) or request.get("method") != "tools/call":
                continue
            params = request.get("params") or {}
            tool_name = str(params.get("name") or "")
            arguments = params.get("arguments") or {}
            if not policy.is_approval_required(tool_name):
                continue
            if not isinstance(arguments, dict):
                await self._deny(send, 403, "Invalid tool arguments for approval validation.")
                return False
            token = headers.get(b"x-approval-grant", b"").decode("utf-8")
            if verify_tool_call_grant(
                token,
                tool_name=tool_name,
                arguments=arguments,
                settings=self.settings,
            ) is None:
                logger.warning("Denied MCP call to approval-gated tool '%s'", tool_name)
                await self._deny(send, 403, "Valid call-scoped HITL approval required.")
                return False
        return True

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "http":
            # Auth-free, information-free endpoint for Kubernetes probes.
            if scope.get("path") in {"/healthz", "/health"}:
                await self._ok(send)
                return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        if scope.get("type") == "http" and self.settings.require_authentication:
            supplied = headers.get(b"x-internal-service-token", b"").decode("utf-8")
            if not hmac.compare_digest(supplied, self.settings.s2s_internal_token):
                await self._deny(send, 401, "Unauthorized")
                return
        if scope.get("type") == "http" and scope.get("method") == "POST":
            body, replay_receive = await self._read_body(receive)
            if not await self._authorize_tool_call(headers, body, send):
                return
            receive = replay_receive
        await self.app(scope, receive, send)


def secure_mcp_app(app: Any) -> MCPServiceAuthMiddleware:
    return MCPServiceAuthMiddleware(app)
