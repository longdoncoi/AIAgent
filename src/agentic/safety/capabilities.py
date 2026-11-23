"""Short-lived, step-scoped authorization capabilities for privileged tools."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any

from src.agentic.core.config import get_settings


@dataclass(frozen=True)
class CapabilityClaims:
    """Verified authorization claims propagated across A2A and MCP boundaries."""

    run_id: str
    step_id: str
    owner_id: str
    agent_name: str
    allowed_tools: tuple[str, ...]
    expires_at: int


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _sign(payload: bytes) -> bytes:
    key = get_settings().checkpoint_signing_key.encode("utf-8")
    return hmac.new(key, b"agentic-tool-capability\x00" + payload, hashlib.sha256).digest()


def issue_tool_capability(
    *,
    run_id: str,
    step_id: str,
    owner_id: str,
    agent_name: str,
    allowed_tools: list[str],
    ttl_seconds: int = 120,
) -> str:
    """Issue an expiring capability bound to one execution step and principal."""
    if not all((run_id, step_id, owner_id, agent_name)):
        raise ValueError("Capability bindings must be non-empty.")
    now = int(time.time())
    body = {
        "v": 1,
        "run_id": run_id,
        "step_id": step_id,
        "owner_id": owner_id,
        "agent_name": agent_name,
        "allowed_tools": sorted(set(allowed_tools)),
        "iat": now,
        "exp": now + max(1, min(ttl_seconds, 300)),
    }
    payload = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"{_b64encode(payload)}.{_b64encode(_sign(payload))}"


def verify_tool_capability(
    token: str | None,
    *,
    run_id: str,
    step_id: str,
    owner_id: str,
    agent_name: str,
) -> CapabilityClaims | None:
    """Verify signature, expiry, and all request bindings; return no partial trust."""
    if not token:
        return None
    try:
        encoded_payload, encoded_signature = token.split(".", 1)
        payload = _b64decode(encoded_payload)
        signature = _b64decode(encoded_signature)
        if not hmac.compare_digest(signature, _sign(payload)):
            return None
        body: dict[str, Any] = json.loads(payload)
        now = int(time.time())
        if body.get("v") != 1 or int(body.get("exp", 0)) < now:
            return None
        if int(body.get("iat", 0)) > now + 30:
            return None
        expected = {
            "run_id": run_id,
            "step_id": step_id,
            "owner_id": owner_id,
            "agent_name": agent_name,
        }
        if any(not value or body.get(key) != value for key, value in expected.items()):
            return None
        allowed_tools = body.get("allowed_tools")
        if not isinstance(allowed_tools, list) or not all(
            isinstance(item, str) and item for item in allowed_tools
        ):
            return None
        return CapabilityClaims(
            run_id=run_id,
            step_id=step_id,
            owner_id=owner_id,
            agent_name=agent_name,
            allowed_tools=tuple(allowed_tools),
            expires_at=int(body["exp"]),
        )
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None
