"""ASGI authentication wrapper shared by network-exposed MCP servers."""

import hmac
from typing import Any

from src.agentic.core.config import get_settings


class MCPServiceAuthMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app
        self.settings = get_settings()

    @staticmethod
    async def _ok(send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "http":
            # Auth-free, information-free endpoint for Kubernetes probes.
            if scope.get("path") in {"/healthz", "/health"}:
                await self._ok(send)
                return
        if scope.get("type") == "http" and self.settings.require_authentication:
            headers = {key.lower(): value for key, value in scope.get("headers", [])}
            supplied = headers.get(b"x-internal-service-token", b"").decode("utf-8")
            if not hmac.compare_digest(supplied, self.settings.s2s_internal_token):
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [(b"content-type", b"application/json")],
                    }
                )
                await send(
                    {"type": "http.response.body", "body": b'{"detail":"Unauthorized"}'}
                )
                return
        await self.app(scope, receive, send)


def secure_mcp_app(app: Any) -> MCPServiceAuthMiddleware:
    return MCPServiceAuthMiddleware(app)
