"""Production hardening regressions v2 — trust boundaries and A2A authorization.

Covers the four security properties that must hold at every trust boundary:

* ``X-Forwarded-For`` is only trusted behind a configured proxy;
* HITL authorization requires a signed, step-scoped capability token;
* JWTs must carry the full claim set and a recognised role;
* rate-limiter configuration and Lua results fail closed.
"""

from __future__ import annotations

import ipaddress
import json
import time
from typing import Any
from unittest.mock import patch

import jwt
import pytest
from fastapi import HTTPException
from langchain_core.messages import AIMessage
from starlette.requests import Request

from src.agentic.a2a.cards import CODING_AGENT_CARD
from src.agentic.a2a.executor import A2AAgentExecutor, A2ATaskPayload, A2ATaskRequest
from src.agentic.api import middleware
from src.agentic.api.deps import get_current_user
from src.agentic.api.middleware import SlidingWindowRateLimiter, _parse_retry, resolve_client_ip
from src.agentic.core.config import get_settings
from src.agentic.safety import approvals
from src.agentic.safety.approvals import issue_approval_grant, verify_approval_grant
from src.agentic.tools.mcp_client import MCPClientManager


def _request_with_peer(peer: str, xff: str | None = None) -> Request:
    """Build a minimal ASGI request with a known peer address."""
    headers: list[tuple[bytes, bytes]] = []
    if xff is not None:
        headers.append((b"x-forwarded-for", xff.encode("utf-8")))
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": (peer, 51000),
        "server": ("testserver", 80),
    }
    return Request(scope)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 1. X-Forwarded-For spoofing
# ---------------------------------------------------------------------------


def test_xff_is_ignored_when_no_trusted_proxies(monkeypatch: pytest.MonkeyPatch) -> None:
    """A direct client cannot spoof its IP by sending X-Forwarded-For."""
    monkeypatch.setattr(middleware, "_TRUSTED_PROXIES", ())
    request = _request_with_peer("203.0.113.9", "1.2.3.4")
    assert resolve_client_ip(request) == "203.0.113.9"


def test_xff_is_walked_from_the_right_behind_a_trusted_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only chains arriving from a trusted proxy reveal the original client."""
    monkeypatch.setattr(
        middleware, "_TRUSTED_PROXIES", (ipaddress.ip_network("10.0.0.0/8"),)
    )
    request = _request_with_peer("10.0.0.1", "1.2.3.4, 10.0.0.2")
    assert resolve_client_ip(request) == "1.2.3.4"


# ---------------------------------------------------------------------------
# 2. Signed approval grants
# ---------------------------------------------------------------------------


def _grant_claims(token: str) -> dict[str, Any]:
    body, _signature = token.rsplit(".", 1)
    return json.loads(approvals._b64decode(body))


def test_approval_grant_fails_closed_for_forged_expired_and_mismatched_tokens() -> None:
    token = issue_approval_grant(
        step_id="step_1",
        fingerprint="fp",
        owner_id="alice",
        agent_name="coding_agent",
        tools=["repo_write_file"],
    )
    claims = _grant_claims(token)
    # A caller-supplied boolean is never part of the signed claim set.
    assert "hitl_approved" not in claims

    common = {
        "step_id": "step_1",
        "fingerprint": "fp",
        "owner_id": "alice",
        "agent_name": "coding_agent",
    }
    assert verify_approval_grant(token + "forged", **common) is None

    expired = issue_approval_grant(
        step_id="step_1",
        fingerprint="fp",
        owner_id="alice",
        agent_name="coding_agent",
        tools=["repo_write_file"],
        ttl_seconds=-60,
    )
    assert verify_approval_grant(expired, **common) is None
    assert verify_approval_grant(token, **{**common, "step_id": "another_step"}) is None
    assert verify_approval_grant(token, **{**common, "fingerprint": "other_fp"}) is None
    assert verify_approval_grant(
        token, **common, tool_name="code_exec_bash"
    ) is None


def test_approval_grant_is_accepted_with_matching_bindings() -> None:
    token = issue_approval_grant(
        step_id="step_1",
        fingerprint="fp",
        owner_id="alice",
        agent_name="coding_agent",
        tools=["repo_write_file"],
    )
    grant = verify_approval_grant(
        token,
        step_id="step_1",
        fingerprint="fp",
        owner_id="alice",
        agent_name="coding_agent",
        tool_name="repo_write_file",
    )
    assert grant is not None
    assert grant.step_id == "step_1"
    assert grant.owner_id == "alice"


# ---------------------------------------------------------------------------
# 3. A2A executor authorization boundary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a2a_executor_ignores_caller_boolean_and_accepts_signed_grant() -> None:
    """Only the HITL-issued token crosses the A2A boundary as authorization."""
    manager = MCPClientManager()

    async def harmless_write(**kwargs: Any) -> str:
        return "ok"

    # Force the local MCP implementation: no SSE server exists in unit tests.
    manager._local["repo_write_file"] = harmless_write
    tool = manager.get_tool("repo_write_file")
    assert tool is not None

    outcomes: list[tuple[str, str]] = []

    class ToolCallingGraph:
        async def ainvoke(self, state: Any) -> dict[str, Any]:
            try:
                result = await tool.ainvoke({"file_path": "safe.txt", "content": "hello"})
            except PermissionError as exc:
                outcomes.append(("denied", str(exc)))
                raise
            outcomes.append(("allowed", result))
            return {"messages": [AIMessage(content=result)]}

    executor = A2AAgentExecutor(card=CODING_AGENT_CARD, graph=ToolCallingGraph())

    with patch.object(manager.settings, "use_remote_mcp", False):
        forge_req = A2ATaskRequest(
            task_id="step_42",
            caller="supervisor",
            target_agent="coding_agent",
            payload=A2ATaskPayload(instruction="write safe.txt"),
            metadata={"hitl_approved": True},
        )
        denied = await executor.execute(forge_req)
        assert denied.status == "failed"
        assert outcomes[-1][0] == "denied"
        assert "requires step-scoped HITL approval" in outcomes[-1][1]

        grant = issue_approval_grant(
            step_id="step_42",
            fingerprint="fp",
            owner_id="alice",
            agent_name="coding_agent",
            tools=["repo_write_file"],
        )
        signed_req = A2ATaskRequest(
            task_id="step_42",
            caller="supervisor",
            target_agent="coding_agent",
            payload=A2ATaskPayload(instruction="write safe.txt"),
            metadata={
                "approval_token": grant,
                "owner_id": "alice",
                "step_fingerprint": "fp",
            },
        )
        outcomes.clear()
        allowed = await executor.execute(signed_req)
        assert allowed.status == "completed"
        assert outcomes[-1] == ("allowed", "ok")


# ---------------------------------------------------------------------------
# 4. JWT claim validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_jwt_without_audience_or_issuer_is_rejected() -> None:
    settings = get_settings()
    now = int(time.time())
    token = jwt.encode(
        {"sub": "alice", "role": "admin", "iat": now, "exp": now + 300},
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )
    with pytest.raises(HTTPException) as exc_info:
        await get_current_user(f"Bearer {token}")
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_jwt_with_unknown_role_is_forbidden() -> None:
    settings = get_settings()
    now = int(time.time())
    token = jwt.encode(
        {
            "sub": "alice",
            "role": "superadmin",
            "iat": now,
            "exp": now + 300,
            "aud": settings.jwt_audience,
            "iss": settings.jwt_issuer,
        },
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )
    with pytest.raises(HTTPException) as exc_info:
        await get_current_user(f"Bearer {token}")
    assert exc_info.value.status_code == 403


# ---------------------------------------------------------------------------
# 5. Rate limiter
# ---------------------------------------------------------------------------


def test_rate_limiter_rejects_non_positive_limit() -> None:
    with pytest.raises(ValueError, match="limit_per_minute must be >= 1"):
        SlidingWindowRateLimiter(limit_per_minute=0)


def test_negative_lua_retry_is_normalised_to_positive_seconds() -> None:
    assert _parse_retry(-5) == (False, 5)
    allowed, retry_after = _parse_retry(-12)
    assert allowed is False
    assert retry_after > 0
